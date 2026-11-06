import os
import unittest
from unittest.mock import patch

from usage_url import (
    LOOPBACK_HTTP_ENV,
    UsageUrlError,
    endpoint_host,
    usage_endpoint,
    validate_base_url,
)

DEFAULT = "https://codex.wlbclub.com"


class TestValidateBaseUrl(unittest.TestCase):
    def test_empty_falls_back_to_default_and_normalizes(self):
        self.assertEqual(validate_base_url("", default_base=DEFAULT), DEFAULT)
        self.assertEqual(
            validate_base_url(" https://example.test/ ", default_base=DEFAULT),
            "https://example.test",
        )
        self.assertEqual(
            validate_base_url("https://example.test/v1/", default_base=DEFAULT),
            "https://example.test/v1",
        )

    def test_https_is_allowed_with_custom_ports_and_paths(self):
        self.assertEqual(
            validate_base_url("https://example.test:8443/base", default_base=DEFAULT),
            "https://example.test:8443/base",
        )

    def test_http_is_rejected_unless_opt_in_loopback(self):
        with patch.dict(os.environ, {LOOPBACK_HTTP_ENV: ""}):
            for base in ("http://example.test", "http://127.0.0.1", "http://localhost"):
                with self.subTest(base=base):
                    with self.assertRaises(UsageUrlError):
                        validate_base_url(base, default_base=DEFAULT)

        with patch.dict(os.environ, {LOOPBACK_HTTP_ENV: "1"}):
            for base in ("http://127.0.0.1:9000", "http://localhost:9000", "http://[::1]:9000"):
                with self.subTest(base=base):
                    self.assertEqual(
                        validate_base_url(base, default_base=DEFAULT),
                        base,
                    )
            with self.assertRaises(UsageUrlError):
                validate_base_url("http://example.test", default_base=DEFAULT)

    def test_credentials_query_fragment_and_junk_are_rejected(self):
        for base in (
            "https://user:password@example.test",
            "https://user@example.test",
            "https://example.test?token=secret-query",
            "https://example.test#secret-fragment",
            "https://example.test:bad",
            "https://[bad",
            "example.test",
            "ftp://example.test",
            "https://",
            "https://exa mple.test",
            "https://example.test\\\\@evil.test",
        ):
            with self.subTest(base=base):
                with self.assertRaises(UsageUrlError):
                    validate_base_url(base, default_base=DEFAULT)

    def test_error_messages_never_echo_input(self):
        for base in (
            "https://user:password-secret@example.test",
            "https://example.test?token=secret-query",
            "https://example.test#secret-fragment",
        ):
            with self.subTest(base=base):
                try:
                    validate_base_url(base, default_base=DEFAULT)
                except UsageUrlError as exc:
                    message = str(exc)
                else:
                    self.fail("expected UsageUrlError")
                self.assertIn("URL 无效", message)
                self.assertNotIn("secret", message)
                self.assertNotIn("example.test", message)

    def test_loopback_detection_covers_names_and_ips(self):
        with patch.dict(os.environ, {LOOPBACK_HTTP_ENV: "true"}):
            for base in ("http://127.0.0.1", "http://localhost", "http://[::1]"):
                with self.subTest(base=base):
                    validate_base_url(base, default_base=DEFAULT)


class TestUsageEndpoint(unittest.TestCase):
    def test_suffix_rules_do_not_duplicate_v1_usage(self):
        cases = (
            ("", f"{DEFAULT}/v1/usage"),
            ("https://example.test", "https://example.test/v1/usage"),
            ("https://example.test/", "https://example.test/v1/usage"),
            ("https://example.test/v1", "https://example.test/v1/usage"),
            ("https://example.test/v1/", "https://example.test/v1/usage"),
            ("https://example.test/v1/usage", "https://example.test/v1/usage"),
            ("https://example.test/v1/usage/", "https://example.test/v1/usage"),
            ("https://example.test/base", "https://example.test/base/v1/usage"),
        )
        for base, expected in cases:
            with self.subTest(base=base):
                self.assertEqual(
                    usage_endpoint(base, default_base=DEFAULT), expected
                )

    def test_invalid_base_never_builds_endpoint(self):
        with self.assertRaises(UsageUrlError):
            usage_endpoint(
                "https://example.test?token=secret", default_base=DEFAULT
            )

    def test_endpoint_host_is_lowercase_and_safe(self):
        self.assertEqual(endpoint_host("https://EXAMPLE.test:8443/v1"), "example.test")
        self.assertEqual(endpoint_host("https://"), "")


if __name__ == "__main__":
    unittest.main()
