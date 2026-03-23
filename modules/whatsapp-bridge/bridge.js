/**
 * WhatsApp Bridge for ClawbotCore v2.0
 * Connects Baileys (WhatsApp Web) to ClawbotCore via HTTP.
 *
 * Messaging:    POST /send, /send-image, /send-video, /send-audio, /send-file, /send-location, /send-contact
 * Msg actions:  POST /react, /edit, /delete, /pin-message
 * Chat mgmt:    POST /chat/pin, /chat/archive, /chat/mute, /chat/read, /chat/delete
 * Groups:       POST /group/create, /group/:jid/update, /group/:jid/members  GET /group/:jid/info
 * Contacts:     POST /check-whatsapp, /profile-info
 * Utility:      GET /status, /media/recent
 * Tool exec:    POST /v1/whatsapp-bridge/:toolName/execute (unified ClawbotCore orchestrator endpoint)
 *
 * Inbound:      POST http://127.0.0.1:8090/v1/channels/whatsapp/inbound
 *               Supports: text, image, audio, video, document, sticker, location, contact
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

// MIME type lookup for media sending
const MIME_MAP = {
  '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.png': 'image/png',
  '.gif': 'image/gif', '.webp': 'image/webp',
  '.mp4': 'video/mp4', '.3gp': 'video/3gpp', '.avi': 'video/x-msvideo',
  '.ogg': 'audio/ogg; codecs=opus', '.mp3': 'audio/mpeg', '.wav': 'audio/wav',
  '.pdf': 'application/pdf', '.zip': 'application/zip',
  '.doc': 'application/msword', '.docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
  '.xls': 'application/vnd.ms-excel', '.xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
  '.txt': 'text/plain', '.csv': 'text/csv', '.json': 'application/json',
};

// ── State ─────────────────────────────────────────────────────────────────────
let sock = null;
let lastQr = null;           // data:image/png;base64,...
let connStatus = 'disconnected'; // 'disconnected' | 'qr_pending' | 'connected' | 'error'
let phoneNumber = null;      // '+33612345678'
let reconnectCount = 0;
const _sentByBridge = new Set(); // message IDs sent by us (to avoid loops)

// ── Helpers ───────────────────────────────────────────────────────────────────

/** Resolve phone/JID to a valid WhatsApp JID */
function _resolveJid(to, rawJid) {
  if (rawJid && rawJid.includes('@')) return rawJid;
  if (to && to.includes('@')) return to;
  if (to) return to.replace(/^\+/, '') + '@s.whatsapp.net';
  return null;
}

/** Track sent message ID to prevent echo loop on self-chat */
function _trackSent(result) {
  if (result && result.key && result.key.id) {
    _sentByBridge.add(result.key.id);
    setTimeout(() => _sentByBridge.delete(result.key.id), 30000);
  }
}

/** Get MIME type from file path */
function _getMime(filePath, fallback) {
  const ext = path.extname(filePath).toLowerCase();
  return MIME_MAP[ext] || fallback || 'application/octet-stream';
}

/** Validate that a file exists and is readable */
function _validateFile(filePath) {
  if (!filePath) return 'missing file path';
  if (!fs.existsSync(filePath)) return `file not found: ${filePath}`;
  try { fs.accessSync(filePath, fs.constants.R_OK); } catch { return `file not readable: ${filePath}`; }
  return null;
}

/** Build a vCard string */
function _buildVCard(name, phone) {
  return `BEGIN:VCARD\nVERSION:3.0\nFN:${name}\nTEL;type=CELL;type=VOICE;waid=${phone.replace(/[^0-9]/g, '')}:${phone}\nEND:VCARD`;
}

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
  } else if (content.documentMessage) {
    msgType = 'file';
    text = content.documentMessage.fileName || '';
    try {
      const buffer = await downloadMediaMessage(msg, 'buffer', {});
      const ext = path.extname(text) || '.bin';
      const tmpPath = `/tmp/wa_file_${msg.key.id}${ext}`;
      fs.writeFileSync(tmpPath, buffer);
      mediaPath = tmpPath;
    } catch (e) {
      console.error('[bridge] Document download failed:', e.message);
    }
  } else if (content.videoMessage) {
    msgType = 'video';
    text = content.videoMessage.caption || '';
    try {
      const buffer = await downloadMediaMessage(msg, 'buffer', {});
      const tmpPath = `/tmp/wa_video_${msg.key.id}.mp4`;
      fs.writeFileSync(tmpPath, buffer);
      mediaPath = tmpPath;
    } catch (e) {
      console.error('[bridge] Video download failed:', e.message);
    }
  } else if (content.stickerMessage) {
    msgType = 'sticker';
    try {
      const buffer = await downloadMediaMessage(msg, 'buffer', {});
      const tmpPath = `/tmp/wa_sticker_${msg.key.id}.webp`;
      fs.writeFileSync(tmpPath, buffer);
      mediaPath = tmpPath;
    } catch (e) {
      console.error('[bridge] Sticker download failed:', e.message);
    }
  } else if (content.locationMessage) {
    msgType = 'location';
    const loc = content.locationMessage;
    text = `Location: ${loc.degreesLatitude},${loc.degreesLongitude}` + (loc.name ? ` (${loc.name})` : '');
  } else if (content.contactMessage || content.contactsArrayMessage) {
    msgType = 'contact';
    const contacts = content.contactsArrayMessage?.contacts || [content.contactMessage];
    text = contacts.map(c => c.displayName || c.vcard || '').join(', ');
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
    _trackSent(result);
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

// ── Media sending endpoints ──────────────────────────────────────────────────

app.post('/send-image', async (req, res) => {
  const { to, jid: rawJid, image_path, caption } = req.body || {};
  const jid = _resolveJid(to, rawJid);
  if (!jid || !image_path) return res.status(400).json({ ok: false, error: 'missing to/jid or image_path' });
  if (connStatus !== 'connected') return res.status(503).json({ ok: false, error: 'not connected' });
  const fileErr = _validateFile(image_path);
  if (fileErr) return res.status(400).json({ ok: false, error: fileErr });
  try {
    const image = fs.readFileSync(image_path);
    const result = await sock.sendMessage(jid, { image, caption: caption || undefined, mimetype: _getMime(image_path, 'image/jpeg') });
    _trackSent(result);
    res.json({ ok: true, messageId: result?.key?.id || null });
  } catch (e) {
    console.error('[bridge] send-image error:', e.message);
    res.status(500).json({ ok: false, error: e.message });
  }
});

app.post('/send-video', async (req, res) => {
  const { to, jid: rawJid, video_path, caption } = req.body || {};
  const jid = _resolveJid(to, rawJid);
  if (!jid || !video_path) return res.status(400).json({ ok: false, error: 'missing to/jid or video_path' });
  if (connStatus !== 'connected') return res.status(503).json({ ok: false, error: 'not connected' });
  const fileErr = _validateFile(video_path);
  if (fileErr) return res.status(400).json({ ok: false, error: fileErr });
  try {
    const video = fs.readFileSync(video_path);
    const result = await sock.sendMessage(jid, { video, caption: caption || undefined, mimetype: _getMime(video_path, 'video/mp4') });
    _trackSent(result);
    res.json({ ok: true, messageId: result?.key?.id || null });
  } catch (e) {
    console.error('[bridge] send-video error:', e.message);
    res.status(500).json({ ok: false, error: e.message });
  }
});

app.post('/send-file', async (req, res) => {
  const { to, jid: rawJid, file_path, filename, mimetype } = req.body || {};
  const jid = _resolveJid(to, rawJid);
  if (!jid || !file_path) return res.status(400).json({ ok: false, error: 'missing to/jid or file_path' });
  if (connStatus !== 'connected') return res.status(503).json({ ok: false, error: 'not connected' });
  const fileErr = _validateFile(file_path);
  if (fileErr) return res.status(400).json({ ok: false, error: fileErr });
  try {
    const document = fs.readFileSync(file_path);
    const result = await sock.sendMessage(jid, {
      document,
      mimetype: mimetype || _getMime(file_path),
      fileName: filename || path.basename(file_path),
    });
    _trackSent(result);
    res.json({ ok: true, messageId: result?.key?.id || null });
  } catch (e) {
    console.error('[bridge] send-file error:', e.message);
    res.status(500).json({ ok: false, error: e.message });
  }
});

app.post('/send-location', async (req, res) => {
  const { to, jid: rawJid, latitude, longitude, name, address } = req.body || {};
  const jid = _resolveJid(to, rawJid);
  if (!jid || latitude == null || longitude == null) return res.status(400).json({ ok: false, error: 'missing to/jid, latitude or longitude' });
  if (connStatus !== 'connected') return res.status(503).json({ ok: false, error: 'not connected' });
  try {
    const result = await sock.sendMessage(jid, {
      location: { degreesLatitude: latitude, degreesLongitude: longitude, name: name || undefined, address: address || undefined },
    });
    _trackSent(result);
    res.json({ ok: true, messageId: result?.key?.id || null });
  } catch (e) {
    console.error('[bridge] send-location error:', e.message);
    res.status(500).json({ ok: false, error: e.message });
  }
});

app.post('/send-contact', async (req, res) => {
  const { to, jid: rawJid, contact_name, contact_phone } = req.body || {};
  const jid = _resolveJid(to, rawJid);
  if (!jid || !contact_name || !contact_phone) return res.status(400).json({ ok: false, error: 'missing to/jid, contact_name or contact_phone' });
  if (connStatus !== 'connected') return res.status(503).json({ ok: false, error: 'not connected' });
  try {
    const vcard = _buildVCard(contact_name, contact_phone);
    const result = await sock.sendMessage(jid, {
      contacts: { displayName: contact_name, contacts: [{ vcard }] },
    });
    _trackSent(result);
    res.json({ ok: true, messageId: result?.key?.id || null });
  } catch (e) {
    console.error('[bridge] send-contact error:', e.message);
    res.status(500).json({ ok: false, error: e.message });
  }
});

// ── Message action endpoints ────────────────────────────────────────────────

app.post('/react', async (req, res) => {
  const { jid, message_id, emoji } = req.body || {};
  if (!jid || !message_id || emoji == null) return res.status(400).json({ ok: false, error: 'missing jid, message_id or emoji' });
  if (connStatus !== 'connected') return res.status(503).json({ ok: false, error: 'not connected' });
  try {
    await sock.sendMessage(jid, { react: { text: emoji, key: { remoteJid: jid, id: message_id } } });
    res.json({ ok: true });
  } catch (e) {
    console.error('[bridge] react error:', e.message);
    res.status(500).json({ ok: false, error: e.message });
  }
});

app.post('/edit', async (req, res) => {
  const { jid, message_id, new_text } = req.body || {};
  if (!jid || !message_id || !new_text) return res.status(400).json({ ok: false, error: 'missing jid, message_id or new_text' });
  if (connStatus !== 'connected') return res.status(503).json({ ok: false, error: 'not connected' });
  try {
    await sock.sendMessage(jid, { edit: { remoteJid: jid, id: message_id, fromMe: true }, text: new_text });
    res.json({ ok: true });
  } catch (e) {
    console.error('[bridge] edit error:', e.message);
    res.status(500).json({ ok: false, error: e.message });
  }
});

app.post('/delete', async (req, res) => {
  const { jid, message_id } = req.body || {};
  if (!jid || !message_id) return res.status(400).json({ ok: false, error: 'missing jid or message_id' });
  if (connStatus !== 'connected') return res.status(503).json({ ok: false, error: 'not connected' });
  try {
    await sock.sendMessage(jid, { delete: { remoteJid: jid, id: message_id, fromMe: true } });
    res.json({ ok: true });
  } catch (e) {
    console.error('[bridge] delete error:', e.message);
    res.status(500).json({ ok: false, error: e.message });
  }
});

app.post('/pin-message', async (req, res) => {
  const { jid, message_id, pin } = req.body || {};
  if (!jid || !message_id || pin == null) return res.status(400).json({ ok: false, error: 'missing jid, message_id or pin' });
  if (connStatus !== 'connected') return res.status(503).json({ ok: false, error: 'not connected' });
  try {
    await sock.sendMessage(jid, { pin: { remoteJid: jid, id: message_id }, type: pin ? 1 : 0 });
    res.json({ ok: true });
  } catch (e) {
    console.error('[bridge] pin-message error:', e.message);
    res.status(500).json({ ok: false, error: e.message });
  }
});

// ── Chat management endpoints ───────────────────────────────────────────────

app.post('/chat/pin', async (req, res) => {
  const { jid, pin } = req.body || {};
  if (!jid || pin == null) return res.status(400).json({ ok: false, error: 'missing jid or pin' });
  if (connStatus !== 'connected') return res.status(503).json({ ok: false, error: 'not connected' });
  try {
    await sock.chatModify({ pin: pin }, jid);
    res.json({ ok: true });
  } catch (e) {
    console.error('[bridge] chat/pin error:', e.message);
    res.status(500).json({ ok: false, error: e.message });
  }
});

app.post('/chat/archive', async (req, res) => {
  const { jid, archive } = req.body || {};
  if (!jid || archive == null) return res.status(400).json({ ok: false, error: 'missing jid or archive' });
  if (connStatus !== 'connected') return res.status(503).json({ ok: false, error: 'not connected' });
  try {
    await sock.chatModify({ archive: archive }, jid);
    res.json({ ok: true });
  } catch (e) {
    console.error('[bridge] chat/archive error:', e.message);
    res.status(500).json({ ok: false, error: e.message });
  }
});

app.post('/chat/mute', async (req, res) => {
  const { jid, mute, duration } = req.body || {};
  if (!jid || mute == null) return res.status(400).json({ ok: false, error: 'missing jid or mute' });
  if (connStatus !== 'connected') return res.status(503).json({ ok: false, error: 'not connected' });
  try {
    const durations = { '8h': 28800, '1w': 604800, 'forever': -1 };
    const mval = mute ? (durations[duration] || -1) : null;
    await sock.chatModify({ mute: mval }, jid);
    res.json({ ok: true });
  } catch (e) {
    console.error('[bridge] chat/mute error:', e.message);
    res.status(500).json({ ok: false, error: e.message });
  }
});

app.post('/chat/read', async (req, res) => {
  const { jid, read } = req.body || {};
  if (!jid || read == null) return res.status(400).json({ ok: false, error: 'missing jid or read' });
  if (connStatus !== 'connected') return res.status(503).json({ ok: false, error: 'not connected' });
  try {
    await sock.chatModify({ markRead: read }, jid);
    res.json({ ok: true });
  } catch (e) {
    console.error('[bridge] chat/read error:', e.message);
    res.status(500).json({ ok: false, error: e.message });
  }
});

app.post('/chat/delete', async (req, res) => {
  const { jid } = req.body || {};
  if (!jid) return res.status(400).json({ ok: false, error: 'missing jid' });
  if (connStatus !== 'connected') return res.status(503).json({ ok: false, error: 'not connected' });
  try {
    await sock.chatModify({ delete: true }, jid);
    res.json({ ok: true });
  } catch (e) {
    console.error('[bridge] chat/delete error:', e.message);
    res.status(500).json({ ok: false, error: e.message });
  }
});

// ── Group endpoints ─────────────────────────────────────────────────────────

app.post('/group/create', async (req, res) => {
  const { name, participants } = req.body || {};
  if (!name || !participants || !participants.length) return res.status(400).json({ ok: false, error: 'missing name or participants' });
  if (connStatus !== 'connected') return res.status(503).json({ ok: false, error: 'not connected' });
  try {
    const jids = participants.map(p => p.includes('@') ? p : p.replace(/^\+/, '') + '@s.whatsapp.net');
    const group = await sock.groupCreate(name, jids);
    res.json({ ok: true, groupJid: group.id, subject: group.subject });
  } catch (e) {
    console.error('[bridge] group/create error:', e.message);
    res.status(500).json({ ok: false, error: e.message });
  }
});

app.get('/group/:jid/info', async (req, res) => {
  const { jid } = req.params;
  if (connStatus !== 'connected') return res.status(503).json({ ok: false, error: 'not connected' });
  try {
    const meta = await sock.groupMetadata(jid);
    res.json({ ok: true, ...meta });
  } catch (e) {
    console.error('[bridge] group/info error:', e.message);
    res.status(500).json({ ok: false, error: e.message });
  }
});

app.post('/group/:jid/update', async (req, res) => {
  const { jid } = req.params;
  const { subject, description, setting } = req.body || {};
  if (connStatus !== 'connected') return res.status(503).json({ ok: false, error: 'not connected' });
  try {
    if (subject) await sock.groupUpdateSubject(jid, subject);
    if (description !== undefined) await sock.groupUpdateDescription(jid, description);
    if (setting) await sock.groupSettingUpdate(jid, setting);
    res.json({ ok: true });
  } catch (e) {
    console.error('[bridge] group/update error:', e.message);
    res.status(500).json({ ok: false, error: e.message });
  }
});

app.post('/group/:jid/members', async (req, res) => {
  const { jid } = req.params;
  const { participants, action } = req.body || {};
  if (!participants || !action) return res.status(400).json({ ok: false, error: 'missing participants or action' });
  const validActions = ['add', 'remove', 'promote', 'demote'];
  if (!validActions.includes(action)) return res.status(400).json({ ok: false, error: `invalid action: ${action}. Must be: ${validActions.join(', ')}` });
  if (connStatus !== 'connected') return res.status(503).json({ ok: false, error: 'not connected' });
  try {
    const jids = participants.map(p => p.includes('@') ? p : p.replace(/^\+/, '') + '@s.whatsapp.net');
    const result = await sock.groupParticipantsUpdate(jid, jids, action);
    res.json({ ok: true, result });
  } catch (e) {
    console.error('[bridge] group/members error:', e.message);
    res.status(500).json({ ok: false, error: e.message });
  }
});

// ── Contact & info endpoints ────────────────────────────────────────────────

app.post('/check-whatsapp', async (req, res) => {
  const { phones } = req.body || {};
  if (!phones || !phones.length) return res.status(400).json({ ok: false, error: 'missing phones array' });
  if (connStatus !== 'connected') return res.status(503).json({ ok: false, error: 'not connected' });
  try {
    const results = await sock.onWhatsApp(...phones.map(p => p.replace(/^\+/, '')));
    res.json({ ok: true, results });
  } catch (e) {
    console.error('[bridge] check-whatsapp error:', e.message);
    res.status(500).json({ ok: false, error: e.message });
  }
});

app.post('/profile-info', async (req, res) => {
  const { jid } = req.body || {};
  if (!jid) return res.status(400).json({ ok: false, error: 'missing jid' });
  if (connStatus !== 'connected') return res.status(503).json({ ok: false, error: 'not connected' });
  try {
    const picture = await sock.profilePictureUrl(jid, 'image').catch(() => null);
    const status = await sock.fetchStatus(jid).catch(() => null);
    const business = await sock.getBusinessProfile(jid).catch(() => null);
    res.json({ ok: true, picture, status, business });
  } catch (e) {
    console.error('[bridge] profile-info error:', e.message);
    res.status(500).json({ ok: false, error: e.message });
  }
});

// ── Media listing ───────────────────────────────────────────────────────────

app.get('/media/recent', (req, res) => {
  try {
    const prefixMap = { image: 'wa_img_', audio: 'wa_audio_', video: 'wa_video_', file: 'wa_file_', sticker: 'wa_sticker_' };
    const filter = req.query.type || 'all';
    const prefixes = filter === 'all' ? ['wa_'] : [prefixMap[filter] || 'wa_'];
    const limit = parseInt(req.query.limit) || 20;
    const files = fs.readdirSync('/tmp')
      .filter(f => prefixes.some(p => f.startsWith(p)))
      .map(f => {
        try {
          const st = fs.statSync('/tmp/' + f);
          return { name: f, path: '/tmp/' + f, size: st.size, modified: st.mtimeMs };
        } catch { return null; }
      })
      .filter(Boolean)
      .sort((a, b) => b.modified - a.modified)
      .slice(0, limit);
    res.json({ ok: true, files });
  } catch (e) {
    res.json({ ok: true, files: [] });
  }
});

// ── Unified tool execution endpoint (for ClawbotCore orchestrator) ──────────

// Route used by orchestrator: POST /v1/whatsapp-bridge/execute { tool, arguments }
app.post('/v1/whatsapp-bridge/execute', async (req, res) => {
  const toolName = req.body?.tool;
  const args = req.body?.arguments || {};
  if (!toolName) return res.status(400).json({ error: 'Missing "tool" field' });

  const noConnRequired = ['get_status', 'list_recent_media'];
  if (!noConnRequired.includes(toolName) && connStatus !== 'connected') {
    return res.status(503).json({ error: 'WhatsApp not connected' });
  }

  try {
    const result = await _executeTool(toolName, args);
    res.json({ result: typeof result === 'string' ? result : JSON.stringify(result) });
  } catch (e) {
    console.error(`[bridge] tool ${toolName} error:`, e.message);
    res.status(500).json({ error: e.message });
  }
});

// Route for direct tool calls: POST /v1/whatsapp-bridge/:toolName/execute
app.post('/v1/whatsapp-bridge/:toolName/execute', async (req, res) => {
  const { toolName } = req.params;
  const args = req.body?.arguments || req.body || {};

  // Tools that don't require connection
  const noConnRequired = ['get_status', 'list_recent_media'];
  if (!noConnRequired.includes(toolName) && connStatus !== 'connected') {
    return res.status(503).json({ error: 'WhatsApp not connected' });
  }

  try {
    const result = await _executeTool(toolName, args);
    res.json(result);
  } catch (e) {
    console.error(`[bridge] tool ${toolName} error:`, e.message);
    res.status(500).json({ error: e.message });
  }
});

async function _executeTool(name, args) {
  switch (name) {
    case 'get_status':
      return { connected: connStatus === 'connected', status: connStatus, phone: phoneNumber };

    case 'send_message': {
      const jid = _resolveJid(args.to);
      const opts = { text: args.text };
      if (args.quote_message_id) {
        opts.quoted = { key: { remoteJid: jid, id: args.quote_message_id } };
      }
      const r = await sock.sendMessage(jid, opts);
      _trackSent(r);
      return { ok: true, messageId: r?.key?.id };
    }

    case 'send_image': {
      const jid = _resolveJid(args.to);
      const err = _validateFile(args.image_path);
      if (err) throw new Error(err);
      const image = fs.readFileSync(args.image_path);
      const r = await sock.sendMessage(jid, { image, caption: args.caption || undefined, mimetype: _getMime(args.image_path, 'image/jpeg') });
      _trackSent(r);
      return { ok: true, messageId: r?.key?.id };
    }

    case 'send_video': {
      const jid = _resolveJid(args.to);
      const err = _validateFile(args.video_path);
      if (err) throw new Error(err);
      const video = fs.readFileSync(args.video_path);
      const r = await sock.sendMessage(jid, { video, caption: args.caption || undefined, mimetype: _getMime(args.video_path, 'video/mp4') });
      _trackSent(r);
      return { ok: true, messageId: r?.key?.id };
    }

    case 'send_audio': {
      const jid = _resolveJid(args.to);
      const err = _validateFile(args.audio_path);
      if (err) throw new Error(err);
      const audio = fs.readFileSync(args.audio_path);
      const r = await sock.sendMessage(jid, { audio, mimetype: 'audio/ogg; codecs=opus', ptt: true });
      _trackSent(r);
      return { ok: true, messageId: r?.key?.id };
    }

    case 'send_file': {
      const jid = _resolveJid(args.to);
      const err = _validateFile(args.file_path);
      if (err) throw new Error(err);
      const document = fs.readFileSync(args.file_path);
      const r = await sock.sendMessage(jid, {
        document, mimetype: _getMime(args.file_path),
        fileName: args.filename || path.basename(args.file_path),
      });
      _trackSent(r);
      return { ok: true, messageId: r?.key?.id };
    }

    case 'send_location': {
      const jid = _resolveJid(args.to);
      const r = await sock.sendMessage(jid, {
        location: { degreesLatitude: args.latitude, degreesLongitude: args.longitude, name: args.name || undefined, address: args.address || undefined },
      });
      _trackSent(r);
      return { ok: true, messageId: r?.key?.id };
    }

    case 'send_contact': {
      const jid = _resolveJid(args.to);
      const vcard = _buildVCard(args.contact_name, args.contact_phone);
      const r = await sock.sendMessage(jid, { contacts: { displayName: args.contact_name, contacts: [{ vcard }] } });
      _trackSent(r);
      return { ok: true, messageId: r?.key?.id };
    }

    case 'react_to_message': {
      await sock.sendMessage(args.jid, { react: { text: args.emoji, key: { remoteJid: args.jid, id: args.message_id } } });
      return { ok: true };
    }

    case 'edit_message': {
      await sock.sendMessage(args.jid, { edit: { remoteJid: args.jid, id: args.message_id, fromMe: true }, text: args.new_text });
      return { ok: true };
    }

    case 'delete_message': {
      await sock.sendMessage(args.jid, { delete: { remoteJid: args.jid, id: args.message_id, fromMe: true } });
      return { ok: true };
    }

    case 'pin_message': {
      await sock.sendMessage(args.jid, { pin: { remoteJid: args.jid, id: args.message_id }, type: args.pin ? 1 : 0 });
      return { ok: true };
    }

    case 'pin_chat': {
      await sock.chatModify({ pin: args.pin }, args.jid);
      return { ok: true };
    }

    case 'archive_chat': {
      await sock.chatModify({ archive: args.archive }, args.jid);
      return { ok: true };
    }

    case 'mute_chat': {
      const durations = { '8h': 28800, '1w': 604800, 'forever': -1 };
      await sock.chatModify({ mute: args.mute ? (durations[args.duration] || -1) : null }, args.jid);
      return { ok: true };
    }

    case 'mark_read': {
      await sock.chatModify({ markRead: args.read }, args.jid);
      return { ok: true };
    }

    case 'delete_chat': {
      await sock.chatModify({ delete: true }, args.jid);
      return { ok: true };
    }

    case 'create_group': {
      const jids = args.participants.map(p => p.includes('@') ? p : p.replace(/^\+/, '') + '@s.whatsapp.net');
      const group = await sock.groupCreate(args.name, jids);
      return { ok: true, groupJid: group.id, subject: group.subject };
    }

    case 'group_info': {
      const meta = await sock.groupMetadata(args.jid);
      return { ok: true, ...meta };
    }

    case 'group_update': {
      if (args.subject) await sock.groupUpdateSubject(args.jid, args.subject);
      if (args.description !== undefined) await sock.groupUpdateDescription(args.jid, args.description);
      if (args.setting) await sock.groupSettingUpdate(args.jid, args.setting);
      return { ok: true };
    }

    case 'group_manage_members': {
      const jids = args.participants.map(p => p.includes('@') ? p : p.replace(/^\+/, '') + '@s.whatsapp.net');
      const r = await sock.groupParticipantsUpdate(args.jid, jids, args.action);
      return { ok: true, result: r };
    }

    case 'check_whatsapp': {
      const results = await sock.onWhatsApp(...args.phones.map(p => p.replace(/^\+/, '')));
      return { ok: true, results };
    }

    case 'get_profile_info': {
      const picture = await sock.profilePictureUrl(args.jid, 'image').catch(() => null);
      const status = await sock.fetchStatus(args.jid).catch(() => null);
      const business = await sock.getBusinessProfile(args.jid).catch(() => null);
      return { ok: true, picture, status, business };
    }

    case 'list_recent_media': {
      const prefixMap = { image: 'wa_img_', audio: 'wa_audio_', video: 'wa_video_', file: 'wa_file_', sticker: 'wa_sticker_' };
      const filter = args.type_filter || 'all';
      const prefixes = filter === 'all' ? ['wa_'] : [prefixMap[filter] || 'wa_'];
      const files = fs.readdirSync('/tmp')
        .filter(f => prefixes.some(p => f.startsWith(p)))
        .map(f => { try { const s = fs.statSync('/tmp/' + f); return { name: f, path: '/tmp/' + f, size: s.size, modified: s.mtimeMs }; } catch { return null; } })
        .filter(Boolean)
        .sort((a, b) => b.modified - a.modified)
        .slice(0, args.limit || 20);
      return { ok: true, files };
    }

    default:
      throw new Error(`Unknown tool: ${name}`);
  }
}

// ── Startup ────────────────────────────────────────────────────────────────────
fs.mkdirSync(AUTH_DIR, { recursive: true });

createWASocket().catch(e => {
  console.error('[bridge] Fatal startup error:', e.message);
  connStatus = 'error';
});

app.listen(BRIDGE_PORT, '127.0.0.1', () => {
  console.log(`[bridge] WhatsApp bridge listening on 127.0.0.1:${BRIDGE_PORT}`);
});
