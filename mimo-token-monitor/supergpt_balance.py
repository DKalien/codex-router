"""查询 SuperGPT 账户余额，支持本地设置并兼容 Router 密钥。"""

import math
from urllib.parse import urlsplit

import requests

from api_client import _third_party_usage_url
from provider_keys import read_key

DEFAULT_BASE_URL = "https://api.supergpt.dev"


def _api_key() -> str:
    return read_key("supergpt-relay")


def _failure(message: str) -> dict:
    return {"ok": False, "balance": None, "currency": None, "error": message}


def fetch_balance(base_url: str = "", api_key: str = "") -> dict:
    """使用设置地址查询余额，默认从 Router 共享存储读取密钥。"""
    base_url = base_url.strip() or DEFAULT_BASE_URL
    try:
        parsed = urlsplit(base_url)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or parsed.query or parsed.fragment):
            raise ValueError("Invalid base URL")
        parsed.port
    except ValueError:
        return _failure("SuperGPT Base URL 无效，请填写不含认证信息、查询参数的 HTTP(S) 地址")
    url = _third_party_usage_url(base_url)
    try:
        key = api_key.strip() or _api_key()
    except (OSError, UnicodeError, RuntimeError):
        return _failure("无法读取 Router 的 SuperGPT 密钥")
    if not key:
        return _failure("请在设置中填写 SuperGPT API Key，或通过 provider-key supergpt-relay set 配置密钥")
    try:
        with requests.Session() as session:
            session.trust_env = False
            response = session.get(
                url,
                headers={"Authorization": f"Bearer {key}"},
                timeout=(3, 10),
                allow_redirects=False,
            )
    except requests.RequestException:
        return _failure("SuperGPT 余额查询网络连接失败")
    try:
        if response.status_code in (401, 403):
            return _failure("SuperGPT API Key 无效或已过期")
        if response.status_code != 200:
            return _failure(f"SuperGPT 余额查询 HTTP {response.status_code}")
        try:
            data = response.json()
        except ValueError:
            return _failure("SuperGPT 余额响应不是有效 JSON")
        if not isinstance(data, dict):
            return _failure("SuperGPT 余额响应格式无效")
        if data.get("isValid") is False:
            return _failure("SuperGPT API Key 当前不可用")
        raw_balance = data.get("balance")
        try:
            if isinstance(raw_balance, bool):
                raise ValueError("Not a monetary value")
            balance = float(raw_balance)
        except (TypeError, ValueError, OverflowError):
            return _failure("SuperGPT 未返回有效的账户余额")
        if not math.isfinite(balance):
            return _failure("SuperGPT 未返回有效的账户余额")
        currency = str(data.get("unit", "")).upper()
        if currency not in ("USD", "CNY"):
            return _failure("SuperGPT 返回了未知的余额币种")
        return {"ok": True, "balance": balance, "currency": currency, "error": None}
    finally:
        response.close()
