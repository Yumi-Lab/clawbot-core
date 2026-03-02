"""
ClawbotCore — Module Registry
Loads module manifests from local install dir and remote store index.
"""

import json
import os
import subprocess
import urllib.request

MODULES_DIR = "/home/pi/.clawbot/modules"
STORE_URL = "https://raw.githubusercontent.com/Yumi-Lab/clawbot-core/main/store/index.json"
STORE_CACHE_TTL = 300  # seconds


def _read_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return None


def load_local_modules():
    """Scan MODULES_DIR for installed module manifests."""
    modules = {}
    if not os.path.isdir(MODULES_DIR):
        return modules
    for name in os.listdir(MODULES_DIR):
        manifest_path = os.path.join(MODULES_DIR, name, "manifest.json")
        m = _read_json(manifest_path)
        if m and "id" in m:
            m["installed"] = True
            m["enabled"] = _is_service_active(m.get("service", ""))
            modules[m["id"]] = m
    return modules


def load_store_index():
    """Fetch store index from GitHub. Returns list of module stubs."""
    try:
        with urllib.request.urlopen(STORE_URL, timeout=8) as resp:
            data = json.loads(resp.read())
            return {m["id"]: m for m in data.get("modules", [])}
    except Exception:
        return {}


def get_all_modules():
    """Merge local installed modules with store catalog."""
    local = load_local_modules()
    store = load_store_index()

    result = {}
    # Start with store entries
    for mid, m in store.items():
        result[mid] = {**m, "installed": False, "enabled": False}
    # Override / enrich with local data
    for mid, m in local.items():
        if mid in result:
            result[mid].update(m)
        else:
            result[mid] = m
    return result


def get_enabled_tools():
    """
    Return OpenAI-compatible tool schemas from all enabled installed modules
    that declare 'tool_definitions' in their manifest.json.

    Tool name format: "{module_id}__{tool_name}" (double underscore separator)
    so the orchestrator can route tool_calls back to the correct module.
    """
    tools = []
    for m in load_local_modules().values():
        if not m.get("enabled"):
            continue
        for tdef in m.get("tool_definitions", []):
            tool_name = tdef.get("name", "")
            if not tool_name:
                continue
            tools.append({
                "type": "function",
                "function": {
                    "name": f"{m['id']}__{tool_name}",
                    "description": tdef.get("description", ""),
                    "parameters": tdef.get("parameters", {
                        "type": "object",
                        "properties": {},
                        "required": [],
                    }),
                },
            })
    return tools


def _is_service_active(service_name):
    if not service_name:
        return False
    try:
        r = subprocess.run(
            ["systemctl", "is-active", service_name],
            capture_output=True, text=True, timeout=3
        )
        return r.stdout.strip() == "active"
    except Exception:
        return False
