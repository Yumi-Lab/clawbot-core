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

  POST /v1/chat/completions        — tool-aware chat proxy (→ PicoClaw + module tools)
"""

import json
import logging
import os
import re
import subprocess
import sys
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse

SESSIONS_DIR = "/home/pi/.clawbot/sessions"

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
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

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
            try:
                entries = []
                for f in sorted(os.listdir(workspace)):
                    fp = os.path.join(workspace, f)
                    if os.path.isfile(fp):
                        entries.append({"name": f, "size": os.path.getsize(fp)})
                self.send_json(200, {"files": entries})
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

        # Tool-aware chat proxy
        if path == "/v1/chat/completions":
            try:
                from orchestrator import chat_with_tools
                stream = data.get("stream", False)
                result = chat_with_tools(data)
                if stream:
                    # Wrap non-streaming response as SSE for clients expecting stream:true
                    content = result.get("choices", [{}])[0].get("message", {}).get("content", "")
                    sse = (
                        f"data: {{\"choices\":[{{\"delta\":{{\"content\":{json.dumps(content)}}},\"index\":0}}]}}\n\n"
                        f"data: [DONE]\n\n"
                    ).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Cache-Control", "no-cache")
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.send_header("Content-Length", str(len(sse)))
                    self.end_headers()
                    self.wfile.write(sse)
                else:
                    self.send_json(200, result)
            except Exception as e:
                log.error("chat_with_tools error: %s", e)
                self.send_json(500, {"error": str(e)})
            return

        # Picoclaw native agent (14 built-in tools)
        if path == "/v1/picoclaw-agent":
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
                env = os.environ.copy()
                env["HOME"] = "/home/pi"
                proc = subprocess.run(
                    ["/usr/local/bin/picoclaw", "agent", "--message", msg],
                    capture_output=True, text=True, timeout=120,
                    env=env,
                )
                # Always parse: extract clean final response + full trace
                raw = (proc.stdout + proc.stderr).encode("utf-8", errors="replace")
                meta = re.search(rb"\{[^}]*final_length=(\d+)[^}]*\}", raw)
                crab = raw.find("🦀".encode())

                if meta and crab >= 0:
                    final_len = int(meta.group(1))
                    start = crab + len("🦀".encode())
                    while start < len(raw) and raw[start:start+1] in (b" ", b"\n", b"\r"):
                        start += 1
                    clean = raw[start:start + final_len].decode("utf-8", errors="replace").strip()
                    # Trace = everything between 🦀 and stats line (non-log)
                    meta_start = raw.find(meta.group(0))
                    trace_raw = raw[crab:meta_start].decode("utf-8", errors="replace")
                else:
                    log_pat = re.compile(r"^\d{4}/\d{2}/\d{2} ")
                    lines = (proc.stdout + proc.stderr).splitlines()
                    resp_lines = [l for l in lines if not log_pat.match(l) and l.strip()]
                    clean = re.sub(r"^🦀\s*", "", "\n".join(resp_lines).strip())
                    trace_raw = "\n".join(resp_lines)

                # Build trace with section markers
                trace_lines = trace_raw.splitlines()
                trace_sections, current = [], []
                for line in trace_lines:
                    if re.match(r"^\s*(#|import |from |def |class |>>>|===|---)", line):
                        if current:
                            trace_sections.append("\n".join(current))
                            current = []
                    current.append(line)
                if current:
                    trace_sections.append("\n".join(current))

                # Build final response: final answer + (in verbose) trace blocks
                if verbose and len(trace_sections) > 1:
                    parts = []
                    for i, sec in enumerate(trace_sections):
                        sec = sec.strip()
                        if not sec:
                            continue
                        # First section is the final response text (already in clean)
                        if i == 0:
                            parts.append(sec)
                        else:
                            parts.append(f"\n\n---\n**[Trace — step {i}]**\n```\n{sec}\n```")
                    response = "\n".join(parts)
                else:
                    response = clean

                if not response:
                    response = "(no response)"
                content = json.dumps(response)
                sse = (
                    f"data: {{\"choices\":[{{\"delta\":{{\"content\":{content}}},\"index\":0}}]}}\n\n"
                    f"data: [DONE]\n\n"
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Content-Length", str(len(sse)))
                self.end_headers()
                self.wfile.write(sse)
            except subprocess.TimeoutExpired:
                self.send_json(504, {"error": "picoclaw agent timed out"})
            except Exception as e:
                log.error("picoclaw-agent error: %s", e)
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
        elif path.startswith("/core/workspace/"):
            from urllib.parse import unquote
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


if __name__ == "__main__":
    server = HTTPServer(("127.0.0.1", PORT), Handler)
    log.info("ClawbotCore listening on http://127.0.0.1:%d", PORT)
    server.serve_forever()
