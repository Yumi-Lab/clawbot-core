"""
TelegramChannel — Telegram Bot API polling channel.
Migrated from the standalone clawbot-telegram bot into the channel abstraction.
Config: /home/pi/.openjarvis-telegram.env
  TELEGRAM_TOKEN=<bot_token>
  ALLOWED_USERS=<comma-separated chat IDs, empty = allow all>
"""

import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request

from channels.base import ChannelBase, ChannelCapabilities, MessageIn

log = logging.getLogger(__name__)

ENV_FILE = "/home/pi/.openjarvis-telegram.env"
MAX_HISTORY = 10  # keep last N exchanges per chat


class TelegramChannel(ChannelBase):
    """Channel for Telegram — long-polling bot API."""
    channel_id = "telegram"

    def __init__(self):
        self.token = ""
        self.allowed_users = set()
        self._offset = 0
        self._running = False
        self._thread = None
        self._histories = {}  # chat_id -> list of message dicts

    def start(self):
        """Start polling in a background thread."""
        env = self._load_env()
        self.token = env.get("TELEGRAM_TOKEN", "")
        if not self.token:
            log.warning("TelegramChannel: no TELEGRAM_TOKEN in %s — not starting", ENV_FILE)
            return
        allowed_raw = env.get("ALLOWED_USERS", "")
        self.allowed_users = set(allowed_raw.split(",")) - {""} if allowed_raw else set()
        self._running = True
        self._thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._thread.start()
        log.info("TelegramChannel started (polling)")

    def stop(self):
        """Stop polling."""
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)
        log.info("TelegramChannel stopped")

    def send(self, session_id, message):
        """Send a message to a Telegram chat. session_id is the chat_id."""
        if message.event_type != "done":
            return  # Only send final responses (Telegram doesn't support streaming)
        if not message.content:
            return
        chunks = self.chunk_text(message.content)
        for chunk in chunks:
            self._send_message(session_id, chunk)

    def get_capabilities(self):
        return ChannelCapabilities(
            streaming=False,
            images=True,
            audio=True,
            files=True,
            groups=True,
            max_message_length=4000,  # Telegram limit is 4096, keep margin
        )

    # ── Internal methods ─────────────────────────────────────────────

    def _load_env(self):
        """Load config from env file."""
        env = {}
        try:
            with open(ENV_FILE) as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, _, v = line.partition("=")
                        env[k.strip()] = v.strip()
        except FileNotFoundError:
            pass
        return env

    def _tg_request(self, method, params=None):
        """Make a request to the Telegram Bot API."""
        url = f"https://api.telegram.org/bot{self.token}/{method}"
        data = json.dumps(params or {}).encode()
        req = urllib.request.Request(
            url, data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=35) as resp:
                return json.loads(resp.read())
        except Exception as e:
            log.warning("Telegram API error (%s): %s", method, e)
            return None

    def _send_message(self, chat_id, text):
        """Send a text message to a Telegram chat."""
        self._tg_request("sendMessage", {"chat_id": chat_id, "text": text})

    def _poll_loop(self):
        """Long-polling loop for Telegram updates."""
        while self._running:
            # Reload env on each cycle (live config reload)
            env = self._load_env()
            new_token = env.get("TELEGRAM_TOKEN", self.token)
            if new_token != self.token:
                self.token = new_token
                log.info("TelegramChannel: token reloaded")
            allowed_raw = env.get("ALLOWED_USERS", "")
            self.allowed_users = set(allowed_raw.split(",")) - {""} if allowed_raw else set()

            result = self._tg_request("getUpdates", {
                "offset": self._offset,
                "timeout": 30,
                "allowed_updates": ["message"],
            })

            if not result or not result.get("ok"):
                time.sleep(5)
                continue

            for update in result.get("result", []):
                self._offset = update["update_id"] + 1
                try:
                    self._handle_update(update)
                except Exception as e:
                    log.error("TelegramChannel update error: %s", e)

    def _handle_update(self, update):
        """Process a single Telegram update."""
        msg = update.get("message", {})
        chat_id = str(msg.get("chat", {}).get("id", ""))
        user_id = str(msg.get("from", {}).get("id", ""))
        text = msg.get("text", "").strip()

        if not chat_id or not text:
            return

        if self.allowed_users and user_id not in self.allowed_users:
            log.info("TelegramChannel: ignored message from user %s (not allowed)", user_id)
            self._send_message(chat_id, "Sorry, you are not authorized to use this bot.")
            return

        # Handle commands
        if text == "/start":
            self._histories.pop(chat_id, None)
            self._send_message(chat_id,
                "ClawbotOS AI Bot\n\nSend me any message to chat with the AI.\n"
                "Use /reset to start a new conversation.")
            return

        if text == "/reset":
            self._histories.pop(chat_id, None)
            self._send_message(chat_id, "Conversation reset.")
            return

        if text == "/status":
            self._handle_status_command(chat_id)
            return

        # Regular message — send to orchestrator via non-streaming API
        log.info("TelegramChannel user %s: %s", user_id, text[:80])
        history = self._histories.get(chat_id, [])
        reply = self._chat_with_orchestrator(text, history)
        self._send_message(chat_id, reply)

        # Update history
        history.append({"role": "user", "content": text})
        history.append({"role": "assistant", "content": reply})
        self._histories[chat_id] = history[-(MAX_HISTORY * 2):]

    def _chat_with_orchestrator(self, message, history):
        """Send message to ClawbotCore orchestrator via local HTTP API."""
        messages = history + [{"role": "user", "content": message}]
        payload = json.dumps({
            "model": "default",
            "messages": messages,
            "stream": False,
            "channel": "telegram",
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
            body = e.read().decode()
            log.error("Orchestrator HTTP error %s: %s", e.code, body)
            return f"[Error {e.code}] {body[:200]}"
        except Exception as e:
            log.error("Orchestrator error: %s", e)
            return f"[Connection error] {e}"

    def _handle_status_command(self, chat_id):
        """Handle /status command — fetch system stats."""
        try:
            with urllib.request.urlopen("http://127.0.0.1:8089/stats", timeout=5) as resp:
                stats = json.loads(resp.read())
            status_text = (
                f"System Status\n"
                f"CPU: {stats.get('cpu', '?')}%\n"
                f"RAM: {stats.get('ram', '?')}%\n"
                f"Disk: {stats.get('disk', '?')}%\n"
                f"Temp: {stats.get('temp', '?')}°C"
            )
        except Exception:
            status_text = "Could not retrieve system status."
        self._send_message(chat_id, status_text)
