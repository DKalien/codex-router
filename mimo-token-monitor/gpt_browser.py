"""使用隔离的 Chromium 获取 GPT 接口响应。"""

from urllib.parse import unquote, urlsplit

import requests

import playwright_session

_ALLOWED_URLS = {
    "https://chatgpt.com/backend-api/wham/usage",
    "https://chatgpt.com/api/auth/session",
}


def fetch_gpt_response(url, headers):
    """仅向指定接口发送一次请求，返回兼容 Requests 的响应。"""
    if url not in _ALLOWED_URLS:
        raise requests.exceptions.RequestException("不允许的 GPT 请求地址")

    playwright = browser = context = None
    captured = []
    sent = False
    try:
        proxy_url = requests.utils.select_proxy(url, requests.utils.get_environ_proxies(url))
        launch_options = {"headless": True, "timeout": 15000}
        if proxy_url:
            parsed = urlsplit(proxy_url if "://" in proxy_url else "http://" + proxy_url)
            if not parsed.hostname or parsed.scheme not in {"http", "https", "socks4", "socks5"}:
                raise ValueError("invalid proxy")
            host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
            server = f"{parsed.scheme}://{host}"
            if parsed.port is not None:
                server += f":{parsed.port}"
            proxy = {"server": server}
            if parsed.username is not None:
                proxy["username"] = unquote(parsed.username)
            if parsed.password is not None:
                proxy["password"] = unquote(parsed.password)
            launch_options["proxy"] = proxy
        else:
            launch_options["args"] = ["--no-proxy-server"]

        playwright = playwright_session._load_sync_playwright()().start()
        browser = playwright.chromium.launch(**launch_options)
        context = browser.new_context(java_script_enabled=False, service_workers="block")
        context.set_default_timeout(15000)

        page = context.new_page()
        cdp = context.new_cdp_session(page)

        def intercept_request(event):
            nonlocal sent
            request = event["request"]
            request_id = event["requestId"]
            if not sent and request["url"] == url and request["method"] == "GET":
                sent = True
                combined = {**request["headers"], **headers}
                cdp.send("Fetch.continueRequest", {
                    "requestId": request_id,
                    "headers": [{"name": name, "value": value} for name, value in combined.items()],
                })
            else:
                cdp.send("Fetch.failRequest", {"requestId": request_id, "errorReason": "Aborted"})

        cdp.on("Fetch.requestPaused", intercept_request)
        cdp.send("Fetch.enable", {"patterns": [{"urlPattern": "*", "requestStage": "Request"}]})
        page.on("response", lambda response: captured.append(response)
                if sent and not captured and response.url == url else None)
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=15000)
        except Exception:
            if not captured or captured[0].status < 300 or captured[0].status >= 400:
                raise
        if not captured:
            raise RuntimeError("missing response")
        result = requests.Response()
        result.status_code = captured[0].status
        result.url = url
        result._content = captured[0].body() if result.status_code == 200 else b""
        result._content_consumed = True
        return result
    except Exception as exc:
        raise requests.exceptions.RequestException(
            f"GPT 浏览器请求失败（{type(exc).__name__}）"
        ) from None
    finally:
        for resource, method in ((context, "close"), (browser, "close"), (playwright, "stop")):
            if resource is not None:
                try:
                    getattr(resource, method)()
                except Exception:
                    pass
