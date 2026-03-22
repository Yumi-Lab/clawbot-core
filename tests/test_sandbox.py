"""
Unit tests for the sandbox permission engine.
stdlib-only — uses unittest, no pytest dependency.
"""

import json
import os
import sys
import tempfile
import unittest

# Add clawbot_core to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "clawbot_core"))

from sandbox.permissions import (
    ToolPermission, ToolPolicy, validate_safe_bin, sanitize_exec_env,
    _ALLOW_TOOLS, _ASK_TOOLS, _PROTECTED_PATHS,
)
from sandbox.approvals import ApprovalStore
from sandbox.manager import SandboxManager


# ── ToolPermission enum ──────────────────────────────────────────

class TestToolPermission(unittest.TestCase):
    def test_values(self):
        self.assertEqual(ToolPermission.ALLOW.value, "allow")
        self.assertEqual(ToolPermission.ASK.value, "ask")
        self.assertEqual(ToolPermission.DENY.value, "deny")

    def test_from_string(self):
        self.assertEqual(ToolPermission("allow"), ToolPermission.ALLOW)
        self.assertEqual(ToolPermission("ask"), ToolPermission.ASK)
        self.assertEqual(ToolPermission("deny"), ToolPermission.DENY)

    def test_members_count(self):
        self.assertEqual(len(ToolPermission), 3)


# ── ToolPolicy defaults ─────────────────────────────────────────

class TestToolPolicyDefaults(unittest.TestCase):
    def setUp(self):
        self.policy = ToolPolicy()

    def test_allow_tools(self):
        for tool in _ALLOW_TOOLS:
            perm, _ = self.policy.check(tool)
            self.assertEqual(perm, ToolPermission.ALLOW, f"{tool} should be ALLOW")

    def test_allow_prefix_documents(self):
        perm, _ = self.policy.check("documents__list")
        self.assertEqual(perm, ToolPermission.ALLOW)
        perm, _ = self.policy.check("documents__search")
        self.assertEqual(perm, ToolPermission.ALLOW)

    def test_ask_tools(self):
        for tool in _ASK_TOOLS:
            perm, reason = self.policy.check(tool)
            # Some tools may be DENY on free plan (email__send, system__ssh)
            if tool in ("email__send",):
                self.assertEqual(perm, ToolPermission.DENY, f"{tool} should be DENY on free")
            else:
                self.assertEqual(perm, ToolPermission.ASK, f"{tool} should be ASK, got {perm}")

    def test_ssh_deny_free(self):
        perm, reason = self.policy.check("system__ssh", plan="free")
        self.assertEqual(perm, ToolPermission.DENY)
        self.assertIn("plan", reason)

    def test_ssh_deny_perso(self):
        perm, _ = self.policy.check("system__ssh", plan="perso")
        self.assertEqual(perm, ToolPermission.DENY)

    def test_ssh_ask_pro(self):
        perm, _ = self.policy.check("system__ssh", plan="pro")
        self.assertEqual(perm, ToolPermission.ASK)

    def test_email_deny_free(self):
        perm, _ = self.policy.check("email__send", plan="free")
        self.assertEqual(perm, ToolPermission.DENY)

    def test_email_ask_pro(self):
        perm, _ = self.policy.check("email__send", plan="pro")
        self.assertEqual(perm, ToolPermission.ASK)

    def test_unknown_tool_asks(self):
        perm, reason = self.policy.check("some__unknown_tool")
        self.assertEqual(perm, ToolPermission.ASK)
        self.assertIn("unknown", reason)

    def test_exec_deny_free(self):
        perm, reason = self.policy.check("exec__run_python", plan="free")
        self.assertEqual(perm, ToolPermission.DENY)
        self.assertIn("paid", reason)

    def test_exec_ask_pro(self):
        perm, _ = self.policy.check("exec__run_python", plan="pro")
        self.assertEqual(perm, ToolPermission.ASK)


# ── ToolPolicy arg-level analysis ───────────────────────────────

class TestToolPolicyArgAnalysis(unittest.TestCase):
    def setUp(self):
        self.policy = ToolPolicy()

    def test_rm_rf_root_denied(self):
        perm, reason = self.policy.check("system__bash", {"command": "rm -rf /"})
        self.assertEqual(perm, ToolPermission.DENY)
        self.assertIn("Dangerous", reason)

    def test_rm_rf_star_denied(self):
        perm, _ = self.policy.check("system__bash", {"command": "rm -rf /*"})
        self.assertEqual(perm, ToolPermission.DENY)

    def test_chmod_777_root_denied(self):
        perm, _ = self.policy.check("system__bash", {"command": "chmod 777 /"})
        self.assertEqual(perm, ToolPermission.DENY)

    def test_dd_dev_denied(self):
        perm, _ = self.policy.check("system__bash", {"command": "dd if=/dev/zero of=/dev/sda"})
        self.assertEqual(perm, ToolPermission.DENY)

    def test_mkfs_denied(self):
        perm, _ = self.policy.check("system__bash", {"command": "mkfs.ext4 /dev/sda1"})
        self.assertEqual(perm, ToolPermission.DENY)

    def test_systemctl_stop_protected_denied(self):
        perm, _ = self.policy.check("system__bash", {"command": "systemctl stop clawbot-core"})
        self.assertEqual(perm, ToolPermission.DENY)

    def test_systemctl_restart_protected_denied(self):
        perm, _ = self.policy.check("system__bash", {"command": "systemctl restart nginx"})
        self.assertEqual(perm, ToolPermission.DENY)

    def test_systemctl_non_protected_ask(self):
        perm, _ = self.policy.check("system__bash", {"command": "systemctl stop some-other-svc"})
        self.assertEqual(perm, ToolPermission.ASK)

    def test_bash_shell_unwrap(self):
        perm, _ = self.policy.check("system__bash", {"command": "bash -c 'rm -rf /'"})
        self.assertEqual(perm, ToolPermission.DENY)

    def test_safe_cmd_ls(self):
        perm, _ = self.policy.check("system__bash", {"command": "ls -la"})
        self.assertEqual(perm, ToolPermission.ASK)

    def test_safe_bin_grep_allow(self):
        perm, reason = self.policy.check("system__bash", {"command": "grep error"})
        self.assertEqual(perm, ToolPermission.ALLOW)
        self.assertIn("safe bin", reason)

    def test_safe_bin_head_allow(self):
        perm, _ = self.policy.check("system__bash", {"command": "head -n 10"})
        self.assertEqual(perm, ToolPermission.ALLOW)

    def test_safe_bin_jq_allow(self):
        perm, _ = self.policy.check("system__bash", {"command": "jq '.data'"})
        self.assertEqual(perm, ToolPermission.ALLOW)

    def test_protected_path_write_denied(self):
        perm, reason = self.policy.check("system__write_file", {"path": "/etc/hosts"})
        self.assertEqual(perm, ToolPermission.DENY)
        self.assertIn("Protected", reason)

    def test_protected_path_delete_denied(self):
        perm, _ = self.policy.check("files__delete", {"path": "/usr/bin/python3"})
        self.assertEqual(perm, ToolPermission.DENY)

    def test_safe_path_write_ask(self):
        perm, _ = self.policy.check("system__write_file", {"path": "/home/pi/test.txt"})
        self.assertEqual(perm, ToolPermission.ASK)


# ── validate_safe_bin ────────────────────────────────────────────

class TestValidateSafeBin(unittest.TestCase):
    def test_empty(self):
        self.assertFalse(validate_safe_bin([]))

    def test_unknown_bin(self):
        self.assertFalse(validate_safe_bin(["vim"]))

    def test_grep_pattern_only(self):
        self.assertTrue(validate_safe_bin(["grep", "error"]))

    def test_grep_with_recursive_denied(self):
        self.assertFalse(validate_safe_bin(["grep", "-r", "error"]))

    def test_grep_with_R_denied(self):
        self.assertFalse(validate_safe_bin(["grep", "-R", "error"]))

    def test_grep_with_file_flag_denied(self):
        self.assertFalse(validate_safe_bin(["grep", "-f", "patterns.txt"]))

    def test_head_no_args(self):
        self.assertTrue(validate_safe_bin(["head"]))

    def test_head_with_n(self):
        self.assertTrue(validate_safe_bin(["head", "-n", "10"]))

    def test_head_with_file_positional(self):
        self.assertFalse(validate_safe_bin(["head", "file.txt"]))

    def test_sort_no_args(self):
        self.assertTrue(validate_safe_bin(["sort"]))

    def test_sort_output_denied(self):
        self.assertFalse(validate_safe_bin(["sort", "-o", "out.txt"]))

    def test_jq_filter(self):
        self.assertTrue(validate_safe_bin(["jq", ".data"]))

    def test_jq_argfile_denied(self):
        self.assertFalse(validate_safe_bin(["jq", "--argfile", "x", "f.json"]))

    def test_tr_two_positional(self):
        self.assertTrue(validate_safe_bin(["tr", "a-z", "A-Z"]))

    def test_tr_three_positional(self):
        self.assertFalse(validate_safe_bin(["tr", "a", "b", "c"]))

    def test_wc_no_args(self):
        self.assertTrue(validate_safe_bin(["wc"]))

    def test_wc_with_file(self):
        self.assertFalse(validate_safe_bin(["wc", "file.txt"]))

    def test_double_dash(self):
        self.assertFalse(validate_safe_bin(["head", "--", "file.txt"]))


# ── sanitize_exec_env ────────────────────────────────────────────

class TestSanitizeExecEnv(unittest.TestCase):
    def test_blocks_path(self):
        os.environ["PATH"] = "/usr/bin"
        env = sanitize_exec_env()
        self.assertNotIn("PATH", env)

    def test_blocks_ld_preload(self):
        os.environ["LD_PRELOAD"] = "/evil.so"
        env = sanitize_exec_env()
        self.assertNotIn("LD_PRELOAD", env)
        del os.environ["LD_PRELOAD"]

    def test_blocks_prefix_aws(self):
        os.environ["AWS_SECRET_KEY"] = "secret"
        env = sanitize_exec_env()
        self.assertNotIn("AWS_SECRET_KEY", env)
        del os.environ["AWS_SECRET_KEY"]

    def test_blocks_prefix_kube(self):
        os.environ["KUBE_CONTEXT"] = "dev"
        env = sanitize_exec_env()
        self.assertNotIn("KUBE_CONTEXT", env)
        del os.environ["KUBE_CONTEXT"]

    def test_allows_normal_vars(self):
        os.environ["MY_APP_VAR"] = "hello"
        env = sanitize_exec_env()
        self.assertEqual(env.get("MY_APP_VAR"), "hello")
        del os.environ["MY_APP_VAR"]

    def test_overrides_respected(self):
        env = sanitize_exec_env(overrides={"CUSTOM": "value"})
        self.assertEqual(env["CUSTOM"], "value")

    def test_overrides_blocked_ignored(self):
        env = sanitize_exec_env(overrides={"PATH": "/evil", "AWS_KEY": "x"})
        self.assertNotIn("PATH", env)
        self.assertNotIn("AWS_KEY", env)


# ── ApprovalStore ────────────────────────────────────────────────

class TestApprovalStore(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self._path = os.path.join(self._tmpdir, "approvals.json")
        self.store = ApprovalStore(path=self._path)

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_initial_state_empty(self):
        self.assertIsNone(self.store.get("system__bash"))

    def test_set_always_and_get(self):
        self.store.set("system__bash", ToolPermission.ALLOW, remember="always")
        self.assertEqual(self.store.get("system__bash"), ToolPermission.ALLOW)

    def test_set_session_and_get(self):
        self.store.set("system__bash", ToolPermission.ALLOW,
                       remember="session", session_id="s1")
        self.assertEqual(self.store.get("system__bash", session_id="s1"),
                         ToolPermission.ALLOW)
        self.assertIsNone(self.store.get("system__bash"))

    def test_set_never_noop(self):
        self.store.set("system__bash", ToolPermission.ALLOW, remember="never")
        self.assertIsNone(self.store.get("system__bash"))

    def test_is_approved(self):
        self.assertFalse(self.store.is_approved("system__bash", None))
        self.store.set("system__bash", ToolPermission.ALLOW, remember="always")
        self.assertTrue(self.store.is_approved("system__bash", None))

    def test_is_approved_deny(self):
        self.store.set("system__bash", ToolPermission.DENY, remember="always")
        self.assertFalse(self.store.is_approved("system__bash", None))

    def test_persistence_roundtrip(self):
        self.store.set("system__bash", ToolPermission.ALLOW, remember="always",
                       command="ls -la")
        store2 = ApprovalStore(path=self._path)
        self.assertEqual(store2.get("system__bash"), ToolPermission.ALLOW)

    def test_clear_session(self):
        self.store.set("system__bash", ToolPermission.ALLOW,
                       remember="session", session_id="s1")
        self.store.clear_session("s1")
        self.assertIsNone(self.store.get("system__bash", session_id="s1"))

    def test_clear_all(self):
        self.store.set("system__bash", ToolPermission.ALLOW, remember="always")
        self.store.set("files__write", ToolPermission.ALLOW,
                       remember="session", session_id="s1")
        self.store.clear_all()
        self.assertIsNone(self.store.get("system__bash"))
        self.assertIsNone(self.store.get("files__write", session_id="s1"))

    def test_permanent_takes_priority_over_session(self):
        self.store.set("system__bash", ToolPermission.ALLOW, remember="always")
        self.store.set("system__bash", ToolPermission.DENY,
                       remember="session", session_id="s1")
        self.assertEqual(self.store.get("system__bash", session_id="s1"),
                         ToolPermission.ALLOW)

    def test_corrupt_file_handled(self):
        with open(self._path, "w") as f:
            f.write("not json at all")
        store = ApprovalStore(path=self._path)
        self.assertIsNone(store.get("system__bash"))

    def test_file_json_format(self):
        self.store.set("system__bash", ToolPermission.ALLOW, remember="always",
                       command="test cmd")
        with open(self._path) as f:
            data = json.load(f)
        self.assertEqual(data["version"], 1)
        self.assertIn("system__bash", data["tools"])
        self.assertEqual(data["tools"]["system__bash"]["permission"], "allow")


# ── SandboxManager ───────────────────────────────────────────────

class TestSandboxManager(unittest.TestCase):
    def setUp(self):
        SandboxManager._reset()
        self._tmpdir = tempfile.mkdtemp()
        self._path = os.path.join(self._tmpdir, "approvals.json")
        self.mgr = SandboxManager(approval_path=self._path)

    def tearDown(self):
        SandboxManager._reset()
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_allow_tool_passes(self):
        perm, _ = self.mgr.evaluate("system__read_file", None)
        self.assertEqual(perm, ToolPermission.ALLOW)

    def test_deny_tool_blocks(self):
        perm, reason = self.mgr.evaluate("system__ssh", None, plan="free")
        self.assertEqual(perm, ToolPermission.DENY)

    def test_ask_tool_returns_ask(self):
        perm, reason = self.mgr.evaluate("system__bash", {"command": "apt update"})
        self.assertEqual(perm, ToolPermission.ASK)
        self.assertIn("approval", reason)

    def test_pre_approved_converts_ask_to_allow(self):
        self.mgr.record_decision("system__bash", ToolPermission.ALLOW,
                                 remember="always")
        perm, reason = self.mgr.evaluate("system__bash", {"command": "apt update"})
        self.assertEqual(perm, ToolPermission.ALLOW)
        self.assertEqual(reason, "pre-approved")

    def test_session_approval(self):
        self.mgr.record_decision("system__bash", ToolPermission.ALLOW,
                                 remember="session", session_id="s1")
        perm, reason = self.mgr.evaluate("system__bash", {"command": "apt update"},
                                         session_id="s1")
        self.assertEqual(perm, ToolPermission.ALLOW)
        self.assertEqual(reason, "pre-approved")

    def test_session_approval_not_in_other_session(self):
        self.mgr.record_decision("system__bash", ToolPermission.ALLOW,
                                 remember="session", session_id="s1")
        perm, _ = self.mgr.evaluate("system__bash", {"command": "apt update"},
                                    session_id="s2")
        self.assertEqual(perm, ToolPermission.ASK)

    def test_deny_overrides_pre_approval(self):
        self.mgr.record_decision("system__bash", ToolPermission.ALLOW,
                                 remember="always")
        perm, _ = self.mgr.evaluate("system__bash", {"command": "rm -rf /"})
        self.assertEqual(perm, ToolPermission.DENY)

    def test_plan_free_vs_pro(self):
        perm_free, _ = self.mgr.evaluate("system__ssh", None, plan="free")
        perm_pro, _ = self.mgr.evaluate("system__ssh", None, plan="pro")
        self.assertEqual(perm_free, ToolPermission.DENY)
        self.assertEqual(perm_pro, ToolPermission.ASK)

    def test_clear_session_works(self):
        self.mgr.record_decision("system__bash", ToolPermission.ALLOW,
                                 remember="session", session_id="s1")
        self.mgr.clear_session("s1")
        perm, _ = self.mgr.evaluate("system__bash", {"command": "apt update"},
                                    session_id="s1")
        self.assertEqual(perm, ToolPermission.ASK)

    def test_singleton(self):
        SandboxManager._reset()
        m1 = SandboxManager.get_instance(approval_path=self._path)
        m2 = SandboxManager.get_instance()
        self.assertIs(m1, m2)


if __name__ == "__main__":
    unittest.main()
