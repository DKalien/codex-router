import ctypes
import os
import sys
from ctypes import wintypes
from pathlib import Path

from PyQt6.QtCore import QProcess, QTimer
from PyQt6.QtWidgets import QApplication, QDialog, QMessageBox

from config import load_config, migrate_provider_keys, save_config
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

def initialize_window():
    cfg = load_config()
    try:
        migrate_provider_keys(cfg)
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
