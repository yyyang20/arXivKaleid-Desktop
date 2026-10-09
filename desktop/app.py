# Copyright (c) 2026 yyyang20
# SPDX-License-Identifier: GPL-3.0-only
# See LICENSE in the project root for the full license text.

from __future__ import annotations

import sys
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from pathlib import Path

from PySide6.QtCore import QEvent, QThread, Signal, Slot, QUrl
from PySide6.QtGui import QDesktopServices, QFont, QIcon
from PySide6.QtWidgets import (
    QApplication, QHBoxLayout, QMessageBox, QStackedWidget, QVBoxLayout, QWidget,
)
from qfluentwidgets import (
    FluentIcon, NavigationInterface, SubtitleLabel, Theme, setTheme, setThemeColor,
)

from desktop import __version__
from desktop import paths, pipeline
from desktop.pipeline import (
    CandidateError, CandidateFetchResult, CandidateSnapshot, fetch_candidates_for_date,
)
from desktop.secrets import SecretError, SecretStore
from desktop.analysis import AnalysisAttempt, AnalysisError, AnalysisResult
from desktop.diagnostics import DesktopDiagnostics
from desktop.errors import DesktopIssue, DesktopOutcome, make_issue
from desktop.progress import ProgressEvent
from desktop.pages import HomePage, HistoryPage, SettingsPage
from desktop.style import PAGE_BACKGROUND
from desktop.task_panel import ANALYSIS_STAGES, STAGE_SYMBOLS
from desktop.history import HistoryError, HistoryStore
from desktop.prompt_page import PromptPage
from desktop.research_requirements import RequirementsError, RequirementsStore


ANALYSIS_NOTICE = (
    "开始分析会将论文标题、摘要、通过门控后的 PDF 提取全文和已保存的完整研究提示词"
    " 发送到你自己的 DeepSeek API，并可能产生费用。\n\n"
    "API Key 仅在本机使用 Windows DPAPI 加密保存；PDF、SQLite 和缓存保存在本机。"
    "应用没有维护者服务器中转或遥测。\n\n"
    "是否同意并继续？详情见随附 PRIVACY.md。"
)
SOURCE_ROOT = Path(__file__).resolve().parents[1]


def load_application_icon() -> QIcon:
    """源码和冻结模式都只从受控只读资源根加载正式 ICO。"""
    icon_path = paths.checked_path(
        paths.resource_root(SOURCE_ROOT), "assets", "app-icon.ico",
    )
    if not icon_path.is_file():
        raise ValueError("Desktop 应用图标缺失。")
    icon = QIcon(str(icon_path))
    if icon.isNull():
        raise ValueError("Desktop 应用图标无效。")
    return icon

class FetchWorker(QThread):
    """工作线程只计算结果；finished 连接的窗口槽在 GUI 线程执行。"""

    progress = Signal(object)

    def __init__(
        self, target_date: date, diagnostics: DesktopDiagnostics | None = None,
        operation_id: str | None = None, parent=None,
    ):
        super().__init__(parent)
        self.target_date = target_date
        self.snapshot: CandidateSnapshot | None = None
        self.outcome: DesktopOutcome | None = None
        self.issue: DesktopIssue | None = None
        self.diagnostics = diagnostics
        self.operation_id = operation_id

    def run(self) -> None:
        try:
            if self.diagnostics is None and self.operation_id is None:
                result = fetch_candidates_for_date(self.target_date, self.progress.emit)
            else:
                result = fetch_candidates_for_date(
                    self.target_date, self.progress.emit,
                    diagnostics=self.diagnostics, operation_id=self.operation_id,
                )
            if isinstance(result, CandidateSnapshot):
                self.snapshot = result
            else:
                self.snapshot = result.snapshot
                self.outcome = result.outcome
        except CandidateError as exc:
            self.issue = exc.issue
        except Exception:
            self.issue = make_issue("AKD-FETCH-UNEXPECTED")


class AnalysisWorker(QThread):
    progress = Signal(object)

    def __init__(self, attempt: AnalysisAttempt, api_key: str, parent=None, history_store=None):
        super().__init__(parent)
        self.attempt = attempt
        self._api_key = api_key
        self.result: AnalysisResult | None = None
        self.issue: DesktopIssue | None = None
        self.history_store = history_store if history_store is not None else HistoryStore()
        self.history_issue: DesktopIssue | None = None
        self.history_saved = False

    def run(self) -> None:
        try:
            self.result = self.attempt.run(self._api_key, self.progress.emit)
        except AnalysisError as exc:
            self.issue = exc.issue
        except Exception:
            self.issue = make_issue("AKD-PREPARE-WORKSPACE_FAILED")
        finally:
            self._api_key = ""
        if self.result is not None:
            # 分析已经成功返回；保存异常不进入 AnalysisAttempt 的整批失败边界。
            try:
                self.history_store.save(self.result)
                self.history_saved = True
            except HistoryError as exc:
                self.history_issue = exc.issue
            except Exception:
                self.history_issue = make_issue("AKD-HISTORY-SAVE_FAILED")


class HistoryWorker(QThread):
    """每个短任务独立连接 SQLite；不访问控件或持有长期后台循环。"""

    def __init__(self, store, action, argument, generation, parent=None):
        super().__init__(parent)
        self.store, self.action, self.argument = store, action, argument
        self.generation = generation
        self.result = None
        self.issue = None

    def run(self):
        try:
            if self.action == "list":
                self.result = self.store.list_records(self.argument)
            elif self.action == "read":
                self.result = self.store.read(self.argument)
            elif self.action == "delete":
                self.result = self.store.delete(self.argument)
        except HistoryError as exc:
            self.issue = exc.issue
        except Exception:
            self.issue = make_issue("AKD-HISTORY-DELETE_FAILED" if self.action == "delete" else "AKD-HISTORY-READ_FAILED")


class DesktopWindow(QWidget):
    def __init__(
        self,
        secret_store: SecretStore | None = None,
        diagnostics: DesktopDiagnostics | None = None,
        window_icon: QIcon | None = None,
        history_store: HistoryStore | None = None,
        requirements_store: RequirementsStore | None = None,
    ):
        super().__init__()
        self.secret_store = secret_store if secret_store is not None else SecretStore()
        self.diagnostics = diagnostics
        self.history_store = history_store if history_store is not None else HistoryStore()
        self.requirements_store = requirements_store if requirements_store is not None else RequirementsStore()
        self.history_worker: HistoryWorker | None = None
        self._history_jobs = []
        self._history_generation = 0
        self.snapshot: CandidateSnapshot | None = None
        self.worker: FetchWorker | AnalysisWorker | None = None
        self.analysis_notice_accepted = False
        # None 表示随北京时间变化的“今天”，历史日期才固定保存。
        self.selected_date: date | None = None
        self._display_target: date | None = None
        # 不使用 Fluent 的默认配置持久化，避免向 cwd 写入额外设置文件。
        setTheme(Theme.LIGHT, save=False)
        setThemeColor("#1677ff", save=False)
        self.setFont(QFont("Microsoft YaHei UI", 10))
        self.setWindowTitle(f"arXivKaleid Desktop v{__version__}")
        self.setWindowIcon(window_icon if window_icon is not None else load_application_icon())
        self.resize(1060, 820)
        self.setMinimumSize(850, 680)
        self.setObjectName("desktopWindow")
        self.setStyleSheet(f"QWidget#desktopWindow {{background: {PAGE_BACKGROUND};}}")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 12, 0, 0)
        self.version_header = SubtitleLabel(f"arXivKaleid   v{__version__}")
        self.version_header.setContentsMargins(20, 0, 20, 0)
        layout.addWidget(self.version_header)
        row = QHBoxLayout()
        row.setSpacing(0)
        self.navigation = NavigationInterface(
            self, showMenuButton=False, showReturnButton=False, collapsible=False,
        )
        self.navigation.setExpandWidth(154)
        self.navigation.setMinimumExpandWidth(0)
        self.navigation.setAcrylicEnabled(False)
        self.navigation.setIndicatorAnimationEnabled(False)
        self.pages = QStackedWidget()
        self.home_page = HomePage()
        self.prompt_page = PromptPage(self.requirements_store)
        self.history_page = HistoryPage()
        from desktop.user_guide import UserGuidePage
        self.user_guide_page = UserGuidePage(pipeline.PROJECT_ROOT)
        self.settings_page = SettingsPage()
        for page, icon, title in (
            (self.home_page, FluentIcon.HOME, "首页"),
            (self.prompt_page, FluentIcon.DOCUMENT, "提示词"),
            (self.history_page, FluentIcon.HISTORY, "历史"),
            (self.user_guide_page, self.user_guide_page.navigation_icon, "使用说明"),
            (self.settings_page, FluentIcon.SETTING, "设置"),
        ):
            self.pages.addWidget(page)
            self.navigation.addItem(
                page.objectName(), icon, title,
                onClick=lambda _triggered, p=page: self.switch_page(p),
            )
        row.addWidget(self.navigation)
        row.addWidget(self.pages, 1)
        layout.addLayout(row, 1)
        self.navigation.expand(useAni=False)
        self.switch_page(self.home_page)
        # 保留稳定的窗口控件入口，供现有回归测试及 portable 自检使用。
        self.api_key = self.settings_page.api_key
        self.secret_status = self.settings_page.secret_status
        self.fetch_button = self.home_page.fetch_button
        self.calendar_button = self.home_page.calendar_button
        self.date_picker = self.home_page.date_picker
        self.analyze_button = self.home_page.analyze_button
        self.statistics = self.home_page.statistics
        self.task_panel = self.home_page.task_panel
        for name in (
            "status", "fetch_progress", "fetch_progress_text", "analysis_steps",
            "analysis_progress_text", "diagnostic_text", "open_logs_button",
        ):
            setattr(self, name, getattr(self.task_panel, name))
        self.open_logs_button.setEnabled(
            bool(self.diagnostics and self.diagnostics.persistent)
        )
        self.open_logs_button.clicked.connect(self.open_log_directory)
        self.reset_analysis_progress()
        self.report = self.home_page.report
        self.report.anchorClicked.connect(self.open_report_link)
        self.history_page.report.anchorClicked.connect(self.open_report_link)
        self.user_guide_page.browser.anchorClicked.connect(self.open_guide_link)
        self.history_page.open_requested.connect(self.open_history_record)
        self.history_page.delete_requested.connect(self.delete_history_record)
        self.history_page.more_requested.connect(self.load_more_history)
        self.history_page.back_requested.connect(self.back_to_history)
        try:
            self.api_key.setText(self.secret_store.load())
        except SecretError:
            self.secret_status.setText("无法恢复 API Key，请重新输入；仍可获取候选。")
            issue = make_issue("AKD-KEY-LOAD_FAILED")
            self.show_issue(issue)
            if self.diagnostics:
                self.diagnostics.issue(
                    operation_id=self.diagnostics.operation_id(),
                    operation_type="api_key", issue=issue,
                )
        self.api_key.editingFinished.connect(self.save_key)
        self.fetch_button.clicked.connect(self.start_fetch)
        self.calendar_button.clicked.connect(self.open_date_picker)
        self.date_picker.date_chosen.connect(self.select_date)
        self.analyze_button.clicked.connect(self.start_analysis)
        if self.diagnostics and not self.diagnostics.persistent:
            self.diagnostic_text.setText("诊断仅保留在当前会话；本地日志不可写。")

    def switch_page(self, page: QWidget) -> None:
        if page is not self.pages.currentWidget() and not self.prompt_page.protect_unsaved():
            self.navigation.setCurrentItem(self.pages.currentWidget().objectName())
            return
        self.pages.setCurrentWidget(page)
        self.navigation.setCurrentItem(page.objectName())
        if page is self.history_page:
            self.refresh_history()
        else:
            self._history_generation += 1

    def refresh_history(self):
        self._history_generation += 1
        # 旧读请求可以丢弃，已经确认的删除仍要完成。
        self._history_jobs = [job for job in self._history_jobs if job[0] == "delete"]
        self.history_page.show_list()
        self.history_page.model.reset()
        self.history_page.message.setText("正在读取历史…")
        self.queue_history("list", None)

    def queue_history(self, action, argument):
        self._history_jobs.append((action, argument, self._history_generation))
        self.dispatch_history()

    def dispatch_history(self):
        if self.history_worker is not None or not self._history_jobs:
            return
        action, argument, generation = self._history_jobs.pop(0)
        self.history_worker = HistoryWorker(self.history_store, action, argument, generation, self)
        self.history_worker.finished.connect(self.finish_history)
        self.history_worker.start()

    @Slot()
    def load_more_history(self):
        cursor = self.history_page.model.cursor
        if cursor is not None:
            self.queue_history("list", cursor)

    @Slot(str)
    def open_history_record(self, record_id):
        self._history_generation += 1
        self.history_page.begin_detail(record_id)
        self.queue_history("read", record_id)

    @Slot()
    def back_to_history(self):
        self.refresh_history()

    def confirm_history_delete(self, record_id):
        row = next((row for row in self.history_page.model.rows if row.record_id == record_id), None)
        if row is None:
            return False
        answer = QMessageBox.question(
            self, "删除历史记录", f"删除 {row.time_text} 北京时间生成的日报？\n删除后无法恢复。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return answer == QMessageBox.StandardButton.Yes

    @Slot(str)
    def delete_history_record(self, record_id):
        if self.confirm_history_delete(record_id):
            self.history_page.message.setText("正在删除历史记录…")
            self.queue_history("delete", record_id)

    @Slot()
    def finish_history(self):
        worker = self.history_worker
        if worker is None:
            return
        if worker.issue is not None and self.diagnostics:
            self.diagnostics.issue(operation_id=self.diagnostics.operation_id(), operation_type="history", issue=worker.issue)
        if worker.generation == self._history_generation and self.pages.currentWidget() is self.history_page:
            page = self.history_page
            if worker.issue is not None:
                message = f"{worker.issue.reason}\n{worker.issue.impact}"
                (page.detail_message if worker.action == "read" else page.message).setText(message)
                if worker.action == "list":
                    page.model.loading = False
                    page.model.has_more = False
            elif worker.action == "list":
                page.model.append(worker.result)
                page.message.setText("" if page.model.rows else "暂无历史记录；成功生成的日报将自动保存。")
            elif worker.action == "read" and page.current_record_id == worker.argument:
                if worker.result is None:
                    page.detail_message.setText("该历史记录已不存在，请返回历史列表。")
                else:
                    try:
                        page.show_record(worker.result)
                    except Exception:
                        page.report.clear()
                        page.detail_message.setText("日报展示失败，保存的原始内容未被修改。")
                        if self.diagnostics:
                            self.diagnostics.issue(operation_id=self.diagnostics.operation_id(), operation_type="history", issue=make_issue("AKD-DISPLAY-MARKDOWN_RENDER_FAILED"))
            elif worker.action == "delete":
                page.model.remove_record(worker.argument)
                page.message.setText("" if page.model.rows else "暂无历史记录；成功生成的日报将自动保存。")
        self.history_worker = None
        worker.deleteLater()
        self.dispatch_history()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if hasattr(self, "date_picker"):
            self.date_picker.hide()
        if hasattr(self, "task_panel"):
            self.task_panel.fit_to_window()

    def event(self, event):
        if (event.type() == QEvent.Type.WindowActivate and hasattr(self, "calendar_button")
                and self.calendar_button.isEnabled()):
            self.refresh_target_date()
        if event.type() == QEvent.Type.ScreenChangeInternal and hasattr(self, "date_picker"):
            self.date_picker.hide()
        return super().event(event)

    def beijing_today(self) -> date:
        return datetime.now(timezone.utc).astimezone(ZoneInfo("Asia/Shanghai")).date()

    def clear_candidate_display(self):
        self.snapshot = None
        self.report.clear()
        self.analyze_button.setEnabled(False)
        for label in self.statistics.values():
            label.setText("—")
        self.task_panel.begin("fetch")
        self.task_panel.fetch_details.clear()
        self.task_panel.finish()
        self.reset_analysis_progress()
        self.status.setText("可点击日历选择指定抓取日期")
        self.fetch_progress_text.setText("尚未抓取")
        self.fetch_progress.setRange(0, 1)
        self.fetch_progress.setValue(0)

    def refresh_target_date(self) -> date:
        today = self.beijing_today()
        if self.worker is None:
            if self.selected_date is not None and not today - timedelta(days=365) <= self.selected_date <= today:
                self.selected_date = None
            target = self.selected_date or today
            if self._display_target is not None and self._display_target != target:
                self.clear_candidate_display()
            self._display_target = target
            self.fetch_button.setText("抓取今天" if target == today else f"抓取 {target.isoformat()}")
            return target
        return self._display_target or today

    @Slot()
    def open_date_picker(self):
        if self.worker is not None or not self.calendar_button.isEnabled():
            return
        target = self.refresh_target_date()
        self.date_picker.prepare(self.beijing_today(), target)
        self.date_picker.show_at(self.calendar_button)

    @Slot(object)
    def select_date(self, selected):
        if self.worker is not None:
            return
        today = self.beijing_today()
        if not today - timedelta(days=365) <= selected <= today:
            return
        previous = self.selected_date or today
        self.selected_date = None if selected == today else selected
        if selected != previous:
            self.clear_candidate_display()
        self._display_target = selected
        self.refresh_target_date()
        self.date_picker.hide()

    def reset_analysis_progress(self) -> None:
        for stage, title in ANALYSIS_STAGES:
            self.analysis_steps[stage].setText(f"{STAGE_SYMBOLS['pending']} {title}")
            self.analysis_steps[stage].setStyleSheet("color: #778396;")
        self.analysis_progress_text.setText("尚未开始分析")

    def refresh_diagnostic_availability(self) -> None:
        available = bool(self.diagnostics and self.diagnostics.persistent)
        self.open_logs_button.setEnabled(available)
        if self.diagnostics and not available and "当前会话" not in self.diagnostic_text.text():
            self.diagnostic_text.setText(
                self.diagnostic_text.text() + "\n诊断仅保留在当前会话；本地日志不可写。"
            )

    def show_issue(self, issue: DesktopIssue, *, operation_id: str | None = None, run_id: int | None = None) -> None:
        scope = {"system": "系统级", "paper": "单篇局部", "outcome": "正常结果"}.get(issue.scope, issue.scope)
        identity = []
        if operation_id:
            identity.append(f"operation ID：{operation_id}")
        if run_id is not None:
            identity.append(f"run ID：{run_id}")
        if self.diagnostics:
            identity.append(f"session ID：{self.diagnostics.session_id}")
        if self.diagnostics and self.diagnostics.relative_log_path:
            identity.append(f"日志：{self.diagnostics.relative_log_path}")
        elif self.diagnostics:
            identity.append("日志：仅保留在当前会话")
        lines = [
            f"阶段：{issue.stage}　代码：{issue.code}",
            f"范围：{scope}",
            f"原因：{issue.reason}",
            f"影响：{issue.impact}",
            f"建议：{issue.action}",
        ]
        if identity:
            lines.append("　".join(identity))
        self.diagnostic_text.setText("\n".join(lines))
        if issue.stage in self.analysis_steps and self.task_panel.kind == "analysis":
            self.analysis_steps[issue.stage].setText(f"✕ {dict(ANALYSIS_STAGES)[issue.stage]}")
            self.analysis_steps[issue.stage].setStyleSheet("color: #a4262c;")
        if issue.code.startswith("AKD-KEY-"):
            self.status.setText(issue.reason)
        self.task_panel.reveal_issue()

    def show_outcome(self, result: DesktopOutcome, *, operation_id: str | None = None) -> None:
        text = f"阶段：{result.stage}　代码：{result.code}\n范围：正常业务结果\n{result.summary}"
        if operation_id:
            text += f"\noperation ID：{operation_id}"
        if self.diagnostics and self.diagnostics.relative_log_path:
            text += f"\n日志：{self.diagnostics.relative_log_path}"
        self.diagnostic_text.setText(text)

    @Slot(object)
    def handle_progress(self, event: object) -> None:
        """所有控件更新都留在窗口所属的 GUI 线程。"""
        if not isinstance(event, ProgressEvent):
            return
        self.task_panel.observe(event)
        self.refresh_diagnostic_availability()
        if event.task_type == "fetch":
            self.fetch_progress_text.setText(event.message)
            self.status.setText("运行进度：正在获取候选")
            if event.state == "running":
                self.fetch_progress.setRange(0, 0)
            else:
                self.fetch_progress.setRange(0, 1)
                self.fetch_progress.setValue(1 if event.state == "completed" else 0)
            return
        if event.task_type != "analysis":
            return
        self.analysis_progress_text.setText(event.message)
        self.status.setText("运行进度：两轮分析")
        if event.stage in self.analysis_steps and event.state in STAGE_SYMBOLS:
            title = dict(ANALYSIS_STAGES)[event.stage]
            self.analysis_steps[event.stage].setText(
                f"{STAGE_SYMBOLS[event.state]} {title}"
            )

    @Slot()
    def save_key(self) -> bool:
        if not self.api_key.isModified():
            return True
        try:
            self.secret_store.save(self.api_key.text())
        except SecretError:
            self.secret_status.setText("API Key 保存失败，请检查运行目录权限后重试。")
            issue = make_issue("AKD-KEY-SAVE_FAILED")
            self.show_issue(issue)
            if self.diagnostics:
                self.diagnostics.issue(
                    operation_id=self.diagnostics.operation_id(),
                    operation_type="api_key", issue=issue,
                )
            return False
        else:
            self.api_key.setModified(False)
            self.secret_status.setText("API Key 已加密保存。")
            return True

    @Slot()
    def start_fetch(self) -> None:
        if self.worker is not None:
            return
        target_date = self.refresh_target_date()
        self.date_picker.hide()
        # 先使旧快照失效，再启动网络工作；API Key 不参与候选管线。
        self.snapshot = None
        self.report.clear()
        self.switch_page(self.home_page)
        self.task_panel.begin("fetch")
        self.reset_analysis_progress()
        self.analyze_button.setEnabled(False)
        self.fetch_button.setEnabled(False)
        self.calendar_button.setEnabled(False)
        for label in self.statistics.values():
            label.setText("—")
        self.status.setText("正在抓取候选…")
        self.fetch_progress_text.setText(
            f"候选抓取中 · 北京时间 {target_date.isoformat()}"
        )
        self.fetch_progress.setRange(0, 0)
        operation_id = self.diagnostics.operation_id() if self.diagnostics else None
        self.worker = FetchWorker(
            target_date, self.diagnostics, operation_id, self
        )
        self.worker.progress.connect(self.handle_progress)
        self.worker.finished.connect(self.finish_fetch)
        self.worker.start()

    @Slot()
    def finish_fetch(self) -> None:
        worker = self.worker
        if worker is None:
            return
        self.snapshot = worker.snapshot
        if self.snapshot is None:
            if worker.outcome is not None:
                self.status.setText(worker.outcome.summary)
                self.show_outcome(worker.outcome, operation_id=worker.operation_id)
                details = worker.outcome.details
                self.statistics["抓取日期（北京时间）"].setText(details.get("target_date", worker.target_date.isoformat()))
                self.statistics["抓取完成时间"].setText(details.get("completed_time", "—"))
                self.statistics["原始条目数"].setText("0")
                self.statistics["去重后候选数"].setText("0")
                self.task_panel.fetch_details.setText(worker.outcome.summary)
            elif worker.issue is not None:
                self.status.setText(worker.issue.reason)
                self.fetch_progress_text.setText(worker.issue.reason)
                self.show_issue(worker.issue, operation_id=worker.operation_id)
            else:
                self.status.setText("本次无法取得候选。")
            self.fetch_progress.setRange(0, 1)
            self.fetch_progress.setValue(0)
        else:
            snapshot = self.snapshot
            values = (
                snapshot.candidate_date.isoformat(), snapshot.completed_time_text,
                str(snapshot.raw_count), str(snapshot.unique_count),
            )
            for label, value in zip(self.statistics.values(), values):
                label.setText(value)
            self.status.setText(f"候选获取完成 · {snapshot.round1_count} 篇候选")
            self.fetch_progress_text.setText(
                f"原始 {snapshot.raw_count} 条 → 去重后候选 {snapshot.unique_count} 篇"
            )
            self.fetch_progress.setRange(0, 1)
            self.fetch_progress.setValue(1)
            self.task_panel.show_snapshot(snapshot, pipeline.CATEGORIES)
        self.task_panel.finish(failed=worker.issue is not None)
        self.analyze_button.setEnabled(self.snapshot is not None and not self.snapshot.analysis_attempted)
        self.fetch_button.setEnabled(True)
        self.calendar_button.setEnabled(True)
        self.refresh_diagnostic_availability()
        self.worker = None
        worker.deleteLater()

    @Slot()
    def confirm_analysis_notice(self) -> bool:
        if self.analysis_notice_accepted:
            return True
        answer = QMessageBox.question(
            self,
            "分析前告知",
            ANALYSIS_NOTICE,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            self.status.setText("已取消分析；未调用模型。")
            normal = DesktopOutcome(
                code="AKO-ANALYSIS-CANCELLED", stage="prepare",
                summary="用户取消分析；未调用模型。",
            )
            self.show_outcome(normal)
            if self.diagnostics:
                self.diagnostics.outcome(
                    operation_id=self.diagnostics.operation_id(),
                    operation_type="analysis", outcome=normal,
                    snapshot_id=self.snapshot.snapshot_id if self.snapshot else None,
                    fetch_operation_id=(self.snapshot.fetch_operation_id or None) if self.snapshot else None,
                )
            return False
        self.analysis_notice_accepted = True
        return True

    @Slot()
    def start_analysis(self) -> None:
        if self.worker is not None or self.snapshot is None or self.snapshot.analysis_attempted:
            return
        if not self.api_key.text().strip():
            self.secret_status.setText("请输入 API Key 后再开始分析。")
            issue = make_issue("AKD-KEY-EMPTY")
            self.show_issue(issue)
            self.switch_page(self.settings_page)
            if self.diagnostics:
                self.diagnostics.issue(
                    operation_id=self.diagnostics.operation_id(),
                    operation_type="api_key", issue=issue,
                    snapshot_id=self.snapshot.snapshot_id,
                    fetch_operation_id=self.snapshot.fetch_operation_id or None,
                )
            return
        if not self.confirm_analysis_notice():
            return
        if not self.save_key():
            self.switch_page(self.settings_page)
            return
        if not self.prompt_page.protect_unsaved():
            return
        try:
            requirements = self.requirements_store.snapshot()
        except (RequirementsError, RuntimeError, OSError, ValueError):
            self.status.setText("研究提示词不可用，未开始分析。请在提示词页面处理保存内容或检查内置资源。")
            self.prompt_page.refresh_states()
            self.switch_page(self.prompt_page)
            return
        # 在启动线程前消费快照；任何失败都不恢复旧快照的分析资格。
        attempt = AnalysisAttempt(self.snapshot, self.diagnostics, requirements=requirements)
        self.prompt_page.set_busy(True)
        self.switch_page(self.home_page)
        self.task_panel.begin("analysis")
        self.fetch_button.setEnabled(False)
        self.calendar_button.setEnabled(False)
        self.date_picker.hide()
        self.analyze_button.setEnabled(False)
        self.api_key.setEnabled(False)
        self.status.setText("正在准备分析…")
        self.reset_analysis_progress()
        self.analysis_progress_text.setText("正在准备分析…")
        self.worker = AnalysisWorker(attempt, self.api_key.text(), self, self.history_store)
        self.worker.progress.connect(self.handle_progress)
        self.worker.finished.connect(self.finish_analysis)
        self.worker.start()

    @Slot()
    def finish_analysis(self) -> None:
        worker = self.worker
        if not isinstance(worker, AnalysisWorker):
            return
        if worker.result is not None:
            self.task_panel.show_result(worker.result)
            try:
                self.report.setMarkdown(worker.result.markdown)
            except Exception as exc:
                self.report.clear()
                issue = make_issue("AKD-DISPLAY-MARKDOWN_RENDER_FAILED")
                if self.diagnostics:
                    self.diagnostics.event(
                        operation_id=worker.result.operation_id,
                        operation_type="display", stage="display", state="fail",
                        code=issue.code, scope=issue.scope,
                        snapshot_id=worker.result.snapshot_id,
                        fetch_operation_id=worker.result.fetch_operation_id or None,
                        run_id=worker.result.run_id, unexpected=exc,
                    )
                self.status.setText("分析成功、日报展示失败；不会重新调用模型。")
                self.show_issue(
                    issue, operation_id=worker.result.operation_id,
                    run_id=worker.result.run_id,
                )
                self.task_panel.finish(failed=True)
            else:
                self.status.setText(
                    f"分析完成\n最终推荐 {worker.result.recommendation_count} 篇"
                )
                self.analysis_progress_text.setText(self.status.text())
                paper_counts: dict[str, int] = {}
                for issue in worker.result.paper_issues:
                    paper_counts[issue.code] = paper_counts.get(issue.code, 0) + 1
                summary = [
                    f"操作成功　operation ID：{worker.result.operation_id}",
                    f"run ID：{worker.result.run_id}",
                ]
                summary.extend(f"{code}：{count} 篇" for code, count in sorted(paper_counts.items()))
                if self.diagnostics and self.diagnostics.relative_log_path:
                    summary.append(f"日志：{self.diagnostics.relative_log_path}")
                elif self.diagnostics:
                    summary.append("诊断仅保留在当前会话")
                self.diagnostic_text.setText("\n".join(summary))
                self.task_panel.finish()
        else:
            if worker.issue is not None:
                self.status.setText(worker.issue.reason)
                self.analysis_progress_text.setText(worker.issue.reason)
                self.show_issue(
                    worker.issue, operation_id=worker.attempt.operation_id,
                    run_id=worker.attempt.run_id,
                )
            else:
                self.status.setText("分析失败，已停止。")
            self.task_panel.finish(failed=True)
        if worker.result is not None:
            if worker.history_issue is not None:
                self.status.setText(self.status.text() + "\n分析成功，历史保存失败；关闭后该日报不会保留。")
                self.diagnostic_text.setText(self.diagnostic_text.text() + f"\n{worker.history_issue.code}：{worker.history_issue.reason}")
                self.task_panel.reveal_issue()
                if self.diagnostics:
                    self.diagnostics.issue(operation_id=worker.result.operation_id, operation_type="history", issue=worker.history_issue,
                                           run_id=worker.result.run_id, snapshot_id=worker.result.snapshot_id)
            elif self.pages.currentWidget() is self.history_page:
                self.refresh_history()
        self.fetch_button.setEnabled(True)
        self.calendar_button.setEnabled(True)
        self.analyze_button.setEnabled(False)
        self.api_key.setEnabled(True)
        self.refresh_diagnostic_availability()
        self.worker = None
        self.prompt_page.set_busy(False)
        worker.deleteLater()

    @Slot(QUrl)
    def open_guide_link(self, url: QUrl) -> None:
        # 只允许四个页面入口；保持切页保护，不调用业务入口或系统浏览器。
        from desktop.user_guide import GUIDE_ROUTES
        target = GUIDE_ROUTES.get(url.toString())
        if self.user_guide_page.loaded and target is not None:
            self.switch_page(getattr(self, target))

    @Slot(QUrl)
    def open_report_link(self, url: QUrl) -> None:
        # 日报只允许正常 arXiv 外链，阻止文件路径和其他 URL scheme。
        if (url.scheme() == "https" and url.host() == "arxiv.org"
                and url.path().startswith(("/abs/", "/pdf/"))
                and not url.userInfo() and url.port() in {-1, 443}):
            QDesktopServices.openUrl(url)

    @Slot()
    def open_log_directory(self) -> None:
        if self.diagnostics and self.diagnostics.persistent and self.diagnostics.log_directory:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.diagnostics.log_directory)))

    def closeEvent(self, event) -> None:
        # 不强行终止 curl 或销毁尚在运行的 QThread；主界面继续响应。
        if self.worker is not None or self.history_worker is not None:
            self.status.setText("任务正在运行，请等待结束后关闭窗口。")
            self.history_page.message.setText("历史读写正在运行，请等待结束后关闭窗口。")
            event.ignore()
            return
        if not self.prompt_page.protect_unsaved():
            event.ignore()
            return
        if not self.save_key():
            self.switch_page(self.settings_page)
            event.ignore()
            return
        event.accept()


def main() -> int:
    application = QApplication(sys.argv)
    diagnostics = DesktopDiagnostics(pipeline.PROJECT_ROOT)
    startup_issue = None
    app_icon = QIcon()
    try:
        paths.prepare_runtime(pipeline.PROJECT_ROOT)
    except Exception as exc:
        startup_issue = make_issue("AKD-STARTUP-RUNTIME_NOT_WRITABLE")
        diagnostics.event(
            operation_id=diagnostics.operation_id(), operation_type="startup",
            stage="startup", state="fail", code=startup_issue.code,
            scope=startup_issue.scope, unexpected=exc,
        )
    try:
        paths.configure_timezone(pipeline.PROJECT_ROOT)
        paths.curl_executable(pipeline.PROJECT_ROOT)
        app_icon = load_application_icon()
    except Exception as exc:
        if startup_issue is None:
            startup_issue = make_issue("AKD-STARTUP-RESOURCE_INVALID")
            diagnostics.event(
                operation_id=diagnostics.operation_id(), operation_type="startup",
                stage="startup", state="fail", code=startup_issue.code,
                scope=startup_issue.scope, unexpected=exc,
            )
    window = DesktopWindow(diagnostics=diagnostics, window_icon=app_icon)
    application.setWindowIcon(window.windowIcon())
    if startup_issue:
        window.status.setText(startup_issue.reason)
        window.show_issue(startup_issue)
        window.fetch_button.setEnabled(False)
        window.calendar_button.setEnabled(False)
        window.analyze_button.setEnabled(False)
        window.api_key.setEnabled(False)
    window.show()
    try:
        return application.exec()
    finally:
        diagnostics.close()


if __name__ == "__main__":
    raise SystemExit(main())
