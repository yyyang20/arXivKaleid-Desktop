# Copyright (c) 2026 yyyang20
# SPDX-License-Identifier: GPL-3.0-only
# See LICENSE in the project root for the full license text.

"""离线 GUI QA：合成快照/事件/日报，窗口真实渲染，禁止业务网络与模型调用。"""
from __future__ import annotations

import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from datetime import date, datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PySide6.QtCore import QPoint, QRect
from PySide6.QtGui import QColor, QFont, QImage, QPainter
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from desktop import app
from desktop.diagnostics import DesktopDiagnostics
from desktop.paths import checked_path
from desktop.pipeline import CandidateSnapshot
from desktop.progress import ProgressEvent


class MemorySecretStore:
    """只在内存保存合成值，不创建或读取 secret.dat。"""
    def load(self):
        return ""

    def save(self, _value):
        pass


def synthetic_snapshot():
    papers = tuple({"arxiv_id": f"2610.{i:05d}", "version": 1} for i in range(117))
    return CandidateSnapshot(
        date(2026, 10, 1), datetime(2026, 10, 2, 2, 6, tzinfo=ZoneInfo("Asia/Shanghai")),
        126, 117, papers,
    )


MARKDOWN = """# arXiv 日报（2026-10-01）

> 离线视觉 QA 合成数据，不是实际业务结果。

- Round 1 输入：117 篇；有效入围：10 篇
- Round 2 合格全文：6 篇；最终推荐：4 篇
- 实际模型、Token、费用在正式日报中由 SQLite 事实提供。

## Round 2 最新推荐（4 篇）

### 1. Polarized images of compact objects

**标签：成像** · **推荐级别：deep_read**

推荐理由：讨论强引力背景下的偏振图像与光子环，适合进一步阅读。

[arXiv 摘要](https://arxiv.org/abs/2610.00001v1) · [PDF](https://arxiv.org/pdf/2610.00001v1)

公开摘要：This synthetic paper explores polarized images near compact objects.

### 2. A new spacetime for strong gravity imaging

**标签：成像｜新解** · **推荐级别：skim_read**

推荐理由：比较新时空背景中的阴影轮廓与偏振分布。

## Round 1 入围详情

此处在正式日报中显示当前 run 的实际入围结果与全文门控事实。
"""


def compose_captures(output, filename, items, columns):
    """只拼接已有窗口截图并加 QA 标题，原始 PNG 独立保留。"""
    width, height, gap = 1060, 820, 20
    rows = (len(items) + columns - 1) // columns
    canvas = QImage(columns * (width + gap) + gap, rows * (height + 50 + gap) + gap,
                    QImage.Format.Format_RGB32)
    canvas.fill(QColor("#e8eef6"))
    painter = QPainter(canvas)
    try:
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        painter.setFont(QFont("Microsoft YaHei UI", 14))
        painter.setPen(QColor("#17243b"))
        for i, (state, title) in enumerate(items):
            x, y = gap + i % columns * (width + gap), gap + i // columns * (height + 50 + gap)
            painter.drawText(x, y + 30, title + " · 离线合成数据 / 真实 Qt 截图")
            source = QImage(str(output / (state + ".png")))
            if source.isNull():
                raise RuntimeError("qa_source_image_missing")
            painter.drawImage(QRect(x, y + 50, width, height), source)
    finally:
        painter.end()
    if not canvas.save(str(output / filename)):
        raise RuntimeError("qa_overview_save_failed")


def main():
    scratch = checked_path(ROOT, ".codex-validation", "alpha8-visual-qa")
    scratch.mkdir(parents=True, exist_ok=True)
    output = Path(tempfile.mkdtemp(prefix="capture-", dir=scratch))
    application = QApplication.instance() or QApplication([])
    diagnostics = DesktopDiagnostics(output)
    window = app.DesktopWindow(MemorySecretStore(), diagnostics)
    window.winId()
    window.windowHandle().setScreen(application.primaryScreen())
    origin = application.primaryScreen().availableGeometry().topLeft()
    window.move(origin.x() + 40, origin.y() + 40)
    captures = []
    gate = threading.Event()

    def wait_until(predicate):
        deadline = time.monotonic() + 8
        while not predicate() and time.monotonic() < deadline:
            application.processEvents()
            QTest.qWait(10)
        if not predicate():
            raise RuntimeError("qa_state_timeout")

    def capture(name):
        application.processEvents()
        QTest.qWait(180)
        pixmap = window.grab()
        if pixmap.isNull() or not pixmap.save(str(output / (name + ".png"))):
            raise RuntimeError("qa_capture_failed")
        captures.append({"state": name, "logical_size": [window.width(), window.height()],
                         "pixel_size": [pixmap.width(), pixmap.height()],
                         "device_pixel_ratio": pixmap.devicePixelRatio(),
                         "report_height": window.home_page.report_stack.height(),
                         "task_height": window.task_panel.height(),
                         "detail_height": window.task_panel.details_scroll.height()})
        popup = window.date_picker
        if popup.isVisible():
            available = window.rect().translated(window.mapToGlobal(QPoint(0, 0))).intersected(window.screen().availableGeometry())
            assert available.contains(popup.geometry()), "qa_calendar_outside_window"
            assert popup.grab().save(str(output / (name + "-popup.png")))
            captures[-1]["calendar_geometry"] = [popup.x(), popup.y(), popup.width(), popup.height()]

    def fetch(day, progress, **_kwargs):
        progress(ProgressEvent(
            task_type="fetch", stage="category", state="running",
            message=f"正在查询北京时间 {day.isoformat()} · 分类 2 / 3：astro-ph.HE\n当前日期已获取 67 条有效记录",
            current_date=day, category="astro-ph.HE", category_index=2,
            category_total=3, processed=67,
        ))
        if not gate.wait(15):
            raise RuntimeError("qa_fetch_gate_timeout")
        return synthetic_snapshot()

    def analyze(attempt, _key, progress):
        for stage, message, count, total in (
            ("round1", "Round 1 完成 · 有效入围 10 篇", None, None),
            ("pdf", "PDF 下载完成 · 已处理 10 / 10", 10, 10),
            ("fulltext", "全文提取中 · 已处理 4 / 9", 4, 9),
        ):
            progress(ProgressEvent(
                task_type="analysis", stage=stage,
                state="running" if stage == "fulltext" else "completed",
                message=message, processed=count, total=total,
            ))
        if not gate.wait(15):
            raise RuntimeError("qa_analysis_gate_timeout")
        for stage, message in (
            ("fulltext", "全文提取完成 · 已处理 9 / 9"),
            ("round2", "Round 2 完成 · 最终推荐 4 篇"),
            ("report", "日报生成完成"),
        ):
            progress(ProgressEvent(task_type="analysis", stage=stage, state="completed", message=message))
        return app.AnalysisResult(1, MARKDOWN, 4, operation_id=attempt.operation_id,
                                  snapshot_id=attempt.snapshot.snapshot_id)

    # 多层禁止网络和子进程传输，避免辅助入口意外触达真实 arXiv/DeepSeek。
    forbidden = AssertionError("visual_qa_network_forbidden")
    try:
        with patch.object(urllib.request, "urlopen", side_effect=forbidden), \
                patch.object(socket.socket, "connect", side_effect=forbidden), \
                patch.object(subprocess, "run", side_effect=forbidden), \
                patch.object(subprocess, "Popen", side_effect=forbidden), \
                patch.object(app, "fetch_candidates_for_date", side_effect=fetch), \
                patch.object(app.AnalysisAttempt, "run", analyze):
            window.show()
            capture("01-initial")
            window.open_date_picker()
            capture("01b-calendar")
            from datetime import timedelta
            from desktop.date_picker import qt_date
            today = window.beijing_today()
            earliest = today - timedelta(days=365)
            window.date_picker.prepare(today, earliest)
            assert not window.date_picker.previous.isEnabled()
            capture("01c-calendar-earliest")
            window.date_picker._choose(qt_date(today - timedelta(days=3)))
            assert window.worker is None and window.snapshot is None
            capture("01d-selected-date")
            window.start_fetch()
            wait_until(lambda: "67" in window.fetch_progress_text.text())
            capture("02-fetching")
            gate.set()
            wait_until(lambda: window.worker is None)
            assert window.snapshot is not None and not window.snapshot.analysis_attempted
            capture("03-fetched")
            window.task_panel.set_expanded(True)
            capture("04-fetch-details")
            gate.clear()
            window.api_key.setText("fake-key-for-offline-qa")
            window.api_key.setModified(True)
            window.analysis_notice_accepted = True
            window.start_analysis()
            wait_until(lambda: "4 / 9" in window.analysis_progress_text.text())
            capture("05-analyzing")
            window.switch_page(window.settings_page)
            capture("06-settings-locked")
            window.switch_page(window.home_page)
            gate.set()
            wait_until(lambda: window.worker is None)
            assert window.snapshot.analysis_attempted and "分析完成" in window.status.text()
            assert "Round 2 最新推荐" in window.report.toPlainText()
            capture("07-completed")
            window.task_panel.set_expanded(True)
            capture("08-analysis-details")
            window.switch_page(window.settings_page)
            capture("09-settings")
            window.switch_page(window.history_page)
            capture("10-history")
            window.switch_page(window.home_page)
            window.task_panel.set_expanded(False)
            window.resize(850, 680)
            capture("11-minimum-size")
            window.task_panel.set_expanded(True)
            capture("11b-minimum-expanded")
            window.home_page.layout().itemAt(0).widget().setFont(QFont("Microsoft YaHei UI", 12))
            window.open_date_picker()
            capture("11c-minimum-calendar")
            window.date_picker.hide()
            window.resize(1060, 820)
            # 真实失败槽使用合成异常；不恢复已消费快照或重试。
            window.start_fetch()
            wait_until(lambda: window.worker is None)
            with patch.object(app.AnalysisAttempt, "run", side_effect=RuntimeError("synthetic")):
                window.start_analysis()
                wait_until(lambda: window.worker is None)
            capture("12-failure")
    finally:
        gate.set()
        if window.worker is not None:
            window.worker.wait(20000)
            application.processEvents()
        window.close()
        diagnostics.close()
    (output / "qa.json").write_text(json.dumps({
        "version": app.__version__, "qt_platform": application.platformName(),
        "offline_synthetic": True, "captures": captures,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    compose_captures(output, "overview.png", [
        ("01-initial", "初始状态"), ("02-fetching", "正在获取候选"),
        ("03-fetched", "候选获取完成 / 收缩"), ("05-analyzing", "正在两轮分析"),
        ("07-completed", "分析完成 / 日报"), ("09-settings", "设置页"),
    ], 3)
    compose_captures(output, "details-history-failure.png", [
        ("04-fetch-details", "候选展开详情"), ("08-analysis-details", "分析展开详情"),
        ("10-history", "历史占位页"), ("12-failure", "失败状态 / 安全诊断"),
    ], 2)
    print(output)


if __name__ == "__main__":
    main()
