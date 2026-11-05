"""Synchronize the MiMo Token Monitor source project with its Git remote."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import time
from typing import Callable

from process_utils import hidden_subprocess_kwargs

_DEFAULT_PROJECT_PATH = "mimo-token-monitor"
_CODE_COMMIT_MESSAGE = "chore(mimo-token-monitor): 同步代码更新"
CODE_SYNC_RESULT_ENV = "MIMO_TOKEN_MONITOR_CODE_SYNC_RESULT"


class GitCommandError(RuntimeError):
    def __init__(self, message: str, detail: str = ""):
        super().__init__(message)
        self.detail = sanitize_detail(detail)


def sanitize_detail(detail: str) -> str:
    clean = re.sub(
        r"([A-Za-z][A-Za-z0-9+.-]*://)([^\s/@:]+):([^\s/@]+)@",
        r"\1***:***@",
        detail,
    )
    clean = re.sub(
        r"([A-Za-z][A-Za-z0-9+.-]*://)([^\s/@:]+)@",
        r"\1***@",
        clean,
    )
    sensitive_keys = (
        "token|access_token|password|passwd|api_key|apikey|secret|credential|"
        "auth|cookie|set-cookie|session|sessionid"
    )
    clean = re.sub(
        r"(?i)(\b(?:cookie|set-cookie)\s*:\s*)[^\r\n]+",
        r"\1***",
        clean,
    )
    clean = re.sub(
        r"(?i)(?<![\w])(remote\s+token)(\s+)[^\s,;&?#]+",
        r"\1\2***",
        clean,
    )
    clean = re.sub(
        rf"(?i)(?<![\w])({sensitive_keys})(\s*[=:]\s*)[^\s,;&?#]+",
        r"\1\2***",
        clean,
    )
    clean = re.sub(
        rf"(?i)([?#&](?:{sensitive_keys})=)[^&#\s]+",
        r"\1***",
        clean,
    )
    clean = re.sub(
        r"(Authorization:\s*Bearer\s+|(?<![\w])Bearer\s+)[^\s,;]+",
        r"\1***",
        clean,
        flags=re.I,
    )
    return clean[-2000:]


class SyncStatus(str, Enum):
    SUCCESS = "success"
    NO_CHANGE = "no_change"
    SKIPPED = "skipped"
    FAILED = "failed"


@dataclass(frozen=True)
class SyncResult:
    status: SyncStatus
    stage: str
    message: str
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.status in {SyncStatus.SUCCESS, SyncStatus.NO_CHANGE}


def _nonempty_env(name: str, default: str) -> str:
    value = os.environ.get(name, default).strip()
    if not value:
        raise ValueError(f"{name} 不能为空")
    return value


def _positive_int(name: str, default: int) -> int:
    raw = os.environ.get(name, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} 必须是正整数") from exc
    if value <= 0:
        raise ValueError(f"{name} 必须是正整数")
    return value


def _enabled_env(name: str, default: bool = True) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} 必须是布尔值")


def _validate_relative_path(path: str, field_name: str) -> str:
    normalized = PurePosixPath(path.replace("\\", "/"))
    if (
        not path
        or normalized.is_absolute()
        or "." in normalized.parts
        or ".." in normalized.parts
    ):
        raise ValueError(f"{field_name} 必须是仓库内的相对路径")
    return normalized.as_posix()


@dataclass(frozen=True)
class CodeSyncConfig:
    project_root: Path
    repo_root: Path
    project_path: str = _DEFAULT_PROJECT_PATH
    remote: str = "origin"
    branch: str = "main"
    timeout_seconds: int = 30
    enabled: bool = True

    @classmethod
    def from_project_root(cls, project_root: Path) -> "CodeSyncConfig":
        project_root = Path(project_root).resolve()
        configured_repo = os.environ.get("MIMO_TOKEN_MONITOR_CODE_REPO_ROOT")
        repo_root = (
            Path(configured_repo).resolve()
            if configured_repo and configured_repo.strip()
            else project_root.parent.resolve()
        )
        configured_path = os.environ.get("MIMO_TOKEN_MONITOR_CODE_PROJECT_PATH")
        if configured_path and configured_path.strip():
            project_path = _validate_relative_path(
                configured_path.strip(), "MIMO_TOKEN_MONITOR_CODE_PROJECT_PATH"
            )
        else:
            try:
                project_path = project_root.relative_to(repo_root).as_posix()
            except ValueError as exc:
                raise ValueError("代码项目目录不在代码仓库根目录内") from exc
            project_path = _validate_relative_path(project_path, "代码项目路径")

        if (repo_root / Path(*project_path.split("/"))).resolve() != project_root:
            raise ValueError("代码项目路径与代码仓库根目录不匹配")

        return cls(
            project_root=project_root,
            repo_root=repo_root,
            project_path=project_path,
            remote=_nonempty_env("MIMO_TOKEN_MONITOR_CODE_GIT_REMOTE", "origin"),
            branch=_nonempty_env("MIMO_TOKEN_MONITOR_CODE_GIT_BRANCH", "main"),
            timeout_seconds=_positive_int(
                "MIMO_TOKEN_MONITOR_CODE_GIT_TIMEOUT_SECONDS", 30
            ),
            enabled=_enabled_env("MIMO_TOKEN_MONITOR_CODE_SYNC_ENABLED"),
        )


class CodeSyncService:
    """Pull clean source trees and push only source-project changes."""

    def __init__(
        self,
        config: CodeSyncConfig,
        runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    ):
        self.config = config
        self._runner = runner

    @property
    def remote_ref(self) -> str:
        return f"refs/remotes/{self.config.remote}/{self.config.branch}"

    def _git(
        self,
        *args: str,
        input_bytes: bytes | None = None,
        env: dict | None = None,
        deadline: float | None = None,
    ) -> bytes:
        command = ["git", "-C", str(self.config.repo_root.resolve()), *args]
        if deadline is None:
            deadline = time.monotonic() + self.config.timeout_seconds
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise GitCommandError("Git 操作总预算已耗尽", "deadline exhausted")
        try:
            completed = self._runner(
                command,
                input=input_bytes,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=max(0.001, remaining),
                check=False,
                env=env,
                **hidden_subprocess_kwargs(),
            )
        except subprocess.TimeoutExpired as exc:
            raise GitCommandError("Git 命令超时", str(exc)) from exc
        except OSError as exc:
            raise GitCommandError("无法启动 Git", str(exc)) from exc
        if completed.returncode != 0:
            detail = completed.stderr.decode("utf-8", errors="replace")
            raise GitCommandError("Git 命令执行失败", detail)
        return completed.stdout

    def _validate_failure(self, exc: GitCommandError) -> SyncResult:
        status = (
            SyncStatus.FAILED
            if "超时" in str(exc) or "预算" in str(exc)
            else SyncStatus.SKIPPED
        )
        return SyncResult(status, "validate", str(exc), exc.detail)

    def validate_repository(self, deadline: float | None = None) -> SyncResult | None:
        cfg = self.config
        if not cfg.enabled:
            return SyncResult(SyncStatus.SKIPPED, "config", "代码仓库同步已禁用")
        if not cfg.project_root.is_dir():
            return SyncResult(SyncStatus.SKIPPED, "validate", "代码项目目录不存在")
        try:
            actual_root = Path(
                self._git("rev-parse", "--show-toplevel", deadline=deadline)
                .decode("utf-8", errors="replace")
                .strip()
            ).resolve()
            if actual_root != cfg.repo_root.resolve():
                return SyncResult(
                    SyncStatus.SKIPPED, "validate", "代码仓库根目录不匹配"
                )

            actual_branch = (
                self._git(
                    "symbolic-ref", "--quiet", "--short", "HEAD", deadline=deadline
                )
                .decode("utf-8", errors="replace")
                .strip()
            )
            if actual_branch != cfg.branch:
                return SyncResult(
                    SyncStatus.SKIPPED,
                    "validate",
                    f"当前代码分支为 {actual_branch or 'detached'}，未自动同步 {cfg.branch}",
                )
        except GitCommandError as exc:
            return self._validate_failure(exc)
        return None

    def _status(self, deadline: float, *pathspec: str) -> str:
        args = ["status", "--porcelain=v1", "--untracked-files=all"]
        if pathspec:
            args.extend(["--", *pathspec])
        return (
            self._git(*args, deadline=deadline)
            .decode("utf-8", errors="replace")
            .strip()
        )

    def _unrelated_status(self, deadline: float) -> str:
        excluded_project = f":(exclude){self.config.project_path}"
        return self._status(deadline, ".", excluded_project)

    def _unrelated_commits(self, remote_ref: str, deadline: float) -> str:
        excluded_project = f":(exclude){self.config.project_path}"
        return (
            self._git(
                "diff",
                "--name-only",
                f"{remote_ref}..HEAD",
                "--",
                ".",
                excluded_project,
                deadline=deadline,
            )
            .decode("utf-8", errors="replace")
            .strip()
        )

    def _fetch(self, deadline: float) -> None:
        self._git(
            "fetch",
            self.config.remote,
            f"{self.config.branch}:{self.remote_ref}",
            deadline=deadline,
        )

    def _relation(self, remote_ref: str, deadline: float) -> tuple[int, int]:
        output = (
            self._git(
                "rev-list",
                "--left-right",
                "--count",
                f"HEAD...{remote_ref}",
                deadline=deadline,
            )
            .decode("utf-8", errors="replace")
            .strip()
        )
        parts = output.split()
        if len(parts) != 2:
            raise GitCommandError("无法判断代码分支关系", output)
        try:
            ahead, behind = (int(parts[0]), int(parts[1]))
        except ValueError as exc:
            raise GitCommandError("无法判断代码分支关系", output) from exc
        return ahead, behind

    def pull_latest(self) -> SyncResult:
        """Fast-forward the clean project checkout before the app starts."""
        deadline = time.monotonic() + self.config.timeout_seconds
        invalid = self.validate_repository(deadline)
        if invalid is not None:
            return invalid
        try:
            if self._status(deadline):
                return SyncResult(
                    SyncStatus.SKIPPED,
                    "code_pull",
                    "代码仓库有未提交修改，已跳过自动拉取",
                    "工作区存在本地修改",
                )
            self._fetch(deadline)
            ahead, behind = self._relation(self.remote_ref, deadline)
            if ahead == 0 and behind == 0:
                return SyncResult(SyncStatus.NO_CHANGE, "code_pull", "代码仓库已是最新")
            if ahead == 0 and behind > 0:
                self._git("merge", "--ff-only", self.remote_ref, deadline=deadline)
                return SyncResult(
                    SyncStatus.SUCCESS, "code_pull", "已拉取代码仓库最新更新"
                )
            if ahead > 0 and behind == 0:
                return SyncResult(
                    SyncStatus.NO_CHANGE,
                    "code_pull",
                    "本地代码已有未推送提交，未执行拉取",
                )
            return SyncResult(
                SyncStatus.FAILED,
                "code_pull",
                "代码仓库本地与远端已分叉，未自动覆盖本地代码",
            )
        except (GitCommandError, OSError) as exc:
            detail = (
                exc.detail
                if isinstance(exc, GitCommandError)
                else sanitize_detail(str(exc))
            )
            return SyncResult(SyncStatus.FAILED, "code_pull", str(exc), detail)

    def push_local_changes(self) -> SyncResult:
        """Commit and push source-project changes without touching other paths."""
        deadline = time.monotonic() + self.config.timeout_seconds
        invalid = self.validate_repository(deadline)
        if invalid is not None:
            return invalid
        try:
            project_status = self._status(deadline, self.config.project_path)
            if project_status and self._unrelated_status(deadline):
                return SyncResult(
                    SyncStatus.SKIPPED,
                    "code_push",
                    "代码仓库存在项目目录外的本地修改，未自动提交",
                    "存在项目目录外的未提交修改",
                )

            self._fetch(deadline)
            ahead, behind = self._relation(self.remote_ref, deadline)
            if behind > 0 and ahead == 0:
                if project_status:
                    return SyncResult(
                        SyncStatus.FAILED,
                        "code_push",
                        "远端代码已有新提交，未自动合并本地修改",
                        "请先手动合并远端代码后再推送",
                    )
                return SyncResult(
                    SyncStatus.NO_CHANGE,
                    "code_push",
                    "远端代码已有更新，本地无需提交推送",
                )
            if behind > 0:
                return SyncResult(
                    SyncStatus.FAILED,
                    "code_push",
                    "远端代码已有新提交，未自动合并本地修改",
                    "请先手动合并远端代码后再推送",
                )
            if ahead > 0 and self._unrelated_commits(self.remote_ref, deadline):
                return SyncResult(
                    SyncStatus.SKIPPED,
                    "code_push",
                    "本地待推送提交包含项目目录外的改动，未自动推送",
                    "待推送提交包含项目目录外的改动",
                )

            if not project_status:
                if ahead == 0:
                    return SyncResult(
                        SyncStatus.NO_CHANGE,
                        "code_push",
                        "代码没有变化，无需提交推送",
                    )
                self._git(
                    "push",
                    self.config.remote,
                    f"HEAD:refs/heads/{self.config.branch}",
                    deadline=deadline,
                )
                return SyncResult(SyncStatus.SUCCESS, "code_push", "已推送本地代码提交")

            self._git("add", "--all", "--", self.config.project_path, deadline=deadline)
            staged = (
                self._git(
                    "diff",
                    "--cached",
                    "--name-only",
                    "--",
                    self.config.project_path,
                    deadline=deadline,
                )
                .decode("utf-8", errors="replace")
                .strip()
            )
            if not staged:
                return SyncResult(
                    SyncStatus.NO_CHANGE, "code_push", "代码没有变化，无需提交推送"
                )

            self._git(
                "commit",
                "-m",
                _CODE_COMMIT_MESSAGE,
                "--",
                self.config.project_path,
                deadline=deadline,
            )
            self._git(
                "push",
                self.config.remote,
                f"HEAD:refs/heads/{self.config.branch}",
                deadline=deadline,
            )
            return SyncResult(
                SyncStatus.SUCCESS, "code_push", "已提交并推送代码仓库更新"
            )
        except (GitCommandError, OSError) as exc:
            detail = (
                exc.detail
                if isinstance(exc, GitCommandError)
                else sanitize_detail(str(exc))
            )
            return SyncResult(SyncStatus.FAILED, "code_push", str(exc), detail)


def run_startup_code_sync(project_root: Path) -> SyncResult:
    try:
        config = CodeSyncConfig.from_project_root(project_root)
        return CodeSyncService(config).pull_latest()
    except (OSError, ValueError) as exc:
        return SyncResult(
            SyncStatus.SKIPPED, "config", str(exc), sanitize_detail(str(exc))
        )


def encode_sync_result(result: SyncResult) -> str:
    return json.dumps(
        {
            "status": result.status.value,
            "stage": result.stage,
            "message": result.message,
            "detail": result.detail,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def decode_sync_result(raw: str) -> SyncResult | None:
    try:
        data = json.loads(raw)
        return SyncResult(
            SyncStatus(data["status"]),
            str(data["stage"]),
            str(data["message"]),
            sanitize_detail(str(data.get("detail", ""))),
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None
