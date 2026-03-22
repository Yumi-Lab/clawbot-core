"""
SandboxManager — facade combining ToolPolicy + ApprovalStore.
"""
from __future__ import annotations

import threading

from .approvals import ApprovalStore
from .permissions import ToolPermission, ToolPolicy


class SandboxManager:
    """Singleton that evaluates tool permissions before execution."""

    _instance: "SandboxManager | None" = None
    _init_lock = threading.Lock()

    def __init__(self, approval_path: str | None = None):
        self._policy = ToolPolicy()
        self._store = ApprovalStore(path=approval_path)

    @classmethod
    def get_instance(cls, approval_path: str | None = None) -> "SandboxManager":
        if cls._instance is None:
            with cls._init_lock:
                if cls._instance is None:
                    cls._instance = cls(approval_path=approval_path)
        return cls._instance

    @classmethod
    def _reset(cls) -> None:
        """Reset singleton (for tests only)."""
        cls._instance = None

    def evaluate(self, tool_name: str, args: dict | None,
                 session_id: str | None = None,
                 plan: str = "free") -> tuple[ToolPermission, str]:
        """
        Main entry point. Returns (permission, reason).

        Flow:
        1. ToolPolicy.check() → base permission
        2. DENY → return immediately
        3. ALLOW → return immediately
        4. ASK → check ApprovalStore for prior decision
           - approved → ALLOW
           - else → ASK
        """
        perm, reason = self._policy.check(tool_name, args, plan)

        if perm == ToolPermission.DENY:
            return perm, reason

        if perm == ToolPermission.ALLOW:
            return perm, reason

        # perm == ASK — check if already approved
        if self._store.is_approved(tool_name, args, session_id):
            return ToolPermission.ALLOW, "pre-approved"

        return ToolPermission.ASK, reason

    def record_decision(self, tool_name: str, permission: ToolPermission,
                        remember: str = "session",
                        session_id: str | None = None,
                        command: str | None = None) -> None:
        """Record a user approval/denial decision."""
        self._store.set(tool_name, permission, remember, session_id, command)

    def clear_session(self, session_id: str) -> None:
        """Clean up session approvals."""
        self._store.clear_session(session_id)

    @property
    def store(self) -> ApprovalStore:
        return self._store
