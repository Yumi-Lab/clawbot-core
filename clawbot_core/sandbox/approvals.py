"""
Persistent approval store — remembers user decisions across sessions.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from .permissions import ToolPermission

_DEFAULT_PATH = Path("/home/pi/.openjarvis/approvals.json")

_EMPTY_STORE = {"version": 1, "tools": {}, "session_approvals": {}}


class ApprovalStore:
    """Thread-safe persistent store for tool approval decisions."""

    def __init__(self, path: Path | str | None = None):
        self._path = Path(path) if path else _DEFAULT_PATH
        self._lock = threading.Lock()
        self._data: dict = {"version": 1, "tools": {}, "session_approvals": {}}
        self._load()

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _load(self) -> None:
        """Load from disk. Missing or corrupt file → start fresh."""
        try:
            if self._path.exists():
                raw = self._path.read_text(encoding="utf-8")
                data = json.loads(raw)
                if isinstance(data, dict) and data.get("version") == 1:
                    self._data = data
                    # Ensure sub-keys exist
                    self._data.setdefault("tools", {})
                    self._data.setdefault("session_approvals", {})
        except (json.JSONDecodeError, OSError):
            self._data = _EMPTY_STORE.copy()

    def save(self) -> None:
        """Write current state to disk."""
        with self._lock:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._data, indent=2), encoding="utf-8")
            tmp.replace(self._path)

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------

    def get(self, tool_name: str, session_id: str | None = None) -> ToolPermission | None:
        """Return stored permission or None if no decision recorded."""
        with self._lock:
            # Check permanent approvals first
            entry = self._data["tools"].get(tool_name)
            if entry:
                return ToolPermission(entry["permission"])

            # Check session approvals
            if session_id:
                sess = self._data["session_approvals"].get(session_id, {})
                entry = sess.get(tool_name)
                if entry:
                    return ToolPermission(entry["permission"])

        return None

    def is_approved(self, tool_name: str, args: dict | None,
                    session_id: str | None = None) -> bool:
        """Convenience: True if tool has an ALLOW decision stored."""
        perm = self.get(tool_name, session_id)
        return perm == ToolPermission.ALLOW

    # ------------------------------------------------------------------
    # Mutate
    # ------------------------------------------------------------------

    def set(self, tool_name: str, permission: ToolPermission,
            remember: str = "session", session_id: str | None = None,
            command: str | None = None) -> None:
        """
        Record a decision.
        remember: "always" → permanent, "session" → session-scoped, "never" → no-op
        """
        if remember == "never":
            return

        now = time.strftime("%Y-%m-%dT%H:%M:%S")

        with self._lock:
            if remember == "always":
                self._data["tools"][tool_name] = {
                    "permission": permission.value,
                    "last_used_at": now,
                    "last_command": (command or "")[:200],
                }
            elif remember == "session" and session_id:
                sess = self._data["session_approvals"].setdefault(session_id, {})
                sess[tool_name] = {
                    "permission": permission.value,
                    "granted_at": now,
                }

        self.save()

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def clear_session(self, session_id: str) -> None:
        """Remove all session-scoped approvals for given session."""
        with self._lock:
            self._data["session_approvals"].pop(session_id, None)
        self.save()

    def clear_all(self) -> None:
        """Reset store to empty (useful for tests)."""
        with self._lock:
            self._data = {"version": 1, "tools": {}, "session_approvals": {}}
        self.save()
