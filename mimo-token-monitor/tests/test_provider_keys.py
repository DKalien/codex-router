import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

import provider_keys


class TestProviderKeys(unittest.TestCase):
    def test_both_providers_reread_router_files_and_support_state_overrides(self):
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp) / "shared"
            state.mkdir()
            for provider, filename in (("wlb-relay", "wlb-api-key.secret"),
                                       ("supergpt-relay", "supergpt-api-key.secret")):
                for variable in ("MODEL_ROUTER_STATE_DIR", "CODEX_ROUTER_STATE_DIR", "KIMI_CODEX_STATE_DIR"):
                    with self.subTest(provider=provider, variable=variable), patch.dict(
                        os.environ, {"CODEX_HOME": temp, variable: str(state)}, clear=True
                    ):
                        (state / filename).write_text(" old-key \n", encoding="utf-8")
                        self.assertEqual(provider_keys.read_key(provider), "old-key")
                        (state / filename).write_text("new-key", encoding="utf-8")
                        self.assertEqual(provider_keys.read_key(provider), "new-key")

    def test_missing_file_environment_and_legacy_match_router_precedence(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {"CODEX_HOME": temp}, clear=True):
            self.assertEqual(provider_keys.read_key("wlb-relay"), "")
            legacy = Path(temp) / "kimi-router"
            legacy.mkdir()
            (legacy / "wlb-api-key.secret").write_text("legacy", encoding="utf-8")
            self.assertEqual(provider_keys.read_key("wlb-relay"), "legacy")
            with patch.dict(os.environ, {"WLB_API_KEY": " env-key "}):
                self.assertEqual(provider_keys.read_key("wlb-relay"), "env-key")

    def test_links_directories_and_unreadable_files_fail_safely(self):
        for mode in (stat.S_IFLNK, stat.S_IFDIR):
            with self.subTest(mode=mode), patch.dict(os.environ, {"CODEX_HOME": "unused"}, clear=True), \
                 patch.object(Path, "lstat", return_value=Mock(st_mode=mode)), \
                 patch.object(Path, "read_text") as read:
                with self.assertRaises(provider_keys.ProviderKeyError):
                    provider_keys.read_key("wlb-relay")
                read.assert_not_called()
        with patch.dict(os.environ, {"CODEX_HOME": "unused"}, clear=True), \
             patch.object(Path, "lstat", return_value=Mock(st_mode=stat.S_IFREG)), \
             patch.object(Path, "read_text", side_effect=PermissionError("secret-value")):
            with self.assertRaises(provider_keys.ProviderKeyError) as raised:
                provider_keys.read_key("supergpt-relay")
            self.assertNotIn("secret-value", str(raised.exception))

    def test_save_uses_router_writer_and_stdin_never_command_line(self):
        root = Path(tempfile.gettempdir()).resolve() / "router"
        with patch.dict(os.environ, {}, clear=True), \
             patch("provider_keys.resolve_router_root", return_value=root), \
             patch("provider_keys.subprocess.run", return_value=Mock(returncode=0, stdout='{"ok":true}')) as run:
            provider_keys.save_keys({"wlb-relay": " test-secret ", "supergpt-relay": ""})
        command = run.call_args.args[0]
        self.assertNotIn("test-secret", " ".join(command))
        self.assertEqual(json.loads(run.call_args.kwargs["input"]), {"wlb-relay": "test-secret"})
        self.assertTrue(command[-2].endswith("/src/provider-credentials.mjs"))
        self.assertIn("writeProviderCredential", command[3])
        self.assertIn("enableProvider", command[3])
        self.assertEqual(run.call_args.kwargs["cwd"], root)
        self.assertIn("timeout", run.call_args.kwargs)

    def test_blank_keys_do_nothing_and_conflicting_environment_is_rejected(self):
        with patch("provider_keys.subprocess.run") as run:
            provider_keys.save_keys({"wlb-relay": " "})
            with self.assertRaisesRegex(provider_keys.ProviderKeyError, "格式无效"):
                provider_keys.save_keys({"wlb-relay": None})
            with patch.dict(os.environ, {"WLB_API_KEY": "old-key"}):
                with self.assertRaisesRegex(provider_keys.ProviderKeyError, "环境变量"):
                    provider_keys.save_keys({"wlb-relay": "new-key"})
            run.assert_not_called()

    def test_partial_save_timeout_and_process_failures_never_expose_credentials(self):
        root = Path(tempfile.gettempdir()).resolve() / "router"
        outcomes = [Mock(returncode=1, stdout='{"ok":false,"saved":["wlb-relay"],"failed":"supergpt-relay"}'),
                    Mock(returncode=1, stdout="secret-value"),
                    subprocess.TimeoutExpired("secret-value", 50), FileNotFoundError("secret-value")]
        for outcome in outcomes:
            with self.subTest(outcome=type(outcome).__name__), patch.dict(os.environ, {}, clear=True), \
                 patch("provider_keys.resolve_router_root", return_value=root), \
                 patch("provider_keys.subprocess.run") as run:
                if isinstance(outcome, Exception):
                    run.side_effect = outcome
                else:
                    run.return_value = outcome
                with self.assertRaises(provider_keys.ProviderKeyError) as raised:
                    provider_keys.save_keys({"wlb-relay": "secret-value", "supergpt-relay": "second"})
                self.assertNotIn("secret-value", str(raised.exception))
                if not isinstance(outcome, Exception) and outcome.stdout.startswith('{'):
                    self.assertIn("WLB 已保存", str(raised.exception))
