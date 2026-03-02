#!/usr/bin/env python3
"""
ClawbotOS Module Template
Replace this with your module logic.

Required endpoints:
  GET /v1/my-module/status  → {"ok": true, "version": "1.0.0"}

Optional:
  Any additional endpoints your module provides.
"""

import json
from http.server import BaseHTTPRequestHandler, HTTPServer

MODULE_ID = "my-module"
MODULE_VERSION = "1.0.0"
PORT = 8091  # Pick a unique port (8091-8199 range for community modules)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def send_json(self, code, data):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == f"/v1/{MODULE_ID}/status":
            self.send_json(200, {"ok": True, "version": MODULE_VERSION})
        else:
            self.send_json(404, {"error": "not found"})


if __name__ == "__main__":
    server = HTTPServer(("127.0.0.1", PORT), Handler)
    print(f"[{MODULE_ID}] listening on :{PORT}")
    server.serve_forever()
