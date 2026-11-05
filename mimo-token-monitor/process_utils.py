"""Small helpers for launching hidden child processes on Windows."""

from __future__ import annotations

import subprocess
import sys


def hidden_subprocess_kwargs() -> dict:
    """Return keyword arguments that hide spawned child windows.

    On Windows a fresh ``STARTUPINFO`` object is created for each call so
    concurrent callers never share mutable state.  On other platforms an
    empty dict is returned so callers can safely unpack the result into
    any ``subprocess`` call without guarding for the current OS.
    """

    if sys.platform != "win32":
        return {}

    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = subprocess.SW_HIDE

    return {
        "creationflags": subprocess.CREATE_NO_WINDOW,
        "startupinfo": startupinfo,
    }
