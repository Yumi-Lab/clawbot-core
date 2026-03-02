"""
ClawbotCore — Tool Loop Orchestrator
Proxies /v1/chat/completions to PicoClaw, injecting tools from installed modules.
Executes tool_calls by calling module HTTP endpoints and loops until final response.
"""

import json
import logging
import os
import urllib.error
import urllib.request

from registry import get_enabled_tools, load_local_modules

PICOCLAW_URL = "http://127.0.0.1:8080/v1/chat/completions"
MODULES_DIR = "/home/pi/.clawbot/modules"
MAX_TOOL_ROUNDS = 5
PICOCLAW_TIMEOUT = 120
TOOL_TIMEOUT = 10

log = logging.getLogger(__name__)


def chat_with_tools(request_body: dict) -> dict:
    """
    Main orchestration loop.
    Injects available tools, calls PicoClaw, executes tool_calls, loops.
    Returns final OpenAI-compatible response dict.
    """
    tools = get_enabled_tools()

    # Work on a copy to avoid mutating caller's data
    body = dict(request_body)
    if tools:
        body["tools"] = tools
        body["tool_choice"] = "auto"
    # Tool loop requires non-streaming internally
    body["stream"] = False

    for round_num in range(MAX_TOOL_ROUNDS):
        log.info("Tool loop round %d/%d", round_num + 1, MAX_TOOL_ROUNDS)
        response = _call_picoclaw(body)

        if not response.get("choices"):
            return response

        choice = response["choices"][0]
        finish = choice.get("finish_reason", "stop")

        if finish != "tool_calls":
            return response  # Final text answer — done

        tool_calls = choice.get("message", {}).get("tool_calls", [])
        if not tool_calls:
            return response

        log.info("Executing %d tool call(s)", len(tool_calls))

        # Append assistant message with tool_calls to history
        body["messages"].append(choice["message"])

        # Execute each tool call and collect results
        for tc in tool_calls:
            fn = tc.get("function", {})
            tool_name = fn.get("name", "")
            arguments_raw = fn.get("arguments", "{}")
            tool_call_id = tc.get("id", "")

            result = _execute_tool(tool_name, arguments_raw)

            body["messages"].append({
                "role": "tool",
                "tool_call_id": tool_call_id,
                "content": result,
            })

    # Safety fallback after max rounds — ask for final answer without tools
    log.warning("Reached max tool rounds (%d), forcing final response", MAX_TOOL_ROUNDS)
    body.pop("tools", None)
    body.pop("tool_choice", None)
    return _call_picoclaw(body)


def _call_picoclaw(body: dict) -> dict:
    """POST request_body to PicoClaw and return parsed JSON response."""
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        PICOCLAW_URL,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=PICOCLAW_TIMEOUT) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body_text = e.read().decode(errors="replace")
        log.error("PicoClaw HTTP %d: %s", e.code, body_text)
        return _error_response(f"PicoClaw error {e.code}: {body_text[:200]}")
    except Exception as e:
        log.error("PicoClaw unreachable: %s", e)
        return _error_response(f"PicoClaw unreachable: {e}")


def _execute_tool(tool_name: str, arguments_raw: str) -> str:
    """
    Execute a tool by calling the owning module's HTTP endpoint.
    tool_name format: "{module_id}__{tool_name}" (double underscore)
    Calls: POST http://127.0.0.1:{port}/v1/{module_id}/execute
    body: {"tool": tool_suffix, "arguments": {...}}
    Returns: string result (tool output or error description)
    """
    if "__" not in tool_name:
        return f"[error] Invalid tool name format: '{tool_name}' (expected module_id__tool)"

    module_id, _, tool_suffix = tool_name.partition("__")

    try:
        arguments = json.loads(arguments_raw) if arguments_raw else {}
    except json.JSONDecodeError:
        arguments = {"raw": arguments_raw}

    port = _get_module_port(module_id)
    if port is None:
        return f"[error] Module '{module_id}' not found or has no port defined"

    url = f"http://127.0.0.1:{port}/v1/{module_id}/execute"
    payload = json.dumps({"tool": tool_suffix, "arguments": arguments}).encode()

    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=TOOL_TIMEOUT) as resp:
            raw = resp.read().decode(errors="replace")
            try:
                data = json.loads(raw)
                # Accept {"result": "..."} or {"output": "..."} or plain string
                return str(data.get("result") or data.get("output") or raw)
            except json.JSONDecodeError:
                return raw
    except urllib.error.HTTPError as e:
        msg = e.read().decode(errors="replace")
        log.error("Tool '%s' HTTP %d: %s", tool_name, e.code, msg)
        return f"[error] Tool returned HTTP {e.code}: {msg[:200]}"
    except Exception as e:
        log.error("Tool '%s' call failed: %s", tool_name, e)
        return f"[error] Tool call failed: {e}"


def _get_module_port(module_id: str) -> int | None:
    """Read port from installed module's manifest.json."""
    manifest_path = os.path.join(MODULES_DIR, module_id, "manifest.json")
    try:
        with open(manifest_path) as f:
            manifest = json.load(f)
        port = manifest.get("port")
        if isinstance(port, int) and 1024 < port < 65535:
            return port
    except Exception:
        pass
    return None


def _error_response(message: str) -> dict:
    """Return an OpenAI-compatible error response."""
    return {
        "id": "err",
        "object": "chat.completion",
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": f"[ClawbotCore error] {message}"},
            "finish_reason": "stop",
        }],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }
