import os
import sys
import unittest
from contextlib import contextmanager
from unittest.mock import Mock, patch

_ORIGINAL_QT_QPA_PLATFORM = os.environ.get("QT_QPA_PLATFORM")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtGui import QCloseEvent  # noqa: E402
from PyQt6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from code_sync import SyncResult, SyncStatus, UpdateCheck  # noqa: E402
from router_control import RouterResult  # noqa: E402
from widget import OVERVIEW_MODE, TokenWidget  # noqa: E402


def tearDownModule():
    if _ORIGINAL_QT_QPA_PLATFORM is None:
        os.environ.pop("QT_QPA_PLATFORM", None)
    else:
        os.environ["QT_QPA_PLATFORM"] = _ORIGINAL_QT_QPA_PLATFORM


@contextmanager
def managed_widget(cfg, **kwargs):
    with patch("widget.save_config"), patch.object(TokenWidget, "_do_fetch"):
        widget = TokenWidget(cfg, **kwargs)
        try:
            yield widget
        finally:
            widget._timer.stop()
            widget.close()
            widget.deleteLater()
            QApplication.processEvents()


class TestStartupRuntime(unittest.TestCase):
    @patch("main.load_config", return_value={"cookie": "keep", "third_party_api_key": "legacy"})
    def test_key_migration_failure_stops_startup_without_losing_settings(self, _load):
        from main import initialize_window
        from provider_keys import ProviderKeyError
        with patch("main.migrate_provider_keys", side_effect=ProviderKeyError("迁移失败")), \
             patch("main.QMessageBox.critical") as error, patch("main.TokenWidget") as widget:
            self.assertIsNone(initialize_window())
        error.assert_called_once()
        widget.assert_not_called()

    @patch("main.load_config", return_value={"cookie": "x", "third_party_api_key": "stale-key"})
    def test_key_migration_conflict_requires_explicit_choice(self, _load):
        from main import initialize_window

        conflict = {"field": "third_party_api_key", "provider": "wlb-relay",
                    "label": "WLB", "reason": "different"}
        for overwrite_choice in (False, True):
            with self.subTest(overwrite=overwrite_choice), \
                 patch("main.migrate_provider_keys", return_value=[conflict]), \
                 patch("main._confirm_key_override", return_value=overwrite_choice) as confirm, \
                 patch("main.resolve_key_migration") as resolve, \
                 patch("main.TokenWidget") as widget_type:
                widget = initialize_window()
            self.assertIs(widget, widget_type.return_value)
            confirm.assert_called_once_with(conflict)
            resolve.assert_called_once()
            args, kwargs = resolve.call_args
            self.assertEqual(args[1], "third_party_api_key")
            self.assertEqual(kwargs["overwrite"], overwrite_choice)

    @patch("main.load_config", return_value={"cookie": "x", "third_party_api_key": "stale-key"})
    def test_key_override_failure_warns_and_keeps_startup(self, _load):
        from main import initialize_window
        from provider_keys import ProviderKeyError

        conflict = {"field": "third_party_api_key", "provider": "wlb-relay",
                    "label": "WLB", "reason": "env"}
        with patch("main.migrate_provider_keys", return_value=[conflict]), \
             patch("main._confirm_key_override", return_value=True), \
             patch("main.resolve_key_migration",
                   side_effect=ProviderKeyError("环境变量覆盖了共享密钥")), \
             patch("main.QMessageBox.warning") as warning, \
             patch("main.TokenWidget") as widget_type:
            widget = initialize_window()
        warning.assert_called_once()
        self.assertIs(widget, widget_type.return_value)

    @patch("main.activate_existing_instance")
    @patch("main.check_single_instance", return_value=None)
    def test_missing_stderr_gets_persistent_descriptor_before_startup(self, check, _activate):
        from main import main

        stream = None
        try:
            with patch.object(sys, "stderr", None):
                def check_stderr():
                    self.assertIsNotNone(sys.stderr)
                    os.fstat(sys.stderr.fileno())
                    return None

                check.side_effect = check_stderr
                self.assertEqual(main(), 0)
                stream = sys.stderr
                self.assertFalse(stream.closed)
                stream.write("Playwright stderr\n")
                stream.flush()
                self.assertEqual(main(), 0)
                self.assertIs(sys.stderr, stream)
        finally:
            if stream is not None:
                stream.close()

    @patch("main.activate_existing_instance")
    @patch("main.check_single_instance", return_value=None)
    def test_existing_stderr_is_preserved(self, _check, _activate):
        from main import main

        stream = sys.stderr
        with patch("main.open", create=True) as open_stream:
            self.assertEqual(main(), 0)
        self.assertIs(sys.stderr, stream)
        open_stream.assert_not_called()

    @patch("main.load_config", return_value={"cookie": "x"})
    def test_initialize_window_loads_local_config_before_showing(self, load_config):
        calls = []
        load_config.side_effect = lambda: calls.append("load") or {"cookie": "x"}
        from main import initialize_window
        with patch("main.TokenWidget") as widget_type:
            widget_type.return_value.show = Mock(side_effect=lambda: calls.append("show"))
            widget = initialize_window()
        self.assertIs(widget, widget_type.return_value)
        widget_type.assert_called_once_with({"cookie": "x"})
        self.assertEqual(calls, ["load", "show"])


class TestExitRuntime(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_widget_true_quit_calls_callback_once(self):
        callback = Mock()
        with managed_widget({"position": [100, 100]}, exit_callback=callback) as widget:
            widget._exit_callback = callback
            widget._quit_app()
            widget._quit_app()
        callback.assert_called_once()

    def test_quit_freezes_widget_and_blocks_write_entries(self):
        callback = Mock()
        with patch("widget.save_config") as save_config, patch.object(TokenWidget, "_do_fetch") as fetch:
            widget = TokenWidget({"position": [100, 100]}, exit_callback=callback)
            fetch.reset_mock()
            widget._quit_app()
            self.assertFalse(widget.isEnabled())
            self.assertFalse(widget._timer.isActive())
            widget._open_settings()
            widget._import_cookie_quick()
            widget._do_fetch()
            save_config.assert_not_called()
            widget.deleteLater()
            QApplication.processEvents()

    def test_widget_close_event_only_hides_to_tray(self):
        callback = Mock()
        event = Mock()
        with managed_widget({"position": [100, 100]}, exit_callback=callback) as widget:
            widget._tray_icon.showMessage = Mock()
            widget.closeEvent(event)
            event.ignore.assert_called_once()
            callback.assert_not_called()
            widget._tray_icon.showMessage.assert_called_once()

    def test_tray_menu_exposes_router_maintenance(self):
        with managed_widget({"position": [100, 100]}) as widget:
            menu = widget._tray_icon.contextMenu()
            actions = {action.text(): action for action in menu.actions()}
            self.assertNotIn("更新模型元数据", actions)
            self.assertIn("刷新", actions)
            self.assertIn("Playwright 续期", actions)
            self.assertIn("路由控制（状态未知）", actions)
            self.assertIn("重启悬浮窗", actions)
            router_menu = actions["路由控制（状态未知）"].menu()
            self.assertIsNotNone(router_menu)
            self.assertEqual(
                [action.text() for action in router_menu.actions() if not action.isSeparator()],
                ["自动（WLB → GPT → SuperGPT）", "切换到 WLB", "切换到 SuperGPT", "切换到 GPT", "子代理供应商", "Pi 路由", "开启路由", "关闭路由", "重启路由器", "检查并更新代码…"],
            )
            subagent_menu = next(
                action.menu() for action in router_menu.actions() if action.text() == "子代理供应商"
            )
            self.assertEqual(
                [action.text() for action in subagent_menu.actions()],
                ["自动（GPT → SuperGPT）", "切换子代理到 WLB", "切换子代理到 SuperGPT", "切换子代理到 GPT"],
            )
            pi_menu = next(
                action.menu() for action in router_menu.actions() if action.text() == "Pi 路由"
            )
            self.assertEqual(
                [action.text() for action in pi_menu.actions()],
                ["切换 Pi 到 WLB", "切换 Pi 到 SuperGPT", "切换 Pi 到 GPT"],
            )

    def test_tray_menu_about_to_show_resets_stale_contents_margins(self):
        with managed_widget({"position": [100, 100]}) as widget:
            menu = widget._tray_menu
            baseline = menu.sizeHint()
            menu.setContentsMargins(0, 0, 16, 126)   # 缺陷截图对应的边距
            inflated = menu.sizeHint()
            self.assertGreater(inflated.height(), baseline.height())
            menu.aboutToShow.emit()
            self.assertEqual(menu.contentsMargins().right(), 0)
            self.assertEqual(menu.contentsMargins().bottom(), 0)
            self.assertEqual(menu.sizeHint(), baseline)

    def test_tray_menu_geometry_anomaly_is_logged(self):
        import tempfile
        with managed_widget({"position": [100, 100]}) as widget, \
                tempfile.TemporaryDirectory() as tmpdir:
            log_path = os.path.join(tmpdir, "tray-menu.log")
            shot_path = os.path.join(tmpdir, "tray-menu.png")
            with patch.object(TokenWidget, "_tray_menu_log_path", return_value=log_path), \
                    patch.object(TokenWidget, "_tray_menu_shot_path", return_value=shot_path):
                widget._log_tray_menu_geometry("hidden")
                self.assertFalse(os.path.exists(log_path))
                widget._tray_menu.setContentsMargins(0, 0, 16, 126)
                widget._log_tray_menu_geometry("shown")
            with open(log_path, encoding="utf-8") as handle:
                content = handle.read()
        self.assertIn("margins=0,0,16,126", content)

    def test_tray_menu_about_to_show_writes_probe_once(self):
        import tempfile
        with managed_widget({"position": [100, 100]}) as widget, \
                tempfile.TemporaryDirectory() as tmpdir:
            probe_path = os.path.join(tmpdir, "probe.log")
            with patch.object(TokenWidget, "_tray_menu_probe_path", return_value=probe_path), \
                    patch.object(TokenWidget, "_tray_menu_log_path",
                                 return_value=os.path.join(tmpdir, "anomaly.log")), \
                    patch.object(TokenWidget, "_tray_menu_shot_path",
                                 return_value=os.path.join(tmpdir, "shot.png")):
                widget._tray_menu.aboutToShow.emit()
                widget._tray_menu.aboutToShow.emit()
            with open(probe_path, encoding="utf-8") as handle:
                lines = handle.read().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertIn("event=first-popup", lines[0])

    def test_tray_menu_startup_probe_written_once_per_process(self):
        import tempfile
        with managed_widget({"position": [100, 100]}) as widget, \
                tempfile.TemporaryDirectory() as tmpdir:
            probe_path = os.path.join(tmpdir, "probe.log")
            for _ in range(3):
                widget._write_tray_menu_probe("startup", path=probe_path)
            with open(probe_path, encoding="utf-8") as handle:
                lines = handle.read().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertIn("event=startup", lines[0])

    def test_route_switch_actions_and_confirmed_selection(self):
        with managed_widget({"position": [100, 100]}) as widget:
            widget._tray_icon.showMessage = Mock()
            widget._set_gpt_route("official")
            self.assertTrue(widget._gpt_route_actions["official"].isChecked())
            self.assertFalse(widget._gpt_route_actions["wlb"].isChecked())
            with patch.object(widget, "_start_router_operation") as start:
                widget._gpt_route_actions["wlb"].trigger()
                start.assert_called_once_with("route-wlb")
                start.reset_mock()
                widget._gpt_route_actions["official"].trigger()
                start.assert_called_once_with("route-official")

            widget._router_worker = Mock()
            widget._on_router_operation_done(
                RouterResult(True, "已切换到 WLB", route_enabled=True, gpt_route="wlb")
            )
            self.assertEqual(widget._gpt_route, "wlb")
            self.assertTrue(widget._gpt_route_actions["wlb"].isChecked())
            self.assertFalse(widget._gpt_route_actions["official"].isChecked())
            widget._router_worker = Mock()
            widget._on_router_operation_done(RouterResult(False, "切换失败"))
            self.assertIsNone(widget._gpt_route)
            self.assertFalse(any(action.isChecked() for action in widget._gpt_route_actions.values()))

    def test_auto_selection_and_effective_highlight_are_independent(self):
        with managed_widget({"position": [100, 100]}) as widget:
            widget._tray_icon.showMessage = Mock()
            widget._set_gpt_route("official")
            with patch("widget.RouterWorker") as worker_type:
                widget._start_router_operation("route-auto")
                worker_type.assert_called_once_with("route-auto", widget)
                self.assertTrue(widget._gpt_route_actions["official"].isChecked())
                self.assertFalse(widget._gpt_route_actions["auto"].isChecked())
                self.assertTrue(all(not action.isEnabled() for action in widget._router_actions))
                widget._on_router_operation_done(RouterResult(True, "自动路由已启用", gpt_route="auto"))
            self.assertTrue(widget._gpt_route_actions["auto"].isChecked())
            self.assertIsNone(widget._effective_gpt_route)
            self.assertIn("auto", widget._subagent_route_actions)
            for effective in ("wlb", "official", "supergpt", None):
                widget._on_router_status_done(RouterResult(
                    True, "自动路由已开启", route_enabled=True, gpt_route="auto",
                    subagent_route="official", gpt_effective_route=effective,
                ))
                self.assertEqual(widget._effective_gpt_route, effective)
                self.assertTrue(widget._gpt_route_actions["auto"].isChecked())
                self.assertTrue(widget._subagent_route_actions["official"].isChecked())
                self.assertEqual(sum(action.isChecked() for action in widget._gpt_route_actions.values()), 1)
            widget._on_router_operation_done(RouterResult(False, "切换失败"))
            self.assertIsNone(widget._effective_gpt_route)
            self.assertFalse(any(action.isChecked() for action in widget._gpt_route_actions.values()))

    def test_subagent_route_switch_actions_and_confirmed_selection(self):
        with managed_widget({"position": [100, 100]}) as widget:
            widget._tray_icon.showMessage = Mock()
            widget._set_subagent_route("official")
            self.assertTrue(widget._subagent_route_actions["official"].isChecked())
            with patch.object(widget, "_start_router_operation") as start:
                widget._subagent_route_actions["wlb"].trigger()
                start.assert_called_once_with("subagent-route-wlb")
                start.reset_mock()
                widget._subagent_route_actions["auto"].trigger()
                start.assert_called_once_with("subagent-route-auto")

            widget._router_worker = Mock()
            widget._on_router_operation_done(
                RouterResult(
                    True,
                    "子代理供应商已切换到 WLB",
                    route_enabled=True,
                    subagent_route="wlb",
                )
            )
            self.assertEqual(widget._subagent_route, "wlb")
            self.assertTrue(widget._subagent_route_actions["wlb"].isChecked())
            self.assertFalse(widget._subagent_route_actions["official"].isChecked())

            widget._router_worker = Mock()
            widget._on_router_status_done(RouterResult(
                True,
                "子代理自动路由已开启",
                route_enabled=True,
                subagent_route="auto",
                subagent_effective_route="supergpt",
            ))
            self.assertEqual(widget._effective_subagent_route, "supergpt")
            self.assertTrue(widget._subagent_route_actions["auto"].isChecked())

            widget._router_worker = Mock()
            widget._on_router_operation_done(RouterResult(False, "切换失败"))
            self.assertIsNone(widget._subagent_route)
            self.assertFalse(any(action.isChecked() for action in widget._subagent_route_actions.values()))

    def test_router_status_updates_tray_menu(self):
        with managed_widget({"position": [100, 100]}) as widget:
            worker = Mock()
            widget._router_worker = worker

            widget._on_router_status_done(
                RouterResult(True, "路由已开启", route_enabled=True)
            )

            self.assertEqual(widget._router_menu_action.text(), "路由控制（已开启）")

            widget._router_worker = Mock()
            widget._on_router_status_done(
                RouterResult(True, "路由已关闭", route_enabled=False)
            )

            self.assertEqual(widget._router_menu_action.text(), "路由控制（已关闭）")

    def test_route_click_waits_for_background_status_then_runs(self):
        with managed_widget({"position": [100, 100]}) as widget:
            widget._router_worker = Mock(operation="status")
            widget._router_worker.isRunning.return_value = True
            widget._start_router_operation("route-wlb")
            self.assertEqual(widget._queued_router_operation, "route-wlb")
            self.assertFalse(any(action.isEnabled() for action in widget._router_actions))
            with patch.object(widget, "_start_router_operation") as start:
                widget._on_router_status_done(
                    RouterResult(True, "路由已开启", route_enabled=True, gpt_route="official")
                )
                start.assert_called_once_with("route-wlb")
            self.assertIsNone(widget._queued_router_operation)

    def test_router_status_refresh_keeps_visible_menu_stable(self):
        with managed_widget({"position": [100, 100]}) as widget:
            widget._set_router_state(True)
            size_before = widget._tray_menu.sizeHint()
            worker = Mock()
            with patch("widget.RouterWorker", return_value=worker):
                widget._refresh_router_state()
            widget._on_router_status_done(
                RouterResult(True, "路由已开启", route_enabled=True)
            )

            self.assertEqual(widget._router_menu_action.text(), "路由控制（已开启）")
            self.assertEqual(widget._tray_menu.sizeHint(), size_before)
            self.assertTrue(all(action.isEnabled() for action in widget._router_actions))
            worker.start.assert_called_once()

    def test_router_action_starts_before_deferred_menu_refresh(self):
        with managed_widget({"position": [100, 100]}) as widget:
            widget._router_worker = None
            worker = Mock()
            worker.isRunning.return_value = True
            widget._tray_icon.showMessage = Mock()
            router_menu = next(
                action.menu()
                for action in widget._tray_menu.actions()
                if action.text() == "路由控制（状态未知）"
            )
            enable_action = next(
                action
                for action in router_menu.actions()
                if action.text() == "开启路由"
            )

            with patch("widget.RouterWorker", return_value=worker) as worker_type:
                widget._tray_menu.aboutToHide.emit()
                enable_action.trigger()
                QApplication.processEvents()

            worker_type.assert_called_once_with("enable", widget)
            worker.start.assert_called_once()
            self.assertIs(widget._router_worker, worker)

    def test_restart_action_requests_exit(self):
        callback = Mock()
        with managed_widget(
            {"position": [100, 100]}, exit_callback=callback
        ) as widget:
            actions = {
                action.text(): action
                for action in widget._tray_icon.contextMenu().actions()
            }

            actions["重启悬浮窗"].trigger()

            self.assertTrue(widget._restart_requested)
            callback.assert_called_once()

    def test_router_completion_reenables_menu_and_notifies(self):
        with managed_widget({"position": [100, 100]}) as widget:
            worker = Mock()
            widget._router_worker = worker
            widget._tray_icon.showMessage = Mock()
            for action in widget._router_actions:
                action.setEnabled(False)

            widget._on_router_operation_done(
                RouterResult(True, "路由器已重启")
            )

            worker.wait.assert_called_once()
            worker.deleteLater.assert_called_once()
            self.assertIsNone(widget._router_worker)
            self.assertTrue(all(action.isEnabled() for action in widget._router_actions))
            widget._tray_icon.showMessage.assert_called_once()



class TestCodeUpdateRuntime(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_tray_menu_exposes_explicit_code_update_entry(self):
        with managed_widget({"position": [100, 100]}) as widget:
            action = widget._code_update_action
            self.assertIsNotNone(action)
            self.assertEqual(action.text(), "检查并更新代码…")

    def test_code_update_requires_confirmation_and_reports_scope(self):
        check = UpdateCheck(
            SyncResult(
                SyncStatus.SUCCESS,
                "code_check",
                "远端有 1 个新提交：Router 核心 1 个文件",
            ),
            behind=1,
            ahead=0,
            changed_paths=("src/router.mjs",),
            scope="Router 核心 1 个文件",
            remote_commit="abc123",
        )
        with managed_widget({"position": [100, 100]}) as widget:
            widget._tray_icon.showMessage = Mock()
            with patch.object(widget, "_confirm_code_update", return_value=False) as confirm, \
                 patch("widget.CodeSyncWorker") as worker_type:
                widget._on_code_check_done(check)
            confirm.assert_called_once_with(check)
            worker_type.assert_not_called()
            self.assertIsNone(widget._code_sync_worker)
            self.assertTrue(widget._code_update_action.isEnabled())

            with patch.object(widget, "_confirm_code_update", return_value=True), \
                 patch("widget.CodeSyncWorker") as worker_type:
                widget._on_code_check_done(check)
                worker_type.assert_called_once_with(
                    "apply", widget, expected_remote="abc123"
                )
            widget._code_sync_worker = None
            widget._on_code_update_done(
                SyncResult(
                    SyncStatus.SUCCESS,
                    "code_pull",
                    "已拉取代码仓库最新更新：Router 核心 1 个文件",
                    "运行中的 Router 未重启，重启后生效",
                )
            )
            widget._tray_icon.showMessage.assert_called()
            self.assertIn(
                "Router 核心", widget._tray_icon.showMessage.call_args.args[1]
            )
            self.assertTrue(widget._code_update_action.isEnabled())

    def test_check_without_updates_or_failure_is_reported(self):
        with managed_widget({"position": [100, 100]}) as widget:
            widget._tray_icon.showMessage = Mock()
            widget._on_code_check_done(
                UpdateCheck(SyncResult(SyncStatus.NO_CHANGE, "code_check", "代码仓库已是最新"))
            )
            widget._tray_icon.showMessage.assert_not_called()
            widget._on_code_check_done(
                UpdateCheck(SyncResult(SyncStatus.FAILED, "code_check", "代码更新检查失败"))
            )
            widget._tray_icon.showMessage.assert_called_once()

    def test_startup_sync_result_only_announces_success_or_failure(self):
        with managed_widget({"position": [100, 100]}) as widget:
            widget._tray_icon.showMessage = Mock()
            widget.show_startup_sync_result(
                SyncResult(SyncStatus.SKIPPED, "config", "代码仓库同步已禁用")
            )
            widget._tray_icon.showMessage.assert_not_called()
            widget.show_startup_sync_result(
                SyncResult(SyncStatus.SUCCESS, "code_pull", "已更新：监控器 1 个文件")
            )
            widget._tray_icon.showMessage.assert_called_once()


class TestBaseUrlFirstUse(unittest.TestCase):
    """Issue #3 补充：旧配置非默认域名首次使用确认，确认前不发送共享 Key。"""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_old_config_non_default_host_blocks_key_until_confirmed(self):
        cfg = {"position": [100, 100], "cookie": "x",
               "third_party_base_url": "https://relay.example.test"}
        with managed_widget(cfg) as widget:
            # 拒绝：不创建 worker（不读不发共享 Key），本会话不重复询问
            with patch("widget.QMessageBox.question",
                       return_value=QMessageBox.StandardButton.No) as question, \
                 patch("widget.ThirdPartyFetchWorker") as worker:
                widget._do_fetch_third_party()
                widget._do_fetch_third_party()
            question.assert_called_once()
            worker.assert_not_called()
            self.assertIn("未确认", widget._tp_error)
            self.assertNotIn(
                "relay.example.test", widget.cfg.get("approved_base_url_hosts", [])
            )

            # 显式确认：持久化已确认域名并放行
            widget._base_url_declined.clear()
            with patch("widget.QMessageBox.question",
                       return_value=QMessageBox.StandardButton.Yes) as question, \
                 patch("widget.save_config") as save_config, \
                 patch("widget.ThirdPartyFetchWorker") as worker:
                widget._do_fetch_third_party()
            question.assert_called_once()
            worker.assert_called_once()
            save_config.assert_called_once()
            self.assertIn(
                "relay.example.test", widget.cfg["approved_base_url_hosts"]
            )

            # 已确认后不再询问
            widget._tp_worker = None
            with patch("widget.QMessageBox.question") as question, \
                 patch("widget.ThirdPartyFetchWorker") as worker:
                widget._do_fetch_third_party()
            question.assert_not_called()
            worker.assert_called_once()

    def test_default_host_passes_and_supergpt_uses_same_gate(self):
        cfg = {"position": [100, 100], "cookie": "x"}
        with managed_widget(cfg) as widget:
            with patch("widget.QMessageBox.question") as question, \
                 patch("widget.ThirdPartyFetchWorker") as worker:
                widget._do_fetch_third_party()
            question.assert_not_called()
            worker.assert_called_once()

            widget.cfg["display_mode"] = OVERVIEW_MODE
            widget.cfg["supergpt_base_url"] = "https://relay.example.test"
            with patch("widget.QMessageBox.question",
                       return_value=QMessageBox.StandardButton.No), \
                 patch("widget.SuperGPTBalanceWorker") as worker:
                widget._do_fetch_supergpt_balance()
            worker.assert_not_called()
            self.assertIn("未确认", widget._supergpt_error)

    def test_confirmed_host_is_read_from_old_config_without_prompt(self):
        cfg = {"position": [100, 100], "cookie": "x",
               "third_party_base_url": "https://relay.example.test",
               "approved_base_url_hosts": ["relay.example.test"]}
        with managed_widget(cfg) as widget:
            with patch("widget.QMessageBox.question") as question, \
                 patch("widget.ThirdPartyFetchWorker") as worker:
                widget._do_fetch_third_party()
            question.assert_not_called()
            worker.assert_called_once()


class TestLifecycleDegradation(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_close_event_hides_without_requesting_exit(self):
        callback = Mock()
        cfg = {"cookie": "x", "position": [100, 100], "refresh_interval": 300,
               "opacity": 0.85, "always_on_top": True}
        with managed_widget(cfg, exit_callback=callback) as widget:
            widget._tray_icon = Mock()
            event = QCloseEvent()
            widget.closeEvent(event)
        self.assertFalse(event.isAccepted())
        callback.assert_not_called()

    def test_quit_action_requests_exit_once(self):
        callback = Mock()
        cfg = {"cookie": "x", "position": [100, 100], "refresh_interval": 300,
               "opacity": 0.85, "always_on_top": True}
        with managed_widget(cfg, exit_callback=callback) as widget:
            widget._quit_app()
            widget._quit_app()
        callback.assert_called_once()

    @patch("main.QProcess.startDetached", return_value=(True, 123))
    def test_restart_relaunches_source_after_shutdown(self, start_detached):
        from main import restart_application

        with (
            patch.object(sys, "argv", ["D:\\app\\main.py"]),
            patch.object(sys, "executable", "D:\\Python\\pythonw.exe"),
            patch.object(sys, "frozen", False, create=True),
        ):
            self.assertTrue(restart_application())

        start_detached.assert_called_once_with(
            "D:\\Python\\pythonw.exe",
            ["D:\\app\\main.py"],
            os.path.dirname(os.path.dirname(__file__)),
        )

    @patch("main.save_config")
    @patch("main.load_config", return_value={})
    @patch("main.SettingsDialog")
    def test_first_configuration_cancel_returns_safely(
        self, dialog_type, _load, save_config
    ):
        from PyQt6.QtWidgets import QDialog
        from main import initialize_window
        dialog_type.return_value.exec.return_value = QDialog.DialogCode.Rejected
        with patch("main.TokenWidget") as widget_type:
            widget = initialize_window()
        self.assertIsNone(widget)
        widget_type.assert_not_called()
        dialog_type.assert_called_once_with({})
        dialog_type.return_value.setWindowTitle.assert_called_once_with("MiMo Token - 首次配置")
        save_config.assert_not_called()

    @patch("main.activate_existing_instance")
    @patch("main.check_single_instance", return_value=None)
    def test_duplicate_instance_returns_before_initialization(self, _check, activate):
        from main import main
        self.assertEqual(main(), 0)
        activate.assert_called_once()
