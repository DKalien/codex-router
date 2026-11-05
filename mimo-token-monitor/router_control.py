"""Run Codex Router maintenance commands for the tray menu."""

from __future__ import annotations

import json
import locale
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping

from process_utils import hidden_subprocess_kwargs


ROUTER_ROOT_ENV = "MIMO_TOKEN_MONITOR_ROUTER_ROOT"


@dataclass(frozen=True)
class RouterResult:
    ok: bool
    message: str
    detail: str = ""
    route_enabled: bool | None = None
    gpt_route: str | None = None
    subagent_route: str | None = None
    gpt_effective_route: str | None = None
    subagent_effective_route: str | None = None
    pi_route: str | None = None


def resolve_router_root(environ: Mapping[str, str] | None = None) -> Path:
    env = os.environ if environ is None else environ
    configured = str(env.get(ROUTER_ROOT_ENV, "")).strip()
    if configured:
        root = Path(os.path.expandvars(configured)).expanduser()
    else:
        codex_home = Path(env.get("CODEX_HOME", Path.home() / ".codex"))
        manifest_path = codex_home / "codex-router" / "install-manifest.json"
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            root = Path(manifest["current"]["sourceRoot"])
        except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
            raise RuntimeError(
                f"找不到 Codex Router 安装位置，请设置 {ROUTER_ROOT_ENV}。"
            ) from exc

    root = root.resolve()
    required = (
        root / "codex-router.ps1",
        root / "src" / "config-manager.mjs",
        root / "src" / "gpt-route.mjs",
        root / "src" / "service.mjs",
        root / "src" / "status.mjs",
    )
    if not all(path.is_file() for path in required):
        raise RuntimeError(f"Codex Router 目录无效：{root}")
    return root


def _commands(operation: str, root: Path) -> list[list[str]]:
    node = "node"
    service = str(root / "src" / "service.mjs")
    gpt_route = str(root / "src" / "gpt-route.mjs")
    status = str(root / "src" / "status.mjs")
    script = str(root / "codex-router.ps1")
    if operation == "status":
        return [[node, status, "--json"]]
    if operation in {
        "route-wlb", "route-official", "route-supergpt", "route-auto",
        "subagent-route-wlb", "subagent-route-official", "subagent-route-supergpt",
        "subagent-route-auto",
        "pi-route-wlb", "pi-route-official", "pi-route-supergpt",
    }:
        scope = (
            ["subagent"] if operation.startswith("subagent-")
            else ["pi"] if operation.startswith("pi-route-")
            else []
        )
        mode = operation.removeprefix("subagent-").removeprefix("pi-route-").removeprefix("route-")
        return [
            [node, status, "--json"],
            [node, gpt_route, *scope, mode],
        ]
    if operation == "restart":
        return [[node, service, "restart"]]
    if operation in {"enable", "disable"}:
        return [[
            "powershell.exe",
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            script,
            operation,
        ]]
    raise ValueError(f"未知路由操作：{operation}")


def _decode(value) -> str:
    if isinstance(value, str):
        return value
    if not isinstance(value, bytes):
        return ""
    encodings = ("utf-8", locale.getpreferredencoding(False))
    for encoding in dict.fromkeys(encodings):
        try:
            return value.decode(encoding)
        except UnicodeDecodeError:
            pass
    return value.decode("utf-8", errors="replace")


def _failure_detail(result: subprocess.CompletedProcess) -> str:
    output = _decode(result.stderr).strip() or _decode(result.stdout).strip()
    return (output.splitlines()[-1] if output else f"命令退出码 {result.returncode}")[:500]


def _parse_json_object(value) -> dict | None:
    try:
        parsed = json.loads(_decode(value))
    except (TypeError, json.JSONDecodeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _status_result(label: str, result: subprocess.CompletedProcess) -> RouterResult:
    payload = _parse_json_object(result.stdout)
    if payload is None:
        return RouterResult(False, f"{label}失败", "路由器返回了无效状态")

    config = payload.get("config")
    health = payload.get("health")
    gpt_route = payload.get("gptRoute")
    if not all(isinstance(value, dict) for value in (config, health, gpt_route)):
        return RouterResult(False, f"{label}失败", "路由器返回了无效状态")
    subagent_route = payload.get("subagentRoute")
    if subagent_route is not None and not isinstance(subagent_route, dict):
        return RouterResult(False, f"{label}失败", "路由器返回了无效状态")
    pi_section = payload.get("pi")
    if pi_section is not None and not isinstance(pi_section, dict):
        return RouterResult(False, f"{label}失败", "路由器返回了无效状态")
    pi_route = pi_section.get("route") if isinstance(pi_section, dict) else None
    if pi_route is not None and not isinstance(pi_route, dict):
        return RouterResult(False, f"{label}失败", "路由器返回了无效状态")

    mode = config.get("mode")
    managed = config.get("managed") is True
    route_enabled = (
        True if mode == "router" and managed else False if mode == "native" else None
    )
    actual_route = None
    actual_subagent_route = None
    actual_pi_route = None
    if mode == "native":
        actual_route = "official"
        actual_subagent_route = "official"
        actual_pi_route = "official"
    elif (
        mode == "router"
        and managed
        and health.get("ok") is True
        and gpt_route.get("mode") in {"official", "wlb", "supergpt", "auto"}
        and gpt_route.get("readable") is True
        and gpt_route.get("ready") is True
    ):
        actual_route = gpt_route["mode"]
        if (
            isinstance(subagent_route, dict)
            and subagent_route.get("mode") in {"official", "wlb", "supergpt", "auto"}
            and subagent_route.get("readable") is True
            and subagent_route.get("ready") is True
        ):
            actual_subagent_route = subagent_route["mode"]
        if (
            isinstance(pi_route, dict)
            and pi_route.get("mode") in {"official", "wlb", "supergpt"}
            and pi_route.get("readable") is True
            and pi_route.get("ready") is True
        ):
            actual_pi_route = pi_route["mode"]

    effective_route = actual_route
    if actual_route == "auto":
        candidate = gpt_route.get("effectiveMode")
        effective_route = candidate if candidate in {"official", "wlb", "supergpt"} else None
    subagent_effective_route = actual_subagent_route
    if actual_subagent_route == "auto":
        candidate = subagent_route.get("effectiveMode") if isinstance(subagent_route, dict) else None
        subagent_effective_route = candidate if candidate in {"official", "supergpt"} else None

    if mode == "native":
        message = "官方直连"
    elif route_enabled and actual_route is not None:
        message = "自动路由已开启" if actual_route == "auto" else "路由已开启"
    elif route_enabled:
        message = "路由未就绪"
    else:
        message = "路由状态未知"
    detail = "路由器尚未就绪" if result.returncode == 1 else ""
    return RouterResult(
        True,
        message,
        detail,
        route_enabled=route_enabled,
        gpt_route=actual_route,
        subagent_route=actual_subagent_route,
        gpt_effective_route=effective_route,
        subagent_effective_route=subagent_effective_route,
        pi_route=actual_pi_route,
    )


def _route_precheck(label: str, result: subprocess.CompletedProcess) -> RouterResult | None:
    payload = _parse_json_object(result.stdout)
    if payload is None:
        return RouterResult(False, f"{label}失败", "路由器返回了无效状态")
    config = payload.get("config")
    health = payload.get("health")
    if not (
        isinstance(config, dict)
        and isinstance(health, dict)
        and config.get("mode") == "router"
        and config.get("managed") is True
        and health.get("ok") is True
    ):
        return RouterResult(
            False,
            f"{label}失败",
            "请先开启路由并确保路由器已健康运行后再切换 GPT 路由",
        )
    return None


def run_router_operation(
    operation: str,
    *,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    environ: Mapping[str, str] | None = None,
    timeout_seconds: float = 360,
) -> RouterResult:
    labels = {
        "status": "检查路由状态",
        "enable": "开启路由",
        "disable": "关闭路由",
        "restart": "重启路由器",
        "route-wlb": "切换 GPT 路由",
        "route-official": "切换 GPT 路由",
        "route-supergpt": "切换 GPT 路由",
        "route-auto": "启用 GPT 自动路由",
        "subagent-route-wlb": "切换子代理供应商",
        "subagent-route-official": "切换子代理供应商",
        "subagent-route-supergpt": "切换子代理供应商",
        "subagent-route-auto": "启用子代理自动路由",
        "pi-route-wlb": "切换 Pi 路由",
        "pi-route-official": "切换 Pi 路由",
        "pi-route-supergpt": "切换 Pi 路由",
    }
    label = labels.get(operation, "路由操作")
    try:
        root = resolve_router_root(environ)
        commands = _commands(operation, root)
    except (RuntimeError, ValueError) as exc:
        return RouterResult(False, f"{label}失败", str(exc))

    child_env = None if environ is None else dict(environ)
    route_operations = {
        "route-wlb", "route-official", "route-supergpt", "route-auto",
        "subagent-route-wlb", "subagent-route-official", "subagent-route-supergpt",
        "subagent-route-auto",
        "pi-route-wlb", "pi-route-official", "pi-route-supergpt",
    }
    for index, command in enumerate(commands):
        try:
            result = runner(
                command,
                cwd=root,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL,
                timeout=timeout_seconds,
                check=False,
                env=child_env,
                **hidden_subprocess_kwargs(),
            )
        except subprocess.TimeoutExpired:
            return RouterResult(False, f"{label}失败", "操作超时")
        except OSError as exc:
            return RouterResult(False, f"{label}失败", str(exc))
        if result.returncode != 0:
            accepts_not_ready = operation == "status" or (
                operation in route_operations and index == 0
            )
            if not (accepts_not_ready and result.returncode == 1):
                return RouterResult(False, f"{label}失败", _failure_detail(result))
        if operation in route_operations and index == 0:
            failure = _route_precheck(label, result)
            if failure is not None:
                return failure

    if operation == "status":
        return _status_result(label, result)

    messages = {
        "enable": "路由已开启",
        "disable": "路由已关闭",
        "restart": "路由器已重启",
        "route-wlb": "GPT 路由已切换到 WLB",
        "route-official": "GPT 路由已切换到官方",
        "route-supergpt": "GPT 路由已切换到 SuperGPT",
        "route-auto": "GPT 自动路由已启用",
        "subagent-route-wlb": "子代理供应商已切换到 WLB",
        "subagent-route-official": "子代理供应商已切换到官方",
        "subagent-route-supergpt": "子代理供应商已切换到 SuperGPT",
        "subagent-route-auto": "子代理自动路由已启用",
        "pi-route-wlb": "Pi 路由已切换到 WLB",
        "pi-route-official": "Pi 路由已切换到官方",
        "pi-route-supergpt": "Pi 路由已切换到 SuperGPT",
    }
    target_route = {
        "route-wlb": "wlb",
        "route-official": "official",
        "route-supergpt": "supergpt",
        "route-auto": "auto",
    }.get(operation)
    target_subagent_route = {
        "subagent-route-wlb": "wlb",
        "subagent-route-official": "official",
        "subagent-route-supergpt": "supergpt",
        "subagent-route-auto": "auto",
    }.get(operation)
    target_pi_route = {
        "pi-route-wlb": "wlb",
        "pi-route-official": "official",
        "pi-route-supergpt": "supergpt",
    }.get(operation)
    return RouterResult(
        True,
        messages[operation],
        route_enabled={
            "enable": True,
            "disable": False,
            "route-wlb": True,
            "route-official": True,
            "route-supergpt": True,
            "route-auto": True,
            "subagent-route-wlb": True,
            "subagent-route-official": True,
            "subagent-route-supergpt": True,
            "subagent-route-auto": True,
            "pi-route-wlb": True,
            "pi-route-official": True,
            "pi-route-supergpt": True,
        }.get(operation),
        gpt_route=target_route,
        subagent_route=target_subagent_route,
        # 与 _status_result 语义一致：手动模式有效路由即目标路由；auto 切换瞬间尚未判定，不猜测。
        subagent_effective_route=(
            target_subagent_route if target_subagent_route != "auto" else None
        ),
        pi_route=target_pi_route,
    )
