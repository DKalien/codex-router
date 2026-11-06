# encoding: utf-8
import os
import pathlib
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

# Ensure mimo-token-monitor package is importable
_root = pathlib.Path(__file__).resolve().parent.parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

# Isolate MIMO_TOKEN_MONITOR_DATA_DIR so save_config never writes production settings.db
_orig_data_dir = os.environ.get("MIMO_TOKEN_MONITOR_DATA_DIR")
_test_data_tmp = tempfile.mkdtemp(prefix="mimo_widget_test_")
os.environ["MIMO_TOKEN_MONITOR_DATA_DIR"] = _test_data_tmp


def setUpModule():
    """Ensure the data-dir env var points to a temp directory before any config import."""
    os.environ["MIMO_TOKEN_MONITOR_DATA_DIR"] = _test_data_tmp


def tearDownModule():
    """Restore the original env var and clean up the temp directory."""
    import shutil
    if _orig_data_dir is not None:
        os.environ["MIMO_TOKEN_MONITOR_DATA_DIR"] = _orig_data_dir
    else:
        os.environ.pop("MIMO_TOKEN_MONITOR_DATA_DIR", None)
    shutil.rmtree(_test_data_tmp, ignore_errors=True)


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QPoint, QRect, Qt  # noqa: E402
from PyQt6.QtGui import QFont, QFontMetrics, QFontDatabase  # noqa: E402
from PyQt6.QtWidgets import QApplication, QLineEdit, QMessageBox  # noqa: E402

# Ensure QApplication exists
_app = QApplication.instance() or QApplication(sys.argv)
if sys.platform == "win32":
    # 离屏平台需显式加载字体，才能验证 Windows 实际文本宽度。
    for font_name in ("msyh.ttc", "msyhbd.ttc"):
        font_path = pathlib.Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / font_name
        if font_path.is_file():
            QFontDatabase.addApplicationFont(str(font_path))

from widget import (  # noqa: E402
    SettingsDialog, TokenWidget, ThirdPartyFetchWorker, ProviderKeysWorker, PlaywrightCookieWorker,
    MIMO_MODE, THIRD_PARTY_MODE, OVERVIEW_MODE,
    BASE_HEIGHT, OVERVIEW_HEIGHT, DUAL_BAR_GAP,
    BG_COLOR, BAR_BG, BALANCE_LAYER_COLORS, TEXT_COLOR, ACCENT_GREEN, ACCENT_YELLOW, ACCENT_RED, DIM, SuperGPTBalanceWorker, _format_overview_expiry,
    _parse_reset_datetime, _format_overview_reset_time, _format_overview_reset_days,
)
from router_control import RouterResult  # noqa: E402


def _make_widget(**overrides):
    """Create a TokenWidget with safe defaults (no real network/config)."""
    cfg = {
        "display_mode": MIMO_MODE,
        "position": [200, 200],
        "always_on_top": False,
        "opacity": 1.0,
        "refresh_interval": 99999,
        "cookie": "",
        "third_party_api_key": "",
        "third_party_base_url": "http://example.com",
        "gpt_session_cookie": "",
    }
    cfg.update(overrides)
    w = TokenWidget(cfg)
    w._tp_key_configured = bool(cfg.get("third_party_api_key"))
    w._timer.stop()
    w._do_fetch_mimo = lambda: None
    w._do_fetch_third_party = lambda: None
    w._do_fetch_gpt = lambda: None
    w._do_fetch_supergpt_balance = lambda: None
    return w


class _OverviewPainter:
    """Small painter double for checking overview text and pen colors."""

    def __init__(self):
        self._pen = None
        self._font = QFont()
        self.text_records = []
        self.bar_records = []
        self._brush = None
        self._states = []

    def setPen(self, pen):
        self._pen = pen

    def setFont(self, font):
        self._font = QFont(font)

    def setBrush(self, brush):
        self._brush = brush

    def drawText(self, *args):
        rect = args[0] if isinstance(args[0], QRect) else None
        alignment = args[1] if rect is not None and len(args) == 3 else None
        self.text_records.append(
            (rect, args[-1], self._pen.color(), QFont(self._font), alignment)
        )

    def drawRoundedRect(self, *args):
        self.bar_records.append((args[:4], self._brush.color()))

    def save(self):
        self._states.append((self._pen, QFont(self._font), self._brush))

    def restore(self):
        self._pen, self._font, self._brush = self._states.pop()

    def setClipPath(self, _path):
        pass


class TestRemainingProgressBars(unittest.TestCase):
    # 已用比例、剩余比例与颜色，覆盖两个阈值及其相邻值。
    cases = (
        (0, 1.0, ACCENT_GREEN), (49.9, 0.501, ACCENT_GREEN),
        (50, 0.5, ACCENT_YELLOW), (50.1, 0.499, ACCENT_YELLOW),
        (79.9, 0.201, ACCENT_YELLOW), (80, 0.2, ACCENT_RED),
        (80.1, 0.199, ACCENT_RED), (100, 0.0, None),
        (-10, 1.0, ACCENT_GREEN), (120, 0.0, None),
    )

    def assert_bar(self, painter, x, y, width, height, fraction, color):
        records = [r for r in painter.bar_records if r[0][:2] == (x, y)]
        self.assertEqual(records[0], ((x, y, width, height), BAR_BG))
        self.assertEqual(len(records), 2 if fraction else 1)
        if fraction:
            self.assertEqual(records[1], ((x, y, int(width * fraction), height), color))

    def test_overview_all_quota_bars_use_remaining_width_and_color(self):
        w = _make_widget(display_mode=OVERVIEW_MODE, cookie="fake",
                         third_party_api_key="k", gpt_session_cookie="fake")
        try:
            for used, remaining, color in self.cases:
                with self.subTest(used=used):
                    w._plan_total, w._plan_used = 100, used
                    window = {"used_percent": used}
                    w._tp_data = {**window, "daily": window}
                    w._gpt_data = {**window, "primary": window, "secondary": window}
                    painter = _OverviewPainter()
                    w._paint_overview(painter)
                    for x, y in ((16, 44), (16, 78), (133, 78), (16, 112), (133, 112)):
                        self.assert_bar(painter, x, y, 111, 14, remaining, color)
        finally:
            w.close()

    def test_shared_detail_bar_uses_remaining_width_and_color(self):
        w = _make_widget()
        try:
            for used, remaining, color in self.cases:
                with self.subTest(used=used):
                    painter = _OverviewPainter()
                    w._draw_usage_progress_bar(painter, used / 100)
                    self.assert_bar(painter, 16, 50, 228, 14, remaining, color)
            for used in (None, float("nan"), float("inf"), -float("inf")):
                with self.subTest(unknown=used):
                    painter = _OverviewPainter()
                    w._draw_usage_progress_bar(painter, used)
                    self.assert_bar(painter, 16, 50, 228, 14, 0, None)
        finally:
            w.close()

    def test_missing_or_unknown_overview_windows_stay_empty(self):
        w = _make_widget(display_mode=OVERVIEW_MODE, cookie="fake",
                         third_party_api_key="k", gpt_session_cookie="fake")
        try:
            for window in (None, {}, {"used_percent": None},
                           {"used_percent": float("nan")}, {"used_percent": float("inf")},
                           {"used_percent": "unknown"},
                           {"used_percent": 0, "has_rate_limit": False}):
                with self.subTest(window=window):
                    w._plan_total = 0
                    w._tp_data = {"daily": window, "has_rate_limit": False, "used_percent": 0}
                    w._gpt_data = {"primary": window, "secondary": window}
                    painter = _OverviewPainter()
                    w._paint_overview(painter)
                    for x, y in ((16, 44), (16, 78), (133, 78), (16, 112), (133, 112)):
                        self.assert_bar(painter, x, y, 111, 14, 0, None)
        finally:
            w.close()

    def test_wlb_detail_unknown_data_stays_empty_and_known_data_matches_label(self):
        w = _make_widget(display_mode=THIRD_PARTY_MODE)
        try:
            for data in (None, {}, {"used_percent": 0, "has_rate_limit": False},
                         {"used_percent": float("nan")}, {"used_percent": 80}):
                with self.subTest(data=data):
                    w._tp_data = data
                    painter = _OverviewPainter()
                    w._paint_third_party(painter)
                    known = data == {"used_percent": 80}
                    self.assert_bar(painter, 16, 50, 228, 14,
                                    0.2 if known else 0, ACCENT_RED if known else None)
                    self.assertEqual(painter.text_records[0][1],
                                     "剩余 20.00%" if known else "剩余 --")
        finally:
            w.close()


class TestModeCycle(unittest.TestCase):
    """Mode cycle order: mimo -> third_party -> overview -> mimo."""

    def test_cycle_order(self):
        w = _make_widget(display_mode=MIMO_MODE)
        self.assertEqual(w.cfg["display_mode"], MIMO_MODE)
        w._toggle_display_mode()
        self.assertEqual(w.cfg["display_mode"], THIRD_PARTY_MODE)
        w._toggle_display_mode()
        self.assertEqual(w.cfg["display_mode"], OVERVIEW_MODE)
        w._toggle_display_mode()
        self.assertEqual(w.cfg["display_mode"], MIMO_MODE)
        w.close()

    def test_cycle_from_overview(self):
        w = _make_widget(display_mode=OVERVIEW_MODE)
        w._toggle_display_mode()
        self.assertEqual(w.cfg["display_mode"], MIMO_MODE)
        w.close()

    def test_switch_button_mouse_move_does_not_drag_window(self):
        w = _make_widget(display_mode=MIMO_MODE)
        w.move(200, 200)
        w._switch_rect = QRect(0, 0, 20, 20)

        press = Mock()
        press.button.return_value = Qt.MouseButton.LeftButton
        press.pos.return_value = QPoint(10, 10)
        w.mousePressEvent(press)

        move = Mock()
        move.buttons.return_value = Qt.MouseButton.LeftButton
        move.position.return_value.toPoint.return_value = QPoint(11, 10)
        w.mouseMoveEvent(move)

        self.assertEqual((w.x(), w.y()), (200, 200))
        w.close()


class TestOverviewFetchDispatch(unittest.TestCase):
    """Overview refresh dispatches both real sources without network calls in the UI thread."""

    def test_overview_starts_both_sources(self):
        w = _make_widget(display_mode=OVERVIEW_MODE, cookie="fake", third_party_api_key="k")
        calls = []
        w._do_fetch_mimo = lambda: calls.append("mimo")
        w._do_fetch_third_party = lambda: calls.append("third_party")
        w._do_fetch_gpt = lambda: calls.append("gpt")

        w._do_fetch()

        self.assertEqual(calls, ["mimo", "third_party", "gpt"])
        w.close()


    def test_refresh_attempt_updates_timestamp_before_dispatch(self):
        w = _make_widget(display_mode=OVERVIEW_MODE)
        calls = []
        w._do_fetch_mimo = lambda: calls.append(("mimo", w._last_update))
        w._do_fetch_third_party = lambda: calls.append(("third_party", w._last_update))
        w._do_fetch_gpt = lambda: calls.append(("gpt", w._last_update))

        with patch("widget.datetime") as fake_datetime:
            fake_datetime.now.return_value.strftime.return_value = "13:14:15"
            w._do_fetch()

        self.assertEqual(w._last_update, "13:14:15")
        self.assertEqual(
            calls,
            [
                ("mimo", "13:14:15"),
                ("third_party", "13:14:15"),
                ("gpt", "13:14:15"),
            ],
        )
        w.close()

    def test_configured_refresh_interval_is_applied_to_timer(self):
        w = _make_widget(refresh_interval=3600)
        self.assertEqual(w._timer.interval(), 3_600_000)
        w.close()


class TestPlaywrightRecovery(unittest.TestCase):
    def test_expired_api_cookie_requires_a_changed_playwright_cookie(self):
        w = _make_widget(cookie="stale", playwright_auto_refresh=True)
        w._refresh_playwright_cookie = Mock()

        w._on_fetch_done(
            {"ok": False, "balance": None, "error": "Cookie 已过期，请重新获取", "auth_expired": True},
            {"ok": False, "data": None, "error": "Cookie 已过期，请重新获取", "auth_expired": True},
        )

        w._refresh_playwright_cookie.assert_called_once_with(
            interactive=False,
            require_cookie_change=True,
        )
        w.close()

    def test_403_does_not_trigger_renewal_and_both_endpoint_errors_are_visible(self):
        w = _make_widget(cookie="fake", playwright_auto_refresh=True)
        w._refresh_playwright_cookie = Mock()
        w._on_fetch_done(
            {"ok": False, "error": "balance HTTP 403 Cookie 过期未知", "auth_expired": False},
            {"ok": False, "error": "tokenPlan/usage HTTP 403", "auth_expired": False},
        )
        w._refresh_playwright_cookie.assert_not_called()
        self.assertIn("balance HTTP 403", w._mimo_error)
        self.assertIn("tokenPlan/usage HTTP 403", w.toolTip())
        w.close()

    def test_manual_validator_waits_only_for_confirmed_401(self):
        for result, expected in (({"ok": True}, True),
                                 ({"ok": False, "auth_expired": True}, False)):
            with patch("widget.api_client.fetch_balance", return_value=result):
                self.assertIs(PlaywrightCookieWorker._cookie_is_usable("fake"), expected)
        for reason in ("balance HTTP 403", "TLS 连接失败"):
            with patch("widget.api_client.fetch_balance", return_value={"ok": False, "error": reason}):
                with self.assertRaisesRegex(Exception, reason):
                    PlaywrightCookieWorker._cookie_is_usable("fake")

    def test_quit_keeps_running_playwright_worker_alive(self):
        w = _make_widget(cookie="fake")
        worker = Mock()
        worker.isRunning.return_value = True
        w._playwright_worker = worker
        with patch.object(w._tray_icon, "showMessage") as message:
            w._quit_app()
        self.assertFalse(w._exit_requested)
        self.assertIs(w._playwright_worker, worker)
        message.assert_called_once()
        w._playwright_worker = None
        w.close()

    def test_worker_validates_both_headless_and_headed_cookies(self):
        for interactive in (False, True):
            worker = PlaywrightCookieWorker(interactive=interactive, previous_cookie="fake-old")
            with patch("widget.playwright_session.refresh_cookie", return_value=(None, "test error")) as refresh:
                worker.run()
            options = refresh.call_args.kwargs
            self.assertIs(options["interactive"], interactive)
            self.assertEqual(options["timeout_seconds"], 180 if interactive else 30)
            self.assertEqual(options["previous_cookie"], "fake-old")
            self.assertIs(options["cookie_validator"], PlaywrightCookieWorker._cookie_is_usable)

    def test_recovery_is_not_rearmed_until_both_api_calls_succeed(self):
        w = _make_widget(cookie="fake", playwright_auto_refresh=True)
        w._refresh_playwright_cookie = Mock()
        expired = {"ok": False, "error": "HTTP 401", "auth_expired": True}
        success = {"ok": True, "balance": 0, "data": None}
        w._on_fetch_done(success, expired)
        w._do_fetch = Mock()
        with patch("widget.save_config"):
            w._on_playwright_cookie_done("fake-new", None, False)
        w._on_fetch_done(success, expired)
        self.assertEqual(w._refresh_playwright_cookie.call_count, 1)
        w._on_fetch_done(success, success)
        w._on_fetch_done(expired, expired)
        self.assertEqual(w._refresh_playwright_cookie.call_count, 2)
        w.close()

    def test_playwright_success_is_silent_and_failure_warns(self):
        w = _make_widget(cookie="stale")
        w._do_fetch = Mock()
        w._playwright_worker = Mock()

        with patch("widget.QMessageBox") as message_box:
            message_box.Icon.Warning = object()
            message_box.StandardButton.Ok = object()
            box = Mock()
            message_box.return_value = box
            w._on_playwright_cookie_done("fresh", None, False)
            message_box.assert_not_called()
            self.assertEqual(w.cfg["cookie"], "fresh")
            self.assertIn("续期成功", w._router_status)
            # Saved cookie is now "fresh"; make the validity probe fail so we
            # exercise the hard-failure path (expired / unusable cookie).
            with patch.object(w, "_current_cookie_still_works", return_value=False):
                w._on_playwright_cookie_done(None, "refresh failed", False)

        message_box.assert_called_once()
        box.exec.assert_called_once()
        self.assertIn("refresh failed", w._router_status)
        self.assertIs(w._router_status_ok, False)
        w.close()

    def test_auto_renew_failure_is_soft_when_cookie_still_works(self):
        w = _make_widget(cookie="still-valid")
        w._do_fetch = Mock()
        w._playwright_worker = Mock()

        with patch.object(w, "_current_cookie_still_works", return_value=True), patch(
            "widget.QMessageBox"
        ) as message_box, patch.object(w._tray_icon, "showMessage") as show_message:
            message_box.Icon.Warning = object()
            message_box.StandardButton.Ok = object()
            w._on_playwright_cookie_done(None, "未找到 xiaomimimo.com Cookie", False)

        message_box.assert_not_called()
        show_message.assert_called_once()
        self.assertIn("仍可用", w._router_status)
        self.assertIs(w._router_status_ok, True)
        w.close()

    def test_interactive_renew_is_blocked_while_worker_runs(self):
        w = _make_widget(cookie="stale")
        running_worker = Mock()
        running_worker.isRunning.return_value = True
        w._playwright_worker = running_worker

        with patch.object(w, "_set_router_status") as set_status, patch.object(
            w._tray_icon, "showMessage"
        ) as show_message:
            w._refresh_playwright_cookie(interactive=True)

        set_status.assert_called_once()
        self.assertIn("已有 Playwright 续期任务", set_status.call_args[0][0])
        show_message.assert_called_once()
        w._playwright_worker = None
        w.close()


class TestHeightSwitch(unittest.TestCase):
    """Overview uses the same compact height as the other display modes."""

    def test_base_height(self):
        w = _make_widget(display_mode=MIMO_MODE)
        self.assertEqual(w.height(), BASE_HEIGHT)
        w.close()

    def test_overview_height(self):
        w = _make_widget(display_mode=OVERVIEW_MODE)
        self.assertEqual(w.height(), BASE_HEIGHT)
        self.assertEqual(OVERVIEW_HEIGHT, BASE_HEIGHT)
        w.close()

    def test_switch_restores_height(self):
        w = _make_widget(display_mode=OVERVIEW_MODE)
        self.assertEqual(w.height(), OVERVIEW_HEIGHT)
        w._set_display_mode(MIMO_MODE)
        self.assertEqual(w.height(), BASE_HEIGHT)
        w.close()


class TestOverviewPercentFormat(unittest.TestCase):
    """Static formatting for overview percentage labels."""

    def test_not_configured(self):
        self.assertEqual(TokenWidget._format_overview_percent(None, False), "\u672a\u914d\u7f6e")

    def test_no_data(self):
        for value in (None, "invalid", float("nan"), float("inf")):
            with self.subTest(value=value):
                self.assertEqual(TokenWidget._format_overview_percent(value, True), "--")

    def test_zero(self):
        self.assertEqual(TokenWidget._format_overview_percent(0.0, True), "100.0%")

    def test_mid(self):
        self.assertEqual(TokenWidget._format_overview_percent(42.5, True), "57.5%")

    def test_clamp_above(self):
        self.assertEqual(TokenWidget._format_overview_percent(150.0, True), "0.0%")

    def test_clamp_below(self):
        self.assertEqual(TokenWidget._format_overview_percent(-10.0, True), "100.0%")


class TestSuperGPTSettings(unittest.TestCase):
    def test_settings_load_mask_and_round_trip_preserve_other_fields(self):
        original = {"supergpt_base_url": "https://old.test", "supergpt_api_key": "old-key",
                    "third_party_api_key": "wlb-key", "unrelated": 42}
        dialog = SettingsDialog(original)
        try:
            self.assertEqual(dialog.supergpt_base_url_edit.text(), "https://old.test")
            self.assertEqual(dialog.supergpt_api_key_edit.text(), "")
            self.assertEqual(dialog.supergpt_api_key_edit.echoMode(), QLineEdit.EchoMode.Password)
            dialog.supergpt_base_url_edit.setText(" https://new.test/v1 ")
            dialog.supergpt_api_key_edit.setText(" new-key ")
            updated = dialog.get_config()
            self.assertEqual(updated["supergpt_base_url"], "https://new.test/v1")
            self.assertNotIn("supergpt_api_key", updated)
            self.assertNotIn("third_party_api_key", updated)
            self.assertEqual(updated["unrelated"], 42)
            self.assertEqual(original["supergpt_api_key"], "old-key")
        finally:
            dialog.close()

    def test_legacy_settings_and_blank_fields_use_defaults(self):
        dialog = SettingsDialog({})
        try:
            self.assertEqual(dialog.supergpt_base_url_edit.text(), "https://api.supergpt.dev")
            self.assertEqual(dialog.supergpt_api_key_edit.text(), "")
            dialog.supergpt_base_url_edit.setText(" ")
            dialog.supergpt_api_key_edit.setText(" ")
            updated = dialog.get_config()
            self.assertEqual(updated["supergpt_base_url"], "https://api.supergpt.dev")
            self.assertNotIn("supergpt_api_key", updated)
        finally:
            dialog.close()

    def test_save_shares_keys_and_failure_keeps_dialog_open(self):
        dialog = SettingsDialog({})
        try:
            dialog.tp_api_key_edit.setText(" new-wlb ")
            dialog.supergpt_api_key_edit.setText(" new-supergpt ")
            with patch("widget.ProviderKeysWorker") as factory:
                dialog.accept()
                factory.assert_called_once_with(
                    {"wlb-relay": "new-wlb", "supergpt-relay": "new-supergpt"}, dialog)
                self.assertFalse(dialog.isEnabled())
                dialog.reject()
                self.assertIsNotNone(dialog._keys_worker)
                factory.return_value.error = "共享密钥保存失败"
                with patch("widget.QMessageBox.critical") as error:
                    dialog._finish_key_save()
                error.assert_called_once()
                self.assertEqual(dialog.result(), 0)
                self.assertTrue(dialog.isEnabled())
                dialog.accept()
                factory.return_value.error = ""
                dialog._finish_key_save()
            self.assertEqual(dialog.result(), 1)
            self.assertEqual(dialog.tp_api_key_edit.text(), "")
            self.assertNotIn("third_party_api_key", dialog.get_config())
        finally:
            dialog.close()

    def test_blank_input_does_not_change_shared_keys(self):
        dialog = SettingsDialog({})
        with patch("widget.ProviderKeysWorker") as factory:
            dialog.accept()
        factory.assert_not_called()
        self.assertEqual(dialog.result(), 1)


    def test_settings_reject_invalid_base_url_before_saving(self):
        dialog = SettingsDialog({})
        try:
            dialog.tp_base_url_edit.setText("http://example.test")
            with patch("widget.QMessageBox.warning") as warning, \
                 patch("widget.ProviderKeysWorker") as factory:
                dialog.accept()
            warning.assert_called_once()
            factory.assert_not_called()
            self.assertEqual(dialog.result(), 0)
        finally:
            dialog.close()

    def test_settings_confirm_non_default_host_before_saving(self):
        dialog = SettingsDialog({})
        try:
            dialog.supergpt_base_url_edit.setText("https://relay.example.test")
            with patch("widget.QMessageBox.question",
                       return_value=QMessageBox.StandardButton.No) as question, \
                 patch("widget.ProviderKeysWorker") as factory:
                dialog.accept()
            question.assert_called_once()
            factory.assert_not_called()
            self.assertEqual(dialog.result(), 0)
            self.assertNotIn(
                "relay.example.test", dialog.cfg.get("approved_base_url_hosts", [])
            )

            with patch("widget.QMessageBox.question",
                       return_value=QMessageBox.StandardButton.Yes):
                dialog.accept()
            self.assertEqual(dialog.result(), 1)
            self.assertEqual(
                dialog.supergpt_base_url_edit.text(), "https://relay.example.test"
            )
            self.assertIn(
                "relay.example.test", dialog.cfg["approved_base_url_hosts"]
            )
        finally:
            dialog.close()


class TestSuperGPTBalance(unittest.TestCase):
    def test_first_row_aligns_with_dual_bar_columns_without_overlap(self):
        w = _make_widget(display_mode=OVERVIEW_MODE, cookie="fake")
        try:
            self.assertEqual(TokenWidget._overview_row_metrics(0)[3], 111)
            for amount, currency in ((0, "USD"), (0, "CNY"), (123456789.12, "USD")):
                w._on_supergpt_balance_done(
                    {"ok": True, "balance": amount, "currency": currency, "error": None}
                )
                painter = _OverviewPainter()
                with patch.object(painter, "drawRoundedRect") as draw_bar:
                    w._paint_overview(painter)
                mimo_bar = next(call.args for call in draw_bar.call_args_list if call.args[0] == 16 and call.args[1] == 44)
                wlb_bar = next(call.args for call in draw_bar.call_args_list if call.args[1] == 78)
                self.assertEqual((mimo_bar[0], mimo_bar[2]), (wlb_bar[0], wlb_bar[2]))
                title = next(row for row in painter.text_records if row[1] == "API余额")
                value = next(row for row in painter.text_records if row[0] and row[0].x() > 133 and row[0].y() == 24)
                self.assertEqual((title[0].x(), title[0].y()), (133, 24))
                self.assertEqual(title[0].y(), value[0].y())
                self.assertLess(title[0].right(), value[0].x())
                self.assertEqual(value[0].right(), 243)
                self.assertTrue(value[4] & Qt.AlignmentFlag.AlignRight)
                balance_bar = next(call.args for call in draw_bar.call_args_list if call.args[0] == 133 and call.args[1] == 44)
                self.assertEqual(balance_bar[:4], (133, 44, 111, 14))
                self.assertLessEqual(QFontMetrics(value[3]).horizontalAdvance(value[1]), value[0].width())
                first_labels = [row for row in painter.text_records if row[0] and row[0].y() == 24 and row[0].x() == 16]
                self.assertTrue(all(row[0].right() <= 126 for row in first_labels))
                self.assertIn(w._supergpt_balance_text(), w.toolTip())
                if amount == 0:
                    self.assertEqual(value[1], "$0.00" if currency == "USD" else "¥0.00")
        finally:
            w.close()

    def test_balance_bar_uses_100_original_currency_and_keeps_stale_colors(self):
        w = _make_widget(display_mode=OVERVIEW_MODE)
        try:
            for amount, currency, fill, color in (
                (None, "USD", 0, None), (0, "USD", 0, None), (-5, "USD", 0, None),
                (12.34, "USD", 13, ACCENT_RED), (25, "USD", 27, ACCENT_YELLOW),
                (20, "USD", 22, ACCENT_RED), (50, "USD", 55, ACCENT_YELLOW),
                (100, "USD", 111, ACCENT_GREEN), (150, "USD", 55, BALANCE_LAYER_COLORS[1]),
                (100, "CNY", 111, ACCENT_GREEN),
            ):
                for error in ("", "请求超时"):
                    with self.subTest(amount=amount, currency=currency, error=error):
                        w._supergpt_balance = None if amount is None else {"balance": amount, "currency": currency}
                        w._supergpt_error = error
                        painter = _OverviewPainter()
                        w._paint_supergpt_balance(painter, 133, 38, 44, 111, 14)
                        self.assertEqual(painter.text_records[0][2], TEXT_COLOR)
                        self.assertEqual(painter.text_records[1][2], DIM if amount is None else TEXT_COLOR)
                        self.assertEqual(painter.bar_records[0][0], (133, 44, 111, 14))
                        self.assertEqual(len(painter.bar_records), 2 if fill else 1)
                        self.assertEqual(painter.bar_records[0][1],
                                         ACCENT_GREEN if amount == 150 else BAR_BG)
                        if fill:
                            self.assertEqual(painter.bar_records[-1], ((133, 44, fill, 14), color))
        finally:
            w.close()

    def test_balance_layer_boundaries_and_decreasing_balance(self):
        w = _make_widget(display_mode=OVERVIEW_MODE)
        green, blue, purple = BALANCE_LAYER_COLORS[:3]
        try:
            for currency in ("USD", "CNY"):
                for amount, layers, fraction, width, background, color in (
                    (100.01, 2, 0.0001, 1, green, blue),
                    (199.99, 2, 0.9999, 110, green, blue),
                    (200, 2, 1.0, 111, green, blue),
                    (200.01, 3, 0.0001, 1, blue, purple),
                    (250, 3, 0.5, 55, blue, purple),
                    (200, 2, 1.0, 111, green, blue),
                    (100, 1, 1.0, 111, BAR_BG, green),
                ):
                    with self.subTest(amount=amount, currency=currency):
                        w._on_supergpt_balance_done(
                            {"ok": True, "balance": amount, "currency": currency})
                        actual_layers, actual_fraction = w._supergpt_balance_layers()
                        self.assertEqual(actual_layers, layers)
                        self.assertAlmostEqual(actual_fraction, fraction)
                        painter = _OverviewPainter()
                        w._paint_supergpt_balance(painter, 133, 38, 44, 111, 14)
                        self.assertEqual(painter.bar_records, [
                            ((133, 44, 111, 14), background), ((133, 44, width, 14), color),
                        ])
                        marker = [r for r in painter.text_records if r[0].y() == 44]
                        self.assertEqual([r[1] for r in marker], [f"×{layers}"] if layers > 1 else [])
                        if marker:
                            self.assertEqual(marker[0][0].x(), 133 + 3)
                            self.assertEqual(marker[0][0].y(), 44)
                            self.assertEqual(marker[0][0].right(), 133 + 111 - 4)
                            self.assertEqual(marker[0][2].getRgb()[:3], (20, 20, 20))
                            self.assertEqual(marker[0][3].pointSize(), 7)
                            self.assertEqual(marker[0][4], Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                        self.assertEqual(painter._font, painter.text_records[1][3])
                        self.assertEqual(painter._pen.color(), TEXT_COLOR)
                        self.assertIn(f"每层 100 {currency}，{layers} 层", w.toolTip())
        finally:
            w.close()

    def test_huge_balances_draw_only_two_visible_layers_and_elide_marker(self):
        w = _make_widget(display_mode=OVERVIEW_MODE)
        try:
            for amount in (123456789.12, sys.float_info.max):
                with self.subTest(amount=amount):
                    w._on_supergpt_balance_done(
                        {"ok": True, "balance": amount, "currency": "USD"})
                    painter = _OverviewPainter()
                    w._paint_supergpt_balance(painter, 133, 38, 44, 111, 14)
                    self.assertEqual(len(painter.bar_records), 2)
                    layers, fraction = w._supergpt_balance_layers()
                    self.assertGreater(layers, 1)
                    self.assertGreater(fraction, 0)
                    self.assertLessEqual(fraction, 1)
                    marker = painter.text_records[-1]
                    self.assertEqual(marker[0].x(), 133 + 3)
                    self.assertEqual(marker[0].y(), 44)
                    self.assertEqual(marker[0].right(), 133 + 111 - 4)
                    self.assertEqual(marker[2].getRgb()[:3], (20, 20, 20))
                    self.assertEqual(marker[3].pointSize(), 7)
                    self.assertEqual(marker[4], Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                    self.assertLessEqual(QFontMetrics(marker[3]).horizontalAdvance(marker[1]), marker[0].width())
                    self.assertTrue(marker[1].startswith("×"))
                    self.assertIn(f"{layers} 层", w.toolTip())
                    self.assertIn(w._supergpt_balance_text(), w.toolTip())
                    if amount == sys.float_info.max:
                        self.assertIn("…", marker[1])
                        self.assertNotEqual(marker[1], f"×{layers}")
        finally:
            w.close()

    def test_multi_layer_balance_does_not_change_window_size_on_mode_switch(self):
        w = _make_widget(display_mode=OVERVIEW_MODE)
        try:
            size = w.size()
            w._on_supergpt_balance_done({"ok": True, "balance": 250, "currency": "CNY"})
            for mode in (MIMO_MODE, THIRD_PARTY_MODE, OVERVIEW_MODE):
                w._set_display_mode(mode)
                self.assertEqual(w.size(), size)
                self.assertEqual(w.height(), BASE_HEIGHT)
        finally:
            w.close()

    def test_failed_refresh_keeps_balance_and_marks_it_stale(self):
        w = _make_widget(display_mode=OVERVIEW_MODE)
        try:
            self.assertEqual(w._supergpt_balance_text(), "--")
            data = {"ok": True, "balance": 250, "currency": "USD", "error": None}
            w._on_supergpt_balance_done(data)
            last_update = w._supergpt_last_update
            self.assertTrue(last_update)
            self.assertIn(last_update, w.toolTip())
            w._set_gpt_route("supergpt")
            painter = _OverviewPainter()
            w._paint_overview(painter)
            original_bars = painter.bar_records
            original_markers = [(r[0], r[1]) for r in painter.text_records if r[0].y() == 44]
            self.assertEqual(original_markers, [(QRect(136, 44, 105, 14), "×3")])
            self.assertEqual(next(row[2] for row in painter.text_records if row[1] == "API余额"), ACCENT_GREEN)
            w._on_supergpt_balance_done({"ok": False, "balance": None, "error": "请求超时"})
            self.assertIs(w._supergpt_balance, data)
            self.assertEqual(w._supergpt_last_update, last_update)
            self.assertIn("数据已过期", w.toolTip())
            self.assertIn("请求超时", w.toolTip())
            painter = _OverviewPainter()
            w._paint_overview(painter)
            self.assertEqual(next(row[2] for row in painter.text_records if row[1] == "API余额"), ACCENT_GREEN)
            self.assertEqual(next(row[2] for row in painter.text_records
                                  if row[0].x() > 133 and row[0].y() == 24), TEXT_COLOR)
            self.assertEqual(painter.bar_records, original_bars)
            self.assertEqual([(r[0], r[1]) for r in painter.text_records if r[0].y() == 44], original_markers)
            self.assertIn("每层 100 USD，3 层，当前层 50.00%", w.toolTip())
            w._on_supergpt_balance_done(data)
            self.assertEqual(w._supergpt_error, "")
        finally:
            w.close()

    def test_only_overview_dispatches_balance_and_never_duplicates_worker(self):
        w = _make_widget(supergpt_base_url="https://example.test", supergpt_api_key="test-key",
                          approved_base_url_hosts=["example.test"])
        try:
            balance = Mock()
            w._do_fetch_supergpt_balance = balance
            for mode in (MIMO_MODE, THIRD_PARTY_MODE, OVERVIEW_MODE):
                w.cfg["display_mode"] = mode
                balance.reset_mock()
                w._do_fetch()
                self.assertEqual(balance.call_count, int(mode == OVERVIEW_MODE))
            worker = Mock()
            with patch("widget.SuperGPTBalanceWorker", return_value=worker) as factory:
                TokenWidget._do_fetch_supergpt_balance(w)
                TokenWidget._do_fetch_supergpt_balance(w)
            factory.assert_called_once_with(w, base_url="https://example.test")
            worker.start.assert_called_once()
            w._exit_requested = True
            TokenWidget._do_fetch_supergpt_balance(w)
            worker.start.assert_called_once()
        finally:
            w._supergpt_worker = None
            w.close()

    def test_worker_isolated_failure_and_interruption_and_quit_waits(self):
        worker = SuperGPTBalanceWorker()
        results = []
        worker.result_ready.connect(results.append)
        with patch("widget.supergpt_balance.fetch_balance", side_effect=RuntimeError("离线")):
            worker.run()
        self.assertEqual(results[0]["error"], "SuperGPT 余额查询失败")
        with patch.object(worker, "isInterruptionRequested", return_value=True), patch("widget.supergpt_balance.fetch_balance") as fetch:
            worker.run()
        fetch.assert_not_called()
        w = _make_widget()
        try:
            w._exit_callback = Mock()
            w._supergpt_worker = Mock()
            w._quit_app()
            w._supergpt_worker.requestInterruption.assert_called_once()
            w._supergpt_worker.wait.assert_called_once()
            w._exit_callback.assert_called_once()
        finally:
            w._supergpt_worker = None
            w.close()

    def test_worker_forwards_configured_endpoint_and_key(self):
        worker = SuperGPTBalanceWorker(base_url="https://example.test")
        with patch("widget.supergpt_balance.fetch_balance", return_value={"ok": True}) as fetch:
            worker.run()
        fetch.assert_called_once_with("https://example.test")

    def test_wlb_worker_rereads_shared_key_on_each_refresh(self):
        worker = ThirdPartyFetchWorker("https://example.test")
        results = []
        worker.finished.connect(results.append)
        with patch("widget.provider_keys.read_key", side_effect=["old-key", "new-key", ""]), \
             patch("widget.api_client.fetch_third_party_usage", side_effect=lambda *args: {"ok": True}) as fetch:
            worker.run()
            worker.run()
            worker.run()
        self.assertEqual([call.args[1] for call in fetch.call_args_list], ["old-key", "new-key"])
        self.assertEqual([result["key_configured"] for result in results], [True, True, False])
        self.assertFalse(results[-1]["ok"])

    def test_key_save_worker_reports_safe_errors(self):
        worker = ProviderKeysWorker({"wlb-relay": "test-key"})
        with patch("widget.provider_keys.save_keys", side_effect=RuntimeError("test-key")):
            worker.run()
        self.assertNotIn("test-key", worker.error)
        self.assertEqual(worker.keys, {})

    def test_quit_waits_for_settings_key_save(self):
        widget = _make_widget()
        dialog = SettingsDialog({}, widget)
        try:
            dialog._keys_worker = Mock()
            widget._tray_icon.showMessage = Mock()
            widget._quit_app()
            self.assertFalse(widget._exit_requested)
            widget._tray_icon.showMessage.assert_called_once()
        finally:
            dialog._keys_worker = None
            dialog.close()
            widget.close()


class TestOverviewRowMetrics(unittest.TestCase):
    """Overview rows stack vertically and fit inside the compact window."""

    def test_three_rows_stack_without_overlap(self):
        rows = [TokenWidget._overview_row_metrics(index) for index in range(3)]
        self.assertEqual([row[0] for row in rows], [16, 16, 16])
        for previous, current in zip(rows, rows[1:]):
            previous_bar_bottom = previous[2] + previous[4]
            current_label_top = current[1] - 14
            self.assertLessEqual(previous_bar_bottom, current_label_top)
        self.assertLessEqual(rows[-1][2] + rows[-1][4] + 10, BASE_HEIGHT)

    def test_bars_match_overview_column_sizes(self):
        for index in range(3):
            _, _, _, bar_width, bar_height = TokenWidget._overview_row_metrics(index)
            self.assertEqual((bar_width, bar_height), (111 if index == 0 else 228, 14))


class TestOverviewExpiryRendering(unittest.TestCase):
    def _render_first_row(self, days_left=2, alert_enabled=True, row_width=500):
        expiry_date = (datetime.now().date() + timedelta(days=days_left)).strftime("%Y-%m-%d")
        w = _make_widget(
            display_mode=OVERVIEW_MODE,
            cookie="fake",
            expiry_date=expiry_date,
            expiry_alert_enabled=alert_enabled,
        )
        w._plan_total = 1000
        w._plan_used = 425
        painter = _OverviewPainter()
        def row_metrics(index):
            return 16, 38 + index * 34, 44 + index * 34, row_width if index == 0 else 228, 14

        with patch.object(TokenWidget, "_overview_row_metrics", side_effect=row_metrics):
            w._paint_overview(painter)
        return w, expiry_date, painter

    @staticmethod
    def _first_row_records(painter):
        return [record for record in painter.text_records if record[0] and record[0].y() == 24 and record[0].x() == 16]

    def test_first_row_combines_title_and_short_expiry(self):
        w, expiry_date, painter = self._render_first_row()
        expiry_text = _format_overview_expiry(expiry_date)
        records = self._first_row_records(painter)
        title_records = [record for record in records if record[1] != "57.5%"]

        self.assertEqual(expiry_text, "2天")
        self.assertEqual(
            "".join(record[1] for record in title_records),
            f"Mimo {expiry_text}",
        )
        self.assertEqual(title_records[0][2], TEXT_COLOR)
        w.close()

    def test_first_row_date_and_reminder_keep_title_color(self):
        for alert_enabled in (True, False):
            with self.subTest(alert_enabled=alert_enabled):
                w, expiry_date, painter = self._render_first_row(
                    days_left=2, alert_enabled=alert_enabled
                )
                title_records = [
                    record
                    for record in self._first_row_records(painter)
                    if record[1] != "57.5%"
                ]
                self.assertEqual(title_records[0][2], TEXT_COLOR)
                w.close()

    def test_first_row_accepts_padded_and_unpadded_dates(self):
        for expiry_date in ("2099-09-25", "2099-9-25"):
            days_left = max(0, (datetime.strptime(expiry_date, "%Y-%m-%d").date() - datetime.now().date()).days)
            self.assertEqual(_format_overview_expiry(expiry_date), f"{days_left}天")

    def test_first_row_invalid_or_missing_expiry_has_no_suffix(self):
        self.assertEqual(_format_overview_expiry(""), "")
        self.assertEqual(_format_overview_expiry("not-a-date"), "")
        today = datetime.now().date()
        self.assertEqual(_format_overview_expiry(today.isoformat()), "0天")
        self.assertEqual(
            _format_overview_expiry((today - timedelta(days=1)).isoformat()), "0天"
        )

    def test_first_row_text_uses_nine_point_font_and_leaves_percent_room(self):
        w, expiry_date, painter = self._render_first_row(row_width=228)
        records = self._first_row_records(painter)
        percent_record = next(record for record in records if record[1] == "57.5%")
        percent_start = (
            percent_record[0].right()
            + 1
            - QFontMetrics(percent_record[3]).horizontalAdvance("57.5%")
        )

        self.assertTrue(records)
        self.assertTrue(all(record[3].family() == "Microsoft YaHei" for record in records))
        self.assertTrue(all(record[3].pointSize() == 9 for record in records))
        self.assertLess(max(record[0].right() for record in records if record[1] != "57.5%"), percent_start)
        w.close()


class TestMimoTooltipLines(unittest.TestCase):
    """MiMo tooltip builder returns plan percentage when data exists."""

    def test_with_plan_data(self):
        w = _make_widget(cookie="fake")
        w._plan_total = 1000
        w._plan_used = 250
        lines = w._build_mimo_tooltip_lines()
        joined = "\n".join(lines)
        self.assertIn("Token Plan: 25.0%", joined)
        w.close()

    def test_no_plan(self):
        w = _make_widget(cookie="fake")
        w._plan_total = 0
        lines = w._build_mimo_tooltip_lines()
        self.assertNotIn("Token Plan", "\n".join(lines))
        w.close()


class TestWlbResetRendering(unittest.TestCase):
    def test_iso_reset_time_is_converted_to_local_time(self):
        utc_reset = datetime(2099, 9, 1, 12, 34, 56, tzinfo=timezone.utc)

        self.assertEqual(
            _parse_reset_datetime(utc_reset.isoformat().replace("+00:00", "Z")),
            utc_reset.astimezone().replace(tzinfo=None),
        )

    def test_unix_reset_time_and_relative_reset_fallback(self):
        utc_reset = datetime(2099, 9, 1, 12, 34, 56, tzinfo=timezone.utc)
        timestamp = utc_reset.timestamp()

        self.assertEqual(
            _parse_reset_datetime(timestamp),
            utc_reset.astimezone().replace(tzinfo=None),
        )
        self.assertEqual(
            _format_overview_reset_time(timestamp),
            utc_reset.astimezone().strftime("%H:%M"),
        )
        self.assertEqual(
            _format_overview_reset_days(timestamp),
            f"{(utc_reset.astimezone().date() - datetime.now().date()).days}天",
        )

        before = datetime.now().astimezone().replace(tzinfo=None)
        fallback = _parse_reset_datetime(None, 3600)
        self.assertAlmostEqual((fallback - before).total_seconds(), 3600, delta=1)
        self.assertRegex(_format_overview_reset_time(None, 3600), r"^\d{2}:\d{2}$")
        self.assertRegex(_format_overview_reset_days(None, 5 * 86400), r"^\d+天$")

    def test_overview_wlb_dual_labels_are_compact_and_use_nine_point_font(self):
        reset_at = (
            datetime.now().astimezone() + timedelta(days=5)
        ).replace(microsecond=0).isoformat()
        w = _make_widget(display_mode=OVERVIEW_MODE, third_party_api_key="k")
        w._tp_data = {
            "daily": {"used_percent": 12.5, "reset_at": reset_at},
            "used_percent": 42.5,
            "remaining_percent": 57.5,
            "window": "7d",
            "total_percent": 100,
            "reset_at": reset_at,
        }
        painter = _OverviewPainter()

        w._paint_overview(painter)

        row_records = [
            record for record in painter.text_records if record[0] and record[0].y() == 58
        ]
        texts = [record[1] for record in row_records]
        self.assertEqual(
            texts,
            [f"WLB - {_format_overview_reset_time(reset_at)}", "87.5%", "5天", "57.5%"],
        )
        self.assertTrue(all(record[3].pointSize() == 9 for record in row_records))
        self.assertFalse(any("重置" in text or "还剩" in text for text in texts))
        self.assertTrue(all(record[2] == TEXT_COLOR for record in row_records))
        w.close()

    def test_active_provider_header_is_green_and_other_headers_stay_white(self):
        w = _make_widget(display_mode=OVERVIEW_MODE)
        try:
            for route in ("wlb", "official", "supergpt", None):
                with self.subTest(route=route):
                    w._set_gpt_route(route)
                    painter = _OverviewPainter()
                    w._paint_overview(painter)
                    for row_y, provider in ((58, "wlb"), (92, "official")):
                        records = [r for r in painter.text_records if r[0] and r[0].y() == row_y]
                        self.assertTrue(records)
                        expected = ACCENT_GREEN if route == provider else TEXT_COLOR
                        self.assertTrue(all(r[2] == expected for r in records))
                    token_title = next(r for r in painter.text_records if r[1].startswith("Mimo"))
                    self.assertEqual(token_title[2], TEXT_COLOR)
        finally:
            w.close()
            w.deleteLater()

    def test_overview_wlb_missing_daily_window_shows_no_data(self):
        w = _make_widget(display_mode=OVERVIEW_MODE, third_party_api_key="k")
        w._tp_data = {
            "daily": {"has_rate_limit": False, "used_percent": 0},
            "used_percent": 42.5,
            "remaining_percent": 57.5,
            "window": "7d",
            "total_percent": 100,
        }
        painter = _OverviewPainter()

        w._paint_overview(painter)

        row_records = [
            record for record in painter.text_records if record[0] and record[0].y() == 58
        ]
        texts = [record[1] for record in row_records]
        self.assertEqual(texts, ["WLB - --:--", "--", "--天", "57.5%"])
        self.assertTrue(all(record[3].pointSize() == 9 for record in row_records))
        w.close()

    def test_overview_gpt_dual_labels_use_primary_time_and_weekly_days(self):
        primary_reset = datetime.now().astimezone() + timedelta(hours=1, minutes=46)
        w = _make_widget(display_mode=OVERVIEW_MODE, gpt_session_cookie="k")
        w._gpt_data = {
            "used_percent": 42.5,
            "primary": {"used_percent": 12.5, "reset_at": primary_reset.timestamp()},
            "secondary": {"used_percent": 42.5, "reset_after": 5 * 86400},
        }
        painter = _OverviewPainter()

        w._paint_overview(painter)

        row_records = [
            record for record in painter.text_records if record[0] and record[0].y() == 92
        ]
        texts = [record[1] for record in row_records]
        self.assertEqual(
            texts,
            [
                f"GPT - {_format_overview_reset_time(primary_reset.timestamp())}",
                "87.5%",
                _format_overview_reset_days(None, 5 * 86400),
                "57.5%",
            ],
        )
        segment_width = (228 - DUAL_BAR_GAP) // 2
        left_records = row_records[:2]
        self.assertEqual(left_records[0][0].x(), 16)
        self.assertEqual(left_records[1][0], QRect(16, 92, segment_width, 18))
        self.assertEqual(
            [record[4] for record in left_records],
            [
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
            ],
        )
        self.assertTrue(all(record[3].pointSize() == 9 for record in row_records))
        w.close()

    def test_overview_gpt_missing_reset_uses_placeholders(self):
        w = _make_widget(display_mode=OVERVIEW_MODE, gpt_session_cookie="k")
        w._gpt_data = {
            "used_percent": 42.5,
            "primary": {"used_percent": 12.5},
            "secondary": {"used_percent": 42.5},
        }
        painter = _OverviewPainter()

        w._paint_overview(painter)

        row_records = [
            record for record in painter.text_records if record[0] and record[0].y() == 92
        ]
        self.assertEqual(
            [record[1] for record in row_records],
            ["GPT - --:--", "87.5%", "--天", "57.5%"],
        )
        w.close()


class TestThirdPartyTooltipLines(unittest.TestCase):
    """Third-party tooltip builder shows percentages from _tp_data."""

    def test_with_data(self):
        w = _make_widget(third_party_api_key="k")
        w._tp_data = {"used_percent": 33.33, "remaining_percent": 66.67,
                       "is_valid": True, "window": "7d", "total_percent": 100,
                       "reset_at": "2099-09-01T12:34:56Z"}
        lines = w._build_third_party_tooltip_lines()
        joined = "\n".join(lines)
        self.assertIn("33.33%", joined)
        self.assertIn("Active", joined)
        expected = datetime(2099, 9, 1, 12, 34, 56, tzinfo=timezone.utc)
        self.assertIn(f"7日重置: {expected.astimezone():%Y-%m-%d %H:%M:%S}", joined)
        w.close()

    def test_no_data(self):
        w = _make_widget(third_party_api_key="k")
        w._tp_data = None
        lines = w._build_third_party_tooltip_lines()
        self.assertEqual(lines[0], "WLB")
        self.assertEqual(len(lines), 1)
        w.close()


class TestOverviewTooltipLines(unittest.TestCase):
    """Overview tooltip lists both sources with correct labels."""

    def test_no_config(self):
        w = _make_widget()
        lines = w._build_overview_tooltip_lines()
        joined = "\n".join(lines)
        self.assertIn("Token Plan 剩余: \u672a\u914d\u7f6e", joined)
        self.assertIn("WLB \u65e5\u9650\u989d剩余: \u672a\u914d\u7f6e", joined)
        self.assertIn("WLB \u5468\u9650\u989d剩余: \u672a\u914d\u7f6e", joined)
        w.close()

    def test_bottom_error_includes_provider_name(self):
        w = _make_widget(display_mode=OVERVIEW_MODE)
        try:
            for attr, label in (("_mimo_error", "MiMo"), ("_tp_error", "WLB"),
                                ("_gpt_error", "GPT"), ("_supergpt_error", "SuperGPT")):
                w._mimo_error = w._tp_error = w._gpt_error = w._supergpt_error = ""
                setattr(w, attr, "HTTP 403")
                w._refresh_error_state()
                self.assertEqual(w._last_error, f"{label}: HTTP 403")
            w._mimo_error = w._tp_error = w._gpt_error = w._supergpt_error = ""
            w._refresh_error_state()
            self.assertEqual(w._last_error, "")
        finally:
            w.close()

    def test_bottom_error_isolated_by_detail_mode(self):
        w = _make_widget(display_mode=MIMO_MODE)
        try:
            w._mimo_error, w._tp_error = "HTTP 403", "WLB down"
            w._refresh_error_state()
            self.assertEqual(w._last_error, "MiMo: HTTP 403")
            w.cfg["display_mode"] = THIRD_PARTY_MODE
            w._refresh_error_state()
            self.assertEqual(w._last_error, "WLB: down")
        finally:
            w.close()

    def test_provider_prefix_removed_from_bottom_and_tooltip(self):
        w = _make_widget(display_mode=OVERVIEW_MODE)
        try:
            for provider, attr in (("GPT", "_gpt_error"), ("SuperGPT", "_supergpt_error")):
                for prefix in (f"{provider}: ", f"{provider.lower()}：", f"{provider.upper()} "):
                    with self.subTest(provider=provider, prefix=prefix):
                        w._gpt_error = w._supergpt_error = ""
                        original = f"{prefix}HTTP 403"
                        setattr(w, attr, original)
                        w._refresh_error_state()
                        w._apply_overview_tooltip_if_needed()
                        self.assertEqual(w._last_error, f"{provider}: HTTP 403")
                        error_line = next(line for line in w.toolTip().splitlines() if "HTTP 403" in line)
                        self.assertEqual(error_line.count(provider), 1)
                        self.assertEqual(getattr(w, attr), original)
        finally:
            w.close()

    def test_empty_error_detail_falls_back_to_unknown(self):
        for error in ("GPT: ", "gpt：", "GPT ", "   "):
            with self.subTest(error=error):
                self.assertEqual(TokenWidget._error_display_detail("GPT", error), "未知错误")

    def test_supergpt_callback_updates_and_clears_bottom_error(self):
        w = _make_widget(display_mode=OVERVIEW_MODE)
        try:
            w._on_supergpt_balance_done({"ok": False, "error": "HTTP 403"})
            self.assertEqual(w._last_error, "SuperGPT: HTTP 403")
            w._on_supergpt_balance_done({"ok": True, "balance": 12, "currency": "USD"})
            self.assertEqual(w._last_error, "")
        finally:
            w.close()

    def test_mimo_configured_no_data(self):
        w = _make_widget(cookie="fake")
        w._plan_total = 0
        lines = w._build_overview_tooltip_lines()
        joined = "\n".join(lines)
        self.assertIn("Token Plan 剩余: --", joined)
        w.close()

    def test_mimo_with_data(self):
        w = _make_widget(cookie="fake")
        w._plan_total = 200
        w._plan_used = 50
        lines = w._build_overview_tooltip_lines()
        joined = "\n".join(lines)
        self.assertIn("Token Plan 剩余: 75.0%", joined)
        w.close()

    def test_tp_with_data(self):
        w = _make_widget(third_party_api_key="k")
        w._tp_data = {"daily": {"used_percent": 6.25},
                       "used_percent": 12.5, "remaining_percent": 87.5,
                       "is_valid": True, "window": "7d", "total_percent": 100}
        lines = w._build_overview_tooltip_lines()
        joined = "\n".join(lines)
        self.assertIn("WLB \u65e5\u9650\u989d剩余: 93.8%", joined)
        self.assertIn("WLB \u5468\u9650\u989d剩余: 87.5%", joined)
        w.close()

    def test_error_included_via_apply(self):
        """Errors are appended by _apply_overview_tooltip_if_needed, not the builder."""
        w = _make_widget(display_mode=OVERVIEW_MODE, cookie="fake")
        w._mimo_error = "test error"
        w._tp_error = "api error"
        w._refresh_error_state()
        w._apply_overview_tooltip_if_needed()
        tooltip = w.toolTip()
        self.assertIn("test error", tooltip)
        self.assertIn("api error", tooltip)
        self.assertIn("总览", tooltip)
        w.close()


class TestThirdPartyCallbackExtraction(unittest.TestCase):
    """Verify _on_third_party_fetch_done extracts flat parsed dict from API wrapper.

    api_client.fetch_third_party_usage() returns:
        {"ok": True, "data": parsed_dict, "error": None, "url": ...}
    where parsed_dict contains used_percent, remaining_percent, is_valid, etc.
    _tp_data must be the flat parsed_dict, not the wrapper.
    """

    def test_callback_extracts_data_from_ok_wrapper(self):
        w = _make_widget(display_mode=THIRD_PARTY_MODE, third_party_api_key="k")
        parsed = {"used_percent": 42.0, "remaining_percent": 58.0,
                  "is_valid": True, "window": "7d", "total_percent": 100}
        wrapper = {"ok": True, "data": parsed, "error": None, "url": "http://example.com"}
        w._on_third_party_fetch_done(wrapper)
        # _tp_data must be the flat parsed dict
        self.assertIs(w._tp_data, parsed)
        self.assertEqual(w._tp_data["used_percent"], 42.0)
        self.assertEqual(w._last_error, "")
        w.close()

    def test_callback_sets_error_on_failure(self):
        w = _make_widget(display_mode=THIRD_PARTY_MODE, third_party_api_key="k")
        wrapper = {"ok": False, "data": None, "error": "API Key 无效"}
        w._on_third_party_fetch_done(wrapper)
        self.assertIsNone(w._tp_data)
        self.assertIn("无效", w._last_error)
        w.close()

    def test_callback_does_not_clear_data_on_failure(self):
        w = _make_widget(display_mode=THIRD_PARTY_MODE, third_party_api_key="k")
        w._tp_data = {"used_percent": 10.0, "remaining_percent": 90.0,
                       "is_valid": True, "window": "7d", "total_percent": 100}
        wrapper = {"ok": False, "data": None, "error": "timeout"}
        w._on_third_party_fetch_done(wrapper)
        # Old data should be preserved on failure
        self.assertIsNotNone(w._tp_data)
        self.assertEqual(w._tp_data["used_percent"], 10.0)
        w.close()

    def test_callback_tooltip_reflects_extracted_data(self):
        w = _make_widget(display_mode=OVERVIEW_MODE, third_party_api_key="k")
        parsed = {"used_percent": 55.5, "remaining_percent": 44.5,
                  "is_valid": True, "window": "7d", "total_percent": 100}
        wrapper = {"ok": True, "data": parsed, "error": None, "url": "http://example.com"}
        w._on_third_party_fetch_done(wrapper)
        tooltip = w.toolTip()
        self.assertIn("WLB 周限额剩余: 44.5%", tooltip)
        w.close()


class TestGPTCallback(unittest.TestCase):
    def test_historical_fallback_never_moves_data_time_backwards(self):
        w = _make_widget(display_mode=OVERVIEW_MODE)
        current_time = datetime(2026, 10, 29, 13, 0)
        current_data = {"used_percent": 30.0}
        class FixedClock(datetime):
            @classmethod
            def now(cls):
                return current_time
        with patch("widget.datetime", FixedClock):
            w._on_gpt_fetch_done({"ok": True, "data": current_data})
        for observed_at in ("2026-10-29T12:00:00", "2026-10-29T13:00:00", "invalid"):
            w._on_gpt_fetch_done({"ok": True, "data": {
                "source": "local_jsonl", "observed_at": observed_at,
                "used_percent": 90.0, "source_errors": ["本机 Codex 登录: 请求超时"],
            }})
            self.assertIs(w._gpt_data, current_data)
            self.assertEqual(w._gpt_data_time, current_time)
            self.assertIn("数据已过期", w.toolTip())
            self.assertIn("请求超时", w.toolTip())
        self.assertIn("GPT 数据时间: 2026-10-29 13:00:00", w.toolTip())
        w.close()

    def test_newer_history_is_accepted_with_warning_and_live_recovers(self):
        w = _make_widget(display_mode=OVERVIEW_MODE)
        history = {"source": "local_jsonl", "observed_at": "2026-10-29T13:00:00",
                   "used_percent": 90.0, "source_errors": ["ChatGPT Cookie: HTTP 503"]}
        w._on_gpt_fetch_done({"ok": True, "data": history})
        self.assertIs(w._gpt_data, history)
        self.assertEqual(w._gpt_data_time, datetime(2026, 10, 29, 13, 0))
        self.assertIn("本地历史数据", w.toolTip())
        self.assertIn("HTTP 503", w.toolTip())
        newer_history = {**history, "observed_at": "2026-10-29T13:01:00"}
        w._on_gpt_fetch_done({"ok": True, "data": newer_history})
        self.assertIs(w._gpt_data, newer_history)
        live = {"source": "codex", "used_percent": 20.0}
        class FixedClock(datetime):
            @classmethod
            def now(cls):
                return datetime(2026, 10, 29, 13, 2)
        with patch("widget.datetime", FixedClock):
            w._on_gpt_fetch_done({"ok": True, "data": live})
        self.assertIs(w._gpt_data, live)
        self.assertEqual(w._gpt_data_time, datetime(2026, 10, 29, 13, 2))
        self.assertEqual(w._gpt_error, "")
        self.assertNotIn("HTTP 503", w.toolTip())
        w._on_gpt_fetch_done({"ok": False, "error": "请求超时"})
        self.assertEqual(w._gpt_data_time, datetime(2026, 10, 29, 13, 2))
        w.close()

    def test_overview_tooltip_shows_both_gpt_windows(self):
        w = _make_widget(display_mode=OVERVIEW_MODE)
        w._gpt_data = {
            "used_percent": 40.0,
            "primary": {"used_percent": 20.0},
            "secondary": {"used_percent": 40.0},
        }

        lines = w._build_overview_tooltip_lines()

        self.assertIn("GPT 5\u5c0f\u65f6剩余: 80.0%", lines)
        self.assertIn("GPT \u5468\u9650\u989d剩余: 60.0%", lines)
        w.close()

    def test_failure_keeps_last_data_and_marks_it_stale(self):
        w = _make_widget(display_mode=OVERVIEW_MODE)
        old_data = {"used_percent": 30.0, "remaining_percent": 70.0}
        w._gpt_data = old_data

        w._on_gpt_fetch_done({"ok": False, "data": None, "error": "请求超时"})

        self.assertIs(w._gpt_data, old_data)
        self.assertIn("继续显示上次数据", w._gpt_error)
        self.assertIn("请求超时", w.toolTip())
        w.close()


class TestOverviewDenominatorZero(unittest.TestCase):
    """_plan_used/_plan_total denominator-zero produces -- and empty bar."""

    def test_zero_denominator(self):
        w = _make_widget(cookie="fake")
        w._plan_total = 0
        w._plan_used = 0
        has_cookie = True
        mimo_pct = (w._plan_used / w._plan_total * 100) if has_cookie and w._plan_total > 0 else None
        self.assertIsNone(mimo_pct)
        formatted = TokenWidget._format_overview_percent(mimo_pct, has_cookie)
        self.assertEqual(formatted, "--")
        w.close()


class TestOverviewRenderSmoke(unittest.TestCase):
    """Smoke-test: render overview mode to QImage without clipping."""

    def test_overview_render_no_clip(self):
        from PyQt6.QtGui import QImage
        w = _make_widget(display_mode=OVERVIEW_MODE, cookie="fake", third_party_api_key="k", gpt_session_cookie="fake_gpt")
        w._plan_total = 1000
        w._plan_used = 300
        w._tp_data = {"daily": {"used_percent": 35.0},
                       "used_percent": 55.0, "remaining_percent": 45.0,
                       "is_valid": True, "window": "7d", "total_percent": 100}
        w._gpt_data = {"used_percent": 30.0, "remaining_percent": 70.0,
                       "primary": {"used_percent": 20.0},
                       "secondary": {"used_percent": 30.0},
                       "reset_at": "2026-01-01T00:00:00Z", "source": "test"}
        w._timer.stop()
        w._do_fetch = lambda: None
        w._do_fetch = lambda: None
        w._last_update = "12:00:00"
        img = QImage(w.width(), w.height(), QImage.Format.Format_ARGB32)
        img.fill(0)
        w.show()
        _app.processEvents()
        w.render(img)

        x0, _, bar_y0, bar_w0, bar_h0 = TokenWidget._overview_row_metrics(0)
        x1, _, bar_y1, bar_w1, bar_h1 = TokenWidget._overview_row_metrics(1)
        x2, _, bar_y2, bar_w2, bar_h2 = TokenWidget._overview_row_metrics(2)
        bottom = bar_y2 + bar_h2 + 12
        self.assertLessEqual(bottom, w.height())
        self.assertEqual(x0, x1)
        self.assertEqual(x1, x2)
        self.assertLess(bar_y0, bar_y1)
        self.assertLess(bar_y1, bar_y2)
        self.assertGreater(img.pixelColor(x0 + bar_w0 // 2, bar_y0 + bar_h0 // 2).alpha(), 0)
        self.assertGreater(img.pixelColor(x1 + bar_w1 // 2, bar_y1 + bar_h1 // 2).alpha(), 0)
        self.assertGreater(img.pixelColor(x2 + bar_w2 // 2, bar_y2 + bar_h2 // 2).alpha(), 0)
        for x, bar_y, bar_w, bar_h in (
            (x1, bar_y1, bar_w1, bar_h1),
            (x2, bar_y2, bar_w2, bar_h2),
        ):
            segment_width = (bar_w - DUAL_BAR_GAP) // 2
            gap_color = img.pixelColor(
                x + segment_width + DUAL_BAR_GAP // 2,
                bar_y + bar_h // 2,
            )
            self.assertEqual(gap_color.getRgb(), BG_COLOR.getRgb())
        w.close()

    def test_update_time_is_painted_when_overview_has_error(self):
        w = _make_widget(display_mode=OVERVIEW_MODE)
        w._last_error = "WLB 请求失败"
        w._last_update = "12:00:00"
        painter = Mock()
        metrics = Mock()
        metrics.horizontalAdvance.return_value = 108
        metrics.elidedText.return_value = "WLB 请求失败"
        painter.fontMetrics.return_value = metrics

        w._paint_refresh_status(painter, 136)

        painted_texts = [call.args[-1] for call in painter.drawText.call_args_list]
        self.assertIn("WLB 请求失败", painted_texts)
        self.assertIn("检查于 12:00:00", painted_texts)
        w.close()


class TestRouterStatusDisplay(unittest.TestCase):
    def test_supergpt_menu_triggers_route_and_only_checks_confirmed_status(self):
        w = _make_widget()
        try:
            self.assertEqual(w._gpt_route_actions["supergpt"].text(), "切换到 SuperGPT")
            w._set_gpt_route("official")
            with patch.object(w, "_start_router_operation") as start:
                w._gpt_route_actions["supergpt"].trigger()
            start.assert_called_once_with("route-supergpt")
            w._set_gpt_route("official")
            self.assertFalse(w._gpt_route_actions["supergpt"].isChecked())
            w._tray_icon.showMessage = Mock()
            w._on_router_operation_done(
                RouterResult(True, "GPT 路由已切换到 SuperGPT", route_enabled=True,
                             gpt_route="supergpt")
            )
            self.assertEqual(w._router_status, "GPT 路由已切换到 SuperGPT")
            self.assertEqual(w._gpt_route, "supergpt")
            self.assertEqual(
                [route for route, action in w._gpt_route_actions.items() if action.isChecked()],
                ["supergpt"],
            )
            w._on_router_status_done(
                RouterResult(True, "路由已开启", route_enabled=True, gpt_route="supergpt")
            )
            self.assertTrue(w._gpt_route_actions["supergpt"].isChecked())
            w._on_router_operation_done(RouterResult(False, "切换 GPT 路由失败"))
            self.assertIsNone(w._gpt_route)
            self.assertFalse(any(action.isChecked() for action in w._gpt_route_actions.values()))
        finally:
            w.close()

    def test_pi_menu_triggers_route_and_only_checks_confirmed_status(self):
        w = _make_widget()
        try:
            self.assertEqual(w._pi_route_actions["wlb"].text(), "切换 Pi 到 WLB")
            w._set_pi_route("official")
            with patch.object(w, "_start_router_operation") as start:
                w._pi_route_actions["wlb"].trigger()
            start.assert_called_once_with("pi-route-wlb")
            w._set_pi_route("official")
            self.assertFalse(w._pi_route_actions["wlb"].isChecked())
            w._tray_icon.showMessage = Mock()
            w._on_router_operation_done(
                RouterResult(True, "Pi 路由已切换到 WLB", route_enabled=True,
                             pi_route="wlb")
            )
            self.assertEqual(w._router_status, "Pi 路由已切换到 WLB")
            self.assertEqual(w._pi_route, "wlb")
            self.assertEqual(
                [route for route, action in w._pi_route_actions.items() if action.isChecked()],
                ["wlb"],
            )
            w._on_router_status_done(
                RouterResult(True, "路由已开启", route_enabled=True,
                             gpt_route="official", pi_route="official")
            )
            self.assertTrue(w._pi_route_actions["official"].isChecked())
            self.assertFalse(w._pi_route_actions["wlb"].isChecked())
            w._on_router_operation_done(RouterResult(False, "切换 Pi 路由失败"))
            self.assertIsNone(w._pi_route)
            self.assertFalse(any(action.isChecked() for action in w._pi_route_actions.values()))
        finally:
            w.close()

    def test_router_operation_start_status_labels(self):
        expected = {
            "enable": "正在开启路由",
            "disable": "正在关闭路由",
            "restart": "正在重启路由器",
            "route-wlb": "正在切换到 WLB",
            "route-official": "正在切换到 GPT",
            "route-supergpt": "正在切换到 SuperGPT",
        }
        for operation, label in expected.items():
            with self.subTest(operation=operation):
                w = _make_widget()
                w._router_worker = None
                worker = Mock()
                worker.isRunning.return_value = False
                w._tray_icon.showMessage = Mock()
                with patch("widget.RouterWorker", return_value=worker):
                    w._start_router_operation(operation)
                self.assertEqual(w._router_status, label)
                worker.start.assert_called_once()
                w.close()

    def test_router_status_uses_short_summary_and_refreshes_paint(self):
        w = _make_widget()
        w.update = Mock()
        w._router_worker = Mock()
        w._on_router_operation_done(
            RouterResult(False, "关闭路由失败", "敏感的长错误详情" * 50)
        )

        self.assertEqual(w._router_status, "关闭路由失败")
        self.assertNotIn("敏感的长错误详情", w._router_status)
        self.assertFalse(w._router_status_ok)
        w.update.assert_called_once()
        self.assertTrue(w._router_status_timer.isActive())
        w._clear_router_status()
        self.assertEqual(w._router_status, "")
        self.assertIsNone(w._router_status_ok)
        w.close()


if __name__ == "__main__":
    unittest.main()
