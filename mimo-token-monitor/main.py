import ctypes
import os
import sys
from ctypes import wintypes
from pathlib import Path

from PyQt6.QtCore import QProcess, QTimer
from PyQt6.QtWidgets import QApplication, QDialog, QMessageBox

from code_sync import CODE_SYNC_RESULT_ENV, decode_sync_result
from config import load_config, migrate_provider_keys, resolve_key_migration, save_config
from provider_keys import ProviderKeyError
from widget import SettingsDialog, TokenWidget

MUTEX_NAME = "MiMoTokenMonitor_SingleInstance"
ACTIVATE_EVENT_NAME = "MiMoTokenMonitor_ActivateExistingInstance"
ERROR_ALREADY_EXISTS = 183
EVENT_MODIFY_STATE = 0x0002
WAIT_OBJECT_0 = 0

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
kernel32.CreateMutexW.argtypes = (wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR)
kernel32.CreateMutexW.restype = wintypes.HANDLE
kernel32.CreateEventW.argtypes = (
    wintypes.LPVOID,
    wintypes.BOOL,
    wintypes.BOOL,
    wintypes.LPCWSTR,
)
kernel32.CreateEventW.restype = wintypes.HANDLE
kernel32.OpenEventW.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR)
kernel32.OpenEventW.restype = wintypes.HANDLE
kernel32.SetEvent.argtypes = (wintypes.HANDLE,)
kernel32.SetEvent.restype = wintypes.BOOL
kernel32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
kernel32.WaitForSingleObject.restype = wintypes.DWORD
kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
kernel32.CloseHandle.restype = wintypes.BOOL

def check_single_instance():
    """Create the mutex, or return None when another process already owns it."""
    ctypes.set_last_error(0)
    mutex = kernel32.CreateMutexW(None, False, MUTEX_NAME)
    if not mutex:
        raise ctypes.WinError(ctypes.get_last_error())
    if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
        kernel32.CloseHandle(mutex)
        return None
    return mutex

def create_activation_event():
    """Create the event used by a later launch to wake this instance."""
    event = kernel32.CreateEventW(None, False, False, ACTIVATE_EVENT_NAME)
    if not event:
        raise ctypes.WinError(ctypes.get_last_error())
    return event

def activate_existing_instance() -> bool:
    """Ask the running instance to restore and focus its floating window."""
    event = kernel32.OpenEventW(EVENT_MODIFY_STATE, False, ACTIVATE_EVENT_NAME)
    if not event:
        return False
    try:
        return bool(kernel32.SetEvent(event))
    finally:
        kernel32.CloseHandle(event)

def activation_requested(event) -> bool:
    return kernel32.WaitForSingleObject(event, 0) == WAIT_OBJECT_0

def restart_application() -> bool:
    frozen = getattr(sys, "frozen", False)
    arguments = sys.argv[1:] if frozen else sys.argv
    working_directory = (
        Path(sys.executable).resolve().parent
        if frozen
        else Path(__file__).resolve().parent
    )
    started, _pid = QProcess.startDetached(
        sys.executable,
        arguments,
        str(working_directory),
    )
    return started

def _confirm_key_override(item: dict) -> bool:
    """旧值与 Router 现有密钥不同时让用户显式选择；默认保留现有值。"""
    reason = "（当前生效值来自环境变量覆盖）" if item.get("reason") == "env" else ""
    box = QMessageBox(
        QMessageBox.Icon.Question,
        "共享密钥迁移",
        f"{item['label']}：Router 中已有不同的生效密钥{reason}。\n"
        "旧配置中的值不会自动覆盖现有密钥。\n"
        "选择「用旧值覆盖」只替换密钥值，不改变供应商启用状态。",
        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
    )
    box.setDefaultButton(QMessageBox.StandardButton.No)
    box.button(QMessageBox.StandardButton.Yes).setText("用旧值覆盖")
    box.button(QMessageBox.StandardButton.No).setText("保留现有值（推荐）")
    return box.exec() == QMessageBox.StandardButton.Yes


def initialize_window():
    cfg = load_config()
    try:
        conflicts = migrate_provider_keys(cfg)
        for item in conflicts:
            overwrite = _confirm_key_override(item)
            try:
                resolve_key_migration(cfg, item["field"], overwrite=overwrite)
            except ProviderKeyError as exc:
                # 单项覆盖失败（如环境变量冲突）保留旧值供重试，不阻塞启动。
                QMessageBox.warning(None, "共享密钥迁移未完成", str(exc))
    except ProviderKeyError as exc:
        QMessageBox.critical(None, "共享密钥迁移失败", str(exc))
        return None

    if not cfg.get("cookie"):
        dlg = SettingsDialog(cfg)
        dlg.setWindowTitle("MiMo Token - 首次配置")
        if dlg.exec() == QDialog.DialogCode.Accepted:
            cfg = dlg.get_config()
            save_config(cfg)
        else:
            return None

    widget = TokenWidget(cfg)
    widget.show()
    return widget

def main() -> int:
    # pythonw 没有 stderr，Playwright 驱动需要持久有效的文件描述符。
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")

    mutex = check_single_instance()
    if mutex is None:
        activate_existing_instance()
        return 0

    activation_event = create_activation_event()
    restart_requested = False
    try:
        app = QApplication(sys.argv)
        app.setQuitOnLastWindowClosed(False)

        widget = initialize_window()
        if widget is None:
            return 0

        raw_sync_result = os.environ.pop(CODE_SYNC_RESULT_ENV, "")
        startup_sync_result = (
            decode_sync_result(raw_sync_result) if raw_sync_result else None
        )
        if startup_sync_result is not None:
            widget.show_startup_sync_result(startup_sync_result)

        activation_timer = QTimer()
        activation_timer.setInterval(250)

        def restore_existing_window():
            if not activation_requested(activation_event):
                return
            if hasattr(widget, "_show_window"):
                widget._show_window()
                return
            widget.show()
            widget.raise_()
            widget.activateWindow()

        activation_timer.timeout.connect(restore_existing_window)
        activation_timer.start()

        exit_code = app.exec()
        restart_requested = widget._restart_requested
    finally:
        if "activation_timer" in locals():
            activation_timer.stop()
        kernel32.CloseHandle(activation_event)
        kernel32.CloseHandle(mutex)

    if restart_requested and not restart_application():
        ctypes.windll.user32.MessageBoxW(
            None,
            "无法重新启动悬浮窗，请手动再次启动。",
            "MiMo Token Monitor",
            0x10,
        )
        return 1
    return exit_code

if __name__ == "__main__":
    raise SystemExit(main())
