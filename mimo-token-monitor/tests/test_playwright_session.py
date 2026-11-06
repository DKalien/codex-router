import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, call, patch

import playwright_session
from playwright_session import (
    PlaywrightSession,
    PlaywrightSessionClosedError,
    PlaywrightSessionError,
    refresh_cookie,
)


class _FakePage:
    def __init__(self):
        self.goto_calls = []
        self.wait_calls = []
        self.now = 0
        self.url = "https://platform.xiaomimimo.com/console/balance"

    def on(self, *args):
        pass

    def goto(self, *args, **kwargs):
        self.goto_calls.append((args, kwargs))

    def wait_for_timeout(self, timeout):
        self.wait_calls.append(timeout)
        self.now += timeout / 1000


class _FakeContext:
    def __init__(self):
        self.pages = []
        self.page = _FakePage()
        self.cookies_value = []
        self.close_calls = 0

    def on(self, *args):
        pass

    def new_page(self):
        self.pages.append(self.page)
        return self.page

    def cookies(self):
        return self.cookies_value

    def close(self):
        self.close_calls += 1


class _FakePlaywright:
    def __init__(self, context):
        self.context = context
        self.launch_calls = []
        self.stop_calls = 0
        self.chromium = self

    def launch_persistent_context(self, *args, **kwargs):
        self.launch_calls.append((args, kwargs))
        return self.context

    def stop(self):
        self.stop_calls += 1


class TestPlaywrightSession(unittest.TestCase):
    def test_start_reuses_isolated_persistent_context_and_opens_page(self):
        with tempfile.TemporaryDirectory() as tmp:
            context = _FakeContext()
            playwright = _FakePlaywright(context)
            manager = Mock()
            manager.start.return_value = playwright
            session = PlaywrightSession(Path(tmp) / "mimo", headless=True)

            with patch.object(playwright_session, "_load_sync_playwright", return_value=lambda: manager):
                self.assertIs(session.start(), context)
                self.assertIs(session.start(), context)
                page = session.open_mimo("https://platform.xiaomimimo.com/")

            self.assertIs(page, context.page)
            self.assertEqual(len(playwright.launch_calls), 1)
            self.assertEqual(playwright.launch_calls[0][0], (str(Path(tmp) / "mimo"),))
            self.assertTrue(playwright.launch_calls[0][1]["headless"])
            self.assertEqual(
                context.page.goto_calls,
                [
                    (
                        ("https://platform.xiaomimimo.com/",),
                        {"wait_until": "domcontentloaded", "timeout": 30_000},
                    )
                ],
            )

    def test_cookie_is_filtered_to_xiaomimimo_without_leaking_other_domains(self):
        context = _FakeContext()
        context.cookies_value = [
            {"name": "mimo_token", "value": "SECRET_COOKIE", "domain": ".xiaomimimo.com"},
            {"name": "session", "value": "SUBDOMAIN_VALUE", "domain": "auth.xiaomimimo.com"},
            {"name": "other", "value": "OTHER_VALUE", "domain": "example.com"},
        ]
        playwright = _FakePlaywright(context)
        manager = Mock()
        manager.start.return_value = playwright
        session = PlaywrightSession(tempfile.mkdtemp())

        with patch.object(playwright_session, "_load_sync_playwright", return_value=lambda: manager):
            self.assertEqual(
                session.get_cookie(),
                "mimo_token=SECRET_COOKIE; session=SUBDOMAIN_VALUE",
            )

        session.close(remove_user_data_dir=True)

    def test_service_token_is_sorted_first(self):
        context = _FakeContext()
        context.cookies_value = [
            {"name": "userId", "value": "1", "domain": ".xiaomimimo.com"},
            {
                "name": "api-platform_serviceToken",
                "value": "TOKEN",
                "domain": ".platform.xiaomimimo.com",
            },
        ]
        playwright = _FakePlaywright(context)
        manager = Mock()
        manager.start.return_value = playwright
        session = PlaywrightSession(tempfile.mkdtemp())

        with patch.object(playwright_session, "_load_sync_playwright", return_value=lambda: manager):
            self.assertEqual(
                session.get_cookie(),
                "api-platform_serviceToken=TOKEN; userId=1",
            )
        session.close()

    def test_missing_cookie_on_sso_page_has_clear_error(self):
        context = _FakeContext()
        page = _FakePage()
        context.pages = [page]
        context.page = page
        page.url = "https://account.xiaomi.com/fe/service/login/password"
        playwright = _FakePlaywright(context)
        manager = Mock()
        manager.start.return_value = playwright
        session = PlaywrightSession(tempfile.mkdtemp())

        with patch.object(playwright_session, "_load_sync_playwright", return_value=lambda: manager):
            with self.assertRaisesRegex(PlaywrightSessionError, "SSO") as raised:
                session.get_cookie()
        self.assertIn("小米账号", str(raised.exception))
        session.close()

    def test_missing_playwright_has_clear_non_sensitive_error(self):
        session = PlaywrightSession(tempfile.mkdtemp())
        with patch.object(
            playwright_session,
            "_load_sync_playwright",
            side_effect=PlaywrightSessionError("未安装 Playwright，请安装依赖"),
        ):
            with self.assertRaisesRegex(PlaywrightSessionError, "未安装 Playwright") as raised:
                session.start()
        self.assertNotIn("SECRET_COOKIE", str(raised.exception))

    def test_target_closed_stops_cookie_polling_with_actionable_error(self):
        context = _FakeContext()
        context.cookies = Mock()
        context.cookies.side_effect = type("TargetClosedError", (Exception,), {})()
        playwright = _FakePlaywright(context)
        manager = Mock()
        manager.start.return_value = playwright
        session = PlaywrightSession(tempfile.mkdtemp())

        with patch.object(playwright_session, "_load_sync_playwright", return_value=lambda: manager):
            with self.assertRaises(PlaywrightSessionClosedError) as raised:
                session.refresh_cookie(timeout_seconds=30)

        self.assertIn("浏览器会话已关闭", str(raised.exception))
        self.assertIn("Chromium", str(raised.exception))
        context.cookies.assert_called_once_with()
        session.close()

    def test_existing_chrome_or_edge_profile_is_rejected(self):
        session = PlaywrightSession(
            Path(tempfile.gettempdir()) / "Google" / "Chrome" / "User Data"
        )
        with self.assertRaisesRegex(PlaywrightSessionError, "独立目录"):
            session.start()

    def test_start_failure_redacts_exception_message_and_close_stops_runtime(self):
        context = _FakeContext()
        playwright = _FakePlaywright(context)
        playwright.chromium.launch_persistent_context = Mock(
            side_effect=RuntimeError("cookie=SECRET_COOKIE")
        )
        manager = Mock()
        manager.start.return_value = playwright
        session = PlaywrightSession(tempfile.mkdtemp())

        with patch.object(playwright_session, "_load_sync_playwright", return_value=lambda: manager):
            with self.assertRaises(PlaywrightSessionError) as raised:
                session.start()
        self.assertIn("持久化会话启动失败", str(raised.exception))
        self.assertIn("阶段：启动 Chromium", str(raised.exception))
        self.assertIn("RuntimeError", str(raised.exception))
        self.assertNotIn("errno=", str(raised.exception))
        self.assertNotIn("SECRET_COOKIE", str(raised.exception))
        self.assertEqual(playwright.stop_calls, 1)

        session.close()
        self.assertEqual(context.close_calls, 0)

    def test_start_oserror_reports_stage_and_codes_without_sensitive_details(self):
        for stage in ("创建专用目录", "启动 Playwright 驱动", "启动 Chromium"):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as tmp:
                error = OSError(13, "cookie=SECRET_COOKIE", "C:/PRIVATE_PROFILE")
                error.winerror = 5
                runtime = Mock()
                manager = Mock()
                manager.start.return_value = runtime
                if stage == "启动 Playwright 驱动":
                    manager.start.side_effect = error
                elif stage == "启动 Chromium":
                    runtime.chromium.launch_persistent_context.side_effect = error
                session = PlaywrightSession(Path(tmp) / "profile")
                with (
                    patch.object(Path, "mkdir", side_effect=error if stage == "创建专用目录" else None),
                    patch.object(playwright_session, "_load_sync_playwright", return_value=lambda: manager),
                    self.assertRaises(PlaywrightSessionError) as raised,
                ):
                    session.start()
                message = str(raised.exception)
                self.assertIn(f"阶段：{stage}", message)
                self.assertIn("errno=13", message)
                self.assertIn("winerror=5", message)
                self.assertNotIn("SECRET_COOKIE", message)
                self.assertNotIn("PRIVATE_PROFILE", message)
                self.assertNotIn(tmp, message)
                self.assertIs(raised.exception.__cause__, error)
                self.assertIsNone(session.context)
                self.assertIsNone(session._playwright)
                if stage == "启动 Chromium":
                    runtime.stop.assert_called_once_with()
                else:
                    runtime.stop.assert_not_called()

    def test_profile_lock_is_actionable_without_exposing_launch_log(self):
        session = PlaywrightSession(tempfile.mkdtemp())
        runtime = Mock()
        manager = Mock()
        manager.start.return_value = runtime
        runtime.chromium.launch_persistent_context.side_effect = RuntimeError(
            "ProcessSingleton fake-secret C:/private-profile"
        )
        with patch.object(playwright_session, "_load_sync_playwright", return_value=lambda: manager):
            with self.assertRaises(PlaywrightSessionError) as raised:
                session.start()
        self.assertIn("占用", str(raised.exception))
        self.assertIn("不要删除", str(raised.exception))
        self.assertNotIn("fake-secret", str(raised.exception))
        self.assertNotIn("private-profile", str(raised.exception))

    def test_crash_event_is_kept_when_followed_by_context_close(self):
        session = PlaywrightSession(tempfile.mkdtemp())
        session._mark_runtime_failure("页面崩溃")
        session._mark_runtime_failure("上下文已关闭")
        session.open_mimo = Mock(return_value=_FakePage())
        session.get_cookie = Mock()
        with self.assertRaises(PlaywrightSessionClosedError) as raised:
            session.refresh_cookie(interactive=True)
        self.assertIn("页面崩溃", str(raised.exception))
        session.get_cookie.assert_not_called()

    def test_start_oserror_without_codes_does_not_invent_them(self):
        error = OSError("SECRET_COOKIE")
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(Path, "mkdir", side_effect=error),
            self.assertRaises(PlaywrightSessionError) as raised,
        ):
            PlaywrightSession(tmp).start()
        self.assertIn("OSError", str(raised.exception))
        self.assertNotIn("errno=", str(raised.exception))
        self.assertNotIn("winerror=", str(raised.exception))
        self.assertNotIn("SECRET_COOKIE", str(raised.exception))

    def test_refresh_cookie_uses_project_profile_and_returns_tuple(self):
        session = Mock()
        session.refresh_cookie.return_value = "mimo_token=SECRET_COOKIE"
        with (
            patch.object(playwright_session, "PlaywrightSession", return_value=session) as session_type,
            patch(
                "config.playwright_user_data_dir",
                return_value="C:/isolated/mimo-profile",
            ),
        ):
            result = refresh_cookie(interactive=True, timeout_seconds=12)

        self.assertEqual(result, ("mimo_token=SECRET_COOKIE", None))
        session_type.assert_called_once_with("C:/isolated/mimo-profile", headless=False)
        session.refresh_cookie.assert_called_once_with(
            interactive=True,
            timeout_seconds=12,
            previous_cookie=None,
            cookie_validator=None,
        )
        session.close.assert_called_once_with()

    def test_refresh_cookie_returns_clear_error_without_cookie(self):
        session = Mock()
        session.refresh_cookie.side_effect = PlaywrightSessionError(
            "未安装 Playwright，请安装依赖"
        )
        with (
            patch.object(playwright_session, "PlaywrightSession", return_value=session),
            patch("config.playwright_user_data_dir", return_value="C:/isolated/mimo-profile"),
        ):
            cookie, error = refresh_cookie()

        self.assertIsNone(cookie)
        self.assertEqual(error, "未安装 Playwright，请安装依赖")
        self.assertNotIn("SECRET_COOKIE", error)
        session.close.assert_called_once_with()

    def test_interactive_refresh_waits_for_login_cookie(self):
        session = PlaywrightSession(tempfile.mkdtemp())
        page = _FakePage()
        session.open_mimo = Mock(return_value=page)
        session.get_cookie = Mock(side_effect=["session_id=stale", "session_id=ready"])
        with patch("playwright_session.time.monotonic", side_effect=lambda: page.now):
            self.assertEqual(
                session.refresh_cookie(interactive=True, timeout_seconds=10,
                                       previous_cookie="session_id=stale"),
                "session_id=ready",
            )
        self.assertEqual(page.wait_calls, [2000, 1000])

    def test_refresh_visits_protected_console_and_waits_for_frontend(self):
        session = PlaywrightSession(tempfile.mkdtemp(), headless=True)
        page = _FakePage()
        session.open_mimo = Mock(return_value=page)
        session.get_cookie = Mock(return_value="session_id=ready")
        with patch("playwright_session.time.monotonic", side_effect=lambda: page.now):
            self.assertEqual(session.refresh_cookie(timeout_seconds=10), "session_id=ready")
        session.open_mimo.assert_called_once_with(timeout=10_000)
        self.assertEqual(page.wait_calls, [2000])
        self.assertEqual(playwright_session.MIMO_URL, "https://platform.xiaomimimo.com/#/console/balance")

    def test_interactive_refresh_validates_each_changed_cookie_only_once(self):
        session = PlaywrightSession(tempfile.mkdtemp())
        page = _FakePage()
        session.open_mimo = Mock(return_value=page)
        session.get_cookie = Mock(side_effect=["tracking=1", "tracking=1", "mimo_token=ready"])
        validator = Mock(side_effect=[False, True])
        with patch("playwright_session.time.monotonic", side_effect=lambda: page.now):
            self.assertEqual(
                session.refresh_cookie(interactive=True, timeout_seconds=10,
                                       cookie_validator=validator),
                "mimo_token=ready",
            )
        self.assertEqual(page.wait_calls, [2000, 1000, 1000])
        self.assertEqual(validator.call_args_list, [call("tracking=1"), call("mimo_token=ready")])

    def test_validator_failure_is_preserved_without_retry(self):
        session = PlaywrightSession(tempfile.mkdtemp())
        page = _FakePage()
        session.open_mimo = Mock(return_value=page)
        session.get_cookie = Mock(return_value="fake=value")
        validator = Mock(side_effect=PlaywrightSessionError("balance HTTP 403"))
        with self.assertRaisesRegex(PlaywrightSessionError, "balance HTTP 403"):
            session.refresh_cookie(interactive=True, cookie_validator=validator)
        validator.assert_called_once_with("fake=value")

    def test_sso_wait_does_not_navigate_away_from_login_and_has_total_deadline(self):
        session = PlaywrightSession(tempfile.mkdtemp())
        page = _FakePage()
        session.open_mimo = Mock(return_value=page)
        session.get_cookie = Mock(side_effect=playwright_session.PlaywrightCookiePendingError("SSO"))
        with patch("playwright_session.time.monotonic", side_effect=lambda: page.now):
            with self.assertRaisesRegex(PlaywrightSessionError, "SSO"):
                session.refresh_cookie(interactive=True, timeout_seconds=5)
        self.assertEqual(page.now, 5)
        self.assertEqual(page.goto_calls, [])

    def test_close_during_settle_preserves_stage_without_reading_cookies(self):
        session = PlaywrightSession(tempfile.mkdtemp())
        page = _FakePage()
        page.wait_for_timeout = Mock(side_effect=type("TargetClosedError", (Exception,), {})("fake-secret"))
        session.open_mimo = Mock(return_value=page)
        session.get_cookie = Mock()
        with self.assertRaises(PlaywrightSessionClosedError) as raised:
            session.refresh_cookie(interactive=True)
        self.assertIn("等待 SSO", str(raised.exception))
        self.assertIn("headed", str(raised.exception))
        self.assertIn("TargetClosedError", str(raised.exception))
        self.assertNotIn("fake-secret", str(raised.exception))
        session.get_cookie.assert_not_called()

    def test_cookie_read_error_is_fatal_even_without_target_closed_type(self):
        session = PlaywrightSession(tempfile.mkdtemp())
        session.open_mimo = Mock(return_value=_FakePage())
        session.get_cookie = Mock(side_effect=PlaywrightSessionError("driver failed"))
        with self.assertRaisesRegex(PlaywrightSessionError, "driver failed"):
            session.refresh_cookie(interactive=True)
        session.get_cookie.assert_called_once()

    def test_module_refresh_cookie_forwards_validator(self):
        session = Mock()
        session.refresh_cookie.return_value = "mimo_token=ready"
        validator = Mock(return_value=True)
        with (
            patch.object(playwright_session, "PlaywrightSession", return_value=session),
            patch("config.playwright_user_data_dir", return_value="C:/isolated/mimo-profile"),
        ):
            result = refresh_cookie(
                interactive=True,
                timeout_seconds=180,
                cookie_validator=validator,
            )

        self.assertEqual(result, ("mimo_token=ready", None))
        session.refresh_cookie.assert_called_once_with(
            interactive=True,
            timeout_seconds=180,
            previous_cookie=None,
            cookie_validator=validator,
        )


if __name__ == "__main__":
    unittest.main()
