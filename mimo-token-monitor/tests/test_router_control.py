import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from router_control import (
    ROUTER_ROOT_ENV,
    resolve_router_root,
    run_router_operation,
)


def make_router_root(base: Path) -> Path:
    root = base / "codex-router"
    (root / "src").mkdir(parents=True)
    (root / "codex-router.ps1").touch()
    (root / "src" / "config-manager.mjs").touch()
    (root / "src" / "gpt-route.mjs").touch()
    (root / "src" / "service.mjs").touch()
    (root / "src" / "status.mjs").touch()
    return root


class TestRouterControl(unittest.TestCase):
    def test_supergpt_status_requires_readable_ready_and_healthy(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_router_root(Path(tmp))
            for healthy, readable, ready in ((True, True, True), (False, True, True),
                                            (True, False, True), (True, True, False)):
                with self.subTest(healthy=healthy, readable=readable, ready=ready):
                    payload = {
                        "config": {"mode": "router", "managed": True},
                        "health": {"ok": healthy},
                        "gptRoute": {"mode": "supergpt", "readable": readable, "ready": ready},
                    }
                    runner = Mock(return_value=subprocess.CompletedProcess(
                        [], 0 if healthy and readable and ready else 1,
                        json.dumps(payload).encode(), b"",
                    ))
                    result = run_router_operation(
                        "status", runner=runner, environ={ROUTER_ROOT_ENV: str(root)}
                    )
                    self.assertTrue(result.ok)
                    self.assertTrue(result.route_enabled)
                    self.assertEqual(result.gpt_route,
                                     "supergpt" if healthy and readable and ready else None)

    def test_supergpt_switch_reuses_precheck_and_reports_command_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_router_root(Path(tmp))
            for enabled, switch_ok in ((True, True), (True, False), (False, True)):
                with self.subTest(enabled=enabled, switch_ok=switch_ok):
                    precheck = subprocess.CompletedProcess(
                        [], 0 if enabled else 1,
                        json.dumps({
                            "config": {"mode": "router" if enabled else "native", "managed": enabled},
                            "health": {"ok": enabled},
                        }).encode(), b"",
                    )
                    runner = Mock(side_effect=[precheck, subprocess.CompletedProcess(
                        [], 0 if switch_ok else 1, b"", "SuperGPT 密钥未配置".encode(),
                    )])
                    result = run_router_operation(
                        "route-supergpt", runner=runner, environ={ROUTER_ROOT_ENV: str(root)}
                    )
                    self.assertEqual(result.ok, enabled and switch_ok)
                    self.assertEqual(runner.call_args_list[0].args[0],
                                     ["node", str(root / "src" / "status.mjs"), "--json"])
                    if enabled:
                        self.assertEqual(runner.call_args_list[1].args[0],
                                         ["node", str(root / "src" / "gpt-route.mjs"), "supergpt"])
                    self.assertEqual(runner.call_count, 2 if enabled else 1)
                    if result.ok:
                        self.assertEqual(result.message, "GPT 路由已切换到 SuperGPT")
                        self.assertTrue(result.route_enabled)
                        self.assertEqual(result.gpt_route, "supergpt")
                    else:
                        self.assertIsNone(result.gpt_route)
                        self.assertIsNone(result.route_enabled)
                        self.assertIn("密钥未配置" if enabled else "先开启路由", result.detail)

    def test_auto_status_keeps_configured_and_effective_routes_separate(self):
        from router_control import _status_result
        for effective in (None, "wlb", "official", "supergpt", "unknown"):
            with self.subTest(effective=effective):
                payload = {
                    "config": {"mode": "router", "managed": True},
                    "health": {"ok": True},
                    "gptRoute": {"mode": "auto", "readable": True, "ready": True,
                                 "effectiveMode": effective},
                    "subagentRoute": {"mode": "official", "readable": True, "ready": True},
                }
                result = _status_result("检查", subprocess.CompletedProcess([], 0, json.dumps(payload)))
                self.assertTrue(result.ok)
                self.assertEqual(result.gpt_route, "auto")
                self.assertEqual(result.gpt_effective_route, None if effective == "unknown" else effective)
                self.assertEqual(result.subagent_route, "official")
                self.assertEqual(result.message, "自动路由已开启")
                payload["health"]["ok"] = False
                result = _status_result("检查", subprocess.CompletedProcess([], 1, json.dumps(payload)))
                self.assertIsNone(result.gpt_route)
                self.assertIsNone(result.gpt_effective_route)

    def test_auto_switch_uses_existing_cli_and_never_guesses_effective_route(self):
        for healthy, success in ((True, True), (False, True), (True, False)):
            with self.subTest(healthy=healthy, success=success), tempfile.TemporaryDirectory() as tmp:
                root = make_router_root(Path(tmp))
                status = {"config": {"mode": "router", "managed": True}, "health": {"ok": healthy}}
                runner = Mock(side_effect=[
                    subprocess.CompletedProcess([], 0, json.dumps(status)),
                    subprocess.CompletedProcess([], 0 if success else 1, "", "自动模式不可用"),
                ])
                result = run_router_operation("route-auto", runner=runner, environ={ROUTER_ROOT_ENV: str(root)})
                self.assertEqual(result.ok, healthy and success)
                self.assertEqual(runner.call_count, 2 if healthy else 1)
                if healthy:
                    self.assertEqual(runner.call_args_list[1].args[0],
                                     ["node", str(root / "src" / "gpt-route.mjs"), "auto"])
                self.assertEqual(result.gpt_route, "auto" if result.ok else None)
                self.assertIsNone(result.gpt_effective_route)
                if result.ok:
                    self.assertEqual(result.message, "GPT 自动路由已启用")

    def test_resolve_root_from_install_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = make_router_root(base)
            codex_home = base / "home" / ".codex"
            manifest = codex_home / "codex-router" / "install-manifest.json"
            manifest.parent.mkdir(parents=True)
            manifest.write_text(
                json.dumps({"current": {"sourceRoot": str(root)}}),
                encoding="utf-8",
            )

            self.assertEqual(
                resolve_router_root({"CODEX_HOME": str(codex_home)}),
                root.resolve(),
            )

    def test_removed_refresh_is_rejected_without_running_commands(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_router_root(Path(tmp))

            def runner(*_args, **_kwargs):
                self.fail("Removed metadata refresh must not execute commands")

            result = run_router_operation(
                "refresh", runner=runner, environ={ROUTER_ROOT_ENV: str(root)}
            )

            self.assertFalse(result.ok)
            self.assertEqual(result.detail, "未知路由操作：refresh")

    def test_restart_runs_service_without_catalog(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_router_root(Path(tmp))
            calls = []

            def runner(command, **kwargs):
                calls.append((command, kwargs))
                return subprocess.CompletedProcess(command, 0, b"ok", b"")

            with patch("router_control.hidden_subprocess_kwargs", return_value={}):
                result = run_router_operation(
                    "restart",
                    runner=runner,
                    environ={ROUTER_ROOT_ENV: str(root)},
                )

            self.assertTrue(result.ok)
            self.assertEqual(
                [call[0][1:] for call in calls],
                [
                    [str(root / "src" / "service.mjs"), "restart"],
                ],
            )
            self.assertTrue(all(call[1]["cwd"] == root.resolve() for call in calls))
            self.assertTrue(
                all(call[1]["stdin"] == subprocess.DEVNULL for call in calls)
            )

    def test_restart_failure_reports_detail(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_router_root(Path(tmp))
            calls = []

            def runner(command, **_kwargs):
                calls.append(command)
                return subprocess.CompletedProcess(command, 1, b"", "服务重启失败".encode())

            with patch("router_control.hidden_subprocess_kwargs", return_value={}):
                result = run_router_operation(
                    "restart",
                    runner=runner,
                    environ={ROUTER_ROOT_ENV: str(root)},
                )

            self.assertFalse(result.ok)
            self.assertEqual(len(calls), 1)
            self.assertEqual(result.detail, "服务重启失败")

    def test_route_controls_reuse_existing_entrypoints(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_router_root(Path(tmp))
            calls = []

            def runner(command, **_kwargs):
                calls.append(command)
                return subprocess.CompletedProcess(command, 0, b"", b"")

            with patch("router_control.hidden_subprocess_kwargs", return_value={}):
                for operation in ("enable", "disable", "restart"):
                    result = run_router_operation(
                        operation,
                        runner=runner,
                        environ={ROUTER_ROOT_ENV: str(root)},
                    )
                    self.assertTrue(result.ok)

            self.assertEqual(calls[0][-2:], [str(root / "codex-router.ps1"), "enable"])
            self.assertEqual(calls[1][-2:], [str(root / "codex-router.ps1"), "disable"])
            self.assertEqual(calls[2][-2:], [str(root / "src" / "service.mjs"), "restart"])

    def test_status_uses_router_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_router_root(Path(tmp))
            calls = []

            def runner(command, **_kwargs):
                calls.append(command)
                return subprocess.CompletedProcess(
                    command,
                    0,
                    json.dumps(
                        {
                            "config": {"mode": "router", "managed": True},
                            "health": {"ok": True},
                            "gptRoute": {
                                "mode": "wlb",
                                "readable": True,
                                "ready": True,
                            },
                            "subagentRoute": {
                                "mode": "supergpt",
                                "readable": True,
                                "ready": True,
                            },
                        }
                    ).encode(),
                    b"",
                )

            with patch("router_control.hidden_subprocess_kwargs", return_value={}):
                result = run_router_operation(
                    "status",
                    runner=runner,
                    environ={ROUTER_ROOT_ENV: str(root)},
                )

            self.assertTrue(result.ok)
            self.assertTrue(result.route_enabled)
            self.assertEqual(result.gpt_route, "wlb")
            self.assertEqual(result.subagent_route, "supergpt")
            self.assertEqual(
                calls,
                [["node", str(root / "src" / "status.mjs"), "--json"]],
            )

    def test_subagent_auto_status_and_switch_use_two_stage_scope(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_router_root(Path(tmp))
            payload = {
                "config": {"mode": "router", "managed": True},
                "health": {"ok": True},
                "gptRoute": {"mode": "official", "readable": True, "ready": True},
                "subagentRoute": {
                    "mode": "auto",
                    "readable": True,
                    "ready": True,
                    "effectiveMode": "supergpt",
                },
            }
            calls = []

            def runner(command, **_kwargs):
                calls.append(command)
                if command[-1:] == ["--json"]:
                    return subprocess.CompletedProcess(command, 0, json.dumps(payload).encode(), b"")
                return subprocess.CompletedProcess(command, 0, b"", b"")

            with patch("router_control.hidden_subprocess_kwargs", return_value={}):
                status = run_router_operation("status", runner=runner, environ={ROUTER_ROOT_ENV: str(root)})
                switched = run_router_operation(
                    "subagent-route-auto", runner=runner, environ={ROUTER_ROOT_ENV: str(root)}
                )

            self.assertTrue(status.ok)
            self.assertEqual(status.subagent_route, "auto")
            self.assertEqual(status.subagent_effective_route, "supergpt")
            self.assertTrue(switched.ok)
            # auto 切换瞬间尚未判定有效档位，不猜测。
            self.assertEqual(switched.subagent_route, "auto")
            self.assertIsNone(switched.subagent_effective_route)
            self.assertEqual(
                calls[-1],
                ["node", str(root / "src" / "gpt-route.mjs"), "subagent", "auto"],
            )

    def test_subagent_route_switch_uses_independent_cli_scope(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_router_root(Path(tmp))
            precheck = subprocess.CompletedProcess(
                [],
                0,
                json.dumps({
                    "config": {"mode": "router", "managed": True},
                    "health": {"ok": True},
                }).encode(),
                b"",
            )
            calls = []

            def runner(command, **_kwargs):
                calls.append(command)
                return precheck if len(calls) == 1 else subprocess.CompletedProcess(command, 0, b"", b"")

            with patch("router_control.hidden_subprocess_kwargs", return_value={}):
                result = run_router_operation(
                    "subagent-route-wlb", runner=runner, environ={ROUTER_ROOT_ENV: str(root)}
                )

            self.assertTrue(result.ok)
            self.assertEqual(result.subagent_route, "wlb")
            # 手动模式有效路由即目标路由，操作完成瞬间即可判定。
            self.assertEqual(result.subagent_effective_route, "wlb")
            self.assertIsNone(result.gpt_route)
            self.assertEqual(
                calls[1], ["node", str(root / "src" / "gpt-route.mjs"), "subagent", "wlb"]
            )

    def test_status_exit_one_keeps_native_official_route(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_router_root(Path(tmp))

            def runner(command, **_kwargs):
                return subprocess.CompletedProcess(
                    command,
                    1,
                    json.dumps(
                        {
                            "config": {"mode": "native", "managed": False},
                            "health": {"ok": False},
                            "gptRoute": {
                                "mode": "official",
                                "readable": True,
                                "ready": True,
                            },
                        }
                    ).encode(),
                    b"",
                )

            with patch("router_control.hidden_subprocess_kwargs", return_value={}):
                result = run_router_operation(
                    "status",
                    runner=runner,
                    environ={ROUTER_ROOT_ENV: str(root)},
                )

            self.assertTrue(result.ok)
            self.assertFalse(result.route_enabled)
            self.assertEqual(result.gpt_route, "official")
            self.assertEqual(result.detail, "路由器尚未就绪")

    def test_status_unhealthy_router_does_not_claim_gpt_route(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_router_root(Path(tmp))

            def runner(command, **_kwargs):
                return subprocess.CompletedProcess(
                    command,
                    1,
                    json.dumps(
                        {
                            "config": {"mode": "router", "managed": True},
                            "health": {"ok": False},
                            "gptRoute": {
                                "mode": "wlb",
                                "readable": True,
                                "ready": True,
                            },
                        }
                    ).encode(),
                    b"",
                )

            with patch("router_control.hidden_subprocess_kwargs", return_value={}):
                result = run_router_operation(
                    "status",
                    runner=runner,
                    environ={ROUTER_ROOT_ENV: str(root)},
                )

            self.assertTrue(result.ok)
            self.assertTrue(result.route_enabled)
            self.assertIsNone(result.gpt_route)

    def test_status_exit_two_is_execution_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_router_root(Path(tmp))

            def runner(command, **_kwargs):
                return subprocess.CompletedProcess(command, 2, b"", "参数错误".encode())

            with patch("router_control.hidden_subprocess_kwargs", return_value={}):
                result = run_router_operation(
                    "status",
                    runner=runner,
                    environ={ROUTER_ROOT_ENV: str(root)},
                )

            self.assertFalse(result.ok)
            self.assertEqual(result.detail, "参数错误")
            self.assertIsNone(result.gpt_route)

    def test_status_malformed_json_is_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_router_root(Path(tmp))

            def runner(command, **_kwargs):
                return subprocess.CompletedProcess(command, 1, b"{broken", b"")

            with patch("router_control.hidden_subprocess_kwargs", return_value={}):
                result = run_router_operation(
                    "status",
                    runner=runner,
                    environ={ROUTER_ROOT_ENV: str(root)},
                )

            self.assertFalse(result.ok)
            self.assertEqual(result.detail, "路由器返回了无效状态")
            self.assertIsNone(result.gpt_route)

    def test_route_switch_checks_enabled_and_reports_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_router_root(Path(tmp))
            calls = []

            def runner(command, **kwargs):
                calls.append((command, kwargs))
                output = (
                    json.dumps(
                        {
                            "config": {"mode": "router", "managed": True},
                            "health": {"ok": True},
                            "gptRoute": {
                                "mode": "unknown",
                                "readable": False,
                                "ready": False,
                            },
                        }
                    ).encode()
                    if len(calls) == 1
                    else b""
                )
                return subprocess.CompletedProcess(
                    command, 1 if len(calls) == 1 else 0, output, b""
                )

            environ = {ROUTER_ROOT_ENV: str(root), "CODEX_HOME": str(Path(tmp) / "home")}
            with patch("router_control.hidden_subprocess_kwargs", return_value={}):
                result = run_router_operation(
                    "route-wlb", runner=runner, environ=environ
                )

            self.assertTrue(result.ok)
            self.assertTrue(result.route_enabled)
            self.assertEqual(result.gpt_route, "wlb")
            self.assertEqual(
                [call[0][1:] for call in calls],
                [
                    [str(root / "src" / "status.mjs"), "--json"],
                    [str(root / "src" / "gpt-route.mjs"), "wlb"],
                ],
            )
            self.assertTrue(all(call[1]["env"] == environ for call in calls))

    def test_route_switch_refuses_when_router_is_disabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_router_root(Path(tmp))
            calls = []

            def runner(command, **kwargs):
                calls.append(command)
                return subprocess.CompletedProcess(
                    command,
                    1,
                    json.dumps(
                        {
                            "config": {"mode": "native", "managed": False},
                            "health": {"ok": False},
                            "gptRoute": {
                                "mode": "official",
                                "readable": True,
                                "ready": True,
                            },
                        }
                    ).encode(),
                    b"",
                )

            with patch("router_control.hidden_subprocess_kwargs", return_value={}):
                result = run_router_operation(
                    "route-official",
                    runner=runner,
                    environ={ROUTER_ROOT_ENV: str(root)},
                )

            self.assertFalse(result.ok)
            self.assertIn("先开启路由", result.detail)
            self.assertIsNone(result.route_enabled)
            self.assertIsNone(result.gpt_route)
            self.assertEqual(len(calls), 1)

    def test_route_switch_failure_keeps_route_fields_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_router_root(Path(tmp))
            calls = []

            def runner(command, **_kwargs):
                calls.append(command)
                if len(calls) == 1:
                    return subprocess.CompletedProcess(
                        command,
                        0,
                        json.dumps(
                            {
                                "config": {"mode": "router", "managed": True},
                                "health": {"ok": True},
                                "gptRoute": {
                                    "mode": "wlb",
                                    "readable": True,
                                    "ready": True,
                                },
                            }
                        ).encode(),
                        b"",
                    )
                return subprocess.CompletedProcess(command, 1, b"", "切换失败".encode())

            with patch("router_control.hidden_subprocess_kwargs", return_value={}):
                result = run_router_operation(
                    "route-official",
                    runner=runner,
                    environ={ROUTER_ROOT_ENV: str(root)},
                )

            self.assertFalse(result.ok)
            self.assertEqual(result.detail, "切换失败")
            self.assertIsNone(result.route_enabled)
            self.assertIsNone(result.gpt_route)

    def test_pi_route_switch_uses_independent_cli_scope(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_router_root(Path(tmp))
            precheck = subprocess.CompletedProcess(
                [],
                0,
                json.dumps({
                    "config": {"mode": "router", "managed": True},
                    "health": {"ok": True},
                }).encode(),
                b"",
            )
            calls = []

            def runner(command, **_kwargs):
                calls.append(command)
                return precheck if len(calls) == 1 else subprocess.CompletedProcess(command, 0, b"", b"")

            with patch("router_control.hidden_subprocess_kwargs", return_value={}):
                result = run_router_operation(
                    "pi-route-wlb", runner=runner, environ={ROUTER_ROOT_ENV: str(root)}
                )

            self.assertTrue(result.ok)
            self.assertEqual(result.pi_route, "wlb")
            self.assertIsNone(result.gpt_route)
            self.assertIsNone(result.subagent_route)
            self.assertTrue(result.route_enabled)
            self.assertEqual(
                calls[1], ["node", str(root / "src" / "gpt-route.mjs"), "pi", "wlb"]
            )

    def test_pi_status_requires_readable_ready_and_healthy(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_router_root(Path(tmp))
            for healthy, readable, ready in ((True, True, True), (False, True, True),
                                            (True, False, True), (True, True, False)):
                with self.subTest(healthy=healthy, readable=readable, ready=ready):
                    payload = {
                        "config": {"mode": "router", "managed": True},
                        "health": {"ok": healthy},
                        "gptRoute": {"mode": "official", "readable": True, "ready": True},
                        "pi": {"route": {"mode": "wlb", "readable": readable, "ready": ready}},
                    }
                    runner = Mock(return_value=subprocess.CompletedProcess(
                        [], 0 if healthy and readable and ready else 1,
                        json.dumps(payload).encode(), b"",
                    ))
                    result = run_router_operation(
                        "status", runner=runner, environ={ROUTER_ROOT_ENV: str(root)}
                    )
                    self.assertTrue(result.ok)
                    self.assertEqual(result.pi_route,
                                     "wlb" if healthy and readable and ready else None)

    def test_pi_status_missing_or_unknown_section_stays_unchecked(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_router_root(Path(tmp))
            for pi_section in (None, {}, {"route": {"mode": "auto", "readable": True, "ready": True}}):
                with self.subTest(pi_section=pi_section):
                    payload = {
                        "config": {"mode": "router", "managed": True},
                        "health": {"ok": True},
                        "gptRoute": {"mode": "official", "readable": True, "ready": True},
                    }
                    if pi_section is not None:
                        payload["pi"] = pi_section
                    runner = Mock(return_value=subprocess.CompletedProcess(
                        [], 0, json.dumps(payload).encode(), b"",
                    ))
                    result = run_router_operation(
                        "status", runner=runner, environ={ROUTER_ROOT_ENV: str(root)}
                    )
                    self.assertTrue(result.ok)
                    self.assertIsNone(result.pi_route)


if __name__ == "__main__":
    unittest.main()
