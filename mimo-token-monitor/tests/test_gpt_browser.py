import unittest
from unittest.mock import Mock, patch

import requests

import gpt_browser

URL = "https://chatgpt.com/backend-api/wham/usage"


class BrowserTests(unittest.TestCase):
    def setUp(self):
        self.playwright = Mock()
        self.browser = self.playwright.chromium.launch.return_value
        self.context = self.browser.new_context.return_value
        self.page = self.context.new_page.return_value
        self.cdp = self.context.new_cdp_session.return_value
        self.manager = Mock()
        self.manager.start.return_value = self.playwright
        self.loader = patch.object(gpt_browser.playwright_session, "_load_sync_playwright", return_value=lambda: self.manager)
        self.loader.start()
        self.addCleanup(self.loader.stop)

    def test_rejects_unknown_url_before_start(self):
        with self.assertRaises(requests.exceptions.RequestException):
            gpt_browser.fetch_gpt_response(URL + "?x=1", {})
        self.manager.start.assert_not_called()

    def test_response_proxy_and_redirect_are_isolated(self):
        response = Mock(url=URL, status=302)
        def event(request_id, url):
            return {"requestId": request_id, "request": {"url": url, "method": "GET", "headers": {}}}

        def navigate(*args, **kwargs):
            intercept = self.cdp.on.call_args.args[1]
            intercept(event("first", URL))
            self.page.on.call_args.args[1](response)
            intercept(event("redirect", "https://evil.example/"))
            intercept(event("repeated", URL))
            intercept(event("same", "https://chatgpt.com/other"))
            raise RuntimeError("redirect aborted")

        self.page.goto.side_effect = navigate
        with patch.object(requests.utils, "get_environ_proxies", return_value={"https": "http://user:secret@localhost:8888"}):
            headers = {"Authorization": "Bearer secret", "Cookie": "session=secret",
                       "ChatGPT-Account-ID": "secret-account"}
            result = gpt_browser.fetch_gpt_response(URL, headers)
        self.assertEqual(result.status_code, 302)
        result.close()
        self.assertEqual(self.playwright.chromium.launch.call_args.kwargs["proxy"],
                         {"server": "http://localhost:8888", "username": "user", "password": "secret"})
        calls = self.cdp.send.call_args_list
        self.assertEqual(calls[0].args, ("Fetch.enable", {"patterns": [{"urlPattern": "*", "requestStage": "Request"}]}))
        self.assertEqual(calls[1].args, ("Fetch.continueRequest", {
            "requestId": "first", "headers": [{"name": k, "value": v} for k, v in headers.items()]}))
        for call, request_id in zip(calls[2:], ("redirect", "repeated", "same")):
            self.assertEqual(call.args, ("Fetch.failRequest", {"requestId": request_id, "errorReason": "Aborted"}))
        self.assertEqual(len(calls), 5)
        self.context.close.assert_called_once()
        self.browser.close.assert_called_once()
        self.playwright.stop.assert_called_once()

    def test_no_proxy_json_and_cleanup(self):
        response = Mock(url=URL, status=200)
        response.body.return_value = b'{"ok": true}'
        def navigate(*args, **kwargs):
            self.cdp.on.call_args.args[1]({"requestId": "first", "request": {"url": URL, "method": "GET", "headers": {}}})
            self.page.on.call_args.args[1](response)
        self.page.goto.side_effect = navigate
        with patch.object(requests.utils, "get_environ_proxies", return_value={}):
            result = gpt_browser.fetch_gpt_response(URL, {})
        self.assertEqual(result.json(), {"ok": True})
        result.close()
        self.assertEqual(self.playwright.chromium.launch.call_args.kwargs["args"], ["--no-proxy-server"])
        self.assertEqual(self.browser.new_context.call_args.kwargs,
                         {"java_script_enabled": False, "service_workers": "block"})
        self.context.set_default_timeout.assert_called_once_with(15000)
        self.page.goto.assert_called_once_with(URL, wait_until="domcontentloaded", timeout=15000)
        self.context.close.assert_called_once()
        self.browser.close.assert_called_once()
        self.playwright.stop.assert_called_once()

    def test_launch_failure_stops_playwright_and_sanitizes_error(self):
        self.playwright.chromium.launch.side_effect = RuntimeError("secret")
        with patch.object(requests.utils, "get_environ_proxies", return_value={}):
            with self.assertRaises(requests.exceptions.RequestException) as error:
                gpt_browser.fetch_gpt_response(URL, {})
        self.assertNotIn("secret", str(error.exception))
        self.playwright.stop.assert_called_once()
        self.browser.close.assert_not_called()
        self.context.close.assert_not_called()

    def test_timeout_is_sanitized_and_closes_resources(self):
        self.page.goto.side_effect = TimeoutError("secret proxy password")
        with patch.object(requests.utils, "get_environ_proxies", return_value={}):
            with self.assertRaises(requests.exceptions.RequestException) as error:
                gpt_browser.fetch_gpt_response(URL, {"Cookie": "secret"})
        self.assertNotIn("secret", str(error.exception))
        self.context.close.assert_called_once()
        self.browser.close.assert_called_once()
        self.playwright.stop.assert_called_once()


if __name__ == "__main__":
    unittest.main()
