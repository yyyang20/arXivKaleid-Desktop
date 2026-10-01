# Copyright (c) 2026 yyyang20
# SPDX-License-Identifier: GPL-3.0-only
# See LICENSE in the project root for the full license text.

from __future__ import annotations

import sys
from datetime import date, datetime, timezone

from PySide6.QtCore import QThread, Signal, Slot, QUrl
from PySide6.QtGui import QDesktopServices, QFont
from PySide6.QtWidgets import (
    QApplication, QHBoxLayout, QMessageBox, QStackedWidget, QVBoxLayout, QWidget,
)
from qfluentwidgets import (
    FluentIcon, NavigationInterface, SubtitleLabel, Theme, setTheme, setThemeColor,
)

from desktop import __version__
from desktop import paths, pipeline
from desktop.pipeline import (
    CandidateError, CandidateFetchResult, CandidateSnapshot, fetch_latest_candidates,
)
from desktop.secrets import SecretError, SecretStore
from desktop.analysis import AnalysisAttempt, AnalysisError, AnalysisResult
from desktop.diagnostics import DesktopDiagnostics
from desktop.errors import DesktopIssue, DesktopOutcome, make_issue
from desktop.progress import ProgressEvent
from desktop.pages import HomePage, HistoryPage, SettingsPage
from desktop.task_panel import ANALYSIS_STAGES, STAGE_SYMBOLS


ANALYSIS_NOTICE = (
    "开始分析会将论文标题、摘要、通过门控后的 PDF 提取全文和内置研究边界"
    "发送到你自己的 DeepSeek API，并可能产生费用。\n\n"
    "API Key 仅在本机使用 Windows DPAPI 加密保存；PDF、SQLite 和缓存保存在本机。"
    "应用没有维护者服务器中转或遥测。\n\n"
    "是否同意并继续？详情见随附 PRIVACY.md。"
)

class FetchWorker(QThread):
    """工作线程只计算结果；finished 连接的窗口槽在 GUI 线程执行。"""

    progress = Signal(object)

    def __init__(
        self, start_date_utc: date, diagnostics: DesktopDiagnostics | None = None,
        operation_id: str | None = None, parent=None,
    ):
        super().__init__(parent)
        self.start_date_utc = start_date_utc
        self.snapshot: CandidateSnapshot | None = None
        self.outcome: DesktopOutcome | None = None
        self.issue: DesktopIssue | None = None
        self.diagnostics = diagnostics
        self.operation_id = operation_id

    def run(self) -> None:
        try:
            if self.diagnostics is None and self.operation_id is None:
                result = fetch_latest_candidates(self.start_date_utc, self.progress.emit)
            else:
                result = fetch_latest_candidates(
                    self.start_date_utc, self.progress.emit,
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

    def __init__(self, attempt: AnalysisAttempt, api_key: str, parent=None):
        super().__init__(parent)
        self.attempt = attempt
        self._api_key = api_key
        self.result: AnalysisResult | None = None
        self.issue: DesktopIssue | None = None

    def run(self) -> None:
        try:
            self.result = self.attempt.run(self._api_key, self.progress.emit)
        except AnalysisError as exc:
            self.issue = exc.issue
        except Exception:
            self.issue = make_issue("AKD-PREPARE-WORKSPACE_FAILED")
        finally:
            self._api_key = ""


class DesktopWindow(QWidget):
    def __init__(
        self,
        secret_store: SecretStore | None = None,
        diagnostics: DesktopDiagnostics | None = None,
    ):
        super().__init__()
        self.secret_store = secret_store if secret_store is not None else SecretStore()
        self.diagnostics = diagnostics
        self.snapshot: CandidateSnapshot | None = None
        self.worker: FetchWorker | AnalysisWorker | None = None
        self.analysis_notice_accepted = False
        # 不使用 Fluent 的默认配置持久化，避免向 cwd 写入额外设置文件。
        setTheme(Theme.LIGHT, save=False)
        setThemeColor("#1677ff", save=False)
        self.setFont(QFont("Microsoft YaHei UI", 10))
        self.setWindowTitle(f"arXivKaleid Desktop v{__version__}")
        self.resize(1060, 820)
        self.setMinimumSize(850, 680)
        self.setObjectName("desktopWindow")
        self.setStyleSheet("QWidget#desktopWindow {background: #f0f4f9;}")
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
        self.history_page = HistoryPage()
        self.settings_page = SettingsPage()
        for page, icon, title in (
            (self.home_page, FluentIcon.HOME, "首页"),
            (self.history_page, FluentIcon.HISTORY, "历史"),
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
        self.analyze_button.clicked.connect(self.start_analysis)
        if self.diagnostics and not self.diagnostics.persistent:
            self.diagnostic_text.setText("诊断仅保留在当前会话；本地日志不可写。")

    def switch_page(self, page: QWidget) -> None:
        self.pages.setCurrentWidget(page)
        self.navigation.setCurrentItem(page.objectName())

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if hasattr(self, "task_panel"):
            self.task_panel.fit_to_window()

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
        start_date_utc = datetime.now(timezone.utc).date()
        # 先使旧快照失效，再启动网络工作；API Key 不参与候选管线。
        self.snapshot = None
        self.report.clear()
        self.switch_page(self.home_page)
        self.task_panel.begin("fetch")
        self.reset_analysis_progress()
        self.analyze_button.setEnabled(False)
        self.fetch_button.setEnabled(False)
        for label in self.statistics.values():
            label.setText("—")
        self.status.setText("正在抓取最新候选…")
        self.fetch_progress_text.setText(
            f"候选抓取中 · 正在检查 UTC {start_date_utc.isoformat()}"
        )
        self.fetch_progress.setRange(0, 0)
        operation_id = self.diagnostics.operation_id() if self.diagnostics else None
        self.worker = FetchWorker(
            start_date_utc, self.diagnostics, operation_id, self
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
                str(snapshot.raw_count), str(snapshot.unique_count), str(snapshot.round1_count),
            )
            for label, value in zip(self.statistics.values(), values):
                label.setText(value)
            self.status.setText(f"候选获取完成 · 锁定 {snapshot.round1_count} 篇")
            self.fetch_progress_text.setText(
                f"原始 {snapshot.raw_count} 条 → 去重 {snapshot.unique_count} 条 → "
                f"锁定 Round 1 候选 {snapshot.round1_count} 篇"
            )
            self.fetch_progress.setRange(0, 1)
            self.fetch_progress.setValue(1)
            self.task_panel.show_snapshot(snapshot, pipeline.CATEGORIES)
        self.task_panel.finish(failed=worker.issue is not None)
        self.analyze_button.setEnabled(self.snapshot is not None and not self.snapshot.analysis_attempted)
        self.fetch_button.setEnabled(True)
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
        # 在启动线程前消费快照；任何失败都不恢复旧快照的分析资格。
        attempt = AnalysisAttempt(self.snapshot, self.diagnostics)
        self.switch_page(self.home_page)
        self.task_panel.begin("analysis")
        self.fetch_button.setEnabled(False)
        self.analyze_button.setEnabled(False)
        self.api_key.setEnabled(False)
        self.status.setText("正在准备分析…")
        self.reset_analysis_progress()
        self.analysis_progress_text.setText("正在准备分析…")
        self.worker = AnalysisWorker(attempt, self.api_key.text(), self)
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
        self.fetch_button.setEnabled(True)
        self.analyze_button.setEnabled(False)
        self.api_key.setEnabled(True)
        self.refresh_diagnostic_availability()
        self.worker = None
        worker.deleteLater()

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
        if self.worker is not None:
            self.status.setText("任务正在运行，请等待结束后关闭窗口。")
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
    except Exception as exc:
        if startup_issue is None:
            startup_issue = make_issue("AKD-STARTUP-RESOURCE_INVALID")
            diagnostics.event(
                operation_id=diagnostics.operation_id(), operation_type="startup",
                stage="startup", state="fail", code=startup_issue.code,
                scope=startup_issue.scope, unexpected=exc,
            )
    window = DesktopWindow(diagnostics=diagnostics)
    if startup_issue:
        window.status.setText(startup_issue.reason)
        window.show_issue(startup_issue)
        window.fetch_button.setEnabled(False)
        window.analyze_button.setEnabled(False)
        window.api_key.setEnabled(False)
    window.show()
    try:
        return application.exec()
    finally:
        diagnostics.close()


if __name__ == "__main__":
    raise SystemExit(main())
