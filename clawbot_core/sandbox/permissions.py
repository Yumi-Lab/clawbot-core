"""
Tool permission engine — policy defaults, safe bins, arg analysis, env sanitization.
"""
from __future__ import annotations

import os
import re
import shlex
from enum import Enum

# ---------------------------------------------------------------------------
# ToolPermission enum
# ---------------------------------------------------------------------------

class ToolPermission(Enum):
    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"


# ---------------------------------------------------------------------------
# Default policies per tool (plan-independent)
# ---------------------------------------------------------------------------

_ALLOW_TOOLS = frozenset({
    "system__read_file", "files__read", "files__list",
    "system__web_search", "system__web_search_kimi", "system__web_search_claude",
    "system__get_system_info", "system__disk",
})

_ALLOW_PREFIXES = ("documents__",)

_ASK_TOOLS = frozenset({
    "system__bash", "system__python", "system__write_file",
    "files__write", "files__delete", "files__move",
    "git__commit", "git__push", "email__send",
})

# Plan-dependent overrides: {plan: {tool: permission}}
_PLAN_OVERRIDES: dict[str, dict[str, ToolPermission]] = {
    "free": {
        "system__ssh": ToolPermission.DENY,
        "email__send": ToolPermission.DENY,
    },
    "perso": {
        "system__ssh": ToolPermission.DENY,
    },
    "pro": {
        "system__ssh": ToolPermission.ASK,
    },
}

# Exec tools (module sandbox) — DENY for free, ASK otherwise
_EXEC_PREFIX = "exec__"

# ---------------------------------------------------------------------------
# Protected paths & services (mirrored from orchestrator for arg analysis)
# ---------------------------------------------------------------------------

_PROTECTED_PATHS = ("/etc/", "/usr/", "/boot/", "/bin/", "/sbin/", "/lib/", "/sys/")

_PROTECTED_SERVICES = frozenset({
    "clawbot-core", "nginx", "clawbot-cloud", "clawbot-status-api",
})

# ---------------------------------------------------------------------------
# Dangerous bash patterns — force DENY regardless of policy
# ---------------------------------------------------------------------------

_DANGEROUS_PATTERNS = [
    re.compile(r"\brm\s+(-[a-zA-Z]*f[a-zA-Z]*\s+)?-[a-zA-Z]*r[a-zA-Z]*\s+/(\s|$|\*)"),
    re.compile(r"\brm\s+(-[a-zA-Z]*r[a-zA-Z]*\s+)?-[a-zA-Z]*f[a-zA-Z]*\s+/(\s|$|\*)"),
    re.compile(r"\bchmod\s+777\s+/(\s|$)"),
    re.compile(r"\bdd\s+.*\bof=/dev/"),
    re.compile(r"\bmkfs\b"),
]

_SERVICE_BLOCK_PATTERNS = [
    "systemctl stop", "systemctl restart", "systemctl disable",
    "systemctl kill", "systemctl mask",
    "service stop", "service restart",
    "kill -9", "pkill",
]

# ---------------------------------------------------------------------------
# Safe bins — stdin-only commands that bypass ASK
# ---------------------------------------------------------------------------

_SAFE_BINS: dict[str, dict] = {
    "grep":  {"max_positional": 1, "denied_flags": {"-r", "-R", "-f", "--include", "--exclude", "--recursive"}},
    "head":  {"max_positional": 0, "denied_flags": set(), "allowed_value_flags": {"-n"}},
    "tail":  {"max_positional": 0, "denied_flags": set(), "allowed_value_flags": {"-n"}},
    "cut":   {"max_positional": 0, "denied_flags": set()},
    "sort":  {"max_positional": 0, "denied_flags": {"-o"}},
    "uniq":  {"max_positional": 0, "denied_flags": set()},
    "tr":    {"max_positional": 2, "denied_flags": set()},
    "wc":    {"max_positional": 0, "denied_flags": set()},
    "jq":    {"max_positional": 1, "denied_flags": {"--argfile", "-f", "--rawfile"}},
}


def validate_safe_bin(argv: list[str]) -> bool:
    """Return True if *argv* is a safe stdin-only command (no file args)."""
    if not argv:
        return False
    bin_name = os.path.basename(argv[0])
    profile = _SAFE_BINS.get(bin_name)
    if profile is None:
        return False

    positional_count = 0
    denied = profile["denied_flags"]
    allowed_value = profile.get("allowed_value_flags", set())
    i = 1
    while i < len(argv):
        arg = argv[i]
        if arg == "--":
            # Everything after -- is positional
            positional_count += len(argv) - i - 1
            break
        if arg.startswith("-"):
            if arg in denied:
                return False
            # Value flags consume next arg
            if arg in allowed_value:
                i += 2
                continue
        else:
            positional_count += 1
        i += 1

    return positional_count <= profile["max_positional"]


# ---------------------------------------------------------------------------
# Env sanitization
# ---------------------------------------------------------------------------

_BLOCKED_ENV_VARS = frozenset({
    "PATH", "LD_PRELOAD", "LD_LIBRARY_PATH",
    "SSH_AUTH_SOCK", "KUBECONFIG", "GIT_ASKPASS",
})

_BLOCKED_ENV_PREFIXES = ("ANSIBLE_", "KUBE_", "AWS_", "GCP_", "AZURE_")


def sanitize_exec_env(overrides: dict | None = None) -> dict:
    """Return a cleaned copy of os.environ suitable for sandboxed execution."""
    env = {}
    for key, val in os.environ.items():
        if key in _BLOCKED_ENV_VARS:
            continue
        if any(key.startswith(p) for p in _BLOCKED_ENV_PREFIXES):
            continue
        env[key] = val
    if overrides:
        for key, val in overrides.items():
            if key not in _BLOCKED_ENV_VARS and not any(key.startswith(p) for p in _BLOCKED_ENV_PREFIXES):
                env[key] = val
    return env


# ---------------------------------------------------------------------------
# Shell unwrapping
# ---------------------------------------------------------------------------

def _unwrap_shell(cmd: str) -> str:
    """Unwrap `bash -c '...'` or `sh -c '...'` to the inner command."""
    stripped = cmd.strip()
    for shell in ("bash", "sh"):
        prefix = f"{shell} -c "
        if stripped.startswith(prefix):
            inner = stripped[len(prefix):]
            # Remove surrounding quotes
            if (inner.startswith('"') and inner.endswith('"')) or \
               (inner.startswith("'") and inner.endswith("'")):
                inner = inner[1:-1]
            return inner
    return cmd


# ---------------------------------------------------------------------------
# ToolPolicy
# ---------------------------------------------------------------------------

class ToolPolicy:
    """Stateless default policy checker."""

    @staticmethod
    def check(tool_name: str, args: dict | None = None,
              plan: str = "free") -> tuple[ToolPermission, str]:
        """Return (permission, reason) for the given tool call."""

        # --- Arg-level analysis (overrides everything) ---
        if tool_name == "system__bash" and args:
            cmd = args.get("command", "")
            inner = _unwrap_shell(cmd)

            # Dangerous patterns → DENY
            for pat in _DANGEROUS_PATTERNS:
                if pat.search(inner):
                    return ToolPermission.DENY, f"Dangerous command blocked: {inner[:60]}"

            # Service blocking
            inner_lower = inner.lower()
            for pattern in _SERVICE_BLOCK_PATTERNS:
                if pattern in inner_lower:
                    if any(svc in inner_lower for svc in _PROTECTED_SERVICES):
                        return ToolPermission.DENY, f"Blocked: '{pattern}' on protected service"

            # Safe bins → ALLOW even though bash is ASK
            try:
                argv = shlex.split(inner)
                if argv and validate_safe_bin(argv):
                    return ToolPermission.ALLOW, "safe bin (stdin-only)"
            except ValueError:
                pass  # Malformed shell — fall through to default

        if tool_name in ("system__write_file", "files__write", "files__delete") and args:
            path = args.get("path", "")
            if any(path.startswith(p) for p in _PROTECTED_PATHS):
                return ToolPermission.DENY, f"Protected path: {path}"

        # --- Plan overrides ---
        plan_lower = plan.lower() if plan else "free"
        overrides = _PLAN_OVERRIDES.get(plan_lower, {})
        if tool_name in overrides:
            perm = overrides[tool_name]
            reason = f"plan '{plan_lower}' restriction" if perm == ToolPermission.DENY else ""
            return perm, reason

        # --- Default policy ---
        if tool_name in _ALLOW_TOOLS:
            return ToolPermission.ALLOW, ""
        if any(tool_name.startswith(p) for p in _ALLOW_PREFIXES):
            return ToolPermission.ALLOW, ""
        if tool_name in _ASK_TOOLS:
            return ToolPermission.ASK, f"{tool_name} requires approval"
        if tool_name.startswith(_EXEC_PREFIX):
            if plan_lower == "free":
                return ToolPermission.DENY, "exec tools require a paid plan"
            return ToolPermission.ASK, f"{tool_name} requires approval"

        # Unknown / third-party module tools → ASK
        return ToolPermission.ASK, f"unknown tool '{tool_name}' requires approval"
