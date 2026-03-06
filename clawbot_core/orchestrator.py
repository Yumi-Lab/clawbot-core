"""
ClawbotCore — Tool Loop Orchestrator
Proxies /v1/chat/completions to PicoClaw, injecting tools from installed modules.
Executes tool_calls by calling module HTTP endpoints and loops until final response.
"""

import json
import logging
import os
import subprocess
import tempfile
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

from registry import get_enabled_tools, load_local_modules

PICOCLAW_CONFIG = "/home/pi/.picoclaw/config.json"
MODULES_DIR = "/home/pi/.clawbot/modules"
MAX_TOOL_ROUNDS = 8
LLM_TIMEOUT = 120
TOOL_TIMEOUT = 10
TOOL_RESULT_MAX_CHARS = 6000   # truncate tool output beyond this to save tokens

# Built-in system tools — available in Core mode alongside module tools
BUILTIN_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "system__bash",
            "description": "Execute a bash shell command on the Pi and return stdout + stderr. Use for system tasks, file operations, service management, etc.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "The bash command to execute"},
                    "timeout": {"type": "integer", "description": "Timeout in seconds (default 30)", "default": 30},
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "system__python",
            "description": "Execute a Python script on the Pi and return its output. Write the full script content, it will be saved to a temp file and run.",
            "parameters": {
                "type": "object",
                "properties": {
                    "code": {"type": "string", "description": "Complete Python script to execute"},
                    "timeout": {"type": "integer", "description": "Timeout in seconds (default 30)", "default": 30},
                },
                "required": ["code"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "system__write_file",
            "description": "Write content to a file on the Pi filesystem.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Absolute path of the file to write"},
                    "content": {"type": "string", "description": "Content to write"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "system__read_file",
            "description": "Read the content of a file on the Pi filesystem.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Absolute path of the file to read"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "system__ssh",
            "description": "Execute a command on a remote server via SSH with password authentication. Use for server audits, remote administration, checking services on external hosts. sshpass must be installed on the Pi.",
            "parameters": {
                "type": "object",
                "properties": {
                    "host": {"type": "string", "description": "Remote hostname or IP address"},
                    "user": {"type": "string", "description": "SSH username"},
                    "password": {"type": "string", "description": "SSH password"},
                    "command": {"type": "string", "description": "Shell command to execute on the remote host"},
                    "port": {"type": "integer", "description": "SSH port (default 22)", "default": 22},
                    "timeout": {"type": "integer", "description": "Timeout in seconds (default 30)", "default": 30},
                },
                "required": ["host", "user", "password", "command"],
            },
        },
    },
]

# Context compaction — triggered when estimated input tokens exceed threshold
COMPACT_THRESHOLD = 15000   # estimated tokens (~chars/4) before compaction
COMPACT_KEEP_RECENT = 6     # number of non-system messages to keep verbatim

log = logging.getLogger(__name__)


def _load_llm_config() -> tuple[str, str, str]:
    """
    Read base_url, api_key and model from picoclaw config.
    Falls back to picoclaw local gateway if config is missing.
    Returns (url, api_key, model).
    """
    try:
        with open(PICOCLAW_CONFIG) as f:
            cfg = json.load(f)
        entry = cfg.get("model_list", [{}])[0]
        base = entry.get("api_base", entry.get("base_url", "")).rstrip("/")
        key = entry.get("api_key", "")
        model = entry.get("model", "")
        if base and key:
            # Anthropic native API is not OpenAI-compatible (/messages vs /chat/completions).
            # Route through PicoClaw (port 8080) which handles format translation.
            if "anthropic.com" in base:
                return "http://127.0.0.1:8080/v1/chat/completions", "", model
            return f"{base}/chat/completions", key, model
    except Exception:
        pass
    # Fallback: picoclaw local gateway
    return "http://127.0.0.1:8080/v1/chat/completions", "", ""


def _estimate_tokens(messages: list) -> int:
    """Rough token estimate: ~4 chars per token."""
    return sum(len(str(m.get("content", ""))) for m in messages) // 4


def _compact_messages(messages: list) -> list:
    """
    Summarize old conversation messages to reduce context size.
    Mirrors Claude Code's automatic context compaction:
      - Keeps system messages intact
      - Summarizes all but the last COMPACT_KEEP_RECENT non-system messages
      - Replaces history with [system] + [summary user msg] + [summary ack] + [recent]
    """
    system_msgs = [m for m in messages if m.get("role") == "system"]
    non_system = [m for m in messages if m.get("role") != "system"]

    if len(non_system) <= COMPACT_KEEP_RECENT:
        return messages  # Nothing to compact

    to_summarize = non_system[:-COMPACT_KEEP_RECENT]
    recent = non_system[-COMPACT_KEEP_RECENT:]

    url, api_key, model = _load_llm_config()
    summary_body = {
        "model": model,
        "stream": False,
        "max_tokens": 1024,
        "messages": to_summarize + [{
            "role": "user",
            "content": (
                "Provide a concise summary of the conversation above. "
                "Include: what was accomplished, files created or modified, "
                "tools used, current state, and any key context needed to continue. "
                "Be brief but complete."
            ),
        }],
    }
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    req = urllib.request.Request(
        url, data=json.dumps(summary_body).encode(), headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
        summary_text = data["choices"][0]["message"]["content"]
    except Exception as e:
        log.warning("Context compaction failed, keeping full history: %s", e)
        return messages

    compacted = system_msgs + [
        {"role": "user", "content": f"[Conversation summary — context compacted]\n\n{summary_text}"},
        {"role": "assistant", "content": "Understood. Continuing based on this summary."},
    ] + recent
    log.info("Context compacted: %d → %d messages (~%d tokens saved)",
             len(messages), len(compacted),
             _estimate_tokens(to_summarize))
    return compacted


def chat_with_tools(request_body: dict) -> dict:
    """
    Main orchestration loop.
    Injects available tools, calls PicoClaw, executes tool_calls, loops.
    Returns final OpenAI-compatible response dict.
    """
    module_tools = get_enabled_tools()
    tools = BUILTIN_TOOLS + module_tools  # system tools always available

    # Work on a copy to avoid mutating caller's data
    body = dict(request_body)
    if tools:
        body["tools"] = tools
        body["tool_choice"] = "auto"

    # Inject system prompt if not already present — instructs LLM to USE tools
    messages = body.get("messages", [])
    has_system = any(m.get("role") == "system" for m in messages)
    if not has_system and tools:
        tool_names = ", ".join(
            t["function"]["name"] for t in tools if t.get("type") == "function"
        )
        system_content = (
            "You are ClawbotOS Core, an AI assistant running on a Raspberry Pi (AllWinner H3, armhf/arm64). "
            f"You have access to the following tools: {tool_names}. "
            "ALWAYS use your tools to complete tasks — never just describe how to do something. "
            "Execute commands, write files, and run code directly. "
            "Be concise and action-oriented.\n"
            "Hardware tips: CPU temp via `cat /sys/class/thermal/thermal_zone0/temp` (divide by 1000 for °C) — "
            "vcgencmd is NOT available on AllWinner. "
            "Network interface is end0 (Ethernet) or wlx* (WiFi USB), not eth0. "
            "Use `ip addr` not `ifconfig`. "
            "Prefer stdlib-only Python (no pip). "
            "When a command fails, try an alternative instead of giving up."
        )
        body["messages"] = [{"role": "system", "content": system_content}] + messages
    # Tool loop requires non-streaming internally
    body["stream"] = False

    # Use model from config if caller didn't specify one
    _, _, default_model = _load_llm_config()
    if not body.get("model") or body.get("model") == "default":
        if default_model:
            body["model"] = default_model

    for round_num in range(MAX_TOOL_ROUNDS):
        # Auto-compact if context is getting too large (like Claude Code's /compact)
        estimated = _estimate_tokens(body["messages"])
        if estimated > COMPACT_THRESHOLD:
            log.info("Context too large (~%d tokens), auto-compacting...", estimated)
            body["messages"] = _compact_messages(body["messages"])

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

        # Execute tool calls in parallel when multiple are requested
        def _run_tc(tc):
            fn = tc.get("function", {})
            tool_name = fn.get("name", "")
            arguments_raw = fn.get("arguments", "{}")
            tool_call_id = tc.get("id", "")
            result = _execute_tool(tool_name, arguments_raw)
            # Truncate oversized tool output to avoid context explosion
            if len(result) > TOOL_RESULT_MAX_CHARS:
                result = result[:TOOL_RESULT_MAX_CHARS] + f"\n[...truncated {len(result) - TOOL_RESULT_MAX_CHARS} chars]"
            return tool_call_id, result

        if len(tool_calls) > 1:
            results_map = {}
            with ThreadPoolExecutor(max_workers=min(len(tool_calls), 4)) as ex:
                futures = {ex.submit(_run_tc, tc): tc.get("id", "") for tc in tool_calls}
                for fut in as_completed(futures):
                    tc_id, result = fut.result()
                    results_map[tc_id] = result
            # Append in original order
            for tc in tool_calls:
                body["messages"].append({
                    "role": "tool",
                    "tool_call_id": tc.get("id", ""),
                    "content": results_map[tc.get("id", "")],
                })
        else:
            tc_id, result = _run_tc(tool_calls[0])
            body["messages"].append({
                "role": "tool",
                "tool_call_id": tc_id,
                "content": result,
            })

    # Safety fallback after max rounds — ask for final answer without tools
    log.warning("Reached max tool rounds (%d), forcing final response", MAX_TOOL_ROUNDS)
    body.pop("tools", None)
    body.pop("tool_choice", None)
    return _call_picoclaw(body)


def chat_with_tools_stream(request_body: dict):
    """
    Generator variant of chat_with_tools for real-time SSE streaming.
    Yields dicts: {"type": "tool_call"|"tool_result"|"done"|"error", ...}
    tool_call: {"type":"tool_call","round":N,"calls":[{"id","name","args"},...]}
    tool_result: {"type":"tool_result","round":N,"results":[{"name","result"},...]}
    done: {"type":"done","content":"final text"}
    error: {"type":"error","message":"..."}
    """
    module_tools = get_enabled_tools()
    tools = BUILTIN_TOOLS + module_tools

    body = dict(request_body)
    if tools:
        body["tools"] = tools
        body["tool_choice"] = "auto"

    messages = body.get("messages", [])
    has_system = any(m.get("role") == "system" for m in messages)
    if not has_system and tools:
        tool_names = ", ".join(
            t["function"]["name"] for t in tools if t.get("type") == "function"
        )
        system_content = (
            "You are ClawbotOS Core, an AI assistant running on a Raspberry Pi (AllWinner H3, armhf/arm64). "
            f"You have access to the following tools: {tool_names}. "
            "ALWAYS use your tools to complete tasks — never just describe how to do something. "
            "Execute commands, write files, and run code directly. "
            "Be concise and action-oriented.\n"
            "Hardware tips: CPU temp via `cat /sys/class/thermal/thermal_zone0/temp` (divide by 1000 for °C) — "
            "vcgencmd is NOT available on AllWinner. "
            "Network interface is end0 (Ethernet) or wlx* (WiFi USB), not eth0. "
            "Use `ip addr` not `ifconfig`. "
            "Prefer stdlib-only Python (no pip). "
            "When a command fails, try an alternative instead of giving up."
        )
        body["messages"] = [{"role": "system", "content": system_content}] + messages
    body["stream"] = False

    _, _, default_model = _load_llm_config()
    if not body.get("model") or body.get("model") == "default":
        if default_model:
            body["model"] = default_model

    for round_num in range(MAX_TOOL_ROUNDS):
        estimated = _estimate_tokens(body["messages"])
        if estimated > COMPACT_THRESHOLD:
            log.info("Context too large (~%d tokens), auto-compacting...", estimated)
            body["messages"] = _compact_messages(body["messages"])

        log.info("Tool loop round %d/%d (stream)", round_num + 1, MAX_TOOL_ROUNDS)
        response = _call_picoclaw(body)

        if not response.get("choices"):
            yield {"type": "error", "message": str(response)}
            return

        choice = response["choices"][0]
        finish = choice.get("finish_reason", "stop")

        if finish != "tool_calls":
            content = (choice.get("message", {}).get("content") or "").strip().replace("\U0001F99E", "")
            yield {"type": "done", "content": content}
            return

        tool_calls = choice.get("message", {}).get("tool_calls", [])
        if not tool_calls:
            content = (choice.get("message", {}).get("content") or "").strip().replace("\U0001F99E", "")
            yield {"type": "done", "content": content}
            return

        # Emit tool_call event with call details
        calls_info = []
        for tc in tool_calls:
            fn = tc.get("function", {})
            try:
                args = json.loads(fn.get("arguments", "{}"))
            except Exception:
                args = {"raw": fn.get("arguments", "")}
            calls_info.append({
                "id": tc.get("id", ""),
                "name": fn.get("name", ""),
                "args": args,
            })
        yield {"type": "tool_call", "round": round_num + 1, "calls": calls_info}

        body["messages"].append(choice["message"])

        # Execute tools (parallel if multiple)
        def _run_tc_stream(tc):
            fn = tc.get("function", {})
            tool_name = fn.get("name", "")
            arguments_raw = fn.get("arguments", "{}")
            tool_call_id = tc.get("id", "")
            result = _execute_tool(tool_name, arguments_raw)
            if len(result) > TOOL_RESULT_MAX_CHARS:
                result = result[:TOOL_RESULT_MAX_CHARS] + f"\n[...truncated {len(result) - TOOL_RESULT_MAX_CHARS} chars]"
            return tool_call_id, fn.get("name", ""), result

        results_list = []
        if len(tool_calls) > 1:
            results_map, names_map = {}, {}
            with ThreadPoolExecutor(max_workers=min(len(tool_calls), 4)) as ex:
                futures = {ex.submit(_run_tc_stream, tc): tc.get("id", "") for tc in tool_calls}
                for fut in as_completed(futures):
                    tc_id, name, result = fut.result()
                    results_map[tc_id] = result
                    names_map[tc_id] = name
            for tc in tool_calls:
                tc_id = tc.get("id", "")
                results_list.append({"name": names_map.get(tc_id, ""), "result": results_map[tc_id]})
                body["messages"].append({
                    "role": "tool",
                    "tool_call_id": tc_id,
                    "content": results_map[tc_id],
                })
        else:
            tc_id, name, result = _run_tc_stream(tool_calls[0])
            results_list.append({"name": name, "result": result})
            body["messages"].append({
                "role": "tool",
                "tool_call_id": tc_id,
                "content": result,
            })

        yield {"type": "tool_result", "round": round_num + 1, "results": results_list}

    # Safety fallback after max rounds
    body.pop("tools", None)
    body.pop("tool_choice", None)
    response = _call_picoclaw(body)
    content = (response.get("choices", [{}])[0].get("message", {}).get("content") or "").strip().replace("\U0001F99E", "")
    yield {"type": "done", "content": content}


def _call_picoclaw(body: dict) -> dict:
    """POST request_body to the configured LLM API and return parsed JSON response."""
    url, api_key, default_model = _load_llm_config()

    # Ensure a model is set
    payload = dict(body)
    if not payload.get("model") or payload.get("model") == "default":
        if default_model:
            payload["model"] = default_model

    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=LLM_TIMEOUT) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body_text = e.read().decode(errors="replace")
        log.error("LLM API HTTP %d: %s", e.code, body_text)
        return _error_response(f"LLM API error {e.code}: {body_text[:200]}")
    except Exception as e:
        log.error("LLM API unreachable: %s", e)
        return _error_response(f"LLM API unreachable: {e}")


_PROTECTED_SERVICES = {"clawbot-core", "picoclaw", "nginx", "clawbot-cloud", "clawbot-status-api"}
_BASH_BLOCKED_PATTERNS = [
    # Prevent stopping/restarting/disabling the services that run ClawbotCore itself
    "systemctl stop", "systemctl restart", "systemctl disable",
    "systemctl kill", "systemctl mask", "service stop", "service restart",
    "kill -9", "pkill picoclaw", "pkill clawbot",
]


def _is_dangerous_command(cmd: str) -> str | None:
    """Return a reason string if the command should be blocked, else None."""
    cmd_lower = cmd.lower()
    for pattern in _BASH_BLOCKED_PATTERNS:
        if pattern in cmd_lower:
            # Allow if targeting something other than protected services
            hits_protected = any(svc in cmd_lower for svc in _PROTECTED_SERVICES)
            if hits_protected:
                return f"Blocked: '{pattern}' on a protected ClawbotOS service"
    return None


def _execute_builtin(tool_suffix: str, arguments: dict) -> str:
    """Execute a built-in system tool directly (no HTTP call needed)."""
    timeout = int(arguments.get("timeout", 30))

    if tool_suffix == "bash":
        cmd = arguments.get("command", "")
        if not cmd:
            return "[error] No command provided"
        reason = _is_dangerous_command(cmd)
        if reason:
            log.warning("Blocked dangerous bash command: %s — %s", cmd[:80], reason)
            return f"[blocked] {reason}"
        try:
            result = subprocess.run(
                cmd, shell=True, capture_output=True, text=True, timeout=timeout,
                env={**os.environ, "HOME": "/home/pi"},
            )
            out = result.stdout + result.stderr
            return out.strip() or "(no output)"
        except subprocess.TimeoutExpired:
            return f"[error] Command timed out after {timeout}s"
        except Exception as e:
            return f"[error] {e}"

    if tool_suffix == "python":
        code = arguments.get("code", "")
        if not code:
            return "[error] No code provided"
        try:
            with tempfile.NamedTemporaryFile(suffix=".py", mode="w", delete=False) as f:
                f.write(code)
                tmp_path = f.name
            result = subprocess.run(
                ["python3", tmp_path], capture_output=True, text=True, timeout=timeout,
                env={**os.environ, "HOME": "/home/pi"},
            )
            os.unlink(tmp_path)
            out = result.stdout + result.stderr
            return out.strip() or "(no output)"
        except subprocess.TimeoutExpired:
            return f"[error] Script timed out after {timeout}s"
        except Exception as e:
            return f"[error] {e}"

    if tool_suffix == "write_file":
        path = arguments.get("path", "")
        content = arguments.get("content", "")
        if not path:
            return "[error] No path provided"
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as f:
                f.write(content)
            return f"Written {len(content)} bytes to {path}"
        except Exception as e:
            return f"[error] {e}"

    if tool_suffix == "read_file":
        path = arguments.get("path", "")
        if not path:
            return "[error] No path provided"
        try:
            with open(path) as f:
                content = f.read()
            return content or "(empty file)"
        except Exception as e:
            return f"[error] {e}"

    if tool_suffix == "ssh":
        import shutil
        host = arguments.get("host", "")
        user = arguments.get("user", "")
        password = arguments.get("password", "")
        command = arguments.get("command", "")
        port = int(arguments.get("port", 22))
        if not all([host, user, password, command]):
            return "[error] Missing required SSH parameters: host, user, password, command"
        if not shutil.which("sshpass"):
            return "[error] sshpass not found — install with: apt-get install sshpass"
        try:
            result = subprocess.run(
                ["sshpass", "-p", password, "ssh",
                 "-o", "StrictHostKeyChecking=no",
                 "-o", "ConnectTimeout=10",
                 "-p", str(port),
                 f"{user}@{host}", command],
                capture_output=True, text=True, timeout=timeout,
                env={**os.environ, "HOME": "/home/pi"},
            )
            out = result.stdout + result.stderr
            return out.strip() or "(no output)"
        except subprocess.TimeoutExpired:
            return f"[error] SSH command timed out after {timeout}s"
        except Exception as e:
            return f"[error] {e}"

    return f"[error] Unknown built-in tool: system__{tool_suffix}"


def _execute_tool(tool_name: str, arguments_raw: str) -> str:
    """
    Execute a tool by calling the owning module's HTTP endpoint.
    tool_name format: "{module_id}__{tool_name}" (double underscore)
    Built-in tools (system__*) are executed locally without HTTP.
    Returns: string result (tool output or error description)
    """
    if "__" not in tool_name:
        return f"[error] Invalid tool name format: '{tool_name}' (expected module_id__tool)"

    module_id, _, tool_suffix = tool_name.partition("__")

    try:
        arguments = json.loads(arguments_raw) if arguments_raw else {}
    except json.JSONDecodeError:
        arguments = {"raw": arguments_raw}

    # Built-in system tools — executed locally
    if module_id == "system":
        return _execute_builtin(tool_suffix, arguments)

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
    """Return an OpenAI-compatible error response with finish_reason='error'."""
    return {
        "id": "err",
        "object": "chat.completion",
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": f"[ClawbotCore error] {message}"},
            "finish_reason": "error",
        }],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }
