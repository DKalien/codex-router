"""Claude Code settings profiles.

Profiles are kept in a small local JSON file under the user's ``.claude``
directory.  The file is deliberately separate from Claude Code's
``settings.json``: switching a profile only updates the selected environment
variables in the latter and leaves hooks, permissions, plugins, and all other
settings untouched.

Profile shape (add another object to ``claude_profiles.json`` when needed)::

    {
      "name": "API-GPT",
      "env": {
        "ANTHROPIC_AUTH_TOKEN": "...",
        "ANTHROPIC_BASE_URL": "...",
        "ANTHROPIC_DEFAULT_FABLE_MODEL": "...",
        "ANTHROPIC_DEFAULT_OPUS_MODEL": "...",
        "ANTHROPIC_DEFAULT_SONNET_MODEL": "...",
        "ANTHROPIC_DEFAULT_HAIKU_MODEL": "...",
        "CLAUDE_CODE_SUBAGENT_MODEL": "..."
      }
    }

The first profile is bootstrapped from the current Claude settings on first
use, so credentials are never checked into the project source.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from config import claude_profiles_path


# These are the only settings that profile switching is allowed to change.
PROFILE_ENV_KEYS = (
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_DEFAULT_FABLE_MODEL",
    "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    "CLAUDE_CODE_SUBAGENT_MODEL",
)
DEFAULT_PROFILE_NAME = "API-Claude"


class ClaudeProfileError(RuntimeError):
    """Raised when Claude profiles or settings cannot be read or written."""


def settings_path(path: str | os.PathLike[str] | None = None) -> Path:
    """Return the Claude settings path, with a test/user override available."""
    raw_path = path or os.environ.get("CLAUDE_SETTINGS_PATH") or os.path.expanduser(
        "~/.claude/settings.json"
    )
    return Path(raw_path).expanduser()


def _profiles_path(path: str | os.PathLike[str] | None = None) -> Path:
    raw_path = path or claude_profiles_path()
    return Path(raw_path).expanduser()


def _read_json(path: Path, description: str) -> Any:
    try:
        with path.open("r", encoding="utf-8") as file:
            return json.load(file)
    except FileNotFoundError as exc:
        raise ClaudeProfileError(f"找不到{description}: {path}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise ClaudeProfileError(f"无法读取{description}: {path}") from exc


def load_settings(path: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    """Read Claude settings and ensure the JSON root is an object."""
    settings_file = settings_path(path)
    settings = _read_json(settings_file, "Claude settings.json")
    if not isinstance(settings, dict):
        raise ClaudeProfileError("Claude settings.json 的根节点必须是 JSON 对象")
    return settings


def _profile_env_from_settings(settings: dict[str, Any]) -> dict[str, str]:
    env = settings.get("env")
    if not isinstance(env, dict):
        raise ClaudeProfileError("Claude settings.json 中缺少有效的 env 对象")
    return {key: str(env.get(key, "")) for key in PROFILE_ENV_KEYS}


def _normalize_profile(raw_profile: Any, index: int) -> dict[str, Any]:
    if not isinstance(raw_profile, dict):
        raise ClaudeProfileError(f"第 {index + 1} 个 Claude 配置必须是 JSON 对象")

    name = raw_profile.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ClaudeProfileError(f"第 {index + 1} 个 Claude 配置缺少 name")

    env = raw_profile.get("env")
    if not isinstance(env, dict):
        raise ClaudeProfileError(f"Claude 配置 {name!r} 缺少有效的 env 对象")

    missing = [key for key in PROFILE_ENV_KEYS if key not in env]
    if missing:
        raise ClaudeProfileError(
            f"Claude 配置 {name!r} 缺少参数: {', '.join(missing)}"
        )

    return {
        "name": name.strip(),
        "env": {key: str(env[key]) for key in PROFILE_ENV_KEYS},
    }


def _normalize_profiles(raw_profiles: Any) -> list[dict[str, Any]]:
    # Accept an object wrapper as well, which makes hand-editing more obvious.
    if isinstance(raw_profiles, dict):
        raw_profiles = raw_profiles.get("profiles")
    if not isinstance(raw_profiles, list):
        raise ClaudeProfileError("claude_profiles.json 必须是配置数组")

    profiles = [_normalize_profile(profile, index) for index, profile in enumerate(raw_profiles)]
    names = [profile["name"] for profile in profiles]
    if len(names) != len(set(names)):
        raise ClaudeProfileError("Claude 配置名称不能重复")
    return profiles


def _atomic_write_json(path: Path, value: Any) -> None:
    temporary_path: str | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary_path = tempfile.mkstemp(
            prefix=f".{path.stem}-", suffix=".tmp", dir=str(path.parent)
        )
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as file:
            json.dump(value, file, indent=2, ensure_ascii=False)
            file.write("\n")
        os.replace(temporary_path, path)
        temporary_path = None
    except OSError as exc:
        raise ClaudeProfileError(f"无法写入配置文件: {path}") from exc
    finally:
        if temporary_path:
            try:
                os.unlink(temporary_path)
            except OSError:
                pass


def _bootstrap_profile(settings_file: Path, profiles_file: Path) -> list[dict[str, Any]]:
    settings = load_settings(settings_file)
    profile = {
        "name": DEFAULT_PROFILE_NAME,
        "env": _profile_env_from_settings(settings),
    }
    profiles = [profile]
    _atomic_write_json(profiles_file, profiles)
    return profiles


def save_profiles(
    profiles: list[dict[str, Any]],
    profiles_file: str | os.PathLike[str] | None = None,
) -> None:
    """Validate and atomically save Claude profile templates."""
    normalized = _normalize_profiles(profiles)
    if not normalized:
        raise ClaudeProfileError("至少需要保留一个 Claude 配置")
    _atomic_write_json(_profiles_path(profiles_file), normalized)


def load_profiles(
    settings_file: str | os.PathLike[str] | None = None,
    profiles_file: str | os.PathLike[str] | None = None,
) -> list[dict[str, Any]]:
    """Load profile templates, creating API-Claude from current settings once."""
    profile_path = _profiles_path(profiles_file)
    if not profile_path.exists():
        return _bootstrap_profile(settings_path(settings_file), profile_path)

    raw_profiles = _read_json(profile_path, "Claude 配置模板")
    profiles = _normalize_profiles(raw_profiles)
    if not profiles:
        return _bootstrap_profile(settings_path(settings_file), profile_path)
    return profiles


def current_profile_name(
    profiles: list[dict[str, Any]],
    settings_file: str | os.PathLike[str] | None = None,
) -> str | None:
    """Return the profile matching settings.json, without exposing credentials."""
    settings = load_settings(settings_file)
    current_env = _profile_env_from_settings(settings)
    for profile in profiles:
        if profile["env"] == current_env:
            return profile["name"]
    return None


def apply_profile(
    profile: dict[str, Any],
    settings_file: str | os.PathLike[str] | None = None,
) -> None:
    """Replace only profile keys in Claude settings, preserving everything else."""
    normalized = _normalize_profile(profile, 0)
    settings_file_path = settings_path(settings_file)
    settings = load_settings(settings_file_path)
    env = settings.get("env")
    if not isinstance(env, dict):
        raise ClaudeProfileError("Claude settings.json 中缺少有效的 env 对象")

    for key in PROFILE_ENV_KEYS:
        env[key] = normalized["env"][key]
    _atomic_write_json(settings_file_path, settings)
