from PyQt6.QtCore import Qt, QTimer, QThread, pyqtSignal, QPoint, QRect, QRectF, QPointF
from PyQt6.QtGui import QColor, QPainter, QBrush, QPen, QAction, QFont, QFontMetrics, QIcon, QCursor, QPolygonF, QPainterPath
from PyQt6.QtWidgets import (
    QWidget, QMenu, QDialog, QFormLayout,
    QLineEdit, QSpinBox, QDoubleSpinBox, QPushButton,
    QCheckBox, QHBoxLayout, QVBoxLayout, QLabel, QMessageBox,
    QApplication, QSystemTrayIcon, QComboBox, QDialogButtonBox,
)
from datetime import datetime, timedelta, timezone
from copy import deepcopy
import json
import math
import os
import sys
import api_client
import code_sync
from claude_profiles import (
    ClaudeProfileError,
    PROFILE_ENV_KEYS,
    apply_profile,
    current_profile_name,
    load_profiles,
    save_profiles,
)
from config import save_config
import cookie_reader
import playwright_session
import provider_keys
import router_control
import snapshot_writer
import supergpt_balance
import usage_url
import window_snap

SNAP_THRESHOLD = 15  # px, 距屏幕边缘多少像素内触发吸附

MIMO_MODE = "mimo"
THIRD_PARTY_MODE = "third_party"
OVERVIEW_MODE = "overview"

BASE_HEIGHT = 140
# Overview keeps the same compact footprint as the other display modes.
OVERVIEW_HEIGHT = BASE_HEIGHT
DUAL_BAR_GAP = 6

# ── Colors ──────────────────────────────────────────────────────
BG_COLOR = QColor(30, 30, 30, 220)
TEXT_COLOR = QColor(230, 230, 230)
ACCENT_GREEN = QColor(76, 175, 80)
ACCENT_YELLOW = QColor(255, 193, 7)
ACCENT_RED = QColor(244, 67, 54)
BAR_BG = QColor(60, 60, 60)
DIM = QColor(150, 150, 150)
BALANCE_LAYER_COLORS = (
    ACCENT_GREEN, QColor(100, 181, 246), QColor(206, 147, 216), QColor(77, 208, 225),
)


def _bar_color(pct: float) -> QColor:
    if pct > 0.5:
        return ACCENT_GREEN
    if pct > 0.2:
        return ACCENT_YELLOW
    return ACCENT_RED


def _fmt_tokens(n) -> str:
    if n is None:
        return "--"
    n = int(n)
    if n >= 1_000_000_000:
        return f"{n / 1_000_000_000:.1f}B"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


def _fmt_money(v) -> str:
    if v is None:
        return "--"
    return f"¥{float(v):.2f}"


def _expiry_days_left(expiry_date):
    """Return days until the user-entered expiry date, or None if invalid."""
    expiry_text = str(expiry_date or "").strip()
    if not expiry_text:
        return None

    try:
        expiry = datetime.strptime(expiry_text, "%Y-%m-%d").date()
    except ValueError:
        return None
    return (expiry - datetime.now().date()).days


def _format_expiry(expiry_date) -> str:
    """Format the user-entered expiry date and add a short-term reminder."""
    expiry_text = str(expiry_date or "").strip()
    if not expiry_text:
        return "未设置"

    days_left = _expiry_days_left(expiry_text)
    if days_left is None:
        return expiry_text
    if 0 <= days_left < 7:
        return f"{expiry_text}，还剩{days_left}天"
    return expiry_text


def _format_overview_expiry(expiry_date) -> str:
    """Format a valid overview date as remaining days."""
    expiry_text = str(expiry_date or "").strip()
    days_left = _expiry_days_left(expiry_text)
    return f"{max(0, days_left)}天" if days_left is not None else ""


def _parse_reset_datetime(reset_at, reset_after=None):
    """Parse an ISO/Unix reset time, falling back to a relative delay."""
    parsed = reset_at if isinstance(reset_at, datetime) else None
    if parsed is None and isinstance(reset_at, (int, float)) and not isinstance(reset_at, bool):
        try:
            parsed = datetime.fromtimestamp(float(reset_at), timezone.utc)
        except (OSError, OverflowError, ValueError):
            parsed = None
    if parsed is None:
        raw_value = str(reset_at or "").strip()
        if raw_value:
            try:
                parsed = datetime.fromisoformat(raw_value.replace("Z", "+00:00"))
            except ValueError:
                try:
                    parsed = datetime.fromtimestamp(float(raw_value), timezone.utc)
                except (OSError, OverflowError, ValueError):
                    parsed = None
    if parsed is not None:
        try:
            return parsed.astimezone().replace(tzinfo=None) if parsed.tzinfo else parsed
        except (OverflowError, OSError, ValueError):
            pass

    try:
        seconds = float(reset_after)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(seconds) or seconds < 0:
        return None
    return datetime.now().astimezone().replace(tzinfo=None) + timedelta(seconds=seconds)


def _reset_parts(window):
    """Return absolute and relative reset values from one usage window."""
    if not isinstance(window, dict):
        return None, None
    reset_after = window.get("reset_after_seconds")
    if reset_after is None:
        reset_after = window.get("reset_after")
    return window.get("reset_at"), reset_after


def _window_used_percent(window):
    """Return a window percentage, treating an absent rate limit as no data."""
    if not isinstance(window, dict) or window.get("has_rate_limit", True) is False:
        return None
    return window.get("used_percent")


def _format_reset_datetime(reset_at, reset_after=None) -> str:
    """Format a reset timestamp for the full-detail views."""
    parsed = _parse_reset_datetime(reset_at, reset_after)
    if parsed is not None:
        return parsed.strftime("%Y-%m-%d %H:%M:%S")
    return str(reset_at or "").strip()


def _format_overview_reset_time(reset_at, reset_after=None) -> str:
    """Format a reset time as HH:MM, or a clear placeholder."""
    parsed = _parse_reset_datetime(reset_at, reset_after)
    return parsed.strftime("%H:%M") if parsed is not None else "--:--"


def _format_overview_reset_days(reset_at, reset_after=None) -> str:
    """Format days until reset as N天, or a clear placeholder."""
    parsed = _parse_reset_datetime(reset_at, reset_after)
    if parsed is None:
        return "--天"
    days_left = max(0, (parsed.date() - datetime.now().date()).days)
    return f"{days_left}天"


# ── Plan tier definitions ──────────────────────────────────────
PLAN_TIERS = [
    (82_000_000_000, "Max", 659),
    (38_000_000_000, "Pro", 329),
    (11_000_000_000, "Standard", 99),
    (4_100_000_000, "Lite", 39),
]


def _get_plan_tier_info(total: int):
    """Return (tier_name, monthly_price, cost_per_credit) based on total plan tokens."""
    for credits, name, price in sorted(PLAN_TIERS, reverse=True):
        if total >= credits * 0.95:
            return name, price, price / credits
    return None, None, None
# ── Probe thread ────────────────────────────────────────────────
class FetchWorker(QThread):
    finished = pyqtSignal(dict, dict)

    def __init__(self, cookie):
        super().__init__()
        self.cookie = cookie

    def run(self):
        bal = api_client.fetch_balance(self.cookie)
        usage = api_client.fetch_usage(self.cookie)
        self.finished.emit(bal, usage)


class PlaywrightCookieWorker(QThread):
    """Refresh the persistent Playwright session away from the UI thread."""

    # Avoid clashing with QThread.finished(); deliver (cookie, error, interactive).
    result_ready = pyqtSignal(object, object, bool)

    def __init__(
        self,
        interactive: bool = False,
        previous_cookie: str | None = None,
        parent=None,
    ):
        super().__init__(parent)
        self.interactive = interactive
        self.previous_cookie = previous_cookie

    @staticmethod
    def _cookie_is_usable(cookie: str) -> bool:
        result = api_client.fetch_balance(cookie)
        return bool(result.get("ok"))

    def run(self):
        try:
            if self.interactive:
                # Keep the headed browser open until a usable cookie appears so the
                # user can finish login / captcha instead of the window closing early.
                cookie, error = playwright_session.refresh_cookie(
                    interactive=True,
                    timeout_seconds=180,
                    previous_cookie=self.previous_cookie,
                    cookie_validator=self._cookie_is_usable,
                )
            else:
                cookie, error = playwright_session.refresh_cookie(
                    interactive=False,
                    timeout_seconds=30,
                    previous_cookie=self.previous_cookie,
                )
                if cookie:
                    result = api_client.fetch_balance(cookie)
                    if not result.get("ok"):
                        error = result.get("error") or "Cookie 验证失败"
                        cookie = None
        except Exception as exc:  # noqa: BLE001 - worker must always emit
            cookie, error = None, f"Playwright 续期异常（{type(exc).__name__}）"
        self.result_ready.emit(cookie, error, self.interactive)



# ── Third-party usage probe thread ──────────────────────────────
class ThirdPartyFetchWorker(QThread):
    """Background thread for third-party usage API fetch."""
    finished = pyqtSignal(dict)

    def __init__(self, base_url: str, window: str = "7d"):
        super().__init__()
        self.base_url = base_url
        self.window = window

    def run(self):
        configured = None
        try:
            key = provider_keys.read_key("wlb-relay")
            configured = bool(key)
            result = api_client.fetch_third_party_usage(self.base_url, key, self.window) if key else {
                "ok": False, "data": None, "error": "请在设置中配置与 Router 共用的 WLB API Key",
            }
        except provider_keys.ProviderKeyError as exc:
            result = {"ok": False, "data": None, "error": str(exc)}
        except Exception:
            result = {"ok": False, "data": None, "error": "WLB 用量查询失败"}
        result["key_configured"] = configured
        self.finished.emit(result)

# -- GPT weekly usage probe thread --
class GPTFetchWorker(QThread):
    finished = pyqtSignal(dict)

    def __init__(self, session_cookie=""):
        super().__init__()
        self.session_cookie = session_cookie

    def run(self):
        result = api_client.fetch_gpt_weekly_usage(self.session_cookie)
        self.finished.emit(result)


class SuperGPTBalanceWorker(QThread):
    result_ready = pyqtSignal(dict)

    def __init__(self, parent=None, *, base_url=""):
        super().__init__(parent)
        self.base_url = base_url

    def run(self):
        if self.isInterruptionRequested():
            return
        try:
            result = supergpt_balance.fetch_balance(self.base_url)
        except Exception:
            result = {"ok": False, "balance": None, "currency": None, "error": "SuperGPT 余额查询失败"}
        if not self.isInterruptionRequested():
            self.result_ready.emit(result)


class RouterWorker(QThread):
    completed = pyqtSignal(object)

    def __init__(self, operation: str, parent=None):
        super().__init__(parent)
        self.operation = operation

    def run(self):
        self.completed.emit(router_control.run_router_operation(self.operation))


class CodeSyncWorker(QThread):
    """显式代码更新：只检查或安全快进磁盘源码，不触碰 Node Router。"""

    completed = pyqtSignal(object)

    def __init__(self, mode: str, parent=None, expected_remote: str = ""):
        super().__init__(parent)
        self.mode = mode  # "check" | "apply"
        self.expected_remote = expected_remote

    def run(self):
        project_root = os.path.dirname(os.path.abspath(__file__))
        try:
            if self.mode == "check":
                result = code_sync.run_explicit_code_check(project_root)
            else:
                result = code_sync.run_explicit_code_update(
                    project_root, expected_remote=self.expected_remote
                )
        except Exception:
            result = code_sync.SyncResult(
                code_sync.SyncStatus.FAILED, "code_sync", "代码更新执行异常"
            )
        self.completed.emit(result)


# ── Settings dialog ─────────────────────────────────────────────
class ProviderKeysWorker(QThread):
    def __init__(self, keys: dict, parent=None):
        super().__init__(parent)
        self.keys = keys
        self.error = ""

    def run(self):
        try:
            provider_keys.save_keys(self.keys)
        except provider_keys.ProviderKeyError as exc:
            self.error = str(exc)
        except Exception:
            self.error = "共享密钥保存失败，请检查 Router 安装位置和文件权限"
        finally:
            self.keys = {}


class SettingsDialog(QDialog):
    def __init__(self, cfg: dict, parent=None):
        super().__init__(parent)
        self.setWindowTitle("MiMo Token 设置")
        self.setFixedSize(500, 600)
        self.cfg = dict(cfg)
        self._keys_worker = None

        layout = QFormLayout(self)

        self.cookie_edit = QLineEdit(cfg.get("cookie", ""))
        self.cookie_edit.setPlaceholderText("手动粘贴或点击右侧按钮从浏览器自动导入")

        import_btn = QPushButton("从浏览器导入")
        import_btn.setFixedWidth(110)
        import_btn.clicked.connect(self._import_cookie)

        cookie_row = QHBoxLayout()
        cookie_row.addWidget(self.cookie_edit)
        cookie_row.addWidget(import_btn)
        layout.addRow("Cookie:", cookie_row)

        self.interval_spin = QSpinBox()
        self.interval_spin.setRange(60, 3600)
        self.interval_spin.setSingleStep(60)
        self.interval_spin.setValue(cfg.get("refresh_interval", 300))
        self.interval_spin.setSuffix(" 秒")
        layout.addRow("刷新间隔:", self.interval_spin)

        self.playwright_check = QCheckBox("Playwright 自动续期")
        self.playwright_check.setChecked(cfg.get("playwright_auto_refresh", True))
        layout.addRow("登录保持:", self.playwright_check)

        self.playwright_interval_spin = QSpinBox()
        self.playwright_interval_spin.setRange(3600, 86400)
        self.playwright_interval_spin.setSingleStep(3600)
        self.playwright_interval_spin.setValue(cfg.get("playwright_refresh_interval", 21600))
        self.playwright_interval_spin.setSuffix(" 秒")
        layout.addRow("续期间隔:", self.playwright_interval_spin)

        self.opacity_spin = QDoubleSpinBox()
        self.opacity_spin.setRange(0.3, 1.0)
        self.opacity_spin.setSingleStep(0.05)
        self.opacity_spin.setValue(cfg.get("opacity", 0.85))
        layout.addRow("透明度:", self.opacity_spin)

        self.expiry_edit = QLineEdit(str(cfg.get("expiry_date", "") or ""))
        self.expiry_edit.setPlaceholderText("例如 2026-08-31")
        layout.addRow("有效期至:", self.expiry_edit)

        self.expiry_alert_check = QCheckBox("有效期小于等于 3 天时显示红色")
        self.expiry_alert_check.setChecked(cfg.get("expiry_alert_enabled", True))
        layout.addRow("到期提醒:", self.expiry_alert_check)

        self.snapshot_edit = QLineEdit(cfg.get("snapshot_path", ""))
        self.snapshot_edit.setPlaceholderText("留空禁用 | 例: ~/.claude/plugins/claude-hud/mimo-snapshot.json")
        layout.addRow("快照路径:", self.snapshot_edit)

        # third-party usage settings
        self.tp_base_url_edit = QLineEdit(cfg.get("third_party_base_url", "https://codex.wlbclub.com"))
        self.tp_base_url_edit.setPlaceholderText("可填主域名、/v1 或完整 /v1/usage，程序会自动规范")
        layout.addRow("WLB Base URL:", self.tp_base_url_edit)

        self.tp_api_key_edit = QLineEdit()
        self.tp_api_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.tp_api_key_edit.setPlaceholderText("与 Router 共用；留空保持现有密钥")
        layout.addRow("WLB API Key:", self.tp_api_key_edit)

        self.supergpt_base_url_edit = QLineEdit(
            cfg.get("supergpt_base_url", supergpt_balance.DEFAULT_BASE_URL)
        )
        self.supergpt_base_url_edit.setPlaceholderText("可填主域名、/v1 或完整 /v1/usage")
        layout.addRow("SuperGPT Base URL:", self.supergpt_base_url_edit)

        self.supergpt_api_key_edit = QLineEdit()
        self.supergpt_api_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.supergpt_api_key_edit.setPlaceholderText("与 Router 共用；留空保持现有密钥")
        layout.addRow("SuperGPT API Key:", self.supergpt_api_key_edit)

        self.gpt_session_cookie_edit = QLineEdit(cfg.get("gpt_session_cookie", ""))
        self.gpt_session_cookie_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.gpt_session_cookie_edit.setPlaceholderText("可选: ChatGPT session-token cookie，用于获取周限额")
        layout.addRow("GPT Session Cookie:", self.gpt_session_cookie_edit)

        hint = QLabel(
            "自动导入: Edge 快捷方式末尾加 --remote-debugging-port=9222 --remote-allow-origins=*，重启浏览器后点击按钮\n"
            "手动导入: F12 → Network → 刷新页面 → 点任意请求 → 复制 Cookie 头\n\n"
            "Playwright 自动续期: 使用独立浏览器配置定时刷新登录状态；首次或失效时可能需要验证码\n"
            "有效期至: 手动填写套餐到期日期，例如 2026-08-31\n"
            "快照路径: 填写后会生成 JSON 供 claude-hud 读取显示用量\n"
            "WLB: 填写 Base URL 和 API Key 后可点击标题栏切换图标显示第三方用量\n"
            "WLB / SuperGPT API Key: 与 Router 共用，保存后双方生效；留空保持不变\n"
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color: gray; font-size: 11px;")
        layout.addRow(hint)

        btn_row = QHBoxLayout()
        ok_btn = QPushButton("保存")
        ok_btn.clicked.connect(self.accept)
        cancel_btn = QPushButton("取消")
        cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(ok_btn)
        btn_row.addWidget(cancel_btn)
        layout.addRow(btn_row)

    def _validate_base_urls(self) -> bool:
        """保存前统一校验 Base URL；非默认域名需用户显式确认。

        与请求路径共用 ``usage_url`` 规则：默认仅 HTTPS、禁止内嵌凭据、
        查询参数与片段；校验失败不发请求、不保存。
        """
        fields = (
            ("WLB", self.tp_base_url_edit, api_client.DEFAULT_THIRD_PARTY_BASE_URL),
            ("SuperGPT", self.supergpt_base_url_edit, supergpt_balance.DEFAULT_BASE_URL),
        )
        for label, edit, default in fields:
            try:
                normalized = usage_url.validate_base_url(
                    edit.text().strip(), default_base=default, label=label
                )
            except usage_url.UsageUrlError as exc:
                QMessageBox.warning(self, f"{label} Base URL 无效", str(exc))
                edit.setFocus()
                return False
            edit.setText(normalized)
            host = usage_url.endpoint_host(normalized)
            if host and host != usage_url.endpoint_host(default):
                answer = QMessageBox.question(
                    self,
                    f"{label} Base URL 确认",
                    f"{label} Base URL 不是官方默认域名，API Key 会发送到该主机。\n"
                    "确认这是你信任的自建或第三方中转地址吗？",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No,
                )
                if answer != QMessageBox.StandardButton.Yes:
                    return False
                approved = set(self.cfg.get("approved_base_url_hosts") or [])
                approved.add(host)
                self.cfg["approved_base_url_hosts"] = sorted(approved)
        return True

    def accept(self):
        if self._keys_worker is not None:
            return
        if not self._validate_base_urls():
            return
        keys = {provider: edit.text().strip() for provider, edit in (
            ("wlb-relay", self.tp_api_key_edit), ("supergpt-relay", self.supergpt_api_key_edit),
        ) if edit.text().strip()}
        if not keys:
            super().accept()
            return
        self._keys_worker = ProviderKeysWorker(keys, self)
        self._keys_worker.finished.connect(self._finish_key_save)
        self.setEnabled(False)
        self._keys_worker.start()

    def _finish_key_save(self):
        worker = self._keys_worker
        self._keys_worker = None
        self.setEnabled(True)
        error = worker.error
        worker.deleteLater()
        if error:
            QMessageBox.critical(self, "共享密钥保存失败", error)
            return
        self.tp_api_key_edit.clear()
        self.supergpt_api_key_edit.clear()
        super().accept()

    def reject(self):
        if self._keys_worker is None:
            super().reject()

    def _import_cookie(self):
        cookie_str, error = cookie_reader.import_cookie_from_browser()
        if not cookie_str:
            QMessageBox.warning(self, "导入失败", error or "无法从浏览器读取 Cookie")
            return

        # 验证 cookie 是否有效
        result = api_client.fetch_balance(cookie_str)
        if result["ok"]:
            self.cookie_edit.setText(cookie_str)
            QMessageBox.information(self, "导入成功", "Cookie 已从浏览器读取并验证有效")
        elif "过期" in (result.get("error") or ""):
            QMessageBox.warning(
                self, "Cookie 已过期",
                "浏览器中的 Cookie 也已过期，请先在浏览器中重新登录\n"
                "platform.xiaomimimo.com，然后重试",
            )
        else:
            # 网络错误等：仍然填入，让用户自行判断
            self.cookie_edit.setText(cookie_str)
            QMessageBox.warning(
                self, "导入成功但验证失败",
                f"Cookie 已读取，但验证时出错：{result.get('error', '未知错误')}\n已填入，请手动确认",
            )

    def get_config(self) -> dict:
        self.cfg["cookie"] = self.cookie_edit.text().strip()
        self.cfg["refresh_interval"] = self.interval_spin.value()
        self.cfg["playwright_auto_refresh"] = self.playwright_check.isChecked()
        self.cfg["playwright_refresh_interval"] = self.playwright_interval_spin.value()
        self.cfg["opacity"] = self.opacity_spin.value()
        self.cfg["expiry_date"] = self.expiry_edit.text().strip()
        self.cfg["expiry_alert_enabled"] = self.expiry_alert_check.isChecked()
        self.cfg["snapshot_path"] = self.snapshot_edit.text().strip()
        self.cfg["third_party_base_url"] = self.tp_base_url_edit.text().strip() or "https://codex.wlbclub.com"
        self.cfg["supergpt_base_url"] = self.supergpt_base_url_edit.text().strip() or supergpt_balance.DEFAULT_BASE_URL
        for field in provider_keys.KEY_FIELDS:
            self.cfg.pop(field, None)
        self.cfg["gpt_session_cookie"] = self.gpt_session_cookie_edit.text().strip()
        return self.cfg


class ClaudeProfilesDialog(QDialog):
    """Edit Claude profile templates without editing JSON by hand."""

    _FIELD_LABELS = {
        "ANTHROPIC_AUTH_TOKEN": "Auth Token:",
        "ANTHROPIC_BASE_URL": "Base URL:",
        "ANTHROPIC_DEFAULT_FABLE_MODEL": "Fable Model:",
        "ANTHROPIC_DEFAULT_OPUS_MODEL": "Opus Model:",
        "ANTHROPIC_DEFAULT_SONNET_MODEL": "Sonnet Model:",
        "ANTHROPIC_DEFAULT_HAIKU_MODEL": "Haiku Model:",
        "CLAUDE_CODE_SUBAGENT_MODEL": "Subagent Model:",
    }

    def __init__(self, profiles: list[dict], parent=None):
        super().__init__(parent)
        self.setWindowTitle("Claude 配置模板")
        self.setMinimumSize(650, 470)
        self.resize(700, 520)
        self._profiles = deepcopy(profiles)
        self._current_index = -1

        root_layout = QVBoxLayout(self)
        intro = QLabel(
            "在这里填写配置模板。保存只更新模板文件，不会立即切换当前 Claude 配置；\n"
            "请在托盘的“切换 Claude 配置”菜单中选择要使用的配置。"
        )
        intro.setWordWrap(True)
        root_layout.addWidget(intro)

        profile_row = QHBoxLayout()
        profile_row.addWidget(QLabel("配置名称:"))
        self.profile_combo = QComboBox()
        self.profile_combo.setMinimumWidth(260)
        profile_row.addWidget(self.profile_combo, 1)
        new_button = QPushButton("新增")
        new_button.clicked.connect(self._add_profile)
        profile_row.addWidget(new_button)
        self.delete_button = QPushButton("删除")
        self.delete_button.clicked.connect(self._delete_profile)
        profile_row.addWidget(self.delete_button)
        root_layout.addLayout(profile_row)

        form = QFormLayout()
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("例如 API-GPT 或 Mimo")
        form.addRow("显示名称:", self.name_edit)

        self._env_edits: dict[str, QLineEdit] = {}
        for key in PROFILE_ENV_KEYS:
            edit = QLineEdit()
            edit.setClearButtonEnabled(True)
            if key == "ANTHROPIC_AUTH_TOKEN":
                edit.setEchoMode(QLineEdit.EchoMode.Password)
                edit.setPlaceholderText("填写 Auth Token")
                show_token = QCheckBox("显示")
                show_token.toggled.connect(
                    lambda checked, edit=edit: edit.setEchoMode(
                        QLineEdit.EchoMode.Normal if checked else QLineEdit.EchoMode.Password
                    )
                )
                token_row = QHBoxLayout()
                token_row.addWidget(edit)
                token_row.addWidget(show_token)
                field = token_row
            elif key == "ANTHROPIC_BASE_URL":
                edit.setPlaceholderText("例如 https://api.example.com")
                field = edit
            else:
                edit.setPlaceholderText("填写模型名称")
                field = edit
            self._env_edits[key] = edit
            form.addRow(self._FIELD_LABELS[key], field)
        root_layout.addLayout(form)

        token_hint = QLabel("Auth Token 默认隐藏，可直接粘贴；其余 settings.json 内容不会被修改。")
        token_hint.setStyleSheet("color: gray; font-size: 11px;")
        root_layout.addWidget(token_hint)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        root_layout.addWidget(buttons)

        self.profile_combo.currentIndexChanged.connect(self._profile_selection_changed)
        for profile in self._profiles:
            self.profile_combo.addItem(str(profile.get("name", "")))
        if self._profiles:
            self.profile_combo.setCurrentIndex(0)
        else:
            self._add_profile()

    def _collect_current(self) -> None:
        if not 0 <= self._current_index < len(self._profiles):
            return
        profile = self._profiles[self._current_index]
        profile["name"] = self.name_edit.text().strip()
        profile["env"] = {
            key: edit.text() for key, edit in self._env_edits.items()
        }
        self.profile_combo.setItemText(self._current_index, profile["name"])

    def _load_profile(self, index: int) -> None:
        if not 0 <= index < len(self._profiles):
            self.name_edit.clear()
            for edit in self._env_edits.values():
                edit.clear()
            self.delete_button.setEnabled(False)
            return

        profile = self._profiles[index]
        self.name_edit.setText(str(profile.get("name", "")))
        env = profile.get("env", {})
        for key, edit in self._env_edits.items():
            edit.setText(str(env.get(key, "")))
        self.delete_button.setEnabled(len(self._profiles) > 1)

    def _profile_selection_changed(self, index: int) -> None:
        self._collect_current()
        self._current_index = index
        self._load_profile(index)

    def _add_profile(self) -> None:
        self._collect_current()
        existing_names = {str(profile.get("name", "")) for profile in self._profiles}
        base_name = "新配置"
        name = base_name
        suffix = 2
        while name in existing_names:
            name = f"{base_name}{suffix}"
            suffix += 1
        profile = {
            "name": name,
            "env": {key: "" for key in PROFILE_ENV_KEYS},
        }
        self._profiles.append(profile)
        self.profile_combo.addItem(name)
        self.profile_combo.setCurrentIndex(len(self._profiles) - 1)

    def _delete_profile(self) -> None:
        if len(self._profiles) <= 1 or not 0 <= self._current_index < len(self._profiles):
            return
        answer = QMessageBox.question(
            self,
            "删除配置",
            f"确定删除配置“{self._profiles[self._current_index].get('name', '')}”吗？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return

        index = self._current_index
        self._profiles.pop(index)
        new_index = min(index, len(self._profiles) - 1)
        self.profile_combo.blockSignals(True)
        try:
            self.profile_combo.removeItem(index)
            self.profile_combo.setCurrentIndex(new_index)
        finally:
            self.profile_combo.blockSignals(False)
        self._current_index = new_index
        self._load_profile(new_index)

    def _save(self) -> None:
        self._collect_current()
        try:
            save_profiles(self._profiles)
        except ClaudeProfileError as exc:
            QMessageBox.warning(self, "保存失败", str(exc))
            return
        self.accept()


# ── Main widget ─────────────────────────────────────────────────
class TokenWidget(QWidget):
    def __init__(self, cfg: dict, exit_callback=None):
        super().__init__()
        self.cfg = cfg
        self._exit_callback = exit_callback
        self._exit_requested = False
        self._restart_requested = False
        self._drag_pos: QPoint | None = None
        self._last_error = ""
        self._mimo_error = ""
        self._tp_error = ""
        # GPT weekly usage state
        self._gpt_data: dict | None = None
        self._gpt_data_time: datetime | None = None
        self._gpt_error = ""
        self._supergpt_balance: dict | None = None
        self._supergpt_error = ""
        self._supergpt_last_update = ""
        self._supergpt_worker: SuperGPTBalanceWorker | None = None
        self._last_update = "等待更新..."
        self._router_status = ""
        self._router_status_ok: bool | None = None
        self._router_status_timer = QTimer(self)
        self._router_status_timer.setSingleShot(True)
        self._router_status_timer.timeout.connect(self._clear_router_status)
        self._pin_btn_rect = QRect()  # placeholder, set in paintEvent
        self._router_worker: RouterWorker | None = None
        self._queued_router_operation: str | None = None
        self._code_sync_worker: CodeSyncWorker | None = None
        self._code_update_action: QAction | None = None
        self._base_url_declined: set[str] = set()
        self._router_actions: list[QAction] = []
        self._claude_profiles: list[dict] = []
        self._claude_profile_actions: dict[str, QAction] = {}
        self._claude_profile_menu: QMenu | None = None
        self._claude_profile_menu_action: QAction | None = None
        self._gpt_route: str | None = None
        self._effective_gpt_route: str | None = None
        self._subagent_route: str | None = None
        self._effective_subagent_route: str | None = None
        self._pi_route: str | None = None
        self._router_poll_timer = QTimer(self)
        self._router_poll_timer.timeout.connect(self._refresh_router_state)
        self._playwright_worker: PlaywrightCookieWorker | None = None
        self._playwright_timer = QTimer(self)
        self._playwright_timer.timeout.connect(self._refresh_playwright_cookie)
        self._playwright_recovery_attempted = False

        # Data from API
        self._balance: float | None = None
        self._plan_total = 0       # total plan credits (limit)
        self._plan_used = 0        # total plan used
        self._month_used = 0       # this month used
        self._month_limit = 0      # this month limit
        # Pay-as-you-go
        self._payg_tokens = 0
        self._payg_input = 0
        self._payg_output = 0
        self._payg_total_cost: float | None = None
        self._payg_month_cost: float | None = None
        # Daily usage
        self._daily_used = 0  # Today's token usage

        # Third-party usage state
        self._tp_data: dict | None = None
        self._tp_key_configured = False
        self._switch_rect = QRect()

        # Always-on-top: controlled by cfg; default True for backward compat
        self._always_on_top = cfg.get("always_on_top", True)
        _flags = Qt.WindowType.FramelessWindowHint | Qt.WindowType.Tool
        if self._always_on_top:
            _flags |= Qt.WindowType.WindowStaysOnTopHint
        self.setWindowFlags(_flags)
        self.setWindowTitle("MiMo Token Monitor")  # native title for Win32 EnumWindows
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground)
        self.setAutoFillBackground(False)
        self.setMouseTracking(True)
        self.setFixedWidth(260)
        self._apply_size_for_mode(cfg.get("display_mode", MIMO_MODE))
        pos = self._resolve_start_position(cfg.get("position", [100, 100]))
        self.move(*pos)
        if cfg.get("position") != list(pos):
            cfg["position"] = list(pos)
            save_config(cfg)
        self.setWindowOpacity(cfg.get("opacity", 0.85))

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._do_fetch)
        interval_ms = cfg.get("refresh_interval", 300) * 1000
        self._timer.start(interval_ms)
        if cfg.get("playwright_auto_refresh", True):
            playwright_interval_ms = max(
                3600, int(cfg.get("playwright_refresh_interval", 21600))
            ) * 1000
            self._playwright_timer.start(playwright_interval_ms)

        # 系统托盘
        self._setup_tray()

        QTimer.singleShot(500, self._do_fetch)

    def _resolve_start_position(self, raw_position) -> tuple[int, int]:
        """Keep a previously saved position visible after monitor changes."""
        try:
            position = (int(raw_position[0]), int(raw_position[1]))
        except (TypeError, ValueError, IndexError):
            position = (100, 100)

        window_rect = QRect(position[0], position[1], self.width(), self.height())
        minimum_visible = 20
        for screen in QApplication.screens():
            visible_part = window_rect.intersected(screen.availableGeometry())
            if (
                visible_part.width() >= minimum_visible
                and visible_part.height() >= minimum_visible
            ):
                return position

        primary_screen = QApplication.primaryScreen()
        if primary_screen is None:
            return (100, 100)

        available = primary_screen.availableGeometry()
        margin = 20
        x = available.left() + min(
            100, max(0, available.width() - self.width() - margin)
        )
        y = available.top() + min(
            100, max(0, available.height() - self.height() - margin)
        )
        return (x, y)

    def _apply_size_for_mode(self, mode: str) -> None:
        """Resize the window consistently when switching display modes."""
        target_height = OVERVIEW_HEIGHT if mode == OVERVIEW_MODE else BASE_HEIGHT
        if self.height() == target_height:
            return
        top_left = self.geometry().topLeft()
        self.setFixedHeight(target_height)
        screen = QApplication.screenAt(top_left)
        if screen is None:
            screen = QApplication.primaryScreen()
        if screen is None:
            return
        geometry = screen.availableGeometry()
        x = max(geometry.left(), min(top_left.x(), geometry.right() - self.width() + 1))
        y = max(geometry.top(), min(top_left.y(), geometry.bottom() - self.height() + 1))
        self.move(x, y)

    @staticmethod
    def _normalize_overview_percent(value):
        try:
            percent = float(value)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(percent):
            return None
        return max(0.0, min(100.0, percent))

    @classmethod
    def _format_overview_percent(cls, value, configured: bool) -> str:
        if not configured:
            return "未配置"
        percent = cls._normalize_overview_percent(value)
        if percent is None:
            return "--"
        return f"{100 - percent:.1f}%"

    @staticmethod
    def _overview_row_metrics(index: int):
        """Align the first-row bar and balance with the dual-bar columns."""
        row_x = 16
        label_y = 38 + index * 34
        bar_y = 44 + index * 34
        bar_width = (228 - DUAL_BAR_GAP) // 2 if index == 0 else 228
        return row_x, label_y, bar_y, bar_width, 14

    def _build_mimo_tooltip_lines(self) -> list:
        lines = []
        if self._balance is not None and self._balance != 0:
            lines.append(f"余额: {_fmt_money(self._balance)}")
        if self._plan_total > 0:
            pct = self._plan_used / self._plan_total * 100
            remaining = self._plan_total - self._plan_used
            lines.append(f"Token Plan: {pct:.1f}%")
            lines.append(f"已用: {_fmt_tokens(self._plan_used)}")
            lines.append(f"总额: {_fmt_tokens(self._plan_total)}")
            lines.append(f"剩余: {_fmt_tokens(max(0, remaining))}")
            _, _, cost_per_credit = _get_plan_tier_info(self._plan_total)
            if cost_per_credit:
                used_cost = self._plan_used * cost_per_credit
                lines.append(f"已用折合 ≈ ¥{used_cost:.2f}")
            if self._daily_used > 0:
                lines.append(f"今日已用 {_fmt_tokens(self._daily_used)}")
            expiry_date = _format_expiry(self.cfg.get("expiry_date", ""))
            lines.append(f"有效期至: {expiry_date}")
        if self._month_limit > 0:
            m_pct = self._month_used / self._month_limit * 100
            lines.append(f"本月: {_fmt_tokens(self._month_used)} / {_fmt_tokens(self._month_limit)} ({m_pct:.1f}%)")
        return lines

    def _build_third_party_tooltip_lines(self) -> list:
        lines = ["WLB"]
        if self._tp_data:
            d = self._tp_data
            remaining_pct = d.get("remaining_percent") if d.get("has_rate_limit", True) else None
            used_pct = _window_used_percent(d)
            lines.append(f"剩余: {remaining_pct:.2f}%" if remaining_pct is not None else "剩余: --")
            lines.append(f"已用: {used_pct:.2f}%" if used_pct is not None else "已用: --")
            lines.append(f"窗口: {d.get('window', '7d')}")
            lines.append(f"总额: {d.get('total_percent', 100)}%")
            reset_text = _format_reset_datetime(*_reset_parts(d))
            if reset_text:
                lines.append(f"7日重置: {reset_text}")
            lines.append(f"状态: {'Active' if d.get('is_valid') else 'Inactive'}")
        return lines

    def _build_overview_tooltip_lines(self) -> list:
        lines = ["总览"]
        lines.append(f"SuperGPT 余额: {self._supergpt_balance_text()}")
        if self._supergpt_balance is not None:
            layers, fraction = self._supergpt_balance_layers()
            lines.append(
                f"SuperGPT 血条: 每层 100 {self._supergpt_balance['currency']}，"
                f"{layers} 层，当前层 {fraction * 100:.2f}%"
            )
        if self._supergpt_last_update:
            lines.append(f"SuperGPT 更新于: {self._supergpt_last_update}")
        if self._supergpt_error:
            stale = "（数据已过期，继续显示上次余额）" if self._supergpt_balance else ""
            detail = self._error_display_detail("SuperGPT", self._supergpt_error)
            lines.append(f"SuperGPT{stale}: {detail}")
        has_cookie = bool(self.cfg.get("cookie", "").strip())
        mimo_pct = (self._plan_used / self._plan_total * 100) if has_cookie and self._plan_total > 0 else None
        lines.append(f"Token Plan 剩余: {self._format_overview_percent(mimo_pct, has_cookie)}")
        has_api_key = self._tp_key_configured
        tp_weekly = self._tp_data if has_api_key and isinstance(self._tp_data, dict) else None
        tp_daily = tp_weekly.get("daily") if isinstance(tp_weekly, dict) else None
        tp_daily_pct = _window_used_percent(tp_daily)
        tp_weekly_pct = _window_used_percent(tp_weekly)
        lines.append(
            f"WLB 日限额剩余: {self._format_overview_percent(tp_daily_pct, has_api_key)}"
        )
        lines.append(
            f"WLB 周限额剩余: {self._format_overview_percent(tp_weekly_pct, has_api_key)}"
        )
        reset_text = _format_reset_datetime(*_reset_parts(tp_weekly))
        if reset_text:
            lines.append(f"WLB 周重置: {reset_text}")
        has_gpt = bool(self.cfg.get("gpt_session_cookie", "").strip()) or (isinstance(self._gpt_data, dict) and self._gpt_data.get("used_percent") is not None)
        gpt_pct = self._gpt_data.get("used_percent") if isinstance(self._gpt_data, dict) else None
        gpt_primary = self._gpt_data.get("primary") if isinstance(self._gpt_data, dict) else None
        primary_pct = _window_used_percent(gpt_primary)
        lines.append(f"GPT 5\u5c0f\u65f6剩余: {self._format_overview_percent(primary_pct, has_gpt)}")
        lines.append(f"GPT \u5468\u9650\u989d剩余: {self._format_overview_percent(gpt_pct, has_gpt)}")
        if self._gpt_data_time is not None:
            lines.append(f"GPT 数据时间: {self._gpt_data_time:%Y-%m-%d %H:%M:%S}")
        return lines

    @staticmethod
    def _error_display_detail(provider: str, error: str) -> str:
        detail = str(error or "").strip()
        for prefix in (f"{provider}:", f"{provider}：", f"{provider} "):
            if detail.casefold().startswith(prefix.casefold()):
                detail = detail[len(prefix):].strip()
                break
        if detail.casefold() == provider.casefold():
            detail = ""
        return detail or "未知错误"

    def _refresh_error_state(self):
        """按当前页面显示对应供应商错误，同时保留各来源原始错误。"""
        display_mode = self.cfg.get("display_mode", MIMO_MODE)
        if display_mode == MIMO_MODE:
            self._last_error = (
                f"MiMo: {self._error_display_detail('MiMo', self._mimo_error)}"
                if self._mimo_error
                else ""
            )
        elif display_mode == THIRD_PARTY_MODE:
            self._last_error = (
                f"WLB: {self._error_display_detail('WLB', self._tp_error)}"
                if self._tp_error
                else ""
            )
        else:
            errors = (
                ("MiMo", self._mimo_error),
                ("WLB", self._tp_error),
                ("GPT", self._gpt_error),
                ("SuperGPT", self._supergpt_error),
            )
            self._last_error = next(
                (
                    f"{name}: {self._error_display_detail(name, error)}"
                    for name, error in errors
                    if error
                ),
                "",
            )

    def _setup_tray(self):
        """Initialize system tray icon and menu."""
        # 兼容 PyInstaller 打包后的路径
        base_path = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))
        icon_path = os.path.join(base_path, "icon.ico")
        if os.path.exists(icon_path):
            self._tray_icon = QSystemTrayIcon(QIcon(icon_path), self)
        else:
            self._tray_icon = QSystemTrayIcon(self)

        # 托盘右键菜单
        tray_menu = QMenu(self)
        self._tray_menu = tray_menu

        show_act = QAction("显示主窗口", self)
        show_act.triggered.connect(self._show_window)
        tray_menu.addAction(show_act)

        refresh_act = QAction("刷新", self)
        refresh_act.triggered.connect(self._do_fetch)
        tray_menu.addAction(refresh_act)

        import_act = QAction("从浏览器导入", self)
        import_act.triggered.connect(self._import_cookie_quick)
        tray_menu.addAction(import_act)

        renew_act = QAction("Playwright 续期", self)
        renew_act.triggered.connect(
            lambda _checked=False: self._refresh_playwright_cookie(interactive=True)
        )
        tray_menu.addAction(renew_act)

        self._setup_claude_profile_menu(tray_menu)

        tray_menu.addSeparator()

        router_menu = tray_menu.addMenu("路由控制（状态未知）")
        self._router_menu_action = router_menu.menuAction()
        self._gpt_route_actions = {}
        for route, title in (
            ("auto", "自动（WLB → GPT → SuperGPT）"),
            ("wlb", "切换到 WLB"),
            ("supergpt", "切换到 SuperGPT"),
            ("official", "切换到 GPT"),
        ):
            action = router_menu.addAction(title)
            action.setCheckable(True)
            action.setToolTip("全局切换：影响后续非子代理的原生 GPT 请求；子代理供应商请使用下方菜单")
            operation = f"route-{route}"
            action.triggered.connect(
                lambda _checked=False, operation=operation: self._start_router_operation(operation)
            )
            self._gpt_route_actions[route] = action
        subagent_menu = router_menu.addMenu("子代理供应商")
        self._subagent_route_actions = {}
        for route, title in (
            ("auto", "自动（GPT → SuperGPT）"),
            ("wlb", "切换子代理到 WLB"),
            ("supergpt", "切换子代理到 SuperGPT"),
            ("official", "切换子代理到 GPT"),
        ):
            action = subagent_menu.addAction(title)
            action.setCheckable(True)
            action.setToolTip("仅影响已识别的原生 GPT 子代理；主代理路由保持不变")
            operation = f"subagent-route-{route}"
            action.triggered.connect(
                lambda _checked=False, operation=operation: self._start_router_operation(operation)
            )
            self._subagent_route_actions[route] = action
        pi_menu = router_menu.addMenu("Pi 路由")
        self._pi_route_actions = {}
        for route, title in (
            ("wlb", "切换 Pi 到 WLB"),
            ("supergpt", "切换 Pi 到 SuperGPT"),
            ("official", "切换 Pi 到 GPT"),
        ):
            action = pi_menu.addAction(title)
            action.setCheckable(True)
            action.setToolTip("仅影响 Pi 的 GPT 请求；Codex 主代理与子代理路由保持不变")
            operation = f"pi-route-{route}"
            action.triggered.connect(
                lambda _checked=False, operation=operation: self._start_router_operation(operation)
            )
            self._pi_route_actions[route] = action
        router_menu.addSeparator()
        enable_act = router_menu.addAction("开启路由")
        enable_act.triggered.connect(
            lambda _checked=False: self._start_router_operation("enable")
        )
        disable_act = router_menu.addAction("关闭路由")
        disable_act.triggered.connect(
            lambda _checked=False: self._start_router_operation("disable")
        )
        router_menu.addSeparator()
        restart_act = router_menu.addAction("重启路由器")
        restart_act.triggered.connect(
            lambda _checked=False: self._start_router_operation("restart")
        )
        self._code_update_action = router_menu.addAction("检查并更新代码…")
        self._code_update_action.setToolTip(
            "显式检查并快进更新磁盘源码；不会重启正在运行的 Router"
        )
        self._code_update_action.triggered.connect(self._check_code_updates)
        self._router_actions = [
            enable_act, disable_act, restart_act,
            *self._gpt_route_actions.values(),
            *self._subagent_route_actions.values(),
            *self._pi_route_actions.values(),
        ]
        tray_menu.aboutToHide.connect(
            lambda: QTimer.singleShot(0, self._refresh_router_state)
        )

        tray_menu.addSeparator()

        restart_widget_act = QAction("重启悬浮窗", self)
        restart_widget_act.triggered.connect(
            lambda _checked=False: self._quit_app(restart=True)
        )
        tray_menu.addAction(restart_widget_act)

        quit_act = QAction("退出", self)
        quit_act.triggered.connect(self._quit_app)
        tray_menu.addAction(quit_act)

        self._tray_icon.setContextMenu(tray_menu)
        self._tray_icon.setToolTip("MiMo Token Monitor")

        # 双击托盘图标显示/隐藏窗口
        self._tray_icon.activated.connect(self._on_tray_activated)

        self._tray_icon.show()
        if QSystemTrayIcon.isSystemTrayAvailable():
            self._refresh_router_state()
            self._router_poll_timer.start(30_000)

    def _setup_claude_profile_menu(self, tray_menu: QMenu) -> None:
        """Add a tray submenu for switching Claude Code environment templates."""
        profile_menu = tray_menu.addMenu("切换 Claude 配置")
        self._claude_profile_menu = profile_menu
        self._claude_profile_menu_action = profile_menu.menuAction()
        profile_menu.aboutToShow.connect(self._reload_claude_profile_menu)
        self._reload_claude_profile_menu()

    def _reload_claude_profile_menu(self) -> None:
        """Reload templates so hand-edited additions work without restarting."""
        profile_menu = self._claude_profile_menu
        if profile_menu is None:
            return

        profile_menu.clear()
        self._claude_profiles = []
        self._claude_profile_actions.clear()

        edit_action = profile_menu.addAction("编辑配置…")
        edit_action.triggered.connect(self._open_claude_profiles_dialog)
        profile_menu.addSeparator()
        try:
            self._claude_profiles = load_profiles()
            active_name = current_profile_name(self._claude_profiles)
        except ClaudeProfileError as exc:
            if self._claude_profile_menu_action is not None:
                self._claude_profile_menu_action.setToolTip(str(exc))
            error_action = profile_menu.addAction("配置不可用")
            error_action.setEnabled(False)
            return

        if self._claude_profile_menu_action is not None:
            self._claude_profile_menu_action.setToolTip(
                "从 claude_profiles.json 读取；只替换 Claude settings.json 的 env 配置参数"
            )
        for profile in self._claude_profiles:
            name = profile["name"]
            action = profile_menu.addAction(name)
            action.setCheckable(True)
            action.setChecked(name == active_name)
            action.setToolTip("只替换 Claude settings.json 的 env 配置参数")
            action.triggered.connect(
                lambda _checked=False, profile=profile: self._switch_claude_profile(profile)
            )
            self._claude_profile_actions[name] = action

    def _open_claude_profiles_dialog(self) -> None:
        """Open the form used to add and edit Claude profile templates."""
        try:
            profiles = load_profiles()
        except ClaudeProfileError as exc:
            self._tray_icon.showMessage(
                "Claude 配置",
                str(exc),
                QSystemTrayIcon.MessageIcon.Warning,
                5000,
            )
            return

        dialog = ClaudeProfilesDialog(profiles, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self._reload_claude_profile_menu()
            self._tray_icon.showMessage(
                "Claude 配置",
                "配置模板已保存，可从菜单选择要使用的配置",
                QSystemTrayIcon.MessageIcon.Information,
                4000,
            )

    def _switch_claude_profile(self, profile: dict) -> None:
        """Apply a profile and refresh the menu only after a successful write."""
        name = profile.get("name", "未命名配置")
        try:
            apply_profile(profile)
            for profile_name, action in self._claude_profile_actions.items():
                action.setChecked(profile_name == name)
            self._tray_icon.showMessage(
                "Claude 配置",
                f"已切换到 {name}，重启 Claude Code 后生效",
                QSystemTrayIcon.MessageIcon.Information,
                4000,
            )
        except ClaudeProfileError as exc:
            # QAction can become checked before its slot runs; restore the
            # actual state so a failed write is never shown as successful.
            try:
                active_name = current_profile_name(self._claude_profiles)
            except ClaudeProfileError:
                active_name = None
            for profile_name, action in self._claude_profile_actions.items():
                action.setChecked(profile_name == active_name)
            self._tray_icon.showMessage(
                "Claude 配置切换失败",
                str(exc),
                QSystemTrayIcon.MessageIcon.Warning,
                5000,
            )

    def _set_router_state(self, enabled: bool | None):
        state = "状态未知" if enabled is None else "已开启" if enabled else "已关闭"
        self._router_menu_action.setText(f"路由控制（{state}）")

    def _set_gpt_route(self, route: str | None, effective_route: str | None = None):
        previous = self._effective_gpt_route
        self._gpt_route = route if route in {"official", "wlb", "supergpt", "auto"} else None
        self._effective_gpt_route = (
            effective_route if effective_route in {"official", "wlb", "supergpt"} else None
        ) if self._gpt_route == "auto" else self._gpt_route
        for name, action in self._gpt_route_actions.items():
            action.setChecked(name == self._gpt_route)
        if previous != self._effective_gpt_route:
            self.update()

    def _set_subagent_route(self, route: str | None, effective_route: str | None = None):
        previous = self._effective_subagent_route
        self._subagent_route = route if route in {"official", "wlb", "supergpt", "auto"} else None
        self._effective_subagent_route = (
            effective_route if effective_route in {"official", "supergpt"} else None
        ) if self._subagent_route == "auto" else self._subagent_route
        for name, action in self._subagent_route_actions.items():
            action.setChecked(name == self._subagent_route)
        if previous != self._effective_subagent_route:
            self.update()

    def _set_pi_route(self, route: str | None):
        self._pi_route = route if route in {"official", "wlb", "supergpt"} else None
        for name, action in self._pi_route_actions.items():
            action.setChecked(name == self._pi_route)

    def _refresh_router_state(self):
        if self._exit_requested:
            return
        if self._tray_menu.isVisible():
            return
        if self._router_worker is not None:
            return
        self._router_worker = RouterWorker("status", self)
        self._router_worker.completed.connect(self._on_router_status_done)
        self._router_worker.start()

    def _finish_router_worker(self):
        worker = self._router_worker
        if worker is not None:
            worker.wait()
            worker.deleteLater()
        self._router_worker = None
        for action in self._router_actions:
            action.setEnabled(True)

    def _on_router_status_done(self, result: router_control.RouterResult):
        self._finish_router_worker()
        self._set_router_state(result.route_enabled if result.ok else None)
        self._set_gpt_route(
            result.gpt_route if result.ok else None,
            result.gpt_effective_route if result.ok else None,
        )
        self._set_subagent_route(
            result.subagent_route if result.ok else None,
            result.subagent_effective_route if result.ok else None,
        )
        self._set_pi_route(result.pi_route if result.ok else None)
        if self._queued_router_operation is not None:
            operation = self._queued_router_operation
            self._queued_router_operation = None
            self._start_router_operation(operation)

    def _set_router_status(
        self, message: str, ok: bool | None = None, clear_after: int = 0
    ):
        self._router_status_timer.stop()
        self._router_status = str(message).splitlines()[0][:120]
        self._router_status_ok = ok
        if clear_after > 0:
            self._router_status_timer.start(clear_after)
        self.update()

    def _clear_router_status(self):
        if not self._router_status:
            return
        self._router_status = ""
        self._router_status_ok = None
        self.update()

    def _start_router_operation(self, operation: str):
        if self._exit_requested:
            return
        # QAction 点击会自动勾选；只展示已确认成功的实际路由。
        self._set_gpt_route(self._gpt_route, self._effective_gpt_route)
        self._set_subagent_route(self._subagent_route)
        self._set_pi_route(self._pi_route)
        if self._router_worker is not None:
            # 后台轮询不应吞掉菜单点击；检查结束后串行执行这次操作。
            if self._router_worker.operation == "status":
                self._queued_router_operation = operation
                for action in self._router_actions:
                    action.setEnabled(False)
                self._set_router_status("等待路由状态检查完成...")
                return
            self._tray_icon.showMessage(
                "Codex Router",
                "已有路由操作正在进行",
                QSystemTrayIcon.MessageIcon.Warning,
                3000,
            )
            return

        labels = {
            "enable": "正在开启路由...",
            "disable": "正在关闭路由...",
            "restart": "正在重启路由器...",
            "route-auto": "正在启用自动路由...",
            "route-wlb": "正在切换到 WLB...",
            "route-supergpt": "正在切换到 SuperGPT...",
            "route-official": "正在切换到 GPT...",
            "subagent-route-wlb": "正在切换子代理到 WLB...",
            "subagent-route-supergpt": "正在切换子代理到 SuperGPT...",
            "subagent-route-official": "正在切换子代理到 GPT...",
            "subagent-route-auto": "正在启用子代理自动路由...",
            "pi-route-wlb": "正在切换 Pi 到 WLB...",
            "pi-route-supergpt": "正在切换 Pi 到 SuperGPT...",
            "pi-route-official": "正在切换 Pi 到 GPT...",
        }
        for action in self._router_actions:
            action.setEnabled(False)
        self._tray_icon.showMessage(
            "Codex Router",
            labels.get(operation, "正在执行路由操作..."),
            QSystemTrayIcon.MessageIcon.Information,
            2000,
        )
        self._set_router_status(
            labels.get(operation, "正在执行路由操作...").rstrip(".")
        )
        self._router_worker = RouterWorker(operation, self)
        self._router_worker.completed.connect(self._on_router_operation_done)
        self._router_worker.start()

    def _on_router_operation_done(self, result: router_control.RouterResult):
        self._finish_router_worker()
        if result.route_enabled is not None:
            self._set_router_state(result.route_enabled)
        elif not result.ok:
            self._set_router_state(None)
        self._set_gpt_route(
            result.gpt_route if result.ok else None,
            result.gpt_effective_route if result.ok else None,
        )
        self._set_subagent_route(
            result.subagent_route if result.ok else None,
            result.subagent_effective_route if result.ok else None,
        )
        self._set_pi_route(result.pi_route if result.ok else None)

        summary = result.message or ("路由操作成功" if result.ok else "路由操作失败")
        self._set_router_status(summary, result.ok, clear_after=5000)

        message = result.message
        if result.detail:
            message = f"{message}\n{result.detail}"
        icon = (
            QSystemTrayIcon.MessageIcon.Information
            if result.ok
            else QSystemTrayIcon.MessageIcon.Warning
        )
        self._tray_icon.showMessage("Codex Router", message, icon, 5000)

    # ── 显式代码更新（检查并更新代码） ─────────────────────────────
    def _check_code_updates(self):
        if self._exit_requested:
            return
        if self._code_sync_worker is not None:
            self._tray_icon.showMessage(
                "代码更新",
                "已有代码更新任务正在进行",
                QSystemTrayIcon.MessageIcon.Warning,
                3000,
            )
            return
        if self._code_update_action is not None:
            self._code_update_action.setEnabled(False)
        self._set_router_status("正在检查代码更新...")
        self._code_sync_worker = CodeSyncWorker("check", self)
        self._code_sync_worker.completed.connect(self._on_code_check_done)
        self._code_sync_worker.start()

    def _finish_code_sync_worker(self):
        worker = self._code_sync_worker
        if worker is not None:
            worker.wait()
            worker.deleteLater()
        self._code_sync_worker = None
        if self._code_update_action is not None:
            self._code_update_action.setEnabled(True)

    def _on_code_check_done(self, check):
        self._finish_code_sync_worker()
        if not check.result.ok:
            message = check.result.message or "代码更新检查失败"
            self._set_router_status(message, False, clear_after=5000)
            self._tray_icon.showMessage(
                "代码更新", message, QSystemTrayIcon.MessageIcon.Warning, 5000
            )
            return
        if not check.has_updates:
            self._set_router_status(check.result.message, True, clear_after=5000)
            return
        if not self._confirm_code_update(check):
            self._set_router_status("已取消代码更新", None, clear_after=5000)
            return
        self._set_router_status("正在快进更新代码...")
        self._code_sync_worker = CodeSyncWorker(
            "apply", self, expected_remote=check.remote_commit
        )
        self._code_sync_worker.completed.connect(self._on_code_update_done)
        self._code_sync_worker.start()

    def _confirm_code_update(self, check) -> bool:
        """快进前展示变更范围并要求确认；默认不更新。"""
        box = QMessageBox(
            QMessageBox.Icon.Question,
            "检查并更新代码",
            f"{check.result.message}。\n"
            f"更新范围：{check.scope}。\n"
            "更新只改变磁盘源码，不会重启正在运行的 Router；\n"
            "Router 核心变更需在「路由控制 → 重启路由器」后生效。\n"
            "确认快进更新？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            self,
        )
        box.setDefaultButton(QMessageBox.StandardButton.No)
        return box.exec() == QMessageBox.StandardButton.Yes

    def _on_code_update_done(self, result):
        self._finish_code_sync_worker()
        message = result.message or ("代码已更新" if result.ok else "代码更新失败")
        self._set_router_status(message, result.ok, clear_after=5000)
        text = f"{message}\n{result.detail}" if result.detail else message
        icon = (
            QSystemTrayIcon.MessageIcon.Information
            if result.ok
            else QSystemTrayIcon.MessageIcon.Warning
        )
        self._tray_icon.showMessage("代码更新", text, icon, 6000)

    def show_startup_sync_result(self, result):
        """启动器执行过同步时展示结果；跳过/无变更不打扰用户。"""
        if result.status not in (
            code_sync.SyncStatus.SUCCESS,
            code_sync.SyncStatus.FAILED,
        ):
            return
        message = result.message or ("代码已更新" if result.ok else "代码更新失败")
        text = f"{message}\n{result.detail}" if result.detail else message
        icon = (
            QSystemTrayIcon.MessageIcon.Information
            if result.ok
            else QSystemTrayIcon.MessageIcon.Warning
        )
        self._tray_icon.showMessage("代码更新", text, icon, 6000)
        self._set_router_status(message, result.ok, clear_after=8000)

    # ── Painting ────────────────────────────────────────────────
    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        # Background
        p.setBrush(QBrush(BG_COLOR))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawRoundedRect(0, 0, self.width(), self.height(), 12, 12)

        # Title + display mode switch icon
        display_mode = self.cfg.get("display_mode", MIMO_MODE)
        font_title = QFont("Microsoft YaHei", 10, QFont.Weight.Bold)
        p.setFont(font_title)
        title_text = "总览" if display_mode == OVERVIEW_MODE else ("MiMo Token" if display_mode == MIMO_MODE else "WLB")
        p.setPen(QPen(
            ACCENT_GREEN if display_mode == THIRD_PARTY_MODE and self._effective_gpt_route == "wlb"
            else TEXT_COLOR
        ))
        p.drawText(16, 22, title_text)

        # Mode switch icon right after title
        fm = p.fontMetrics()
        title_w = int(fm.horizontalAdvance(title_text))
        switch_x = 16 + title_w + 4
        switch_y = 22 - int(fm.ascent())
        switch_h = int(fm.height())
        self._switch_rect = QRect(int(switch_x), int(switch_y), 18, switch_h)
        self._draw_switch_icon(p, self._switch_rect)

        # Balance on the right (MiMo mode only)
        if display_mode == MIMO_MODE and self._balance is not None and self._balance != 0:
            p.setPen(QPen(ACCENT_GREEN))
            p.drawText(150, 22, _fmt_money(self._balance))

        # 置顶按钮
        self._pin_btn_rect = self._draw_pin_button(p)

        # 刷新按钮（从浏览器导入）
        self._refresh_btn_rect = self._draw_refresh_button(p)

        # 最小化按钮（右上角 ─ 符号）
        self._minimize_btn_rect = self._draw_minimize_button(p)

        # Content area
        font_small = QFont("Microsoft YaHei", 9)
        p.setFont(font_small)
        p.setPen(QPen(TEXT_COLOR))

        if display_mode == OVERVIEW_MODE:
            self._paint_overview(p)
        elif display_mode == THIRD_PARTY_MODE:
            self._paint_third_party(p)
        elif self._plan_total > 0:
            pct = self._plan_used / self._plan_total
            pct_text = self._format_overview_percent(pct * 100, True)

            p.drawText(16, 42, "Token Plan")
            p.drawText(200, 42, pct_text)

            self._draw_usage_progress_bar(p, pct)

            p.setPen(QPen(TEXT_COLOR))
            p.drawText(16, 80, f"{_fmt_tokens(self._plan_used)} / {_fmt_tokens(self._plan_total)}")
            remaining = self._plan_total - self._plan_used
            p.drawText(100, 66, 144, 16,
                       Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                       f"剩余: {_fmt_tokens(max(0, remaining))}")

            plan_name, price, cost_per_credit = _get_plan_tier_info(self._plan_total)
            if cost_per_credit:
                used_cost = self._plan_used * cost_per_credit
                p.drawText(16, 82, 228, 16,
                           Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                           f"{plan_name} 套餐 ¥{used_cost:.2f} / ¥{price}")

            # Display daily usage (show even if 0)
            if self._plan_total > 0:  # Only show when we have plan data
                p.setPen(QPen(ACCENT_GREEN))
                daily_text = _fmt_tokens(self._daily_used) if self._daily_used > 0 else "0"

                # Calculate daily cost
                _, _, cost_per_credit = _get_plan_tier_info(self._plan_total)
                if cost_per_credit and self._daily_used > 0:
                    daily_cost = self._daily_used * cost_per_credit
                    p.drawText(16, 98, 228, 16,
                               Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                               f"今日已用: {daily_text} / ¥{daily_cost:.2f}")
                else:
                    p.drawText(16, 98, 228, 16,
                               Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                               f"今日已用: {daily_text}")

            expiry_date = _format_expiry(self.cfg.get("expiry_date", ""))
            expiry_days_left = _expiry_days_left(self.cfg.get("expiry_date", ""))
            alert_enabled = self.cfg.get("expiry_alert_enabled", True)
            expiry_color = (
                ACCENT_RED
                if alert_enabled and expiry_days_left is not None and expiry_days_left <= 3
                else DIM
            )
            p.setPen(QPen(expiry_color))
            p.drawText(16, 124, f"有效期至 {expiry_date}")

        elif self._payg_tokens > 0 or self._payg_total_cost:
            # Pay-as-you-go display
            p.drawText(16, 42, "按量付费")
            p.setPen(QPen(TEXT_COLOR))
            if self._payg_tokens > 0:
                p.drawText(16, 62, f"总用量: {_fmt_tokens(self._payg_tokens)}")
                p.drawText(16, 78, f"输入: {_fmt_tokens(self._payg_input)}  输出: {_fmt_tokens(self._payg_output)}")
            if self._payg_total_cost:
                p.drawText(16, 96, f"总费用: ¥{self._payg_total_cost}")
            if self._payg_month_cost:
                p.setPen(QPen(DIM))
                p.drawText(140, 96, f"本月: ¥{self._payg_month_cost}")

        elif self._balance is not None and self._balance != 0:
            p.drawText(16, 50, f"余额: {_fmt_money(self._balance)}")
            p.setPen(QPen(DIM))
            p.drawText(16, 70, "暂无用量数据")
        else:
            p.setPen(QPen(DIM))
            p.drawText(16, 50, "等待数据...")

        # Update time / error (bottom right)
        status_y = 136 if display_mode == OVERVIEW_MODE else 134
        self._paint_refresh_status(p, status_y)

        p.end()

    def _paint_refresh_status(self, p: QPainter, status_y: int):
        """Paint a persistent update time plus an optional elided error."""
        font_tiny = QFont("Microsoft YaHei", 7)
        p.setFont(font_tiny)
        update_text = f"检查于 {self._last_update}"
        update_x = max(16, self.width() - 16 - p.fontMetrics().horizontalAdvance(update_text))
        status_text = self._router_status or self._last_error
        if status_text:
            status_color = (
                ACCENT_GREEN
                if self._router_status and self._router_status_ok is True
                else ACCENT_RED
                if self._router_status and self._router_status_ok is False
                else ACCENT_YELLOW
                if self._router_status
                else ACCENT_RED
            )
            p.setPen(QPen(status_color))
            error_width = max(0, update_x - 22)
            error_text = p.fontMetrics().elidedText(
                status_text,
                Qt.TextElideMode.ElideRight,
                error_width,
            )
            p.drawText(16, status_y, error_text)
        p.setPen(QPen(DIM))
        p.drawText(update_x, status_y, update_text)

    def _draw_usage_progress_bar(self, p: QPainter, used_fraction: float | None):
        """按剩余量绘制详情进度条，输入仍为已用比例；未知数据留空。"""
        bar_x, bar_y, bar_w, bar_h = 16, 50, 228, 14
        percent = self._normalize_overview_percent(
            float(used_fraction) * 100 if used_fraction is not None else None
        )
        remaining_fraction = 0.0 if percent is None else (100 - percent) / 100.0

        p.setBrush(QBrush(BAR_BG))
        p.drawRoundedRect(bar_x, bar_y, bar_w, bar_h, 4, 4)

        fill_w = int(bar_w * remaining_fraction)
        if fill_w > 0:
            # 当填充宽度较小时，限制圆角半径，避免超出外框圆角范围。
            fill_radius = min(4, fill_w // 2)
            p.setBrush(QBrush(_bar_color(remaining_fraction)))

            # 将填充裁剪到外框路径内，确保小比例填充也不会露出外框圆角。
            p.save()
            bar_path = QPainterPath()
            bar_path.addRoundedRect(QRectF(bar_x, bar_y, bar_w, bar_h), 4, 4)
            p.setClipPath(bar_path)
            p.drawRoundedRect(bar_x, bar_y, fill_w, bar_h, fill_radius, fill_radius)
            p.restore()

    def _draw_minimize_button(self, p: QPainter):
        """绘制右上角最小化按钮，返回按钮区域 QRect。"""
        btn_size = 20
        btn_margin = 8
        btn_x = self.width() - btn_size - btn_margin
        btn_y = btn_margin
        btn_rect = QRect(btn_x, btn_y, btn_size, btn_size)

        # 按钮背景（悬停时高亮）
        mouse_pos = self.mapFromGlobal(self.cursor().pos())
        hovered = btn_rect.contains(mouse_pos)
        p.setBrush(QBrush(QColor(255, 255, 255, 40) if hovered else QColor(255, 255, 255, 20)))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawRoundedRect(btn_rect, 4, 4)

        # 绘制 ─ 符号
        p.setPen(QPen(QColor(190, 190, 190), 2))
        line_y = btn_y + btn_size // 2
        p.drawLine(btn_x + 6, line_y, btn_x + btn_size - 6, line_y)

        return btn_rect

    def _draw_refresh_button(self, p: QPainter):
        """绘制从浏览器导入按钮（刷新图标），返回按钮区域 QRect。"""
        btn_size = 20
        btn_margin = 8
        gap = 4  # 与最小化按钮的间距
        min_btn_x = self.width() - btn_size - btn_margin
        btn_x = min_btn_x - btn_size - gap
        btn_y = btn_margin
        btn_rect = QRect(btn_x, btn_y, btn_size, btn_size)

        # 按钮背景（悬停时高亮）
        mouse_pos = self.mapFromGlobal(self.cursor().pos())
        hovered = btn_rect.contains(mouse_pos)
        p.setBrush(QBrush(QColor(255, 255, 255, 50) if hovered else QColor(255, 255, 255, 20)))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawRoundedRect(btn_rect, 4, 4)

        # 绘制下载图标：向下箭头 + 底座横线
        cx = int(btn_x + btn_size / 2)
        cy = int(btn_y + btn_size / 2)
        icon_color = QColor(190, 190, 190)

        p.setPen(QPen(icon_color, 2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        # 箭头竖线
        p.drawLine(cx, cy - 4, cx, cy + 2)
        # 箭头两边
        p.drawLine(cx, cy + 2, cx - 3, cy - 1)
        p.drawLine(cx, cy + 2, cx + 3, cy - 1)
        # 底座横线
        p.drawLine(cx - 4, cy + 4, cx + 4, cy + 4)

        return btn_rect

    def _set_always_on_top(self, enabled: bool):
        """Toggle WindowStaysOnTopHint while preserving window position."""
        if self._exit_requested:
            return
        self._always_on_top = enabled
        self.cfg["always_on_top"] = enabled
        save_config(self.cfg)
        pos = self.pos()
        flags = Qt.WindowType.FramelessWindowHint | Qt.WindowType.Tool
        if enabled:
            flags |= Qt.WindowType.WindowStaysOnTopHint
        self.setWindowFlags(flags)
        self.move(pos)
        self.show()
        if enabled:
            self.raise_()
        self.update()

    def _draw_pin_button(self, p: QPainter):
        """Draw pin/unpin toggle button; returns button QRect."""
        btn_size = 20
        btn_margin = 8
        gap = 4  # spacing between buttons
        # pin is leftmost: to the left of the refresh button
        refresh_btn_x = self.width() - btn_size - btn_margin - btn_size - gap
        btn_x = refresh_btn_x - btn_size - gap
        btn_y = btn_margin
        btn_rect = QRect(btn_x, btn_y, btn_size, btn_size)

        # Hover background (same style as minimize/refresh buttons)
        mouse_pos = self.mapFromGlobal(self.cursor().pos())
        hovered = btn_rect.contains(mouse_pos)
        p.setBrush(QBrush(QColor(255, 255, 255, 40) if hovered else QColor(255, 255, 255, 20)))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawRoundedRect(btn_rect, 4, 4)

        cx = int(btn_x + btn_size / 2)
        cy = int(btn_y + btn_size / 2)

        if self._always_on_top:
            pin_color = QColor(100, 200, 255)  # bright accent when pinned
        else:
            pin_color = QColor(120, 120, 120)  # dim when unpinned

        p.setPen(QPen(pin_color, 2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        # Pin: circle (pin head)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawEllipse(int(cx - 3), int(cy - 4), 6, 6)
        # Pin: vertical shaft
        p.drawLine(int(cx), int(cy + 2), int(cx), int(cy + 5))
        # Pin: base line
        p.drawLine(int(cx - 4), int(cy + 5), int(cx + 4), int(cy + 5))

        # Unpinned: diagonal slash overlay (red)
        if not self._always_on_top:
            p.setPen(QPen(QColor(244, 67, 54), 2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            p.drawLine(int(btn_x + 4), int(btn_y + btn_size - 4),
                       int(btn_x + btn_size - 4), int(btn_y + 4))

        return btn_rect

    def _draw_switch_icon(self, p: QPainter, rect: QRect):
        """Draw a compact two-way circular-arrow display mode switch icon."""
        mouse_pos = self.mapFromGlobal(self.cursor().pos())
        hovered = rect.contains(mouse_pos)
        color = QColor(200, 200, 200) if hovered else DIM

        side = min(11, int(rect.width()) - 6, int(rect.height()) - 6)
        icon_rect = QRect(
            int(rect.center().x() - side // 2),
            int(rect.center().y() - side // 2),
            int(side),
            int(side),
        )
        cx = int(icon_rect.center().x())
        cy = int(icon_rect.center().y())

        p.save()
        p.setPen(QPen(color, 2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawArc(icon_rect, 35 * 16, 135 * 16)
        p.drawArc(icon_rect, 215 * 16, 135 * 16)

        # Arrowheads point into the two gaps, making the cycle direction clear.
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(color))
        p.drawPolygon(QPolygonF([
            QPointF(cx + 3, cy - 4),
            QPointF(cx + 6, cy - 3),
            QPointF(cx + 4, cy - 1),
        ]))
        p.drawPolygon(QPolygonF([
            QPointF(cx - 3, cy + 4),
            QPointF(cx - 6, cy + 3),
            QPointF(cx - 4, cy + 1),
        ]))
        p.restore()

    def _toggle_display_mode(self):
        """Cycle between MiMo Token, API Usage, and overview display modes."""
        current_mode = self.cfg.get("display_mode", MIMO_MODE)
        if current_mode == MIMO_MODE:
            next_mode = THIRD_PARTY_MODE
        elif current_mode == THIRD_PARTY_MODE:
            next_mode = OVERVIEW_MODE
        else:
            next_mode = MIMO_MODE
        self._set_display_mode(next_mode)

    def _set_display_mode(self, mode: str):
        """Switch display mode and refresh."""
        if self._exit_requested:
            return
        self.cfg["display_mode"] = mode
        save_config(self.cfg)
        self._refresh_error_state()
        self._apply_size_for_mode(mode)
        self._do_fetch()
        self.update()

    def _paint_overview(self, p: QPainter):
        """Paint the overview page listing all available data sources."""
        overview_font = QFont("Microsoft YaHei", 9)
        p.setFont(overview_font)
        has_cookie = bool(self.cfg.get("cookie", "").strip())
        has_api_key = self._tp_key_configured
        has_gpt = bool(self.cfg.get("gpt_session_cookie", "").strip()) or (isinstance(self._gpt_data, dict) and self._gpt_data.get("used_percent") is not None)

        rows: list[tuple[str, bool, object]] = [
            (
                "Mimo",
                has_cookie,
                (self._plan_used / self._plan_total * 100)
                if has_cookie and self._plan_total > 0
                else None,
            ),
            (
                "WLB",
                has_api_key,
                _window_used_percent(self._tp_data) if has_api_key else None,
            ),
            (
                "GPT \u5468\u9650\u989d",
                has_gpt,
                self._gpt_data.get("used_percent")
                if has_gpt and self._gpt_data is not None
                else None,
            ),
        ]

        for idx, (name, configured, raw_percent) in enumerate(rows):
            cell_x, label_y, bar_y, bar_w, bar_h = self._overview_row_metrics(idx)

            label_rect = QRect(cell_x, label_y - 14, bar_w, 18)
            if idx in (1, 2):
                if idx == 1:
                    weekly = (
                        self._tp_data
                        if configured and isinstance(self._tp_data, dict)
                        else None
                    )
                    primary = weekly.get("daily") if isinstance(weekly, dict) else None
                    secondary = weekly
                    primary_reset_at, primary_reset_after = _reset_parts(primary)
                    primary_reset_text = _format_overview_reset_time(
                        primary_reset_at, primary_reset_after
                    )
                    primary_title = f"WLB - {primary_reset_text}"
                    primary_center_text = None
                else:
                    primary = self._gpt_data.get("primary") if isinstance(self._gpt_data, dict) else None
                    secondary = self._gpt_data.get("secondary") if isinstance(self._gpt_data, dict) else None
                    secondary = secondary or (self._gpt_data if isinstance(self._gpt_data, dict) else None)
                    primary_reset_at, primary_reset_after = _reset_parts(primary)
                    primary_reset_text = _format_overview_reset_time(
                        primary_reset_at, primary_reset_after
                    )
                    primary_title = f"GPT - {primary_reset_text}"
                    primary_center_text = None
                secondary_reset_at, secondary_reset_after = _reset_parts(secondary)
                secondary_title = _format_overview_reset_days(
                    secondary_reset_at, secondary_reset_after
                )
                primary_text = self._format_overview_percent(
                    _window_used_percent(primary),
                    configured,
                )
                secondary_text = self._format_overview_percent(
                    _window_used_percent(secondary),
                    configured,
                )
                active_route = "wlb" if idx == 1 else "official"
                p.setPen(QPen(ACCENT_GREEN if self._effective_gpt_route == active_route else TEXT_COLOR))
                segment_width = (bar_w - DUAL_BAR_GAP) // 2
                labels = (
                    (cell_x, primary_title, primary_center_text, primary_text),
                    (
                        cell_x + segment_width + DUAL_BAR_GAP,
                        secondary_title,
                        None,
                        secondary_text,
                    ),
                )
                for segment_x, title, center_text, percent_text in labels:
                    label_font = QFont(overview_font)
                    metrics = QFontMetrics(label_font)
                    label_gap = 4
                    # Reserve the widest percentage so digit changes do not move GPT's reset time.
                    reserved_percent_width = metrics.horizontalAdvance("100.0%")
                    content_width = (
                        sum(
                            metrics.horizontalAdvance(text)
                            for text in (title, center_text)
                            if text
                        )
                        + reserved_percent_width
                        + label_gap
                    )
                    if content_width > segment_width:
                        label_font.setLetterSpacing(
                            QFont.SpacingType.PercentageSpacing,
                            max(50, int(segment_width / content_width * 100) - 5),
                        )
                        metrics = QFontMetrics(label_font)
                    p.setFont(label_font)
                    segment_rect = QRect(segment_x, label_rect.y(), segment_width, label_rect.height())
                    percent_width = metrics.horizontalAdvance(percent_text)
                    title_width = max(0, segment_width - percent_width - label_gap)
                    p.drawText(
                        QRect(segment_rect.x(), segment_rect.y(), title_width, segment_rect.height()),
                        Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                        metrics.elidedText(title, Qt.TextElideMode.ElideRight, title_width),
                    )
                    if center_text:
                        p.drawText(
                            segment_rect,
                            Qt.AlignmentFlag.AlignCenter | Qt.AlignmentFlag.AlignVCenter,
                            center_text,
                        )
                    p.drawText(
                        segment_rect,
                        Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                        percent_text,
                    )
                self._draw_dual_usage_bar(p, cell_x, bar_y, bar_w, bar_h, primary, secondary)
                continue

            percent_text = self._format_overview_percent(raw_percent, configured)
            if idx == 0:
                self._paint_supergpt_balance(p, cell_x + bar_w + DUAL_BAR_GAP, label_y, bar_y, bar_w, bar_h)
                suffix = _format_overview_expiry(self.cfg.get("expiry_date", ""))
                title_text = f"{name} {suffix}" if suffix else name
                metrics = QFontMetrics(overview_font)
                title_width = max(
                    0, label_rect.width() - metrics.horizontalAdvance(percent_text) - 4
                )
                title_rect = QRect(
                    label_rect.x(), label_rect.y(), title_width, label_rect.height()
                )
                p.setPen(QPen(TEXT_COLOR))
                p.setFont(overview_font)
                p.drawText(
                    title_rect,
                    Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                    metrics.elidedText(title_text, Qt.TextElideMode.ElideRight, title_width),
                )

                p.setPen(QPen(TEXT_COLOR))
                p.setFont(overview_font)
                p.drawText(
                    label_rect,
                    Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                    percent_text,
                )
            p.setBrush(QBrush(BAR_BG))
            p.drawRoundedRect(cell_x, bar_y, bar_w, bar_h, 4, 4)

            percent = self._normalize_overview_percent(raw_percent)
            fraction = 0.0 if percent is None else (100 - percent) / 100.0
            fill_w = int(bar_w * fraction)
            if fill_w > 0:
                fill_radius = min(4, fill_w // 2)
                p.setBrush(QBrush(_bar_color(fraction)))
                p.save()
                bar_path = QPainterPath()
                bar_path.addRoundedRect(QRectF(cell_x, bar_y, bar_w, bar_h), 4, 4)
                p.setClipPath(bar_path)
                p.drawRoundedRect(cell_x, bar_y, fill_w, bar_h, fill_radius, fill_radius)
                p.restore()

    def _supergpt_balance_text(self):
        if self._supergpt_balance is None:
            return "--"
        symbol = "¥" if self._supergpt_balance["currency"] == "CNY" else "$"
        return f"{symbol}{self._supergpt_balance['balance']:.2f}"

    def _supergpt_balance_layers(self):
        """每 100 原币种为一层；整百余额停留在当前满层。"""
        if self._supergpt_balance is None:
            return 0, 0.0
        balance = self._supergpt_balance["balance"]
        if not math.isfinite(balance) or balance <= 0:
            return 0, 0.0
        complete, remainder = divmod(balance, 100)
        return int(complete) + int(remainder > 0), remainder / 100 if remainder else 1.0

    def _paint_supergpt_balance(self, p, x, label_y, bar_y, width, height):
        text = self._supergpt_balance_text()
        title = "API余额"
        font = QFont("Microsoft YaHei", 9)
        while font.pointSize() > 7 and QFontMetrics(font).horizontalAdvance(title + text) + 4 > width:
            font.setPointSize(font.pointSize() - 1)
        metrics = QFontMetrics(font)
        label_width = metrics.horizontalAdvance(title)
        value_x = x + label_width + 4
        value_width = max(0, width - label_width - 4)
        p.setFont(font)
        p.setPen(QPen(ACCENT_GREEN if self._effective_gpt_route == "supergpt" else TEXT_COLOR))
        p.drawText(QRect(x, label_y - 14, label_width, 18), Qt.AlignmentFlag.AlignVCenter, title)
        p.setPen(QPen(DIM if self._supergpt_balance is None else TEXT_COLOR))
        p.drawText(QRect(value_x, label_y - 14, value_width, 18),
                   Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                   metrics.elidedText(text, Qt.TextElideMode.ElideRight, value_width))
        layers, fraction = self._supergpt_balance_layers()
        background = BAR_BG if layers <= 1 else BALANCE_LAYER_COLORS[(layers - 2) % len(BALANCE_LAYER_COLORS)]
        color = _bar_color(fraction) if layers <= 1 else BALANCE_LAYER_COLORS[(layers - 1) % len(BALANCE_LAYER_COLORS)]
        self._draw_overview_bar(p, x, bar_y, width, height, fraction, color, background)
        if layers > 1:
            p.save()
            marker_font = QFont("Microsoft YaHei", 7, QFont.Weight.Bold)
            p.setFont(marker_font)
            p.setPen(QPen(QColor(20, 20, 20)))
            marker_rect = QRect(x + 3, bar_y, max(0, width - 6), height)
            p.drawText(marker_rect, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                       QFontMetrics(marker_font).elidedText(
                           f"×{layers}", Qt.TextElideMode.ElideRight, marker_rect.width()))
            p.restore()

    def _draw_overview_bar(self, p, x, y, width, height, fraction, color, background=BAR_BG):
        p.setBrush(QBrush(background))
        p.setPen(QPen(TEXT_COLOR))
        p.drawRoundedRect(x, y, width, height, 4, 4)
        if fraction <= 0:
            return
        fill_width = max(1, int(width * fraction))
        fill_radius = min(4, fill_width // 2)
        p.setBrush(QBrush(color))
        p.save()
        path = QPainterPath()
        path.addRoundedRect(QRectF(x, y, width, height), 4, 4)
        p.setClipPath(path)
        p.drawRoundedRect(x, y, fill_width, height, fill_radius, fill_radius)
        p.restore()

    def _draw_dual_usage_bar(self, p: QPainter, x: int, y: int, width: int, height: int,
                             primary: dict | None, secondary: dict | None):
        """按剩余量绘制两个独立窗口，缺失窗口留空。"""
        gap = DUAL_BAR_GAP
        segment_width = (width - gap) // 2
        for segment_x, window in (
            (x, primary),
            (x + segment_width + gap, secondary),
        ):
            percent = self._normalize_overview_percent(_window_used_percent(window))
            fraction = 0.0 if percent is None else (100 - percent) / 100.0
            self._draw_overview_bar(p, segment_x, y, segment_width, height,
                                    fraction, _bar_color(fraction))

    def _paint_third_party(self, p: QPainter):
        """Paint third-party usage data in the content area."""
        d = self._tp_data or {}
        has_data = self._tp_data is not None
        is_valid = d.get("is_valid", False)
        used_pct = self._normalize_overview_percent(_window_used_percent(d))

        # Remaining percentage prominently
        p.setPen(QPen(ACCENT_GREEN if has_data and is_valid else DIM))
        font_accent = QFont("Microsoft YaHei", 10, QFont.Weight.Bold)
        p.setFont(font_accent)
        remaining_text = f"剩余 {100 - used_pct:.2f}%" if used_pct is not None else "剩余 --"
        p.drawText(16, 42, remaining_text)

        # 进度条始终可见，按剩余比例填充，未知数据留空。
        font_small = QFont("Microsoft YaHei", 9)
        p.setFont(font_small)
        used_frac = used_pct / 100.0 if used_pct is not None else None
        p.setPen(QPen(TEXT_COLOR))
        self._draw_usage_progress_bar(p, used_frac)

        # Details line
        p.setPen(QPen(TEXT_COLOR))
        if has_data:
            used_text = f"{used_pct:.2f}%" if used_pct is not None else "--"
            details = f"已用 {used_text} | {d.get('window', '7d')} | {d.get('total_percent', 100)}%"
        else:
            details = "等待 API 数据 | 7d | 100%"
        p.drawText(16, 80, details)

        # Status
        status_text = "Active" if is_valid else "Inactive" if has_data else "等待"
        p.setPen(QPen(ACCENT_GREEN if is_valid else ACCENT_RED if has_data else DIM))
        p.drawText(16, 98, f"状态: {status_text}")

        reset_text = _format_reset_datetime(*_reset_parts(d)) if has_data else ""
        if reset_text:
            p.setPen(QPen(DIM))
            reset_line = f"7日重置: {reset_text}"
            reset_line = p.fontMetrics().elidedText(
                reset_line,
                Qt.TextElideMode.ElideRight,
                228,
            )
            p.drawText(
                QRect(16, 104, 228, 16),
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                reset_line,
            )

    def _confirm_base_url_first_use(
        self, label: str, base_url: str, default_base: str
    ) -> bool:
        """旧配置中的非默认域名首次使用前必须确认；确认前不发送共享 Key。

        已确认主机记入 ``approved_base_url_hosts`` 持久化；本会话内拒绝过的
        主机不再重复询问，但依旧不发送密钥。无效 URL 直接放行给请求层，
        由其在发送密钥前拒绝。
        """
        try:
            normalized = usage_url.validate_base_url(
                base_url, default_base=default_base, label=label
            )
        except usage_url.UsageUrlError:
            return True
        host = usage_url.endpoint_host(normalized)
        if not host or host == usage_url.endpoint_host(default_base):
            return True
        approved = set(self.cfg.get("approved_base_url_hosts") or [])
        if host in approved:
            return True
        if host in self._base_url_declined:
            return False
        answer = QMessageBox.question(
            self,
            f"{label} Base URL 首次使用确认",
            f"{label} Base URL 使用非默认域名 {host}（可能来自旧配置）。\n"
            "确认前不会向该主机发送共享 API Key。\n"
            "是否信任该地址并继续？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            self._base_url_declined.add(host)
            return False
        approved.add(host)
        self.cfg["approved_base_url_hosts"] = sorted(approved)
        save_config(self.cfg)
        return True

    def _do_fetch_supergpt_balance(self):
        if self._exit_requested or self.cfg.get("display_mode", MIMO_MODE) != OVERVIEW_MODE:
            return
        if self._supergpt_worker is not None:
            return
        if not self._confirm_base_url_first_use(
            "SuperGPT",
            self.cfg.get("supergpt_base_url", ""),
            supergpt_balance.DEFAULT_BASE_URL,
        ):
            self._on_supergpt_balance_done({
                "ok": False,
                "balance": None,
                "currency": None,
                "error": "SuperGPT Base URL 非默认域名未确认，已跳过查询（未发送共享 Key）",
            })
            return
        self._supergpt_worker = SuperGPTBalanceWorker(
            self,
            base_url=self.cfg.get("supergpt_base_url", ""),
        )
        self._supergpt_worker.result_ready.connect(self._on_supergpt_balance_done)
        self._supergpt_worker.finished.connect(self._finish_supergpt_worker)
        self._supergpt_worker.start()

    def _finish_supergpt_worker(self):
        worker = self._supergpt_worker
        self._supergpt_worker = None
        if worker is not None:
            worker.wait()
            worker.deleteLater()

    def _on_supergpt_balance_done(self, result: dict):
        if self._exit_requested:
            return
        if result.get("ok") and result.get("balance") is not None:
            self._supergpt_balance = result
            self._supergpt_error = ""
            self._supergpt_last_update = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        else:
            self._supergpt_error = result.get("error") or "余额查询失败"
        self._refresh_error_state()
        self._apply_overview_tooltip_if_needed()
        self.update()

    def _do_fetch_third_party(self):
        """Dispatch third-party usage fetch."""
        if self._exit_requested:
            return
        worker = getattr(self, "_tp_worker", None)
        if worker is not None and worker.isRunning():
            return
        base_url = self.cfg.get("third_party_base_url", "https://codex.wlbclub.com")
        if not self._confirm_base_url_first_use(
            "WLB", base_url, api_client.DEFAULT_THIRD_PARTY_BASE_URL
        ):
            self._on_third_party_fetch_done({
                "ok": False,
                "error": "WLB Base URL 非默认域名未确认，已跳过查询（未发送共享 Key）",
            })
            return
        self._tp_worker = ThirdPartyFetchWorker(base_url)
        self._tp_worker.finished.connect(self._on_third_party_fetch_done)
        self._tp_worker.start()

    def _on_third_party_fetch_done(self, result: dict):
        """Handle third-party usage fetch result."""
        if self._exit_requested:
            return
        if result.get("key_configured") is not None:
            self._tp_key_configured = result["key_configured"]
        if result.get("ok"):
            self._tp_key_configured = True
            self._tp_data = result.get("data")
            self._tp_error = ""
        else:
            self._tp_error = result.get("error", "第三方用量查询失败")
        self._refresh_error_state()
        self._last_update = datetime.now().strftime("%H:%M:%S")
        self._apply_overview_tooltip_if_needed()
        self.update()

    # ── Mouse events ────────────────────────────────────────────
    def _do_fetch_gpt(self):
        """Dispatch GPT weekly usage fetch."""
        if self._exit_requested:
            return
        if hasattr(self, "_gpt_worker") and self._gpt_worker.isRunning():
            return
        session_cookie = self.cfg.get("gpt_session_cookie", "")
        self._gpt_worker = GPTFetchWorker(session_cookie)
        self._gpt_worker.finished.connect(self._on_gpt_fetch_done)
        self._gpt_worker.start()

    def _on_gpt_fetch_done(self, result: dict):
        """Handle GPT weekly usage fetch result."""
        if self._exit_requested:
            return
        if result.get("ok"):
            data = result.get("data")
            if isinstance(data, dict) and data.get("source") == "local_jsonl":
                observed_at = _parse_reset_datetime(data.get("observed_at"))
                if observed_at is not None and (
                    self._gpt_data_time is None or observed_at > self._gpt_data_time
                ):
                    self._gpt_data = data
                    self._gpt_data_time = observed_at
                details = "；".join(str(error) for error in data.get("source_errors", []) if error)
                self._gpt_error = "实时查询失败，数据已过期，显示本地历史数据或上次数据"
                if observed_at is None:
                    self._gpt_error += "；本地历史数据时间无效"
                if details:
                    self._gpt_error += f"：{details}"
            else:
                self._gpt_data = data
                self._gpt_data_time = datetime.now()
                self._gpt_error = ""
        else:
            detail = result.get("error", "GPT \u7528\u91cf\u67e5\u8be2\u5931\u8d25")
            self._gpt_error = (
                f"刷新失败，继续显示上次数据：{detail}"
                if self._gpt_data is not None
                else detail
            )
        self._refresh_error_state()
        self._last_update = datetime.now().strftime("%H:%M:%S")
        self._apply_overview_tooltip_if_needed()
        self.update()

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self._drag_pos = None
            # 检测是否点击显示模式切换图标
            if hasattr(self, '_switch_rect') and self._switch_rect.contains(e.pos()):
                self._toggle_display_mode()
                e.accept()
                return
            # 检测是否点击置顶按钮
            if hasattr(self, '_pin_btn_rect') and self._pin_btn_rect.contains(e.pos()):
                self._set_always_on_top(not self._always_on_top)
                e.accept()
                return
            # 检测是否点击刷新按钮（从浏览器导入）
            if hasattr(self, '_refresh_btn_rect') and self._refresh_btn_rect.contains(e.pos()):
                self._import_cookie_quick()
                e.accept()
                return
            # 检测是否点击最小化按钮
            if hasattr(self, '_minimize_btn_rect') and self._minimize_btn_rect.contains(e.pos()):
                self.close()
                e.accept()
                return
            self._drag_pos = e.globalPosition().toPoint() - self.frameGeometry().topLeft()
            e.accept()

    def mouseMoveEvent(self, e):
        if self._drag_pos is not None and e.buttons() & Qt.MouseButton.LeftButton:
            new_pos = e.globalPosition().toPoint() - self._drag_pos

            screen = QApplication.screenAt(e.globalPosition().toPoint())
            if screen:
                geo = screen.availableGeometry()
                w, h = self.width(), self.height()
                x, y = new_pos.x(), new_pos.y()

                geo_right = geo.x() + geo.width()
                geo_bottom = geo.y() + geo.height()
                if x < geo.x() + SNAP_THRESHOLD:
                    x = geo.x()
                elif x + w > geo_right - SNAP_THRESHOLD:
                    x = geo_right - w

                if y < geo.y() + SNAP_THRESHOLD:
                    y = geo.y()
                elif y + h > geo_bottom - SNAP_THRESHOLD:
                    y = geo_bottom - h

                new_pos = QPoint(x, y)

            # Inter-window snap (ETF Tracker / other MiMo instance)
            # Win32 GetWindowRect returns physical pixel coordinates.
            # On dpr>1 monitors these differ from Qt logical coordinates,
            # so we convert to physical space before snapping, then back.
            try:
                own_hwnd = int(self.winId())
                rects = window_snap.get_other_window_rects(own_hwnd)
                if rects:
                    # Determine the screen where the window currently is
                    snap_screen = QApplication.screenAt(new_pos) or screen
                    if snap_screen:
                        scr_geo = snap_screen.geometry()
                        scr_origin = (scr_geo.x(), scr_geo.y())
                        dpr = window_snap.normalize_dpr(snap_screen.devicePixelRatio())
                    else:
                        scr_origin = (0, 0)
                        dpr = 1.0

                    # Convert own position/size to physical pixels
                    phys_pos = window_snap.qt_to_physical_position(
                        (new_pos.x(), new_pos.y()), scr_origin, dpr,
                    )
                    phys_size = window_snap.qt_to_physical_size(
                        (self.width(), self.height()), dpr,
                    )
                    # Threshold in physical pixels
                    phys_threshold = max(1, round(SNAP_THRESHOLD * dpr))

                    # Snap in physical space (rects are already physical)
                    phys_snapped = window_snap.snap_position(
                        phys_pos, phys_size, rects, phys_threshold,
                    )

                    # Convert back to Qt logical coordinates
                    qt_pos = window_snap.physical_to_qt_position(
                        phys_snapped, scr_origin, dpr,
                    )
                    new_pos = QPoint(qt_pos[0], qt_pos[1])
            except Exception:
                pass

            self.move(new_pos)
            e.accept()
        else:
            # 鼠标悬停时更新按钮高亮状态，并切换光标
            pos = e.position().toPoint()
            on_btn = False
            if hasattr(self, '_minimize_btn_rect') and self._minimize_btn_rect.contains(pos):
                on_btn = True
            if hasattr(self, '_refresh_btn_rect') and self._refresh_btn_rect.contains(pos):
                on_btn = True
            if hasattr(self, '_pin_btn_rect') and self._pin_btn_rect.contains(pos):
                on_btn = True
            if hasattr(self, '_switch_rect') and self._switch_rect.contains(pos):
                on_btn = True
            new_cursor = Qt.CursorShape.PointingHandCursor if on_btn else Qt.CursorShape.ArrowCursor
            if self.cursor().shape() != new_cursor:
                self.setCursor(QCursor(new_cursor))
                self.update()

    def mouseReleaseEvent(self, e):
        if self._exit_requested:
            return
        if e.button() == Qt.MouseButton.LeftButton and self._drag_pos is not None:
            self._drag_pos = None
            self.cfg["position"] = [self.x(), self.y()]
            save_config(self.cfg)

    def mouseDoubleClickEvent(self, e):
        self._do_fetch()

    def closeEvent(self, event):
        """拦截关闭事件，最小化到系统托盘而非退出。"""
        event.ignore()
        self.hide()
        self._tray_icon.showMessage(
            "MiMo Token Monitor",
            "程序已最小化到系统托盘，双击图标可恢复窗口。",
            QSystemTrayIcon.MessageIcon.Information,
            2000
        )

    def _on_tray_activated(self, reason):
        """处理托盘图标点击事件。"""
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self._show_window()

    def _show_window(self):
        """显示并激活窗口。"""
        self.showNormal()
        self.activateWindow()
        self.raise_()

    def _quit_app(self, _checked=False, *, restart=False):
        """真正退出应用程序；退出请求发出后忽略重复请求。"""
        if self._exit_requested:
            return
        if any(dialog._keys_worker is not None for dialog in self.findChildren(SettingsDialog)):
            self._tray_icon.showMessage(
                "共享密钥", "密钥保存完成后再退出",
                QSystemTrayIcon.MessageIcon.Warning, 3000,
            )
            return
        if self._router_worker is not None and self._router_worker.isRunning():
            self._tray_icon.showMessage(
                "Codex Router",
                "路由操作完成后再退出",
                QSystemTrayIcon.MessageIcon.Warning,
                3000,
            )
            return
        self._restart_requested = restart
        self._exit_requested = True
        self.setEnabled(False)
        self._timer.stop()
        self._playwright_timer.stop()
        self._router_poll_timer.stop()
        if self._supergpt_worker is not None:
            self._supergpt_worker.requestInterruption()
            self._supergpt_worker.wait()
        if self._playwright_worker is not None and self._playwright_worker.isRunning():
            self._playwright_worker.wait(5000)
        if self._exit_callback is not None:
            self._exit_callback()
        else:
            self.finish_quit()

    def finish_quit(self):
        self._tray_icon.hide()
        QApplication.quit()

    def contextMenuEvent(self, e):
        menu = QMenu(self)

        refresh_act = QAction("刷新", self)
        refresh_act.triggered.connect(self._do_fetch)
        menu.addAction(refresh_act)

        import_act = QAction("从浏览器导入", self)
        import_act.triggered.connect(self._import_cookie_quick)
        menu.addAction(import_act)

        renew_act = QAction("Playwright 续期", self)
        renew_act.triggered.connect(
            lambda _checked=False: self._refresh_playwright_cookie(interactive=True)
        )
        menu.addAction(renew_act)

        settings_act = QAction("设置", self)
        settings_act.triggered.connect(self._open_settings)
        menu.addAction(settings_act)

        debug_act = QAction("查看原始数据", self)
        debug_act.triggered.connect(self._show_debug)
        menu.addAction(debug_act)

        menu.addSeparator()

        quit_act = QAction("退出", self)
        quit_act.triggered.connect(self._quit_app)
        menu.addAction(quit_act)

        menu.exec(e.globalPos())

    # ── Import ──────────────────────────────────────────────────
    def _import_cookie_quick(self):
        """从浏览器快速导入 Cookie，自动保存并刷新数据。"""
        if self._exit_requested:
            return
        cookie_str, error = cookie_reader.import_cookie_from_browser()
        if not cookie_str:
            QMessageBox.warning(self, "导入失败", error or "无法从浏览器读取 Cookie")
            return

        # 验证 cookie 是否有效
        result = api_client.fetch_balance(cookie_str)
        if result["ok"]:
            # 保存到配置并刷新
            self.cfg["cookie"] = cookie_str
            self._playwright_recovery_attempted = False
            save_config(self.cfg)
            QMessageBox.information(self, "导入成功", "Cookie 已从浏览器读取并验证有效，正在刷新数据...")
            self._do_fetch()
        elif "过期" in (result.get("error") or ""):
            QMessageBox.warning(
                self, "Cookie 已过期",
                "浏览器中的 Cookie 也已过期，请先在浏览器中重新登录\n"
                "platform.xiaomimimo.com，然后重试",
            )
        else:
            # 网络错误等：仍然保存，让用户自行判断
            self.cfg["cookie"] = cookie_str
            save_config(self.cfg)
            QMessageBox.warning(
                self, "导入成功但验证失败",
                f"Cookie 已读取，但验证时出错：{result.get('error', '未知错误')}\n已保存，请手动确认",
            )
            self._do_fetch()

    def _refresh_playwright_cookie(
        self,
        interactive: bool = False,
        require_cookie_change: bool = False,
    ):
        """Refresh the isolated Playwright login and replace the saved Cookie."""
        if self._exit_requested:
            return
        if not interactive and not self.cfg.get("playwright_auto_refresh", True):
            return
        if self._playwright_worker is not None and self._playwright_worker.isRunning():
            if interactive:
                message = "已有 Playwright 续期任务正在进行，请稍候"
                self._set_router_status(message, ok=False, clear_after=4000)
                self._tray_icon.showMessage(
                    "Playwright 续期",
                    message,
                    QSystemTrayIcon.MessageIcon.Warning,
                    3000,
                )
            return

        previous_cookie = self.cfg.get("cookie", "") if require_cookie_change else None
        if interactive:
            status = "正在打开登录浏览器，请在窗口中完成登录..."
            self._set_router_status(status)
            self._tray_icon.showMessage(
                "Playwright 续期",
                "已打开浏览器，请完成 MiMo 登录或验证码；窗口会保持到登录成功或超时",
                QSystemTrayIcon.MessageIcon.Information,
                5000,
            )
        else:
            self._set_router_status("正在自动续期 Cookie...")
        self._playwright_worker = PlaywrightCookieWorker(
            interactive,
            previous_cookie,
            self,
        )
        self._playwright_worker.result_ready.connect(self._on_playwright_cookie_done)
        self._playwright_worker.start()

    def _current_cookie_still_works(self) -> bool:
        """Best-effort check whether the saved Cookie can still call MiMo."""
        cookie = (self.cfg.get("cookie") or "").strip()
        if not cookie:
            return False
        try:
            result = api_client.fetch_balance(cookie)
        except Exception:
            return False
        return bool(result.get("ok"))

    def _on_playwright_cookie_done(self, cookie, error, interactive: bool):
        worker = self._playwright_worker
        self._playwright_worker = None
        if worker is not None:
            worker.deleteLater()

        if cookie:
            self.cfg["cookie"] = cookie
            self._playwright_recovery_attempted = False
            save_config(self.cfg)
            self._mimo_error = ""
            self._set_router_status("Cookie 续期成功", ok=True, clear_after=5000)
            self._tray_icon.showMessage(
                "Playwright 续期",
                "Cookie 已更新，正在刷新用量",
                QSystemTrayIcon.MessageIcon.Information,
                3000,
            )
            self._do_fetch()
            return

        message = error or "无法刷新登录状态；如需登录或验证码验证，请从菜单手动续期。"
        first_line = message.splitlines()[0][:80]
        cookie_ok = self._current_cookie_still_works()

        # Playwright 独立会话没登录 ≠ 配置里的 Cookie 失效。
        # 后者仍可用时，自动续期失败只给状态/托盘提示，不弹阻塞对话框。
        if cookie_ok and not interactive:
            status = "Playwright 未登录，当前 Cookie 仍可用"
            self._set_router_status(status, ok=True, clear_after=10000)
            self._tray_icon.showMessage(
                "Playwright 续期",
                status + "。如需自动续期，请手动执行一次「Playwright 续期」并完成登录。",
                QSystemTrayIcon.MessageIcon.Information,
                6000,
            )
            self.update()
            return

        if cookie_ok:
            first_line = "Playwright 登录未完成，当前 Cookie 仍可用"
            message = (
                f"{message}\n\n"
                "当前配置的 Cookie 仍然有效，用量可继续刷新。\n"
                "若要启用 Playwright 自动续期，请在打开的浏览器中完成登录。"
            )

        # 先把状态画在悬浮窗上，并且在对话框关闭前不清除
        #（否则用户看弹窗超过 clear_after 时，关掉后悬浮窗已经没字了）。
        self._set_router_status(first_line, ok=False)
        self._tray_icon.showMessage(
            "Playwright 续期失败",
            message.splitlines()[0][:120],
            QSystemTrayIcon.MessageIcon.Warning,
            6000,
        )
        box = QMessageBox(
            QMessageBox.Icon.Warning,
            "Playwright 续期失败",
            message,
            QMessageBox.StandardButton.Ok,
            self,
        )
        box.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
        box.exec()
        self._set_router_status(first_line, ok=False, clear_after=12000)
        self.update()

    # ── Fetch ───────────────────────────────────────────────────
    def _do_fetch(self):
        if self._exit_requested:
            return
        self._last_update = datetime.now().strftime("%H:%M:%S")
        self.update()
        display_mode = self.cfg.get("display_mode", "mimo")
        if display_mode == OVERVIEW_MODE:
            self._do_fetch_mimo()
            self._do_fetch_third_party()
            self._do_fetch_supergpt_balance()
        elif display_mode == THIRD_PARTY_MODE:
            self._do_fetch_third_party()
        else:
            self._do_fetch_mimo()
        self._do_fetch_gpt()

    def _do_fetch_mimo(self):
        """Dispatch MiMo plan/balance fetch."""
        if self._exit_requested:
            return
        if hasattr(self, "_worker") and self._worker.isRunning():
            return

        cookie = self.cfg.get("cookie", "")
        if not cookie:
            self._mimo_error = "请先在设置中填入 Cookie"
            self._refresh_error_state()
            self._apply_overview_tooltip_if_needed()
            self.update()
            return

        self._worker = FetchWorker(cookie)
        self._worker.finished.connect(self._on_fetch_done)
        self._worker.start()

    def _on_fetch_done(self, bal_result: dict, usage_result: dict):
        if self._exit_requested:
            return
        if not bal_result["ok"]:
            self._mimo_error = bal_result.get("error", "余额查询失败")
        elif not usage_result["ok"]:
            self._mimo_error = usage_result.get("error", "用量查询失败")
        else:
            self._mimo_error = ""
        expired = any(
            "过期" in str(result.get("error") or "")
            for result in (bal_result, usage_result)
            if isinstance(result, dict)
        )
        if (
            expired
            and self.cfg.get("playwright_auto_refresh", True)
            and not self._playwright_recovery_attempted
        ):
            self._playwright_recovery_attempted = True
            self._refresh_playwright_cookie(
                interactive=False,
                require_cookie_change=True,
            )
        self._refresh_error_state()

        # Parse balance
        if bal_result["ok"] and bal_result["balance"] is not None:
            self._balance = float(bal_result["balance"])

        # Parse usage / plan info
        if usage_result["ok"] and usage_result["data"]:
            self._parse_plan(usage_result["data"])
            # Update daily baseline after parsing month usage
            self._update_daily_baseline()

        self._last_update = datetime.now().strftime("%H:%M:%S")
        self._apply_overview_tooltip_if_needed()
        self._write_snapshot()
        self.update()

    def _apply_overview_tooltip_if_needed(self):
        """Build tooltip for the current display mode."""
        display_mode = self.cfg.get("display_mode", MIMO_MODE)
        if display_mode == MIMO_MODE:
            lines = self._build_mimo_tooltip_lines()
        elif display_mode == THIRD_PARTY_MODE:
            lines = self._build_third_party_tooltip_lines()
        else:
            lines = self._build_overview_tooltip_lines()
        if display_mode == OVERVIEW_MODE:
            if self._mimo_error:
                lines.append(
                    "Token Plan 错误: "
                    f"{self._error_display_detail('MiMo', self._mimo_error)}"
                )
            if self._tp_error:
                lines.append(
                    f"WLB 错误: {self._error_display_detail('WLB', self._tp_error)}"
                )
            if self._gpt_error:
                lines.append(
                    "GPT \u5468\u9650\u989d \u9519\u8bef: "
                    f"{self._error_display_detail('GPT', self._gpt_error)}"
                )
        elif self._last_error:
            lines.append(f"错误: {self._last_error}")
        if self._router_status:
            lines.append(f"状态: {self._router_status}")
        tooltip_text = "\n".join(lines)
        self.setToolTip(tooltip_text)
        if hasattr(self, '_tray_icon'):
            self._tray_icon.setToolTip(tooltip_text[:128] if len(tooltip_text) > 128 else tooltip_text)

    def _parse_plan(self, data):
        """Parse usage info from API response.

        Supports two formats:
        1. Token Plan: {"data": {"usage": {"items": [{"name": "plan_total_token", "used": X, "limit": Y}]}, "monthUsage": {...}}}
        2. Pay-as-you-go: {"data": {"tokenUsage": {"totalToken": X}, "costUsage": {"totalCost": "0.00", "currentMonthCost": "0.00"}}}
        """
        try:
            root = data.get("data", data) if isinstance(data, dict) else data
            if not isinstance(root, dict):
                return

            # Format 1: Token Plan
            usage = root.get("usage")
            if usage and isinstance(usage, dict) and usage.get("items"):
                for item in usage["items"]:
                    if item.get("name") == "plan_total_token":
                        self._plan_used = int(item.get("used", 0))
                        self._plan_total = int(item.get("limit", 0))
                        break

            month = root.get("monthUsage")
            if month and isinstance(month, dict) and month.get("items"):
                mi = month["items"][0]
                self._month_used = int(mi.get("used", 0))
                self._month_limit = int(mi.get("limit", 0))

            # Format 2: Pay-as-you-go token usage
            token_usage = root.get("tokenUsage")
            if token_usage and isinstance(token_usage, dict):
                total = token_usage.get("totalToken")
                if total is not None:
                    self._payg_tokens = int(total)
                inp = token_usage.get("inputToken", 0)
                out = token_usage.get("outputToken", 0)
                self._payg_input = int(inp or 0)
                self._payg_output = int(out or 0)

            cost = root.get("costUsage")
            if cost and isinstance(cost, dict):
                self._payg_total_cost = cost.get("totalCost")
                self._payg_month_cost = cost.get("currentMonthCost")

        except Exception:
            pass

    def _update_daily_baseline(self):
        """Update daily usage baseline and calculate today's usage.

        Logic:
        - Check if today is a new day
        - If new day, save current month_usage as baseline
        - Calculate today's usage = current month_usage - baseline
        """
        today = datetime.now().strftime("%Y-%m-%d")
        baseline_date = self.cfg.get("daily_baseline_date", "")
        baseline_usage = self.cfg.get("daily_baseline_usage", 0)

        # Check if it's a new day or baseline is not set
        if baseline_date != today:
            # New day: save current month_used as baseline
            self.cfg["daily_baseline_date"] = today
            self.cfg["daily_baseline_usage"] = self._month_used
            save_config(self.cfg)
            baseline_usage = self._month_used

        # Calculate today's usage
        # Handle case where month_used might reset (new billing cycle)
        if self._month_used >= baseline_usage:
            self._daily_used = self._month_used - baseline_usage
        else:
            # Month reset (new billing cycle), reset baseline
            self.cfg["daily_baseline_usage"] = self._month_used
            save_config(self.cfg)
            self._daily_used = 0
    # ── Snapshot ─────────────────────────────────────────────────
    def _write_snapshot(self):
        """Write usage snapshot for claude-hud to read."""
        snapshot_path = self.cfg.get("snapshot_path", "")
        if not snapshot_path:
            return

        snapshot_writer.write_snapshot(
            snapshot_path=snapshot_path,
            balance=self._balance,
            plan_used=self._plan_used,
            plan_total=self._plan_total,
            month_used=self._month_used,
            month_limit=self._month_limit,
            daily_used=self._daily_used,
            expiry_date=self.cfg.get("expiry_date", ""),
            error=self._mimo_error if self._mimo_error else None,
        )

    # ── Actions ─────────────────────────────────────────────────
    def _open_settings(self):
        if self._exit_requested:
            return
        dlg = SettingsDialog(self.cfg, self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            if self._exit_requested:
                return
            self.cfg = dlg.get_config()
            save_config(self.cfg)
            self.setWindowOpacity(self.cfg.get("opacity", 0.85))
            self.update()
            interval_ms = self.cfg.get("refresh_interval", 300) * 1000
            self._timer.setInterval(interval_ms)
            self._playwright_timer.stop()
            if self.cfg.get("playwright_auto_refresh", True):
                self._playwright_timer.start(
                    max(3600, int(self.cfg.get("playwright_refresh_interval", 21600))) * 1000
                )
            self._do_fetch()

    def _show_debug(self):
        """Show raw API response for debugging."""
        cookie = self.cfg.get("cookie", "")
        if not cookie:
            QMessageBox.information(self, "调试", "请先配置 Cookie")
            return
        bal = api_client.fetch_balance(cookie)
        usage = api_client.fetch_usage(cookie)
        usage_url = usage.get("url", "未找到")
        text = f"=== 余额 ===\n{json.dumps(bal, indent=2, ensure_ascii=False)}\n\n=== 用量 (端点: {usage_url}) ===\n{json.dumps(usage, indent=2, ensure_ascii=False)}"
        QMessageBox.information(self, "原始 API 数据", text[:2000])
