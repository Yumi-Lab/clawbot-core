"""
WeComChannel — WeCom (Enterprise WeChat) channel via local Node.js bridge (port 3101).

Config: /etc/clawbot/clawbot.cfg -> [wecom] section
  bot_id = <from WeCom admin>
  secret = <from WeCom admin>
  allow_from = *                    -> accept messages from anyone
  allow_from = userid1,userid2     -> whitelist (comma-separated WeCom userids)
  default_model = default           -> model for LLM calls
  default_mode = core               -> routing mode (core, core-agent)

Slash commands (send via WeCom):
  /help    - list commands
  /model   - show/set model
  /mode    - show/set mode (core, core-agent)
  /reset   - clear conversation
  /status  - system stats
"""

import configparser
import json
import logging
import threading
import time
import urllib.request

from channels.base import ChannelBase, ChannelCapabilities

log = logging.getLogger(__name__)

BRIDGE_URL = "http://127.0.0.1:3101"
CONFIG_PATH = "/etc/clawbot/clawbot.cfg"
MAX_HISTORY = 10
STREAM_TIMEOUT = 300
PROGRESS_DEBOUNCE = 5

_TOOL_PROGRESS = {
    "system__web_search": "Searching the web...",
    "system__read_file": "Reading file...",
    "system__write_file": "Writing file...",
    "system__bash": "Running command...",
    "system__python": "Running Python...",
    "system__ssh": "Connecting via SSH...",
}


class WeComChannel(ChannelBase):
    """Channel for WeCom (Enterprise WeChat) — communicates via local bridge."""
    channel_id = "wecom"

    def __init__(self):
        self._histories: dict = {}   # userid -> list[{role, content}]
        self._user_prefs: dict = {}  # userid -> {"model": str, "mode": str}
        self._lock = threading.Lock()

    def start(self):
        pass  # Bridge is a separate Node.js process

    def stop(self):
        pass

    def send(self, session_id: str, message) -> None:
        """Send final response to a WeCom user. session_id = userid."""
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
            audio=False,  # Voice comes pre-transcribed, no audio send
            files=True,
            groups=True,
            max_message_length=20480,  # WeCom limit per reply
        )

    # -- Public helpers --------------------------------------------------------

    def is_available(self) -> bool:
        """Return True if the bridge is connected to WeCom."""
        try:
            with urllib.request.urlopen(f"{BRIDGE_URL}/status", timeout=3) as resp:
                data = json.loads(resp.read())
            return bool(data.get("connected", False))
        except Exception:
            return False

    def get_bridge_status(self) -> dict:
        """Return full bridge status dict."""
        try:
            with urllib.request.urlopen(f"{BRIDGE_URL}/status", timeout=3) as resp:
                return json.loads(resp.read())
        except Exception:
            return {"connected": False, "status": "error", "botId": None}

    def send_message(self, to: str, content: str, req_id: str = "",
                     chat_id: str = "") -> dict:
        """Send a text message via the bridge."""
        body = {"text": content}
        if req_id:
            body["req_id"] = req_id
        if chat_id:
            body["chat_id"] = chat_id
        elif not req_id:
            # Fallback: use userid as chat_id for active push
            body["chat_id"] = to

        payload = json.dumps(body).encode()
        req = urllib.request.Request(
            f"{BRIDGE_URL}/send", data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read())
        except Exception as e:
            log.error("WeCom send_message error: %s", e)
            return {"ok": False, "error": str(e)}

    def on_inbound(self, payload: dict):
        """
        Handle an inbound payload from the bridge.
        Runs the full LLM round-trip and sends the reply back.
        """
        sender = payload.get("from", "")
        if not sender:
            return None

        chat_id = payload.get("chat_id", "")
        chat_type = payload.get("chat_type", "single")
        req_id = payload.get("_req_id", "")

        if not self._is_allowed(sender):
            log.info("WeCom: ignoring message from %s (not in allow_from)", sender)
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
        if user_content.startswith("/"):
            if self._handle_command(sender, user_content.strip(), req_id, chat_id):
                return {"session_id": sender, "sender": sender, "text": user_content,
                        "type": "command", "media_path": None}

        # Run in background thread so the bridge gets immediate 200
        threading.Thread(
            target=self._reply_async,
            args=(sender, user_content, req_id, chat_id),
            daemon=True,
        ).start()

        return {
            "session_id": sender,
            "sender": sender,
            "text": user_content,
            "type": msg_type,
            "media_path": media_path,
        }

    # -- Slash commands --------------------------------------------------------

    def _handle_command(self, sender: str, text: str, req_id: str = "",
                        chat_id: str = "") -> bool:
        """Handle /commands. Returns True if handled."""
        parts = text.split(None, 1)
        cmd = parts[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""

        if cmd == "/help":
            self.send_message(sender,
                "ClawBot WeCom Commands:\n"
                "/model - show current model\n"
                "/model <name> - set model\n"
                "/models - list all available models\n"
                "/mode - show current mode\n"
                "/mode <core|agent> - set mode\n"
                "/reset - clear conversation\n"
                "/status - system stats\n"
                "/help - this message",
                req_id=req_id, chat_id=chat_id)
            return True

        if cmd == "/models":
            lines = ["**Available models:**\n"]
            lines.append("**Anthropic:**")
            lines.append("  `haiku` -> claude-haiku-4-5")
            lines.append("  `sonnet` -> claude-sonnet-4-6")
            lines.append("  `opus` -> claude-opus-4-6")
            lines.append("**Qwen:**")
            lines.append("  `qwen-flash` -> qwen3.5-flash")
            lines.append("  `qwen-plus` -> qwen3.5-plus")
            lines.append("  `qwen-max` -> qwen3-max")
            lines.append("  `qwen-coder` -> qwen3-coder-plus")
            lines.append("  `qwq` -> qwq-plus")
            lines.append("**Moonshot:**")
            lines.append("  `kimi` -> kimi-for-coding")
            lines.append("**DeepSeek:**")
            lines.append("  `deepseek` -> deepseek-chat")
            lines.append("\n`default` -> uses dashboard setting")
            self.send_message(sender, "\n".join(lines),
                              req_id=req_id, chat_id=chat_id)
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
            _VALID_MODELS = set(_MODEL_ALIASES.values()) | set(_MODEL_ALIASES.keys())
            prefs = self._get_user_prefs(sender)
            if arg:
                resolved = _MODEL_ALIASES.get(arg.lower(),
                           arg if arg in _VALID_MODELS else None)
                if resolved:
                    prefs["model"] = resolved
                    self._user_prefs[sender] = prefs
                    display = resolved if resolved != arg.lower() else arg
                    self.send_message(sender, f"Model set to: {display}",
                                      req_id=req_id, chat_id=chat_id)
                else:
                    self.send_message(sender,
                        f"Unknown model: {arg}\nUse /models to see available models.",
                        req_id=req_id, chat_id=chat_id)
            else:
                self.send_message(sender, f"Current model: {prefs['model']}",
                                  req_id=req_id, chat_id=chat_id)
            return True

        if cmd == "/mode":
            prefs = self._get_user_prefs(sender)
            if arg:
                mode = arg.lower().replace(" ", "-")
                if mode in ("core", "agent", "core-agent"):
                    prefs["mode"] = mode
                    self._user_prefs[sender] = prefs
                    self.send_message(sender, f"Mode set to: {mode}",
                                      req_id=req_id, chat_id=chat_id)
                else:
                    self.send_message(sender, "Valid modes: core, agent, core-agent",
                                      req_id=req_id, chat_id=chat_id)
            else:
                self.send_message(sender, f"Current mode: {prefs['mode']}",
                                  req_id=req_id, chat_id=chat_id)
            return True

        if cmd == "/reset":
            with self._lock:
                self._histories.pop(sender, None)
            self.send_message(sender, "Conversation cleared.",
                              req_id=req_id, chat_id=chat_id)
            return True

        if cmd == "/status":
            self._send_status(sender, req_id, chat_id)
            return True

        return False

    def _send_status(self, sender: str, req_id: str = "", chat_id: str = ""):
        """Send system stats via WeCom."""
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
        self.send_message(sender, text, req_id=req_id, chat_id=chat_id)

    # -- User preferences ------------------------------------------------------

    def _get_user_prefs(self, userid: str) -> dict:
        """Get user prefs, initialized from config defaults."""
        prefs = self._user_prefs.get(userid)
        if prefs:
            return prefs
        cfg = configparser.ConfigParser()
        cfg.read(CONFIG_PATH)
        prefs = {
            "model": cfg.get("wecom", "default_model", fallback="default"),
            "mode": cfg.get("wecom", "default_mode", fallback="core"),
        }
        self._user_prefs[userid] = prefs
        return prefs

    # -- Internal --------------------------------------------------------------

    def _reply_async(self, sender: str, user_content: str,
                     req_id: str = "", chat_id: str = "") -> None:
        """Call orchestrator via streaming, send reply. Background thread."""
        with self._lock:
            history = list(self._histories.get(sender, []))

        prefs = self._get_user_prefs(sender)

        reply = self._call_orchestrator_stream(
            user_content, history, sender,
            req_id=req_id, chat_id=chat_id,
            model=prefs["model"], mode=prefs["mode"])
        if not reply:
            return

        # Persist history
        history.append({"role": "user", "content": user_content})
        history.append({"role": "assistant", "content": reply})
        with self._lock:
            self._histories[sender] = history[-(MAX_HISTORY * 2):]

        # Send reply (chunked if needed, though WeCom allows 20KB)
        for chunk in self.chunk_text(reply):
            self.send_message(sender, chunk, req_id=req_id, chat_id=chat_id)

    def _call_orchestrator_stream(self, message: str, history: list,
                                   sender: str, req_id: str = "",
                                   chat_id: str = "",
                                   model: str = "default",
                                   mode: str = "core") -> str:
        """Call orchestrator in-process via streaming generator."""
        from orchestrator import chat_with_tools_stream

        body = {
            "model": model,
            "messages": history + [{"role": "user", "content": message}],
            "stream": True,
            "channel": "wecom",
        }

        gen = self._get_stream_generator(body, message, mode)

        final_content = ""
        partial_content = ""
        last_progress = 0
        start_time = time.time()

        try:
            for event in gen:
                elapsed = time.time() - start_time
                if elapsed > STREAM_TIMEOUT:
                    log.warning("WeCom: stream timeout after %.0fs", elapsed)
                    if partial_content:
                        return partial_content + "\n\n[Timeout - partial response]"
                    return "Task took too long. Please try a simpler request."

                etype = event.get("type")

                if etype == "tool_call":
                    now = time.time()
                    if now - last_progress >= PROGRESS_DEBOUNCE:
                        calls = event.get("calls", [])
                        tools = [c.get("tool", "") for c in calls]
                        progress_msg = self._tool_progress_text(tools)
                        self.send_message(sender, progress_msg,
                                          req_id="", chat_id=chat_id)
                        last_progress = now

                elif etype == "content_delta":
                    partial_content += event.get("text", "")

                elif etype == "done":
                    final_content = event.get("content", "")

                elif etype == "error":
                    log.error("WeCom orchestrator error: %s", event.get("message"))
                    return event.get("message", "An error occurred.")

        except Exception as e:
            log.error("WeCom orchestrator stream error: %s", e)
            if partial_content:
                return partial_content + "\n\n[Error - partial response]"
            return f"Error: {e}"

        return final_content

    def _get_stream_generator(self, body: dict, message: str, mode: str):
        """Return the appropriate streaming generator based on mode."""
        from orchestrator import chat_with_tools_stream

        if mode in ("agent", "core-agent"):
            try:
                from orchestrator import route_to_agents, chat_with_multi_agents_stream
                agent_ids = route_to_agents(message)
                if agent_ids:
                    log.info("WeCom: routing to agents %s", agent_ids)
                    return chat_with_multi_agents_stream(body, agent_ids)
            except ImportError:
                log.warning("WeCom: agent routing not available, falling back to core")
            except Exception as e:
                log.warning("WeCom: agent routing failed (%s), falling back to core", e)

        return chat_with_tools_stream(body)

    @staticmethod
    def _tool_progress_text(tool_names: list) -> str:
        """Map tool names to user-friendly progress text."""
        labels = []
        for t in tool_names:
            label = _TOOL_PROGRESS.get(t)
            if not label:
                clean = t.replace("system__", "").replace("__", " > ")
                label = f"Working ({clean})..."
            labels.append(label)
        return " | ".join(labels) if labels else "Working..."

    def _is_allowed(self, userid: str) -> bool:
        """Check [wecom] allow_from in clawbot.cfg."""
        try:
            cfg = configparser.ConfigParser()
            cfg.read(CONFIG_PATH)
            raw = cfg.get("wecom", "allow_from", fallback="*").strip()
            if raw == "*" or not raw:
                return True
            allowed = {n.strip() for n in raw.split(",") if n.strip()}
            return userid in allowed
        except Exception:
            return True
