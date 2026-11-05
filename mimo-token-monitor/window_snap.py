"""Pure geometry + Win32 inter-window snap helper.

No Qt dependency.  Win32 calls are guarded; on non-Windows or import
failure every function degrades gracefully to a no-op.
"""

from __future__ import annotations

import math
import sys
from typing import List, Tuple

# -- Win32 helpers (Windows-only, fail-safe) --

_TARGET_TITLES = frozenset({"ETF Tracker", "MiMo Token Monitor"})


def get_other_window_rects(own_hwnd: int) -> List[Tuple[int, int, int, int]]:
    """Return (left, top, right, bottom) for every visible, non-iconic
    top-level window whose title is in _TARGET_TITLES except the
    window identified by own_hwnd.

    Returns an empty list on non-Windows, on any API failure, or when no
    qualifying window is found.
    """
    if sys.platform != "win32":
        return []

    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32

        EnumWindows = user32.EnumWindows
        EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
        IsWindowVisible = user32.IsWindowVisible
        IsIconic = user32.IsIconic
        GetWindowTextLengthW = user32.GetWindowTextLengthW
        GetWindowTextW = user32.GetWindowTextW
        GetWindowRect = user32.GetWindowRect
    except Exception:
        return []

    results: List[Tuple[int, int, int, int]] = []

    def _callback(hwnd: int, _lparam: int) -> bool:
        if hwnd == own_hwnd:
            return True
        if not IsWindowVisible(hwnd) or IsIconic(hwnd):
            return True
        length = GetWindowTextLengthW(hwnd)
        if length <= 0:
            return True
        buf = ctypes.create_unicode_buffer(length + 1)
        GetWindowTextW(hwnd, buf, length + 1)
        title = buf.value
        if title not in _TARGET_TITLES:
            return True
        rect = wintypes.RECT()
        if GetWindowRect(hwnd, ctypes.byref(rect)):
            results.append((rect.left, rect.top, rect.right, rect.bottom))
        return True

    try:
        EnumWindows(EnumWindowsProc(_callback), 0)
    except Exception:
        return []

    return results


# -- Coordinate-space conversion helpers ------------------------------------
#
# Qt uses logical (DPI-scaled) coordinates whose origin equals
# QScreen.geometry().topLeft().  Win32 GetWindowRect returns physical
# pixel coordinates in the virtual-desktop space.
#
# On the primary monitor dpr is typically 1.0 so the two spaces match.
# On a secondary monitor with dpr=2.0 the Qt origin is scaled:
#   physical = screen_origin + (qt_point - screen_origin) * dpr
#   qt       = screen_origin + (physical_point - screen_origin) / dpr
#
# screen_origin is QScreen.geometry().topLeft() (a Qt logical point that
# happens to equal the Win32 virtual-desktop origin of that monitor).


def normalize_dpr(dpr) -> float:
    """Return a usable device-pixel ratio for coordinate conversion."""
    try:
        value = float(dpr)
    except (TypeError, ValueError):
        return 1.0
    return value if math.isfinite(value) and value > 0 else 1.0


def qt_to_physical_position(position, screen_origin, dpr):
    """Convert a Qt logical top-left to a Win32 physical pixel top-left."""
    dpr = normalize_dpr(dpr)
    ox, oy = screen_origin
    return (
        round(ox + (position[0] - ox) * dpr),
        round(oy + (position[1] - oy) * dpr),
    )


def physical_to_qt_position(position, screen_origin, dpr):
    """Convert a Win32 physical pixel top-left to a Qt logical top-left."""
    dpr = normalize_dpr(dpr)
    ox, oy = screen_origin
    return (
        round(ox + (position[0] - ox) / dpr),
        round(oy + (position[1] - oy) / dpr),
    )


def qt_to_physical_size(size, dpr):
    """Convert a Qt logical size to a Win32 physical pixel size."""
    dpr = normalize_dpr(dpr)
    return (round(size[0] * dpr), round(size[1] * dpr))


# -- Pure geometry snap --


def _overlap_ok(
    own_lo: int, own_hi: int,
    other_lo: int, other_hi: int,
    threshold: int,
) -> bool:
    """True when the two 1-D intervals overlap or are within threshold."""
    return max(own_lo, other_lo) <= min(own_hi, other_hi) + threshold


def snap_position(
    position: Tuple[int, int],
    size: Tuple[int, int],
    target_rects: List[Tuple[int, int, int, int]],
    threshold: int = 15,
) -> Tuple[int, int]:
    """Return an adjusted (x, y) so that the window edges snap to edges of
    target_rects when within threshold pixels and perpendicular-axis overlap
    (or gap) is also within threshold.

    Supports four-edge mutual snapping:
      - left/right edges meeting (side by side)
      - top/bottom edges meeting (stacked)
      - same-direction alignment (left-left, right-right, top-top, bottom-bottom)
    """
    if not target_rects:
        return position

    x, y = position
    w, h = size
    best_dx = None
    best_dy = None
    best_abs_dx = threshold + 1
    best_abs_dy = threshold + 1

    for tl, tt, tr, tb in target_rects:
        # Vertical-edge snaps (adjust x)
        candidates_x = [
            (tl - x, abs(tl - x)),              # own left  -> target left  (left-left)
            (tr - x, abs(tr - x)),              # own left  -> target right (left->right)
            (tl - (x + w), abs(tl - (x + w))),  # own right -> target left  (right->left)
            (tr - (x + w), abs(tr - (x + w))),  # own right -> target right (right-right)
        ]
        for dx, abs_dx in candidates_x:
            if abs_dx <= best_abs_dx and abs_dx <= threshold:
                if _overlap_ok(y, y + h, tt, tb, threshold):
                    best_abs_dx = abs_dx
                    best_dx = dx

        # Horizontal-edge snaps (adjust y)
        candidates_y = [
            (tt - y, abs(tt - y)),              # own top    -> target top    (top-top)
            (tb - y, abs(tb - y)),              # own top    -> target bottom (top->bottom)
            (tt - (y + h), abs(tt - (y + h))),  # own bottom -> target top   (bottom->top)
            (tb - (y + h), abs(tb - (y + h))),  # own bottom -> target bottom (bottom-bottom)
        ]
        for dy, abs_dy in candidates_y:
            if abs_dy <= best_abs_dy and abs_dy <= threshold:
                if _overlap_ok(x, x + w, tl, tr, threshold):
                    best_abs_dy = abs_dy
                    best_dy = dy

    x_out = x + (best_dx if best_dx is not None else 0)
    y_out = y + (best_dy if best_dy is not None else 0)
    return (x_out, y_out)
