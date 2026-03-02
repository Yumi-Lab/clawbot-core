#!/usr/bin/env python3
"""
ClawbotCore — Module Registry & Middleware
Lightweight HTTP API on 127.0.0.1:8090

Endpoints:
  GET  /core/health
  GET  /core/modules               — list all modules (installed + store)
  GET  /core/modules/{id}          — module details
  POST /core/modules/{id}/install  — install from repo   body: {"repo": "https://..."}
  POST /core/modules/{id}/enable   — enable (start service)
  POST /core/modules/{id}/disable  — disable (stop service)
  POST /core/modules/{id}/uninstall

  POST /v1/chat/completions        — tool-aware chat proxy (→ PicoClaw + module tools)
"""

import json
import logging
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse

from registry import get_all_modules, load_local_modules
from installer import install, uninstall, enable, disable

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

        # Tool-aware chat proxy
        if path == "/v1/chat/completions":
            try:
                from orchestrator import chat_with_tools
                result = chat_with_tools(data)
                self.send_json(200, result)
            except Exception as e:
                log.error("chat_with_tools error: %s", e)
                self.send_json(500, {"error": str(e)})
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


if __name__ == "__main__":
    server = HTTPServer(("127.0.0.1", PORT), Handler)
    log.info("ClawbotCore listening on http://127.0.0.1:%d", PORT)
    server.serve_forever()
