"""
ClawbotCore — MCP Server for Vault
Exposes vault operations as MCP tools over stdio.
Stdlib-only, no pip dependencies.

Usage:
  python3 mcp_vault.py

MCP protocol: JSON-RPC 2.0 over stdin/stdout (one JSON object per line).
"""
from __future__ import annotations

import json
import logging
import sys

from vault import Vault

log = logging.getLogger("mcp_vault")
logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                    format="%(asctime)s [mcp_vault] %(message)s")

# ── Tool definitions ──────────────────────────────────────────────────────────

TOOLS = [
    {
        "name": "vault_store",
        "description": (
            "Store a credential in the encrypted vault. "
            "Use a clear, searchable name like 'ionos_smtp' or 'github_deploy'."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Unique key (e.g. 'ionos_smtp', 'aws_prod')"},
                "value": {"type": "string", "description": "The secret value (will be AES-256 encrypted)"},
                "username": {"type": "string", "description": "Login/email associated (e.g. 'nicolas@3d-expert.fr')", "default": ""},
                "category": {"type": "string", "enum": ["llm", "email", "ssh", "api", "oauth", "other"], "default": "other"},
                "note": {"type": "string", "description": "Context (e.g. 'IONOS SMTP smtp.ionos.com:587')", "default": ""},
            },
            "required": ["name", "value"],
        },
    },
    {
        "name": "vault_get",
        "description": "Retrieve a decrypted secret by name.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Secret name to retrieve"},
            },
            "required": ["name"],
        },
    },
    {
        "name": "vault_list",
        "description": (
            "List all secrets with name, username, category and note. "
            "Does NOT reveal secret values. Use this to find the right credential."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "category": {"type": "string", "description": "Filter by category (optional)"},
            },
        },
    },
    {
        "name": "vault_delete",
        "description": "Delete a secret from the vault by name.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Secret name to delete"},
            },
            "required": ["name"],
        },
    },
    {
        "name": "vault_search",
        "description": (
            "Search vault secrets by keyword. Matches against name, username, "
            "category and note fields. Returns matching entries without values."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search keyword (e.g. 'ionos', 'smtp', 'nicolas')"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "vault_flag_secret",
        "description": (
            "Flag and protect a raw secret seen in conversation. "
            "The value will be masked in all future messages."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "value": {"type": "string", "description": "The raw secret value"},
                "suggested_name": {"type": "string", "description": "Name for this secret"},
                "category": {"type": "string", "enum": ["llm", "email", "ssh", "api", "oauth", "other"], "default": "other"},
                "pattern_hint": {"type": "string", "description": "Regex pattern for auto-detection (optional)", "default": ""},
            },
            "required": ["value", "suggested_name"],
        },
    },
    {
        "name": "vault_protect_pii",
        "description": (
            "Protect personal data (name, address, phone) from being sent to AI. "
            "The value will be replaced by an alias in all messages."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Identifier (e.g. 'my_name', 'home_address')"},
                "value": {"type": "string", "description": "The personal data to protect"},
                "category": {"type": "string", "enum": ["name", "address", "phone", "email", "financial", "other"], "default": "other"},
            },
            "required": ["name", "value"],
        },
    },
]

# ── Tool execution ────────────────────────────────────────────────────────────

_vault: Vault | None = None


def _get_vault() -> Vault:
    global _vault
    if _vault is None:
        _vault = Vault()
    return _vault


def execute_tool(name: str, arguments: dict) -> dict:
    """Execute a vault tool. Returns {"content": [{"type": "text", "text": ...}]}."""
    try:
        v = _get_vault()

        if name == "vault_store":
            ok = v.store(
                arguments["name"], arguments["value"],
                arguments.get("category", "other"),
                arguments.get("note", ""),
                arguments.get("username", ""),
            )
            text = f"Secret '{arguments['name']}' stored." if ok else "Failed to store."
            if arguments.get("username"):
                text += f" (user: {arguments['username']})"

        elif name == "vault_get":
            val = v.get(arguments["name"])
            text = val if val else f"Secret '{arguments['name']}' not found."

        elif name == "vault_list":
            items = v.list(arguments.get("category"))
            if not items:
                text = "Vault is empty."
            else:
                lines = []
                for s in items:
                    parts = [s["name"], f"[{s['category']}]"]
                    if s.get("username"):
                        parts.append(f"user: {s['username']}")
                    if s.get("note"):
                        parts.append(f"— {s['note']}")
                    lines.append("  - " + " ".join(parts))
                text = f"Vault contains {len(items)} secret(s):\n" + "\n".join(lines)

        elif name == "vault_delete":
            ok = v.delete(arguments["name"])
            text = f"Secret '{arguments['name']}' deleted." if ok else f"'{arguments['name']}' not found."

        elif name == "vault_search":
            q = arguments["query"].lower()
            items = v.list()
            matches = [
                s for s in items
                if q in s["name"].lower()
                or q in (s.get("username") or "").lower()
                or q in (s.get("category") or "").lower()
                or q in (s.get("note") or "").lower()
            ]
            if not matches:
                text = f"No secrets matching '{arguments['query']}'."
            else:
                lines = []
                for s in matches:
                    parts = [s["name"], f"[{s['category']}]"]
                    if s.get("username"):
                        parts.append(f"user: {s['username']}")
                    if s.get("note"):
                        parts.append(f"— {s['note']}")
                    lines.append("  - " + " ".join(parts))
                text = f"Found {len(matches)} match(es):\n" + "\n".join(lines)

        elif name == "vault_flag_secret":
            alias = v.protect(
                arguments["suggested_name"], arguments["value"],
                kind="secret", category=arguments.get("category", "other"),
            )
            text = f"Secret protected as {alias}."
            pattern = arguments.get("pattern_hint", "")
            if pattern and v.learn_pattern(arguments["suggested_name"], pattern, arguments.get("category", "")):
                text += f" Pattern '{pattern}' learned for future auto-detection."

        elif name == "vault_protect_pii":
            alias = v.protect(
                arguments["name"], arguments["value"],
                kind="pii", category=arguments.get("category", "other"),
            )
            text = f"PII protected. '{arguments['name']}' → '{alias}' in all future messages."

        else:
            text = f"Unknown tool: {name}"

    except Exception as e:
        log.error("Tool %s error: %s", name, e)
        text = f"[error] {e}"

    return {"content": [{"type": "text", "text": text}]}


# ── MCP JSON-RPC server ──────────────────────────────────────────────────────

SERVER_INFO = {
    "name": "clawbot-vault",
    "version": "1.0.0",
}

CAPABILITIES = {
    "tools": {},
}


def handle_request(req: dict) -> dict | None:
    """Handle a single JSON-RPC request. Returns response dict or None for notifications."""
    method = req.get("method", "")
    rid = req.get("id")

    if method == "initialize":
        return {
            "jsonrpc": "2.0", "id": rid,
            "result": {
                "protocolVersion": "2024-11-05",
                "serverInfo": SERVER_INFO,
                "capabilities": CAPABILITIES,
            },
        }

    if method == "notifications/initialized":
        return None  # notification, no response

    if method == "tools/list":
        return {
            "jsonrpc": "2.0", "id": rid,
            "result": {"tools": TOOLS},
        }

    if method == "tools/call":
        params = req.get("params", {})
        tool_name = params.get("name", "")
        arguments = params.get("arguments", {})
        result = execute_tool(tool_name, arguments)
        return {
            "jsonrpc": "2.0", "id": rid,
            "result": result,
        }

    # Unknown method
    return {
        "jsonrpc": "2.0", "id": rid,
        "error": {"code": -32601, "message": f"Method not found: {method}"},
    }


def main():
    log.info("MCP Vault server starting (stdio)")
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            sys.stdout.write(json.dumps({
                "jsonrpc": "2.0", "id": None,
                "error": {"code": -32700, "message": "Parse error"},
            }) + "\n")
            sys.stdout.flush()
            continue

        resp = handle_request(req)
        if resp is not None:
            sys.stdout.write(json.dumps(resp) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
