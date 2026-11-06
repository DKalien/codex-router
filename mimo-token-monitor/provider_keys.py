"""WLB/SuperGPT 与 Codex Router 共用的密钥存储。"""

import json
import os
from pathlib import Path
import stat
import subprocess

from process_utils import hidden_subprocess_kwargs
from router_control import resolve_router_root


KEY_FIELDS = {"third_party_api_key": "wlb-relay", "supergpt_api_key": "supergpt-relay"}
_PROVIDERS = {"wlb-relay": ("WLB", "WLB_API_KEY", "wlb-api-key.secret"),
              "supergpt-relay": ("SuperGPT", "SUPERGPT_API_KEY", "supergpt-api-key.secret")}


class ProviderKeyError(RuntimeError):
    """可直接展示的脱敏密钥操作错误。"""


def provider_label(provider: str) -> str:
    """供应商展示名（WLB / SuperGPT），用于脱敏后的状态消息。"""
    return _PROVIDERS[provider][0]

def env_override(provider: str) -> str:
    """返回环境变量覆盖值（仅查询变量，不读取文件）。"""
    _, env_name, _ = _PROVIDERS[provider]
    return os.environ.get(env_name, "").strip()

def read_persistent_key(provider: str) -> str:
    """只读取 Router 持久存储中的密钥（跳过环境变量覆盖）。

    迁移旧配置时用于比对现状：值不同不得静默覆盖。
    """
    label, _, filename = _PROVIDERS[provider]
    codex_home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    state = Path(os.environ.get("MODEL_ROUTER_STATE_DIR")
                 or os.environ.get("CODEX_ROUTER_STATE_DIR")
                 or os.environ.get("KIMI_CODEX_STATE_DIR") or codex_home / "codex-router")
    for directory in (state, codex_home / "kimi-router", codex_home / "kimi-proxy"):
        secret = directory / filename
        try:
            if not stat.S_ISREG(secret.lstat().st_mode):
                raise OSError("密钥文件不是普通文件")
            value = secret.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            continue
        except (OSError, UnicodeError, RuntimeError):
            raise ProviderKeyError(f"无法读取 Router 的 {label} 密钥，请检查文件权限") from None
        if value:
            return value
    return ""


def read_key(provider: str) -> str:
    """读取当前生效密钥：环境变量覆盖优先，其次 Router 持久存储。"""
    return env_override(provider) or read_persistent_key(provider)


def _save_script(enable: bool) -> str:
    """生成写入脚本；enable=False 只写密钥，不改动供应商启用状态。"""
    enable_import = (
        "const { enableProvider } = await import(process.argv[2]);\n"
        if enable else ""
    )
    enable_call = "enableProvider(provider);\n    " if enable else ""
    return f"""
import {{ readFileSync }} from 'node:fs';
const {{ writeProviderCredential, resolveProviderCredential }} = await import(process.argv[1]);
{enable_import}const keys = JSON.parse(readFileSync(0, 'utf8'));
const saved = [];
for (const [provider, key] of Object.entries(keys)) {{
  try {{
    writeProviderCredential(provider, key);
    {enable_call}if (resolveProviderCredential(provider, {{persistent: true}})?.value !== key) throw new Error();
    saved.push(provider);
  }} catch {{
    process.stdout.write(JSON.stringify({{ok: false, saved, failed: provider}}));
    process.exit(1);
  }}
}}
process.stdout.write(JSON.stringify({{ok: true, saved}}));
"""


def save_keys(keys: dict[str, str], *, enable: bool = True) -> None:
    """通过 stdin 传递密钥，复用 Router 的原子写入与私有 ACL。

    ``enable=False`` 仅写入密钥、不改动供应商启用状态；旧配置迁移
    绝不隐式启用供应商，启用与否由用户单独决定。
    """
    if any(not isinstance(value, str) for value in keys.values()):
        raise ProviderKeyError("API Key 格式无效，请填写字符串密钥")
    keys = {provider: value.strip() for provider, value in keys.items() if value.strip()}
    if not keys:
        return
    for provider, key in keys.items():
        if provider not in _PROVIDERS:
            raise ProviderKeyError("不支持的密钥供应商")
        label, env_name, _ = _PROVIDERS[provider]
        if any(char.isspace() for char in key) or key.lower().startswith("bearer "):
            raise ProviderKeyError(f"{label} API Key 格式无效，请只粘贴完整密钥")
        if os.environ.get(env_name, "").strip() not in ("", key):
            raise ProviderKeyError(f"{label} 的 {env_name} 环境变量覆盖了共享密钥，请先移除该变量")
    try:
        root = resolve_router_root()
        result = subprocess.run(
            ["node", "--input-type=module", "-e", _save_script(enable),
             (root / "src" / "provider-credentials.mjs").as_uri(),
             (root / "src" / "provider-selection.mjs").as_uri()],
            input=json.dumps(keys), capture_output=True, text=True, encoding="utf-8",
            timeout=50, cwd=root, **hidden_subprocess_kwargs(),
        )
        status = json.loads(result.stdout)
        if not isinstance(status, dict):
            raise ValueError("无效的保存结果")
    except subprocess.TimeoutExpired:
        raise ProviderKeyError("共享密钥保存超时，部分密钥可能已更新；请检查 Router 后重试") from None
    except (OSError, RuntimeError, ValueError):
        raise ProviderKeyError("无法保存共享密钥，请检查 Router 安装位置、Node 和文件权限") from None
    if result.returncode != 0 or status.get("ok") is not True:
        saved = "、".join(_PROVIDERS[p][0] for p in status.get("saved", []) if p in _PROVIDERS)
        label = _PROVIDERS.get(status.get("failed"), ("共享",))[0]
        prefix = f"{saved} 已保存；" if saved else ""
        action = "保存或启用失败" if enable else "保存失败"
        raise ProviderKeyError(f"{prefix}{label} 密钥{action}，请检查 Router 文件权限后重试")
