"""WLB / SuperGPT 用量请求共用的 Base URL 安全校验与规范化。

两家供应商必须共享同一套规则：默认仅 HTTPS、禁止 URL 内嵌登录凭据、
禁止查询参数与片段；测试用回环 HTTP 必须显式开启并限制到 loopback。
错误消息只描述原因，绝不回显输入原文（可能含密钥）。
"""

from __future__ import annotations

import ipaddress
import os
from urllib.parse import urlsplit, urlunsplit

LOOPBACK_HTTP_ENV = "MIMO_TOKEN_MONITOR_ALLOW_HTTP_LOOPBACK"

class UsageUrlError(ValueError):
    """Base URL 不符合安全规则；消息可直接展示，不包含输入原文。"""

def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}

def _is_loopback_host(host: str) -> bool:
    if host.casefold() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False

def _invalid(label: str, reason: str) -> UsageUrlError:
    return UsageUrlError(f"{label} Base URL 无效：{reason}")

def validate_base_url(base_url: str, *, default_base: str, label: str = "第三方") -> str:
    """校验并规范化 Base URL（不含 /v1/usage 后缀），失败抛 UsageUrlError。

    规则：默认仅 HTTPS；禁止空白/反斜杠、用户名密码、查询参数与片段；
    仅在显式开启 ``MIMO_TOKEN_MONITOR_ALLOW_HTTP_LOOPBACK`` 时允许
    loopback 主机使用 http://（本地测试服务器）。
    """
    raw = (base_url or "").strip() or default_base.strip()
    if not raw:
        raise _invalid(label, "缺少地址")
    if any(ch.isspace() for ch in raw) or "\\" in raw:
        raise _invalid(label, "不得包含空白或反斜杠字符")
    try:
        parsed = urlsplit(raw)
        host = parsed.hostname
        port = parsed.port  # 触发非法端口的 ValueError
    except ValueError:
        raise _invalid(label, "无法解析主机名或端口") from None
    if not host:
        raise _invalid(label, "缺少主机名")
    if parsed.username is not None or parsed.password is not None:
        raise _invalid(label, "不得包含用户名或密码")
    if parsed.query or parsed.fragment:
        raise _invalid(label, "不得包含查询参数或片段")
    scheme = parsed.scheme.casefold()
    if scheme == "http":
        if not (_env_flag(LOOPBACK_HTTP_ENV) and _is_loopback_host(host)):
            raise _invalid(label, "仅支持 HTTPS 地址")
    elif scheme != "https":
        raise _invalid(label, "仅支持 HTTPS 地址")
    # 用解析后的结构重建，丢弃大小写差异与空查询/片段。
    netloc = f"[{host}]" if ":" in host else host
    if port is not None:
        netloc = f"{netloc}:{port}"
    return urlunsplit((scheme, netloc, parsed.path.rstrip("/"), "", ""))

def usage_endpoint(base_url: str, *, default_base: str, label: str = "第三方") -> str:
    """返回规范化后的 /v1/usage 端点；合法 /v1、/v1/usage 后缀不重复拼接。"""
    base = validate_base_url(base_url, default_base=default_base, label=label)
    parsed = urlsplit(base)
    path = parsed.path
    lowered = path.casefold()
    if lowered.endswith("/v1/usage"):
        suffix = ""
    elif lowered.endswith("/v1"):
        suffix = "/usage"
    else:
        suffix = "/v1/usage"
    return urlunsplit((parsed.scheme, parsed.netloc, f"{path}{suffix}", "", ""))

def endpoint_host(url: str) -> str:
    """返回端点主机名（小写）；仅用于设置界面的默认域名提示。"""
    try:
        return (urlsplit(url).hostname or "").casefold()
    except ValueError:
        return ""
