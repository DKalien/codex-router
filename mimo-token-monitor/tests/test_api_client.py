import unittest
from unittest.mock import Mock, patch

from api_client import (
    DEFAULT_THIRD_PARTY_BASE_URL,
    _third_party_usage_url,
    fetch_third_party_usage,
    parse_third_party_usage,
)


class TestDirectUsageRequests(unittest.TestCase):
    def test_direct_queries_keep_parameters_and_close_responses(self):
        import api_client
        cases = (
            (api_client.fetch_balance, ("cookie",), api_client.BALANCE_URL, api_client._headers("cookie"), 10, {"balance": 12}),
            (api_client.fetch_usage, ("cookie",), api_client.USAGE_URLS[0], api_client._headers("cookie"), 10, {"code": 0}),
            (api_client.fetch_third_party_usage, ("https://example.test", "key"), "https://example.test/v1/usage", {"Authorization": "Bearer key"}, 15, {"rate_limits": [{"window": "7d", "limit": 100, "used": 1}]}),
        )
        for fetch, args, url, headers, timeout, payload in cases:
            for status in (200, 401, "json_error"):
                with self.subTest(fetch=fetch.__name__, status=status), patch("api_client.requests.Session") as factory:
                    client = factory.return_value.__enter__.return_value
                    count = len(api_client.USAGE_URLS) if fetch is api_client.fetch_usage and status == "json_error" else 1
                    responses = [Mock(status_code=200 if status == "json_error" else status) for _ in range(count)]
                    for response in responses:
                        response.json.return_value = payload
                        if status == "json_error":
                            response.json.side_effect = ValueError("invalid JSON")
                    client.get.side_effect = responses
                    self.assertEqual(fetch(*args)["ok"], status == 200)
                    self.assertIs(client.trust_env, False)
                    if count == 1:
                        client.get.assert_called_once_with(url, headers=headers, timeout=timeout)
                    for response in responses:
                        response.close.assert_called_once()

    def test_mimo_usage_fallback_closes_each_response(self):
        import api_client
        with patch("api_client.requests.Session") as factory:
            client = factory.return_value.__enter__.return_value
            responses = [Mock(status_code=404), Mock(status_code=200)]
            responses[1].json.return_value = {"data": {}}
            client.get.side_effect = responses
            self.assertTrue(api_client.fetch_usage("cookie")["ok"])
            self.assertEqual(factory.call_count, 2)
            self.assertIs(client.trust_env, False)
            for response in responses:
                response.close.assert_called_once()


class TestThirdPartyUsageUrl(unittest.TestCase):
    def test_default_base_url(self):
        self.assertEqual(DEFAULT_THIRD_PARTY_BASE_URL, "https://codex.wlbclub.com")
        self.assertEqual(
            _third_party_usage_url(""),
            f"{DEFAULT_THIRD_PARTY_BASE_URL}/v1/usage",
        )

    def test_base_url_without_api_path(self):
        self.assertEqual(
            _third_party_usage_url("https://example.test/"),
            "https://example.test/v1/usage",
        )

    def test_base_url_with_v1_path(self):
        self.assertEqual(
            _third_party_usage_url("https://example.test/v1"),
            "https://example.test/v1/usage",
        )

    def test_full_usage_url_is_not_duplicated(self):
        self.assertEqual(
            _third_party_usage_url("https://example.test/v1/usage/"),
            "https://example.test/v1/usage",
        )

    @patch("api_client.requests.Session")
    def test_fetch_uses_normalized_v1_path(self, session):
        get = session.return_value.__enter__.return_value.get
        response = Mock(status_code=401)
        get.return_value = response

        result = fetch_third_party_usage("https://example.test/v1", "test-key")
        self.assertIs(session.return_value.__enter__.return_value.trust_env, False)
        response.close.assert_called_once()

        self.assertFalse(result["ok"])
        self.assertEqual(result["url"], "https://example.test/v1/usage")
        get.assert_called_once_with(
            "https://example.test/v1/usage",
            headers={"Authorization": "Bearer test-key"},
            timeout=15,
        )


class TestThirdPartyUsageParser(unittest.TestCase):
    def test_selected_window_preserves_reset_at(self):
        data = {
            "rate_limits": [
                {"window": "1d", "used": 1, "limit": 10, "reset_at": "ignored"},
                {
                    "window": "7d",
                    "used": 25,
                    "limit": 100,
                    "reset_at": "2026-09-01T00:00:00Z",
                },
            ]
        }

        result = parse_third_party_usage(data)

        self.assertEqual(result["reset_at"], "2026-09-01T00:00:00Z")
        self.assertEqual(result["used_percent"], 25.0)

    def test_missing_reset_at_is_backward_compatible(self):
        result = parse_third_party_usage(
            {"rate_limits": [{"window": "7d", "used": 25, "limit": 100}]}
        )

        self.assertIsNone(result["reset_at"])

    def test_preserves_relative_reset_seconds(self):
        data = {
            "rate_limits": [
                {"window": "1d", "used": 25, "limit": 100, "reset_after": 0}
            ]
        }
        result = parse_third_party_usage(data, "1d")

        self.assertEqual(result["reset_after_seconds"], 0)

    @patch("api_client.requests.Session")
    def test_fetch_keeps_weekly_flat_and_attaches_daily(self, session):
        get = session.return_value.__enter__.return_value.get
        response = Mock(status_code=200)
        response.json.return_value = {
            "rate_limits": [
                {"window": "1d", "used": 10, "limit": 50},
                {"window": "7d", "used": 25, "limit": 100},
            ]
        }
        get.return_value = response

        result = fetch_third_party_usage("https://example.test", "test-key")

        self.assertTrue(result["ok"])
        self.assertEqual(get.call_count, 1)
        self.assertEqual(result["data"]["window"], "7d")
        self.assertEqual(result["data"]["used_percent"], 25.0)
        self.assertEqual(result["data"]["daily"]["window"], "1d")
        self.assertEqual(result["data"]["daily"]["used_percent"], 20.0)

    def test_daily_usage_is_capped_by_weekly_remaining(self):
        result = parse_third_party_usage(
            {
                "rate_limits": [
                    {
                        "window": "1d",
                        "used": 19.345675,
                        "limit": 50,
                        "remaining": 30.654325,
                    },
                    {
                        "window": "7d",
                        "used": 120.020837,
                        "limit": 120,
                        "remaining": 0,
                    },
                ]
            }
        )

        daily = result["daily"]
        self.assertAlmostEqual(daily["used"], 19.345675)
        self.assertAlmostEqual(daily["limit"], 50.0)
        self.assertAlmostEqual(daily["remaining"], 30.654325)
        self.assertEqual(daily["used_percent"], 100.0)
        self.assertEqual(daily["remaining_percent"], 0.0)
        self.assertEqual(result["used_percent"], 100.02)

    @patch("api_client.requests.Session")
    def test_missing_daily_window_does_not_fail_weekly_fetch(self, session):
        get = session.return_value.__enter__.return_value.get
        response = Mock(status_code=200)
        response.json.return_value = {
            "rate_limits": [{"window": "7d", "used": 25, "limit": 100}]
        }
        get.return_value = response

        result = fetch_third_party_usage("https://example.test", "test-key")

        self.assertTrue(result["ok"])
        self.assertFalse(result["data"]["daily"]["has_rate_limit"])


class TestGPTWeeklyUsageParser(unittest.TestCase):
    def test_new_format_extracts_primary_and_secondary_windows(self):
        from api_client import _parse_gpt_windows

        data = {
            "rate_limits": {
                "primary": {"used_percent": 12.5, "window_minutes": 300, "reset_after_seconds": 3600},
                "secondary": {"used_percent": 42.5, "window_minutes": 10080, "reset_after_seconds": 86400},
            }
        }
        result = _parse_gpt_windows(data)
        self.assertAlmostEqual(result["primary"]["used_percent"], 12.5)
        self.assertAlmostEqual(result["secondary"]["used_percent"], 42.5)

    def test_list_windows_identified_by_duration(self):
        from api_client import _parse_gpt_windows

        data = {"rate_limits": [
            {"limit_window_seconds": 18000, "used_percent": 10},
            {"limit_window_seconds": 604800, "used_percent": 60},
        ]}
        result = _parse_gpt_windows(data)
        self.assertEqual(result["primary"]["used_percent"], 10.0)
        self.assertEqual(result["secondary"]["used_percent"], 60.0)

    def test_only_primary_window_has_no_weekly_result(self):
        from api_client import _parse_gpt_windows, _parse_gpt_secondary_window

        for minutes in (None, 300, 1440, "invalid"):
            with self.subTest(minutes=minutes):
                data = {"rate_limits": {"primary": {"used_percent": 10, "window_minutes": minutes}}}
                self.assertIsNone(_parse_gpt_secondary_window(data))
                self.assertEqual(_parse_gpt_windows(data)["primary"]["used_percent"], 10.0)

    def test_rate_limits_weekly_primary_in_minutes(self):
        from api_client import _gpt_result_with_windows

        data = {"rate_limits": {
            "primary": {"used_percent": 62, "window_minutes": 10080, "resets_at": 1792472967},
            "secondary": None,
        }}
        result = _gpt_result_with_windows(data, "local_jsonl")
        self.assertIsNone(result["primary"])
        self.assertEqual(result["secondary"]["used_percent"], 62.0)
        self.assertEqual(result["remaining_percent"], 38.0)
        self.assertEqual(result["reset_at"], 1792472967)

    def test_new_format_rate_limits_list(self):
        from api_client import _parse_gpt_secondary_window
        data = {"data": {"rate_limits": [{"window": "weekly", "used_percent": 42.5, "resets_at": "2026-08-01T00:00:00Z"}]}}
        result = _parse_gpt_secondary_window(data)
        self.assertIsNotNone(result)
        self.assertAlmostEqual(result["used_percent"], 42.5)
        self.assertAlmostEqual(result["remaining_percent"], 57.5)
        self.assertEqual(result["reset_at"], "2026-08-01T00:00:00Z")

    def test_old_format_secondary_window(self):
        from api_client import _parse_gpt_secondary_window
        data = {"rate_limit": {"secondary_window": {"used_percent": 75.0, "reset_after_seconds": 3600}}}
        result = _parse_gpt_secondary_window(data)
        self.assertIsNotNone(result)
        self.assertAlmostEqual(result["used_percent"], 75.0)
        self.assertEqual(result["reset_after_seconds"], 3600)

    def test_old_format_weekly_primary_window(self):
        from api_client import _parse_gpt_windows, _parse_gpt_secondary_window
        data = {"rate_limit": {"secondary_window": None, "primary_window": {
            "used_percent": 30.0,
            "limit_window_seconds": 604800,
            "reset_at": 1785923727,
            "reset_after_seconds": 525327,
        }}}
        result = _parse_gpt_secondary_window(data)
        self.assertIsNotNone(result)
        self.assertEqual(result["used_percent"], 30.0)
        self.assertEqual(result["reset_at"], 1785923727)
        self.assertIsNone(_parse_gpt_windows(data)["primary"])

    def test_old_format_five_hour_primary_is_not_weekly(self):
        from api_client import _parse_gpt_secondary_window
        data = {"rate_limit": {"secondary_window": None, "primary_window": {
            "used_percent": 30.0,
            "limit_window_seconds": 18000,
        }}}
        self.assertIsNone(_parse_gpt_secondary_window(data))

    def test_string_percent(self):
        from api_client import _parse_gpt_secondary_window
        data = {"rate_limits": [{"window": "weekly", "used_percent": "33.33"}]}
        result = _parse_gpt_secondary_window(data)
        self.assertAlmostEqual(result["used_percent"], 33.33)

    def test_clamp_above_100(self):
        from api_client import _parse_gpt_secondary_window
        data = {"rate_limits": [{"window": "weekly", "used_percent": 150.0}]}
        result = _parse_gpt_secondary_window(data)
        self.assertAlmostEqual(result["used_percent"], 100.0)

    def test_non_finite_percent_returns_none(self):
        from api_client import _parse_gpt_secondary_window
        data = {"rate_limits": [{"window": "weekly", "used_percent": "NaN"}]}
        self.assertIsNone(_parse_gpt_secondary_window(data))

    def test_no_secondary_returns_none(self):
        from api_client import _parse_gpt_secondary_window
        data = {"rate_limits": [{"window": "daily", "used_percent": 10.0}]}
        self.assertIsNone(_parse_gpt_secondary_window(data))

    def test_none_input(self):
        from api_client import _parse_gpt_secondary_window
        self.assertIsNone(_parse_gpt_secondary_window(None))
        self.assertIsNone(_parse_gpt_secondary_window("invalid"))

    def test_rate_limits_object_format(self):
        from api_client import _parse_gpt_secondary_window
        data = {"rate_limits": {"primary": {"used_percent": 10}, "secondary": {"used_percent": 65.5, "resets_at": "2026-08-01T00:00:00Z"}}}
        result = _parse_gpt_secondary_window(data)
        self.assertIsNotNone(result)
        self.assertAlmostEqual(result["used_percent"], 65.5)
        self.assertEqual(result["reset_at"], "2026-08-01T00:00:00Z")

    def test_rate_limits_object_nested_data(self):
        from api_client import _parse_gpt_secondary_window
        data = {"data": {"rate_limits": {"secondary": {"used_percent": 30.0}}}}
        result = _parse_gpt_secondary_window(data)
        self.assertIsNotNone(result)
        self.assertAlmostEqual(result["used_percent"], 30.0)

    def test_list_window_7d(self):
        from api_client import _parse_gpt_secondary_window
        data = {"rate_limits": [{"window": "7d", "used_percent": 22.0}]}
        result = _parse_gpt_secondary_window(data)
        self.assertIsNotNone(result)
        self.assertAlmostEqual(result["used_percent"], 22.0)

    def test_list_window_secondary(self):
        from api_client import _parse_gpt_secondary_window
        data = {"rate_limits": [{"window": "secondary", "used_percent": 44.0}]}
        result = _parse_gpt_secondary_window(data)
        self.assertIsNotNone(result)
        self.assertAlmostEqual(result["used_percent"], 44.0)

class TestFetchGPTWeeklyUsage(unittest.TestCase):

    def test_jsonl_selects_event_time_instead_of_file_modification_time(self):
        import json
        import os
        import pathlib
        import tempfile
        from api_client import _gpt_try_jsonl

        with tempfile.TemporaryDirectory() as td:
            sessions = pathlib.Path(td) / ".codex" / "sessions"
            sessions.mkdir(parents=True)
            for name, timestamp, used, modified in (
                ("old", "2026-10-29T16:00:00+08:00", 1, 200),
                ("new", "2026-10-29T09:00:00Z", 9, 100),
            ):
                path = sessions / f"rollout-{name}.jsonl"
                entry = {"timestamp": timestamp, "payload": {
                    "rate_limits": {"secondary": {"used_percent": used}},
                }}
                path.write_text(json.dumps(entry) + '\n' + json.dumps({"payload": {"message": "继续执行"}}), encoding="utf-8")
                os.utime(path, (modified, modified))
            with patch("api_client._pl.Path.home", return_value=pathlib.Path(td)):
                result = _gpt_try_jsonl()

        self.assertEqual(result["used_percent"], 9)
        self.assertEqual(result["observed_at"], "2026-10-29T09:00:00Z")

    def test_jsonl_ignores_missing_invalid_or_naive_timestamps(self):
        import json
        import pathlib
        import tempfile
        from api_client import _gpt_try_jsonl

        with tempfile.TemporaryDirectory() as td:
            sessions = pathlib.Path(td) / ".codex" / "sessions"
            sessions.mkdir(parents=True)
            entries = [{"timestamp": timestamp, "payload": {"rate_limits": {
                "secondary": {"used_percent": 9},
            }}} for timestamp in (None, "invalid", "2026-10-29T09:00:00")]
            (sessions / "rollout-invalid.jsonl").write_text(
                '\n'.join(json.dumps(entry) for entry in entries), encoding="utf-8",
            )
            errors = []
            with patch("api_client._pl.Path.home", return_value=pathlib.Path(td)):
                self.assertIsNone(_gpt_try_jsonl(errors))
            self.assertIn("有效时间", errors[0])

    def test_jsonl_new_snapshot_does_not_reuse_old_primary_window(self):
        import json
        import pathlib
        import tempfile
        from api_client import _gpt_try_jsonl

        with tempfile.TemporaryDirectory() as td:
            sessions = pathlib.Path(td) / ".codex" / "sessions"
            sessions.mkdir(parents=True)
            entries = [
                {"timestamp": "2026-10-29T08:00:00Z", "payload": {"rate_limits": {
                    "primary": {"used_percent": 6}, "secondary": {"used_percent": 1},
                }}},
                {"timestamp": "2026-10-29T09:00:00Z", "payload": {"rate_limits": {
                    "secondary": {"used_percent": 9},
                }}},
            ]
            (sessions / "rollout-one.jsonl").write_text(
                '\n'.join(json.dumps(entry) for entry in entries), encoding="utf-8",
            )
            with patch("api_client._pl.Path.home", return_value=pathlib.Path(td)):
                result = _gpt_try_jsonl()
        self.assertIsNone(result["primary"])
        self.assertEqual(result["secondary"]["used_percent"], 9)

    def test_jsonl_fallback_retains_live_source_failures(self):
        from api_client import fetch_gpt_weekly_usage

        def fail_auth(errors):
            errors.append("本机 Codex 登录: 请求超时")

        fallback = {"source": "local_jsonl", "used_percent": 1}
        with (patch("api_client._gpt_try_local_auth", side_effect=fail_auth),
              patch("api_client._gpt_try_jsonl", return_value=fallback)):
            result = fetch_gpt_weekly_usage()

        self.assertTrue(result["ok"])
        self.assertIn("本机 Codex 登录: 请求超时", result["data"]["source_errors"])
        self.assertIn("ChatGPT Cookie: 未配置", result["data"]["source_errors"])

    def test_live_usage_success_does_not_read_historical_fallback(self):
        from api_client import fetch_gpt_weekly_usage

        live = {"source": "local_codex_auth", "used_percent": 9}
        with (patch("api_client._gpt_try_local_auth", return_value=live),
              patch("api_client._gpt_try_session_cookie") as cookie,
              patch("api_client._gpt_try_jsonl") as history):
            result = fetch_gpt_weekly_usage()
        self.assertIs(result["data"], live)
        cookie.assert_not_called()
        history.assert_not_called()

    def test_gpt_session_reused_and_cookies_cleared(self):
        import api_client
        session = api_client._GPT_SESSION
        headers = {"Cookie": "explicit=one"}
        seen = []

        def get(url, **kwargs):
            seen.append((api_client._GPT_SESSION, dict(session.cookies), url, kwargs))
            session.cookies.set("response-cookie", "secret")
            return Mock(status_code=200)

        session.cookies.set("stale", "secret")
        with patch.object(session, "get", side_effect=get):
            api_client._gpt_get("https://example.test/one", headers)
            api_client._gpt_get("https://example.test/two", headers)
        self.assertEqual([entry[0] for entry in seen], [session, session])
        self.assertEqual([entry[1] for entry in seen], [{}, {}])
        self.assertEqual(seen[0][3], {"headers": headers, "timeout": 15, "allow_redirects": False})
        self.assertEqual(dict(session.cookies), {})

    def test_gpt_session_access_is_serialized(self):
        import concurrent.futures
        import threading
        import time
        import api_client

        active = 0
        peak = 0
        guard = threading.Lock()
        def get(url, **kwargs):
            nonlocal active, peak
            with guard:
                active += 1
                peak = max(peak, active)
            time.sleep(0.03)
            with guard:
                active -= 1
            return Mock(status_code=200)

        with patch.object(api_client._GPT_SESSION, "get", side_effect=get):
            with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
                list(pool.map(lambda _: api_client._gpt_get("https://example.test", {}), range(4)))
        self.assertEqual(peak, 1)

    def test_gpt_session_cookies_cleared_on_network_failure(self):
        import api_client
        import requests
        session = api_client._GPT_SESSION
        def fail(*args, **kwargs):
            self.assertEqual(dict(session.cookies), {})
            session.cookies.set("response-cookie", "secret")
            raise requests.exceptions.ConnectionError()
        with (patch.object(session, "get", side_effect=fail),
              patch("api_client.gpt_browser.fetch_gpt_response", return_value=Mock(status_code=200))):
            api_client._gpt_get("https://example.test", {})
        self.assertEqual(dict(session.cookies), {})

    def test_transient_status_is_retried_once(self):
        from unittest.mock import Mock, patch
        from api_client import _gpt_get
        unavailable = Mock(status_code=503)
        success = Mock(status_code=200)
        with patch("api_client._GPT_SESSION.get", side_effect=[unavailable, success]) as get:
            result = _gpt_get("https://example.test", {})
        self.assertIs(result, success)
        self.assertEqual(get.call_count, 2)
        unavailable.close.assert_called_once()

    def test_network_failure_uses_browser_with_same_authentication(self):
        import requests
        from api_client import _gpt_get, GPT_USAGE_URL
        headers = {"Authorization": "Bearer test-token", "ChatGPT-Account-ID": "test-account"}
        browser_response = Mock(status_code=200)
        with (patch("api_client._GPT_SESSION.get", side_effect=requests.exceptions.ProxyError("test-secret")) as get,
              patch("api_client.gpt_browser.fetch_gpt_response", return_value=browser_response) as browser):
            result = _gpt_get(GPT_USAGE_URL, headers)
        self.assertIs(result, browser_response)
        self.assertEqual(get.call_count, 2)
        get.assert_called_with(GPT_USAGE_URL, headers=headers, timeout=15, allow_redirects=False)
        browser.assert_called_once_with(GPT_USAGE_URL, headers)

    def test_http_responses_do_not_start_browser(self):
        from api_client import _gpt_get, GPT_USAGE_URL
        for status in (200, 302, 401, 403, 429, 503):
            with self.subTest(status=status):
                responses = [Mock(status_code=status), Mock(status_code=status)]
                with (patch("api_client._GPT_SESSION.get", side_effect=responses) as get,
                      patch("api_client.gpt_browser.fetch_gpt_response") as browser):
                    result = _gpt_get(GPT_USAGE_URL, {})
                self.assertEqual(result.status_code, status)
                self.assertEqual(get.call_count, 2 if status in (429, 503) else 1)
                browser.assert_not_called()

    def test_both_network_clients_fail_with_safe_source_reason(self):
        import requests
        from api_client import fetch_gpt_weekly_usage
        cases = (
            (requests.exceptions.ProxyError, "代理连接失败"),
            (requests.exceptions.SSLError, "TLS 连接失败"),
            (requests.exceptions.ConnectTimeout, "连接超时"),
            (requests.exceptions.ReadTimeout, "读取响应超时"),
        )
        for error_type, expected in cases:
            with self.subTest(error_type=error_type):
                def auth(errors):
                    from api_client import _gpt_get, GPT_USAGE_URL
                    return _gpt_get(GPT_USAGE_URL, {})
                with (patch("api_client._gpt_try_local_auth", side_effect=auth),
                      patch("api_client._gpt_try_jsonl", return_value=None),
                      patch("api_client._GPT_SESSION.get", side_effect=error_type("test-secret")),
                      patch("api_client.gpt_browser.fetch_gpt_response",
                            side_effect=requests.exceptions.RequestException("test-secret"))):
                    result = fetch_gpt_weekly_usage()
                self.assertFalse(result["ok"])
                self.assertIn(expected, result["error"])
                self.assertIn("Chromium 兜底查询也未成功", result["error"])
                self.assertNotIn("test-secret", result["error"])

    def test_session_cookie_uses_get(self):
        from unittest.mock import patch, Mock
        from api_client import fetch_gpt_weekly_usage, GPT_AUTH_SESSION_URL, GPT_USAGE_URL
        auth_resp = Mock(status_code=200)
        auth_resp.json.return_value = {"accessToken": "tok", "account": {"id": "aid"}}
        usage_resp = Mock(status_code=200)
        usage_resp.json.return_value = {"rate_limits": {"secondary": {"used_percent": 10.0}}}
        calls = []
        def track_get(url, **kwargs):
            calls.append(url)
            return auth_resp if url == GPT_AUTH_SESSION_URL else usage_resp
        with (patch("api_client._gpt_try_local_auth", return_value=None),
            patch("api_client._GPT_SESSION.get", side_effect=track_get)):
            result = fetch_gpt_weekly_usage(session_cookie="cookie")
        self.assertTrue(result["ok"])
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0], GPT_AUTH_SESSION_URL)
        self.assertEqual(calls[1], GPT_USAGE_URL)

    def test_session_cookie_two_step(self):
        from unittest.mock import patch, Mock
        from api_client import fetch_gpt_weekly_usage
        auth_resp = Mock(status_code=200)
        auth_resp.json.return_value = {"accessToken": "tok123", "account": {"id": "acc1"}}
        usage_data = {"rate_limits": [{"window": "weekly", "used_percent": 55.0}]}
        usage_resp = Mock(status_code=200)
        usage_resp.json.return_value = usage_data
        with (patch("api_client._GPT_SESSION.get", side_effect=[auth_resp, usage_resp]),
            patch("api_client._gpt_try_local_auth", return_value=None)):
            result = fetch_gpt_weekly_usage(session_cookie="sess_token=abc")
        self.assertTrue(result["ok"])
        self.assertAlmostEqual(result["data"]["used_percent"], 55.0)
        self.assertEqual(result["data"]["source"], "session_cookie")


    def test_local_auth_account_id_priority(self):
        import json
        import pathlib
        import tempfile
        from unittest.mock import patch, Mock
        from api_client import _gpt_try_local_auth
        usage_data = {"rate_limits": {"secondary": {"used_percent": 20.0}}}
        usage_resp = Mock(status_code=200)
        usage_resp.json.return_value = usage_data
        with tempfile.TemporaryDirectory() as td:
            auth_dir = pathlib.Path(td) / ".codex"
            auth_dir.mkdir()
            auth = {"tokens": {"access_token": "tok", "account_id": "from_tokens"}, "account_id": "from_root"}
            (auth_dir / "auth.json").write_text(json.dumps(auth), encoding="utf-8")
            with (
                patch("api_client._pl.Path.home", return_value=pathlib.Path(td)) as _ph,
                patch("api_client._GPT_SESSION.get", return_value=usage_resp) as mock_get,
            ):
                result = _gpt_try_local_auth()
            self.assertIsNotNone(result)
            self.assertEqual(result["source"], "local_codex_auth")
            # Verify tokens.account_id was used (not root.account_id)
            headers = mock_get.call_args[1].get("headers", {})
            self.assertEqual(headers.get("ChatGPT-Account-ID"), "from_tokens")

    def test_no_data_returns_error(self):
        from unittest.mock import patch
        from api_client import fetch_gpt_weekly_usage
        with (patch("api_client._gpt_try_local_auth", return_value=None),
            patch("api_client._gpt_try_session_cookie", return_value=None),
            patch("api_client._gpt_try_jsonl", return_value=None)):
            result = fetch_gpt_weekly_usage()
        self.assertFalse(result["ok"])
        self.assertIn("GPT", result["error"])
        # Must not display 0% for missing data
        self.assertIsNone(result["data"])

    def test_failure_reports_each_source_reason(self):
        import pathlib
        import tempfile
        from unittest.mock import patch
        from api_client import fetch_gpt_weekly_usage
        with tempfile.TemporaryDirectory() as td:
            with patch("api_client._pl.Path.home", return_value=pathlib.Path(td)):
                result = fetch_gpt_weekly_usage()
        self.assertFalse(result["ok"])
        self.assertIn("本机 Codex 登录: 未找到登录令牌", result["error"])
        self.assertIn("ChatGPT Cookie: 未配置", result["error"])
        self.assertIn("本地会话: 会话目录不存在", result["error"])

    def test_jsonl_fallback(self):
        import json
        import pathlib
        import tempfile
        from unittest.mock import patch
        from api_client import fetch_gpt_weekly_usage
        with tempfile.TemporaryDirectory() as td:
            codex_dir = pathlib.Path(td) / ".codex"
            codex_dir.mkdir()
            sessions = codex_dir / "sessions"
            sessions.mkdir()
            jsonl_path = sessions / "rollout-2026.jsonl"
            entry = {"timestamp": "2026-10-29T09:00:00Z", "payload": {"rate_limits": [{"window": "weekly", "used_percent": 88.0}]}}
            jsonl_path.write_text(json.dumps(entry), encoding="utf-8")
            with (patch("api_client._gpt_try_local_auth", return_value=None),
                patch("api_client._gpt_try_session_cookie", return_value=None),
                patch("api_client._pl.Path.home", return_value=pathlib.Path(td))):
                result = fetch_gpt_weekly_usage()
            self.assertTrue(result["ok"])
            self.assertAlmostEqual(result["data"]["used_percent"], 88.0)
            self.assertEqual(result["data"]["source"], "local_jsonl")


    def test_jsonl_rate_limits_object(self):
        import json
        import pathlib
        import tempfile
        from unittest.mock import patch
        import requests
        from api_client import fetch_gpt_weekly_usage
        with tempfile.TemporaryDirectory() as td:
            codex_dir = pathlib.Path(td) / ".codex"
            codex_dir.mkdir()
            sessions = codex_dir / "sessions"
            sessions.mkdir()
            jsonl_path = sessions / "rollout-2026.jsonl"
            # rate_limits as dict object (not list)
            entry = {"timestamp": "2026-10-29T09:00:00Z", "payload": {"rate_limits": {
                "primary": {"used_percent": 62, "window_minutes": 10080, "resets_at": 1792472967},
                "secondary": None,
            }}}
            jsonl_path.write_text(json.dumps(entry), encoding="utf-8")
            (codex_dir / "auth.json").write_text(
                json.dumps({"tokens": {"access_token": "test-token"}}), encoding="utf-8"
            )
            with (patch("api_client._GPT_SESSION.get", side_effect=requests.exceptions.ConnectionError) as get,
                patch("api_client.gpt_browser.fetch_gpt_response", side_effect=requests.exceptions.RequestException),
                patch("api_client._pl.Path.home", return_value=pathlib.Path(td))):
                result = fetch_gpt_weekly_usage(session_cookie="test-cookie")
            self.assertTrue(result["ok"])
            self.assertEqual(get.call_count, 4)
            self.assertEqual(result["data"]["source"], "local_jsonl")
            self.assertIsNone(result["data"]["primary"])
            self.assertEqual(result["data"]["used_percent"], 62.0)
            self.assertEqual(result["data"]["remaining_percent"], 38.0)
            self.assertEqual(result["data"]["secondary"]["reset_at"], 1792472967)

    def test_http_error_returns_none(self):
        import json
        import pathlib
        import tempfile
        from unittest.mock import patch, Mock
        from api_client import _gpt_try_local_auth
        with tempfile.TemporaryDirectory() as td:
            auth_dir = pathlib.Path(td) / ".codex"
            auth_dir.mkdir()
            (auth_dir / "auth.json").write_text(json.dumps({"tokens": {"access_token": "tok"}}), encoding="utf-8")
            resp = Mock(status_code=403)
            with (patch("api_client._pl.Path.home", return_value=pathlib.Path(td)),
                patch("api_client._GPT_SESSION.get", return_value=resp)):
                result = _gpt_try_local_auth()
            self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
