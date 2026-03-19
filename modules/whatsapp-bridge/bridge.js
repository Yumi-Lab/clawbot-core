'use strict';
/**
 * WhatsApp Bridge for ClawbotCore
 * Connects Baileys (WhatsApp Web) to ClawbotCore via HTTP.
 *
 * Endpoints:
 *   GET  /status        → { connected, status, phone, qr }
 *   POST /send          → { to, text }
 *   POST /send-audio    → { to, audio_path }
 *
 * Inbound messages are forwarded to:
 *   POST http://127.0.0.1:8090/v1/channels/whatsapp/inbound
 */

const {
  default: makeWASocket,
  useMultiFileAuthState,
  DisconnectReason,
  downloadMediaMessage,
} = require('@whiskeysockets/baileys');
const express = require('express');
const QRCode = require('qrcode');
const fs = require('fs');
const path = require('path');
const http = require('http');

const BRIDGE_PORT = 3100;
const CLAWBOT_HOST = '127.0.0.1';
const CLAWBOT_PORT = 8090;
const CLAWBOT_INBOUND_PATH = '/v1/channels/whatsapp/inbound';
const AUTH_DIR = process.env.AUTH_DIR || path.join(__dirname, 'auth');
const MAX_RECONNECTS = 5;
const RECONNECT_DELAY_MS = 3000;

// ── State ─────────────────────────────────────────────────────────────────────
let sock = null;
let lastQr = null;           // data:image/png;base64,...
let connStatus = 'disconnected'; // 'disconnected' | 'qr_pending' | 'connected' | 'error'
let phoneNumber = null;      // '+33612345678'
let reconnectCount = 0;

// ── Baileys socket ─────────────────────────────────────────────────────────────
async function createWASocket() {
  const { state, saveCreds } = await useMultiFileAuthState(AUTH_DIR);

  sock = makeWASocket({
    auth: state,
    browser: ['OpenJarvis', 'Chrome', '1.0.0'],
    connectTimeoutMs: 60000,
    printQRInTerminal: false,
    // Suppress noisy Baileys logs
    logger: require('pino')({ level: 'silent' }),
  });

  sock.ev.on('creds.update', saveCreds);

  sock.ev.on('connection.update', async ({ connection, lastDisconnect, qr }) => {
    // New QR code available
    if (qr) {
      try {
        lastQr = await QRCode.toDataURL(qr);
        connStatus = 'qr_pending';
        console.log('[bridge] QR code ready — scan with WhatsApp');
      } catch (e) {
        console.error('[bridge] QR generation error:', e.message);
      }
    }

    if (connection === 'open') {
      connStatus = 'connected';
      lastQr = null;
      reconnectCount = 0;
      // JID format: "33612345678:16@s.whatsapp.net" → "+33612345678"
      const jid = (sock.authState && sock.authState.creds && sock.authState.creds.me)
        ? sock.authState.creds.me.id
        : '';
      const num = jid.split(':')[0].split('@')[0].replace(/[^0-9]/g, '');
      phoneNumber = num ? '+' + num : null;
      console.log('[bridge] Connected as', phoneNumber);
    }

    if (connection === 'close') {
      const code = lastDisconnect && lastDisconnect.error && lastDisconnect.error.output
        ? lastDisconnect.error.output.statusCode
        : 0;
      console.log('[bridge] Connection closed — code:', code);

      if (code === DisconnectReason.loggedOut) {
        console.log('[bridge] Logged out — clearing session');
        connStatus = 'disconnected';
        phoneNumber = null;
        lastQr = null;
        reconnectCount = 0;
        _clearAuth();
        setTimeout(createWASocket, RECONNECT_DELAY_MS);
      } else if (reconnectCount < MAX_RECONNECTS) {
        reconnectCount++;
        connStatus = 'disconnected';
        console.log(`[bridge] Reconnecting (${reconnectCount}/${MAX_RECONNECTS})...`);
        setTimeout(createWASocket, RECONNECT_DELAY_MS);
      } else {
        connStatus = 'error';
        console.error('[bridge] Max reconnects reached — manual restart required');
      }
    }
  });

  sock.ev.on('messages.upsert', async ({ messages, type }) => {
    if (type !== 'notify') return;
    for (const msg of messages) {
      if (msg.key.fromMe) continue;
      if (msg.key.remoteJid === 'status@broadcast') continue;
      try {
        await _handleInbound(msg);
      } catch (e) {
        console.error('[bridge] Inbound error:', e.message);
      }
    }
  });
}

function _clearAuth() {
  try {
    fs.rmSync(AUTH_DIR, { recursive: true, force: true });
    fs.mkdirSync(AUTH_DIR, { recursive: true });
  } catch (e) {
    console.error('[bridge] Could not clear auth dir:', e.message);
  }
}

// ── Inbound message handler ────────────────────────────────────────────────────
async function _handleInbound(msg) {
  const jid = msg.key.remoteJid;
  if (!jid) return;

  const rawNum = jid.split('@')[0].replace(/[^0-9]/g, '');
  const from = '+' + rawNum;

  let msgType = 'text';
  let text = '';
  let mediaPath = null;
  const content = msg.message;
  if (!content) return;

  if (content.conversation) {
    text = content.conversation;
  } else if (content.extendedTextMessage) {
    text = content.extendedTextMessage.text || '';
  } else if (content.audioMessage) {
    msgType = 'audio';
    try {
      const buffer = await downloadMediaMessage(msg, 'buffer', {});
      const tmpPath = `/tmp/wa_audio_${msg.key.id}.ogg`;
      fs.writeFileSync(tmpPath, buffer);
      mediaPath = tmpPath;
    } catch (e) {
      console.error('[bridge] Audio download failed:', e.message);
    }
  } else if (content.imageMessage) {
    msgType = 'image';
    text = content.imageMessage.caption || '';
    try {
      const buffer = await downloadMediaMessage(msg, 'buffer', {});
      const tmpPath = `/tmp/wa_img_${msg.key.id}.jpg`;
      fs.writeFileSync(tmpPath, buffer);
      mediaPath = tmpPath;
    } catch (e) {
      console.error('[bridge] Image download failed:', e.message);
    }
  } else {
    // Unsupported message type — ignore
    return;
  }

  const payload = JSON.stringify({ from, text, type: msgType, media_path: mediaPath });
  await _postToCore(CLAWBOT_INBOUND_PATH, payload);
}

function _postToCore(urlPath, body) {
  return new Promise((resolve, reject) => {
    const options = {
      hostname: CLAWBOT_HOST,
      port: CLAWBOT_PORT,
      path: urlPath,
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Content-Length': Buffer.byteLength(body),
      },
    };
    const req = http.request(options, res => {
      res.on('data', () => {});
      res.on('end', resolve);
    });
    req.on('error', reject);
    req.write(body);
    req.end();
  });
}

// ── Express HTTP server ────────────────────────────────────────────────────────
const app = express();
app.use(express.json());

app.get('/status', (_req, res) => {
  res.json({
    connected: connStatus === 'connected',
    status: connStatus,
    phone: phoneNumber,
    qr: connStatus === 'qr_pending' ? lastQr : null,
  });
});

app.post('/send', async (req, res) => {
  const { to, text } = req.body || {};
  if (!to || !text) {
    return res.status(400).json({ ok: false, error: 'missing to or text' });
  }
  if (connStatus !== 'connected') {
    return res.status(503).json({ ok: false, error: 'not connected' });
  }
  try {
    const jid = to.replace(/^\+/, '') + '@s.whatsapp.net';
    const result = await sock.sendMessage(jid, { text });
    res.json({ ok: true, messageId: result && result.key ? result.key.id : null });
  } catch (e) {
    console.error('[bridge] send error:', e.message);
    res.status(500).json({ ok: false, error: e.message });
  }
});

app.post('/send-audio', async (req, res) => {
  const { to, audio_path } = req.body || {};
  if (!to || !audio_path) {
    return res.status(400).json({ ok: false, error: 'missing to or audio_path' });
  }
  if (connStatus !== 'connected') {
    return res.status(503).json({ ok: false, error: 'not connected' });
  }
  try {
    const jid = to.replace(/^\+/, '') + '@s.whatsapp.net';
    const audio = fs.readFileSync(audio_path);
    const result = await sock.sendMessage(jid, {
      audio,
      mimetype: 'audio/ogg; codecs=opus',
      ptt: true,
    });
    res.json({ ok: true, messageId: result && result.key ? result.key.id : null });
  } catch (e) {
    console.error('[bridge] send-audio error:', e.message);
    res.status(500).json({ ok: false, error: e.message });
  }
});

// ── Startup ────────────────────────────────────────────────────────────────────
fs.mkdirSync(AUTH_DIR, { recursive: true });

createWASocket().catch(e => {
  console.error('[bridge] Fatal startup error:', e.message);
  connStatus = 'error';
});

app.listen(BRIDGE_PORT, '127.0.0.1', () => {
  console.log(`[bridge] WhatsApp bridge listening on 127.0.0.1:${BRIDGE_PORT}`);
});
