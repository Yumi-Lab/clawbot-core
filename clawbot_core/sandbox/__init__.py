"""
ClawbotCore Tool Sandboxing — Permission engine for tool execution.

Provides a 3-tier permission system (ALLOW / ASK / DENY) with:
- Default policies per tool, modulated by user plan
- Persistent approval store (~/.openjarvis/approvals.json)
- Safe-bin detection for stdin-only commands
- Arg-level analysis (dangerous patterns → force DENY)
- Env sanitization for sandboxed execution

Usage:
    from clawbot_core.sandbox import SandboxManager, ToolPermission

    sandbox = SandboxManager.get_instance()
    permission, reason = sandbox.evaluate("system__bash", {"command": "ls"}, session_id)
"""

from .permissions import ToolPermission, ToolPolicy, validate_safe_bin, sanitize_exec_env
from .approvals import ApprovalStore
from .manager import SandboxManager

__all__ = [
    "ToolPermission", "ToolPolicy", "ApprovalStore", "SandboxManager",
    "validate_safe_bin", "sanitize_exec_env",
]
