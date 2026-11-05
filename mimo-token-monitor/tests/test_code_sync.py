from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from code_sync import CodeSyncConfig, CodeSyncService, SyncStatus, sanitize_detail
from process_utils import hidden_subprocess_kwargs


def run_git(cwd: Path, *args: str):
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )


class CodeRepoFixture:
    def __init__(self, root: Path):
        self.remote = root / "remote.git"
        self.repo = root / "checkout"
        self.project = self.repo / "mimo-token-monitor"
        self.other = self.repo / "other-project"
        subprocess.run(
            ["git", "init", "--bare", str(self.remote)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        )
        subprocess.run(
            ["git", "init", "-b", "main", str(self.repo)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        )
        run_git(self.repo, "config", "user.name", "MiMo Test")
        run_git(self.repo, "config", "user.email", "mimo@example.test")
        run_git(self.repo, "remote", "add", "origin", str(self.remote))
        self.project.mkdir(parents=True)
        self.other.mkdir(parents=True)
        (self.project / "main.py").write_text("remote-v1\n", encoding="utf-8")
        (self.other / "keep.txt").write_text("keep-v1\n", encoding="utf-8")
        run_git(self.repo, "add", ".")
        run_git(self.repo, "commit", "-m", "seed")
        run_git(self.repo, "push", "-u", "origin", "main")

    def config(self) -> CodeSyncConfig:
        return CodeSyncConfig(
            project_root=self.project.resolve(),
            repo_root=self.repo.resolve(),
            project_path="mimo-token-monitor",
            timeout_seconds=10,
        )


class TestCodeSync(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="mimo_code_sync_")
        self.fixture = CodeRepoFixture(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def _clone_and_push_remote_change(self, content: str):
        clone = Path(self.tmp.name) / "publisher"
        subprocess.run(
            ["git", "clone", "-b", "main", str(self.fixture.remote), str(clone)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        )
        run_git(clone, "config", "user.name", "Publisher")
        run_git(clone, "config", "user.email", "publisher@example.test")
        (clone / "mimo-token-monitor" / "main.py").write_text(content, encoding="utf-8")
        run_git(clone, "add", "mimo-token-monitor/main.py")
        run_git(clone, "commit", "-m", "remote code update")
        run_git(clone, "push", "origin", "main")

    def test_pull_fast_forwards_clean_checkout_before_start(self):
        self._clone_and_push_remote_change("remote-v2\n")

        result = CodeSyncService(self.fixture.config()).pull_latest()

        self.assertEqual(result.status, SyncStatus.SUCCESS)
        self.assertEqual(
            (self.fixture.project / "main.py").read_text(encoding="utf-8"),
            "remote-v2\n",
        )
        self.assertEqual(
            run_git(self.fixture.repo, "status", "--porcelain").stdout, b""
        )

    def test_pull_preserves_dirty_local_code(self):
        local_file = self.fixture.project / "main.py"
        local_file.write_text("local-edit\n", encoding="utf-8")
        self._clone_and_push_remote_change("remote-v2\n")

        result = CodeSyncService(self.fixture.config()).pull_latest()

        self.assertEqual(result.status, SyncStatus.SKIPPED)
        self.assertEqual(local_file.read_text(encoding="utf-8"), "local-edit\n")

    def test_push_commits_and_pushes_only_project_changes(self):
        local_file = self.fixture.project / "main.py"
        local_file.write_text("local-v2\n", encoding="utf-8")

        result = CodeSyncService(self.fixture.config()).push_local_changes()

        self.assertEqual(result.status, SyncStatus.SUCCESS)
        remote_head = (
            run_git(self.fixture.remote, "rev-parse", "refs/heads/main")
            .stdout.strip()
            .decode()
        )
        changed_paths = (
            run_git(
                self.fixture.remote,
                "diff-tree",
                "--no-commit-id",
                "--name-only",
                "-r",
                remote_head,
            )
            .stdout.decode()
            .splitlines()
        )
        self.assertEqual(changed_paths, ["mimo-token-monitor/main.py"])
        self.assertEqual(
            run_git(self.fixture.repo, "status", "--porcelain").stdout, b""
        )

    def test_push_skips_when_other_project_has_local_changes(self):
        (self.fixture.project / "main.py").write_text("local-v2\n", encoding="utf-8")
        other_file = self.fixture.other / "keep.txt"
        other_file.write_text("unrelated-local-edit\n", encoding="utf-8")
        before = run_git(self.fixture.remote, "rev-parse", "refs/heads/main").stdout

        result = CodeSyncService(self.fixture.config()).push_local_changes()

        self.assertEqual(result.status, SyncStatus.SKIPPED)
        self.assertEqual(
            run_git(self.fixture.remote, "rev-parse", "refs/heads/main").stdout,
            before,
        )
        self.assertIn("local-v2", (self.fixture.project / "main.py").read_text())
        self.assertIn("unrelated-local-edit", other_file.read_text())

    def test_push_does_not_overwrite_local_code_when_remote_moved(self):
        local_file = self.fixture.project / "main.py"
        local_file.write_text("local-v2\n", encoding="utf-8")
        self._clone_and_push_remote_change("remote-v2\n")
        before = run_git(self.fixture.repo, "rev-parse", "HEAD").stdout

        result = CodeSyncService(self.fixture.config()).push_local_changes()

        self.assertEqual(result.status, SyncStatus.FAILED)
        self.assertEqual(run_git(self.fixture.repo, "rev-parse", "HEAD").stdout, before)
        self.assertEqual(local_file.read_text(encoding="utf-8"), "local-v2\n")

    def test_no_project_change_does_not_create_commit(self):
        before = run_git(self.fixture.repo, "rev-parse", "HEAD").stdout

        result = CodeSyncService(self.fixture.config()).push_local_changes()

        self.assertEqual(result.status, SyncStatus.NO_CHANGE)
        self.assertEqual(run_git(self.fixture.repo, "rev-parse", "HEAD").stdout, before)

    def test_pushes_existing_local_project_commit(self):
        (self.fixture.project / "main.py").write_text(
            "local-committed\n", encoding="utf-8"
        )
        run_git(self.fixture.repo, "add", "mimo-token-monitor/main.py")
        run_git(self.fixture.repo, "commit", "-m", "local code commit")

        result = CodeSyncService(self.fixture.config()).push_local_changes()

        self.assertEqual(result.status, SyncStatus.SUCCESS)
        self.assertEqual(
            run_git(self.fixture.remote, "rev-parse", "refs/heads/main").stdout,
            run_git(self.fixture.repo, "rev-parse", "HEAD").stdout,
        )

    def test_error_details_hide_credentials(self):
        clean = sanitize_detail(
            "ssh://user:password@example.test token=abc Bearer secret-token"
        )

        self.assertNotIn("password", clean)
        self.assertNotIn("abc", clean)
        self.assertNotIn("secret-token", clean)


class TestHiddenSubprocessKwargs(unittest.TestCase):
    def test_git_runner_receives_hidden_window_kwargs(self):
        config = CodeSyncConfig(
            project_root=Path.cwd(),
            repo_root=Path.cwd().parent,
            project_path="mimo-token-monitor",
            timeout_seconds=5,
        )
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=b"ok", stderr=b"",
        )
        runner = Mock(return_value=completed)
        service = CodeSyncService(config, runner=runner)

        service._git("status")

        runner.assert_called_once()
        _, kwargs = runner.call_args
        expected = hidden_subprocess_kwargs()
        if sys.platform == "win32":
            self.assertEqual(
                kwargs.get("creationflags"), subprocess.CREATE_NO_WINDOW,
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
            self.assertEqual(expected, {})


class TestProcessUtilsHiddenSubprocessKwargs(unittest.TestCase):
    def test_windows_calls_return_distinct_startupinfo(self):
        if sys.platform != "win32":
            self.skipTest("仅在 Windows 下验证 STARTUPINFO")

        first = hidden_subprocess_kwargs()
        second = hidden_subprocess_kwargs()

        self.assertNotEqual(first, second)
        self.assertIsNot(first["startupinfo"], second["startupinfo"])
        self.assertEqual(
            first["creationflags"], subprocess.CREATE_NO_WINDOW,
        )
        self.assertEqual(
            second["creationflags"], subprocess.CREATE_NO_WINDOW,
        )
        self.assertTrue(
            first["startupinfo"].dwFlags & subprocess.STARTF_USESHOWWINDOW,
        )
        self.assertEqual(
            first["startupinfo"].wShowWindow, subprocess.SW_HIDE,
        )
        self.assertTrue(
            second["startupinfo"].dwFlags & subprocess.STARTF_USESHOWWINDOW,
        )
        self.assertEqual(
            second["startupinfo"].wShowWindow, subprocess.SW_HIDE,
        )

    def test_patched_linux_returns_empty_mapping(self):
        with patch("process_utils.sys.platform", "linux"):
            result = hidden_subprocess_kwargs()

        self.assertIsInstance(result, dict)
        self.assertEqual(result, {})


if __name__ == "__main__":
    unittest.main()
