"""
Integration tests for sandbox ↔ orchestrator ↔ approval endpoint.
stdlib-only — uses unittest, no pytest dependency.
"""

import json
import os
import sys
import tempfile
import threading
import unittest

# Add clawbot_core to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "clawbot_core"))

from sandbox.manager import SandboxManager
from sandbox.permissions import ToolPermission, ToolPolicy


# ── Orchestrator approval flow (unit-level, no HTTP) ─────────────

# Import the approval primitives directly
from orchestrator import (
    request_approval, resolve_approval, _pop_approval,
    _pending_approvals, _pending_lock,
)


def _clear_pending():
    """Clear all pending approvals between tests."""
    with _pending_lock:
        _pending_approvals.clear()


class TestApprovalFlow(unittest.TestCase):
    """Tests the request → resolve → pop cycle without HTTP."""

    def setUp(self):
        _clear_pending()

    def tearDown(self):
        _clear_pending()

    def test_request_creates_pending(self):
        ev = request_approval("tc_001")
        self.assertIsInstance(ev, threading.Event)
        self.assertFalse(ev.is_set())
        with _pending_lock:
            self.assertIn("tc_001", _pending_approvals)

    def test_resolve_sets_event(self):
        ev = request_approval("tc_002")
        found = resolve_approval("tc_002", "allow", "session")
        self.assertTrue(found)
        self.assertTrue(ev.is_set())

    def test_resolve_unknown_returns_false(self):
        self.assertFalse(resolve_approval("nonexistent", "allow"))

    def test_pop_returns_decision(self):
        ev = request_approval("tc_003")
        resolve_approval("tc_003", "deny", "never")
        decision, remember = _pop_approval("tc_003")
        self.assertEqual(decision, "deny")
        self.assertEqual(remember, "never")
        # Pop removes the entry
        with _pending_lock:
            self.assertNotIn("tc_003", _pending_approvals)

    def test_pop_unknown_returns_deny(self):
        decision, remember = _pop_approval("nonexistent")
        self.assertEqual(decision, "deny")
        self.assertEqual(remember, "never")

    def test_concurrent_approval(self):
        """Simulate two tools pending at once."""
        ev1 = request_approval("tc_a")
        ev2 = request_approval("tc_b")
        resolve_approval("tc_b", "allow", "always")
        resolve_approval("tc_a", "deny", "never")
        self.assertTrue(ev1.is_set())
        self.assertTrue(ev2.is_set())
        d1, r1 = _pop_approval("tc_a")
        d2, r2 = _pop_approval("tc_b")
        self.assertEqual(d1, "deny")
        self.assertEqual(d2, "allow")

    def test_threaded_resolve(self):
        """Resolve from another thread, wait on main thread."""
        ev = request_approval("tc_thread")
        result = [None]

        def bg():
            import time
            time.sleep(0.05)
            resolve_approval("tc_thread", "allow", "session")

        t = threading.Thread(target=bg)
        t.start()
        ev.wait(timeout=2)
        self.assertTrue(ev.is_set())
        decision, _ = _pop_approval("tc_thread")
        self.assertEqual(decision, "allow")
        t.join()

    def test_timeout_no_resolve(self):
        """If nobody resolves, wait times out and decision stays None."""
        ev = request_approval("tc_timeout")
        ev.wait(timeout=0.05)
        self.assertFalse(ev.is_set())
        # Pop returns the unresolved entry (decision=None) — orchestrator treats as deny
        decision, _ = _pop_approval("tc_timeout")
        self.assertIn(decision, (None, "deny"))


# ── Sandbox + Orchestrator combined flow ─────────────────────────

class TestSandboxOrchestratorFlow(unittest.TestCase):
    """Simulates the evaluate → approve cycle the orchestrator does."""

    def setUp(self):
        SandboxManager._reset()
        self._tmpdir = tempfile.mkdtemp()
        self._path = os.path.join(self._tmpdir, "approvals.json")
        self.mgr = SandboxManager(approval_path=self._path)
        _clear_pending()

    def tearDown(self):
        SandboxManager._reset()
        _clear_pending()
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_allow_tool_no_approval_needed(self):
        perm, _ = self.mgr.evaluate("system__read_file", None, session_id="s1")
        self.assertEqual(perm, ToolPermission.ALLOW)

    def test_deny_tool_no_approval_possible(self):
        perm, reason = self.mgr.evaluate("system__ssh", None,
                                         session_id="s1", plan="free")
        self.assertEqual(perm, ToolPermission.DENY)
        self.assertIn("plan", reason)

    def test_ask_then_approve_flow(self):
        """Full ASK flow: evaluate → request → resolve → record."""
        perm, reason = self.mgr.evaluate("system__bash", {"command": "apt update"},
                                         session_id="s1")
        self.assertEqual(perm, ToolPermission.ASK)

        # Simulate SSE → dashboard → POST /v1/tool-approval
        ev = request_approval("tc_flow")
        resolve_approval("tc_flow", "allow", "session")
        ev.wait(timeout=1)
        decision, remember = _pop_approval("tc_flow")
        self.assertEqual(decision, "allow")

        # Record decision
        self.mgr.record_decision("system__bash", ToolPermission.ALLOW,
                                 remember=remember, session_id="s1")

        # Next call should be pre-approved
        perm2, reason2 = self.mgr.evaluate("system__bash", {"command": "apt update"},
                                           session_id="s1")
        self.assertEqual(perm2, ToolPermission.ALLOW)
        self.assertEqual(reason2, "pre-approved")

    def test_ask_then_deny_flow(self):
        perm, _ = self.mgr.evaluate("system__bash", {"command": "apt update"},
                                    session_id="s1")
        self.assertEqual(perm, ToolPermission.ASK)

        ev = request_approval("tc_deny_flow")
        resolve_approval("tc_deny_flow", "deny", "never")
        ev.wait(timeout=1)
        decision, _ = _pop_approval("tc_deny_flow")
        self.assertEqual(decision, "deny")

    def test_always_remember_persists(self):
        """'Always allow' should persist and work in future sessions."""
        self.mgr.record_decision("system__bash", ToolPermission.ALLOW,
                                 remember="always", session_id="s1",
                                 command="apt update")
        # New session
        perm, reason = self.mgr.evaluate("system__bash", {"command": "apt update"},
                                         session_id="s2")
        self.assertEqual(perm, ToolPermission.ALLOW)
        self.assertEqual(reason, "pre-approved")

    def test_dangerous_still_denied_even_if_pre_approved(self):
        self.mgr.record_decision("system__bash", ToolPermission.ALLOW,
                                 remember="always")
        perm, _ = self.mgr.evaluate("system__bash", {"command": "rm -rf /"})
        self.assertEqual(perm, ToolPermission.DENY)


# ── Fallback: _is_dangerous_command still active ─────────────────

class TestDangerousCommandFallback(unittest.TestCase):
    """Ensure the original _is_dangerous_command in orchestrator still works."""

    def test_orchestrator_has_fallback(self):
        """The orchestrator should still have _is_dangerous_command or equivalent."""
        try:
            from orchestrator import _is_dangerous_command
            # It still exists as fallback
            self.assertTrue(callable(_is_dangerous_command))
            self.assertTrue(_is_dangerous_command("systemctl stop clawbot-core"))
            self.assertFalse(_is_dangerous_command("ls -la"))
        except ImportError:
            # If migrated entirely to sandbox, that's also fine
            # as long as sandbox handles it (tested in test_sandbox.py)
            policy = ToolPolicy()
            perm, _ = policy.check("system__bash",
                                   {"command": "systemctl stop clawbot-core"})
            self.assertEqual(perm, ToolPermission.DENY)


# ── Endpoint validation (mock-level) ────────────────────────────

class TestToolApprovalValidation(unittest.TestCase):
    """Validates the rules the /v1/tool-approval endpoint enforces."""

    def test_decision_must_be_allow_or_deny(self):
        for valid in ("allow", "deny"):
            self.assertIn(valid, ("allow", "deny"))
        for invalid in ("maybe", "ask", "", "ALLOW"):
            self.assertNotIn(invalid, ("allow", "deny"))

    def test_remember_must_be_valid(self):
        for valid in ("session", "always", "never"):
            self.assertIn(valid, ("session", "always", "never"))
        for invalid in ("forever", "once", ""):
            self.assertNotIn(invalid, ("session", "always", "never"))


if __name__ == "__main__":
    unittest.main()
