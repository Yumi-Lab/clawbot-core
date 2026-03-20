"""
WhatsAppChannel — WhatsApp channel via local Node.js Baileys bridge (port 3100).

Config: /etc/clawbot/clawbot.cfg → [whatsapp] section
  allow_from = *                    → accept messages from anyone
  allow_from = +33612345678,+1...   → whitelist (comma-separated E.164 numbers)
"""

import configparser
import json
import logging
import threading
import urllib.error
import urllib.request

from channels.base import ChannelBase, ChannelCapabilities

log = logging.getLogger(__name__)

BRIDGE_URL = "http://127.0.0.1:3100"
CONFIG_PATH = "/etc/clawbot/clawbot.cfg"
MAX_HISTORY = 10  # keep last N exchanges per phone number


class WhatsAppChannel(ChannelBase):
    """Channel for WhatsApp — communicates via local Baileys bridge."""
    channel_id = "whatsapp"

    def __init__(self):
        self._histories: dict = {}   # phone → list[{role, content}]
        self._lock = threading.Lock()

    def start(self):
        pass  # Bridge is a separate Node.js process

    def stop(self):
        pass

    def send(self, session_id: str, message) -> None:
        """Send final response to a WhatsApp number. session_id = E.164 phone."""
        if message.event_type != "done":
            return
        if not message.content:
            return
        for chunk in self.chunk_text(message.content):
            self.send_message(session_id, chunk)

    def get_capabilities(self) -> ChannelCapabilities:
        return ChannelCapabilities(
            streaming=False,
            images=True,
            audio=True,
            files=False,
            groups=False,
            max_message_length=4000,
        )

    # ── Public helpers ─────────────────────────────────────────────────────────

    def is_available(self) -> bool:
        """Return True if the bridge is connected to WhatsApp."""
        try:
            with urllib.request.urlopen(f"{BRIDGE_URL}/status", timeout=3) as resp:
                data = json.loads(resp.read())
            return bool(data.get("connected", False))
        except Exception:
            return False

    def get_bridge_status(self) -> dict:
        """Return full bridge status dict: { connected, status, phone, qr }."""
        try:
            with urllib.request.urlopen(f"{BRIDGE_URL}/status", timeout=3) as resp:
                return json.loads(resp.read())
        except Exception as e:
            return {"connected": False, "status": "error", "phone": None, "qr": None}

    def send_message(self, to: str, content: str, content_type: str = "text") -> dict:
        """Send a text or audio message via the bridge."""
        # If 'to' contains @ it's a raw JID, pass as jid field
        is_jid = "@" in to
        if content_type == "audio":
            body = {"audio_path": content}
            body["jid" if is_jid else "to"] = to
            payload = json.dumps(body).encode()
            endpoint = f"{BRIDGE_URL}/send-audio"
        else:
            body = {"text": content}
            body["jid" if is_jid else "to"] = to
            payload = json.dumps(body).encode()
            endpoint = f"{BRIDGE_URL}/send"

        req = urllib.request.Request(
            endpoint, data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read())
        except Exception as e:
            log.error("WhatsApp send_message error: %s", e)
            return {"ok": False, "error": str(e)}

    def normalize_sender(self, raw_from: str) -> str:
        """Ensure phone number has leading +."""
        raw = raw_from.strip()
        if not raw.startswith("+"):
            return "+" + raw
        return raw

    def on_inbound(self, payload: dict):
        """
        Handle an inbound payload from the bridge.
        Runs the full LLM round-trip and sends the reply back.
        Returns normalized info dict, or None if the sender is filtered.
        """
        raw_from = payload.get("from", "")
        sender = self.normalize_sender(raw_from)
        # JID for reply — use original JID from bridge (supports @lid format)
        reply_jid = payload.get("jid", "")

        if not self._is_allowed(sender):
            log.info("WhatsApp: ignoring message from %s (not in allow_from)", sender)
            return None

        msg_type = payload.get("type", "text")
        text = payload.get("text", "")
        media_path = payload.get("media_path")

        # Build user content string
        if text:
            user_content = text
        elif media_path:
            user_content = f"[{msg_type}: {media_path}]"
        else:
            return None  # Nothing to process

        # Run in a background thread so the bridge gets an immediate 200
        threading.Thread(
            target=self._reply_async,
            args=(sender, user_content, reply_jid),
            daemon=True,
        ).start()

        return {
            "session_id": sender,
            "sender": sender,
            "text": user_content,
            "type": msg_type,
            "media_path": media_path,
        }

    # ── Internal ────────────────────────────────────────────────────────────────

    def _reply_async(self, sender: str, user_content: str, reply_jid: str = "") -> None:
        """Call orchestrator, send reply. Runs in a background thread."""
        with self._lock:
            history = list(self._histories.get(sender, []))

        reply = self._call_orchestrator(user_content, history)
        if not reply:
            return

        # Persist history
        history.append({"role": "user", "content": user_content})
        history.append({"role": "assistant", "content": reply})
        with self._lock:
            self._histories[sender] = history[-(MAX_HISTORY * 2):]

        # Send reply in chunks respecting WhatsApp 4000-char limit
        # Use JID if available (supports @lid format), fallback to phone number
        reply_to = reply_jid if reply_jid else sender
        for chunk in self.chunk_text(reply):
            self.send_message(reply_to, chunk)

    def _call_orchestrator(self, message: str, history: list) -> str:
        """POST to ClawbotCore /v1/chat/completions (non-streaming)."""
        messages = history + [{"role": "user", "content": message}]
        payload = json.dumps({
            "model": "default",
            "messages": messages,
            "stream": False,
            "channel": "whatsapp",
        }).encode()

        req = urllib.request.Request(
            "http://127.0.0.1:8090/v1/chat/completions",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                data = json.loads(resp.read())
            return data["choices"][0]["message"]["content"]
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")
            log.error("WhatsApp orchestrator HTTP %s: %s", e.code, body[:200])
            return ""
        except Exception as e:
            log.error("WhatsApp orchestrator error: %s", e)
            return ""

    def _is_allowed(self, phone: str) -> bool:
        """Check [whatsapp] allow_from in /etc/clawbot/clawbot.cfg."""
        try:
            cfg = configparser.ConfigParser()
            cfg.read(CONFIG_PATH)
            raw = cfg.get("whatsapp", "allow_from", fallback="*").strip()
            if raw == "*" or not raw:
                return True
            allowed = {n.strip() for n in raw.split(",") if n.strip()}
            return phone in allowed
        except Exception:
            return True  # Fail open if config unreadable
