# Copyright (c) 2026 yyyang20
# SPDX-License-Identifier: GPL-3.0-only
# See LICENSE in the project root for the full license text.

"""任务展示组件：只观察已有事件，不推算进度或改变业务结果。"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QHBoxLayout, QLabel, QProgressBar, QVBoxLayout, QWidget
from qfluentwidgets import FluentIcon, PushButton, ScrollArea, ToolButton

from desktop.progress import ProgressEvent
from desktop.style import SurfaceCard


ANALYSIS_STAGES = (
    ("round1", "Round 1"), ("pdf", "PDF 下载"), ("fulltext", "全文提取"),
    ("round2", "Round 2"), ("report", "日报生成"),
)
STAGE_SYMBOLS = {
    "pending": "○", "running": "▶", "completed": "✓",
    "skipped": "—", "outcome": "—", "failed": "✕",
}


def wrapping_label(text="", parent=None):
    label = QLabel(text, parent)
    label.setWordWrap(True)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    return label


class TaskPanel(SurfaceCard):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.running = False
        self.kind = "fetch"
        self.events: dict[str, ProgressEvent] = {}
        self.expanded = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 10, 16, 10)
        header = QHBoxLayout()
        self.status = wrapping_label("尚未获取候选")
        self.status.setObjectName("taskSummary")
        header.addWidget(self.status, 1)
        self.toggle = ToolButton(FluentIcon.CHEVRON_DOWN_MED)
        self.toggle.setAccessibleName("展开任务详情")
        self.toggle.clicked.connect(lambda: self.set_expanded(not self.expanded))
        header.addWidget(self.toggle)
        layout.addLayout(header)

        # 展开详情可以滚动，给日报留出空间；失败信息不会随成功摘要一起隐藏。
        self.details_scroll = ScrollArea(self)
        self.details_scroll.setWidgetResizable(True)
        self.details_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.details_scroll.setMaximumHeight(260)
        self.details_scroll.setMinimumHeight(80)
        self.details_scroll.setStyleSheet("QScrollArea {background: transparent; border: none;}")
        content = QWidget()
        content.setStyleSheet("background: transparent;")
        details = QVBoxLayout(content)
        details.setContentsMargins(0, 4, 8, 4)
        self.fetch_area = QWidget()
        fetch = QVBoxLayout(self.fetch_area)
        fetch.setContentsMargins(0, 0, 0, 0)
        self.fetch_progress = QProgressBar()
        self.fetch_progress.setTextVisible(False)
        self.fetch_progress.setRange(0, 1)
        self.fetch_progress.setValue(0)
        self.fetch_progress.setFixedHeight(6)
        self.fetch_progress.setStyleSheet(
            "QProgressBar {border: none; border-radius: 3px; background: #dce7f4;}"
            "QProgressBar::chunk {background: #1677ff; border-radius: 3px;}"
        )
        fetch.addWidget(self.fetch_progress)
        self.fetch_progress_text = wrapping_label("尚未开始抓取")
        fetch.addWidget(self.fetch_progress_text)
        self.fetch_details = wrapping_label()
        fetch.addWidget(self.fetch_details)
        details.addWidget(self.fetch_area)

        self.analysis_area = QWidget()
        analysis = QVBoxLayout(self.analysis_area)
        analysis.setContentsMargins(0, 0, 0, 0)
        stages = QHBoxLayout()
        self.analysis_steps = {}
        for stage, title in ANALYSIS_STAGES:
            label = wrapping_label(f"○ {title}")
            label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            label.setMinimumWidth(70)
            self.analysis_steps[stage] = label
            stages.addWidget(label, 1)
        analysis.addLayout(stages)
        self.analysis_progress_text = wrapping_label("尚未开始分析")
        analysis.addWidget(self.analysis_progress_text)
        self.analysis_details = wrapping_label()
        analysis.addWidget(self.analysis_details)
        details.addWidget(self.analysis_area)
        self.analysis_area.hide()
        self.diagnostic_text = wrapping_label("尚无诊断信息")
        details.addWidget(self.diagnostic_text)
        self.open_logs_button = PushButton(FluentIcon.FOLDER, "打开日志目录")
        self.open_logs_button.setAccessibleName("打开日志目录")
        self.open_logs_button.setToolTip("打开日志目录")
        self.details_scroll.setWidget(content)
        layout.addWidget(self.details_scroll)
        layout.addWidget(self.open_logs_button, 0, Qt.AlignmentFlag.AlignLeft)
        self.set_expanded(False)
        self.hide()

    def set_expanded(self, expanded: bool):
        self.expanded = expanded or self.running
        self.details_scroll.setVisible(self.expanded)
        self.open_logs_button.setVisible(self.expanded)
        self.fit_to_window()
        self.toggle.setIcon(FluentIcon.UP if self.expanded else FluentIcon.CHEVRON_DOWN_MED)
        self.toggle.setAccessibleName("收起任务详情" if self.expanded else "展开任务详情")

    def fit_to_window(self):
        # 小窗口压缩可滚动详情，大窗口增加阅读高度；日报始终保留可见空间。
        height = 80 if self.running else min(240, max(40, self.window().height() - 690))
        self.details_scroll.setFixedHeight(height)
        self.layout().invalidate()
        self.setMinimumHeight(self.layout().minimumSize().height())
        self.updateGeometry()

    def begin(self, kind: str):
        self.kind = kind
        self.running = True
        self.events.clear()
        self.fetch_details.clear()
        self.analysis_details.clear()
        self.fetch_details.hide()
        self.analysis_details.hide()
        self.diagnostic_text.hide()
        self.fetch_progress.show()
        self.fetch_progress_text.show()
        self.analysis_progress_text.show()
        self.diagnostic_text.setText("尚无诊断信息")
        self.fetch_area.setVisible(kind == "fetch")
        self.analysis_area.setVisible(kind == "analysis")
        self.toggle.setEnabled(False)
        self.set_tone("running")
        self.show()
        self.set_expanded(True)

    def set_tone(self, tone: str):
        colors = {"running": ("#edf5ff", "#bdd8ff", "#1559a7"),
                  "success": ("#f0faf3", "#b9e4c7", "#137333"),
                  "error": ("#fff2f0", "#f0bcb7", "#a4262c")}
        background, border, color = colors[tone]
        self.set_surface(background, border)
        self.setStyleSheet(
            f"QLabel#taskSummary {{color: {color}; font-weight: 600;}}"
        )

    def finish(self, *, failed=False):
        self.running = False
        self.fetch_details.setVisible(self.kind == "fetch")
        self.analysis_details.setVisible(self.kind == "analysis")
        self.fetch_progress.hide()
        self.fetch_progress_text.hide()
        self.analysis_progress_text.hide()
        self.diagnostic_text.setVisible(self.diagnostic_text.text() != "尚无诊断信息")
        self.toggle.setEnabled(True)
        self.set_tone("error" if failed else "success")
        self.show()
        self.set_expanded(failed)

    def reveal_issue(self):
        self.diagnostic_text.show()
        self.set_tone("error")
        self.show()
        self.set_expanded(True)

    def observe(self, event: ProgressEvent):
        if event.task_type != "analysis" or event.stage not in dict(ANALYSIS_STAGES):
            return
        self.events[event.stage] = event
        color = {"running": "#1559a7", "completed": "#137333", "failed": "#a4262c"}.get(event.state, "#778396")
        self.analysis_steps[event.stage].setStyleSheet(f"color: {color};")
        # 已处理包括单篇失败与门控排除，绝不改写为成功数量。
        lines = [self.events[s].message for s, _ in ANALYSIS_STAGES if s in self.events]
        self.analysis_details.setText("\n".join(lines))

    def show_snapshot(self, snapshot, categories):
        self.fetch_details.setText(
            f"抓取日期（北京时间）：{snapshot.candidate_date.isoformat()}\n"
            f"抓取完成时间：{snapshot.completed_time_text}\n"
            f"分类来源：{' · '.join(categories)}\n"
            f"原始 {snapshot.raw_count} 条 → 去重后候选 {snapshot.unique_count} 篇\n"
            "去重后候选将全部进入 Round 1；尚未分析。"
        )

    def show_result(self, result):
        text = self.analysis_details.text()
        identity = [f"最终推荐：{result.recommendation_count} 篇", f"run ID：{result.run_id}"]
        if result.operation_id:
            identity.append(f"operation ID：{result.operation_id}")
        identity.append("当前快照已消费；再次分析需重新获取候选。")
        self.analysis_details.setText("\n".join(filter(None, [*identity, text])))
