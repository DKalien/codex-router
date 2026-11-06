import subprocess
import sys
import tempfile
import os
from pathlib import Path
from unittest.mock import patch
import unittest

from code_sync import CODE_SYNC_RESULT_ENV, SyncResult, SyncStatus
from launcher import configure_merged_checkout, main, start_monitor
from process_utils import hidden_subprocess_kwargs


class TestLauncher(unittest.TestCase):
    def test_embedded_router_checkout_uses_lite_branch_without_overriding_explicit_values(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            project = root / "mimo-token-monitor"
            project.mkdir()
            (root / "package.json").write_text(
                '{"name":"codex-model-router"}', encoding="utf-8"
            )
            with patch.dict(os.environ, {}, clear=True):
                configure_merged_checkout(project)
                self.assertEqual(
                    Path(os.environ["MIMO_TOKEN_MONITOR_CODE_REPO_ROOT"]),
                    root,
                )
                self.assertEqual(
                    os.environ["MIMO_TOKEN_MONITOR_CODE_PROJECT_PATH"],
                    "mimo-token-monitor",
                )
                self.assertEqual(
                    os.environ["MIMO_TOKEN_MONITOR_CODE_GIT_BRANCH"],
                    "lite",
                )

    def test_embedded_router_checkout_disables_startup_sync_by_default(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            project = root / "mimo-token-monitor"
            project.mkdir()
            (root / "package.json").write_text(
                '{"name":"codex-model-router"}', encoding="utf-8"
            )
            with patch.dict(os.environ, {}, clear=True):
                configure_merged_checkout(project)
                self.assertEqual(
                    os.environ["MIMO_TOKEN_MONITOR_CODE_SYNC_ENABLED"], "0"
                )

    def test_embedded_router_checkout_keeps_explicit_sync_opt_in(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            project = root / "mimo-token-monitor"
            project.mkdir()
            (root / "package.json").write_text(
                '{"name":"codex-model-router"}', encoding="utf-8"
            )
            with patch.dict(
                os.environ,
                {"MIMO_TOKEN_MONITOR_CODE_SYNC_ENABLED": "1"},
                clear=True,
            ):
                configure_merged_checkout(project)
                self.assertEqual(
                    os.environ["MIMO_TOKEN_MONITOR_CODE_SYNC_ENABLED"], "1"
                )

    def test_start_monitor_passes_sync_result_to_child_environment(self):
        project_root = Path("C:/workspace/mimo-token-monitor")
        result = SyncResult(SyncStatus.SUCCESS, "code_pull", "代码已更新")
        with patch("launcher.subprocess.Popen") as popen:
            start_monitor(["python.exe"], project_root, result)

        kwargs = popen.call_args.kwargs
        expected_hidden = hidden_subprocess_kwargs()
        self.assertEqual(kwargs.get("cwd"), project_root)
        if sys.platform == "win32":
            self.assertEqual(
                kwargs.get("creationflags"),
                subprocess.CREATE_NO_WINDOW,
            )
            self.assertIsInstance(
                kwargs.get("startupinfo"), subprocess.STARTUPINFO,
            )
            self.assertTrue(
                kwargs["startupinfo"].dwFlags
                & subprocess.STARTF_USESHOWWINDOW,
            )
            self.assertEqual(
                kwargs["startupinfo"].wShowWindow,
                subprocess.SW_HIDE,
            )
        else:
            self.assertNotIn("creationflags", kwargs)
            self.assertNotIn("startupinfo", kwargs)
            self.assertEqual(expected_hidden, {})
        self.assertIn(CODE_SYNC_RESULT_ENV, kwargs["env"])
        self.assertIn("代码已更新", kwargs["env"][CODE_SYNC_RESULT_ENV])

    @patch("launcher.start_monitor")
    @patch("launcher.local_python_candidates", return_value=[["python.exe"]])
    @patch("launcher.run_startup_code_sync")
    @patch("launcher.find_project_root")
    def test_main_syncs_code_before_spawning_monitor(
        self, find_project_root, run_startup, _candidates, start_monitor
    ):
        project_root = Path("C:/workspace/mimo-token-monitor")
        result = SyncResult(SyncStatus.NO_CHANGE, "code_pull", "代码仓库已是最新")
        find_project_root.return_value = project_root
        run_startup.return_value = result

        self.assertEqual(main(), 0)

        run_startup.assert_called_once_with(project_root)
        start_monitor.assert_called_once_with(["python.exe"], project_root, result)

    @patch("launcher.find_project_root", return_value=None)
    def test_main_reports_missing_project_without_sync(self, _find):
        with patch("launcher.show_error") as show_error, patch(
            "launcher.run_startup_code_sync"
        ) as run_startup:
            self.assertEqual(main(), 1)

        show_error.assert_called_once()
        run_startup.assert_not_called()


if __name__ == "__main__":
    unittest.main()
