import json
import unittest
from unittest.mock import Mock, patch

import requests

import api_client


def response(status, payload, headers=None):
    result = requests.Response()
    result.status_code = status
    result._content = json.dumps(payload).encode("utf-8")
    result._content_consumed = True
    result.headers.update({"Content-Type": "application/json", **(headers or {})})
    result.close = Mock(wraps=result.close)
    return result


def key_response(headers=None):
    return response(200, {"code": 0, "data": {
        "signKey": "fake-sign-key", "epoch": 1799999000,
    }}, headers)


class TestMimoSigning(unittest.TestCase):
    def test_both_queries_recover_from_explicit_signature_challenge(self):
        for fetch, payload in ((api_client.fetch_balance, {"balance": 0}),
                               (api_client.fetch_usage, {"code": 0, "data": {}})):
            for status in (401, 403):
                with self.subTest(fetch=fetch.__name__, status=status), patch("api_client.requests.Session") as factory:
                    session = factory.return_value.__enter__.return_value
                    challenge = response(status, {"code": "SIGN_MISSING"})
                    key = key_response()
                    success = response(200, payload)
                    session.get.side_effect = [challenge, key, success]
                    result = fetch("fake-cookie")
                    self.assertTrue(result["ok"], result)
                    self.assertFalse(result["auth_expired"])
                    self.assertIs(session.trust_env, False)
                    self.assertEqual(session.get.call_count, 3)
                    self.assertEqual(session.get.call_args_list[1].args,
                                     (api_client.BASE + "/api/v1/user/sign-key",))
                    headers = session.get.call_args_list[2].kwargs["headers"]
                    self.assertEqual(headers["Cookie"], "fake-cookie")
                    self.assertEqual(headers["X-Key-Epoch"], "1799999000")
                    self.assertEqual(len(headers["X-Sign"]), 64)
                    self.assertNotIn("fake-sign-key", str(result))
                    for request in session.get.call_args_list:
                        self.assertIs(request.kwargs["allow_redirects"], False)
                    for item in (challenge, key, success):
                        item.close.assert_called_once()

    def test_signature_matches_node_crypto_reference_and_server_clock(self):
        for server_time in ("1800000000000", "1800000000"):
            with self.subTest(server_time=server_time):
                session = Mock()
                key = key_response({"x-server-time": server_time})
                session.get.return_value = key
                with patch("api_client.time.time", return_value=123):
                    headers = api_client._mimo_sign_headers(
                        session, api_client.USAGE_URLS[0] + "?z=9&a=2&&a=%2B", "fake-cookie",
                    )
                # 独立 Node crypto 参考值，含 query 原始编码排序和 GET 空正文。
                self.assertEqual(headers, {
                    "X-Sign": "4e3ee5daa726ca38131e904e0b5a7f30e87d4f6b8e42dceb8a9ed3e93aa117dc",
                    "X-Timestamp": "1800000000", "X-Key-Epoch": "1799999000",
                })
                key.close.assert_called_once()

    def test_invalid_server_clock_uses_local_time(self):
        for value in (None, "bad", "nan", "inf", "-1"):
            with self.subTest(value=value), patch("api_client.time.time", return_value=1800000000):
                session = Mock()
                session.get.return_value = key_response({"x-server-time": value})
                headers = api_client._mimo_sign_headers(session, api_client.BALANCE_URL, "fake-cookie")
                self.assertEqual(headers["X-Timestamp"], "1800000000")

    def test_general_403_and_invalid_signature_never_request_key(self):
        for payload, content_type in (({"code": "FORBIDDEN", "message": "fake-secret"}, "application/json"),
                                      ({"code": "SIGN_INVALID"}, "application/json"),
                                      ({"code": "SIGN_MISSING"}, "text/html")):
            with self.subTest(payload=payload, content_type=content_type), patch("api_client.requests.Session") as factory:
                session = factory.return_value.__enter__.return_value
                item = response(403, payload, {"Content-Type": content_type})
                session.get.return_value = item
                result = api_client.fetch_balance("fake-cookie")
                self.assertFalse(result["ok"])
                self.assertFalse(result["auth_expired"])
                session.get.assert_called_once()
                self.assertNotIn("fake-secret", str(result))
                item.close.assert_called_once()

    def test_remaining_signature_error_is_terminal_and_not_cookie_expiry(self):
        for fetch in (api_client.fetch_balance, api_client.fetch_usage):
            for code in ("SIGN_KEY_MISSING", "SIGN_MISSING", "TIMESTAMP_INVALID"):
                with self.subTest(fetch=fetch.__name__, code=code), patch("api_client.requests.Session") as factory:
                    session = factory.return_value.__enter__.return_value
                    items = [response(403, {"code": code}), key_response(),
                             response(401, {"code": "TIMESTAMP_INVALID"})]
                    session.get.side_effect = items
                    result = fetch("fake-cookie")
                    self.assertFalse(result["ok"])
                    self.assertFalse(result["auth_expired"])
                    self.assertIn("TIMESTAMP_INVALID", result["error"])
                    self.assertEqual(session.get.call_count, 3)
                    for item in items:
                        item.close.assert_called_once()

    def test_key_http_failure_distinguishes_expired_login_and_refuses_redirect(self):
        for fetch in (api_client.fetch_balance, api_client.fetch_usage):
            for status in (302, 401, 403, 500):
                with self.subTest(fetch=fetch.__name__, status=status), patch("api_client.requests.Session") as factory:
                    session = factory.return_value.__enter__.return_value
                    items = [response(403, {"code": "SIGN_MISSING"}),
                             response(status, {"message": "fake-secret"},
                                      {"Location": "https://untrusted.example/fake-secret"})]
                    session.get.side_effect = items
                    result = fetch("fake-cookie")
                    self.assertFalse(result["ok"])
                    self.assertIs(result["auth_expired"], status == 401)
                    self.assertIn(f"/api/v1/user/sign-key: HTTP {status}", result["error"])
                    self.assertNotIn("fake-secret", str(result))
                    self.assertEqual(session.get.call_count, 2)
                    self.assertIs(session.get.call_args.kwargs["stream"], True)
                    self.assertIs(session.get.call_args.kwargs["allow_redirects"], False)
                    for item in items:
                        item.close.assert_called_once()

    def test_invalid_key_payloads_are_redacted(self):
        payloads = (
            {"code": 403, "message": "fake-secret"}, {"data": {}}, {"data": []},
            {"data": {"signKey": "fake-secret", "epoch": True}},
            {"data": {"signKey": "fake-secret", "epoch": "nan"}},
            {"data": {"signKey": "fake-secret", "epoch": -1}},
            {"data": {"signKey": "fake-secret", "epoch": 1.5}},
            {"data": {"signKey": {}, "epoch": 1}},
            {"data": {"signKey": "", "epoch": 1}},
        )
        for payload in payloads:
            with self.subTest(payload=payload), patch("api_client.requests.Session") as factory:
                session = factory.return_value.__enter__.return_value
                items = [response(403, {"code": "SIGN_MISSING"}), response(200, payload)]
                session.get.side_effect = items
                result = api_client.fetch_balance("fake-cookie")
                self.assertFalse(result["ok"])
                self.assertFalse(result["auth_expired"])
                self.assertNotIn("fake-secret", str(result))
                self.assertEqual(session.get.call_count, 2)
                for item in items:
                    item.close.assert_called_once()

    def test_key_response_is_bounded_and_malformed_json_is_safe(self):
        for body in (b"x" * (64 * 1024 + 1), b"fake-secret"):
            with self.subTest(size=len(body)), patch("api_client.requests.Session") as factory:
                session = factory.return_value.__enter__.return_value
                key = key_response()
                key._content = body
                session.get.side_effect = [response(403, {"code": "SIGN_MISSING"}), key]
                result = api_client.fetch_balance("fake-cookie")
                self.assertFalse(result["ok"])
                self.assertNotIn("fake-secret", str(result))
                key.close.assert_called_once()

    def test_network_failure_closes_resources_and_redacts_exception(self):
        for position in (1, 2):
            with self.subTest(position=position), patch("api_client.requests.Session") as factory:
                session = factory.return_value.__enter__.return_value
                challenge = response(403, {"code": "SIGN_MISSING"})
                key = key_response()
                items = [challenge, key, requests.exceptions.ReadTimeout("fake-secret")]
                items[position] = requests.exceptions.ReadTimeout("fake-secret")
                session.get.side_effect = items
                result = api_client.fetch_balance("fake-cookie")
                self.assertFalse(result["ok"])
                self.assertIn("超时", result["error"])
                self.assertNotIn("fake-secret", str(result))
                challenge.close.assert_called_once()
                if position == 2:
                    key.close.assert_called_once()

    def test_challenge_parser_accepts_fixed_messages_and_limits_json(self):
        item = response(403, {"message": "缺少签名请求头"})
        self.assertEqual(api_client._mimo_sign_error(item), "SIGN_MISSING")
        item._content = b"x" * (64 * 1024 + 1)
        self.assertIsNone(api_client._mimo_sign_error(item))
        item._content = b"fake-secret"
        self.assertIsNone(api_client._mimo_sign_error(item))


if __name__ == "__main__":
    unittest.main()
