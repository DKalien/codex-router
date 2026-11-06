"""Small Windows launcher for the project-local MiMo Token Monitor source."""

from __future__ import annotations

import ctypes
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Sequence

from code_sync import (
    CODE_SYNC_RESULT_ENV,
    encode_sync_result,
    run_startup_code_sync,
)
from process_utils import hidden_subprocess_kwargs


APP_NAME = "MiMo Token Monitor"


def show_error(message: str) -> None:
    """Show errors even though the launcher itself is built without a console."""
    ctypes.windll.user32.MessageBoxW(None, message, APP_NAME, 0x10)


def find_project_root() -> Path | None:
    """Find the source directory when launched from the root or its dist folder."""
    launcher_path = Path(sys.executable if getattr(sys, "frozen", False) else __file__).resolve()
    for candidate in (launcher_path.parent, launcher_path.parent.parent):
        if (candidate / "main.py").is_file():
            return candidate
    return None


def configure_merged_checkout(project_root: Path) -> None:
    """Use the parent Router checkout and its ``lite`` branch when embedded."""
    package_path = project_root.parent / "package.json"
    try:
        package = json.loads(package_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return
    if package.get("name") != "codex-model-router":
        return
    os.environ.setdefault("MIMO_TOKEN_MONITOR_CODE_REPO_ROOT", str(project_root.parent))
    os.environ.setdefault("MIMO_TOKEN_MONITOR_CODE_PROJECT_PATH", project_root.name)
    os.environ.setdefault("MIMO_TOKEN_MONITOR_CODE_GIT_BRANCH", "lite")
    # 合并布局下启动监控器绝不隐式 fast-forward 整个 Router checkout：
    # 只有托盘「检查并更新代码」显式操作（或本变量显式设为 1）才同步。
    os.environ.setdefault("MIMO_TOKEN_MONITOR_CODE_SYNC_ENABLED", "0")


def local_python_candidates(project_root: Path) -> list[list[str]]:
    """Return Python commands in the order best suited for a local project."""
    candidates: list[list[str]] = []
    for name in ("pythonw.exe", "python.exe"):
        venv_python = project_root / ".venv" / "Scripts" / name
        if venv_python.is_file():
            candidates.append([str(venv_python)])

    if not getattr(sys, "frozen", False):
        candidates.append([sys.executable])

    for name in ("pythonw.exe", "python.exe"):
        executable = shutil.which(name)
        if executable:
            candidates.append([executable])

    py_launcher = shutil.which("py.exe") or shutil.which("py")
    if py_launcher:
        candidates.append([py_launcher, "-3"])

    unique: list[list[str]] = []
    seen: set[tuple[str, ...]] = set()
    for candidate in candidates:
        key = tuple(candidate)
        if key not in seen:
            seen.add(key)
            unique.append(candidate)
    return unique


def start_monitor(
    command: Sequence[str],
    project_root: Path,
    startup_code_sync_result=None,
) -> None:
    child_env = os.environ.copy()
    if startup_code_sync_result is not None:
        child_env[CODE_SYNC_RESULT_ENV] = encode_sync_result(
            startup_code_sync_result
        )
    subprocess.Popen(
        [*command, str(project_root / "main.py")],
        cwd=project_root,
        env=child_env,
        **hidden_subprocess_kwargs(),
    )


def main() -> int:
    project_root = find_project_root()
    if project_root is None:
        show_error(
            "找不到项目源码。请将此启动器保留在项目根目录或 dist 目录中，并确保 main.py 存在。"
        )
        return 1

    configure_merged_checkout(project_root)

    # Pull before spawning Python so the child imports the freshly updated
    # source files.  A failed/unsafe pull is passed to the GUI for a warning;
    # the existing local checkout remains the fallback.
    startup_code_sync_result = run_startup_code_sync(project_root)
    for command in local_python_candidates(project_root):
        try:
            start_monitor(command, project_root, startup_code_sync_result)
            return 0
        except OSError:
            continue

    show_error(
        "找不到可用的 Python 环境。请安装 Python 3，并在项目目录运行：\n"
        "python -m pip install -r requirements.txt"
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
