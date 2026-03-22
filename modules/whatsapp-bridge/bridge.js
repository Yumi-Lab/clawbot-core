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

import {
  makeWASocket,
  useMultiFileAuthState,
  makeCacheableSignalKeyStore,
  fetchLatestWaWebVersion,
  Browsers,
  DisconnectReason,
  downloadMediaMessage,
} from '@whiskeysockets/baileys';
import express from 'express';
import QRCode from 'qrcode';
import fs from 'fs';
import path from 'path';
import http from 'http';
import { fileURLToPath } from 'url';
import pino from 'pino';

const __dirname = path.dirname(fileURLToPath(import.meta.url));

const BRIDGE_PORT = 3100;
const CLAWBOT_HOST = '127.0.0.1';
const CLAWBOT_PORT = 8090;
const CLAWBOT_INBOUND_PATH = '/v1/channels/whatsapp/inbound';
const AUTH_DIR = process.env.AUTH_DIR || path.join(__dirname, 'auth');
const MAX_RECONNECTS = 10;
const RECONNECT_DELAY_MS = 5000;

// ── State ─────────────────────────────────────────────────────────────────────
let sock = null;
let lastQr = null;           // data:image/png;base64,...
let connStatus = 'disconnected'; // 'disconnected' | 'qr_pending' | 'connected' | 'error'
let phoneNumber = null;      // '+33612345678'
let reconnectCount = 0;
const _sentByBridge = new Set(); // message IDs sent by us (to avoid loops)

// ── Baileys socket ─────────────────────────────────────────────────────────────
async function createWASocket() {
  const { state, saveCreds } = await useMultiFileAuthState(AUTH_DIR);
  const logger = pino({ level: 'silent' });
  const { version } = await fetchLatestWaWebVersion({});

  sock = makeWASocket({
    auth: {
      creds: state.creds,
      keys: makeCacheableSignalKeyStore(state.keys, logger),
    },
    browser: Browsers.macOS('Desktop'),
    version,
    connectTimeoutMs: 60000,
    logger,
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
      const jid = sock.user ? sock.user.id : '';
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
        console.error('[bridge] Max reconnects reached — exiting for systemd restart');
        setTimeout(() => process.exit(1), 1000);
      }
    }
  });

  sock.ev.on('messages.upsert', async (upsert) => {
    for (const msg of upsert.messages) {
      if (msg.key.remoteJid === 'status@broadcast') continue;
      if (!msg.message) continue;
      // Skip messages sent by the bridge itself (prevent loops)
      if (_sentByBridge.has(msg.key.id)) {
        _sentByBridge.delete(msg.key.id);
        continue;
      }
      const isSelf = _isSelfChat(msg.key.remoteJid);
      // type=notify → normal incoming; type=append → self-message or synced
      if (upsert.type === 'notify' && msg.key.fromMe && !isSelf) continue;
      if (upsert.type === 'append' && !isSelf) continue;
      console.log('[bridge] inbound from:', msg.key.remoteJid, 'fromMe:', msg.key.fromMe, 'type:', upsert.type, 'altJid:', msg.key.remoteJidAlt || 'NONE');
      try {
        await _handleInbound(msg);
      } catch (e) {
        console.error('[bridge] Inbound error:', e.message);
      }
    }
  });
}

function _isSelfChat(jid) {
  if (!sock || !sock.user) return false;
  // sock.user.id can be "33642536328:16@s.whatsapp.net" or a LID
  const myPhone = sock.user.id.split(':')[0].split('@')[0];
  const remoteId = (jid || '').split(':')[0].split('@')[0];
  // Direct match (same format)
  if (myPhone === remoteId) return true;
  // Also store LID of self if we see it in user.lid
  if (sock.user.lid) {
    const myLid = sock.user.lid.split(':')[0].split('@')[0];
    if (myLid === remoteId) return true;
  }
  return false;
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

  // Prefer alt_jid (real phone@s.whatsapp.net) over LID for phone extraction
  const altJid = msg.key.remoteJidAlt || '';
  const phoneSource = (altJid.includes('@s.whatsapp.net') ? altJid : jid);
  const rawNum = phoneSource.split('@')[0].replace(/[^0-9]/g, '');
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

  const payload = JSON.stringify({ from, jid, alt_jid: altJid, text, type: msgType, media_path: mediaPath });
  console.log('[bridge] forwarding to core:', from, 'text:', (text || '').substring(0, 50), 'altJid:', altJid || 'NONE');
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
  const { to, jid: rawJid, text } = req.body || {};
  if ((!to && !rawJid) || !text) {
    return res.status(400).json({ ok: false, error: 'missing to/jid or text' });
  }
  if (connStatus !== 'connected') {
    return res.status(503).json({ ok: false, error: 'not connected' });
  }
  try {
    // Use raw JID if provided (for @lid format), otherwise convert phone to @s.whatsapp.net
    const jid = rawJid || (to.replace(/^\+/, '') + '@s.whatsapp.net');
    const result = await sock.sendMessage(jid, { text });
    // Track sent message ID to prevent loop on self-chat
    if (result && result.key && result.key.id) {
      _sentByBridge.add(result.key.id);
      setTimeout(() => _sentByBridge.delete(result.key.id), 30000);
    }
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
