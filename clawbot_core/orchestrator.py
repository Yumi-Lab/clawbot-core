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
CORE_PROMPTS_PATH = "/home/pi/.clawbot/core-prompts.json"

DEFAULT_SYSTEM_PROMPT = (
    "You are ClawbotOS Core, an AI assistant running on a Raspberry Pi (AllWinner H3, armhf/arm64). "
    "You have access to the following tools: {tools}. "
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


def _load_core_prompts():
    """Load editable system prompts from /home/pi/.clawbot/core-prompts.json."""
    try:
        with open(CORE_PROMPTS_PATH) as f:
            return json.load(f)
    except Exception:
        return {}
MAX_TOOL_ROUNDS = 9999
LLM_TIMEOUT = 900  # 15 min — Anthropic can take a long time for complex/long responses
TOOL_TIMEOUT = 10
TOOL_RESULT_MAX_CHARS = 6000   # truncate tool output beyond this to save tokens

# ── Per-session cancellation ────────────────────────────────────────────────
_CANCELLED_SESSIONS: set = set()

def cancel_session(session_id: str):
    """Signal a running chat_with_tools_stream to stop at next round boundary."""
    if session_id:
        _CANCELLED_SESSIONS.add(session_id)

def is_cancelled(session_id: str) -> bool:
    return bool(session_id and session_id in _CANCELLED_SESSIONS)

def clear_cancelled(session_id: str):
    _CANCELLED_SESSIONS.discard(session_id)

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
    {
        "type": "function",
        "function": {
            "name": "system__spawn_agents",
            "description": (
                "Run multiple sub-agents in parallel on independent tasks and collect their results. "
                "Use when a complex request can be decomposed into independent subtasks handled by different specialists. "
                "Each agent works autonomously — do NOT use for sequential tasks that depend on each other. "
                "Returns the combined output of all agents."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "tasks": {
                        "type": "array",
                        "description": "List of independent tasks to run in parallel",
                        "items": {
                            "type": "object",
                            "properties": {
                                "agent": {"type": "string", "description": "Agent ID to assign this task to"},
                                "task": {"type": "string", "description": "Full task description for this agent — be specific and self-contained"},
                            },
                            "required": ["agent", "task"],
                        },
                    },
                    "timeout": {"type": "integer", "description": "Max seconds to wait per agent (default 120)", "default": 120},
                },
                "required": ["tasks"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "system__handoff",
            "description": (
                "Transfer a specific sub-task to a specialist agent. Use ONLY when the task requires "
                "expertise outside your specialization. Write a COMPACT, self-contained brief — "
                "the target agent has NO access to the conversation history. "
                "Rules: (1) context = only what the target agent strictly needs to know, as short as possible. "
                "(2) task = precise actionable instruction. (3) expected_output = exact format you need back. "
                "Do NOT use for tasks you can handle yourself."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "agent_id": {"type": "string", "description": "ID of the target agent (e.g. 'julien', 'marc', 'sophie', 'thierry')"},
                    "task": {"type": "string", "description": "Precise, actionable instruction — self-contained, no assumed context"},
                    "context": {"type": "string", "description": "Minimal background the agent needs. Summarize only what is strictly necessary."},
                    "expected_output": {"type": "string", "description": "Exact format or content you need back (e.g. 'the file content as text', 'a list of IPs', 'a working Python function')"},
                },
                "required": ["agent_id", "task"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "system__schedule_task",
            "description": (
                "Schedule a task to run automatically at a specified time or recurrence. "
                "Use when the user asks to schedule, automate, plan, or set a reminder for a recurring or one-time action. "
                "Supports: once (exact datetime), daily (every day at HH:MM), weekly (day + time), "
                "hourly (every hour at :MM), interval (every N minutes)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Short human-readable name for the task"},
                    "instruction": {"type": "string", "description": "Full instruction to execute when the task runs (as if the user typed it)"},
                    "schedule_type": {
                        "type": "string",
                        "enum": ["once", "daily", "weekly", "hourly", "interval"],
                        "description": "Type of schedule",
                    },
                    "datetime": {"type": "string", "description": "ISO 8601 datetime for 'once' type (e.g. '2025-12-25T09:00:00')"},
                    "time": {"type": "string", "description": "Time HH:MM for 'daily' or 'weekly' (e.g. '09:30')"},
                    "day_of_week": {"type": "string", "description": "Day of week for 'weekly' in English or French (e.g. 'monday', 'lundi')"},
                    "minute": {"type": "integer", "description": "Minute of the hour for 'hourly' (0-59)"},
                    "interval_minutes": {"type": "integer", "description": "Interval in minutes for 'interval' type"},
                },
                "required": ["name", "instruction", "schedule_type"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "system__list_tasks",
            "description": "List all scheduled tasks with their status, next run time, and recent execution history.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "system__cancel_task",
            "description": "Cancel (delete) or pause a scheduled task by its ID.",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "string", "description": "The task ID to cancel (8-char string)"},
                    "action": {
                        "type": "string",
                        "enum": ["delete", "pause"],
                        "description": "delete = permanent removal, pause = temporary suspension. Default: delete",
                        "default": "delete",
                    },
                },
                "required": ["task_id"],
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


# ─── Anthropic direct streaming (bypasses PicoClaw to get true token streaming) ─

ANTHROPIC_API = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VER = "2023-06-01"


def _load_anthropic_config() -> tuple:
    """Return (api_key, model) if Anthropic is configured in picoclaw config, else (None, '')."""
    try:
        with open(PICOCLAW_CONFIG) as f:
            cfg = json.load(f)
        entry = cfg.get("model_list", [{}])[0]
        base = entry.get("api_base", entry.get("base_url", "")).rstrip("/")
        key = entry.get("api_key", "")
        model = entry.get("model", "")
        if "anthropic.com" in base and key:
            return key, model
    except Exception:
        pass
    return None, ""


def _to_anthropic_messages(messages: list) -> tuple:
    """Convert OpenAI-format messages to Anthropic format. Returns (system_str|None, messages)."""
    system = None
    out = []
    for msg in messages:
        role = msg.get("role", "")
        content = msg.get("content") or ""
        if role == "system":
            system = str(content)
        elif role == "user":
            out.append({"role": "user", "content": str(content)})
        elif role == "assistant":
            tcs = msg.get("tool_calls", [])
            if tcs:
                blocks = []
                if content:
                    blocks.append({"type": "text", "text": str(content)})
                for tc in tcs:
                    fn = tc.get("function", {})
                    try:
                        inp = json.loads(fn.get("arguments", "{}"))
                    except Exception:
                        inp = {}
                    blocks.append({
                        "type": "tool_use",
                        "id": tc.get("id", ""),
                        "name": fn.get("name", ""),
                        "input": inp,
                    })
                out.append({"role": "assistant", "content": blocks})
            else:
                out.append({"role": "assistant", "content": str(content)})
        elif role == "tool":
            tc_id = msg.get("tool_call_id", "")
            result = str(content)
            # Group consecutive tool results into one user message (Anthropic requirement)
            if out and out[-1]["role"] == "user" and isinstance(out[-1].get("content"), list):
                out[-1]["content"].append({
                    "type": "tool_result", "tool_use_id": tc_id, "content": result
                })
            else:
                out.append({
                    "role": "user",
                    "content": [{"type": "tool_result", "tool_use_id": tc_id, "content": result}]
                })
    return system, out


def _to_anthropic_tools(tools: list) -> list:
    """Convert OpenAI function tools to Anthropic tool format."""
    result = []
    for t in tools:
        if t.get("type") == "function":
            fn = t["function"]
            result.append({
                "name": fn.get("name", ""),
                "description": fn.get("description", ""),
                "input_schema": fn.get("parameters", {"type": "object", "properties": {}}),
            })
    return result


def _call_anthropic_stream(body: dict):
    """
    Call Anthropic API directly with streaming. body is in OpenAI format.
    Yields:
      {"type": "content_delta", "text": "..."}   — text tokens as generated
      {"type": "response", "message": {...}, "finish_reason": "..."}  — final assembled msg
      {"type": "error", "message": "..."}         — on failure
    """
    api_key, default_model = _load_anthropic_config()
    model = body.get("model") or default_model or "claude-haiku-4-5-20251001"
    max_tokens = body.get("max_tokens", 8192)
    tools = body.get("tools", [])

    system, anthro_msgs = _to_anthropic_messages(body.get("messages", []))
    anthro_tools = _to_anthropic_tools(tools)

    payload = {
        "model": model,
        "messages": anthro_msgs,
        "max_tokens": max_tokens,
        "stream": True,
    }
    if system:
        payload["system"] = system
    if anthro_tools:
        payload["tools"] = anthro_tools
        payload["tool_choice"] = {"type": "auto"}

    headers = {
        "Content-Type": "application/json",
        "x-api-key": api_key,
        "anthropic-version": ANTHROPIC_VER,
    }

    content_parts = []
    tool_blocks = {}   # index -> {id, name, input_str}
    finish_reason = "stop"

    try:
        req = urllib.request.Request(
            ANTHROPIC_API, data=json.dumps(payload).encode(), headers=headers, method="POST"
        )
        with urllib.request.urlopen(req, timeout=LLM_TIMEOUT) as resp:
            for raw_line in resp:
                line = raw_line.decode("utf-8").rstrip("\r\n")
                if not line.startswith("data: "):
                    continue
                chunk_str = line[6:]
                if chunk_str.strip() == "[DONE]":
                    break
                try:
                    ev = json.loads(chunk_str)
                except Exception:
                    continue

                ev_type = ev.get("type", "")

                if ev_type == "content_block_start":
                    idx = ev.get("index", 0)
                    block = ev.get("content_block", {})
                    if block.get("type") == "tool_use":
                        tool_blocks[idx] = {
                            "id": block.get("id", ""),
                            "name": block.get("name", ""),
                            "input_str": "",
                        }

                elif ev_type == "content_block_delta":
                    idx = ev.get("index", 0)
                    delta = ev.get("delta", {})
                    if delta.get("type") == "text_delta":
                        text = delta.get("text", "")
                        if text:
                            content_parts.append(text)
                            yield {"type": "content_delta", "text": text}
                    elif delta.get("type") == "input_json_delta":
                        if idx in tool_blocks:
                            tool_blocks[idx]["input_str"] += delta.get("partial_json", "")

                elif ev_type == "message_delta":
                    stop_reason = ev.get("delta", {}).get("stop_reason", "")
                    if stop_reason == "tool_use":
                        finish_reason = "tool_calls"
                    elif stop_reason:
                        finish_reason = stop_reason

    except urllib.error.HTTPError as e:
        body_text = e.read().decode(errors="replace")
        log.error("Anthropic API HTTP %d: %s", e.code, body_text)
        yield {"type": "error", "message": f"Anthropic API error {e.code}: {body_text[:200]}"}
        return
    except Exception as e:
        log.error("Anthropic stream failed: %s", e)
        yield {"type": "error", "message": f"Anthropic API unreachable: {e}"}
        return

    # Reconstruct OpenAI-compatible message from accumulated chunks
    content = "".join(content_parts)
    message = {"role": "assistant", "content": content or None}
    if tool_blocks:
        message["tool_calls"] = []
        for idx in sorted(tool_blocks):
            tb = tool_blocks[idx]
            try:
                inp = json.loads(tb["input_str"]) if tb["input_str"] else {}
                args_str = json.dumps(inp)
            except Exception:
                args_str = tb["input_str"]
            message["tool_calls"].append({
                "id": tb["id"],
                "type": "function",
                "function": {"name": tb["name"], "arguments": args_str},
            })
        finish_reason = "tool_calls"

    yield {"type": "response", "message": message, "finish_reason": finish_reason}


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
        with urllib.request.urlopen(req, timeout=90) as resp:
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

    # Patch spawn_agents tool: inject actual available agent IDs so LLM doesn't hallucinate names
    _available_agents = load_agents()
    if _available_agents:
        _agent_ids_desc = "Agent ID — must be one of: " + ", ".join(
            f"{aid} ({a.get('name', aid)})" for aid, a in _available_agents.items() if a.get("enabled", True)
        )
        import copy
        tools = copy.deepcopy(tools)
        for _t in tools:
            if _t.get("function", {}).get("name") == "system__spawn_agents":
                try:
                    _t["function"]["parameters"]["properties"]["tasks"]["items"]["properties"]["agent"]["description"] = _agent_ids_desc
                except (KeyError, TypeError):
                    pass

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
        _prompts = _load_core_prompts()
        _template = _prompts.get("system_prompt", DEFAULT_SYSTEM_PROMPT)
        _extra = _prompts.get("extra_rules", "").strip()
        system_content = _template.replace("{tools}", tool_names)
        if _extra:
            system_content += "\n\n" + _extra
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


def chat_with_tools_stream(request_body: dict, override_tools: list = None, session_id: str = None):
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
        _prompts = _load_core_prompts()
        _template = _prompts.get("system_prompt", DEFAULT_SYSTEM_PROMPT)
        _extra = _prompts.get("extra_rules", "").strip()
        system_content = _template.replace("{tools}", tool_names)
        if _extra:
            system_content += "\n\n" + _extra
        body["messages"] = [{"role": "system", "content": system_content}] + messages

    # Inject matched skills into system prompt
    _user_msg = next(
        (m.get("content", "") if isinstance(m.get("content"), str)
         else (m["content"][0].get("text", "") if m.get("content") else "")
         for m in reversed(body.get("messages", []))
         if m.get("role") == "user"),
        "")
    try:
        from skills import match_skills, build_skill_prompt
        _matched = match_skills(_user_msg)
        _skill_section = build_skill_prompt(_matched)
        if _skill_section:
            _msgs = body.get("messages", [])
            if _msgs and _msgs[0].get("role") == "system":
                _msgs = list(_msgs)
                _msgs[0] = dict(_msgs[0])
                _msgs[0]["content"] = _msgs[0]["content"] + "\n\n" + _skill_section
                body["messages"] = _msgs
            else:
                body["messages"] = [{"role": "system", "content": _skill_section}] + _msgs
    except Exception as _e:
        log.debug("Skills injection skipped: %s", _e)

    body["stream"] = False

    log.info("[MODEL] request_body model=%s", request_body.get("model"))

    _, _, default_model = _load_llm_config()
    if not body.get("model") or body.get("model") == "default":
        if default_model:
            body["model"] = default_model

    log.info("[MODEL] after resolve: body model=%s default=%s", body.get("model"), default_model)
    model_name = body.get("model", "Claude")

    for round_num in range(MAX_TOOL_ROUNDS):
        # Check if user cancelled this session
        if is_cancelled(session_id):
            clear_cancelled(session_id)
            log.info("Session %s cancelled by user at round %d", session_id, round_num + 1)
            yield {"type": "done", "content": "⛔ Tâche arrêtée par l'utilisateur."}
            return

        estimated = _estimate_tokens(body["messages"])
        yield {"type": "context_usage", "tokens": estimated, "max": COMPACT_THRESHOLD}
        if estimated > COMPACT_THRESHOLD:
            yield {"type": "thinking", "message": f"Compacting context (~{estimated // 1000}k tokens)..."}
            log.info("Context too large (~%d tokens), auto-compacting...", estimated)
            body["messages"] = _compact_messages(body["messages"])

        if round_num == 0:
            yield {"type": "thinking", "message": f"Calling {model_name}..."}
        else:
            yield {"type": "thinking", "message": f"Analyzing results — round {round_num + 1}/{MAX_TOOL_ROUNDS}"}

        log.info("Tool loop round %d/%d (stream)", round_num + 1, MAX_TOOL_ROUNDS)

        # Use Anthropic direct streaming when configured — gives real-time token output.
        # Falls back to PicoClaw (non-streaming, bulk response) for other providers.
        anthro_key, _ = _load_anthropic_config()
        choice = None
        finish = "stop"
        if anthro_key:
            for llm_ev in _call_anthropic_stream(body):
                if llm_ev["type"] == "content_delta":
                    yield {"type": "content_delta", "text": llm_ev["text"]}
                elif llm_ev["type"] == "response":
                    choice = {"message": llm_ev["message"], "finish_reason": llm_ev["finish_reason"]}
                    finish = llm_ev["finish_reason"]
                elif llm_ev["type"] == "error":
                    yield {"type": "error", "message": llm_ev["message"]}
                    return
            if not choice:
                yield {"type": "error", "message": "No response from Anthropic"}
                return
        else:
            # Stream from cloud (OpenAI-compatible) — real-time token output for Kimi etc.
            choice = None
            finish = "stop"
            for llm_ev in _call_picoclaw_stream(body):
                if llm_ev["type"] == "content_delta":
                    yield {"type": "content_delta", "text": llm_ev["text"]}
                elif llm_ev["type"] == "thinking_delta":
                    pass  # Kimi reasoning — accumulated in message for round-trip
                elif llm_ev["type"] == "response":
                    choice = {"message": llm_ev["message"], "finish_reason": llm_ev["finish_reason"]}
                    finish = llm_ev["finish_reason"]
                elif llm_ev["type"] == "error":
                    yield {"type": "error", "message": llm_ev["message"]}
                    return
            if not choice:
                yield {"type": "error", "message": "No response from LLM"}
                return

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

    # Safety fallback after max rounds — force a summary response
    log.warning("Reached max tool rounds (%d), forcing final summary", MAX_TOOL_ROUNDS)
    yield {"type": "thinking", "message": "Preparing final response..."}
    body.pop("tools", None)
    body.pop("tool_choice", None)
    # Inject explicit instruction to summarize rather than call more tools
    body["messages"].append({
        "role": "user",
        "content": "[System: You have reached the maximum number of tool calls. Summarize what you have done and what was accomplished. If the task is incomplete, explain what remains and why.]"
    })
    response = _call_picoclaw(body)
    content = (response.get("choices", [{}])[0].get("message", {}).get("content") or "").strip().replace("\U0001F99E", "")
    if not content:
        content = "Task completed. Maximum tool rounds reached."
    yield {"type": "done", "content": content}


def _call_picoclaw_stream(body: dict):
    """Streaming variant of _call_picoclaw. Yields events:
    {"type": "content_delta", "text": "..."} — incremental text
    {"type": "response", "message": {...}, "finish_reason": "..."} — final assembled response
    {"type": "error", "message": "..."} — error
    """
    url, api_key, default_model = _load_llm_config()
    payload = dict(body)
    if not payload.get("model") or payload.get("model") == "default":
        if default_model:
            payload["model"] = default_model
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    payload["stream"] = True
    payload["_from_tunnel"] = True

    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=LLM_TIMEOUT) as resp:
            content_acc = ""
            reasoning_acc = ""
            tool_calls_acc = {}  # index → {id, name, arguments_str}
            finish_reason = "stop"
            for raw_line in resp:
                line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
                if not line:
                    continue
                # Handle both "data: {...}" and "data:{...}" (Kimi omits space)
                if line.startswith("data:"):
                    raw_data = line[5:].strip()
                else:
                    continue
                if raw_data == "[DONE]":
                    break
                try:
                    ev = json.loads(raw_data)
                    # Detect upstream error event (e.g. Kimi 403, cloud errors)
                    if ev.get("error"):
                        err = ev["error"]
                        code = err.get("code", "")
                        msg = err.get("message", "Unknown upstream error")
                        yield {"type": "error", "message": f"API error {code}: {msg}" if code else msg}
                        return
                    choice = (ev.get("choices") or [{}])[0]
                    delta = choice.get("delta", {})
                    fr = choice.get("finish_reason")
                    if fr:
                        finish_reason = fr
                    # Reasoning content (Kimi thinking) — accumulate for round-trip
                    reasoning = delta.get("reasoning_content")
                    if reasoning:
                        reasoning_acc += reasoning
                    # Text content
                    text = delta.get("content")
                    if text:
                        content_acc += text
                        yield {"type": "content_delta", "text": text}
                    # Tool calls (streamed incrementally)
                    for tc in delta.get("tool_calls", []):
                        idx = tc.get("index", 0)
                        if idx not in tool_calls_acc:
                            tool_calls_acc[idx] = {
                                "id": tc.get("id", f"call_{idx}"),
                                "type": "function",
                                "function": {"name": "", "arguments": ""},
                            }
                        entry = tool_calls_acc[idx]
                        fn = tc.get("function", {})
                        if fn.get("name"):
                            entry["function"]["name"] = fn["name"]
                        if fn.get("arguments"):
                            entry["function"]["arguments"] += fn["arguments"]
                except Exception:
                    continue
            # Build final message
            message = {"role": "assistant", "content": content_acc or None}
            if reasoning_acc:
                message["reasoning_content"] = reasoning_acc
            if tool_calls_acc:
                message["tool_calls"] = [tool_calls_acc[i] for i in sorted(tool_calls_acc)]
            yield {"type": "response", "message": message, "finish_reason": finish_reason}
    except urllib.error.HTTPError as e:
        body_text = e.read().decode(errors="replace")
        log.error("LLM API HTTP %d: %s", e.code, body_text)
        yield {"type": "error", "message": f"LLM API error {e.code}: {body_text[:200]}"}
    except Exception as e:
        log.error("LLM API unreachable: %s", e)
        yield {"type": "error", "message": f"LLM API unreachable: {e}"}


def _call_picoclaw(body: dict) -> dict:
    """POST request_body to the configured LLM API and return parsed JSON response.

    PicoClaw proxies to Anthropic synchronously (non-streaming) and sends the full
    response in one shot. LLM_TIMEOUT is set high enough to cover any generation.
    """
    url, api_key, default_model = _load_llm_config()

    payload = dict(body)
    if not payload.get("model") or payload.get("model") == "default":
        if default_model:
            payload["model"] = default_model

    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    payload["stream"] = False
    # Prevent cloud from routing this back to the device via WebSocket tunnel.
    # Without this flag, cloud sees the device online and forwards the request back,
    # creating a second orchestrator instance that handles the tool loop silently.
    # With this flag, cloud calls Anthropic directly and returns finish_reason: "tool_calls"
    # so THIS orchestrator instance handles the tool loop and emits tool_call/tool_result SSE events.
    payload["_from_tunnel"] = True

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
            return '[error] Missing required argument "command". Call system__bash with: {"command": "your shell command here"}'
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
        if not code or not code.strip():
            return '[error] Missing required argument "code". Call system__python with: {"code": "your complete Python script here"}'
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
        content = arguments.get("content")
        if not path:
            return '[error] Missing required argument "path". Call system__write_file with: {"path": "/absolute/path/to/file", "content": "file content here"}'
        if content is None:
            return '[error] Missing required argument "content". Call system__write_file with: {"path": "' + path + '", "content": "file content here"}'
        content = str(content)
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
            return '[error] Missing required argument "path". Call system__read_file with: {"path": "/absolute/path/to/file"}'
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

    if tool_suffix == "spawn_agents":
        import threading
        tasks = arguments.get("tasks", [])
        agent_timeout = int(arguments.get("timeout", 120))
        if not tasks:
            return "[error] No tasks provided"
        agents = load_agents()
        results = {}
        errors = {}

        def _run_agent_task(agent_id: str, task_text: str):
            if agent_id not in agents:
                available = ", ".join(agents.keys()) or "none configured"
                errors[agent_id] = f"Agent '{agent_id}' not found. Valid agent IDs: {available}"
                return
            body = {"messages": [{"role": "user", "content": task_text}]}
            text_parts = []
            try:
                for event in chat_with_agent_stream(body, agent_id):
                    if event.get("type") == "text":
                        text_parts.append(event.get("text", ""))
                    elif event.get("type") == "error":
                        errors[agent_id] = event.get("message", "unknown error")
                        return
                results[agent_id] = "".join(text_parts).strip() or "(no output)"
            except Exception as e:
                errors[agent_id] = str(e)

        threads = []
        for item in tasks:
            agent_id = item.get("agent", "")
            task_text = item.get("task", "")
            if not agent_id or not task_text:
                continue
            t = threading.Thread(target=_run_agent_task, args=(agent_id, task_text), daemon=True)
            threads.append((agent_id, t))
            t.start()

        for agent_id, t in threads:
            t.join(timeout=agent_timeout)
            if t.is_alive():
                errors[agent_id] = f"Timed out after {agent_timeout}s"

        parts = []
        for item in tasks:
            aid = item.get("agent", "")
            task = item.get("task", "")
            agent_name = agents.get(aid, {}).get("name", aid) if aid in agents else aid
            if aid in errors:
                parts.append(f"### {agent_name}\n**Error:** {errors[aid]}")
            else:
                parts.append(f"### {agent_name}\n{results.get(aid, '(no response)')}")

        return "\n\n".join(parts)

    if tool_suffix == "schedule_task":
        from scheduler import create_task
        name = arguments.get("name", "Unnamed task")
        instruction = arguments.get("instruction", "")
        schedule_type = arguments.get("schedule_type", "once")
        if not instruction:
            return "[error] No instruction provided"
        kwargs = {k: v for k, v in arguments.items() if k not in ("name", "instruction", "schedule_type")}
        try:
            task = create_task(name, instruction, schedule_type, **kwargs)
            return f"Task created: '{task['name']}' (id: {task['id']}) — next run: {task.get('next_run', 'N/A')}"
        except Exception as e:
            return f"[error] {e}"

    if tool_suffix == "list_tasks":
        from scheduler import list_tasks
        try:
            tasks = list_tasks()
            if not tasks:
                return "No scheduled tasks."
            lines = []
            for t in tasks:
                status = t.get("status", "unknown")
                next_run = t.get("next_run") or "N/A"
                last_run = t.get("last_run") or "never"
                lines.append(
                    f"- [{t['id']}] {t['name']} | type: {t['schedule_type']} | "
                    f"status: {status} | next: {next_run} | last: {last_run}"
                )
            return "\n".join(lines)
        except Exception as e:
            return f"[error] {e}"

    if tool_suffix == "cancel_task":
        from scheduler import delete_task, pause_task
        task_id = arguments.get("task_id", "")
        action = arguments.get("action", "delete")
        if not task_id:
            return "[error] No task_id provided"
        try:
            if action == "pause":
                ok = pause_task(task_id)
                return f"Task {task_id} paused." if ok else f"[error] Task {task_id} not found"
            else:
                ok = delete_task(task_id)
                return f"Task {task_id} deleted." if ok else f"[error] Task {task_id} not found"
        except Exception as e:
            return f"[error] {e}"

    if tool_suffix == "handoff":
        agent_id = arguments.get("agent_id", "")
        task = arguments.get("task", "")
        context = arguments.get("context", "")
        expected_output = arguments.get("expected_output", "")
        if not agent_id or not task:
            return "[error] handoff requires agent_id and task"
        agents = load_agents()
        if agent_id not in agents:
            available = ", ".join(agents.keys())
            return f"[error] Unknown agent: '{agent_id}'. Available: {available}"
        # Build structured handoff brief
        parts = ["[HANDOFF BRIEF]"]
        if context:
            parts.append(f"Context: {context}")
        parts.append(f"Task: {task}")
        if expected_output:
            parts.append(f"Expected output: {expected_output}")
        parts.append("---\nRespond directly and concisely. No need to introduce yourself.")
        handoff_msg = "\n".join(parts)
        body = {"messages": [{"role": "user", "content": handoff_msg}], "model": "default"}
        result_parts = []
        try:
            for ev in chat_with_agent_stream(body, agent_id):
                if ev.get("type") == "done":
                    result_parts.append(ev.get("content", ""))
                elif ev.get("type") == "text":
                    result_parts.append(ev.get("text", ""))
        except Exception as e:
            return f"[error] Handoff to '{agent_id}' failed: {e}"
        agent_name = agents[agent_id].get("name", agent_id)
        result = "".join(result_parts).strip() or "(no response)"
        return f"[Handoff → {agent_name}]\n{result}"

    return f"[error] Unknown built-in tool: system__{tool_suffix}"


def _web_search(query: str, max_results: int = 5) -> str:
    """Search the web using Bing (primary) or DuckDuckGo (fallback). Pure stdlib."""
    import html as html_mod
    import re

    encoded = urllib.request.quote(query)
    ua = "Mozilla/5.0 (X11; Linux armv7l) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

    def _parse_bing(body):
        results = []
        # Bing: <h2><a href="...">title</a></h2> inside .b_algo blocks
        blocks = re.findall(r'<li class="b_algo".*?</li>', body, re.DOTALL)
        if not blocks:
            # fallback pattern
            blocks = re.findall(r'<h2>.*?<p[^>]*>.*?</p>', body, re.DOTALL)
        for block in blocks[:max_results]:
            title_m = re.search(r'<h2[^>]*>.*?<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', block, re.DOTALL)
            if not title_m:
                continue
            url = title_m.group(1)
            title = re.sub(r"<[^>]+>", "", title_m.group(2)).strip()
            title = html_mod.unescape(title)
            snip_m = re.search(r'<p[^>]*>(.*?)</p>', block, re.DOTALL)
            snippet = html_mod.unescape(re.sub(r"<[^>]+>", "", snip_m.group(1)).strip()) if snip_m else ""
            results.append(f"{len(results)+1}. {title}\n   {url}\n   {snippet}")
        return results

    def _parse_ddg(body):
        results = []
        links = re.findall(
            r'<a\s+rel="nofollow"\s+class="result__a"[^>]*href="([^"]*)"[^>]*>(.*?)</a>',
            body, re.DOTALL)
        snippets = re.findall(r'<a\s+class="result__snippet"[^>]*>(.*?)</a>', body, re.DOTALL)
        for i, (href, title_html) in enumerate(links[:max_results]):
            title = html_mod.unescape(re.sub(r"<[^>]+>", "", title_html).strip())
            snippet = html_mod.unescape(re.sub(r"<[^>]+>", "", snippets[i]).strip()) if i < len(snippets) else ""
            real_url = href
            if "uddg=" in href:
                m = re.search(r"uddg=([^&]+)", href)
                if m:
                    real_url = urllib.request.unquote(m.group(1))
            results.append(f"{i+1}. {title}\n   {real_url}\n   {snippet}")
        return results

    engine = _load_core_prompts().get("search_engine", "auto")

    providers = []
    if engine == "bing":
        providers = ["bing"]
    elif engine == "duckduckgo":
        providers = ["duckduckgo"]
    else:  # auto — try Bing first, fallback DDG
        providers = ["bing", "duckduckgo"]

    for provider in providers:
        try:
            if provider == "bing":
                req = urllib.request.Request(
                    f"https://www.bing.com/search?q={encoded}&setlang=fr",
                    headers={"User-Agent": ua, "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8"})
                with urllib.request.urlopen(req, timeout=10) as resp:
                    body = resp.read().decode("utf-8", errors="replace")
                results = _parse_bing(body)
            else:
                req = urllib.request.Request(
                    f"https://html.duckduckgo.com/html/?q={encoded}",
                    headers={"User-Agent": ua})
                with urllib.request.urlopen(req, timeout=10) as resp:
                    body = resp.read().decode("utf-8", errors="replace")
                results = _parse_ddg(body)
            if results:
                return f"Search results for: {query}\n\n" + "\n\n".join(results)
        except Exception as e:
            log.warning("%s search failed: %s", provider, e)

    return f"No results found for: {query}"


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
# ═══════════════════════════════════════════════════════════════════════════════
# Twins Partner — quality reviewer for agent responses
# ═══════════════════════════════════════════════════════════════════════════════

_TWINS_REVIEWER_PROMPT = (
    "You are a quality reviewer. You receive an original task and an agent's response. "
    "Evaluate whether the response correctly and completely fulfills the task. "
    "Reply with EXACTLY one of these two formats:\n"
    "[APPROVED] <one-line validation note>\n"
    "[NEEDS_REVISION] <specific issue 1>; <specific issue 2>; ...\n"
    "Be concise. Focus on: correctness, completeness, and whether the expected output was delivered. "
    "Do NOT redo the work yourself — only evaluate."
)


def _twins_review(original_task: str, agent_response: str, agent_name: str) -> tuple:
    """Run a Haiku reviewer pass on agent output. Returns (approved: bool, feedback: str)."""
    api_key, _ = _load_anthropic_config()
    if not api_key:
        return True, "(no reviewer — Anthropic key required for Twins Partner)"

    # Load reviewer prompt from config (editable live in UI), fallback to default constant
    reviewer_system = _TWINS_REVIEWER_PROMPT
    try:
        cfg = _load_core_prompts()
        if cfg.get("twins_reviewer_prompt", "").strip():
            reviewer_system = cfg["twins_reviewer_prompt"]
    except Exception:
        pass

    review_prompt = (
        f"Original task:\n{original_task}\n\n"
        f"Response from {agent_name}:\n{agent_response[:4000]}\n\n"
        "Evaluate and reply with [APPROVED] or [NEEDS_REVISION]."
    )
    payload = json.dumps({
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 300,
        "system": reviewer_system,
        "messages": [{"role": "user", "content": review_prompt}],
    }).encode()
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=payload,
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
        text = (data.get("content") or [{}])[0].get("text", "").strip()
        if text.startswith("[APPROVED]"):
            return True, text[len("[APPROVED]"):].strip()
        elif text.startswith("[NEEDS_REVISION]"):
            return False, text[len("[NEEDS_REVISION]"):].strip()
        # Ambiguous response — approve by default
        return True, text[:100]
    except Exception as e:
        log.warning("Twins review failed: %s", e)
        return True, "(reviewer error — approved by default)"


# Sub-Agent System — configurable personas with skills, routing & parallel exec
# ═══════════════════════════════════════════════════════════════════════════════

_DEFAULT_AGENTS = [
    {
        "id": "julien",
        "name": "Julien — Python Dev",
        "avatar": "\U0001f40d",
        "color": "#3776ab",
        "system_prompt": (
            "You are Julien, an expert Python developer running on ClawbotOS (Raspberry Pi, AllWinner H3). "
            "Write clean, efficient Python code. Use stdlib when possible (no pip on this device). "
            "Always execute code with your tools — never just describe what to do. Be concise."
        ),
        "skills": ["system__python", "system__bash", "system__write_file", "system__read_file", "system__handoff"],
        "keywords": ["python", "script", "code", "function", "class", "debug", "pip", "module", "import", "def",
                     "programme", "coder", "variable", "boucle", "erreur"],
        "enabled": True,
    },
    {
        "id": "marc",
        "name": "Marc — Sysadmin",
        "avatar": "\U0001f527",
        "color": "#e74c3c",
        "system_prompt": (
            "You are Marc, a Linux system administrator expert on ClawbotOS (Raspberry Pi, AllWinner H3, Armbian). "
            "You manage services, network, storage, security. Interface: end0 (Ethernet), wlx* (WiFi USB). "
            "Use `ip addr` not `ifconfig`. CPU temp: `cat /sys/class/thermal/thermal_zone0/temp` / 1000. "
            "Be concise and action-oriented."
        ),
        "skills": ["system__bash", "system__read_file", "system__write_file", "system__ssh", "system__handoff"],
        "keywords": ["system", "service", "network", "disk", "memory", "cpu", "process", "linux", "server",
                     "ssh", "firewall", "log", "systemctl", "apt", "admin", "config", "daemon",
                     "serveur", "connecter", "connexion", "reseau", "disque", "memoire", "processus",
                     "utilisateur", "permission", "droit", "port", "ip", "adresse",
                     "fichier", "dossier", "remote", "distant", "mot de passe", "password", "root"],
        "enabled": True,
    },
    {
        "id": "sophie",
        "name": "Sophie — Web Researcher",
        "avatar": "\U0001f310",
        "color": "#2ecc71",
        "system_prompt": (
            "You are Sophie, a web research specialist. Search the internet to find accurate, up-to-date information. "
            "Summarize findings clearly with sources. Cross-reference multiple results for accuracy. "
            "Always use your web_search tool to answer questions."
        ),
        "skills": ["system__web_search", "system__bash", "system__handoff"],
        "keywords": ["search", "find", "research", "google", "web", "internet", "look up", "information",
                     "what is", "who is", "how to", "documentation", "tutorial",
                     "chercher", "rechercher", "trouver", "internet", "c'est quoi", "qu'est-ce que",
                     "comment", "documentation", "info"],
        "enabled": True,
    },
    {
        "id": "thierry",
        "name": "Thierry — File Manager",
        "avatar": "\U0001f4c1",
        "color": "#f39c12",
        "system_prompt": (
            "You are Thierry, a file management specialist on ClawbotOS. You organize, read, write, and manage files "
            "efficiently. You can create scripts, config files, and documentation. "
            "Always show file contents or confirmation after operations."
        ),
        "skills": ["system__read_file", "system__write_file", "system__bash", "system__handoff"],
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
        "Tu es un routeur. Décide si le message nécessite un agent spécialisé ou peut être traité directement.\n\n"
        "Règle principale : réponds 'none' si :\n"
        "- la conversation est simple, générale ou conversationnelle (salutations, questions ouvertes, discussions)\n"
        "- aucune compétence spécifique d'un agent n'est clairement requise\n"
        "Ne sélectionne un agent QUE si la tâche nécessite ses compétences précises.\n\n"
        "Agents disponibles:\n" + "\n".join(descs) + "\n\n"
        f"Message: \"{user_message}\"\n\n"
        "Réponds UNIQUEMENT avec l'id de l'agent (ex: julien) ou: none"
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
    """Route user message to the best agent.
    Haiku decides whether an agent is needed at all (returns 'none' for simple messages).
    Shortcut: messages ≤ 3 words go directly to Core without calling Haiku.
    Returns list of agent configs, or [] for Core fallback.
    """
    if agents is None:
        agents = load_agents()

    msg = user_message.strip()

    # Shortcut: very short messages → Core directly, no LLM call
    if len(msg.split()) <= 3:
        log.info("Short message — Core direct (skip routing)")
        return []

    # Haiku decides: agent id or "none" (simple/conversational → Core)
    chosen_id = _route_via_llm(msg, agents)
    if chosen_id:
        log.info("Haiku routing → %s", chosen_id)
        return [agents[chosen_id]]

    log.info("Haiku chose none — Core direct")
    return []


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

    # Inject matched skills into agent system prompt
    _agent_user_msg = next(
        (m.get("content", "") if isinstance(m.get("content"), str)
         else (m["content"][0].get("text", "") if m.get("content") else "")
         for m in reversed(request_body.get("messages", []))
         if m.get("role") == "user"),
        "")
    try:
        from skills import match_skills, build_skill_prompt
        _matched = match_skills(_agent_user_msg)
        _skill_section = build_skill_prompt(_matched)
        if _skill_section:
            system_prompt += "\n\n" + _skill_section
    except Exception as _e:
        log.debug("Skills injection skipped in agent: %s", _e)

    body = dict(request_body)
    messages = [m for m in body.get("messages", []) if m.get("role") != "system"]
    body["messages"] = [{"role": "system", "content": system_prompt}] + messages

    # Inject available agents for handoff awareness
    if "system__handoff" in (agent.get("skills") or []):
        all_agents = load_agents()
        other_agents = [
            f"- {a['id']}: {a.get('name', a['id'])} ({', '.join(a.get('skills', [])[:2])})"
            for aid, a in all_agents.items() if aid != agent_id and a.get("enabled", True)
        ]
        if other_agents:
            system_prompt += (
                "\n\nAvailable agents you can delegate to via system__handoff:\n"
                + "\n".join(other_agents)
                + "\nUse system__handoff when a task falls outside your expertise."
            )

    agent_name = agent.get("name", agent_id)
    twins_enabled = agent.get("twins_partner", False)
    yield {"type": "thinking", "message": f"Agent {agent_name} initializing...", "agent_id": agent_id}

    # Stream main agent response, collect full text for optional review
    full_response = ""
    for event in chat_with_tools_stream(body, override_tools=tools):
        event["agent_id"] = agent_id
        if event.get("type") == "done":
            full_response = event.get("content", "")
        yield event

    # Twins Partner review cycle
    if twins_enabled and full_response and _agent_user_msg:
        yield {"type": "thinking", "message": "🔍 Twins Partner reviewing response...", "agent_id": agent_id}
        approved, feedback = _twins_review(_agent_user_msg, full_response, agent_name)
        if approved:
            yield {"type": "thinking", "message": f"✓ Twins Partner: {feedback or 'approved'}", "agent_id": agent_id}
        else:
            yield {"type": "thinking", "message": f"⚠ Twins Partner found issues — requesting revision...", "agent_id": agent_id}
            revision_content = (
                f"{_agent_user_msg}\n\n"
                f"[REVISION REQUEST — quality reviewer found issues]\n{feedback}\n\n"
                "Please fix the issues above and provide a corrected, complete response."
            )
            revision_body = dict(body)
            sys_msg = [m for m in body["messages"] if m.get("role") == "system"]
            revision_body["messages"] = sys_msg + [{"role": "user", "content": revision_content}]
            for event in chat_with_tools_stream(revision_body, override_tools=tools):
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
