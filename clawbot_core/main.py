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

  POST /v1/chat/completions        — tool-aware chat proxy (→ PicoClaw + module tools)
"""

import base64
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

# Picoclaw agent session compaction
PICOCLAW_SESSION_FILE = "/home/pi/.picoclaw/workspace/sessions/agent_main_main.json"
PICOCLAW_CONFIG_FILE = "/home/pi/.picoclaw/config.json"
AGENT_TOKEN_THRESHOLD = 10000  # estimated tokens before compaction
AGENT_KEEP_RECENT = 8          # keep last N messages verbatim

from registry import get_all_modules, load_local_modules
from installer import install, uninstall, enable, disable
from update_manager import list_updates, do_update

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
    """
    If the picoclaw agent session is too large, summarize it via the cloud LLM.
    Mirrors Claude Code's automatic context compaction.
    Reads/writes PICOCLAW_SESSION_FILE directly before each picoclaw subprocess call.
    """
    try:
        if not os.path.exists(PICOCLAW_SESSION_FILE):
            return

        with open(PICOCLAW_SESSION_FILE) as f:
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

        # Load LLM config from picoclaw config
        try:
            with open(PICOCLAW_CONFIG_FILE) as f:
                cfg = json.load(f)
            entry = cfg.get("model_list", [{}])[0]
            base = entry.get("api_base", entry.get("base_url", "")).rstrip("/")
            api_key = entry.get("api_key", "")
            model = entry.get("model", "")
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

        with open(PICOCLAW_SESSION_FILE, "w") as f:
            json.dump(session, f)

        log.info("Agent session compacted: %d → %d messages (~%d tokens freed)",
                 len(msgs), len(compacted_msgs), total_chars // 4)

    except Exception as e:
        log.warning("Agent session compaction failed (non-fatal): %s", e)


def _parse_agent_steps(trace_raw: str, clean_final: str) -> list:
    """Parse picoclaw raw output into structured step list for UI rendering.
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
    "clawbot-core", "picoclaw", "nginx",
    "clawbot-cloud", "clawbot-status-api", "clawbot-telegram",
    "clawbot-kiosk", "wifi-watchdog",
]


def _get_models():
    """Return available models in OpenAI /v1/models list format."""
    models = []
    seen = set()
    try:
        with open(PICOCLAW_CONFIG_FILE) as f:
            cfg = json.load(f)
        for entry in cfg.get("model_list", []):
            mid = entry.get("model", "")
            if mid and mid not in seen:
                seen.add(mid)
                models.append({
                    "id": mid, "object": "model",
                    "created": 1704067200, "owned_by": "clawbot",
                })
    except Exception:
        pass
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
    """Return (cfg_dict, error_str). Reads picoclaw config file."""
    try:
        with open(PICOCLAW_CONFIG_FILE) as f:
            return json.load(f), None
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
            workspace = "/home/pi/.picoclaw/workspace"
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
            workspace = "/home/pi/.picoclaw/workspace"
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

        # ── Core prompts (system prompt + extra rules) ───────────────────────
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

        # Session create/update
        if path.startswith("/core/sessions/"):
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
                matched = route_to_agents(user_msg)
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

        # Tool-aware chat proxy
        if path == "/v1/chat/completions":
            stream = data.get("stream", False)
            session_id = data.get("session_id")
            if stream:
                try:
                    from orchestrator import chat_with_tools_stream
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Cache-Control", "no-cache")
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.send_header("X-Accel-Buffering", "no")  # disable nginx buffering
                    self.end_headers()
                    gen = chat_with_tools_stream(data)
                    final_content = None
                    try:
                        for event_dict in gen:
                            event_type = event_dict.get("type", "data")
                            if event_type == "done":
                                final_content = event_dict.get("content", "")
                            # Native ClawBot event (named SSE event for dashboard)
                            chunk = f"event: {event_type}\ndata: {json.dumps(event_dict)}\n\n".encode()
                            self.wfile.write(chunk)
                            self.wfile.flush()
                            # On "done": also emit a standard OpenAI delta chunk
                            # so Open WebUI / LibreChat / AnythingLLM receive the content
                            if event_type == "done":
                                openai_delta = json.dumps({
                                    "id": "chatcmpl-clawbot",
                                    "object": "chat.completion.chunk",
                                    "created": int(time.time()),
                                    "model": data.get("model", "clawbot"),
                                    "choices": [{
                                        "index": 0,
                                        "delta": {"content": final_content or ""},
                                        "finish_reason": "stop",
                                    }],
                                })
                                self.wfile.write(f"data: {openai_delta}\n\n".encode())
                                self.wfile.flush()
                    except BrokenPipeError:
                        # Client disconnected — continue processing in background
                        log.info("Client disconnected, continuing in background (session=%s)", session_id)
                        def _drain():
                            content = final_content or ""
                            try:
                                for ev in gen:
                                    if ev.get("type") == "done":
                                        content = ev.get("content", "")
                            except Exception as ex:
                                log.warning("Background drain error: %s", ex)
                            _save_assistant_to_session(session_id, content)
                        threading.Thread(target=_drain, daemon=True).start()
                        return
                    except Exception as e:
                        log.error("chat stream error: %s", e)
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
                    log.error("chat_with_tools_stream setup error: %s", e)
            else:
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
                    ["/usr/local/bin/picoclaw", "agent", "--message", msg],
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
                    self.send_json(504, {"error": "picoclaw agent timed out"})
                    return
                # Always parse: extract clean final response + full trace
                raw = (stdout_text + stderr_text).encode("utf-8", errors="replace")
                meta = re.search(rb"\{[^}]*final_length=(\d+)[^}]*\}", raw)
                # picoclaw v0.2.0 emits 🦞 (shrimp U+1F99E) before the final response
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
                    log.info("Client disconnected (picoclaw-agent), saving to session %s", session_id_pc)
                    _save_assistant_to_session(session_id_pc, clean)
            except Exception as e:
                log.error("picoclaw-agent error: %s", e)
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
                with open(PICOCLAW_CONFIG_FILE, "w") as f:
                    json.dump(cfg, f, indent=2)
                subprocess.run(
                    ["sudo", "systemctl", "restart", "picoclaw"],
                    capture_output=True, timeout=5,
                )
                self.send_json(200, {"ok": True, "message": "config updated, picoclaw restarting"})
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
            workspace = "/home/pi/.picoclaw/workspace"
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
        else:
            self.send_json(404, {"error": "not found"})


    def do_DELETE(self):
        path = urlparse(self.path).path.rstrip("/")
        if path.startswith("/core/sessions/"):
            sid = path[len("/core/sessions/"):]
            if ".." in sid or "/" in sid:
                self.send_json(400, {"error": "invalid id"})
                return
            fpath = os.path.join(SESSIONS_DIR, sid + ".json")
            if os.path.isfile(fpath):
                os.remove(fpath)
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
            fpath = os.path.join("/home/pi/.picoclaw/workspace", filename)
            if os.path.isfile(fpath):
                os.remove(fpath)
            self.send_json(200, {"ok": True})
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
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    log.info("ClawbotCore listening on http://127.0.0.1:%d", PORT)
    server.serve_forever()
