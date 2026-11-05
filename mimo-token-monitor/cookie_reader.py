"""
Browser cookie reader for automatic cookie import.

Uses Chrome DevTools Protocol (CDP) to read cookies directly from a running
browser instance, bypassing cookie encryption entirely.
Falls back to reading from the local cookie database via browser_cookie3.
"""

import json
import urllib.request

TARGET_DOMAIN = "xiaomimimo.com"

# Common CDP debug ports
_CDP_PORTS = [9222, 9223]


# ── CDP approach ─────────────────────────────────────────────────


def _try_cdp(port: int) -> tuple[str | None, str | None]:
    """Try to read cookies via Chrome DevTools Protocol.

    Returns:
        (cookie_string, None) on success, (None, error_message) on failure.
    """
    try:
        url = f"http://localhost:{port}/json"
        req = urllib.request.Request(url, headers={"Host": "localhost"})
        with urllib.request.urlopen(req, timeout=3) as resp:
            pages = json.loads(resp.read())
    except Exception:
        return None, f"无法连接到浏览器调试端口 {port}"

    if not pages:
        return None, f"调试端口 {port} 已连接但无页面"

    # Use the first page's WebSocket to send CDP command
    ws_url = pages[0].get("webSocketDebuggerUrl", "")
    if not ws_url:
        return None, f"调试端口 {port} 无可用 WebSocket"

    # Fix WebSocket URL: Edge may omit port, e.g. ws://localhost/devtools/...
    # Ensure it uses the correct debug port
    if f":{port}" not in ws_url:
        ws_url = ws_url.replace("ws://localhost/", f"ws://localhost:{port}/")

    try:
        return _cdp_get_cookies_ws(ws_url)
    except ImportError:
        return None, "需要安装 websocket-client: pip install websocket-client"
    except Exception as e:
        return None, f"CDP 通信失败: {str(e)[:80]}"


def _cdp_get_cookies_ws(ws_url: str) -> tuple[str | None, str | None]:
    """Get cookies via CDP WebSocket connection."""
    import websocket  # type: ignore

    ws = websocket.create_connection(ws_url, timeout=5)
    try:
        ws.send(json.dumps({
            "id": 1,
            "method": "Network.getAllCookies",
        }))
        resp = json.loads(ws.recv())
        cookies = resp.get("result", {}).get("cookies", [])
    finally:
        ws.close()

    # Filter for target domain
    matched = [
        c for c in cookies
        if TARGET_DOMAIN in c.get("domain", "")
    ]

    if not matched:
        return None, f"浏览器中未找到 {TARGET_DOMAIN} 的 Cookie，请先登录"

    cookie_str = "; ".join(f"{c['name']}={c['value']}" for c in matched)
    return cookie_str, None


# ── browser_cookie3 fallback ─────────────────────────────────────


def _try_browser_db(browser_func, browser_key: str) -> tuple[str | None, str | None]:
    """Try to read cookies from a browser's local database.

    Returns:
        (cookie_string, None) on success, (None, error_message) on failure.
    """
    try:
        cj = browser_func(domain_name=TARGET_DOMAIN)
        cookie_str = "; ".join(f"{c.name}={c.value}" for c in cj)
        if cookie_str:
            return cookie_str, None
        return None, f"浏览器数据库中未找到 {TARGET_DOMAIN} 的 Cookie"
    except Exception as e:
        msg = str(e)
        if "locked" in msg.lower() or "database" in msg.lower():
            return None, "浏览器数据库被锁定，请关闭浏览器后重试"
        if "decrypt" in msg.lower() or "key" in msg.lower():
            return None, "Cookie 解密失败（浏览器可能使用了新版加密）"
        return None, f"读取失败: {msg[:80]}"


# ── Public API ───────────────────────────────────────────────────


def import_cookie_from_browser() -> tuple[str | None, str | None]:
    """Import cookie from running browser or local database.

    Strategy:
      1. Try CDP (Chrome DevTools Protocol) — reads from running browser,
         works regardless of cookie encryption.
      2. Fall back to browser_cookie3 — reads from local cookie database.

    Returns:
        (cookie_string, None) on success, (None, error_message) on failure.
    """
    # 1. Try CDP first (works with any encryption)
    cdp_errors = []
    for port in _CDP_PORTS:
        cookie_str, err = _try_cdp(port)
        if cookie_str:
            return cookie_str, None
        if err:
            cdp_errors.append(f"端口 {port}: {err}")

    # 2. Fall back to browser_cookie3
    try:
        import browser_cookie3  # type: ignore[import-untyped]
    except ImportError:
        return None, (
            "自动读取需要以下任一条件：\n\n"
            "方式一（推荐）：在 Edge 快捷方式目标末尾添加：\n"
            "  --remote-debugging-port=9222 --remote-allow-origins=*\n"
            "然后重启浏览器，再点击「从浏览器导入」\n"
            "（方式一还需要 websocket-client：pip install websocket-client）\n\n"
            "方式二：pip install browser_cookie3\n\n"
            "CDP 诊断信息：\n" + "\n".join(f"• {e}" for e in cdp_errors)
        )

    errors = []
    for func, key in [
        (browser_cookie3.edge, "edge"),
        (browser_cookie3.chrome, "chrome"),
        (browser_cookie3.firefox, "firefox"),
    ]:
        cookie_str, err = _try_browser_db(func, key)
        if cookie_str:
            return cookie_str, None
        if err:
            errors.append(err)

    # All methods failed — give actionable instructions
    all_errors = cdp_errors + errors
    return None, (
        "自动读取失败，请尝试以下方式：\n\n"
        "推荐：在 Edge/Chrome 快捷方式目标末尾添加：\n"
        "  --remote-debugging-port=9222 --remote-allow-origins=*\n"
        "然后重启浏览器，再点击「从浏览器导入」\n\n"
        "详细错误：\n" + "\n".join(f"• {e}" for e in all_errors)
    )
