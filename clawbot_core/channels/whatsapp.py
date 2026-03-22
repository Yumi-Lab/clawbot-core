"""
WhatsAppChannel — WhatsApp channel via local Node.js Baileys bridge (port 3100).

Config: /etc/clawbot/clawbot.cfg → [whatsapp] section
  allow_from = *                    → accept messages from anyone
  allow_from = +33612345678,+1...   → whitelist (comma-separated E.164 numbers)
  default_model = default           → model for LLM calls (default, haiku, sonnet, opus, kimi, qwen, deepseek...)
  default_mode = core               → routing mode (core, core-agent)

Slash commands (send via WhatsApp):
  /help    — list commands
  /model   — show/set model
  /mode    — show/set mode (core, core-agent)
  /reset   — clear conversation history
  /status  — system stats
"""

import configparser
import json
import logging
import os
import threading
import time
import urllib.request

from channels.base import ChannelBase, ChannelCapabilities

log = logging.getLogger(__name__)

BRIDGE_URL = "http://127.0.0.1:3100"
CONFIG_PATH = "/etc/clawbot/clawbot.cfg"
SESSIONS_DIR = "/home/pi/.openjarvis/sessions"
MAX_HISTORY = 10  # keep last N exchanges per phone number
PROGRESS_DEBOUNCE = 5  # min seconds between progress messages
UPSTREAM_INTERVAL = 30  # seconds — flush accumulated content to WhatsApp periodically

# Tool name → user-friendly progress message
_TOOL_PROGRESS = {
    "system__web_search": "Searching the web...",
    "system__read_file": "Reading file...",
    "system__write_file": "Writing file...",
    "system__bash": "Running command...",
    "system__python": "Running Python...",
    "system__ssh": "Connecting via SSH...",
}


class WhatsAppChannel(ChannelBase):
    """Channel for WhatsApp — communicates via local Baileys bridge."""
    channel_id = "whatsapp"

    def __init__(self):
        self._histories: dict = {}   # phone → list[{role, content}]
        self._user_prefs: dict = {}  # phone → {"model": str, "mode": str}
        self._lock = threading.Lock()
        self._active: dict = {}      # phone → True if a thread is currently processing
        self._queued: dict = {}      # phone → list of queued messages to process next

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
        except Exception:
            return {"connected": False, "status": "error", "phone": None, "qr": None}

    def send_message(self, to: str, content: str, content_type: str = "text") -> dict:
        """Send a text or audio message via the bridge."""
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
        # If alt_jid has a real phone JID, prefer it for sender identification
        alt_jid = payload.get("alt_jid", "")
        if alt_jid and "@s.whatsapp.net" in alt_jid:
            phone_part = alt_jid.split("@")[0].replace("+", "")
            raw_from = phone_part
        sender = self.normalize_sender(raw_from)
        reply_jid = payload.get("jid", "")

        if not self._is_allowed(sender):
            log.info("WhatsApp: ignoring message from %s (not in allow_from)", sender)
            return None

        msg_type = payload.get("type", "text")
        text = payload.get("text", "")
        media_path = payload.get("media_path")

        if text:
            user_content = text
        elif media_path:
            user_content = f"[{msg_type}: {media_path}]"
        else:
            return None

        # Handle slash commands before LLM call
        reply_to = reply_jid if reply_jid else sender
        if user_content.startswith("/"):
            if self._handle_command(sender, user_content.strip(), reply_to):
                return {"session_id": sender, "sender": sender, "text": user_content,
                        "type": "command", "media_path": None}

        # If a thread is already processing for this sender, queue the message
        with self._lock:
            if self._active.get(sender):
                self._queued.setdefault(sender, []).append(user_content)
                log.info("WhatsApp: queued message for %s (%d in queue)",
                         sender, len(self._queued[sender]))
                self.send_message(reply_to, "Message received, I'll handle it right after the current task.")
                return {"session_id": sender, "sender": sender, "text": user_content,
                        "type": msg_type, "media_path": media_path}
            self._active[sender] = True

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

    # ── Slash commands ─────────────────────────────────────────────────────────

    def _handle_command(self, sender: str, text: str, reply_to: str) -> bool:
        """Handle /commands. Returns True if handled."""
        parts = text.split(None, 1)
        cmd = parts[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""

        if cmd == "/help":
            self.send_message(reply_to,
                "ClawBot WhatsApp Commands:\n"
                "/model — show current model\n"
                "/model <name> — set model\n"
                "/models — list all available models\n"
                "/mode — show current mode\n"
                "/mode <core|agent> — set mode\n"
                "/reset — clear conversation\n"
                "/status — system stats\n"
                "/help — this message")
            return True

        if cmd == "/models":
            lines = ["*Available models:*\n"]
            lines.append("*Anthropic:*")
            lines.append("  `haiku` → claude-haiku-4-5")
            lines.append("  `sonnet` → claude-sonnet-4-6")
            lines.append("  `opus` → claude-opus-4-6")
            lines.append("*Qwen:*")
            lines.append("  `qwen-flash` → qwen3.5-flash")
            lines.append("  `qwen-plus` → qwen3.5-plus")
            lines.append("  `qwen-max` → qwen3-max")
            lines.append("  `qwen-coder` → qwen3-coder-plus")
            lines.append("  `qwq` → qwq-plus")
            lines.append("*Moonshot:*")
            lines.append("  `kimi` → kimi-for-coding")
            lines.append("*DeepSeek:*")
            lines.append("  `deepseek` → deepseek-chat")
            lines.append("\n`default` → uses dashboard setting")
            self.send_message(reply_to, "\n".join(lines))
            return True

        if cmd == "/model":
            _MODEL_ALIASES = {
                "default": "default",
                "haiku": "claude-haiku-4-5-20251001",
                "sonnet": "claude-sonnet-4-6",
                "opus": "claude-opus-4-6",
                "kimi": "kimi-for-coding",
                "qwen-flash": "qwen3.5-flash",
                "qwen-plus": "qwen3.5-plus",
                "qwen-max": "qwen3-max",
                "qwen-coder": "qwen3-coder-plus",
                "qwen-coder-flash": "qwen3-coder-flash",
                "qwq": "qwq-plus",
                "deepseek": "deepseek-chat",
            }
            # Also accept full model IDs directly
            _VALID_MODELS = set(_MODEL_ALIASES.values()) | set(_MODEL_ALIASES.keys())
            prefs = self._get_user_prefs(sender)
            if arg:
                resolved = _MODEL_ALIASES.get(arg.lower(), arg if arg in _VALID_MODELS else None)
                if resolved:
                    prefs["model"] = resolved
                    self._user_prefs[sender] = prefs
                    display = resolved if resolved != arg.lower() else arg
                    self.send_message(reply_to, f"Model set to: {display}")
                else:
                    self.send_message(reply_to, f"Unknown model: {arg}\nUse /models to see available models.")
            else:
                self.send_message(reply_to, f"Current model: {prefs['model']}")
            return True

        if cmd == "/mode":
            prefs = self._get_user_prefs(sender)
            if arg:
                mode = arg.lower().replace(" ", "-")
                if mode in ("core", "agent", "core-agent"):
                    prefs["mode"] = mode
                    self._user_prefs[sender] = prefs
                    self.send_message(reply_to, f"Mode set to: {mode}")
                else:
                    self.send_message(reply_to, "Valid modes: core, agent, core-agent")
            else:
                self.send_message(reply_to, f"Current mode: {prefs['mode']}")
            return True

        if cmd == "/reset":
            with self._lock:
                self._histories.pop(sender, None)
            # Delete session file on disk
            sid = self._session_id_for(sender)
            fpath = os.path.join(SESSIONS_DIR, sid + ".json")
            try:
                if os.path.isfile(fpath):
                    os.remove(fpath)
            except Exception:
                pass
            self.send_message(reply_to, "Conversation cleared.")
            return True

        if cmd == "/status":
            self._send_status(reply_to)
            return True

        return False  # Not a known command

    def _send_status(self, reply_to: str):
        """Send system stats via WhatsApp."""
        try:
            with urllib.request.urlopen("http://127.0.0.1:8089/stats", timeout=5) as resp:
                stats = json.loads(resp.read())
            text = (
                f"System Status\n"
                f"CPU: {stats.get('cpu', '?')}%\n"
                f"RAM: {stats.get('ram', '?')}%\n"
                f"Disk: {stats.get('disk', '?')}%\n"
                f"Temp: {stats.get('temp', '?')}C")
        except Exception:
            text = "Could not retrieve system status."
        self.send_message(reply_to, text)

    # ── User preferences ───────────────────────────────────────────────────────

    def _get_user_prefs(self, phone: str) -> dict:
        """Get user prefs (model, mode), initialized from config defaults."""
        prefs = self._user_prefs.get(phone)
        if prefs:
            return prefs
        cfg = configparser.ConfigParser()
        cfg.read(CONFIG_PATH)
        prefs = {
            "model": cfg.get("whatsapp", "default_model", fallback="default"),
            "mode": cfg.get("whatsapp", "default_mode", fallback="core"),
        }
        self._user_prefs[phone] = prefs
        return prefs

    # ── Session persistence (disk-backed, survives restarts) ─────────────────

    @staticmethod
    def _session_id_for(phone: str) -> str:
        """Convert phone to a safe session filename prefix."""
        return "wa_" + phone.replace("+", "").replace(" ", "")

    def _load_history(self, sender: str) -> list:
        """Load conversation history from disk session file, with in-memory cache."""
        with self._lock:
            cached = self._histories.get(sender)
            if cached is not None:
                return list(cached)

        sid = self._session_id_for(sender)
        fpath = os.path.join(SESSIONS_DIR, sid + ".json")
        try:
            if os.path.isfile(fpath):
                with open(fpath) as f:
                    session = json.load(f)
                msgs = session.get("messages", [])
                # Keep only user/assistant pairs (strip system, tool messages)
                history = [m for m in msgs if m.get("role") in ("user", "assistant")]
                history = history[-(MAX_HISTORY * 2):]
                with self._lock:
                    self._histories[sender] = history
                return list(history)
        except Exception as e:
            log.warning("WhatsApp: failed to load session %s: %s", sid, e)

        return []

    def _save_history(self, sender: str, history: list):
        """Save conversation history to disk session file and update cache."""
        trimmed = history[-(MAX_HISTORY * 2):]
        with self._lock:
            self._histories[sender] = trimmed

        sid = self._session_id_for(sender)
        fpath = os.path.join(SESSIONS_DIR, sid + ".json")
        try:
            os.makedirs(SESSIONS_DIR, exist_ok=True)
            session = {
                "id": sid,
                "title": f"WhatsApp {sender}",
                "messages": trimmed,
                "createdAt": int(time.time() * 1000),
                "updatedAt": int(time.time() * 1000),
            }
            # Preserve existing session metadata if file exists
            if os.path.isfile(fpath):
                try:
                    with open(fpath) as f:
                        existing = json.load(f)
                    session["createdAt"] = existing.get("createdAt", session["createdAt"])
                except Exception:
                    pass
            with open(fpath, "w") as f:
                json.dump(session, f)
        except Exception as e:
            log.warning("WhatsApp: failed to save session %s: %s", sid, e)

    # ── Internal ────────────────────────────────────────────────────────────────

    def _reply_async(self, sender: str, user_content: str, reply_jid: str = "") -> None:
        """Call orchestrator via in-process streaming, send reply. Background thread.
        Drains queued messages after each response (like Claude Code follow-ups)."""
        reply_to = reply_jid if reply_jid else sender
        current_message = user_content

        try:
            while True:
                history = self._load_history(sender)

                # ACK — confirm receipt immediately
                self.send_message(reply_to, "Got it, working on it...")

                prefs = self._get_user_prefs(sender)

                full_reply, sent_len = self._call_orchestrator_stream(
                    current_message, history, reply_to,
                    model=prefs["model"], mode=prefs["mode"])
                if not full_reply:
                    break

                # Persist history (memory + disk)
                history.append({"role": "user", "content": current_message})
                history.append({"role": "assistant", "content": full_reply})
                self._save_history(sender, history)

                # Send only the portion not yet sent upstream
                remainder = full_reply[sent_len:]
                if remainder.strip():
                    for chunk in self.chunk_text(remainder):
                        self.send_message(reply_to, chunk)

                # Check queue — process next message if any
                with self._lock:
                    queue = self._queued.get(sender, [])
                    if queue:
                        current_message = queue.pop(0)
                        if not queue:
                            del self._queued[sender]
                        log.info("WhatsApp: processing queued message for %s", sender)
                        continue
                    break
        finally:
            with self._lock:
                self._active.pop(sender, None)

    def _call_orchestrator_stream(self, message: str, history: list,
                                   reply_to: str, model: str = "default",
                                   mode: str = "core") -> tuple:
        """Call orchestrator in-process via streaming generator.
        Sends progress messages and upstream content flushes to WhatsApp.
        Returns (full_content, sent_length) — sent_length is how much was
        already sent upstream so _reply_async only sends the remainder."""
        from orchestrator import chat_with_tools_stream

        body = {
            "model": model,
            "messages": history + [{"role": "user", "content": message}],
            "stream": True,
            "channel": "whatsapp",
        }

        # Select generator based on mode
        gen = self._get_stream_generator(body, message, mode)

        final_content = ""
        partial_content = ""
        sent_len = 0  # how many chars already flushed upstream
        last_progress = 0
        last_upstream = time.time()

        try:
            for event in gen:
                etype = event.get("type")

                if etype == "tool_call":
                    now = time.time()
                    # Flush accumulated content before tool progress
                    sent_len = self._upstream_flush(
                        reply_to, partial_content, sent_len)
                    last_upstream = now
                    if now - last_progress >= PROGRESS_DEBOUNCE:
                        calls = event.get("calls", [])
                        tools = [c.get("tool", "") for c in calls]
                        progress_msg = self._tool_progress_text(tools)
                        self.send_message(reply_to, progress_msg)
                        last_progress = now

                elif etype == "content_delta":
                    partial_content += event.get("text", "")
                    # Periodic upstream flush
                    now = time.time()
                    if now - last_upstream >= UPSTREAM_INTERVAL:
                        sent_len = self._upstream_flush(
                            reply_to, partial_content, sent_len)
                        last_upstream = now

                elif etype == "done":
                    final_content = event.get("content", "")

                elif etype == "error":
                    log.error("WhatsApp orchestrator error event: %s", event.get("message"))
                    return (event.get("message", "An error occurred."), 0)

        except Exception as e:
            log.error("WhatsApp orchestrator stream error: %s", e)
            if partial_content:
                return (partial_content + "\n\n[Error — partial response]", sent_len)
            return (f"Error: {e}", 0)

        return (final_content or partial_content, sent_len)

    def _upstream_flush(self, reply_to: str, content: str, sent_len: int) -> int:
        """Send any unsent content upstream to WhatsApp. Returns new sent_len."""
        unsent = content[sent_len:]
        if unsent.strip():
            for chunk in self.chunk_text(unsent):
                self.send_message(reply_to, chunk)
            return len(content)
        return sent_len

    def _get_stream_generator(self, body: dict, message: str, mode: str):
        """Return the appropriate streaming generator based on mode."""
        from orchestrator import chat_with_tools_stream

        if mode in ("agent", "core-agent"):
            try:
                from orchestrator import route_to_agents, chat_with_multi_agents_stream
                agent_ids = route_to_agents(message)
                if agent_ids:
                    log.info("WhatsApp: routing to agents %s", agent_ids)
                    return chat_with_multi_agents_stream(body, agent_ids)
            except ImportError:
                log.warning("WhatsApp: agent routing not available, falling back to core")
            except Exception as e:
                log.warning("WhatsApp: agent routing failed (%s), falling back to core", e)

        return chat_with_tools_stream(body)

    @staticmethod
    def _tool_progress_text(tool_names: list) -> str:
        """Map tool names to user-friendly progress text."""
        labels = []
        for t in tool_names:
            label = _TOOL_PROGRESS.get(t)
            if not label:
                # Clean up module__tool format
                clean = t.replace("system__", "").replace("__", " > ")
                label = f"Working ({clean})..."
            labels.append(label)
        return " | ".join(labels) if labels else "Working..."

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
