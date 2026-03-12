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
AGENTS_DIR = "/home/pi/.clawbot/agents"
MAX_TOOL_ROUNDS = 15
LLM_TIMEOUT = 240
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
    {
        "type": "function",
        "function": {
            "name": "system__web_search",
            "description": "Search the web using DuckDuckGo and return the top results with titles, URLs, and snippets. Use this to find current information, documentation, tutorials, or answers to questions.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "The search query"},
                    "max_results": {"type": "integer", "description": "Maximum number of results to return (default 5, max 10)", "default": 5},
                },
                "required": ["query"],
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


def chat_with_tools(request_body: dict, override_tools: list = None) -> dict:
    """
    Main orchestration loop.
    Injects available tools, calls PicoClaw, executes tool_calls, loops.
    Returns final OpenAI-compatible response dict.
    override_tools: if set, use these instead of auto-discovered tools.
    """
    if override_tools is not None:
        tools = override_tools
    else:
        module_tools = get_enabled_tools()
        tools = BUILTIN_TOOLS + module_tools

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


def chat_with_tools_stream(request_body: dict, override_tools: list = None):
    """
    Generator variant of chat_with_tools for real-time SSE streaming.
    Yields dicts: {"type": "tool_call"|"tool_result"|"done"|"error", ...}
    tool_call: {"type":"tool_call","round":N,"calls":[{"id","name","args"},...]}
    tool_result: {"type":"tool_result","round":N,"results":[{"name","result"},...]}
    done: {"type":"done","content":"final text"}
    error: {"type":"error","message":"..."}
    """
    if override_tools is not None:
        tools = override_tools
    else:
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

    model_name = body.get("model", "Claude")

    for round_num in range(MAX_TOOL_ROUNDS):
        estimated = _estimate_tokens(body["messages"])
        if estimated > COMPACT_THRESHOLD:
            yield {"type": "thinking", "message": f"Compacting context (~{estimated // 1000}k tokens)..."}
            log.info("Context too large (~%d tokens), auto-compacting...", estimated)
            body["messages"] = _compact_messages(body["messages"])

        if round_num == 0:
            yield {"type": "thinking", "message": f"Calling {model_name}..."}
        else:
            yield {"type": "thinking", "message": f"Analyzing results — round {round_num + 1}/{MAX_TOOL_ROUNDS}"}

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

        # Emit thinking for tool execution
        tool_names_str = ", ".join(c["name"].replace("system__", "") for c in calls_info)
        yield {"type": "thinking", "message": f"Executing {tool_names_str}..."}

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
    yield {"type": "thinking", "message": "Preparing final response..."}
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

    if tool_suffix == "web_search":
        query = arguments.get("query", "")
        if not query:
            return "[error] No query provided"
        max_results = min(int(arguments.get("max_results", 5)), 10)
        try:
            return _web_search(query, max_results)
        except Exception as e:
            return f"[error] Web search failed: {e}"

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


def _web_search(query: str, max_results: int = 5) -> str:
    """Search DuckDuckGo HTML and extract results. Pure stdlib, no pip."""
    import html as html_mod
    import re

    encoded = urllib.request.quote(query)
    url = f"https://html.duckduckgo.com/html/?q={encoded}"
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (X11; Linux armv7l) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    })
    with urllib.request.urlopen(req, timeout=10) as resp:
        body = resp.read().decode("utf-8", errors="replace")

    results = []
    # DuckDuckGo HTML results are in <a class="result__a" ...> blocks
    # and snippets in <a class="result__snippet" ...> blocks
    links = re.findall(
        r'<a\s+rel="nofollow"\s+class="result__a"[^>]*href="([^"]*)"[^>]*>(.*?)</a>',
        body, re.DOTALL,
    )
    snippets = re.findall(
        r'<a\s+class="result__snippet"[^>]*>(.*?)</a>',
        body, re.DOTALL,
    )

    for i, (href, title_html) in enumerate(links[:max_results]):
        title = re.sub(r"<[^>]+>", "", title_html).strip()
        title = html_mod.unescape(title)
        snippet = ""
        if i < len(snippets):
            snippet = re.sub(r"<[^>]+>", "", snippets[i]).strip()
            snippet = html_mod.unescape(snippet)
        # DuckDuckGo wraps URLs in a redirect; extract the real URL
        real_url = href
        if "uddg=" in href:
            match = re.search(r"uddg=([^&]+)", href)
            if match:
                real_url = urllib.request.unquote(match.group(1))
        results.append(f"{i+1}. {title}\n   {real_url}\n   {snippet}")

    if not results:
        return f"No results found for: {query}"
    return f"Search results for: {query}\n\n" + "\n\n".join(results)


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


# ═══════════════════════════════════════════════════════════════════════════════
# Sub-Agent System — configurable personas with skills, routing & parallel exec
# ═══════════════════════════════════════════════════════════════════════════════

_DEFAULT_AGENTS = [
    {
        "id": "python-dev",
        "name": "Python Dev",
        "avatar": "\U0001f40d",
        "color": "#3776ab",
        "system_prompt": (
            "You are an expert Python developer running on ClawbotOS (Raspberry Pi, AllWinner H3). "
            "Write clean, efficient Python code. Use stdlib when possible (no pip on this device). "
            "Always execute code with your tools — never just describe what to do. Be concise."
        ),
        "skills": ["system__python", "system__bash", "system__write_file", "system__read_file"],
        "keywords": ["python", "script", "code", "function", "class", "debug", "pip", "module", "import", "def",
                     "programme", "coder", "variable", "boucle", "erreur"],
        "enabled": True,
    },
    {
        "id": "sysadmin",
        "name": "SysAdmin",
        "avatar": "\U0001f527",
        "color": "#e74c3c",
        "system_prompt": (
            "You are a Linux system administrator expert on ClawbotOS (Raspberry Pi, AllWinner H3, Armbian). "
            "You manage services, network, storage, security. Interface: end0 (Ethernet), wlx* (WiFi USB). "
            "Use `ip addr` not `ifconfig`. CPU temp: `cat /sys/class/thermal/thermal_zone0/temp` / 1000. "
            "Be concise and action-oriented."
        ),
        "skills": ["system__bash", "system__read_file", "system__write_file", "system__ssh"],
        "keywords": ["system", "service", "network", "disk", "memory", "cpu", "process", "linux", "server",
                     "ssh", "firewall", "log", "systemctl", "apt", "admin", "config", "daemon",
                     "serveur", "connecter", "connexion", "reseau", "disque", "memoire", "processus",
                     "utilisateur", "permission", "droit", "port", "ip", "adresse",
                     "fichier", "dossier", "remote", "distant", "mot de passe", "password", "root"],
        "enabled": True,
    },
    {
        "id": "web-researcher",
        "name": "Web Researcher",
        "avatar": "\U0001f310",
        "color": "#2ecc71",
        "system_prompt": (
            "You are a web research specialist. Search the internet to find accurate, up-to-date information. "
            "Summarize findings clearly with sources. Cross-reference multiple results for accuracy. "
            "Always use your web_search tool to answer questions."
        ),
        "skills": ["system__web_search", "system__bash"],
        "keywords": ["search", "find", "research", "google", "web", "internet", "look up", "information",
                     "what is", "who is", "how to", "documentation", "tutorial",
                     "chercher", "rechercher", "trouver", "internet", "c'est quoi", "qu'est-ce que",
                     "comment", "documentation", "info"],
        "enabled": True,
    },
    {
        "id": "file-manager",
        "name": "File Manager",
        "avatar": "\U0001f4c1",
        "color": "#f39c12",
        "system_prompt": (
            "You are a file management specialist on ClawbotOS. You organize, read, write, and manage files "
            "efficiently. You can create scripts, config files, and documentation. "
            "Always show file contents or confirmation after operations."
        ),
        "skills": ["system__read_file", "system__write_file", "system__bash"],
        "keywords": ["file", "folder", "directory", "create", "write", "read", "edit", "move", "copy",
                     "delete", "config", "json", "yaml", "txt", "save",
                     "fichier", "dossier", "repertoire", "creer", "ecrire", "lire", "modifier",
                     "copier", "supprimer", "sauvegarder"],
        "enabled": True,
    },
]


def _init_default_agents():
    """Create default agents if agents directory is empty."""
    os.makedirs(AGENTS_DIR, exist_ok=True)
    if any(f.endswith(".json") for f in os.listdir(AGENTS_DIR)):
        return
    for agent in _DEFAULT_AGENTS:
        with open(os.path.join(AGENTS_DIR, agent["id"] + ".json"), "w") as f:
            json.dump(agent, f, indent=2)
    log.info("Created %d default agents", len(_DEFAULT_AGENTS))


def load_agents() -> dict:
    """Load all agent configurations from AGENTS_DIR."""
    _init_default_agents()
    agents = {}
    try:
        for fname in sorted(os.listdir(AGENTS_DIR)):
            if not fname.endswith(".json"):
                continue
            try:
                with open(os.path.join(AGENTS_DIR, fname)) as f:
                    agent = json.load(f)
                agents[agent["id"]] = agent
            except Exception:
                pass
    except Exception:
        pass
    return agents


def save_agent(agent: dict) -> None:
    """Save agent config to disk."""
    os.makedirs(AGENTS_DIR, exist_ok=True)
    with open(os.path.join(AGENTS_DIR, agent["id"] + ".json"), "w") as f:
        json.dump(agent, f, indent=2)


def delete_agent(agent_id: str) -> bool:
    """Delete agent config from disk."""
    path = os.path.join(AGENTS_DIR, agent_id + ".json")
    if os.path.isfile(path):
        os.remove(path)
        return True
    return False


def _route_via_llm(user_message: str, agents: dict) -> str | None:
    """Call Haiku to classify which agent should handle the message.
    Returns agent id or None if no match / error.
    """
    enabled = {k: v for k, v in agents.items() if v.get("enabled", True)}
    if not enabled:
        return None

    # Build concise agent descriptions for the classifier
    descs = []
    for a in enabled.values():
        skills = ", ".join(a.get("skills", []))
        # Truncate system_prompt to keep the classification prompt small
        desc = (a.get("system_prompt") or "")[:200]
        descs.append(f"- id: {a['id']} | name: {a['name']} | skills: {skills} | role: {desc}")

    prompt = (
        "Tu es un routeur intelligent. Analyse le message utilisateur et choisis l'agent le plus adapté.\n\n"
        "Agents disponibles:\n" + "\n".join(descs) + "\n\n"
        f"Message utilisateur: \"{user_message}\"\n\n"
        "Réponds UNIQUEMENT avec l'id de l'agent le plus adapté (ex: sysadmin). "
        "Si aucun agent ne correspond, réponds: none"
    )

    url, api_key, _ = _load_llm_config()
    body = {
        "model": "claude-haiku-4-5-20251001",
        "stream": False,
        "max_tokens": 30,
        "messages": [{"role": "user", "content": prompt}],
    }
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    try:
        req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
        chosen = data["choices"][0]["message"]["content"].strip().lower()
        # Clean up: Haiku may add quotes or extra text
        chosen = chosen.strip('"\'`.').split()[0] if chosen else ""
        log.info("LLM router chose agent: '%s'", chosen)
        if chosen and chosen != "none" and chosen in enabled:
            return chosen
    except Exception as e:
        log.warning("LLM routing failed: %s", e)
    return None


def _route_via_keywords(user_message: str, agents: dict) -> list:
    """Fallback: match user message to agents based on keywords.
    Returns list of agent configs sorted by relevance (highest score first).
    """
    msg_lower = user_message.lower()
    scored = []
    for agent in agents.values():
        if not agent.get("enabled", True):
            continue
        score = 0
        for kw in agent.get("keywords", []):
            if kw.lower() in msg_lower:
                score += 1
        if score > 0:
            scored.append((score, agent))
    scored.sort(key=lambda x: -x[0])
    return [a for _, a in scored]


def route_to_agents(user_message: str, agents: dict = None) -> list:
    """Route user message to the best agent using LLM classification (Haiku).
    Falls back to keyword matching if LLM call fails.
    Returns list of agent configs sorted by relevance.
    """
    if agents is None:
        agents = load_agents()

    # Primary: LLM-based routing via Haiku
    chosen_id = _route_via_llm(user_message, agents)
    if chosen_id:
        return [agents[chosen_id]]

    # Fallback: keyword-based routing
    log.info("LLM routing returned no match, trying keyword fallback")
    return _route_via_keywords(user_message, agents)


def _build_agent_tools(agent_config: dict) -> list:
    """Build tool list filtered to agent's skills."""
    agent_skills = set(agent_config.get("skills", []))
    all_tools = BUILTIN_TOOLS + get_enabled_tools()
    if not agent_skills:
        return all_tools
    return [t for t in all_tools if t["function"]["name"] in agent_skills]


def chat_with_agent_stream(request_body: dict, agent_id: str):
    """
    Stream chat using a specific agent's persona and filtered tools.
    Yields same events as chat_with_tools_stream plus agent_id in each event.
    """
    agents = load_agents()
    agent = agents.get(agent_id)
    if not agent:
        yield {"type": "error", "message": f"Agent '{agent_id}' not found", "agent_id": agent_id}
        return

    tools = _build_agent_tools(agent)
    tool_names = ", ".join(t["function"]["name"] for t in tools if t.get("type") == "function")
    system_prompt = (
        f"{agent.get('system_prompt', 'You are a helpful assistant.')}\n\n"
        f"You have access to the following tools: {tool_names}. "
        "ALWAYS use your tools to complete tasks — never just describe how to do something. "
        "Execute commands, write files, and run code directly."
    )

    body = dict(request_body)
    messages = [m for m in body.get("messages", []) if m.get("role") != "system"]
    body["messages"] = [{"role": "system", "content": system_prompt}] + messages

    agent_name = agent.get("name", agent_id)
    yield {"type": "thinking", "message": f"Agent {agent_name} initializing...", "agent_id": agent_id}

    for event in chat_with_tools_stream(body, override_tools=tools):
        event["agent_id"] = agent_id
        yield event


def chat_with_multi_agents_stream(request_body: dict, agent_ids: list):
    """
    Run multiple agents in parallel. Yields events tagged with agent_id.
    First yields an agent_start event with all participating agents.
    """
    import queue
    import threading

    agents = load_agents()
    participating = []
    for aid in agent_ids:
        if aid in agents and agents[aid].get("enabled", True):
            a = agents[aid]
            participating.append({
                "id": a["id"], "name": a["name"],
                "avatar": a.get("avatar", "\U0001f916"),
                "color": a.get("color", "#00ffe0"),
            })

    if not participating:
        yield {"type": "error", "message": "No valid agents found"}
        return

    yield {"type": "agent_start", "agents": participating}

    if len(participating) == 1:
        yield from chat_with_agent_stream(request_body, participating[0]["id"])
        return

    # Parallel execution via threads
    result_queue = queue.Queue()

    def _run_agent(aid):
        try:
            for event in chat_with_agent_stream(request_body, aid):
                result_queue.put(event)
        except Exception as e:
            result_queue.put({"type": "error", "message": str(e), "agent_id": aid})
        result_queue.put({"type": "_agent_finished", "agent_id": aid})

    threads = []
    for p in participating:
        t = threading.Thread(target=_run_agent, args=(p["id"],), daemon=True)
        t.start()
        threads.append(t)

    finished_count = 0
    while finished_count < len(participating):
        try:
            event = result_queue.get(timeout=LLM_TIMEOUT + 30)
            if event.get("type") == "_agent_finished":
                finished_count += 1
                continue
            yield event
        except Exception:
            break

    yield {"type": "all_done"}
