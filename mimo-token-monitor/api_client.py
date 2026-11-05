import os
import atexit
import threading
import requests
import json as _json
import math as _math
import pathlib as _pl
from datetime import datetime as _datetime
import gpt_browser

# 确保打包后 requests 也能找到 CA 证书
if "SSL_CERT_FILE" not in os.environ:
    try:
        import certifi
        os.environ["SSL_CERT_FILE"] = certifi.where()
    except Exception:
        pass

BASE = "https://platform.xiaomimimo.com"
BALANCE_URL = f"{BASE}/api/v1/balance"

# Multiple candidate endpoints for usage/plan data
# Token Plan endpoints (from frontend JS: /tokenPlan/usage, /tokenPlan/subscription/status)
USAGE_URLS = [
    f"{BASE}/api/v1/tokenPlan/usage",
    f"{BASE}/api/v1/tokenPlan/subscription/status",
    f"{BASE}/api/v1/tokenPlan/subscription/order",
    f"{BASE}/api/v1/usage",
]


def _headers(cookie: str) -> dict:
    return {
        "Cookie": cookie,
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/131.0",
        "Referer": "https://platform.xiaomimimo.com/",
    }


def fetch_balance(cookie: str) -> dict:
    resp = None
    try:
        with requests.Session() as session:
            session.trust_env = False
            resp = session.get(BALANCE_URL, headers=_headers(cookie), timeout=10)
        if resp.status_code == 401:
            return {"ok": False, "balance": None, "error": "Cookie 已过期，请重新获取"}
        if resp.status_code >= 400:
            return {"ok": False, "balance": None, "error": f"HTTP {resp.status_code}"}
        data = resp.json()
        balance: int | float | str | None = None
        if isinstance(data, dict):
            d = data.get("data", data)
            if isinstance(d, (int, float)):
                balance = d
            elif isinstance(d, dict):
                balance = d.get("balance") or d.get("amount") or d.get("remain")
            elif isinstance(d, str):
                balance = d
            # Direct on root
            if balance is None:
                balance = data.get("balance")
        return {"ok": True, "balance": balance, "error": None}
    except requests.exceptions.ConnectionError:
        return {"ok": False, "balance": None, "error": "网络连接失败"}
    except Exception as e:
        return {"ok": False, "balance": None, "error": str(e)[:100]}
    finally:
        if resp is not None:
            resp.close()


def fetch_usage(cookie: str) -> dict:
    """Try multiple endpoints until one returns valid data."""
    headers = _headers(cookie)
    errors = []

    for url in USAGE_URLS:
        resp = None
        try:
            with requests.Session() as session:
                session.trust_env = False
                resp = session.get(url, headers=headers, timeout=10)
            if resp.status_code == 401:
                return {"ok": False, "data": None, "error": "Cookie 已过期，请重新获取"}
            if resp.status_code == 404:
                errors.append(f"{url.split('/')[-1]}: 404")
                continue
            if resp.status_code >= 400:
                errors.append(f"{url.split('/')[-1]}: {resp.status_code}")
                continue
            data = resp.json()
            # Accept any successful JSON response with actual content
            if isinstance(data, dict) and data.get("code") == 0:
                return {"ok": True, "data": data, "error": None, "url": url}
            # Some endpoints might not have code field
            if isinstance(data, dict) and "data" in data:
                return {"ok": True, "data": data, "error": None, "url": url}
            errors.append(f"{url.split('/')[-1]}: 格式不匹配")
        except Exception as e:
            errors.append(f"{url.split('/')[-1]}: {str(e)[:30]}")
        finally:
            if resp is not None:
                resp.close()

    return {"ok": False, "data": None, "error": f"所有端点均失败: {'; '.join(errors[:3])}"}

DEFAULT_THIRD_PARTY_BASE_URL = "https://codex.wlbclub.com"


def _third_party_usage_url(base_url: str) -> str:
    """Build the usage endpoint without duplicating a user-supplied /v1 path."""
    base = (base_url or DEFAULT_THIRD_PARTY_BASE_URL).strip().rstrip("/")
    if not base:
        base = DEFAULT_THIRD_PARTY_BASE_URL

    lower_base = base.casefold()
    if lower_base.endswith("/v1/usage"):
        return base
    if lower_base.endswith("/v1"):
        return f"{base}/usage"
    return f"{base}/v1/usage"


def parse_third_party_usage(data, window: str = "7d") -> dict:
    """Parse third-party usage API response."""
    empty = {
        "is_valid": False, "window": window, "used": 0, "limit": 0,
        "remaining": 0, "used_percent": 0, "remaining_percent": 0,
        "total_percent": 100, "unit": "%", "has_rate_limit": False,
        "reset_at": None,
    }
    if not isinstance(data, dict):
        return dict(empty)

    source = data
    rate_limits = source.get("rate_limits")
    if not isinstance(rate_limits, list) and isinstance(data.get("data"), dict):
        source = data["data"]
        rate_limits = source.get("rate_limits")
    if not isinstance(rate_limits, list):
        return dict(empty)

    entry = None
    for item in rate_limits:
        if isinstance(item, dict) and item.get("window") == window:
            entry = item
            break

    if entry is None:
        return dict(empty)

    try:
        limit_val = float(entry.get("limit", 0) or 0)
    except (ValueError, TypeError):
        limit_val = 0.0
    try:
        used_val = float(entry.get("used", 0) or 0)
    except (ValueError, TypeError):
        used_val = 0.0

    remaining_raw = entry.get("remaining")
    try:
        remaining_val = float(remaining_raw) if remaining_raw is not None else max(0.0, limit_val - used_val)
    except (ValueError, TypeError):
        remaining_val = max(0.0, limit_val - used_val)

    remaining_pct = (remaining_val / limit_val * 100) if limit_val > 0 else 0
    used_pct = (used_val / limit_val * 100) if limit_val > 0 else 0

    # The CC Switch extractor reads validity from the response root, not the
    # selected rate-limit entry. Keep the nullish fallback semantics of `??`.
    is_valid = data.get("isValid")
    if is_valid is None:
        is_valid = data.get("status") == "active"
        if "isValid" not in data and "status" not in data and source is not data:
            is_valid = source.get("status") == "active"

    reset_after = entry.get("reset_after_seconds")
    if reset_after is None:
        reset_after = entry.get("reset_after")

    parsed = {
        "is_valid": bool(is_valid),
        "window": window,
        "used": used_val,
        "limit": limit_val,
        "total": 100.0,
        "remaining": remaining_val,
        "used_percent": round(used_pct, 2),
        "remaining_percent": round(remaining_pct, 2),
        "total_percent": 100,
        "unit": "%",
        "has_rate_limit": True,
        "reset_at": entry.get("reset_at"),
        "reset_after_seconds": reset_after,
    }
    if window == "7d":
        # Keep the established weekly fields flat while exposing the daily
        # window from the same response.  A missing 1d entry is valid data.
        daily = parse_third_party_usage(data, "1d")
        if daily["has_rate_limit"] and limit_val > 0:
            effective_remaining = max(0.0, min(daily["remaining"], remaining_val))
            effective_limit = daily["used"] + effective_remaining
            if effective_limit > 0:
                daily["used_percent"] = round(daily["used"] / effective_limit * 100, 2)
                daily["remaining_percent"] = round(effective_remaining / effective_limit * 100, 2)
        parsed["daily"] = daily
    return parsed


def fetch_third_party_usage(base_url: str, api_key: str, window: str = "7d") -> dict:
    """Fetch third-party usage from external API.

    GET the normalized base_url/v1/usage endpoint with Authorization: Bearer apiKey.
    Returns: ok, data, error, url
    """
    url = _third_party_usage_url(base_url)

    if not str(api_key or "").strip():
        return {"ok": False, "data": None, "error": "API Key 不能为空", "url": url}

    resp = None
    try:
        with requests.Session() as session:
            session.trust_env = False
            resp = session.get(
                url,
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=15,
            )
        if resp.status_code == 401:
            return {"ok": False, "data": None, "error": "API Key 无效或已过期", "url": url}
        if resp.status_code >= 400:
            return {"ok": False, "data": None, "error": f"HTTP {resp.status_code}", "url": url}

        raw = resp.json()
        parsed = parse_third_party_usage(raw, window)
        if not parsed["has_rate_limit"]:
            return {
                "ok": False,
                "data": None,
                "error": f"响应中未找到 {window} 用量数据",
                "url": url,
            }
        return {"ok": True, "data": parsed, "error": None, "url": url}
    except requests.exceptions.ConnectionError:
        return {"ok": False, "data": None, "error": "网络连接失败", "url": url}
    except requests.exceptions.JSONDecodeError:
        return {"ok": False, "data": None, "error": "响应不是有效 JSON", "url": url}
    except Exception as e:
        return {"ok": False, "data": None, "error": str(e)[:100], "url": url}
    finally:
        if resp is not None:
            resp.close()


# -- GPT Weekly Usage -------------------------------------------------------

GPT_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
GPT_AUTH_SESSION_URL = "https://chatgpt.com/api/auth/session"
GPT_PRIMARY_WINDOW_SECONDS = 5 * 60 * 60
GPT_WEEKLY_WINDOW_SECONDS = 7 * 24 * 60 * 60
_GPT_SESSION = requests.Session()
_GPT_SESSION_LOCK = threading.Lock()
atexit.register(_GPT_SESSION.close)


class GPTQueryError(RuntimeError):
    """仅携带脱敏后的 GPT 查询失败原因。"""


def _gpt_get(url: str, headers: dict):
    """重试瞬时失败，网络仍失败时改用隔离 Chromium 查询。"""
    for attempt in range(2):
        try:
            with _GPT_SESSION_LOCK:
                _GPT_SESSION.cookies.clear()
                try:
                    response = _GPT_SESSION.get(url, headers=headers, timeout=15, allow_redirects=False)
                finally:
                    _GPT_SESSION.cookies.clear()
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
            if not attempt:
                continue
            try:
                return gpt_browser.fetch_gpt_response(url, headers)
            except requests.exceptions.RequestException:
                reason = {
                    "ProxyError": "代理连接失败",
                    "SSLError": "TLS 连接失败",
                    "ConnectTimeout": "连接超时",
                    "ReadTimeout": "读取响应超时",
                    "Timeout": "请求超时",
                }.get(type(exc).__name__, "网络连接失败")
                raise GPTQueryError(f"{reason}；Chromium 兜底查询也未成功") from None
        if attempt == 0 and (response.status_code == 429 or response.status_code >= 500):
            response.close()
            continue
        return response


def _gpt_failure(errors: list | None, source: str, reason: str):
    if errors is not None:
        errors.append(f"{source}: {reason}")
    return None


def _parse_gpt_window(candidate):
    """Normalize one GPT rate-limit window."""
    if not isinstance(candidate, dict):
        return None
    raw_pct = candidate.get("used_percent")
    if raw_pct is None:
        raw_pct = candidate.get("usedPercent")
    try:
        used_pct = float(raw_pct)
    except (ValueError, TypeError):
        return None
    if not _math.isfinite(used_pct):
        return None
    used_pct = max(0.0, min(100.0, used_pct))
    return {
        "used_percent": round(used_pct, 2),
        "remaining_percent": round(100.0 - used_pct, 2),
        "reset_at": candidate.get("resets_at") or candidate.get("reset_at"),
        "reset_after_seconds": candidate.get("reset_after_seconds") or candidate.get("reset_after"),
    }


def _parse_gpt_windows(data):
    """Extract weekly/secondary window fields from a ChatGPT usage response.

    Supports multiple response shapes:
      New object:  rate_limits is a dict with a .secondary sub-dict
      New list:    rate_limits is a list; find entry with window=weekly/7d/secondary
      Old:         rate_limit.secondary_window is a dict
    Nested data wrappers are unwrapped automatically.
    Returns ``{"primary": ..., "secondary": ...}``; missing windows are None."""
    if not isinstance(data, dict):
        return None

    source = data.get("data") if isinstance(data.get("data"), dict) else data

    primary = secondary = None

    rate_limits = source.get("rate_limits")
    if isinstance(rate_limits, dict):
        primary = rate_limits.get("primary") or rate_limits.get("primary_window")
        secondary = rate_limits.get("secondary") or rate_limits.get("secondary_window")
    elif isinstance(rate_limits, list):
        for entry in rate_limits:
            if not isinstance(entry, dict):
                continue
            window = str(entry.get("window") or entry.get("type") or "").lower()
            try:
                seconds = int(entry.get("limit_window_seconds", 0) or 0)
            except (TypeError, ValueError):
                seconds = 0
            if window in {"weekly", "7d", "secondary"} or seconds == GPT_WEEKLY_WINDOW_SECONDS:
                secondary = secondary or entry
            elif window in {"primary", "5h", "5_hour", "5-hour"} or seconds == GPT_PRIMARY_WINDOW_SECONDS:
                primary = primary or entry

    # Old format: rate_limit.secondary_window is normally the weekly window.
    rl = source.get("rate_limit") if isinstance(source.get("rate_limit"), dict) else None
    if rl:
        primary = primary or rl.get("primary_window")
        secondary = secondary or rl.get("secondary_window")

    # 只有一个窗口时，按时长识别放在 primary 中的周窗口。
    if secondary is None and isinstance(primary, dict):
        try:
            seconds = int(primary.get("limit_window_seconds") or
                          int(primary.get("window_minutes", 0) or 0) * 60)
        except (TypeError, ValueError, OverflowError):
            seconds = 0
        if seconds == GPT_WEEKLY_WINDOW_SECONDS:
            secondary, primary = primary, None

    return {"primary": _parse_gpt_window(primary), "secondary": _parse_gpt_window(secondary)}


def _parse_gpt_secondary_window(data):
    """Backward-compatible weekly-window parser."""
    windows = _parse_gpt_windows(data)
    return windows.get("secondary") if windows else None


def _gpt_result_with_windows(data, source):
    windows = _parse_gpt_windows(data)
    secondary = windows.get("secondary") if windows else None
    if secondary is None:
        return None
    result = dict(secondary)
    result["primary"] = windows.get("primary")
    result["secondary"] = dict(secondary)
    result["source"] = source
    return result


def fetch_gpt_weekly_usage(session_cookie=""):
    """Fetch GPT weekly (secondary window) usage.

    Sources tried in order:
      1. Local Codex auth.json (offline, no user config needed)
      2. Configured ChatGPT session cookie (two-step auth exchange)
      3. Local JSONL session files (offline fallback)

    Returns: {ok, data: {used_percent, remaining_percent, reset_at,
             reset_after_seconds, source}, error}
    """
    errors = []
    sources = [
        ("本机 Codex 登录", _gpt_try_local_auth, {"errors": errors}),
        ("ChatGPT Cookie", _gpt_try_session_cookie, {"session_cookie": session_cookie, "errors": errors}),
        ("本地会话", _gpt_try_jsonl, {"errors": errors}),
    ]
    for source, method, kwargs in sources:
        try:
            result = method(**kwargs)
            if result is not None:
                if result.get("source") == "local_jsonl":
                    result["source_errors"] = list(errors)
                return {"ok": True, "data": result, "error": None}
        except GPTQueryError as exc:
            errors.append(f"{source}: {exc}")
        except requests.exceptions.Timeout:
            errors.append(f"{source}: 请求超时")
        except requests.exceptions.ConnectionError:
            errors.append(f"{source}: 网络连接失败")
        except requests.exceptions.JSONDecodeError:
            errors.append(f"{source}: 响应不是有效 JSON")
        except requests.exceptions.RequestException:
            errors.append(f"{source}: 网络请求失败")
        except Exception as exc:
            errors.append(f"{source}: {type(exc).__name__}")

    detail = "；".join(errors) if errors else "未找到可用数据源"
    return {"ok": False, "data": None, "error": f"GPT 周限额刷新失败：{detail}"}

def _gpt_try_local_auth(errors=None):
    """Try reading access_token from local Codex auth.json and fetching usage."""
    home = _pl.Path.home()
    candidates = [
        home / ".codex" / "auth.json",
        home / ".chatgpt" / "auth.json",
    ]
    access_token = None
    account_id = None
    for path in candidates:
        if not path.exists():
            continue
        try:
            auth_data = _json.loads(path.read_text(encoding="utf-8"))
            tokens = auth_data.get("tokens") if isinstance(auth_data, dict) else None
            if isinstance(tokens, dict):
                access_token = tokens.get("access_token")
                # Priority: tokens.account_id > root.account_id > root.account.id
                account_id = tokens.get("account_id")
                if not account_id:
                    account_id = auth_data.get("account_id")
                if not account_id:
                    acct = auth_data.get("account")
                    if isinstance(acct, dict):
                        account_id = acct.get("id")
        except Exception:
            continue
        if access_token:
            break

    if not access_token:
        return _gpt_failure(errors, "本机 Codex 登录", "未找到登录令牌")

    hdrs = {
        "Authorization": f"Bearer {access_token}",
        "User-Agent": "Mozilla/5.0 Codex-Usage/1.0",
        "Originator": "codex-cli",
    }
    if account_id:
        hdrs["ChatGPT-Account-ID"] = account_id

    resp = _gpt_get(GPT_USAGE_URL, hdrs)
    try:
        if resp.status_code != 200:
            reason = "登录已失效" if resp.status_code in {401, 403} else "用量接口请求失败"
            return _gpt_failure(errors, "本机 Codex 登录", f"{reason}（HTTP {resp.status_code}）")
        data = resp.json()
    finally:
        resp.close()
    parsed = _gpt_result_with_windows(data, "local_codex_auth")
    if parsed is None:
        return _gpt_failure(errors, "本机 Codex 登录", "响应中没有 7 天额度窗口")

    return parsed

def _gpt_try_session_cookie(session_cookie="", errors=None):
    """Two-step: exchange session cookie for access_token via GET, then fetch usage."""
    if not session_cookie or not session_cookie.strip():
        return _gpt_failure(errors, "ChatGPT Cookie", "未配置")

    resp = _gpt_get(
        GPT_AUTH_SESSION_URL,
        {
            "Cookie": session_cookie,
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/131.0",
        },
    )
    try:
        if resp.status_code != 200:
            reason = "已失效" if resp.status_code in {401, 403} else "登录接口请求失败"
            return _gpt_failure(errors, "ChatGPT Cookie", f"{reason}（HTTP {resp.status_code}）")
        auth_data = resp.json()
    finally:
        resp.close()
    if not isinstance(auth_data, dict):
        return None

    access_token = auth_data.get("accessToken")
    if not access_token:
        return _gpt_failure(errors, "ChatGPT Cookie", "登录响应缺少访问令牌")

    account_id = None
    account = auth_data.get("account")
    if isinstance(account, dict):
        account_id = account.get("id")

    hdrs = {
        "Authorization": f"Bearer {access_token}",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/131.0",
        "Originator": "codex-cli",
    }
    if account_id:
        hdrs["ChatGPT-Account-ID"] = account_id

    resp2 = _gpt_get(GPT_USAGE_URL, hdrs)
    try:
        if resp2.status_code != 200:
            reason = "登录已失效" if resp2.status_code in {401, 403} else "用量接口请求失败"
            return _gpt_failure(errors, "ChatGPT Cookie", f"{reason}（HTTP {resp2.status_code}）")
        data = resp2.json()
    finally:
        resp2.close()
    parsed = _gpt_result_with_windows(data, "session_cookie")
    if parsed is None:
        return _gpt_failure(errors, "ChatGPT Cookie", "响应中没有 7 天额度窗口")

    return parsed


def _gpt_try_jsonl(errors=None):
    """从最近会话中按额度事件时间选择最新完整快照。"""
    home = _pl.Path.home()
    sessions_dir = home / ".codex" / "sessions"
    if not sessions_dir.is_dir():
        return _gpt_failure(errors, "本地会话", "会话目录不存在")

    jsonl_files = sorted(
        sessions_dir.rglob("rollout-*.jsonl"),
        key=lambda f: f.stat().st_mtime,
        reverse=True,
    )[:10]

    newest = None
    newest_time = None
    for jsonl_path in jsonl_files:
        try:
            text = jsonl_path.read_text(encoding="utf-8", errors="replace")
            for line in reversed(text.splitlines()):
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = _json.loads(line)
                except _json.JSONDecodeError:
                    continue
                payload = entry.get("payload") if isinstance(entry, dict) else None
                if not isinstance(payload, dict):
                    continue
                timestamp = entry.get("timestamp")
                try:
                    observed = _datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
                except (AttributeError, TypeError, ValueError):
                    continue
                if observed.tzinfo is None:
                    continue
                parsed = _gpt_result_with_windows(payload, "local_jsonl")
                if parsed is not None and (newest_time is None or observed > newest_time):
                    parsed["observed_at"] = timestamp
                    newest, newest_time = parsed, observed
        except Exception:
            continue

    if newest is not None:
        return newest
    return _gpt_failure(errors, "本地会话", "最近 10 个会话没有带有效时间的周限额记录")
