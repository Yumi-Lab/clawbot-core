/**
 * WeCom (Enterprise WeChat) Bridge for ClawbotCore
 * Connects to WeCom via @wecom/aibot-node-sdk WebSocket.
 *
 * Endpoints:
 *   GET  /status        -> { connected, status, botId }
 *   POST /send          -> { chatid, text }
 *
 * Inbound messages are forwarded to:
 *   POST http://127.0.0.1:8090/v1/channels/wecom/inbound
 *
 * Config is read from /etc/clawbot/clawbot.cfg [wecom] section
 * or from environment variables WECOM_BOT_ID / WECOM_SECRET.
 */

const { WSClient, generateReqId } = require('@wecom/aibot-node-sdk');
const express = require('express');
const fs = require('fs');
const http = require('http');
const path = require('path');

const BRIDGE_PORT = 3101;
const CLAWBOT_HOST = '127.0.0.1';
const CLAWBOT_PORT = 8090;
const CLAWBOT_INBOUND_PATH = '/v1/channels/wecom/inbound';
const CONFIG_PATH = process.env.CONFIG_PATH || '/etc/clawbot/clawbot.cfg';

// ── Config ──────────────────────────────────────────────────────────────────────

function _readConfig() {
  // Read botId and secret from clawbot.cfg [wecom] section
  let botId = process.env.WECOM_BOT_ID || '';
  let secret = process.env.WECOM_SECRET || '';
  try {
    if (fs.existsSync(CONFIG_PATH)) {
      const raw = fs.readFileSync(CONFIG_PATH, 'utf8');
      const lines = raw.split('\n');
      let inSection = false;
      for (const line of lines) {
        const trimmed = line.trim();
        if (trimmed.startsWith('[')) {
          inSection = trimmed.toLowerCase() === '[wecom]';
          continue;
        }
        if (!inSection) continue;
        const match = trimmed.match(/^(\w+)\s*=\s*(.+)/);
        if (!match) continue;
        const [, key, val] = match;
        if (key === 'bot_id' && !process.env.WECOM_BOT_ID) botId = val.trim();
        if (key === 'secret' && !process.env.WECOM_SECRET) secret = val.trim();
      }
    }
  } catch (e) {
    console.error('[wecom-bridge] Config read error:', e.message);
  }
  return { botId, secret };
}

// ── State ───────────────────────────────────────────────────────────────────────
let wsClient = null;
let connStatus = 'disconnected'; // 'disconnected' | 'connected' | 'error' | 'no_config'
let currentBotId = null;

// ── WeCom SDK connection ────────────────────────────────────────────────────────

function createWeComClient() {
  const { botId, secret } = _readConfig();
  if (!botId || !secret) {
    console.log('[wecom-bridge] No botId/secret configured — waiting for config');
    connStatus = 'no_config';
    return;
  }

  currentBotId = botId;
  connStatus = 'disconnected';
  console.log('[wecom-bridge] Connecting with botId:', botId.substring(0, 8) + '...');

  wsClient = new WSClient({
    botId,
    secret,
    heartbeatInterval: 30000,
    reconnectInterval: 5000,
    maxReconnectAttempts: 10,
    maxAuthFailureAttempts: 3,
  });

  // ── Connection events ───────────────────────────────────────────────────────

  wsClient.on('connected', () => {
    console.log('[wecom-bridge] WebSocket connected');
  });

  wsClient.on('authenticated', () => {
    connStatus = 'connected';
    console.log('[wecom-bridge] Authenticated — ready to receive messages');
  });

  wsClient.on('disconnected', (reason) => {
    console.log('[wecom-bridge] Disconnected:', reason);
    connStatus = 'disconnected';
    // 'disconnected_event' means server kicked us (new connection opened elsewhere)
    // SDK does NOT auto-reconnect for this — exit for systemd restart
    if (reason === 'disconnected_event') {
      console.error('[wecom-bridge] Server disconnected (new client?) — exiting for systemd restart');
      setTimeout(() => process.exit(1), 1000);
    }
  });

  wsClient.on('reconnecting', (attempt) => {
    console.log(`[wecom-bridge] Reconnecting (attempt ${attempt})...`);
    connStatus = 'disconnected';
  });

  wsClient.on('error', (err) => {
    console.error('[wecom-bridge] Error:', err.message || err);
    if (err.name === 'WSReconnectExhaustedError' || err.name === 'WSAuthFailureError') {
      connStatus = 'error';
      console.error('[wecom-bridge] Fatal error — exiting for systemd restart');
      setTimeout(() => process.exit(1), 1000);
    }
  });

  // ── Message events ──────────────────────────────────────────────────────────

  wsClient.on('message', async (frame) => {
    _storePendingFrame(frame);
    try {
      await _handleInbound(frame);
    } catch (e) {
      console.error('[wecom-bridge] Inbound error:', e.message);
    }
  });

  // ── Event callbacks (enter_chat, etc.) ──────────────────────────────────────

  wsClient.on('event.enter_chat', async (frame) => {
    try {
      await wsClient.replyWelcome(frame, {
        msgtype: 'text',
        text: { content: 'Hello! I am ClawBot, your AI assistant. How can I help you?' },
      });
    } catch (e) {
      console.error('[wecom-bridge] Welcome reply error:', e.message);
    }
  });

  // ── Connect ─────────────────────────────────────────────────────────────────
  wsClient.connect();
}

// ── Inbound message handler ─────────────────────────────────────────────────────

async function _handleInbound(frame) {
  const body = frame.body || {};
  const msgType = body.msgtype || 'unknown';
  const userId = (body.from && body.from.userid) || 'unknown';
  const chatId = body.chatid || '';
  const chatType = body.chattype || 'single';
  const msgId = body.msgid || '';

  let text = '';
  let type = 'text';
  let mediaPath = null;

  if (msgType === 'text' && body.text) {
    text = body.text.content || '';
  } else if (msgType === 'voice' && body.voice) {
    // Voice messages come pre-transcribed by WeCom
    text = body.voice.content || '';
    type = 'voice';
  } else if (msgType === 'image' && body.image) {
    type = 'image';
    // Download and decrypt image
    try {
      const { buffer, filename } = await wsClient.downloadFile(body.image.url, body.image.aeskey);
      const tmpPath = `/tmp/wecom_img_${msgId}.${_getExt(filename, 'jpg')}`;
      fs.writeFileSync(tmpPath, buffer);
      mediaPath = tmpPath;
    } catch (e) {
      console.error('[wecom-bridge] Image download failed:', e.message);
    }
  } else if (msgType === 'file' && body.file) {
    type = 'file';
    try {
      const { buffer, filename } = await wsClient.downloadFile(body.file.url, body.file.aeskey);
      const tmpPath = `/tmp/wecom_file_${msgId}_${filename || 'file'}`;
      fs.writeFileSync(tmpPath, buffer);
      mediaPath = tmpPath;
    } catch (e) {
      console.error('[wecom-bridge] File download failed:', e.message);
    }
  } else if (msgType === 'mixed' && body.mixed) {
    // Mixed message: extract text parts, download image parts
    const parts = (body.mixed.msg_item || []);
    const textParts = [];
    for (const part of parts) {
      if (part.msgtype === 'text' && part.text) {
        textParts.push(part.text.content || '');
      } else if (part.msgtype === 'image' && part.image) {
        try {
          const { buffer, filename } = await wsClient.downloadFile(part.image.url, part.image.aeskey);
          const tmpPath = `/tmp/wecom_mixed_${msgId}.${_getExt(filename, 'jpg')}`;
          fs.writeFileSync(tmpPath, buffer);
          if (!mediaPath) mediaPath = tmpPath;
        } catch (e) {
          console.error('[wecom-bridge] Mixed image download failed:', e.message);
        }
      }
    }
    text = textParts.join('\n');
    type = mediaPath ? 'mixed' : 'text';
  } else {
    // Unsupported message type
    console.log('[wecom-bridge] Unsupported message type:', msgType);
    return;
  }

  if (!text && !mediaPath) return;

  console.log('[wecom-bridge] inbound from:', userId, 'chat:', chatType, 'type:', type, 'text:', (text || '').substring(0, 50));

  const payload = JSON.stringify({
    from: userId,
    chat_id: chatId,
    chat_type: chatType,
    text,
    type,
    media_path: mediaPath,
    msg_id: msgId,
    // Pass the frame req_id so the channel can reply via WebSocket
    _req_id: frame.headers && frame.headers.req_id || '',
  });

  await _postToCore(CLAWBOT_INBOUND_PATH, payload);
}

function _getExt(filename, fallback) {
  if (!filename) return fallback;
  const dot = filename.lastIndexOf('.');
  return dot >= 0 ? filename.substring(dot + 1) : fallback;
}

// ── HTTP helpers ────────────────────────────────────────────────────────────────

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
    const req = http.request(options, (res) => {
      res.on('data', () => {});
      res.on('end', resolve);
    });
    req.on('error', reject);
    req.write(body);
    req.end();
  });
}

// ── Reply queue — Core calls POST /send, we reply via WeCom SDK ─────────────────
// The WeCom SDK uses WebSocket for replies, not HTTP.
// We store pending frames so the channel can reply by req_id.

const _pendingFrames = new Map(); // req_id -> frame
const FRAME_TTL_MS = 300000; // 5 minutes

function _storePendingFrame(frame) {
  const reqId = frame.headers && frame.headers.req_id;
  if (!reqId) return;
  _pendingFrames.set(reqId, { frame, ts: Date.now() });
  // Cleanup old frames
  for (const [k, v] of _pendingFrames) {
    if (Date.now() - v.ts > FRAME_TTL_MS) _pendingFrames.delete(k);
  }
}

// ── Express HTTP server ─────────────────────────────────────────────────────────
const app = express();
app.use(express.json());

app.get('/status', (_req, res) => {
  res.json({
    connected: connStatus === 'connected',
    status: connStatus,
    botId: currentBotId ? currentBotId.substring(0, 8) + '...' : null,
  });
});

app.post('/send', async (req, res) => {
  const { req_id, chat_id, text, stream } = req.body || {};

  if (!text) {
    return res.status(400).json({ ok: false, error: 'missing text' });
  }
  if (connStatus !== 'connected' || !wsClient) {
    return res.status(503).json({ ok: false, error: 'not connected' });
  }

  try {
    // Method 1: Reply via pending frame (WebSocket reply to user message)
    if (req_id && _pendingFrames.has(req_id)) {
      const { frame } = _pendingFrames.get(req_id);
      _pendingFrames.delete(req_id);

      if (stream) {
        // Streaming reply: single finish frame
        const streamId = generateReqId('stream');
        await wsClient.replyStream(frame, streamId, text, true);
      } else {
        // Single-shot reply via replyStream with finish=true
        const streamId = generateReqId('stream');
        await wsClient.replyStream(frame, streamId, text, true);
      }
      return res.json({ ok: true });
    }

    // Method 2: Active push (no frame context — send to chat_id or user)
    if (chat_id) {
      await wsClient.sendMessage(chat_id, {
        msgtype: 'markdown',
        markdown: { content: text },
      });
      return res.json({ ok: true });
    }

    return res.status(400).json({ ok: false, error: 'missing req_id or chat_id' });
  } catch (e) {
    console.error('[wecom-bridge] send error:', e.message);
    res.status(500).json({ ok: false, error: e.message });
  }
});

// Reload config and reconnect
app.post('/reconnect', (_req, res) => {
  console.log('[wecom-bridge] Reconnect requested');
  if (wsClient) {
    try { wsClient.disconnect(); } catch (_) {}
    wsClient = null;
  }
  connStatus = 'disconnected';
  createWeComClient();
  res.json({ ok: true, message: 'reconnecting' });
});

// ── Startup ─────────────────────────────────────────────────────────────────────
createWeComClient();

app.listen(BRIDGE_PORT, '127.0.0.1', () => {
  console.log(`[wecom-bridge] WeCom bridge listening on 127.0.0.1:${BRIDGE_PORT}`);
});
