import json
import os
import sqlite3
from copy import deepcopy
import tempfile

from provider_keys import (
    KEY_FIELDS,
    ProviderKeyError,
    env_override,
    provider_label,
    read_persistent_key,
    save_keys,
)

DEFAULT_CONFIG = {
    "cookie": "",
    "refresh_interval": 300,
    "opacity": 0.85,
    "position": [100, 100],
    "snapshot_path": "",  # Path for claude-hud snapshot (empty = disabled)
    "expiry_date": "",  # User-entered plan expiry date
    "expiry_alert_enabled": True,  # Highlight near-expiry dates in red
    "daily_baseline_date": "",  # Date of daily baseline (YYYY-MM-DD format)
    "daily_baseline_usage": 0,  # Month-used value at start of day
    "always_on_top": True,
    "third_party_base_url": "https://codex.wlbclub.com",
    "supergpt_base_url": "https://api.supergpt.dev",
    "gpt_session_cookie": "",
    "display_mode": "mimo",
    "playwright_auto_refresh": True,
    "playwright_refresh_interval": 21600,
}

# Default external project data directory (Windows path by design).
_DEFAULT_PROJECT_DATA_DIR = os.path.join("D:\\python\\data", "mimo-token-monitor")

_DB_NAME = "settings.db"
_SETTINGS_TABLE = "settings"
_PRAGMAS = [
    "PRAGMA journal_mode=DELETE;",
    "PRAGMA busy_timeout=5000;",
]


def _legacy_config_dir() -> str:
    """Return legacy config dir; overridable for tests."""
    override = os.environ.get("MIMO_LEGACY_CONFIG_DIR")
    return override or os.path.join(os.path.expanduser("~"), ".mimo-widget")


def _legacy_config_path() -> str:
    return os.path.join(_legacy_config_dir(), "config.json")


def _project_data_dir() -> str:
    """Return the external project data directory (may be overridden by env).

    This function only returns a path; it never creates directories here.
    """
    override = os.environ.get("MIMO_TOKEN_MONITOR_DATA_DIR")
    return override or _DEFAULT_PROJECT_DATA_DIR


def _db_path() -> str:
    return os.path.join(_project_data_dir(), _DB_NAME)


def playwright_user_data_dir() -> str:
    """Return the isolated browser profile used for Playwright renewal."""
    return os.path.join(_project_data_dir(), "playwright-profile")


def claude_profiles_path() -> str:
    """Return the local Claude profile template file path."""
    override = os.environ.get("MIMO_CLAUDE_PROFILES_PATH")
    if override:
        return override
    return os.path.join(os.path.expanduser("~"), ".claude", "claude_profiles.json")


def _ensure_db(conn: sqlite3.Connection) -> None:
    """Ensure the settings schema exists on an open connection."""
    with conn:
        for pragma in _PRAGMAS:
            conn.execute(pragma)
        conn.execute(
            f"CREATE TABLE IF NOT EXISTS {_SETTINGS_TABLE} ("
            "key TEXT PRIMARY KEY,"
            "value_json TEXT NOT NULL"
            ");"
        )


def _open_db(path: str) -> sqlite3.Connection | None:
    """Try to open and initialize a SQLite database; return None on failure."""
    conn = None
    try:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        conn = sqlite3.connect(path)
        _ensure_db(conn)
        return conn
    except Exception:
        if conn is not None:
            conn.close()
        return None


def _read_db_config(conn: sqlite3.Connection) -> dict | None:
    """Read a complete config from SQLite.

    Returns None when the table is empty or a row cannot be parsed.
    """
    try:
        with conn:
            rows = conn.execute(
                f"SELECT key, value_json FROM {_SETTINGS_TABLE}"
            ).fetchall()
    except Exception:
        return None

    if not rows:
        return None

    cfg: dict = {}
    for key, value_json in rows:
        try:
            value = json.loads(value_json)
        except Exception:
            return None
        cfg[key] = value

    merged = deepcopy(DEFAULT_CONFIG)
    merged.update(cfg)
    return merged


def _write_db_config(conn: sqlite3.Connection, cfg: dict) -> bool:
    """Replace settings table contents with cfg atomically.

    Returns True on success, False on failure.
    """
    merged = deepcopy(DEFAULT_CONFIG)
    merged.update(cfg)
    try:
        items = [(k, json.dumps(v, ensure_ascii=False)) for k, v in merged.items()]
        with conn:
            conn.execute(f"DELETE FROM {_SETTINGS_TABLE}")
            conn.executemany(
                f"INSERT INTO {_SETTINGS_TABLE}(key, value_json) VALUES (?, ?)",
                items,
            )
        return True
    except Exception:
        return False


def _read_legacy_json() -> dict | None:
    """Read legacy ~/.mimo-widget/config.json if available."""
    path = _legacy_config_path()
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        if not isinstance(cfg, dict):
            return None
        merged = deepcopy(DEFAULT_CONFIG)
        merged.update(cfg)
        return merged
    except Exception:
        return None


def _atomic_save_json(path: str, cfg: dict) -> bool:
    """Save JSON config atomically using tmp + os.replace."""
    tmp_path = None
    try:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(
            prefix=".mimo-config-", suffix=".tmp", dir=parent or "."
        )
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
        os.replace(tmp_path, path)
        tmp_path = None
        return True
    except Exception:
        return False
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


def load_config() -> dict:
    """Load config from external SQLite, fallback to legacy JSON / defaults.

    - External DB empty or unavailable -> migrate from legacy JSON when present.
    - Returns DEFAULT_CONFIG-merged dict; callers do not need to merge again.
    """
    path = _db_path()
    conn = _open_db(path)
    if conn is not None:
        try:
            cfg = _read_db_config(conn)
            if cfg is not None:
                return cfg

            legacy = _read_legacy_json()
            if legacy is not None:
                if _write_db_config(conn, legacy):
                    cfg_after = _read_db_config(conn)
                    if cfg_after is not None:
                        return cfg_after
                return legacy

            return deepcopy(DEFAULT_CONFIG)
        finally:
            conn.close()

    legacy = _read_legacy_json()
    if legacy is not None:
        return legacy

    return deepcopy(DEFAULT_CONFIG)


def save_config(cfg: dict) -> bool:
    """Save config to external SQLite, fallback to legacy JSON atomically."""
    cfg = {key: value for key, value in cfg.items() if key not in KEY_FIELDS}
    path = _db_path()
    conn = _open_db(path)
    if conn is not None:
        try:
            ok = _write_db_config(conn, cfg)
            if ok:
                return True
        finally:
            conn.close()

    merged = deepcopy(DEFAULT_CONFIG)
    merged.update(cfg)
    return _atomic_save_json(_legacy_config_path(), merged)


def _cleanup_key_fields(cfg: dict, fields: set[str]) -> None:
    """确认目标状态后清理旧密钥字段；清理失败保留旧数据供重试。"""
    legacy = _read_legacy_json()
    if legacy is not None and any(legacy.get(field) for field in fields):
        legacy = {key: value for key, value in legacy.items() if key not in fields}
        if not _atomic_save_json(_legacy_config_path(), legacy):
            raise ProviderKeyError("密钥已保存到 Router，但旧 JSON 清理失败；请检查设置目录权限后重试")
    cleaned = {key: value for key, value in cfg.items() if key not in fields}
    if not save_config(cleaned):
        raise ProviderKeyError("密钥已保存到 Router，但旧设置清理失败；请检查设置目录权限后重试")
    cfg.clear()
    cfg.update(cleaned)


def migrate_provider_keys(cfg: dict) -> list[dict]:
    """把旧悬浮窗密钥迁移到 Router 共享存储，返回需要用户决定的冲突列表。

    - 默认保留 Router 现有生效值：仅当没有生效密钥时才写入旧值；
      旧值与现值不同时不写入，返回冲突项由用户显式选择是否覆盖。
    - 迁移只写密钥（enable=False），绝不隐式启用供应商。
    - 逐项独立处理：成功项立即清理旧字段，失败项保留供重试；重试时
      按现状重新比对，旧值不会二次覆盖已变化的 Router 密钥（幂等）。
    - 错误与返回值都不携带密钥原文。
    """
    conflicts: list[dict] = []
    migrated: list[str] = []
    errors: list[str] = []
    for field, provider in KEY_FIELDS.items():
        raw = cfg.get(field)
        if not raw:
            continue
        label = provider_label(provider)
        if not isinstance(raw, str) or not raw.strip():
            errors.append(f"{label} 旧密钥格式无效，已保留旧设置")
            continue
        old = raw.strip()
        try:
            override = env_override(provider)
            current = override or read_persistent_key(provider)
        except ProviderKeyError as exc:
            errors.append(f"{label}：{exc}")
            continue
        if not current:
            try:
                save_keys({provider: old}, enable=False)
            except ProviderKeyError as exc:
                errors.append(f"{label}：{exc}")
                continue
            migrated.append(field)
            continue
        if current == old:
            migrated.append(field)
            continue
        conflicts.append({
            "field": field,
            "provider": provider,
            "label": label,
            "reason": "env" if override else "different",
        })
    if migrated:
        _cleanup_key_fields(cfg, set(migrated))
    if errors:
        done = "、".join(provider_label(KEY_FIELDS[f]) for f in migrated)
        prefix = f"{done} 已完成；" if done else ""
        raise ProviderKeyError(
            f"{prefix}{'；'.join(errors)}（失败项旧值已保留，重启后自动重试）"
        )
    return conflicts


def resolve_key_migration(cfg: dict, field: str, *, overwrite: bool) -> None:
    """执行用户对密钥冲突的显式选择；覆盖只写密钥，不启用供应商。"""
    provider = KEY_FIELDS[field]
    raw = cfg.get(field)
    if not raw:
        return
    if overwrite:
        if not isinstance(raw, str) or not raw.strip():
            raise ProviderKeyError(
                f"{provider_label(provider)} 旧密钥格式无效，无法覆盖 Router 现有值"
            )
        save_keys({provider: raw.strip()}, enable=False)
    _cleanup_key_fields(cfg, {field})
