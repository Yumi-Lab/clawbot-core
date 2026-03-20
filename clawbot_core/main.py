#!/usr/bin/env python3
"""
ClawbotCore — Module Registry & Middleware
Lightweight HTTP API on 127.0.0.1:8090

Endpoints:
  GET  /core/health
  GET  /core/modules               — list all modules (installed + store)
  GET  /core/modules/{id}          — module details
  POST /core/modules/{id}/install  — install from repo   body: {"repo": "https://..."}
  GET  /core/updates               — list managed repos + update status (clawbot.cfg)
  POST /core/updates/{name}/update — git pull + restart services for a managed repo
  POST /core/modules/{id}/enable   — enable (start service)
  POST /core/modules/{id}/disable  — disable (stop service)
  POST /core/modules/{id}/uninstall

  GET  /core/sessions              — list chat sessions
  GET  /core/sessions/{id}         — get session (messages + metadata)
  POST /core/sessions/{id}         — create/update session  body: {name, mode, messages}
  DELETE /core/sessions/{id}       — delete session

  GET  /core/workspace             — list workspace files
  GET  /core/workspace/{file}      — download workspace file

  GET  /core/skills                — list all skills (including disabled)
  GET  /core/skills/{id}           — get skill by id
  POST /core/skills[/{id}]         — create/update skill  body: {id, name, triggers, instructions, ...}
  POST /core/skills/{id}/enable    — enable skill
  POST /core/skills/{id}/disable   — disable skill
  DELETE /core/skills/{id}         — delete skill (builtin skills cannot be deleted)

  POST /v1/chat/completions        — tool-aware chat proxy (→ ClawBot Core + module tools)
"""

import base64
import configparser
import json
import logging
import os
import re
import signal
import subprocess
import sys
import threading
import time
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
from urllib.parse import urlparse, unquote, parse_qs

SESSIONS_DIR = "/home/pi/.clawbot/sessions"
SESSIONS_TRASH_DIR = "/home/pi/.clawbot/sessions_trash"


def _save_assistant_to_session(session_id, content):
    """Background-save assistant response to session file after client disconnect."""
    if not session_id or not content:
        return
    try:
        fpath = os.path.join(SESSIONS_DIR, session_id + ".json")
        if not os.path.isfile(fpath):
            return
        with open(fpath) as f:
            session = json.load(f)
        msgs = session.get("messages", [])
        # Only append if last message is user (no assistant response yet)
        if msgs and msgs[-1].get("role") == "user":
            msgs.append({"role": "assistant", "content": content})
            session["messages"] = msgs
            session["updatedAt"] = int(time.time() * 1000)
            with open(fpath, "w") as f:
                json.dump(session, f)
            log.info("Background save: response appended to session %s", session_id)
    except Exception as e:
        log.warning("Background session save failed: %s", e)

AGENT_SESSION_FILE = "/home/pi/.picoclaw/workspace/sessions/agent_main_main.json"
AGENT_WORKSPACE    = "/home/pi/.picoclaw/workspace"
AGENT_TOKEN_THRESHOLD = 10000  # estimated tokens before compaction
AGENT_KEEP_RECENT = 8          # keep last N messages verbatim

from registry import get_all_modules, load_local_modules
from installer import install, uninstall, enable, disable
from update_manager import list_updates, do_update

# Channel abstraction layer
from channels.router import get_router as _get_channel_router
from channels.web import WebChannel as _WebChannel
from channels.api import APIChannel as _APIChannel
from channels.telegram import TelegramChannel as _TelegramChannel
from channels.voice import VoiceChannel as _VoiceChannel
from channels.whatsapp import WhatsAppChannel as _WhatsAppChannel

def _init_channels():
    """Initialize channel router with default channels."""
    router = _get_channel_router()
    if not router.get_channel("web"):
        router.register(_WebChannel())
    if not router.get_channel("api"):
        router.register(_APIChannel())
    if not router.get_channel("telegram"):
        router.register(_TelegramChannel())
    if not router.get_channel("voice"):
        router.register(_VoiceChannel())
    if not router.get_channel("whatsapp"):
        router.register(_WhatsAppChannel())
    return router

_channel_router = _init_channels()

# ── WhatsApp bridge process management ────────────────────────────────────────

_WA_BRIDGE_DIR = "/usr/local/lib/clawbot-core/modules/whatsapp-bridge"
_wa_proc = None

def _wa_start_bridge(allow_from: str = "*"):
    """Lance le bridge Baileys en background si pas déjà actif."""
    global _wa_proc
    if _wa_proc and _wa_proc.poll() is None:
        log.info("WA bridge already running (pid=%d)", _wa_proc.pid)
        return
    bridge_js = os.path.join(_WA_BRIDGE_DIR, "bridge.js")
    if not os.path.exists(bridge_js):
        log.error("WA bridge not found at %s", bridge_js)
        return
    env = os.environ.copy()
    env["ALLOW_FROM"] = allow_from
    env["CORE_URL"] = "http://127.0.0.1:8090"
    env["AUTH_DIR"] = "/home/pi/.clawbot/wa-auth"
    try:
        _wa_proc = subprocess.Popen(
            ["node", bridge_js],
            cwd=_WA_BRIDGE_DIR,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        log.info("WA bridge started (pid=%d)", _wa_proc.pid)
    except Exception as e:
        log.error("Failed to start WA bridge: %s", e)

def _wa_stop_bridge():
    """Arrête le bridge Baileys."""
    global _wa_proc
    if _wa_proc and _wa_proc.poll() is None:
        _wa_proc.terminate()
        log.info("WA bridge stopped")
    _wa_proc = None


PORT = 8090

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)

_LOG_PAT = re.compile(r"^\d{4}/\d{2}/\d{2} ")
_ERR_PAT = re.compile(r"(Error:|Traceback|ModuleNotFoundError|ImportError|exit status \d|Exit code:)", re.I)


def _maybe_compact_agent_session():
    """Auto-compact the agent session when it exceeds the token threshold."""
    try:
        if not os.path.exists(AGENT_SESSION_FILE):
            return

        with open(AGENT_SESSION_FILE) as f:
            session = json.load(f)

        msgs = session.get("messages", [])
        if not msgs:
            return

        # Estimate token count (~4 chars/token)
        total_chars = sum(len(str(m.get("content", ""))) for m in msgs)
        if total_chars // 4 < AGENT_TOKEN_THRESHOLD:
            return  # No compaction needed

        to_summarize = msgs[:-AGENT_KEEP_RECENT]
        recent = msgs[-AGENT_KEEP_RECENT:]

        if len(to_summarize) < 4:
            return  # Not enough old messages to make compaction worthwhile

        # Load LLM config from device config (status-api)
        try:
            import urllib.request as _ur
            with _ur.urlopen("http://127.0.0.1:8089/config", timeout=3) as _r:
                _dcfg = json.loads(_r.read())
            base = _dcfg.get("baseurl", "").rstrip("/")
            api_key = _dcfg.get("apikey", "")
            model = _dcfg.get("model", "")
            url = f"{base}/chat/completions" if base and api_key else None
        except Exception:
            url = None

        if not url:
            log.warning("Agent compaction: no LLM config, skipping")
            return

        # Call LLM to summarize the old messages
        import urllib.request, urllib.error
        summary_body = json.dumps({
            "model": model,
            "stream": False,
            "max_tokens": 800,
            "messages": to_summarize + [{
                "role": "user",
                "content": (
                    "Provide a concise summary of the conversation above. "
                    "Include: what was accomplished, files created or modified, "
                    "tools used, current state, and key context needed to continue. "
                    "Be brief but complete."
                ),
            }],
        }).encode()

        headers = {"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"}
        req = urllib.request.Request(url, data=summary_body, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read())
        summary_text = result["choices"][0]["message"]["content"]

        # Replace session history with compacted version
        compacted_msgs = [
            {"role": "user", "content": f"[Context summary — conversation was compacted]\n\n{summary_text}"},
            {"role": "assistant", "content": "Understood. I'll continue based on this summary."},
        ] + recent
        session["messages"] = compacted_msgs

        with open(AGENT_SESSION_FILE, "w") as f:
            json.dump(session, f)

        log.info("Agent session compacted: %d → %d messages (~%d tokens freed)",
                 len(msgs), len(compacted_msgs), total_chars // 4)

    except Exception as e:
        log.warning("Agent session compaction failed (non-fatal): %s", e)


def _parse_agent_steps(trace_raw: str, clean_final: str) -> list:
    """Parse agent raw output into structured step list for UI rendering.
    Each step: {"type": "code"|"error"|"output", "text": str, "n": int}
    """
    steps = []
    block, btype = [], None
    n = 0

    def flush():
        nonlocal n
        t = "\n".join(block).strip()
        if t:
            n += 1
            steps.append({"n": n, "type": btype or "code", "text": t})
        block.clear()

    for raw_line in trace_raw.splitlines():
        line = raw_line
        # Skip log lines and 🦀 lines
        if _LOG_PAT.match(line) or line.strip().startswith("🦀"):
            continue
        stripped = line.strip()
        if not stripped:
            flush()
            btype = None
            continue
        # Classify line
        if _ERR_PAT.search(line):
            new_type = "error"
        elif stripped.startswith("File ") and '"' in stripped:
            new_type = "error"
        else:
            new_type = "code"
        # Switch block type on change
        if btype is not None and new_type != btype:
            flush()
            btype = new_type
        else:
            btype = new_type
        block.append(line)

    flush()
    return steps


# ─── API helpers ──────────────────────────────────────────────────────────────

MANAGED_SERVICES = [
    "clawbot-core", "nginx",
    "clawbot-cloud", "clawbot-status-api", "clawbot-telegram",
    "clawbot-kiosk", "wifi-watchdog",
]


def _get_models():
    """Return available models in OpenAI /v1/models list format."""
    models = []
    seen = set()
    for mid in ["claude-haiku-4-5-20251001", "claude-sonnet-4-6", "claude-opus-4-6", "kimi-for-coding"]:
        seen.add(mid)
        models.append({"id": mid, "object": "model", "created": 1704067200, "owned_by": "clawbot"})
    for mid in ["clawbot", "clawbot-core"]:
        if mid not in seen:
            models.append({"id": mid, "object": "model",
                           "created": 1704067200, "owned_by": "clawbot"})
    return models


def _get_system_stats():
    """Read system stats from /proc — non-blocking."""
    stats = {}
    try:
        with open("/proc/loadavg") as f:
            la = f.read().split()
        stats["load_avg_1m"] = float(la[0])
        stats["load_avg_5m"] = float(la[1])
        stats["load_avg_15m"] = float(la[2])
    except Exception:
        pass
    try:
        mem = {}
        with open("/proc/meminfo") as f:
            for line in f:
                p = line.split(":")
                if len(p) == 2:
                    mem[p[0].strip()] = int(p[1].strip().split()[0])
        total = mem.get("MemTotal", 1)
        avail = mem.get("MemAvailable", total)
        stats.update({
            "ram_total_mb": total // 1024,
            "ram_used_mb": (total - avail) // 1024,
            "ram_percent": round(100.0 * (total - avail) / total, 1),
        })
    except Exception:
        stats.update({"ram_total_mb": 0, "ram_used_mb": 0, "ram_percent": 0.0})
    try:
        st = os.statvfs("/")
        total_b = st.f_blocks * st.f_frsize
        free_b = st.f_bfree * st.f_frsize
        stats.update({
            "disk_total_gb": round(total_b / 1e9, 1),
            "disk_used_gb": round((total_b - free_b) / 1e9, 1),
            "disk_percent": round(100.0 * (total_b - free_b) / total_b, 1),
        })
    except Exception:
        stats.update({"disk_total_gb": 0, "disk_used_gb": 0, "disk_percent": 0.0})
    try:
        with open("/sys/class/thermal/thermal_zone0/temp") as f:
            stats["temp_c"] = round(int(f.read().strip()) / 1000.0, 1)
    except Exception:
        stats["temp_c"] = 0.0
    return stats


def _service_status(name):
    try:
        r = subprocess.run(["systemctl", "is-active", name],
                           capture_output=True, text=True, timeout=3)
        return r.stdout.strip()
    except Exception:
        return "unknown"


def _service_control(name, action):
    """action: start | stop | restart. Returns (ok, message)."""
    if name not in MANAGED_SERVICES:
        return False, "service not in allowed list"
    if name == "clawbot-core" and action == "stop":
        return False, "cannot stop clawbot-core via API (would kill this service)"
    try:
        r = subprocess.run(["sudo", "systemctl", action, name],
                           capture_output=True, text=True, timeout=10)
        return r.returncode == 0, r.stderr.strip() or "ok"
    except Exception as e:
        return False, str(e)


def _get_config_raw():
    """Return (cfg_dict, error_str). Reads device config from status-api."""
    try:
        import urllib.request as _ur
        with _ur.urlopen("http://127.0.0.1:8089/config", timeout=3) as _r:
            return json.loads(_r.read()), None
    except Exception as e:
        return None, str(e)


def _mask_config(cfg):
    """Return deep copy of config with API keys masked for safe display."""
    import copy
    c = copy.deepcopy(cfg)
    for entry in c.get("model_list", []):
        k = entry.get("api_key", "")
        if k:
            entry["api_key"] = k[:8] + "…" + k[-4:] if len(k) > 12 else "***"
    return c


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # silence access logs

    def send_json(self, code, data):
        body = json.dumps(data, indent=2).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.end_headers()

    def do_PUT(self):
        """Route PUT requests to do_POST — REST semantics for config updates."""
        self.do_POST()

    def do_GET(self):
        path = urlparse(self.path).path.rstrip("/")

        if path == "/core/health":
            self.send_json(200, {"ok": True, "service": "clawbot-core"})

        elif path == "/core/version":
            vf = os.path.join(os.path.dirname(__file__), "..", "VERSION")
            try:
                with open(vf) as f:
                    ver = f.read().strip()
            except Exception:
                ver = "unknown"
            self.send_json(200, {"version": ver})

        elif path == "/core/modules":
            try:
                modules = get_all_modules()
                self.send_json(200, {"modules": list(modules.values())})
            except Exception as e:
                self.send_json(500, {"error": str(e)})

        elif path.startswith("/core/modules/"):
            module_id = path[len("/core/modules/"):]
            modules = get_all_modules()
            if module_id in modules:
                self.send_json(200, modules[module_id])
            else:
                self.send_json(404, {"error": f"Module '{module_id}' not found"})

        elif path == "/core/updates":
            try:
                self.send_json(200, {"updates": list_updates()})
            except Exception as e:
                self.send_json(500, {"error": str(e)})

        elif path.startswith("/core/workspace/"):
            import mimetypes
            filename = path[len("/core/workspace/"):]
            # Prevent directory traversal
            if ".." in filename or filename.startswith("/"):
                self.send_json(400, {"error": "invalid filename"})
                return
            workspace = AGENT_WORKSPACE
            filepath = os.path.join(workspace, filename)
            if not os.path.isfile(filepath):
                self.send_json(404, {"error": "file not found"})
                return
            mime, _ = mimetypes.guess_type(filepath)
            mime = mime or "application/octet-stream"
            with open(filepath, "rb") as f:
                data = f.read()
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(data)

        elif path == "/core/workspace":
            workspace = AGENT_WORKSPACE
            qs = parse_qs(urlparse(self.path).query)
            sub = qs.get("path", [""])[0].strip("/")
            target = os.path.normpath(os.path.join(workspace, sub)) if sub else workspace
            if not target.startswith(workspace):
                self.send_json(403, {"error": "Forbidden"}); return
            try:
                entries = []
                for f in sorted(os.listdir(target), key=lambda x: x.lower()):
                    fp = os.path.join(target, f)
                    st = os.stat(fp)
                    entries.append({
                        "name": f,
                        "path": (sub + "/" + f).lstrip("/") if sub else f,
                        "size": st.st_size,
                        "mtime": st.st_mtime,
                        "type": "dir" if os.path.isdir(fp) else "file"
                    })
                self.send_json(200, {"files": entries, "path": sub})
            except Exception as e:
                self.send_json(500, {"error": str(e)})

        elif path == "/core/sessions":
            try:
                os.makedirs(SESSIONS_DIR, exist_ok=True)
                sessions = []
                for fname in sorted(os.listdir(SESSIONS_DIR)):
                    if not fname.endswith(".json"):
                        continue
                    try:
                        with open(os.path.join(SESSIONS_DIR, fname)) as f:
                            s = json.load(f)
                        sessions.append({k: s[k] for k in ("id","name","mode","createdAt","updatedAt") if k in s})
                    except Exception:
                        pass
                sessions.sort(key=lambda s: s.get("updatedAt", 0), reverse=True)
                self.send_json(200, {"sessions": sessions})
            except Exception as e:
                self.send_json(500, {"error": str(e)})

        elif path == "/core/sessions/trash":
            try:
                os.makedirs(SESSIONS_TRASH_DIR, exist_ok=True)
                trash = []
                for fname in sorted(os.listdir(SESSIONS_TRASH_DIR), reverse=True):
                    if not fname.endswith(".json"):
                        continue
                    try:
                        with open(os.path.join(SESSIONS_TRASH_DIR, fname)) as f:
                            s = json.load(f)
                        trash.append({k: s[k] for k in ("id","name","mode","createdAt","updatedAt") if k in s})
                    except Exception:
                        pass
                self.send_json(200, {"sessions": trash})
            except Exception as e:
                self.send_json(500, {"error": str(e)})

        elif path.startswith("/core/sessions/"):
            sid = path[len("/core/sessions/"):]
            if ".." in sid or "/" in sid:
                self.send_json(400, {"error": "invalid id"})
                return
            fpath = os.path.join(SESSIONS_DIR, sid + ".json")
            if not os.path.isfile(fpath):
                self.send_json(404, {"error": "session not found"})
                return
            try:
                with open(fpath) as f:
                    self.send_json(200, json.load(f))
            except Exception as e:
                self.send_json(500, {"error": str(e)})

        elif path == "/core/agents":
            from orchestrator import load_agents
            try:
                agents = load_agents()
                self.send_json(200, {"agents": list(agents.values())})
            except Exception as e:
                self.send_json(500, {"error": str(e)})

        elif path.startswith("/core/agents/"):
            from orchestrator import load_agents
            agent_id = path[len("/core/agents/"):]
            agents = load_agents()
            if agent_id in agents:
                self.send_json(200, agents[agent_id])
            else:
                self.send_json(404, {"error": f"Agent '{agent_id}' not found"})

        elif path == "/core/skills":
            from skills import load_skills
            try:
                skills = load_skills(include_disabled=True)
                self.send_json(200, {"skills": list(skills.values())})
            except Exception as e:
                self.send_json(500, {"error": str(e)})

        elif path.startswith("/core/skills/"):
            from skills import load_skills
            skill_id = path[len("/core/skills/"):]
            if ".." in skill_id or "/" in skill_id:
                self.send_json(400, {"error": "invalid id"})
                return
            skills = load_skills(include_disabled=True)
            if skill_id in skills:
                self.send_json(200, skills[skill_id])
            else:
                self.send_json(404, {"error": f"Skill '{skill_id}' not found"})

        # ── Search engines ────────────────────────────────────────────────────
        elif path == "/core/search-engines":
            from orchestrator import _load_search_engines
            self.send_json(200, _load_search_engines())

        elif path == "/core/search-engines/scan":
            _scan_path = "/home/pi/.clawbot/scan-results.json"
            try:
                with open(_scan_path) as _sf:
                    self.send_json(200, json.load(_sf))
            except FileNotFoundError:
                self.send_json(200, {"results": [], "model": None, "scanned_at": None})
            except Exception as e:
                self.send_json(500, {"error": str(e)})

        elif path == "/core/search-engines/models":
            # Always return the 4 known ClawBot models (configured via device config)
            models = [
                {"model": "claude-haiku-4-5-20251001", "provider": "Anthropic", "group": "Anthropic", "configured": True},
                {"model": "claude-sonnet-4-6",         "provider": "Anthropic", "group": "Anthropic", "configured": True},
                {"model": "claude-opus-4-6",           "provider": "Anthropic", "group": "Anthropic", "configured": True},
                {"model": "kimi-for-coding",           "provider": "Moonshot",  "group": "Moonshot",  "configured": True},
            ]
            self.send_json(200, {"models": models, "source": "device-config"})

        # ── Core prompts (system prompt + extra rules) ───────────────────────
        elif path == "/core/region":
            from orchestrator import _detect_region, _region_cache
            if "force=1" in self.path:
                _region_cache["ts"] = 0  # invalidate cache
            self.send_json(200, {"country_code": _detect_region()})
            return

        elif path == "/core/prompts":
            from orchestrator import DEFAULT_SYSTEM_PROMPT, CORE_PROMPTS_PATH, _TWINS_REVIEWER_PROMPT
            try:
                with open(CORE_PROMPTS_PATH) as f:
                    data = json.load(f)
            except FileNotFoundError:
                data = {"system_prompt": DEFAULT_SYSTEM_PROMPT, "extra_rules": ""}
            except Exception as e:
                self.send_json(500, {"error": str(e)})
                return
            # Inject default twins reviewer prompt if not yet saved
            if "twins_reviewer_prompt" not in data:
                data["twins_reviewer_prompt"] = _TWINS_REVIEWER_PROMPT
            self.send_json(200, data)

        elif path == "/core/tasks":
            from scheduler import list_tasks
            try:
                self.send_json(200, list_tasks())
            except Exception as e:
                self.send_json(500, {"error": str(e)})

        # ── WhatsApp bridge status proxy ─────────────────────────────────────────
        elif path == "/v1/whatsapp/status":
            channel = _channel_router.get_channel("whatsapp")
            if channel:
                self.send_json(200, channel.get_bridge_status())
            else:
                self.send_json(200, {"connected": False, "status": "unavailable", "phone": None, "qr": None})

        # ── OpenAI-compatible model listing (required by Open WebUI, LibreChat, etc.) ──
        elif path == "/v1/models":
            self.send_json(200, {"object": "list", "data": _get_models()})

        elif path.startswith("/v1/models/"):
            mid = path[len("/v1/models/"):]
            by_id = {m["id"]: m for m in _get_models()}
            if mid in by_id:
                self.send_json(200, by_id[mid])
            else:
                self.send_json(404, {"error": {
                    "message": f"Model '{mid}' not found",
                    "type": "invalid_request_error", "code": "model_not_found",
                }})

        # ── Config ──────────────────────────────────────────────────────────────
        elif path == "/core/config":
            cfg, err = _get_config_raw()
            if err:
                self.send_json(500, {"error": err})
            else:
                self.send_json(200, _mask_config(cfg))

        # ── System stats — proxy to status-api, fallback to direct /proc read ──
        elif path == "/core/system":
            try:
                with urllib.request.urlopen(
                    "http://127.0.0.1:8089/api/metrics", timeout=3
                ) as r:
                    self.send_json(200, json.loads(r.read()))
            except Exception:
                self.send_json(200, _get_system_stats())

        # ── Services list ────────────────────────────────────────────────────────
        elif path == "/core/services":
            services = [
                {"name": n, "status": _service_status(n)} for n in MANAGED_SERVICES
            ]
            self.send_json(200, {"services": services})

        # ── Live log stream (SSE) for a managed service ──────────────────────────
        elif path.startswith("/core/services/") and path.endswith("/logs"):
            svc = path[len("/core/services/"):-len("/logs")]
            if svc not in MANAGED_SERVICES:
                self.send_json(403, {"error": "service not in allowed list"})
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            proc = subprocess.Popen(
                ["journalctl", "-u", svc, "-n", "100", "-f",
                 "--no-pager", "--output=short"],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            )
            try:
                for raw_line in iter(proc.stdout.readline, b""):
                    text = raw_line.decode("utf-8", errors="replace").rstrip()
                    if text:
                        self.wfile.write(
                            f"data: {json.dumps({'line': text})}\n\n".encode()
                        )
                        self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                try:
                    proc.kill()
                except Exception:
                    pass

        # ── Connectors: GET /core/connectors, /core/connectors/gdrive/status ──
        elif path == "/core/connectors":
            connectors_list = []
            try:
                from connectors import CONFIG_PATH as _cfg_path, get_active_connector
                if os.path.isfile(_cfg_path):
                    with open(_cfg_path) as f:
                        _cfg = json.load(f)
                    if "gdrive" in _cfg:
                        conn = get_active_connector() if _cfg.get("active") == "gdrive" else None
                        connectors_list.append({
                            "id": "gdrive",
                            "name": "Google Drive",
                            "active": _cfg.get("active") == "gdrive",
                            "connected": conn.is_connected() if conn else False,
                        })
            except Exception as e:
                log.warning("Connectors list error: %s", e)
            self.send_json(200, {"connectors": connectors_list})

        elif path == "/core/connectors/gdrive/status":
            try:
                from connectors import get_active_connector
                conn = get_active_connector()
                connected = conn.is_connected() if conn else False
            except Exception:
                connected = False
            self.send_json(200, {"connected": connected})

        else:
            self.send_json(404, {"error": "not found"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        try:
            data = json.loads(body) if body else {}
        except json.JSONDecodeError:
            self.send_json(400, {"error": "invalid JSON"})
            return

        path = urlparse(self.path).path.rstrip("/")

        # Search engine scan & repair
        if path == "/core/search-engines/scan":
            from orchestrator import (
                _load_search_engines, _save_search_engines,
                _fetch_search_html, _parse_results, _adapt_engine_patterns,
                _resolve_model_config
            )
            import socket as _socket

            # Resolve AI model — allow client override via _resolve_model_config
            model_override = data.get("model") or None
            ai_base, ai_key, ai_model = _resolve_model_config(model_override)
            log.info("[REPAIR] === Search engine repair START === model=%s", ai_model)
            if not ai_base:
                ai_model = model_override or "unavailable"

            _orig = _socket.getaddrinfo
            def _ipv4(host, port, family=0, *a, **kw):
                return _orig(host, port, _socket.AF_INET, *a, **kw)
            _socket.getaddrinfo = _ipv4
            engines = _load_search_engines()
            filter_engine = data.get("engine")  # single-engine scan if provided
            report = []
            try:
                for name, cfg in engines.items():
                    if filter_engine and name != filter_engine:
                        continue
                    logs = []
                    entry = {"engine": name, "status": "ok", "adapted": False,
                             "reliability": cfg.get("reliability", 0.5), "error": None, "logs": logs}
                    try:
                        logs.append(f"→ Fetching {cfg.get('url','?').replace('{query}','test+weather+today')[:60]}...")
                        html_body = _fetch_search_html(cfg, "test weather today")
                        logs.append(f"  HTML received: {len(html_body)} chars")
                        results = _parse_results(html_body, cfg.get("patterns", {}), 3)
                        if results:
                            cfg["reliability"] = min(1.0, cfg.get("reliability", 0.5) * 0.9 + 0.1)
                            entry["status"] = "ok"
                            entry["results_count"] = len(results)
                            logs.append(f"  ✓ {len(results)} results parsed — patterns OK")
                        else:
                            logs.append("  ✗ 0 results with current patterns")
                            if ai_base:
                                logs.append(f"  → Calling AI model [{ai_model}] for pattern adaptation...")
                                new_patterns = _adapt_engine_patterns(name, cfg, html_body,
                                    model_override=ai_model, base_override=ai_base, key_override=ai_key)
                                if new_patterns:
                                    logs.append(f"  AI returned new patterns: {list(new_patterns.keys())}")
                                    cfg["patterns"] = new_patterns
                                    engines[name] = cfg
                                    retry = _parse_results(html_body, new_patterns, 3)
                                    if retry:
                                        cfg["reliability"] = min(1.0, cfg.get("reliability", 0.5) * 0.9 + 0.1)
                                        entry["status"] = "ok"
                                        entry["adapted"] = True
                                        entry["results_count"] = len(retry)
                                        logs.append(f"  ✓ Retry OK — {len(retry)} results with new patterns")
                                    else:
                                        cfg["reliability"] = max(0.1, cfg.get("reliability", 0.5) * 0.8)
                                        entry["status"] = "fail"
                                        entry["error"] = "0 results even after AI adaptation"
                                        logs.append("  ✗ New patterns still return 0 results")
                                else:
                                    cfg["reliability"] = max(0.1, cfg.get("reliability", 0.5) * 0.8)
                                    entry["status"] = "fail"
                                    entry["error"] = "AI adaptation returned nothing"
                                    logs.append("  ✗ AI returned no patterns (model may be unreachable)")
                            else:
                                cfg["reliability"] = max(0.1, cfg.get("reliability", 0.5) * 0.8)
                                entry["status"] = "fail"
                                entry["error"] = "No AI model configured — cannot adapt"
                                logs.append("  ✗ No AI model available (check PicoClaw config)")
                    except Exception as e:
                        cfg["reliability"] = max(0.1, cfg.get("reliability", 0.5) * 0.8)
                        entry["status"] = "error"
                        entry["error"] = str(e)
                        logs.append(f"  ✗ Exception: {e}")
                    entry["reliability"] = round(cfg.get("reliability", 0.5), 2)
                    engines[name] = cfg
                    report.append(entry)
                _save_search_engines(engines)
                # Persist scan results — merge with previous if single-engine scan
                import datetime as _dt
                _scan_path = "/home/pi/.clawbot/scan-results.json"
                if filter_engine:
                    # Merge: keep previous results, update only the scanned engine
                    try:
                        with open(_scan_path) as _sf:
                            _prev = json.load(_sf)
                    except Exception:
                        _prev = {"model": ai_model, "scanned_at": None, "results": []}
                    _prev_results = {e["engine"]: e for e in _prev.get("results", [])}
                    for e in report:
                        _prev_results[e["engine"]] = e
                    _scan_payload = {
                        "model": ai_model,
                        "scanned_at": _prev.get("scanned_at"),
                        "last_repair": _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        "results": list(_prev_results.values()),
                    }
                else:
                    _scan_payload = {
                        "model": ai_model,
                        "scanned_at": _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        "results": report,
                    }
                try:
                    os.makedirs("/home/pi/.clawbot", exist_ok=True)
                    with open(_scan_path, "w") as _sf:
                        json.dump(_scan_payload, _sf)
                except Exception:
                    pass
            finally:
                _socket.getaddrinfo = _orig
                repaired = sum(1 for e in report if e.get("adapted"))
                log.info("[REPAIR] === Search engine repair END === repaired=%d/%d | model=%s",
                         repaired, len(report), ai_model)
            self.send_json(200, _scan_payload)
            return

        # Cancel a running session task
        if path == "/v1/cancel":
            from orchestrator import cancel_session
            sid = data.get("session_id", "")
            if sid:
                cancel_session(sid)
                log.info("Cancel requested for session %s", sid)
                self.send_json(200, {"ok": True, "cancelled": sid})
            else:
                self.send_json(400, {"error": "session_id required"})
            return

        # WhatsApp bridge management
        if path == "/wa/connect":
            allow_from = data.get("allow_from", "*")
            _wa_start_bridge(allow_from)
            self.send_json(200, {"ok": True})
            return

        if path == "/wa/disconnect":
            _wa_stop_bridge()
            self.send_json(200, {"ok": True})
            return

        # WhatsApp allow_from config update
        if path == "/v1/whatsapp/config":
            allow = data.get("allow_from", "*").strip()
            try:
                cfg_path = "/etc/clawbot/clawbot.cfg"
                cfg = configparser.ConfigParser()
                cfg.read(cfg_path)
                if not cfg.has_section("whatsapp"):
                    cfg.add_section("whatsapp")
                cfg.set("whatsapp", "allow_from", allow)
                with open(cfg_path, "w") as f:
                    cfg.write(f)
                self.send_json(200, {"ok": True, "message": "allow_from updated"})
            except Exception as e:
                log.error("WhatsApp config error: %s", e)
                self.send_json(500, {"error": str(e)})
            return

        # WhatsApp inbound — called by the Baileys bridge
        if path == "/v1/channels/whatsapp/inbound":
            channel = _channel_router.get_channel("whatsapp")
            if channel:
                try:
                    # on_inbound normalizes, filters allow_from, fires LLM reply in background
                    channel.on_inbound(data)
                except Exception as e:
                    log.error("WhatsApp inbound error: %s", e)
            self.send_json(200, {"ok": True})
            return

        # WhatsApp logout — clear bridge session and restart bridge
        if path == "/v1/whatsapp/logout":
            try:
                import shutil
                auth_dir = "/home/pi/.clawbot/wa-auth"
                if os.path.isdir(auth_dir):
                    shutil.rmtree(auth_dir)
                    os.makedirs(auth_dir, exist_ok=True)
                # Restart bridge so it generates a fresh QR
                _wa_stop_bridge()
                _wa_start_bridge()
                self.send_json(200, {"ok": True, "message": "Session cleared — rescan QR to reconnect"})
            except Exception as e:
                log.error("WhatsApp logout error: %s", e)
                self.send_json(500, {"error": str(e)})
            return

        # Session create/update
        if path.startswith("/core/sessions/trash/"):
            sid = path[len("/core/sessions/trash/"):]
            if ".." in sid or "/" in sid:
                self.send_json(400, {"error": "invalid id"})
                return
            src = os.path.join(SESSIONS_TRASH_DIR, sid + ".json")
            if os.path.isfile(src):
                os.makedirs(SESSIONS_DIR, exist_ok=True)
                os.rename(src, os.path.join(SESSIONS_DIR, sid + ".json"))
            self.send_json(200, {"ok": True})
        elif path.startswith("/core/sessions/"):
            sid = path[len("/core/sessions/"):]
            if ".." in sid or "/" in sid:
                self.send_json(400, {"error": "invalid id"})
                return
            os.makedirs(SESSIONS_DIR, exist_ok=True)
            fpath = os.path.join(SESSIONS_DIR, sid + ".json")
            now = int(time.time() * 1000)
            existing = {}
            if os.path.isfile(fpath):
                try:
                    with open(fpath) as f:
                        existing = json.load(f)
                except Exception:
                    pass
            existing.update({
                "id": sid,
                "name": data.get("name", existing.get("name", "New chat")),
                "mode": data.get("mode", existing.get("mode", "core")),
                "channel": data.get("channel", existing.get("channel", "web")),
                "messages": data.get("messages", existing.get("messages", [])),
                "createdAt": existing.get("createdAt", now),
                "updatedAt": now,
            })
            with open(fpath, "w") as f:
                json.dump(existing, f)
            self.send_json(200, {"ok": True})
            return

        # Agent create/update
        if path.startswith("/core/agents/"):
            from orchestrator import save_agent
            agent_id = path[len("/core/agents/"):]
            if ".." in agent_id or "/" in agent_id:
                self.send_json(400, {"error": "invalid id"})
                return
            agent = dict(data)
            agent["id"] = agent_id
            try:
                save_agent(agent)
                self.send_json(200, {"ok": True})
            except Exception as e:
                self.send_json(500, {"error": str(e)})
            return

        # Skill create/update/enable/disable
        if path == "/core/skills" or path.startswith("/core/skills/"):
            from skills import load_skills, save_skill
            remainder = path[len("/core/skills"):].lstrip("/")
            # enable/disable: POST /core/skills/{id}/enable or /disable
            if "/" in remainder:
                parts = remainder.split("/", 1)
                skill_id, action = parts[0], parts[1]
                if ".." in skill_id or ".." in action:
                    self.send_json(400, {"error": "invalid id"})
                    return
                if action in ("enable", "disable"):
                    skills = load_skills(include_disabled=True)
                    if skill_id not in skills:
                        self.send_json(404, {"error": f"Skill '{skill_id}' not found"})
                        return
                    skill = dict(skills[skill_id])
                    skill["enabled"] = (action == "enable")
                    try:
                        save_skill(skill)
                        self.send_json(200, {"ok": True})
                    except Exception as e:
                        self.send_json(500, {"error": str(e)})
                else:
                    self.send_json(400, {"error": f"Unknown action '{action}'"})
                return
            # create/update: POST /core/skills or /core/skills/{id}
            skill_id = remainder or data.get("id")
            if not skill_id:
                self.send_json(400, {"error": "skill id required"})
                return
            if ".." in skill_id or "/" in skill_id:
                self.send_json(400, {"error": "invalid id"})
                return
            skills = load_skills(include_disabled=True)
            existing = skills.get(skill_id, {})
            skill = {**existing, **data, "id": skill_id}
            try:
                save_skill(skill)
                self.send_json(200, {"ok": True})
            except Exception as e:
                self.send_json(500, {"error": str(e)})
            return

        # Task create
        if path == "/core/tasks":
            from scheduler import create_task
            try:
                name = data.get("name", "Unnamed task")
                instruction = data.get("instruction", "")
                schedule_type = data.get("schedule_type", "once")
                kwargs = {k: v for k, v in data.items() if k not in ("name", "instruction", "schedule_type")}
                task = create_task(name, instruction, schedule_type, **kwargs)
                self.send_json(200, task)
            except Exception as e:
                self.send_json(500, {"error": str(e)})
            return

        # Task pause/resume
        if path.startswith("/core/tasks/"):
            from scheduler import pause_task, resume_task
            remainder = path[len("/core/tasks/"):]
            if "/" in remainder:
                task_id, action = remainder.split("/", 1)
                if ".." in task_id or ".." in action:
                    self.send_json(400, {"error": "invalid id"})
                    return
                if action == "pause":
                    ok = pause_task(task_id)
                elif action == "resume":
                    ok = resume_task(task_id)
                else:
                    self.send_json(400, {"error": f"Unknown action '{action}'"})
                    return
                self.send_json(200, {"ok": ok})
            else:
                self.send_json(400, {"error": "Use /core/tasks/{id}/pause or /resume"})
            return

        # Core prompts save
        if path == "/core/prompts":
            from orchestrator import CORE_PROMPTS_PATH
            try:
                os.makedirs(os.path.dirname(CORE_PROMPTS_PATH), exist_ok=True)
                with open(CORE_PROMPTS_PATH, "w") as f:
                    json.dump({
                        "system_prompt": data.get("system_prompt", ""),
                        "extra_rules": data.get("extra_rules", ""),
                        "twins_reviewer_prompt": data.get("twins_reviewer_prompt", ""),
                        "search_engine": data.get("search_engine", "auto"),
                        "search_mode": data.get("search_mode", "auto"),
                        "brave_api_key": data.get("brave_api_key", ""),
                        "chat_model": data.get("chat_model", ""),
                    }, f, indent=2)
                self.send_json(200, {"ok": True})
            except Exception as e:
                self.send_json(500, {"error": str(e)})
            return

        # Sub-agent chat with routing
        if path == "/v1/chat/agents":
            agent_ids = data.get("agent_ids", [])
            auto_route = data.get("auto_route", not agent_ids)
            session_id = data.get("session_id")

            if auto_route and not agent_ids:
                from orchestrator import route_to_agents
                # Start SSE early to send routing thinking events
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("X-Accel-Buffering", "no")
                self.end_headers()
                messages = data.get("messages", [])
                user_msg = next(
                    (m.get("content", "") for m in reversed(messages) if m.get("role") == "user"), ""
                )
                # Only show routing indicator for non-trivial messages
                if len(user_msg.strip().split()) > 3:
                    thinking = json.dumps({"type": "thinking", "message": "Sélection de l'agent..."})
                    self.wfile.write(f"event: thinking\ndata: {thinking}\n\n".encode())
                    self.wfile.flush()
                matched = route_to_agents(user_msg, last_agent_id=data.get("last_agent_id"))
                if matched:
                    agent_ids = [a["id"] for a in matched[:1]]
                    # Tell frontend which agent was selected
                    selected = json.dumps({"type": "thinking", "message": f"Selected: {matched[0].get('name', agent_ids[0])}"})
                    self.wfile.write(f"event: thinking\ndata: {selected}\n\n".encode())
                    self.wfile.flush()
                else:
                    # Fallback: regular Core mode (headers already sent above)
                    from orchestrator import chat_with_tools_stream
                    gen = chat_with_tools_stream(data)
                    fallback_content = None
                    try:
                        fallback_info = json.dumps({"type": "agent_start", "agents": [
                            {"id": "core", "name": "Core", "avatar": "⚡", "color": "#00ffe0"}
                        ]})
                        self.wfile.write(f"event: agent_start\ndata: {fallback_info}\n\n".encode())
                        self.wfile.flush()
                        for event_dict in gen:
                            event_dict["agent_id"] = "core"
                            event_type = event_dict.get("type", "data")
                            if event_type == "done":
                                fallback_content = event_dict.get("content", "")
                            chunk = f"event: {event_type}\ndata: {json.dumps(event_dict)}\n\n".encode()
                            self.wfile.write(chunk)
                            self.wfile.flush()
                    except BrokenPipeError:
                        log.info("Client disconnected (agent fallback), continuing in background (session=%s)", session_id)
                        def _drain_fb():
                            content = fallback_content or ""
                            try:
                                for ev in gen:
                                    if ev.get("type") == "done":
                                        content = ev.get("content", "")
                            except Exception as ex:
                                log.warning("Background drain error: %s", ex)
                            _save_assistant_to_session(session_id, content)
                        threading.Thread(target=_drain_fb, daemon=True).start()
                        return
                    except Exception as e:
                        log.error("agent fallback error: %s", e)
                    try:
                        self.wfile.write(b"data: [DONE]\n\n")
                        self.wfile.flush()
                    except Exception:
                        pass
                    return

            from orchestrator import chat_with_multi_agents_stream
            try:
                # Headers already sent if auto_route; send them if not
                if not auto_route:
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Cache-Control", "no-cache")
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.send_header("X-Accel-Buffering", "no")
                    self.end_headers()
                gen_ma = chat_with_multi_agents_stream(data, agent_ids)
                ma_content_parts = []
                try:
                    for event_dict in gen_ma:
                        event_type = event_dict.get("type", "data")
                        if event_type == "done":
                            ma_content_parts.append(event_dict.get("content", ""))
                        chunk = f"event: {event_type}\ndata: {json.dumps(event_dict)}\n\n".encode()
                        self.wfile.write(chunk)
                        self.wfile.flush()
                except BrokenPipeError:
                    log.info("Client disconnected (multi-agent), continuing in background (session=%s)", session_id)
                    def _drain_ma():
                        try:
                            for ev in gen_ma:
                                if ev.get("type") == "done":
                                    ma_content_parts.append(ev.get("content", ""))
                        except Exception as ex:
                            log.warning("Background drain error: %s", ex)
                        _save_assistant_to_session(session_id, "\n\n".join(ma_content_parts))
                    threading.Thread(target=_drain_ma, daemon=True).start()
                    return
                except Exception as e:
                    log.error("multi-agent stream error: %s", e)
                    try:
                        err = json.dumps({"type": "error", "message": str(e)})
                        self.wfile.write(f"event: error\ndata: {err}\n\n".encode())
                        self.wfile.flush()
                    except Exception:
                        pass
                try:
                    self.wfile.write(b"data: [DONE]\n\n")
                    self.wfile.flush()
                except Exception:
                    pass
            except Exception as e:
                log.error("agent chat setup error: %s", e)
            return

        # Tool-aware chat proxy — delegated to WebChannel
        if path == "/v1/chat/completions":
            stream = data.get("stream", False)
            session_id = data.get("session_id")
            # Clear any stale cancel flag — user is submitting a new message
            if session_id:
                from orchestrator import clear_cancelled
                clear_cancelled(session_id)
            # Resolve channel (default: web for backward compat)
            channel_id = data.get("channel", "web")
            channel = _channel_router.get_channel(channel_id)
            if not channel:
                channel = _channel_router.get_channel("web")
            if stream and hasattr(channel, "handle_stream_request"):
                try:
                    channel.handle_stream_request(
                        self, data, session_id,
                        save_fn=_save_assistant_to_session,
                    )
                except Exception as e:
                    log.error("chat_with_tools_stream setup error: %s", e)
            elif hasattr(channel, "handle_sync_request"):
                try:
                    channel.handle_sync_request(self, data)
                except Exception as e:
                    log.error("chat_with_tools error: %s", e)
                    self.send_json(500, {"error": str(e)})
            else:
                # Fallback: non-streaming via orchestrator directly
                try:
                    from orchestrator import chat_with_tools
                    result = chat_with_tools(data)
                    self.send_json(200, result)
                except Exception as e:
                    log.error("chat_with_tools error: %s", e)
                    self.send_json(500, {"error": str(e)})
            return

        # Picoclaw native agent (14 built-in tools)
        if path == "/v1/picoclaw-agent":
            session_id_pc = data.get("session_id")
            messages = data.get("messages", [])
            msg = next(
                (m.get("content", "") for m in reversed(messages) if m.get("role") == "user"),
                ""
            )
            if not msg:
                self.send_json(400, {"error": "no user message"})
                return
            try:
                verbose = data.get("verbose", False)
                # Auto-compact session if context is too large (like Claude Code /compact)
                _maybe_compact_agent_session()
                env = os.environ.copy()
                env["HOME"] = "/home/pi"
                # Use Popen + process group so we can kill all child processes on timeout
                proc = subprocess.Popen(
                    ["/usr/local/bin/picoclaw", "agent", "--message", msg],  # legacy agent binary
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    preexec_fn=os.setsid,
                    env=env,
                )
                try:
                    stdout_b, stderr_b = proc.communicate(timeout=240)
                    stdout_text = stdout_b.decode("utf-8", errors="replace")
                    stderr_text = stderr_b.decode("utf-8", errors="replace")
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    proc.wait()
                    self.send_json(504, {"error": "agent timed out"})
                    return
                # Always parse: extract clean final response + full trace
                raw = (stdout_text + stderr_text).encode("utf-8", errors="replace")
                meta = re.search(rb"\{[^}]*final_length=(\d+)[^}]*\}", raw)
                # Agent binary emits 🦞 (shrimp U+1F99E) before the final response
                shrimp = "🦞".encode()
                crab = raw.find(shrimp)

                if meta and crab >= 0:
                    final_len = int(meta.group(1))
                    start = crab + len(shrimp)
                    while start < len(raw) and raw[start:start+1] in (b" ", b"\n", b"\r"):
                        start += 1
                    clean = raw[start:start + final_len].decode("utf-8", errors="replace").strip()
                    # Trace = tool execution output AFTER the final response, before stats
                    meta_start = raw.find(meta.group(0))
                    trace_raw = raw[start + final_len:meta_start].decode("utf-8", errors="replace")
                else:
                    # Fallback: strip log lines, shrimp prefix, and metadata line
                    log_pat = re.compile(r"^\d{4}/\d{2}/\d{2} ")
                    meta_pat = re.compile(r"\{[^}]*final_length=\d+[^}]*\}")
                    lines = (stdout_text + stderr_text).splitlines()
                    resp_lines = [
                        l for l in lines
                        if not log_pat.match(l) and not meta_pat.search(l) and l.strip()
                    ]
                    clean = re.sub(r"^🦞\s*", "", "\n".join(resp_lines).strip())
                    trace_raw = "\n".join(resp_lines)

                # Parse trace into structured steps for the UI
                steps = _parse_agent_steps(trace_raw, clean) if verbose else []

                if not clean:
                    clean = "(no response)"

                # Build SSE: optional trace event + final response
                parts = []
                if verbose and steps:
                    steps_data = json.dumps({"steps": steps})
                    parts.append(f"event: trace\ndata: {steps_data}\n\n")
                final_data = json.dumps({"choices": [{"delta": {"content": clean}, "index": 0}]})
                parts.append(f"data: {final_data}\n\n")
                parts.append("data: [DONE]\n\n")
                sse = "".join(parts).encode()
                try:
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Cache-Control", "no-cache")
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.send_header("Content-Length", str(len(sse)))
                    self.end_headers()
                    self.wfile.write(sse)
                except BrokenPipeError:
                    log.info("Client disconnected, saving to session %s", session_id_pc)
                    _save_assistant_to_session(session_id_pc, clean)
            except Exception as e:
                log.error("agent error: %s", e)
                self.send_json(500, {"error": str(e)})
            return

        # ── Service control: POST /core/services/{name}/start|stop|restart ─────────
        if path.startswith("/core/services/"):
            parts_svc = path.split("/")
            if len(parts_svc) == 5 and parts_svc[4] in ("start", "stop", "restart"):
                ok, msg = _service_control(parts_svc[3], parts_svc[4])
                self.send_json(200 if ok else 400, {"ok": ok, "message": msg})
            else:
                self.send_json(404, {"error": "not found"})
            return

        # ── Config update: POST /core/config ─────────────────────────────────────
        if path == "/core/config":
            cfg, err = _get_config_raw()
            if err:
                self.send_json(500, {"error": err})
                return
            if "model_list" in data:
                cfg["model_list"] = data["model_list"]
            else:
                if not cfg.get("model_list"):
                    cfg["model_list"] = [{}]
                for k in ("api_base", "api_key", "model"):
                    if k in data:
                        cfg["model_list"][0][k] = data[k]
            if "default_model" in data:
                cfg.setdefault("agents", {}).setdefault("defaults", {})["model"] = data["default_model"]
            try:
                import urllib.request as _ur
                _body = json.dumps({
                    "provider": cfg.get("provider", "clawbot"),
                    "model": (cfg.get("model_list") or [{}])[0].get("model", ""),
                    "api_key": (cfg.get("model_list") or [{}])[0].get("api_key", ""),
                    "base_url": (cfg.get("model_list") or [{}])[0].get("api_base",
                        (cfg.get("model_list") or [{}])[0].get("base_url", "")),
                }).encode()
                _req = _ur.Request("http://127.0.0.1:8089/config",
                    data=_body, headers={"Content-Type": "application/json"}, method="POST")
                with _ur.urlopen(_req, timeout=5):
                    pass
                self.send_json(200, {"ok": True, "message": "config updated"})
            except Exception as e:
                self.send_json(500, {"error": str(e)})
            return

        # ── Workspace file upload: POST /core/workspace/upload ───────────────────
        # Body: {"filename": "foo.py", "content": "...", "encoding": "utf8"|"base64"}
        if path == "/core/workspace/upload":
            filename = data.get("filename", "")
            content = data.get("content", "")
            encoding = data.get("encoding", "utf8")
            if not filename or ".." in filename or "/" in filename:
                self.send_json(400, {"error": "invalid filename"})
                return
            workspace = AGENT_WORKSPACE
            os.makedirs(workspace, exist_ok=True)
            fpath = os.path.join(workspace, filename)
            try:
                if encoding == "base64":
                    data_bytes = base64.b64decode(content)
                else:
                    data_bytes = content.encode("utf-8")
                with open(fpath, "wb") as f:
                    f.write(data_bytes)
                self.send_json(200, {"ok": True, "filename": filename, "size": len(data_bytes)})
            except Exception as e:
                self.send_json(500, {"error": str(e)})
            return

        # /core/updates/{name}/update
        if path.startswith("/core/updates/") and path.endswith("/update"):
            name = path[len("/core/updates/"):-len("/update")]
            ok, msg = do_update(name)
            self.send_json(200 if ok else 500, {"ok": ok, "message": msg})
            return

        # /core/modules/{id}/{action}
        parts = path.split("/")
        # Expected: ['', 'core', 'modules', '{id}', '{action}']
        if len(parts) == 5 and parts[1] == "core" and parts[2] == "modules":
            module_id = parts[3]
            action = parts[4]

            if action == "install":
                repo = data.get("repo", "")
                if not repo:
                    self.send_json(400, {"error": "missing 'repo' field"})
                    return
                log.info("Installing module '%s' from %s", module_id, repo)
                ok, msg = install(module_id, repo)
                self.send_json(200 if ok else 500, {"ok": ok, "message": msg})

            elif action == "uninstall":
                log.info("Uninstalling module '%s'", module_id)
                ok, msg = uninstall(module_id)
                self.send_json(200 if ok else 400, {"ok": ok, "message": msg})

            elif action == "enable":
                log.info("Enabling module '%s'", module_id)
                ok, msg = enable(module_id)
                self.send_json(200 if ok else 400, {"ok": ok, "message": msg})

            elif action == "disable":
                log.info("Disabling module '%s'", module_id)
                ok, msg = disable(module_id)
                self.send_json(200 if ok else 400, {"ok": ok, "message": msg})

            else:
                self.send_json(404, {"error": f"Unknown action '{action}'"})
        # ── Connector auth: POST /core/connectors/gdrive/auth ───────────────
        elif path == "/core/connectors/gdrive/auth":
            code = data.get("code")
            redirect_uri = data.get("redirect_uri", "urn:ietf:wg:oauth:2.0:oob")
            if not code:
                self.send_json(400, {"error": "missing 'code' field"})
                return
            try:
                from connectors import CONNECTORS_DIR as _cd, CONFIG_PATH as _cp, reset_connector
                from connectors.gdrive import GoogleDriveConnector
                os.makedirs(_cd, exist_ok=True)
                GoogleDriveConnector.exchange_code(code, redirect_uri)
                # Activate gdrive in config
                cfg = {}
                if os.path.isfile(_cp):
                    with open(_cp) as f:
                        cfg = json.load(f)
                cfg["active"] = "gdrive"
                with open(_cp, "w") as f:
                    json.dump(cfg, f, indent=2)
                reset_connector()
                self.send_json(200, {"ok": True, "message": "Google Drive connected"})
            except Exception as e:
                log.error("GDrive auth failed: %s", e)
                self.send_json(500, {"error": str(e)})

        else:
            self.send_json(404, {"error": "not found"})


    def do_DELETE(self):
        path = urlparse(self.path).path.rstrip("/")
        if path == "/core/sessions/trash":
            try:
                if os.path.isdir(SESSIONS_TRASH_DIR):
                    for fn in os.listdir(SESSIONS_TRASH_DIR):
                        if fn.endswith(".json"):
                            os.remove(os.path.join(SESSIONS_TRASH_DIR, fn))
            except Exception:
                pass
            self.send_json(200, {"ok": True})
        elif path.startswith("/core/sessions/trash/"):
            sid = path[len("/core/sessions/trash/"):]
            if ".." in sid or "/" in sid:
                self.send_json(400, {"error": "invalid id"})
                return
            fpath = os.path.join(SESSIONS_TRASH_DIR, sid + ".json")
            if os.path.isfile(fpath):
                os.remove(fpath)
            self.send_json(200, {"ok": True})
        elif path.startswith("/core/sessions/"):
            sid = path[len("/core/sessions/"):]
            if ".." in sid or "/" in sid:
                self.send_json(400, {"error": "invalid id"})
                return
            fpath = os.path.join(SESSIONS_DIR, sid + ".json")
            if os.path.isfile(fpath):
                os.makedirs(SESSIONS_TRASH_DIR, exist_ok=True)
                os.rename(fpath, os.path.join(SESSIONS_TRASH_DIR, sid + ".json"))
            self.send_json(200, {"ok": True})
        elif path.startswith("/core/agents/"):
            from orchestrator import delete_agent
            agent_id = path[len("/core/agents/"):]
            if ".." in agent_id or "/" in agent_id:
                self.send_json(400, {"error": "invalid id"})
                return
            delete_agent(agent_id)
            self.send_json(200, {"ok": True})
        elif path.startswith("/core/skills/"):
            from skills import delete_skill
            skill_id = path[len("/core/skills/"):]
            if ".." in skill_id or "/" in skill_id:
                self.send_json(400, {"error": "invalid id"})
                return
            deleted = delete_skill(skill_id)
            if deleted:
                self.send_json(200, {"ok": True})
            else:
                self.send_json(404, {"error": f"Skill '{skill_id}' not found or is builtin"})
        elif path.startswith("/core/tasks/"):
            from scheduler import delete_task
            task_id = path[len("/core/tasks/"):]
            if ".." in task_id or "/" in task_id:
                self.send_json(400, {"error": "invalid id"})
                return
            ok = delete_task(task_id)
            self.send_json(200, {"ok": ok})
        elif path.startswith("/core/workspace/"):
            filename = unquote(path[len("/core/workspace/"):])
            if ".." in filename or "/" in filename:
                self.send_json(400, {"error": "invalid filename"})
                return
            fpath = os.path.join(AGENT_WORKSPACE, filename)
            if os.path.isfile(fpath):
                os.remove(fpath)
            self.send_json(200, {"ok": True})
        # ── Connector disconnect: DELETE /core/connectors/gdrive/auth ─────────
        elif path == "/core/connectors/gdrive/auth":
            try:
                from connectors import CONFIG_PATH as _cp, reset_connector
                from connectors.gdrive import TOKEN_PATH
                if os.path.isfile(TOKEN_PATH):
                    os.remove(TOKEN_PATH)
                if os.path.isfile(_cp):
                    with open(_cp) as f:
                        cfg = json.load(f)
                    cfg["active"] = None
                    with open(_cp, "w") as f:
                        json.dump(cfg, f, indent=2)
                reset_connector()
                self.send_json(200, {"ok": True, "message": "Google Drive disconnected"})
            except Exception as e:
                log.error("GDrive disconnect failed: %s", e)
                self.send_json(500, {"error": str(e)})
        else:
            self.send_json(404, {"error": "not found"})


class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True  # kill threads when main process exits

    def get_request(self):
        """Override to set TCP_NODELAY on each accepted connection socket."""
        conn, addr = super().get_request()
        import socket as _sock
        try:
            conn.setsockopt(_sock.IPPROTO_TCP, _sock.TCP_NODELAY, 1)
        except Exception:
            pass
        return conn, addr


if __name__ == "__main__":
    from scheduler import start_scheduler
    start_scheduler()
    _channel_router.start_all()
    log.info("Channels started: %s", _channel_router.list_channels())
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    log.info("ClawbotCore listening on http://127.0.0.1:%d", PORT)
    server.serve_forever()
