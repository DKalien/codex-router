import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import supergpt_balance


class TestSuperGPTBalance(unittest.TestCase):
    def test_reuses_router_key_without_writing_other_settings(self):
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp) / "codex-router"
            state.mkdir()
            secret = state / "supergpt-api-key.secret"
            secret.write_text(" test-local-secret \n", encoding="utf-8")
            with patch.dict(os.environ, {"CODEX_HOME": temp}, clear=True):
                self.assertEqual(supergpt_balance._api_key(), "test-local-secret")
            self.assertEqual(secret.read_text(encoding="utf-8"), " test-local-secret \n")
            with patch.dict(os.environ, {"SUPERGPT_API_KEY": " test-env-key "}, clear=True):
                self.assertEqual(supergpt_balance._api_key(), "test-env-key")
            with patch.dict(os.environ, {"MODEL_ROUTER_STATE_DIR": str(state), "CODEX_HOME": "unused"}, clear=True):
                self.assertEqual(supergpt_balance._api_key(), "test-local-secret")

    def test_link_or_non_file_credentials_are_rejected(self):
        for mode in (stat.S_IFLNK, stat.S_IFDIR):
            with self.subTest(mode=mode), patch.dict(os.environ, {"CODEX_HOME": "unused"}, clear=True), \
                 patch.object(Path, "lstat", return_value=Mock(st_mode=mode)), \
                 patch.object(Path, "read_text") as read, \
                 patch("supergpt_balance.requests.Session") as session:
                result = supergpt_balance.fetch_balance()
                self.assertFalse(result["ok"])
                read.assert_not_called()
                session.assert_not_called()

    def test_missing_key_never_makes_a_request(self):
        with patch("supergpt_balance._api_key", return_value=""), patch("supergpt_balance.requests.Session") as session:
            self.assertFalse(supergpt_balance.fetch_balance()["ok"])
            session.assert_not_called()

    def test_balance_is_account_balance_including_zero_not_key_remaining(self):
        for amount in (0, 35.11384311, "12.50", -0.01):
            with self.subTest(amount=amount):
                response = Mock(status_code=200)
                response.json.return_value = {"balance": amount, "remaining": 999, "unit": "USD", "isValid": True}
                with patch("supergpt_balance._api_key", return_value="test-secret"), \
                     patch("supergpt_balance.requests.Session") as session:
                    client = session.return_value.__enter__.return_value
                    client.get.return_value = response
                    self.assertEqual(supergpt_balance.fetch_balance(), {"ok": True, "balance": float(amount), "currency": "USD", "error": None})
                    session.assert_called_once_with()
                    self.assertIs(client.trust_env, False)
                    client.get.assert_called_once_with("https://api.supergpt.dev/v1/usage", headers={"Authorization": "Bearer test-secret"}, timeout=(3, 10), allow_redirects=False)
                    response.close.assert_called_once()

    def test_custom_url_and_api_key_override_router_defaults(self):
        response = Mock(status_code=200)
        response.json.return_value = {"balance": 12, "unit": "USD", "isValid": True}
        with patch("supergpt_balance._api_key", side_effect=OSError("unreadable")) as router_key, \
             patch("supergpt_balance.requests.Session") as session:
            client = session.return_value.__enter__.return_value
            client.get.return_value = response
            for base in ("https://example.test", "https://example.test/v1/",
                         "https://example.test/v1/usage/"):
                with self.subTest(base=base):
                    client.get.reset_mock()
                    self.assertTrue(supergpt_balance.fetch_balance(base, " configured-key ")["ok"])
                    client.get.assert_called_once_with(
                        "https://example.test/v1/usage",
                        headers={"Authorization": "Bearer configured-key"},
                        timeout=(3, 10), allow_redirects=False,
                    )
                    response.close.assert_called_once()
            router_key.assert_not_called()

    def test_blank_setting_key_keeps_router_fallback_with_custom_url(self):
        response = Mock(status_code=200)
        response.json.return_value = {"balance": 12, "unit": "USD"}
        with patch("supergpt_balance._api_key", return_value="router-key"), \
             patch("supergpt_balance.requests.Session") as session:
            client = session.return_value.__enter__.return_value
            client.get.return_value = response
            self.assertTrue(supergpt_balance.fetch_balance(" https://example.test ", " ")["ok"])
            self.assertEqual(client.get.call_args.args[0], "https://example.test/v1/usage")
            self.assertEqual(client.get.call_args.kwargs["headers"], {"Authorization": "Bearer router-key"})

    def test_invalid_url_fails_before_reading_or_sending_credentials(self):
        for url in ("example.test", "file:///tmp/secret", "https://",
                    "https://user:password@example.test", "https://[bad",
                    "https://example.test:bad", "https://example.test?token=secret",
                    "https://example.test#fragment"):
            with self.subTest(url=url), patch("supergpt_balance._api_key") as router_key, \
                 patch("supergpt_balance.requests.Session") as session:
                result = supergpt_balance.fetch_balance(url)
                self.assertFalse(result["ok"])
                self.assertIn("URL 无效", result["error"])
                self.assertNotIn("secret", result["error"])
                router_key.assert_not_called()
                session.assert_not_called()

    def test_invalid_response_never_becomes_zero_or_exposes_body(self):
        values = [{"remaining": 999, "unit": "USD"}, {"balance": True, "unit": "USD"},
                  {"balance": "NaN", "unit": "USD"}, {"balance": "inf", "unit": "USD"},
                  {"balance": 30, "unit": "unknown"}, {"balance": 30, "unit": "USD", "isValid": False}, []]
        for value in values:
            with self.subTest(value=value):
                response = Mock(status_code=200)
                response.json.return_value = value
                with patch("supergpt_balance._api_key", return_value="test-secret"), patch("supergpt_balance.requests.Session") as session:
                    session.return_value.__enter__.return_value.get.return_value = response
                    result = supergpt_balance.fetch_balance()
                self.assertFalse(result["ok"])
                self.assertIsNone(result["balance"])
                response.close.assert_called_once()

    def test_http_json_and_network_failures_are_safe(self):
        for code in (301, 401, 403, 429, 500):
            with self.subTest(code=code):
                response = Mock(status_code=code, text="test-secret")
                with patch("supergpt_balance._api_key", return_value="test-secret"), patch("supergpt_balance.requests.Session") as session:
                    session.return_value.__enter__.return_value.get.return_value = response
                    result = supergpt_balance.fetch_balance()
                self.assertFalse(result["ok"])
                self.assertNotIn("test-secret", result["error"])
                response.json.assert_not_called()
                response.close.assert_called_once()
        response = Mock(status_code=200)
        response.json.side_effect = ValueError("test-secret")
        with patch("supergpt_balance._api_key", return_value="test-secret"), patch("supergpt_balance.requests.Session") as session:
            session.return_value.__enter__.return_value.get.return_value = response
            self.assertNotIn("test-secret", supergpt_balance.fetch_balance()["error"])
            response.close.assert_called_once()
        with patch("supergpt_balance._api_key", return_value="test-secret"), \
             patch("supergpt_balance.requests.Session") as session:
            session.return_value.__enter__.return_value.get.side_effect = supergpt_balance.requests.Timeout("test-secret")
            self.assertNotIn("test-secret", supergpt_balance.fetch_balance()["error"])


if __name__ == "__main__":
    unittest.main()
