"""
ClawbotCore — Tool Loop Orchestrator
Handles /v1/chat/completions with tool injection from installed modules.
Executes tool_calls by calling module HTTP endpoints and loops until final response.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import tempfile
import threading
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

from registry import get_enabled_tools, load_local_modules
from sandbox import SandboxManager, ToolPermission
from tool_registry import get_builtin_tools, AGENT_MEMORY_TOOLS, DISPATCH_TABLE

# ── Sandbox singleton ────────────────────────────────────────────────────────
_sandbox = SandboxManager.get_instance()

# ── Pending approvals (ASK tools wait here for user decision) ────────────────
_pending_approvals: dict = {}   # call_id → {"event": Event, "decision": str, "remember": str}
_pending_lock = threading.Lock()
_APPROVAL_TIMEOUT = 120  # seconds


def request_approval(call_id: str) -> threading.Event:
    """Register a pending approval and return an Event to wait on."""
    ev = threading.Event()
    with _pending_lock:
        _pending_approvals[call_id] = {"event": ev, "decision": None, "remember": "never"}
    return ev


def resolve_approval(call_id: str, decision: str, remember: str = "never") -> bool:
    """Resolve a pending approval from the HTTP endpoint. Returns True if found."""
    with _pending_lock:
        pending = _pending_approvals.get(call_id)
        if pending:
            pending["decision"] = decision
            pending["remember"] = remember
            pending["event"].set()
            return True
    return False


def _pop_approval(call_id: str) -> tuple:
    """Pop and return (decision, remember) after event was set."""
    with _pending_lock:
        pending = _pending_approvals.pop(call_id, None)
        if pending:
            return pending["decision"], pending["remember"]
    return "deny", "never"

# ── Storage connector sync helpers ────────────────────────────────────────────

_SYNC_PREFIXES = ["/home/pi/workshop/"]


def _local_to_remote(local_path: str):
    """Map a local path to a Drive-relative path, or None if not in sync scope."""
    for prefix in _SYNC_PREFIXES:
        if local_path.startswith(prefix):
            return local_path[len(prefix):]
    return None


def _connector_upload_bg(local_path: str):
    """Fire-and-forget upload to active storage connector. Daemon thread."""
    try:
        remote = _local_to_remote(local_path)
        if remote is None:
            return
        from connectors import get_active_connector
        conn = get_active_connector()
        if conn is None:
            return
        import threading
        threading.Thread(
            target=_do_upload, args=(conn, local_path, remote), daemon=True
        ).start()
    except Exception:
        pass  # never block tool loop


def _do_upload(conn, local_path, remote_path):
    try:
        conn.upload_file(local_path, remote_path)
        print(f"[connector] Uploaded {local_path} -> {remote_path}")
    except Exception as e:
        print(f"[connector] Upload failed {local_path}: {e}")


def _connector_pull(local_path: str):
    """Try to download file from Drive. Blocking. Returns content or None."""
    try:
        remote = _local_to_remote(local_path)
        if remote is None:
            return None
        from connectors import get_active_connector
        conn = get_active_connector()
        if conn is None:
            return None
        if not conn.file_exists(remote):
            return None
        os.makedirs(os.path.dirname(local_path), exist_ok=True)
        conn.download_file(remote, local_path)
        with open(local_path) as f:
            return f.read() or "(empty file)"
    except Exception as e:
        print(f"[connector] Pull failed {local_path}: {e}")
        return None


# ──────────────────────────────────────────────────────────────────────────────

MODULES_DIR = "/home/pi/.openjarvis/modules"
STATUS_API_URL = "http://127.0.0.1:8089"
AGENTS_DIR = "/home/pi/.openjarvis/agents"
AGENT_MEMORY_DIR = "/home/pi/.openjarvis/agent-memory"
CORE_PROMPTS_PATH = "/home/pi/.openjarvis/core-prompts.json"

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
    """Load editable system prompts from /home/pi/.openjarvis/core-prompts.json."""
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

# ── Mid-stream message injection (follow-up messages into active tool loop) ──
_inject_queues: dict[str, list] = {}   # session_id → list of user messages to inject
_inject_lock = threading.Lock()


def inject_message(session_id: str, content: str) -> bool:
    """Append a user message to the inject queue for an active session.
    Returns True if session has an active tool loop, False otherwise."""
    with _inject_lock:
        if session_id not in _inject_queues:
            return False  # no active session — caller should send normal request
        _inject_queues[session_id].append(content)
        return True


def _register_inject_session(session_id: str):
    """Register a session for mid-stream injection (called at tool loop start)."""
    if not session_id:
        return
    with _inject_lock:
        _inject_queues[session_id] = []


def _unregister_inject_session(session_id: str) -> list[str]:
    """Unregister session (called at tool loop end). Returns orphaned messages."""
    if not session_id:
        return []
    with _inject_lock:
        return _inject_queues.pop(session_id, [])


def _drain_inject_queue(session_id: str) -> list[str]:
    """Pop all pending inject messages for this session."""
    if not session_id:
        return []
    with _inject_lock:
        msgs = _inject_queues.get(session_id, [])
        if msgs:
            _inject_queues[session_id] = []
        return msgs


def mark_draining(session_id: str):
    """Mark session as draining (no new injects accepted).
    Called from main.py on BrokenPipeError before background drain."""
    if not session_id:
        return
    with _inject_lock:
        _inject_queues.pop(session_id, None)


# Web search tool definitions — extracted so _build_web_search_tools() can compose them per mode
_TOOL_WEB_SEARCH = {
    "type": "function",
    "function": {
        "name": "system__web_search",
        "description": (
            "Scraping-based web search. Fast, free, no API cost. "
            "Best for simple factual queries, news, weather, prices. "
            "Auto-selects best engine by region. Self-healing patterns."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "The search query"},
                "max_results": {"type": "integer", "description": "Maximum number of results to return (default 5, max 10)", "default": 5},
            },
            "required": ["query"],
        },
    },
}

_TOOL_WEB_SEARCH_KIMI = {
    "type": "function",
    "function": {
        "name": "system__web_search_kimi",
        "description": (
            "Kimi AI-powered search with deep reasoning. "
            "Best for complex topics, synthesis, recent events, technical research. "
            "Costs API tokens — use when scraping returns poor or no results, "
            "or when the query requires understanding and synthesis, not just links."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "The search query"},
                "max_results": {"type": "integer", "description": "Number of results (default 5)", "default": 5},
            },
            "required": ["query"],
        },
    },
}

_TOOL_WEB_SEARCH_CLAUDE = {
    "type": "function",
    "function": {
        "name": "system__web_search_claude",
        "description": (
            "Claude AI-powered search. "
            "Best for synthesis and complex queries when scraping returns poor results. "
            "Costs API tokens — use only when system__web_search is insufficient."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "The search query"},
                "max_results": {"type": "integer", "description": "Number of results (default 5)", "default": 5},
            },
            "required": ["query"],
        },
    },
}

_WEB_SEARCH_TOOL_NAMES = {"system__web_search", "system__web_search_kimi", "system__web_search_claude"}


def _build_web_search_tools(search_mode: str, user_model: str) -> list:
    """Return the subset of web search tools to inject based on mode + active model.

    Pi Only  → scraping only (Alia cannot call API tools)
    LLM First → scraping + provider-matched API tool only (no cross-provider)
    Auto      → all three tools with neutral descriptions (Alia decides)
    """
    model_lower = (user_model or "").lower()
    is_kimi = any(k in model_lower for k in ("kimi", "moonshot"))
    is_claude = any(k in model_lower for k in ("claude", "anthropic"))

    if search_mode == "pi":
        return [_TOOL_WEB_SEARCH]
    elif search_mode == "llm":
        if is_kimi:
            return [_TOOL_WEB_SEARCH, _TOOL_WEB_SEARCH_KIMI]
        elif is_claude:
            return [_TOOL_WEB_SEARCH, _TOOL_WEB_SEARCH_CLAUDE]
        else:
            return [_TOOL_WEB_SEARCH]  # provider without native search API
    else:  # "auto"
        return [_TOOL_WEB_SEARCH, _TOOL_WEB_SEARCH_KIMI, _TOOL_WEB_SEARCH_CLAUDE]


# Built-in tool schemas loaded from tool_registry.py
BUILTIN_TOOLS = get_builtin_tools()

# Context compaction — triggered when estimated input tokens exceed threshold
COMPACT_THRESHOLD = 15000   # estimated tokens (~chars/4) before compaction
COMPACT_KEEP_RECENT = 6     # number of non-system messages to keep verbatim

log = logging.getLogger(__name__)


_device_cfg_cache: dict = {"data": None, "ts": 0.0}

def _load_device_config() -> dict:
    """Read LLM config from clawbot-status-api. Cached 30s.
    Returns {provider, model, apikey, baseurl, ...}."""
    import time as _t
    now = _t.time()
    if _device_cfg_cache["data"] and now - _device_cfg_cache["ts"] < 30:
        return _device_cfg_cache["data"]
    try:
        req = urllib.request.Request(f"{STATUS_API_URL}/config")
        with urllib.request.urlopen(req, timeout=3) as r:
            data = json.loads(r.read())
        _device_cfg_cache["data"] = data
        _device_cfg_cache["ts"] = now
        return data
    except Exception as e:
        log.warning("Failed to load device config: %s", e)
        return _device_cfg_cache["data"] or {}


def _load_llm_config() -> tuple[str, str, str]:
    """Read base_url, api_key and model from device config. Returns (url, api_key, model).
    Vault llm_api_key takes priority over config file."""
    cfg = _load_device_config()
    base = cfg.get("baseurl", "").rstrip("/")
    key = cfg.get("apikey", "")
    model = cfg.get("model", "")
    # Vault override for API key
    try:
        v = _get_vault()
        if v:
            vkey = v.get("llm_api_key")
            if vkey:
                key = vkey
    except Exception:
        pass
    if base and key:
        return f"{base}/chat/completions", key, model
    return "", "", ""


def _estimate_tokens(messages: list) -> int:
    """Rough token estimate: ~4 chars per token."""
    return sum(len(str(m.get("content", ""))) for m in messages) // 4


def _sanitize_messages(messages: list) -> None:
    """In-place cleanup of messages to prevent API errors.
    - Remove assistant messages with empty content and no tool_calls
    - Ensure user messages have non-empty content
    - Ensure tool results have non-empty content
    - Ensure conversation doesn't end with assistant message (prefill guard)"""
    i = 0
    while i < len(messages):
        msg = messages[i]
        role = msg.get("role", "")
        content = msg.get("content")
        content_str = str(content).strip() if content is not None else ""

        if role == "assistant" and not msg.get("tool_calls"):
            if not content_str:
                log.warning("Sanitize: removing empty assistant message at index %d", i)
                messages.pop(i)
                continue
        elif role == "user" and not content_str:
            msg["content"] = "..."
            log.warning("Sanitize: replaced empty user message at index %d", i)
        elif role == "tool" and not content_str:
            msg["content"] = "(empty)"
            log.warning("Sanitize: replaced empty tool result at index %d", i)
        i += 1

    # Prefill guard — conversation must end with user or tool message
    if messages and messages[-1].get("role") == "assistant" and not messages[-1].get("tool_calls"):
        log.warning("Sanitize prefill guard: last message is assistant, roles=%s",
                     [m.get("role") for m in messages[-5:]])
        messages.append({"role": "user", "content": "[continue]"})


# ─── Anthropic direct streaming (true token streaming) ─────────────────────────

ANTHROPIC_API = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VER = "2023-06-01"


def _load_kimi_config() -> tuple:
    """Return (url, api_key) if Kimi is the configured provider, else (None, None).
    Vault llm_api_key takes priority."""
    cfg = _load_device_config()
    base = cfg.get("baseurl", "").rstrip("/")
    key = cfg.get("apikey", "")
    try:
        v = _get_vault()
        if v:
            vkey = v.get("llm_api_key")
            if vkey:
                key = vkey
    except Exception:
        pass
    if "kimi" in base and key:
        return f"{base}/chat/completions", key
    return None, None


def _load_anthropic_config() -> tuple:
    """Return (api_key, model) if Anthropic direct is configured, else (None, '').
    Vault llm_api_key takes priority."""
    cfg = _load_device_config()
    base = cfg.get("baseurl", "").rstrip("/")
    key = cfg.get("apikey", "")
    model = cfg.get("model", "")
    try:
        v = _get_vault()
        if v:
            vkey = v.get("llm_api_key")
            if vkey:
                key = vkey
    except Exception:
        pass
    if "anthropic.com" in base and key:
        return key, model
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
            # Anthropic rejects empty text content blocks
            text = str(content).strip() or "..."
            out.append({"role": "user", "content": text})
        elif role == "assistant":
            tcs = msg.get("tool_calls", [])
            if tcs:
                blocks = []
                if content and str(content).strip():
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
                # Anthropic rejects empty assistant content — skip empty messages
                text = str(content).strip()
                if text:
                    out.append({"role": "assistant", "content": text})
                else:
                    log.debug("Skipping empty assistant message in Anthropic conversion")
        elif role == "tool":
            tc_id = msg.get("tool_call_id", "")
            result = str(content) or "(empty)"
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

    # Guard: Anthropic API rejects conversations ending with assistant message (prefill)
    # This can happen after tool loops if messages get into unexpected state
    if anthro_msgs and anthro_msgs[-1].get("role") == "assistant":
        log.warning("Prefill guard: messages end with assistant — appending empty user turn. "
                     "Last 3 roles: %s", [m.get("role") for m in anthro_msgs[-3:]])
        anthro_msgs.append({"role": "user", "content": "[continue]"})

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
        log.error("Anthropic debug — model=%s, msg_count=%d, roles=%s",
                   model, len(anthro_msgs), [m.get("role") for m in anthro_msgs[-5:]])
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


def chat_with_tools(request_body: dict, override_tools: list = None, agent_id: str = None) -> dict:
    """
    Main orchestration loop.
    Injects available tools, calls LLM provider, executes tool_calls, loops.
    Returns final OpenAI-compatible response dict.
    override_tools: if set, use these instead of auto-discovered tools.
    """
    if override_tools is not None:
        tools = override_tools
    else:
        module_tools = get_enabled_tools()
        _search_mode = _load_core_prompts().get("search_mode", "auto")
        _current_model = request_body.get("model", "")
        _web_tools = _build_web_search_tools(_search_mode, _current_model)
        _vault_off = not _is_vault_enabled()
        _non_web_builtins = [t for t in BUILTIN_TOOLS
                             if t.get("function", {}).get("name") not in _WEB_SEARCH_TOOL_NAMES
                             and not (_vault_off and t.get("function", {}).get("name", "").startswith("vault__"))]
        tools = _web_tools + _non_web_builtins + module_tools

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

    # Allow callers to skip tool injection (e.g. wizard agent generation)
    if request_body.get("tool_choice") == "none":
        body.pop("tools", None)
        body["stream"] = False
        _, _, default_model = _load_llm_config()
        if not body.get("model") or body.get("model") == "default":
            if default_model:
                body["model"] = default_model
        response = _call_llm(body)
        return response

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
        _template = _prompts.get("system_prompt", DEFAULT_SYSTEM_PROMPT) or DEFAULT_SYSTEM_PROMPT
        _extra = _prompts.get("extra_rules", "").strip()
        system_content = _template.replace("{tools}", tool_names)
        if _extra:
            system_content += "\n\n" + _extra
        if system_content.strip():
            body["messages"] = [{"role": "system", "content": system_content}] + messages
    # Tool loop requires non-streaming internally
    body["stream"] = False

    # Use model from config if caller didn't specify one
    _, _, default_model = _load_llm_config()
    if not body.get("model") or body.get("model") == "default":
        if default_model:
            body["model"] = default_model

    for round_num in range(MAX_TOOL_ROUNDS):
        log.info("Tool loop round %d/%d", round_num + 1, MAX_TOOL_ROUNDS)
        response = _call_llm(body)

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

        # All tools allowed — sandbox disabled (to be reimplemented with UI)
        executable_tcs = list(tool_calls)

        # Execute cleared tool calls in parallel when multiple are requested
        _user_model = body.get("model")
        def _run_tc(tc):
            fn = tc.get("function", {})
            tool_name = fn.get("name", "")
            arguments_raw = fn.get("arguments", "{}")
            tool_call_id = tc.get("id", "")
            result = _execute_tool(tool_name, arguments_raw, user_model=_user_model, agent_id=agent_id)
            if len(result) > TOOL_RESULT_MAX_CHARS:
                result = result[:TOOL_RESULT_MAX_CHARS] + f"\n[...truncated {len(result) - TOOL_RESULT_MAX_CHARS} chars]"
            return tool_call_id, result

        if len(executable_tcs) > 1:
            results_map = {}
            with ThreadPoolExecutor(max_workers=min(len(executable_tcs), 4)) as ex:
                futures = {ex.submit(_run_tc, tc): tc.get("id", "") for tc in executable_tcs}
                for fut in as_completed(futures):
                    tc_id, result = fut.result()
                    results_map[tc_id] = result
            for tc in executable_tcs:
                body["messages"].append({
                    "role": "tool",
                    "tool_call_id": tc.get("id", ""),
                    "content": results_map[tc.get("id", "")],
                })
        else:
            tc_id, result = _run_tc(executable_tcs[0])
            body["messages"].append({
                "role": "tool",
                "tool_call_id": tc_id,
                "content": result,
            })

    # Safety fallback after max rounds — ask for final answer without tools
    log.warning("Reached max tool rounds (%d), forcing final response", MAX_TOOL_ROUNDS)
    body.pop("tools", None)
    body.pop("tool_choice", None)
    return _call_llm(body)


def chat_with_tools_stream(request_body: dict, override_tools: list = None, session_id: str = None, agent_id: str = None):
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
        _search_mode = _load_core_prompts().get("search_mode", "auto")
        _current_model = request_body.get("model", "")
        _web_tools = _build_web_search_tools(_search_mode, _current_model)
        _vault_off = not _is_vault_enabled()
        _non_web_builtins = [t for t in BUILTIN_TOOLS
                             if t.get("function", {}).get("name") not in _WEB_SEARCH_TOOL_NAMES
                             and not (_vault_off and t.get("function", {}).get("name", "").startswith("vault__"))]
        tools = _web_tools + _non_web_builtins + module_tools

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
        _template = _prompts.get("system_prompt", DEFAULT_SYSTEM_PROMPT) or DEFAULT_SYSTEM_PROMPT
        _extra = _prompts.get("extra_rules", "").strip()
        system_content = _template.replace("{tools}", tool_names)
        if _extra:
            system_content += "\n\n" + _extra
        # VaultProxy: inject security instruction for LLM
        system_content += (
            "\n\nSECURITY — VAULT RULES:\n"
            "1. Values like __vault_xxx__ are protected aliases. Use them as-is — the system handles substitution.\n"
            "2. When the user SHARES CREDENTIALS (e.g. 'mon accès X c'est user/password chez Y'), "
            "call vault__store to save them properly:\n"
            "   - name: descriptive key (e.g. 'ionos_email')\n"
            "   - value: the password/secret\n"
            "   - username: the login/email if provided\n"
            "   - category: 'email', 'ssh', 'api', 'llm', or 'other'\n"
            "   - note: service details (e.g. 'IONOS SMTP smtp.ionos.com')\n"
            "   Then confirm what you stored (without revealing the password).\n"
            "3. If you see a raw credential NOT explicitly shared by the user (e.g. leaked in a tool result), "
            "call vault__flag_secret immediately. Include a pattern_hint regex if the key has a "
            "recognizable prefix (e.g. 'r8_[a-zA-Z0-9]{30,}' for Replicate).\n"
            "4. NEVER reveal the real value behind an alias. NEVER echo back passwords in your response.\n"
            "5. When the user shares a TOTP/2FA secret (base32 key or otpauth:// URI), "
            "call vault__totp_add to store it securely. Ask the user for a name if not obvious. "
            "NEVER store TOTP secrets via vault__store or vault__flag_secret — always use vault__totp_add."
        )
        if system_content.strip():
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

    # ── VaultProxy: auto-detect new secrets then mask all protected values ────
    _vault = _get_vault()
    _totp_hints_all = []
    if _vault:
        _auto_names = []
        for _msg in body.get("messages", []):
            if _msg.get("role") != "user":
                continue
            _c = _msg.get("content", "")
            if isinstance(_c, str):
                _masked, _names, _totp_h = _vault.auto_protect(_c)
                _msg["content"] = _masked
                _auto_names.extend(_names)
                _totp_hints_all.extend(_totp_h)
            elif isinstance(_c, list):
                for _part in _c:
                    if isinstance(_part, dict) and _part.get("type") == "text":
                        _masked, _names, _totp_h = _vault.auto_protect(_part.get("text", ""))
                        _part["text"] = _masked
                        _auto_names.extend(_names)
                        _totp_hints_all.extend(_totp_h)
        if _auto_names:
            log.info("Auto-protected %d new values: %s", len(_auto_names), _auto_names)
            yield {"type": "vault_intercept", "secrets": _auto_names}
            # Inject hint into last user message so LLM knows what was masked
            _hint_parts = []
            for _aname in _auto_names:
                _kind = "email" if "email" in _aname else "phone" if "phone" in _aname else "value"
                _hint_parts.append(f"__vault_{_aname}__ = protected {_kind}")
            _hint = "\n[vault-context: " + ", ".join(_hint_parts) + "]"
            # Append to last user message
            for _msg in reversed(body.get("messages", [])):
                if _msg.get("role") == "user":
                    if isinstance(_msg.get("content"), str):
                        _msg["content"] += _hint
                    break
        # ── TOTP hint injection: tell LLM a TOTP secret was detected ──
        if _totp_hints_all:
            _totp_hint = (
                "\n[SYSTEM HINT: A TOTP/2FA secret was detected in the user's message. "
                "Ask the user if they want to store it as a TOTP entry for code generation. "
                "If yes, call vault__totp_add with the appropriate name and secret. "
                "Detected value(s): " +
                ", ".join(h["value"][:20] + "..." for h in _totp_hints_all) + "]"
            )
            for _msg in reversed(body.get("messages", [])):
                if _msg.get("role") == "user":
                    if isinstance(_msg.get("content"), str):
                        _msg["content"] += _totp_hint
                    break
        # Mask ALL messages (system, assistant, tool) for already-known values
        for _msg in body.get("messages", []):
            _c = _msg.get("content", "")
            if isinstance(_c, str):
                _msg["content"] = _vault.mask(_c)
            elif isinstance(_c, list):
                for _part in _c:
                    if isinstance(_part, dict) and _part.get("type") == "text":
                        _part["text"] = _vault.mask(_part.get("text", ""))

    body["stream"] = False

    log.info("[MODEL] request_body model=%s", request_body.get("model"))

    _, _, default_model = _load_llm_config()
    if not body.get("model") or body.get("model") == "default":
        if default_model:
            body["model"] = default_model

    log.info("[MODEL] after resolve: body model=%s default=%s", body.get("model"), default_model)
    model_name = body.get("model", "Claude")

    _register_inject_session(session_id)
    try:
      for round_num in range(MAX_TOOL_ROUNDS):
        # Check if user cancelled this session
        if is_cancelled(session_id):
            clear_cancelled(session_id)
            log.info("Session %s cancelled by user at round %d", session_id, round_num + 1)
            yield {"type": "done", "content": "⛔ Tâche arrêtée par l'utilisateur."}
            return

        # ── Mid-stream inject drain ──────────────────────────────────
        if round_num > 0:
            injected = _drain_inject_queue(session_id)
            for inj_msg in injected:
                body["messages"].append({"role": "user", "content": inj_msg})
                yield {"type": "user_injected", "content": inj_msg}
                log.info("Mid-stream inject: session=%s msg=%s", session_id, inj_msg[:60])

        estimated = _estimate_tokens(body["messages"])
        yield {"type": "context_usage", "tokens": estimated, "max": COMPACT_THRESHOLD}

        if round_num == 0:
            yield {"type": "thinking", "message": f"Calling {model_name}..."}
        else:
            yield {"type": "thinking", "message": f"Analyzing results — round {round_num + 1}/{MAX_TOOL_ROUNDS}"}

        log.info("Tool loop round %d/%d (stream)", round_num + 1, MAX_TOOL_ROUNDS)

        # Sanitize messages before API call — prevent empty content errors
        _sanitize_messages(body["messages"])

        # Use Anthropic direct streaming when configured — gives real-time token output.
        # Falls back to non-streaming bulk response for other providers.
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
            for llm_ev in _call_llm_stream(body):
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
            # ── Mid-stream inject: check before truly stopping ──
            injected = _drain_inject_queue(session_id)
            if injected:
                # Append assistant partial + injected user messages, loop back
                assistant_msg = choice.get("message", {})
                if assistant_msg:
                    body["messages"].append(assistant_msg)
                for inj_msg in injected:
                    body["messages"].append({"role": "user", "content": inj_msg})
                    yield {"type": "user_injected", "content": inj_msg}
                    log.info("Mid-stream inject on stop: session=%s msg=%s", session_id, inj_msg[:60])
                continue  # loop back — LLM will see the injected messages
            content = (choice.get("message", {}).get("content") or "").strip().replace("\U0001F99E", "")
            # VaultProxy: mask any leaked values in final response
            if _vault:
                content = _vault.mask(content)
            yield {"type": "done", "content": content}
            return

        tool_calls = choice.get("message", {}).get("tool_calls", [])
        if not tool_calls:
            # ── Mid-stream inject: check before truly stopping ──
            injected = _drain_inject_queue(session_id)
            if injected:
                assistant_msg = choice.get("message", {})
                if assistant_msg:
                    body["messages"].append(assistant_msg)
                for inj_msg in injected:
                    body["messages"].append({"role": "user", "content": inj_msg})
                    yield {"type": "user_injected", "content": inj_msg}
                    log.info("Mid-stream inject on stop: session=%s msg=%s", session_id, inj_msg[:60])
                continue
            content = (choice.get("message", {}).get("content") or "").strip().replace("\U0001F99E", "")
            if _vault:
                content = _vault.mask(content)
            yield {"type": "done", "content": content}
            return

        # Emit tool_call event with call details (mask vault secrets in args)
        calls_info = []
        for tc in tool_calls:
            fn = tc.get("function", {})
            try:
                args = json.loads(fn.get("arguments", "{}"))
            except Exception:
                args = {"raw": fn.get("arguments", "")}
            # Mask sensitive args for vault tools before sending to frontend
            _tc_name = fn.get("name", "")
            if _tc_name in ("vault__store", "vault__flag_secret", "vault__protect_pii") and "value" in args:
                args = dict(args)
                args["value"] = "••••••••"
            calls_info.append({
                "id": tc.get("id", ""),
                "name": _tc_name,
                "args": args,
            })
        yield {"type": "tool_call", "round": round_num + 1, "calls": calls_info}

        body["messages"].append(choice["message"])

        # All tools allowed — sandbox disabled (to be reimplemented with UI)
        executable_tcs = list(tool_calls)
        results_list = []

        # ── Execute cleared tools ─────────────────────────────────────
        if executable_tcs:
            tool_names_str = ", ".join(
                tc.get("function", {}).get("name", "").replace("system__", "")
                for tc in executable_tcs
            )
            yield {"type": "thinking", "message": f"Executing {tool_names_str}..."}

            _user_model = body.get("model")
            def _run_tc_stream(tc):
                fn = tc.get("function", {})
                tool_name = fn.get("name", "")
                arguments_raw = fn.get("arguments", "{}")
                tool_call_id = tc.get("id", "")
                # VaultProxy: unmask aliases → real values before execution
                if _vault:
                    arguments_raw = _vault.unmask(arguments_raw)
                result = _execute_tool(tool_name, arguments_raw, user_model=_user_model, agent_id=agent_id)
                if len(result) > TOOL_RESULT_MAX_CHARS:
                    result = result[:TOOL_RESULT_MAX_CHARS] + f"\n[...truncated {len(result) - TOOL_RESULT_MAX_CHARS} chars]"
                # VaultProxy: re-mask real values in tool result before LLM sees it
                if _vault:
                    result = _vault.mask(result)
                return tool_call_id, fn.get("name", ""), result

            if len(executable_tcs) > 1:
                results_map, names_map = {}, {}
                with ThreadPoolExecutor(max_workers=min(len(executable_tcs), 4)) as ex:
                    futures = {ex.submit(_run_tc_stream, tc): tc.get("id", "") for tc in executable_tcs}
                    for fut in as_completed(futures):
                        tc_id, name, result = fut.result()
                        results_map[tc_id] = result
                        names_map[tc_id] = name
                for tc in executable_tcs:
                    tc_id = tc.get("id", "")
                    results_list.append({"name": names_map.get(tc_id, ""), "result": results_map[tc_id]})
                    body["messages"].append({"role": "tool", "tool_call_id": tc_id, "content": results_map[tc_id]})
            else:
                tc_id, name, result = _run_tc_stream(executable_tcs[0])
                results_list.append({"name": name, "result": result})
                body["messages"].append({"role": "tool", "tool_call_id": tc_id, "content": result})

        # VaultProxy: deduplicate protected entries after vault__store
        if _vault and any(r.get("name") == "vault__store" for r in results_list):
            for tc in executable_tcs:
                fn = tc.get("function", {})
                if fn.get("name") == "vault__store":
                    try:
                        _args = json.loads(_vault.unmask(fn.get("arguments", "{}")))
                        _sv = _args.get("value")
                        if _sv:
                            _vault.remove_duplicate_protected(_sv)
                    except Exception:
                        pass

        # VaultProxy: retroactive mask after any vault write created new protections
        _vault_write_tools = ("vault__flag_secret", "vault__protect_pii", "vault__store")
        if _vault and any(r.get("name") in _vault_write_tools for r in results_list):
            for _msg in body.get("messages", []):
                _c = _msg.get("content", "")
                if isinstance(_c, str):
                    _msg["content"] = _vault.mask(_c)
                elif isinstance(_c, list):
                    for _part in _c:
                        if isinstance(_part, dict) and _part.get("type") == "text":
                            _part["text"] = _vault.mask(_part.get("text", ""))
            # Purge session file
            if session_id:
                try:
                    _spath = f"/home/pi/.openjarvis/sessions/{session_id}.json"
                    if os.path.isfile(_spath):
                        with open(_spath) as _f:
                            _sess = json.load(_f)
                        _dirty = False
                        for _m in _sess.get("messages", []):
                            _mc = _m.get("content", "")
                            if isinstance(_mc, str):
                                _masked_mc = _vault.mask(_mc)
                                if _masked_mc != _mc:
                                    _m["content"] = _masked_mc
                                    _dirty = True
                        if _dirty:
                            with open(_spath, "w") as _f:
                                json.dump(_sess, _f)
                except Exception:
                    pass

        # Mask vault__get results in SSE output (frontend sees "••••••••", LLM sees real value)
        sse_results = []
        for r in results_list:
            if r.get("name") == "vault__get" and r.get("result") and not r["result"].startswith("[error]") and "not found" not in r["result"]:
                sse_results.append({"name": r["name"], "result": "••••••••"})
            else:
                sse_results.append(r)
        yield {"type": "tool_result", "round": round_num + 1, "results": sse_results}

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
      response = _call_llm(body)
      content = (response.get("choices", [{}])[0].get("message", {}).get("content") or "").strip().replace("\U0001F99E", "")
      if not content:
          content = "Task completed. Maximum tool rounds reached."
      if _vault:
          content = _vault.mask(content)
      yield {"type": "done", "content": content}
    finally:
      orphans = _unregister_inject_session(session_id)
      if orphans:
          log.warning("Orphaned inject messages for %s: %s", session_id, [m[:60] for m in orphans])


def _call_llm_stream(body: dict):
    """Streaming variant of _call_llm. Yields events:
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
                        log.error("LLM SSE error event: code=%s msg=%s roles=%s",
                                  code, msg[:200],
                                  [m.get("role") for m in body.get("messages", [])[-5:]])
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


def _call_llm(body: dict) -> dict:
    """POST request_body to the configured LLM API and return parsed JSON response."""
    url, api_key, default_model = _load_llm_config()

    payload = dict(body)
    if not payload.get("model") or payload.get("model") == "default":
        if default_model:
            payload["model"] = default_model

    headers = {"Content-Type": "application/json"}

    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    payload["stream"] = False
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



# Dangerous command blocking removed — to be reimplemented with user-facing UI


# ── Built-in sub-handlers ──────────────────────────────────────────────────────
# Each _builtin_* function handles one system__ tool suffix.
# Uniform signature: (arguments: dict, user_model: str = None) -> str


def _builtin_bash(arguments: dict, user_model: str = None) -> str:
    cmd = arguments.get("command", "")
    if not cmd:
        return '[error] Missing required argument "command". Call system__bash with: {"command": "your shell command here"}'
    timeout = int(arguments.get("timeout", 30))
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


def _builtin_python(arguments: dict, user_model: str = None) -> str:
    code = arguments.get("code", "")
    if not code or not code.strip():
        return '[error] Missing required argument "code". Call system__python with: {"code": "your complete Python script here"}'
    timeout = int(arguments.get("timeout", 30))
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


def _builtin_write_file(arguments: dict, user_model: str = None) -> str:
    path = arguments.get("path", "")
    file_content = arguments.get("content")
    if not path:
        return '[error] Missing required argument "path". Call system__write_file with: {"path": "/absolute/path/to/file", "content": "file content here"}'
    if file_content is None:
        return '[error] Missing required argument "content". Call system__write_file with: {"path": "' + path + '", "content": "file content here"}'
    file_content = str(file_content)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(file_content)
        _connector_upload_bg(path)
        return f"Written {len(file_content)} bytes to {path}"
    except Exception as e:
        return f"[error] {e}"


def _builtin_read_file(arguments: dict, user_model: str = None) -> str:
    path = arguments.get("path", "")
    if not path:
        return '[error] Missing required argument "path". Call system__read_file with: {"path": "/absolute/path/to/file"}'
    try:
        with open(path) as f:
            file_content = f.read()
        return file_content or "(empty file)"
    except FileNotFoundError:
        pulled = _connector_pull(path)
        if pulled is not None:
            return pulled
        return f"[error] File not found: {path}"
    except Exception as e:
        return f"[error] {e}"


def _builtin_web_search_kimi(arguments: dict, user_model: str = None) -> str:
    query = arguments.get("query", "")
    if not query:
        return "[error] No query provided"
    max_results = min(int(arguments.get("max_results", 5)), 10)
    try:
        return _web_search_kimi(query, max_results)
    except Exception as e:
        return f"[error] web_search_kimi: {e}"


def _builtin_web_search_claude(arguments: dict, user_model: str = None) -> str:
    query = arguments.get("query", "")
    if not query:
        return "[error] No query provided"
    max_results = min(int(arguments.get("max_results", 5)), 10)
    try:
        return _web_search_claude(query, max_results)
    except Exception as e:
        return f"[error] web_search_claude: {e}"


def _builtin_web_search(arguments: dict, user_model: str = None) -> str:
    query = arguments.get("query", "")
    if not query:
        return "[error] No query provided"
    max_results = min(int(arguments.get("max_results", 5)), 10)
    try:
        return _web_search(query, max_results, user_model=user_model)
    except Exception as e:
        return f"[error] Web search failed: {e}"


def _builtin_ssh(arguments: dict, user_model: str = None) -> str:
    import shutil
    host = arguments.get("host", "")
    user = arguments.get("user", "")
    password = arguments.get("password", "")
    command = arguments.get("command", "")
    port = int(arguments.get("port", 22))
    timeout = int(arguments.get("timeout", 30))
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


def _builtin_spawn_agents(arguments: dict, user_model: str = None) -> str:
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


def _builtin_schedule_task(arguments: dict, user_model: str = None) -> str:
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


def _builtin_list_tasks(arguments: dict, user_model: str = None) -> str:
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


def _builtin_cancel_task(arguments: dict, user_model: str = None) -> str:
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


def _builtin_handoff(arguments: dict, user_model: str = None) -> str:
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


def _builtin_get_system_info(arguments: dict, user_model: str = None) -> str:
    try:
        import socket
        lines = []
        # Hostname
        lines.append(f"hostname: {socket.gethostname()}")
        # CPU usage
        try:
            with open("/proc/stat") as f:
                cpu = f.readline().split()
            idle = int(cpu[4])
            total = sum(int(x) for x in cpu[1:])
            lines.append(f"cpu_usage: {round(100 * (1 - idle / total), 1)}%")
        except Exception:
            pass
        # RAM
        try:
            with open("/proc/meminfo") as f:
                mem = {k.strip(): v.strip() for k, v in (l.split(":", 1) for l in f if ":" in l)}
            total_kb = int(mem.get("MemTotal", "0 kB").split()[0])
            avail_kb = int(mem.get("MemAvailable", "0 kB").split()[0])
            used_kb = total_kb - avail_kb
            lines.append(f"ram_total: {total_kb // 1024} MB")
            lines.append(f"ram_used: {used_kb // 1024} MB ({round(100 * used_kb / total_kb, 1)}%)")
        except Exception:
            pass
        # Temperature
        try:
            with open("/sys/class/thermal/thermal_zone0/temp") as f:
                lines.append(f"cpu_temp: {int(f.read().strip()) / 1000:.1f}°C")
        except Exception:
            pass
        # Disk
        try:
            r = subprocess.run(["df", "-h", "/"], capture_output=True, text=True, timeout=5)
            parts = r.stdout.strip().splitlines()
            if len(parts) > 1:
                fields = parts[1].split()
                lines.append(f"disk_total: {fields[1]}, disk_used: {fields[2]} ({fields[4]})")
        except Exception:
            pass
        # IPs
        try:
            r = subprocess.run(["ip", "-brief", "addr"], capture_output=True, text=True, timeout=5)
            for line in r.stdout.strip().splitlines():
                parts = line.split()
                if len(parts) >= 3 and parts[0] != "lo" and parts[2]:
                    lines.append(f"ip_{parts[0]}: {parts[2]}")
        except Exception:
            pass
        return "\n".join(lines)
    except Exception as e:
        return f"[error] {e}"


def _builtin_ssh_execute(arguments: dict, user_model: str = None) -> str:
    # Alias of system__ssh for new naming
    return _builtin_ssh(arguments, user_model=user_model)


def _builtin_disk(arguments: dict, user_model: str = None) -> str:
    path = arguments.get("path", "/")
    try:
        result = subprocess.run(
            ["df", "-h", path], capture_output=True, text=True, timeout=10,
            env={**os.environ, "HOME": "/home/pi"},
        )
        return result.stdout.strip() or result.stderr.strip() or "(no output)"
    except Exception as e:
        return f"[error] {e}"


def _builtin_search_engines_list(arguments: dict, user_model: str = None) -> str:
    engines = _load_search_engines()
    lines = []
    for name, cfg in sorted(engines.items(), key=lambda x: -x[1].get("reliability", 0.5)):
        lines.append(f"- {name}: reliability={cfg.get('reliability',0.5):.2f} regions={cfg.get('regions',[])} lang={cfg.get('language','any')}")
    return "Search engines pool:\n" + "\n".join(lines)


def _builtin_search_engine_add(arguments: dict, user_model: str = None) -> str:
    name = arguments.get("name", "").strip().lower()
    url = arguments.get("url", "")
    regions = arguments.get("regions", ["OTHER"])
    language = arguments.get("language", "any")
    headers = arguments.get("headers", {})
    patterns = arguments.get("patterns", {})
    if not name or not url:
        return "[error] name and url are required"
    engines = _load_search_engines()
    engines[name] = {
        "url": url,
        "headers": headers or {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/122.0.0.0 Safari/537.36"
        },
        "regions": regions,
        "language": language,
        "timeout": 10,
        "reliability": 0.5,
        "patterns": patterns,
    }
    _save_search_engines(engines)
    return f"Engine '{name}' added to pool. Use system__search_engine_test to validate."


def _builtin_search_engine_test(arguments: dict, user_model: str = None) -> str:
    name = arguments.get("name", "")
    query = arguments.get("query", "test search engine")
    engines = _load_search_engines()
    if name not in engines:
        return f"[error] Engine '{name}' not found. Use system__search_engines_list."
    cfg = engines[name]
    try:
        html_body = _fetch_search_html(cfg, query)
        results = _parse_results(html_body, cfg.get("patterns", {}), 3)
        if results:
            cfg["reliability"] = min(1.0, cfg.get("reliability", 0.5) * 0.9 + 0.1)
            engines[name] = cfg
            _save_search_engines(engines)
            return f"\u2713 Engine '{name}' working \u2014 {len(results)} results.\n\n" + "\n".join(results[:2])
        # Try to adapt — use user_model if available
        new_patterns = _adapt_engine_patterns(name, cfg, html_body, user_model=user_model)
        if new_patterns:
            cfg["patterns"] = new_patterns
            results = _parse_results(html_body, new_patterns, 3)
            if results:
                cfg["reliability"] = 0.6
                engines[name] = cfg
                _save_search_engines(engines)
                return f"\u2713 Engine '{name}' fixed by AI \u2014 {len(results)} results.\n\n" + "\n".join(results[:2])
        cfg["reliability"] = max(0.1, cfg.get("reliability", 0.5) * 0.7)
        engines[name] = cfg
        _save_search_engines(engines)
        return f"\u2717 Engine '{name}' returns 0 results. Patterns may need manual review."
    except Exception as e:
        cfg["reliability"] = max(0.1, cfg.get("reliability", 0.5) * 0.7)
        engines[name] = cfg
        _save_search_engines(engines)
        return f"\u2717 Engine '{name}' failed: {e}"


# ── Built-in sub-dispatch table ───────────────────────────────────────────────

_BUILTIN_SUB_DISPATCH = {
    "bash":                _builtin_bash,
    "python":              _builtin_python,
    "write_file":          _builtin_write_file,
    "read_file":           _builtin_read_file,
    "web_search_kimi":     _builtin_web_search_kimi,
    "web_search_claude":   _builtin_web_search_claude,
    "web_search":          _builtin_web_search,
    "ssh":                 _builtin_ssh,
    "spawn_agents":        _builtin_spawn_agents,
    "schedule_task":       _builtin_schedule_task,
    "list_tasks":          _builtin_list_tasks,
    "cancel_task":         _builtin_cancel_task,
    "handoff":             _builtin_handoff,
    "get_system_info":     _builtin_get_system_info,
    "ssh_execute":         _builtin_ssh_execute,
    "disk":                _builtin_disk,
    "search_engines_list": _builtin_search_engines_list,
    "search_engine_add":   _builtin_search_engine_add,
    "search_engine_test":  _builtin_search_engine_test,
}


def _execute_builtin(tool_suffix: str, arguments: dict, user_model: str = None) -> str:
    """Execute a built-in system tool directly (no HTTP call needed).
    Dispatches to _builtin_* handlers via _BUILTIN_SUB_DISPATCH table.
    user_model: model name selected by user — propagated to web_search."""
    handler = _BUILTIN_SUB_DISPATCH.get(tool_suffix)
    if handler:
        return handler(arguments, user_model=user_model)
    return f"[error] Unknown built-in tool: system__{tool_suffix}"

_region_cache = {"code": None, "ts": 0}

def _detect_region() -> str:
    """Detect current country code via IP geolocation. Cached 1h. Returns 'CN' or 'OTHER'."""
    import time
    now = time.time()
    if _region_cache["code"] and now - _region_cache["ts"] < 3600:
        return _region_cache["code"]
    try:
        req = urllib.request.Request(
            "http://ip-api.com/json/?fields=countryCode",
            headers={"User-Agent": "ClawbotCore/1.0"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode())
            code = data.get("countryCode", "OTHER")
    except Exception:
        code = _region_cache["code"] or "OTHER"
    _region_cache["code"] = code
    _region_cache["ts"] = now
    log.info("Detected region: %s", code)
    return code


SEARCH_ENGINES_PATH = "/home/pi/.openjarvis/search-engines.json"

# Rotating UA pool — realistic Chrome/Firefox on Windows/macOS/Android
# Each entry: (user-agent, Sec-Ch-Ua, Sec-Ch-Ua-Platform)
_UA_POOL = [
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        '"Chromium";v="122", "Not(A:Brand";v="24", "Google Chrome";v="122"',
        '"Windows"',
    ),
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        '"Chromium";v="120", "Not(A:Brand";v="24", "Google Chrome";v="120"',
        '"Windows"',
    ),
    (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        '"Chromium";v="122", "Not(A:Brand";v="24", "Google Chrome";v="122"',
        '"macOS"',
    ),
    (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_3_1) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.3.1 Safari/605.1.15",
        '"Not A(Brand";v="99", "Safari";v="17"',
        '"macOS"',
    ),
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:123.0) Gecko/20100101 Firefox/123.0",
        '"Not A(Brand";v="99", "Firefox";v="123"',
        '"Windows"',
    ),
    (
        "Mozilla/5.0 (X11; Linux x86_64; rv:122.0) Gecko/20100101 Firefox/122.0",
        '"Not A(Brand";v="99", "Firefox";v="122"',
        '"Linux"',
    ),
    (
        "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.6261.64 Mobile Safari/537.36",
        '"Chromium";v="122", "Not(A:Brand";v="24", "Google Chrome";v="122"',
        '"Android"',
    ),
    (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_3_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.3.1 Mobile/15E148 Safari/604.1",
        '"Not A(Brand";v="99"',
        '"iOS"',
    ),
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36 Edg/121.0.0.0",
        '"Chromium";v="121", "Not A(Brand";v="99", "Microsoft Edge";v="121"',
        '"Windows"',
    ),
    (
        "Mozilla/5.0 (Linux; Android 13; Samsung Galaxy S23) AppleWebKit/537.36 (KHTML, like Gecko) SamsungBrowser/23.0 Chrome/115.0.0.0 Mobile Safari/537.36",
        '"Chromium";v="115", "Not(A:Brand";v="99", "Samsung Internet";v="23"',
        '"Android"',
    ),
]

_DEFAULT_SEARCH_ENGINES = {
    "duckduckgo": {
        "url": "https://html.duckduckgo.com/html/?q={query}",
        "headers": {"Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8"},
        "regions": ["OTHER"], "language": "any", "timeout": 10, "reliability": 0.9,
        "patterns": {"type": "ddg",
            "links": r'<a\s+rel="nofollow"\s+class="result__a"[^>]*href="([^"]*)"[^>]*>(.*?)</a>',
            "snippets": r'<a\s+class="result__snippet"[^>]*>(.*?)</a>'},
    },
    "bing": {
        "url": "https://www.bing.com/search?q={query}&setlang=fr&count=10",
        "headers": {"Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8"},
        "regions": ["OTHER"], "language": "any", "timeout": 10, "reliability": 0.8,
        "patterns": {"type": "block",
            "blocks": r'<li class="b_algo".*?</li>',
            "title_url": r'<h2[^>]*>.*?<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
            "snippet": r'<p[^>]*>(.*?)</p>'},
    },
    "bing_cn": {
        "url": "https://cn.bing.com/search?q={query}&count=10",
        "headers": {"Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"},
        "regions": ["CN"], "language": "any", "timeout": 10, "reliability": 0.6,
        "patterns": {"type": "block",
            "blocks": r'<li class="b_algo".*?</li>',
            "title_url": r'<h2[^>]*>.*?<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
            "snippet": r'<p[^>]*>(.*?)</p>'},
    },
    "baidu": {
        "url": "https://www.baidu.com/s?wd={query}&rn=10",
        "headers": {"Accept-Language": "zh-CN,zh;q=0.9"},
        "regions": ["CN"], "language": "zh", "timeout": 10, "reliability": 0.5,
        "patterns": {"type": "block",
            "blocks": r'<div[^>]+class="[^"]*c-container[^"]*"[^>]*>.*?</div>\s*</div>',
            "title_url": r'<h3[^>]*>.*?<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
            "snippet": r'class="[^"]*c-abstract[^"]*"[^>]*>(.*?)</div>'},
    },
    "sogou": {
        "url": "https://www.sogou.com/web?query={query}&num=10",
        "headers": {"Accept-Language": "zh-CN,zh;q=0.9"},
        "regions": ["CN"], "language": "zh", "timeout": 10, "reliability": 0.5,
        "patterns": {"type": "block",
            "blocks": r'<div[^>]+class="[^"]*vrwrap[^"]*"[^>]*>.*?</div>',
            "title_url": r'<h3[^>]*>.*?<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
            "snippet": r'<p[^>]*class="[^"]*star-wiki[^"]*"[^>]*>(.*?)</p>'},
    },
    "google": {
        "url": "https://www.google.com/search?q={query}&num=10&hl=fr",
        "headers": {"Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8"},
        "regions": ["OTHER"], "language": "any", "timeout": 10, "reliability": 0.7,
        "patterns": {"type": "block",
            "blocks": r'<div[^>]+class="[^"]*tF2Cxc[^"]*"[^>]*>.*?</div>\s*</div>',
            "title_url": r'<a[^>]+href="([^"]+)"[^>]*>.*?<h3[^>]*>(.*?)</h3>',
            "snippet": r'<div[^>]+class="[^"]*VwiC3b[^"]*"[^>]*>(.*?)</div>'},
    },
    "brave": {
        "url": "https://api.search.brave.com/res/v1/web/search?q={query}&count=10",
        "headers": {"Accept": "application/json", "Accept-Encoding": "gzip"},
        "regions": ["*"], "language": "any", "timeout": 10, "reliability": 0.95,
        "patterns": {"type": "brave_json"},
        "requires_key": "brave_api_key",
    },
}

def _load_search_engines() -> dict:
    try:
        with open(SEARCH_ENGINES_PATH) as f:
            data = json.load(f)
        engines = {k: v.copy() for k, v in _DEFAULT_SEARCH_ENGINES.items()}
        engines.update(data.get("engines", {}))
        return engines
    except Exception:
        return {k: v.copy() for k, v in _DEFAULT_SEARCH_ENGINES.items()}

def _save_search_engines(engines: dict):
    try:
        os.makedirs(os.path.dirname(SEARCH_ENGINES_PATH), exist_ok=True)
        with open(SEARCH_ENGINES_PATH, "w") as f:
            json.dump({"version": 1, "engines": engines}, f, indent=2)
    except Exception as e:
        log.warning("Failed to save search engines: %s", e)

def _parse_results(body: str, patterns: dict, max_results: int) -> list:
    import html as html_mod, re as _re
    results = []
    ptype = patterns.get("type", "block")
    if ptype == "brave_json":
        try:
            data = json.loads(body)
            for i, item in enumerate(data.get("web", {}).get("results", [])[:max_results]):
                title = item.get("title", "")
                url = item.get("url", "")
                snippet = item.get("description", "")
                if title and url:
                    results.append(f"{i+1}. {title}\n   {url}\n   {snippet}")
        except Exception as e:
            log.warning("Brave JSON parse error: %s", e)
        return results
    try:
        if ptype == "ddg":
            links = _re.findall(patterns.get("links", ""), body, _re.DOTALL)
            snippets = _re.findall(patterns.get("snippets", ""), body, _re.DOTALL)
            for i, (href, title_html) in enumerate(links[:max_results]):
                title = html_mod.unescape(_re.sub(r"<[^>]+>", "", title_html).strip())
                snippet = html_mod.unescape(_re.sub(r"<[^>]+>", "", snippets[i]).strip()) if i < len(snippets) else ""
                real_url = href
                if "uddg=" in href:
                    m = _re.search(r"uddg=([^&]+)", href)
                    if m: real_url = urllib.request.unquote(m.group(1))
                if title:
                    results.append(f"{len(results)+1}. {title}\n   {real_url}\n   {snippet}")
        else:
            blocks = _re.findall(patterns.get("blocks", ""), body, _re.DOTALL)
            for block in blocks[:max_results]:
                tu_m = _re.search(patterns.get("title_url", ""), block, _re.DOTALL)
                if not tu_m: continue
                url = tu_m.group(1)
                title = html_mod.unescape(_re.sub(r"<[^>]+>", "", tu_m.group(2)).strip())
                snip_m = _re.search(patterns.get("snippet", ""), block, _re.DOTALL)
                snippet = html_mod.unescape(_re.sub(r"<[^>]+>", "", snip_m.group(1)).strip()) if snip_m else ""
                if title and url:
                    results.append(f"{len(results)+1}. {title}\n   {url}\n   {snippet}")
    except Exception as e:
        log.warning("Parse error (%s): %s", ptype, e)
    return results

def _fetch_search_html(engine_cfg: dict, query: str) -> str:
    import gzip as _gzip, zlib as _zlib, random as _random
    url = engine_cfg.get("url", "").replace("{query}", urllib.request.quote(query))
    timeout = engine_cfg.get("timeout", 10)

    # If engine requires an API key, load it and inject — skip if missing
    key_config_field = engine_cfg.get("requires_key")
    if key_config_field:
        api_key = _load_core_prompts().get(key_config_field, "").strip()
        if not api_key:
            raise ValueError(f"API key '{key_config_field}' not configured — skipping {engine_cfg.get('url','')}")
        headers = dict(engine_cfg.get("headers", {}))
        headers["X-Subscription-Token"] = api_key
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            enc = resp.getheader("Content-Encoding", "")
            if enc == "gzip":
                return _gzip.decompress(raw).decode("utf-8", errors="replace")
            return raw.decode("utf-8", errors="replace")

    # Standard scraping — full Chrome-like headers + rotating UA
    ua, sec_ch_ua, platform = _random.choice(_UA_POOL)
    is_mobile = "Mobile" in ua or "iPhone" in ua
    headers = {
        "User-Agent": ua,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
        "Accept-Language": "fr-FR,fr;q=0.9,en-US;q=0.8,en;q=0.7",
        "Accept-Encoding": "gzip, deflate",
        "Cache-Control": "max-age=0",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "Sec-Ch-Ua": sec_ch_ua,
        "Sec-Ch-Ua-Mobile": "?1" if is_mobile else "?0",
        "Sec-Ch-Ua-Platform": platform,
    }
    # Engine-specific overrides (Accept-Language etc.)
    headers.update(engine_cfg.get("headers", {}))

    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        enc = resp.getheader("Content-Encoding", "")
        if enc == "gzip":
            return _gzip.decompress(raw).decode("utf-8", errors="replace")
        if enc == "deflate":
            return _zlib.decompress(raw).decode("utf-8", errors="replace")
        return raw.decode("utf-8", errors="replace")

def _resolve_model_config(model_name: str = None) -> tuple:
    """Resolve (api_base, api_key, model) for a given model name.
    Uses device config as base; overrides model if specified."""
    cfg = _load_device_config()
    base = cfg.get("baseurl", "").rstrip("/")
    key = cfg.get("apikey", "")
    model = model_name or cfg.get("model", "")
    return (base, key, model)


def _adapt_engine_patterns(engine_name: str, engine_cfg: dict, html_sample: str,
                            user_model: str = None,
                            model_override: str = None, base_override: str = None, key_override: str = None):
    """Call configured LLM to generate new scraping patterns from raw HTML.
    user_model: model name selected by user — resolved via _resolve_model_config.
    model/base/key_override: explicit overrides (legacy, used by repair endpoint)."""
    import re as _re
    try:
        if model_override or base_override or key_override:
            # Legacy path: explicit overrides from repair endpoint
            base_r, key_r, model_r = _resolve_model_config(model_override)
            base = (base_override or base_r).rstrip("/")
            api_key = key_override or key_r
            model = model_override or model_r
        elif user_model:
            base, api_key, model = _resolve_model_config(user_model)
        else:
            base, api_key, model = _resolve_model_config()
        if not base or not api_key:
            log.warning("[ADAPT] No API config found for model=%s", user_model or model_override or "default")
            return None
        log.info("[ADAPT] engine=%s | model=%s | api_base=%s", engine_name, model, base[:40])
    except Exception as e:
        log.warning("[ADAPT] Config resolution failed: %s", e)
        return None
    prompt = f"""You are a web scraping expert. The search engine "{engine_name}" changed its HTML and our scraper returns 0 results.

Current failing patterns: {json.dumps(engine_cfg.get('patterns', {}), indent=2)}

HTML sample (first 5000 chars):
{html_sample[:5000]}

Return ONLY a valid JSON object (no markdown, no explanation):
{{
  "type": "block",
  "blocks": "regex to find result containers (DOTALL mode)",
  "title_url": "regex with group(1)=url group(2)=title",
  "snippet": "regex with group(1)=description text"
}}
Or for DuckDuckGo-style pages:
{{
  "type": "ddg",
  "links": "regex with group(1)=url group(2)=title",
  "snippets": "regex with group(1)=description"
}}"""
    body_data = json.dumps({"model": model, "stream": False, "max_tokens": 800,
        "messages": [{"role": "user", "content": prompt}]}).encode()
    req = urllib.request.Request(f"{base}/chat/completions", data=body_data,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read())
        text = result["choices"][0]["message"]["content"].strip()
        text = _re.sub(r'^```[a-z]*\n?', '', text, flags=_re.MULTILINE).replace('```', '').strip()
        new_patterns = json.loads(text)
        log.info("[ADAPT] result: patterns=%s | success=True", list(new_patterns.keys()))
        return new_patterns
    except Exception as e:
        log.warning("[ADAPT] FAILED: %s", e)
        return None


def _translate_query_for_engine(query: str, target_lang: str, user_model: str = None) -> str:
    """Translate a search query to match the target engine's language.
    Returns translated query or original if translation fails/unnecessary."""
    import re as _re
    has_cjk = bool(_re.search(r'[\u4e00-\u9fff\u3400-\u4dbf\uff00-\uffef]', query))
    query_lang = "zh" if has_cjk else "non-zh"

    # No translation needed if languages match
    if target_lang == "any":
        log.info("[TRANSLATE] SKIPPED: engine accepts any language")
        return query
    if target_lang == "zh" and query_lang == "zh":
        log.info("[TRANSLATE] SKIPPED: query already in Chinese")
        return query
    if target_lang != "zh" and query_lang != "zh":
        log.info("[TRANSLATE] SKIPPED: query already in target language")
        return query

    # Need translation
    lang_name = "Chinese" if target_lang == "zh" else "English"
    base, api_key, model = _resolve_model_config(user_model)
    if not base or not api_key:
        log.warning("[TRANSLATE] No API config — using original query")
        return query

    log.info("[TRANSLATE] query=\"%s\" | from=%s | to=%s | model=%s", query[:60], query_lang, target_lang, model)
    prompt = f"Translate this search query to {lang_name}. Return ONLY the translated query, nothing else: {query}"
    body_data = json.dumps({"model": model, "stream": False, "max_tokens": 200,
        "messages": [{"role": "user", "content": prompt}]}).encode()
    req = urllib.request.Request(f"{base}/chat/completions", data=body_data,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read())
        translated = result["choices"][0]["message"]["content"].strip().strip('"\'')
        log.info("[TRANSLATE] result: \"%s\"", translated[:80])
        return translated
    except Exception as e:
        log.warning("[TRANSLATE] FAILED: %s | using original query", e)
        return query


def _web_search_kimi(query: str, max_results: int = 5) -> str:
    """Search via Kimi $web_search — routes through cloud proxy (Pi doesn't hold Moonshot key)."""
    import socket as _socket
    _orig = _socket.getaddrinfo
    def _ipv4(h, p, f=0, *a, **kw): return _orig(h, p, _socket.AF_INET, *a, **kw)
    _socket.getaddrinfo = _ipv4
    try:
        dcfg = _load_device_config()
        base = dcfg.get("baseurl", "").rstrip("/")
        api_key = dcfg.get("apikey", "")
        if not base or not api_key:
            return "[web_search_kimi] No API config found."
        # Call cloud /v1/kimi-web-search — cloud holds the Moonshot key
        body = json.dumps({"query": query, "max_results": max_results}).encode()
        req = urllib.request.Request(
            f"{base}/kimi-web-search",
            data=body,
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=35) as r:
            data = json.loads(r.read())
        content = data.get("result", "")
        if content:
            return f"Search results (Kimi API) for: {query}\n\n{content}"
        return f"[web_search_kimi] No results returned for: {query}"
    except Exception as e:
        log.warning("web_search_kimi failed: %s", e)
        return f"[web_search_kimi] Error: {e}. Use system__web_search as fallback."
    finally:
        _socket.getaddrinfo = _orig


def _web_search_claude(query: str, max_results: int = 5) -> str:
    """Search via Claude web_search — routes through cloud proxy (Pi doesn't hold Anthropic key)."""
    import socket as _socket
    _orig = _socket.getaddrinfo
    def _ipv4(h, p, f=0, *a, **kw): return _orig(h, p, _socket.AF_INET, *a, **kw)
    _socket.getaddrinfo = _ipv4
    try:
        dcfg = _load_device_config()
        base = dcfg.get("baseurl", "").rstrip("/")
        api_key = dcfg.get("apikey", "")
        if not base or not api_key:
            return "[web_search_claude] No API config found in device config."
        # Call cloud /v1/claude-web-search — cloud holds the Anthropic key
        body = json.dumps({"query": query, "max_results": max_results}).encode()
        req = urllib.request.Request(
            f"{base}/claude-web-search",
            data=body,
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=35) as r:
            data = json.loads(r.read())
        content = data.get("result", "")
        if content:
            return f"Search results (Claude API) for: {query}\n\n{content}"
        return f"[web_search_claude] No results returned for: {query}"
    except Exception as e:
        log.warning("web_search_claude failed: %s", e)
        return f"[web_search_claude] Error: {e}. Use system__web_search as fallback."
    finally:
        _socket.getaddrinfo = _orig


def _web_search(query: str, max_results: int = 5, user_model: str = None) -> str:
    """Search the web using data-driven engine pool with AI self-healing. Pure stdlib.
    user_model: model name selected by user — propagated to adaptation and translation."""
    import re
    import socket as _socket

    # Force IPv4 — Pi has no IPv6 connectivity, avoid IPv6 timeouts
    _orig_getaddrinfo = _socket.getaddrinfo
    def _ipv4_getaddrinfo(host, port, family=0, *args, **kwargs):
        return _orig_getaddrinfo(host, port, _socket.AF_INET, *args, **kwargs)
    _socket.getaddrinfo = _ipv4_getaddrinfo

    try:
        has_cjk = bool(re.search(r'[\u4e00-\u9fff\u3400-\u4dbf\uff00-\uffef]', query))
        core_cfg = _load_core_prompts()
        configured_engine = core_cfg.get("search_engine", "auto")
        brave_api_key = core_cfg.get("brave_api_key", "").strip()
        # Vault override for Brave API key
        try:
            v = _get_vault()
            if v:
                vbrave = v.get("brave_api_key")
                if vbrave:
                    brave_api_key = vbrave
        except Exception:
            pass
        # search_mode: "auto" | "llm" | "pi"
        search_mode = core_cfg.get("search_mode", "auto")
        all_engines = _load_search_engines()
        region = _detect_region()

        log.info("[SEARCH] === Web search START ===")
        log.info("[SEARCH] query=\"%s\" | user_model=%s | region=%s | search_mode=%s",
                 query[:80], user_model or "default", region, search_mode)
        log.info("[SEARCH] query_lang=%s | has_cjk=%s", "zh" if has_cjk else "non-zh", has_cjk)

        # LLM First mode — try provider-matched API before scraping
        if search_mode == "llm":
            _model_lower = (user_model or "").lower()
            if any(k in _model_lower for k in ("kimi", "moonshot")):
                result = _web_search_kimi(query, max_results)
                if not result.startswith("[web_search_kimi]"):
                    return result
            elif any(k in _model_lower for k in ("claude", "anthropic")):
                result = _web_search_claude(query, max_results)
                if not result.startswith("[web_search_claude]"):
                    return result
            # Provider without native search API, or API failed — fall through to scraping

        if configured_engine != "auto" and configured_engine in all_engines:
            providers = [configured_engine]
        else:
            in_china = (region == "CN")

            def _score(item):
                name, cfg = item
                regions = cfg.get("regions", ["OTHER"])
                # In China: only allow CN engines for local scraping
                # (google/ddg/bing are GFW-blocked — pointless to try)
                gfw_blocked = {"google", "duckduckgo", "bing"}
                if in_china and name in gfw_blocked:
                    return -1.0
                if region not in regions and "OTHER" not in regions and "*" not in regions:
                    return -1.0
                # Brave with key = always top priority
                if name == "brave" and brave_api_key:
                    return 2.0
                if name == "brave" and not brave_api_key:
                    return -1.0
                rel = cfg.get("reliability", 0.5)
                lang = cfg.get("language", "any")
                lang_bonus = 0.2 if (has_cjk and lang == "zh") or (not has_cjk and lang == "any") else 0.0
                return rel + lang_bonus

            candidates = sorted(all_engines.items(), key=_score, reverse=True)
            providers = [name for name, cfg in candidates if _score((name, cfg)) >= 0]
            # Log engine scores
            scored = [(n, round(_score((n, c)), 2)) for n, c in all_engines.items()]
            log.info("[SEARCH] engines scored: %s", " ".join(f"{n}={s}" for n, s in scored))
            log.info("[SEARCH] selected engines: %s", providers)

        # Cache translations per language to avoid re-translating for each engine
        _translated_cache = {}
        engines_tried = 0
        adaptations = 0

        for engine_name in providers:
            engine_cfg = all_engines.get(engine_name)
            if not engine_cfg:
                continue
            engines_tried += 1
            try:
                # Translate query if engine language doesn't match query language
                engine_lang = engine_cfg.get("language", "any")
                if engine_lang not in _translated_cache:
                    _translated_cache[engine_lang] = _translate_query_for_engine(query, engine_lang, user_model)
                search_query = _translated_cache[engine_lang]

                log.info("[SEARCH] engine=%s | query=\"%s\" | lang=%s", engine_name, search_query[:60], engine_lang)
                html_body = _fetch_search_html(engine_cfg, search_query)
                log.info("[SEARCH] fetch %s: html_size=%dKB", engine_name, len(html_body) // 1024)
                results = _parse_results(html_body, engine_cfg.get("patterns", {}), max_results)
                if results:
                    log.info("[SEARCH] %s: %d results parsed OK", engine_name, len(results))
                    engine_cfg["reliability"] = min(1.0, engine_cfg.get("reliability", 0.5) * 0.9 + 0.1)
                    _save_search_engines(all_engines)
                    log.info("[SEARCH] === Web search END === total_results=%d | engines_tried=%d | adaptations=%d",
                             len(results), engines_tried, adaptations)
                    return f"Search results via [{engine_name}] for: {query}\n\n" + "\n\n".join(results)
                # 0 results → AI self-healing
                log.info("[SEARCH] %s: 0 results → pattern adaptation using model=%s", engine_name, user_model or "default")
                adaptations += 1
                new_patterns = _adapt_engine_patterns(engine_name, engine_cfg, html_body, user_model=user_model)
                if new_patterns:
                    engine_cfg["patterns"] = new_patterns
                    all_engines[engine_name] = engine_cfg
                    _save_search_engines(all_engines)
                    results = _parse_results(html_body, new_patterns, max_results)
                    if results:
                        log.info("[SEARCH] adaptation successful for %s — %d results", engine_name, len(results))
                        engine_cfg["reliability"] = min(1.0, engine_cfg.get("reliability", 0.5) * 0.9 + 0.1)
                        _save_search_engines(all_engines)
                        log.info("[SEARCH] === Web search END === total_results=%d | engines_tried=%d | adaptations=%d",
                                 len(results), engines_tried, adaptations)
                        return f"Search results via [{engine_name} — AI repaired] for: {query}\n\n" + "\n\n".join(results)
                engine_cfg["reliability"] = max(0.1, engine_cfg.get("reliability", 0.5) * 0.8)
                _save_search_engines(all_engines)
            except Exception as e:
                log.warning("[SEARCH] %s failed: %s", engine_name, e)
                if engine_name in all_engines:
                    all_engines[engine_name]["reliability"] = max(0.1, all_engines[engine_name].get("reliability", 0.5) * 0.8)
                    _save_search_engines(all_engines)
    finally:
        _socket.getaddrinfo = _orig_getaddrinfo

    # All local scrapers failed
    log.info("[SEARCH] === Web search END === no results | engines_tried=%d | adaptations=%d",
             engines_tried if 'engines_tried' in dir() else 0, adaptations if 'adaptations' in dir() else 0)

    if search_mode == "llm":
        # LLM First: already tried API at top — retry as last resort
        _model_lower = (user_model or "").lower()
        if any(k in _model_lower for k in ("kimi", "moonshot")):
            log.info("[SEARCH] LLM First: scraping also failed, retrying Kimi API")
            return _web_search_kimi(query, max_results)
        elif any(k in _model_lower for k in ("claude", "anthropic")):
            log.info("[SEARCH] LLM First: scraping also failed, retrying Claude API")
            return _web_search_claude(query, max_results)

    # Pi Only: no API fallback
    # Auto: return clear signal — Alia will call system__web_search_kimi or system__web_search_claude herself
    return f"No results found for: {query}. You can try system__web_search_kimi or system__web_search_claude for AI-powered search."


EXEC_WORKDIR = "/tmp/clawbot-agent"
EXEC_USER = "openjarvis-agents"
# Protected paths removed — to be reimplemented with user-facing UI


def _execute_files_tool(tool_suffix: str, arguments: dict) -> str:
    """Execute a files module tool."""
    import shutil as _shutil

    if tool_suffix == "read":
        path = arguments.get("path", "")
        if not path:
            return '[error] Missing "path"'
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                content = f.read()
            return content or "(empty file)"
        except FileNotFoundError:
            pulled = _connector_pull(path)
            if pulled is not None:
                return pulled
            return f"[error] File not found: {path}"
        except Exception as e:
            return f"[error] {e}"

    if tool_suffix == "write":
        path = arguments.get("path", "")
        content = arguments.get("content")
        force = arguments.get("force", False)
        if not path:
            return '[error] Missing "path"'
        if content is None:
            return '[error] Missing "content"'
        content = str(content)
        try:
            parent = os.path.dirname(os.path.abspath(path))
            os.makedirs(parent, exist_ok=True)
            tmp = path + ".tmp." + str(os.getpid())
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(content)
            os.replace(tmp, path)
            _connector_upload_bg(path)
            return f"Written {len(content)} bytes to {path}"
        except Exception as e:
            try:
                os.unlink(tmp)
            except Exception:
                pass
            return f"[error] {e}"

    if tool_suffix == "list":
        path = arguments.get("path", "")
        recursive = arguments.get("recursive", False)
        if not path:
            return '[error] Missing "path"'
        try:
            import datetime as _dt
            lines = []
            if recursive:
                for root, dirs, files in os.walk(path):
                    for name in sorted(dirs):
                        lines.append(os.path.join(root, name) + "/")
                    for name in sorted(files):
                        fp = os.path.join(root, name)
                        try:
                            stat = os.stat(fp)
                            lines.append(f"{fp}  {stat.st_size}B  {_dt.datetime.fromtimestamp(stat.st_mtime).strftime('%Y-%m-%d %H:%M')}")
                        except Exception:
                            lines.append(fp)
            else:
                for name in sorted(os.listdir(path)):
                    fp = os.path.join(path, name)
                    try:
                        stat = os.stat(fp)
                        kind = "d" if os.path.isdir(fp) else "f"
                        lines.append(f"[{kind}] {name}  {stat.st_size}B  {_dt.datetime.fromtimestamp(stat.st_mtime).strftime('%Y-%m-%d %H:%M')}")
                    except Exception:
                        lines.append(name)
            return "\n".join(lines) if lines else "(empty directory)"
        except Exception as e:
            return f"[error] {e}"

    if tool_suffix == "move":
        src = arguments.get("src", "")
        dst = arguments.get("dst", "")
        if not src or not dst:
            return '[error] Missing "src" or "dst"'
        try:
            os.makedirs(os.path.dirname(os.path.abspath(dst)), exist_ok=True)
            _shutil.move(src, dst)
            return f"Moved {src} → {dst}"
        except Exception as e:
            return f"[error] {e}"

    if tool_suffix == "delete":
        path = arguments.get("path", "")
        recursive = arguments.get("recursive", False)
        if not path:
            return '[error] Missing "path"'
        try:
            if os.path.isdir(path):
                if recursive:
                    _shutil.rmtree(path)
                    return f"Deleted directory {path} recursively"
                else:
                    os.rmdir(path)
                    return f"Deleted empty directory {path}"
            else:
                os.unlink(path)
                return f"Deleted {path}"
        except Exception as e:
            return f"[error] {e}"

    if tool_suffix == "dir_create" or tool_suffix == "mkdir":
        path = arguments.get("path", "")
        if not path:
            return '[error] Missing "path"'
        try:
            os.makedirs(path, exist_ok=True)
            return f"Created directory {path}"
        except Exception as e:
            return f"[error] {e}"

    return f"[error] Unknown files tool: files__{tool_suffix}"


def _execute_documents_tool(tool_suffix: str, arguments: dict) -> str:
    """Execute a documents module tool."""
    if tool_suffix == "pdf_to_text":
        path = arguments.get("path", "")
        if not path:
            return '[error] Missing "path"'
        try:
            result = subprocess.run(
                ["pdftotext", path, "-"],
                capture_output=True, text=True, timeout=30,
            )
            if result.returncode != 0:
                return f"[error] pdftotext failed: {result.stderr.strip()}"
            return result.stdout.strip() or "(no text extracted)"
        except FileNotFoundError:
            return "[error] pdftotext not found — install with: apt-get install poppler-utils"
        except Exception as e:
            return f"[error] {e}"

    if tool_suffix == "csv_parse" or tool_suffix == "csv_read":
        import csv as _csv
        path = arguments.get("path", "")
        delimiter = arguments.get("delimiter", ",")
        max_rows = int(arguments.get("max_rows", 100))
        if not path:
            return '[error] Missing "path"'
        try:
            rows = []
            with open(path, encoding="utf-8", errors="replace", newline="") as f:
                reader = _csv.DictReader(f, delimiter=delimiter)
                for i, row in enumerate(reader):
                    if i >= max_rows:
                        break
                    rows.append(dict(row))
            return json.dumps(rows, ensure_ascii=False)
        except Exception as e:
            return f"[error] {e}"

    if tool_suffix == "csv_write":
        import csv as _csv
        path = arguments.get("path", "")
        data = arguments.get("data", [])
        delimiter = arguments.get("delimiter", ",")
        if not path:
            return '[error] Missing "path"'
        if not isinstance(data, list) or not data:
            return '[error] "data" must be a non-empty array of objects'
        try:
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
            fieldnames = list(data[0].keys())
            with open(path, "w", encoding="utf-8", newline="") as f:
                writer = _csv.DictWriter(f, fieldnames=fieldnames, delimiter=delimiter)
                writer.writeheader()
                writer.writerows(data)
            return f"Written {len(data)} rows to {path}"
        except Exception as e:
            return f"[error] {e}"

    return f"[error] Unknown documents tool: documents__{tool_suffix}"


def _execute_web_tool(tool_suffix: str, arguments: dict, user_model: str = None) -> str:
    """Execute a web module tool."""
    if tool_suffix == "search":
        query = arguments.get("query", "")
        max_results = min(int(arguments.get("max_results", 5)), 10)
        if not query:
            return "[error] No query provided"
        try:
            return _web_search(query, max_results, user_model=user_model)
        except Exception as e:
            return f"[error] Web search failed: {e}"

    if tool_suffix == "http_get":
        url = arguments.get("url", "")
        headers = arguments.get("headers") or {}
        timeout = int(arguments.get("timeout", 30))
        if not url:
            return '[error] Missing "url"'
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read().decode("utf-8", errors="replace")
            return body[:TOOL_RESULT_MAX_CHARS]
        except Exception as e:
            return f"[error] {e}"

    if tool_suffix == "http_post":
        url = arguments.get("url", "")
        body = arguments.get("body") or {}
        headers = arguments.get("headers") or {}
        timeout = int(arguments.get("timeout", 30))
        if not url:
            return '[error] Missing "url"'
        try:
            data = json.dumps(body).encode()
            req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json", **headers})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read().decode("utf-8", errors="replace")[:TOOL_RESULT_MAX_CHARS]
        except Exception as e:
            return f"[error] {e}"

    if tool_suffix == "file_download":
        url = arguments.get("url", "")
        dest = arguments.get("dest", "")
        timeout = int(arguments.get("timeout", 60))
        if not url or not dest:
            return '[error] Missing "url" or "dest"'
        try:
            os.makedirs(os.path.dirname(os.path.abspath(dest)), exist_ok=True)
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = resp.read()
            with open(dest, "wb") as f:
                f.write(data)
            return f"Downloaded {len(data)} bytes to {dest}"
        except Exception as e:
            return f"[error] {e}"

    return f"[error] Unknown web tool: web__{tool_suffix}"


def _execute_exec_tool(tool_suffix: str, arguments: dict) -> str:
    """Execute an exec module tool (sandboxed via openjarvis-agents user)."""
    import shutil as _shutil

    os.makedirs(EXEC_WORKDIR, exist_ok=True)
    # Check if sandbox user exists
    _use_sandbox = bool(_shutil.which("sudo") and
                        subprocess.run(["id", EXEC_USER], capture_output=True, timeout=3).returncode == 0)

    timeout = int(arguments.get("timeout", 60))

    if tool_suffix == "run_python":
        code = arguments.get("code", "")
        if not code.strip():
            return '[error] Missing "code"'
        try:
            with tempfile.NamedTemporaryFile(suffix=".py", mode="w", dir=EXEC_WORKDIR, delete=False) as f:
                f.write(code)
                tmp_path = f.name
            try:
                if _use_sandbox:
                    cmd = ["sudo", "-u", EXEC_USER, "python3", tmp_path]
                else:
                    cmd = ["python3", tmp_path]
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                                        cwd=EXEC_WORKDIR, env={**os.environ, "HOME": EXEC_WORKDIR})
                out = result.stdout + result.stderr
                return out.strip() or "(no output)"
            finally:
                try:
                    os.unlink(tmp_path)
                except Exception:
                    pass
        except subprocess.TimeoutExpired:
            return f"[error] Script timed out after {timeout}s"
        except Exception as e:
            return f"[error] {e}"

    if tool_suffix == "run_bash":
        script = arguments.get("script", "")
        if not script.strip():
            return '[error] Missing "script"'
        try:
            with tempfile.NamedTemporaryFile(suffix=".sh", mode="w", dir=EXEC_WORKDIR, delete=False) as f:
                f.write("#!/bin/bash\n" + script)
                tmp_path = f.name
            os.chmod(tmp_path, 0o755)
            try:
                if _use_sandbox:
                    cmd = ["sudo", "-u", EXEC_USER, "bash", tmp_path]
                else:
                    cmd = ["bash", tmp_path]
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                                        cwd=EXEC_WORKDIR, env={**os.environ, "HOME": EXEC_WORKDIR})
                out = result.stdout + result.stderr
                return out.strip() or "(no output)"
            finally:
                try:
                    os.unlink(tmp_path)
                except Exception:
                    pass
        except subprocess.TimeoutExpired:
            return f"[error] Script timed out after {timeout}s"
        except Exception as e:
            return f"[error] {e}"

    return f"[error] Unknown exec tool: exec__{tool_suffix}"


# ── Vault tools ──────────────────────────────────────────────────────────────
VAULT_SETTINGS_PATH = "/home/pi/.openjarvis/vault-settings.json"
_vault_instance = None


def _is_vault_enabled():
    """Check if vault is enabled. Default: True (enabled)."""
    try:
        with open(VAULT_SETTINGS_PATH) as f:
            return json.loads(f.read()).get("vault_enabled", True)
    except Exception:
        return True  # Default: vault enabled


def _get_vault():
    """Lazy-init vault singleton.  Pre-warm substitution cache on first call.
    Returns None if vault is disabled via settings toggle."""
    if not _is_vault_enabled():
        return None  # Vault disabled — bypass everything
    global _vault_instance
    if _vault_instance is None:
        from vault import Vault
        _vault_instance = Vault()
        # Pre-warm: decrypt all substitutions now so first mask() is free
        try:
            _vault_instance._load_substitutions()
        except Exception:
            pass
    return _vault_instance


def _execute_vault_tool(tool_suffix: str, arguments: dict) -> str:
    """Execute a vault module tool."""
    if not _is_vault_enabled():
        return "[vault disabled] The credential vault is currently disabled by the user. Cannot execute vault operations."
    try:
        if tool_suffix == "store":
            v = _get_vault()
            ok = v.store(arguments["name"], arguments["value"], arguments.get("category", "other"), arguments.get("note", ""), arguments.get("username", ""))
            return f"Secret '{arguments['name']}' stored successfully." if ok else "Failed to store secret."

        if tool_suffix == "get":
            v = _get_vault()
            val = v.get(arguments["name"])
            return val if val else f"Secret '{arguments['name']}' not found."

        if tool_suffix == "list":
            v = _get_vault()
            items = v.list(arguments.get("category"))
            if not items:
                return "Vault is empty."
            lines = []
            for s in items:
                parts = [s['name'], f"[{s['category']}]"]
                if s.get('username'):
                    parts.append(f"user: {s['username']}")
                if s.get('note'):
                    parts.append(f"— {s['note']}")
                lines.append("  - " + " ".join(parts))
            return f"Vault contains {len(items)} secret(s):\n" + "\n".join(lines)

        if tool_suffix == "delete":
            v = _get_vault()
            ok = v.delete(arguments["name"])
            return f"Secret '{arguments['name']}' deleted." if ok else f"Secret '{arguments['name']}' not found."

        if tool_suffix == "flag_secret":
            v = _get_vault()
            _val = arguments["value"]
            # Route otpauth:// URIs to TOTP storage instead of generic secret
            if _val.strip().startswith("otpauth://"):
                try:
                    stored_name, is_new = v.totp_add_from_uri(_val.strip())
                    return (f"TOTP entry '{stored_name}' {'added' if is_new else 'updated'} "
                            f"from URI. Code generation ready via vault__totp_generate.")
                except Exception as _e:
                    log.warning("flag_secret otpauth:// route failed: %s — falling back", _e)
            _name = arguments["suggested_name"]
            _cat = arguments.get("category", "other")
            _pattern = arguments.get("pattern_hint", "")
            _alias = v.protect(_name, _val, kind="secret", category=_cat)
            _learned = False
            if _pattern:
                _learned = v.learn_pattern(_name, _pattern, _cat)
            _msg = f"Secret protected as {_alias}."
            if _learned:
                _msg += f" Pattern '{_pattern}' learned — future {_name} keys will be auto-detected."
            _msg += " Ask user if they want to keep it stored."
            return _msg

        if tool_suffix == "protect_pii":
            v = _get_vault()
            _alias = v.protect(arguments["name"], arguments["value"], kind="pii", category=arguments.get("category", "other"))
            return f"Personal data protected. '{arguments['name']}' will appear as '{_alias}' in all future messages."

        if tool_suffix == "search":
            v = _get_vault()
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
                return f"No secrets matching '{arguments['query']}'."
            lines = []
            for s in matches:
                parts = [s["name"], f"[{s['category']}]"]
                if s.get("username"):
                    parts.append(f"user: {s['username']}")
                if s.get("note"):
                    parts.append(f"— {s['note']}")
                lines.append("  - " + " ".join(parts))
            return f"Found {len(matches)} match(es):\n" + "\n".join(lines)

        if tool_suffix == "totp_add":
            v = _get_vault()
            name = arguments.get("name", "")
            secret = arguments.get("secret", "")
            issuer = arguments.get("issuer", "")
            if not name or not secret:
                return "Error: name and secret are required"
            if secret.startswith("otpauth://"):
                stored_name, is_new = v.totp_add_from_uri(secret, name_override=name)
                return f"TOTP '{stored_name}' {'added' if is_new else 'updated'} from URI"
            else:
                is_new = v.totp_add(name, secret, issuer=issuer)
                return f"TOTP '{name}' {'added' if is_new else 'updated'}"

        if tool_suffix == "totp_code":
            v = _get_vault()
            name = arguments.get("name", "")
            if not name:
                return "Error: name is required"
            result = v.totp_get_code(name)
            if result is None:
                return f"Error: TOTP entry '{name}' not found"
            return f"{result['code']} (expires in {result['remaining']}s)"

        if tool_suffix == "totp_list":
            v = _get_vault()
            entries = v.totp_list()
            if not entries:
                return "No TOTP entries stored"
            lines = [f"- {e['name']}" + (f" ({e['issuer']})" if e['issuer'] else "") for e in entries]
            return "TOTP entries:\n" + "\n".join(lines)

        if tool_suffix == "totp_delete":
            v = _get_vault()
            name = arguments.get("name", "")
            if not name:
                return "Error: name is required"
            deleted = v.totp_delete(name)
            return f"TOTP '{name}' deleted" if deleted else f"TOTP entry '{name}' not found"

        if tool_suffix == "totp_search":
            v = _get_vault()
            q = arguments.get("query", "").lower()
            if not q:
                return "Error: query is required"
            entries = v.totp_list()
            matches = [e for e in entries if q in e["name"].lower() or q in (e.get("issuer") or "").lower()]
            if not matches:
                return f"No TOTP entries matching '{arguments['query']}'."
            lines = [f"- {e['name']}" + (f" ({e['issuer']})" if e.get('issuer') else "") for e in matches]
            return f"Found {len(matches)} TOTP match(es):\n" + "\n".join(lines)

        if tool_suffix == "totp_verify":
            name = arguments.get("name", "")
            code = arguments.get("code", "")
            if not name or not code:
                return "Error: name and code are required"
            v = _get_vault()
            valid = v.totp_verify(name, code)
            return f"Code {'VALID ✓' if valid else 'INVALID ✗'} for '{name}'"

    except Exception as e:
        log.error("vault tool error: %s", e)
        return f"[error] Vault operation failed: {e}"

    return f"[error] Unknown vault tool: vault__{tool_suffix}"


def _execute_email_tool(tool_suffix: str, arguments: dict) -> str:
    """Execute an email module tool."""
    if tool_suffix == "send":
        import smtplib
        from email.mime.text import MIMEText
        from email.mime.multipart import MIMEMultipart

        to = arguments.get("to", "")
        subject = arguments.get("subject", "")
        body = arguments.get("body", "")
        cc = arguments.get("cc", "")

        if not to or not subject or not body:
            return '[error] Missing required fields: to, subject, body'

        email_config_path = "/home/pi/.openjarvis/email.json"
        try:
            with open(email_config_path) as f:
                cfg = json.load(f)
        except Exception as e:
            return f"[error] Email config not found at {email_config_path}: {e}\nCreate it with: {{\"smtp_host\": \"...\", \"smtp_port\": 587, \"user\": \"...\", \"password\": \"...\", \"from_name\": \"ClawBot\"}}"

        smtp_host = cfg.get("smtp_host", "")
        smtp_port = int(cfg.get("smtp_port", 587))
        user = cfg.get("user", "")
        password = cfg.get("password", "")
        from_name = cfg.get("from_name", "ClawBot")

        # Vault override for SMTP password
        try:
            v = _get_vault()
            if v:
                vpwd = v.get("smtp_password")
                if vpwd:
                    password = vpwd
        except Exception:
            pass

        if not smtp_host or not user or not password:
            return "[error] Incomplete email config (smtp_host, user, password required)"

        try:
            msg = MIMEMultipart()
            msg["From"] = f"{from_name} <{user}>"
            msg["To"] = to
            msg["Subject"] = subject
            if cc:
                msg["Cc"] = cc
            msg.attach(MIMEText(body, "plain", "utf-8"))

            with smtplib.SMTP(smtp_host, smtp_port, timeout=30) as server:
                server.ehlo()
                server.starttls()
                server.login(user, password)
                recipients = [to] + ([cc] if cc else [])
                server.sendmail(user, recipients, msg.as_string())

            return f"Email sent to {to}"
        except Exception as e:
            return f"[error] SMTP failed: {e}"

    return f"[error] Unknown email tool: email__{tool_suffix}"


def _execute_git_tool(tool_suffix: str, arguments: dict) -> str:
    """Execute a git module tool."""
    if tool_suffix == "status":
        repo_path = arguments.get("repo_path", "")
        if not repo_path:
            return '[error] Missing "repo_path"'
        try:
            r = subprocess.run(["git", "status", "--short", "-b"], capture_output=True, text=True,
                               timeout=30, cwd=repo_path)
            return r.stdout.strip() + (r.stderr.strip() and "\n" + r.stderr.strip() or "")
        except Exception as e:
            return f"[error] {e}"

    if tool_suffix == "commit":
        repo_path = arguments.get("repo_path", "")
        message = arguments.get("message", "")
        if not repo_path or not message:
            return '[error] Missing "repo_path" or "message"'
        try:
            add = subprocess.run(["git", "add", "-A"], capture_output=True, text=True, timeout=30, cwd=repo_path)
            if add.returncode != 0:
                return f"[error] git add failed: {add.stderr.strip()}"
            commit = subprocess.run(["git", "commit", "-m", message], capture_output=True, text=True,
                                    timeout=30, cwd=repo_path)
            out = commit.stdout.strip() + (commit.stderr.strip() and "\n" + commit.stderr.strip() or "")
            if commit.returncode != 0:
                return f"[error] git commit failed: {out}"
            return out
        except Exception as e:
            return f"[error] {e}"

    if tool_suffix == "push":
        repo_path = arguments.get("repo_path", "")
        remote = arguments.get("remote", "origin")
        branch = arguments.get("branch", "")
        if not repo_path:
            return '[error] Missing "repo_path"'
        try:
            cmd = ["git", "push", remote]
            if branch:
                cmd.append(branch)
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=60, cwd=repo_path)
            out = result.stdout.strip() + (result.stderr.strip() and "\n" + result.stderr.strip() or "")
            return out or "Push successful"
        except Exception as e:
            return f"[error] {e}"

    if tool_suffix == "pull":
        repo_path = arguments.get("repo_path", "")
        remote = arguments.get("remote", "origin")
        branch = arguments.get("branch", "")
        if not repo_path:
            return '[error] Missing "repo_path"'
        try:
            cmd = ["git", "pull", remote]
            if branch:
                cmd.append(branch)
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=60, cwd=repo_path)
            out = result.stdout.strip() + (result.stderr.strip() and "\n" + result.stderr.strip() or "")
            return out or "Already up to date."
        except Exception as e:
            return f"[error] {e}"

    if tool_suffix == "log":
        repo_path = arguments.get("repo_path", "")
        n = int(arguments.get("n", 10))
        if not repo_path:
            return '[error] Missing "repo_path"'
        try:
            result = subprocess.run(
                ["git", "log", f"-{n}", "--oneline", "--decorate"],
                capture_output=True, text=True, timeout=30, cwd=repo_path,
            )
            return result.stdout.strip() or "(no commits)"
        except Exception as e:
            return f"[error] {e}"

    return f"[error] Unknown git tool: git__{tool_suffix}"


def _execute_memory_tool(tool_suffix: str, arguments: dict, agent_id: str = None) -> str:
    """Execute memory__* tools (save/read/delete agent persistent memory)."""
    if not agent_id:
        return "[error] memory tools require an agent context"
    if tool_suffix == "save":
        return save_agent_memory(agent_id, arguments.get("key", ""), arguments.get("value", ""))
    if tool_suffix == "read":
        mem = load_agent_memory(agent_id)
        return mem if mem else "(no memories saved yet)"
    if tool_suffix == "delete":
        return delete_agent_memory(agent_id, arguments.get("key", ""))
    return f"[error] Unknown memory tool: memory__{tool_suffix}"


# ── Handler map — resolves DISPATCH_TABLE handler names to callables ──────────
_HANDLER_MAP = {
    "_execute_builtin":        lambda: _execute_builtin,
    "_execute_files_tool":     lambda: _execute_files_tool,
    "_execute_documents_tool": lambda: _execute_documents_tool,
    "_execute_web_tool":       lambda: _execute_web_tool,
    "_execute_exec_tool":      lambda: _execute_exec_tool,
    "_execute_email_tool":     lambda: _execute_email_tool,
    "_execute_git_tool":       lambda: _execute_git_tool,
    "_execute_vault_tool":     lambda: _execute_vault_tool,
    "_execute_memory_tool":    lambda: _execute_memory_tool,
}


def _execute_tool(tool_name: str, arguments_raw: str, user_model: str = None, agent_id: str = None) -> str:
    """Execute a tool call and return a plain-text result string.

    Routing order:
      1. DISPATCH_TABLE lookup (built-in handlers in this file)
      2. HTTP POST to external module at 127.0.0.1:{port}/v1/{module_id}/execute

    Args:
        tool_name:      "{module_id}__{tool_suffix}" (double underscore separator)
        arguments_raw:  JSON string of tool arguments
        user_model:     Model selected by user — forwarded to web_search handlers
        agent_id:       Agent context — forwarded to memory handlers

    Returns:
        Plain-text result string. Errors are prefixed with "[error] ".

    External module response format (expected JSON):
        {"result": "..."}  or  {"output": "..."}  →  extracted as string
        {"error": "..."}                          →  "[error] ..."
        Plain text / other JSON                   →  returned as-is
    """
    if "__" not in tool_name:
        return f"[error] Invalid tool name format: '{tool_name}' (expected module_id__tool)"

    module_id, _, tool_suffix = tool_name.partition("__")

    try:
        arguments = json.loads(arguments_raw) if arguments_raw else {}
    except json.JSONDecodeError:
        arguments = {"raw": arguments_raw}

    # ── Built-in dispatch via DISPATCH_TABLE ──────────────────────────────────
    entry = DISPATCH_TABLE.get(module_id)
    if entry:
        handler_fn = _HANDLER_MAP.get(entry["handler"])
        if not handler_fn:
            return f"[error] No handler registered for '{entry['handler']}'"
        handler = handler_fn()

        # Apply aliases (e.g. agents__delegate → handoff, scheduler__create → schedule_task)
        aliases = entry.get("aliases")
        if aliases:
            mapped = aliases.get(tool_suffix)
            if not mapped:
                return f"[error] Unknown {module_id} tool: {module_id}__{tool_suffix}"
            tool_suffix = mapped

        # Build kwargs based on entry flags
        kwargs = {}
        if entry.get("user_model"):
            kwargs["user_model"] = user_model
        if entry.get("agent_id"):
            kwargs["agent_id"] = agent_id

        return handler(tool_suffix, arguments, **kwargs)

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
            return _normalize_module_response(raw, tool_name)
    except urllib.error.HTTPError as e:
        msg = e.read().decode(errors="replace")
        log.error("Tool '%s' HTTP %d: %s", tool_name, e.code, msg)
        return f"[error] Tool returned HTTP {e.code}: {msg[:200]}"
    except Exception as e:
        log.error("Tool '%s' call failed: %s", tool_name, e)
        return f"[error] Tool call failed: {e}"


def _normalize_module_response(raw: str, tool_name: str) -> str:
    """Normalize an external module's HTTP response into a plain string.

    Accepted JSON formats (in priority order):
      {"error": "..."}         → "[error] ..."
      {"result": "..."}        → str(result)
      {"output": "..."}        → str(output)
      any other JSON / string  → returned as-is

    Non-JSON responses are returned verbatim.
    Empty responses are flagged as errors.
    """
    if not raw or not raw.strip():
        log.warning("Tool '%s' returned empty response", tool_name)
        return f"[error] Tool '{tool_name}' returned empty response"
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            if "error" in data:
                return f"[error] {data['error']}"
            result = data.get("result") or data.get("output")
            if result is not None:
                return str(result)
        return raw
    except (json.JSONDecodeError, ValueError):
        return raw


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


# ── Agent persistent memory ──────────────────────────────────────────────────

def _agent_memory_path(agent_id: str) -> str:
    return os.path.join(AGENT_MEMORY_DIR, agent_id + ".md")


def load_agent_memory(agent_id: str) -> str:
    """Load persistent memory for an agent. Returns empty string if none."""
    path = _agent_memory_path(agent_id)
    try:
        with open(path) as f:
            return f.read().strip()
    except FileNotFoundError:
        return ""


def save_agent_memory(agent_id: str, key: str, value: str) -> str:
    """Append a key-value fact to agent's persistent memory file.
    Deduplicates: if key already exists, replaces it.
    Returns confirmation message.
    """
    os.makedirs(AGENT_MEMORY_DIR, exist_ok=True)
    path = _agent_memory_path(agent_id)

    # Load existing lines
    lines = []
    try:
        with open(path) as f:
            lines = f.readlines()
    except FileNotFoundError:
        pass

    # Remove existing line with same key (case-insensitive)
    key_lower = key.lower().strip()
    new_lines = [ln for ln in lines if not ln.lower().strip().startswith(f"- **{key_lower}**")]

    # Append new fact
    new_lines.append(f"- **{key}**: {value}\n")

    with open(path, "w") as f:
        f.writelines(new_lines)

    log.info("Agent memory saved: %s → %s = %s", agent_id, key, value[:80])
    return f"Memorized: {key} = {value}"


def delete_agent_memory(agent_id: str, key: str) -> str:
    """Remove a specific key from agent memory."""
    path = _agent_memory_path(agent_id)
    try:
        with open(path) as f:
            lines = f.readlines()
    except FileNotFoundError:
        return "No memory to delete."

    key_lower = key.lower().strip()
    new_lines = [ln for ln in lines if not ln.lower().strip().startswith(f"- **{key_lower}**")]

    if len(new_lines) == len(lines):
        return f"Key '{key}' not found in memory."

    with open(path, "w") as f:
        f.writelines(new_lines)

    log.info("Agent memory deleted: %s → %s", agent_id, key)
    return f"Forgot: {key}"


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


def route_to_agents(user_message: str, agents: dict = None, last_agent_id: str = None) -> list:
    """Route user message to the best agent.
    Priority: direct name addressing → Haiku LLM routing → keyword fallback → sticky agent → Core.
    last_agent_id: if provided, acts as sticky default when no explicit routing match is found.
    Returns list of agent configs, or [] for Core fallback.
    """
    if agents is None:
        agents = load_agents()

    msg = user_message.strip()
    msg_lower = msg.lower()

    # Priority 1: direct name addressing — "Sophie, fais un devis" or "Sophie fais…"
    first_token = msg_lower.split()[0].rstrip(',;:!?') if msg.split() else ''
    for agent_cfg in agents.values():
        if not agent_cfg.get("enabled", True):
            continue
        name = agent_cfg.get("name", "").lower().strip()
        if not name or len(name) < 3:
            continue
        if first_token == name:
            log.info("Direct name routing → %s", agent_cfg["id"])
            return [agent_cfg]

    # Shortcut: very short messages → sticky agent or Core (no LLM call)
    if len(msg.split()) <= 3:
        if last_agent_id and last_agent_id in agents and agents[last_agent_id].get("enabled", True):
            log.info("Short message — sticky agent %s", last_agent_id)
            return [agents[last_agent_id]]
        log.info("Short message — Core direct (skip routing)")
        return []

    # Priority 2: Haiku LLM routing
    chosen_id = _route_via_llm(msg, agents)
    if chosen_id:
        log.info("Haiku routing → %s", chosen_id)
        return [agents[chosen_id]]

    # Priority 3: keyword fallback (includes agent name when properly stored)
    kw_matches = _route_via_keywords(msg, agents)
    if kw_matches:
        log.info("Keyword routing → %s", kw_matches[0]["id"])
        return [kw_matches[0]]

    # Priority 4: sticky agent — continue with last used agent if no match found
    if last_agent_id and last_agent_id in agents and agents[last_agent_id].get("enabled", True):
        log.info("No routing match — sticky agent %s", last_agent_id)
        return [agents[last_agent_id]]

    log.info("No routing match — Core direct")
    return []


def _build_agent_tools(agent_config: dict) -> list:
    """All tools available to all agents — skills array is metadata only (UI display)."""
    return BUILTIN_TOOLS + get_enabled_tools()


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

    # Add memory tools if memory_enabled
    if agent.get("memory_enabled", False):
        tools = tools + AGENT_MEMORY_TOOLS

    tool_names = ", ".join(t["function"]["name"] for t in tools if t.get("type") == "function")
    system_prompt = (
        f"{agent.get('system_prompt', 'You are a helpful assistant.')}\n\n"
        f"You have access to the following tools: {tool_names}. "
        "ALWAYS use your tools to complete tasks — never just describe how to do something. "
        "Execute commands, write files, and run code directly."
    )

    # Inject persistent memory if available
    memory = load_agent_memory(agent_id)
    if memory:
        system_prompt += (
            "\n\n## Your persistent memory\n"
            "These facts persist across all sessions and all interfaces (dashboard, Cowork, etc.):\n"
            f"{memory}\n\n"
            "Use this memory to stay consistent. When the user tells you new important facts "
            "about yourself or them, use the memory_save tool to remember them permanently."
        )
    elif agent.get("memory_enabled", False):
        system_prompt += (
            "\n\nYou have persistent memory enabled. When the user tells you important facts "
            "(your name, preferences, context about them or their projects), use the memory_save "
            "tool to remember them permanently across all sessions."
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
    for event in chat_with_tools_stream(body, override_tools=tools, agent_id=agent_id):
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
            for event in chat_with_tools_stream(revision_body, override_tools=tools, agent_id=agent_id):
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
