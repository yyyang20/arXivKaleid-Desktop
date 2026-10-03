# Copyright (c) 2026 yyyang20
# SPDX-License-Identifier: GPL-3.0-only
# See LICENSE in the project root for the full license text.

from __future__ import annotations

import importlib.util
import os
import threading
import time
import unittest
from datetime import datetime, timezone
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

from desktop.pipeline import CandidateSnapshot
from desktop.progress import ProgressEvent
from desktop.secrets import SecretError
from test_desktop_pipeline import DAY, IsolatedDesktopTest, paper


HAS_QT = (importlib.util.find_spec("PySide6") is not None
          and importlib.util.find_spec("qfluentwidgets") is not None)
if HAS_QT:
    # 仅当前测试进程使用 offscreen，不修改用户或系统环境。
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    from PySide6.QtCore import QCoreApplication, QEvent, QThread, Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication, QLineEdit
    from desktop import app
    from desktop.style import CARD_BACKGROUND, PAGE_BACKGROUND, SurfaceCard


@unittest.skipUnless(HAS_QT, "Desktop GUI dependencies PySide6 / qfluentwidgets are absent")
class DesktopAppTests(IsolatedDesktopTest):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

    def setUp(self):
        super().setUp()
        self.store = Mock(load=Mock(return_value=""))
        self.window = app.DesktopWindow(self.store)
        self.addCleanup(self.close_window)
        self.result = CandidateSnapshot(DAY, datetime(2026, 9, 25, 0, 0, tzinfo=timezone.utc).astimezone(ZoneInfo("Asia/Shanghai")), 2, 1, (paper(),))

    def close_window(self):
        if self.window.worker is not None:
            self.window.worker.wait(5000)
            self.application.processEvents()
        self.window.close()
        self.window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.application.processEvents()

    def wait_until(self, predicate):
        end = time.monotonic() + 5
        while not predicate() and time.monotonic() < end:
            self.application.processEvents()
            QTest.qWait(5)
        self.assertTrue(predicate())

    def test_initial_state_and_password_mode(self):
        self.assertTrue(self.window.fetch_button.isEnabled())
        self.assertFalse(self.window.analyze_button.isEnabled())
        self.assertIsNone(self.window.snapshot)
        self.assertEqual(self.window.api_key.echoMode(), QLineEdit.EchoMode.Password)
        self.assertFalse(self.window.fetch_progress.isTextVisible())
        self.assertEqual(self.window.fetch_progress.maximum(), 1)
        self.assertTrue(all(
            label.text().startswith("○")
            for label in self.window.analysis_steps.values()
        ))
        self.assertTrue(self.window.task_panel.isHidden())
        self.assertIs(self.window.pages.currentWidget(), self.window.home_page)
        self.assertFalse(self.window.windowIcon().isNull())
        icon_sizes = {(size.width(), size.height()) for size in self.window.windowIcon().availableSizes()}
        self.assertEqual(icon_sizes, {(16, 16), (24, 24), (32, 32),
                                      (48, 48), (64, 64), (256, 256)})
        for size in (16, 24, 32, 48, 64, 256):
            image = self.window.windowIcon().pixmap(size, size).toImage()
            self.assertEqual(image.pixelColor(0, 0).alpha(), 0)

    def test_alpha7_pages_share_surface_palette_and_key_focus_style(self):
        self.assertIn(PAGE_BACKGROUND, self.window.styleSheet())
        self.assertIn(PAGE_BACKGROUND, self.window.home_page.styleSheet())
        self.assertIn(PAGE_BACKGROUND, self.window.history_page.styleSheet())
        self.assertIn(PAGE_BACKGROUND, self.window.settings_page.styleSheet())
        self.assertEqual(self.window.api_key.objectName(), "apiKeyInput")
        self.assertIn("border: 1px solid #1677ff", self.window.api_key.styleSheet())
        cards = self.window.home_page.findChildren(SurfaceCard)
        self.assertGreaterEqual(len(cards), 3)
        self.assertTrue(all(card._surface.name() == CARD_BACKGROUND for card in cards[:2]))
        margins = self.window.home_page.layout().contentsMargins()
        self.assertEqual((margins.left(), margins.top(), margins.right(), margins.bottom()),
                         (24, 18, 24, 18))

    def test_pages_and_versions_use_the_single_source_without_history_storage(self):
        from desktop import __version__
        self.assertEqual(self.window.pages.count(), 3)
        self.assertIn(f"v{__version__}", self.window.windowTitle())
        self.assertIn(f"v{__version__}", self.window.version_header.text())
        self.assertEqual(self.window.settings_page.version_label.text(), f"v{__version__}")
        self.assertTrue(self.window.settings_page.isAncestorOf(self.window.api_key))
        self.assertFalse(self.window.home_page.isAncestorOf(self.window.api_key))
        self.window.switch_page(self.window.history_page)
        self.assertIs(self.window.pages.currentWidget(), self.window.history_page)
        self.window.navigation.widget("settingsPage").click()
        self.assertIs(self.window.pages.currentWidget(), self.window.settings_page)
        self.window.navigation.widget("homePage").click()
        self.assertIs(self.window.pages.currentWidget(), self.window.home_page)
        self.assertIsNone(self.window.snapshot)
        self.assert_no_analysis()

    def test_fetch_success_collapses_and_expands_only_real_snapshot_details(self):
        self.window.show()
        with patch("desktop.app.fetch_latest_candidates", return_value=self.result):
            self.window.start_fetch()
            self.assertTrue(self.window.task_panel.expanded)
            self.assertFalse(self.window.task_panel.toggle.isEnabled())
            self.wait_until(lambda: self.window.worker is None)
        self.assertFalse(self.window.task_panel.expanded)
        self.assertFalse(self.window.task_panel.isHidden())
        self.window.task_panel.toggle.click()
        self.assertTrue(self.window.task_panel.expanded)
        details = self.window.task_panel.fetch_details.text()
        self.assertIn("原始 2 条 → 去重 1 条", details)
        self.assertIn("尚未分析", details)
        self.assertNotIn(".json", details)
        self.assertNotIn("费用", details)

    def test_analysis_locks_key_on_settings_page_then_collapses_success(self):
        self.prepare_analysis()
        gate = threading.Event()
        self.addCleanup(gate.set)

        def analyze(_attempt, _key, progress):
            progress(ProgressEvent(task_type="analysis", stage="pdf", state="running",
                                   message="已处理 2 / 3", processed=2, total=3))
            gate.wait(3)
            return app.AnalysisResult(7, "# 测试日报", 0, operation_id="synthetic-operation")

        with patch.object(app.AnalysisAttempt, "run", analyze):
            self.window.start_analysis()
            self.window.switch_page(self.window.settings_page)
            self.assertFalse(self.window.api_key.isEnabled())
            self.assertFalse(self.window.api_key.viewButton.isEnabled())
            self.wait_until(lambda: "2 / 3" in self.window.analysis_progress_text.text())
            self.assertTrue(self.window.task_panel.expanded)
            self.assertNotIn("%", self.window.analysis_progress_text.text())
            gate.set()
            self.wait_until(lambda: self.window.worker is None)
        self.assertTrue(self.window.api_key.isEnabled())
        self.assertFalse(self.window.task_panel.expanded)
        self.window.task_panel.set_expanded(True)
        text = self.window.task_panel.analysis_details.text()
        self.assertIn("已处理 2 / 3", text)
        self.assertNotIn("成功下载", text)
        self.assertIn("run ID：7", text)
        self.assertIn("synthetic-operation", text)
        self.assertEqual(self.window.home_page.report_stack.currentIndex(), 1)

    def test_expanded_details_and_logs_do_not_overlap_at_minimum_size(self):
        self.window.show()
        self.window.resize(850, 680)
        self.window.task_panel.begin("fetch")
        self.window.task_panel.show_snapshot(self.result, app.pipeline.CATEGORIES)
        self.window.task_panel.finish()
        self.window.task_panel.set_expanded(True)
        self.application.processEvents()
        panel = self.window.task_panel
        self.assertLessEqual(panel.details_scroll.geometry().bottom(), panel.open_logs_button.geometry().top())
        self.assertGreaterEqual(self.window.home_page.report_stack.height(), 150)
        self.assertLessEqual(panel.open_logs_button.geometry().bottom(), panel.height())
        self.assertGreaterEqual(self.window.statistics["原始条目数"].height(), 24)

    def test_startup_write_failure_is_fixed_gui_error_without_fallback(self):
        with patch.object(app.paths, "prepare_runtime", side_effect=PermissionError("private-user-path")), patch.object(app, "QApplication") as application, patch.object(app, "DesktopWindow", return_value=self.window):
            application.return_value.exec.return_value = 0
            self.assertEqual(app.main(), 0)
        application.return_value.setWindowIcon.assert_called_once()
        self.assertIn("可写位置", self.window.status.text())
        self.assertNotIn("private", self.window.status.text())
        self.assertTrue(self.window.task_panel.expanded)
        self.assertIn("AKD-STARTUP-RUNTIME_NOT_WRITABLE", self.window.diagnostic_text.text())
        self.assertFalse(self.window.fetch_button.isEnabled())
        self.assertFalse(self.window.analyze_button.isEnabled())
        self.assertFalse(self.window.api_key.isEnabled())

    def test_success_then_failed_refetch_clears_old_snapshot_and_blocks_overlap(self):
        gate = threading.Event()
        thread_ids = []

        def fetch(_day, progress):
            thread_ids.append(QThread.currentThread())
            if len(thread_ids) == 2:
                gate.wait(3)
                raise RuntimeError("synthetic private failure")
            progress(ProgressEvent(
                task_type="fetch", stage="complete", state="completed",
                message="候选抓取完成 · 锁定 1 篇", result_count=1,
            ))
            return self.result

        with patch("desktop.app.fetch_latest_candidates", side_effect=fetch) as fetch_mock:
            self.window.fetch_button.click()
            self.wait_until(lambda: self.window.worker is None)
            self.assertIs(self.window.snapshot, self.result)
            self.assertTrue(self.window.analyze_button.isEnabled())
            self.assertEqual(self.window.statistics["抓取完成时间"].text(), "2026-09-25 08:00:00 北京时间")
            self.window.analyze_button.click()
            self.assertIn("请输入 API Key", self.window.secret_status.text())
            self.assert_no_analysis()
            self.window.fetch_button.click()
            self.assertIsNone(self.window.snapshot)
            self.assertFalse(self.window.analyze_button.isEnabled())
            self.assertFalse(self.window.fetch_button.isEnabled())
            self.assertTrue(all(label.text() == "—" for label in self.window.statistics.values()))
            self.window.start_fetch()
            self.wait_until(lambda: len(thread_ids) == 2)
            self.assertEqual(fetch_mock.call_count, 2)
            self.assertTrue(all(t != self.application.thread() for t in thread_ids))
            gate.set()
            self.wait_until(lambda: self.window.worker is None)
        self.assertIsNone(self.window.snapshot)
        self.assertFalse(self.window.analyze_button.isEnabled())
        self.assertTrue(self.window.fetch_button.isEnabled())
        self.assertEqual(self.window.fetch_progress.maximum(), 1)
        self.assertEqual(self.window.fetch_progress.value(), 0)
        self.assertNotIn("private", self.window.status.text())
        self.assert_no_analysis()

    def test_click_captures_utc_date_before_worker_runs(self):
        clock = Mock()
        clock.now.return_value = datetime(2026, 9, 24, 23, 59, tzinfo=timezone.utc)
        with patch("desktop.app.datetime", clock), patch("desktop.app.fetch_latest_candidates", return_value=self.result) as fetch:
            self.window.start_fetch()
            self.wait_until(lambda: self.window.worker is None)
        clock.now.assert_called_once_with(timezone.utc)
        self.assertEqual(fetch.call_args.args[0].isoformat(), "2026-09-24")

    def test_edit_finished_saves_and_next_window_restores_key(self):
        self.window.api_key.setText("fake-key-only")
        self.window.api_key.setModified(True)
        self.window.api_key.editingFinished.emit()
        self.store.save.assert_called_once_with("fake-key-only")
        self.store.load.return_value = "fake-key-only"
        second = app.DesktopWindow(self.store)
        try:
            self.assertEqual(second.api_key.text(), "fake-key-only")
            self.assertEqual(second.api_key.echoMode(), QLineEdit.EchoMode.Password)
        finally:
            second.close()
            second.deleteLater()

    def test_settings_key_enter_saves_once_and_navigation_keeps_the_value(self):
        self.window.show()
        self.window.navigation.widget("settingsPage").click()
        self.application.processEvents()
        self.window.api_key.setFocus()
        QTest.keyClicks(self.window.api_key, "fake-key-only")
        QTest.keyClick(self.window.api_key, Qt.Key.Key_Return)
        self.store.save.assert_called_once_with("fake-key-only")
        self.window.navigation.widget("homePage").click()
        self.window.navigation.widget("settingsPage").click()
        self.assertEqual(self.window.api_key.text(), "fake-key-only")
        self.store.save.assert_called_once()

    def test_secret_errors_are_safe_and_do_not_disable_fetch(self):
        self.store.save.side_effect = SecretError("synthetic-private-secret")
        self.window.api_key.setText("fake-key-only")
        self.window.api_key.setModified(True)
        self.window.save_key()
        self.assertNotIn("private", self.window.secret_status.text())
        self.assertTrue(self.window.fetch_button.isEnabled())
        self.store.load.side_effect = SecretError("synthetic-private-secret")
        second = app.DesktopWindow(self.store)
        try:
            self.assertNotIn("private", second.secret_status.text())
            self.assertEqual(second.api_key.text(), "")
            self.assertTrue(second.fetch_button.isEnabled())
        finally:
            second.close()
            second.deleteLater()

    def test_close_during_fetch_keeps_worker_alive_until_finished(self):
        gate = threading.Event()
        def fetch(_day, _progress):
            gate.wait(3)
            return self.result
        with patch("desktop.app.fetch_latest_candidates", side_effect=fetch):
            self.window.show()
            self.window.start_fetch()
            self.assertFalse(self.window.close())
            self.assertIsNotNone(self.window.worker)
            gate.set()
            self.wait_until(lambda: self.window.worker is None)
            self.assertTrue(self.window.close())

    def test_close_save_failure_remains_visible_then_can_retry(self):
        self.window.show()
        self.window.api_key.setText("fake-key-only")
        self.window.api_key.setModified(True)
        self.store.save.side_effect = SecretError("synthetic-private-secret")
        self.assertFalse(self.window.close())
        self.assertTrue(self.window.isVisible())
        self.assertNotIn("private", self.window.secret_status.text())
        self.store.save.side_effect = None
        self.assertTrue(self.window.close())

    def test_fetch_progress_is_indeterminate_and_uses_event_category_total(self):
        gate = threading.Event()

        def fetch(day, progress):
            progress(ProgressEvent(
                task_type="fetch", stage="category", state="running",
                message=(f"候选抓取中 · UTC {day.isoformat()} · "
                         "分类 1 / 4：gr-qc · 当前日期已获取 7 篇"),
                current_date=day, category="gr-qc",
                category_index=1, category_total=4, processed=7,
            ))
            gate.wait(3)
            return self.result

        with patch("desktop.app.fetch_latest_candidates", side_effect=fetch):
            self.window.start_fetch()
            self.wait_until(lambda: "1 / 4" in self.window.fetch_progress_text.text())
            self.assertEqual(self.window.fetch_progress.maximum(), 0)
            self.assertNotIn("%", self.window.fetch_progress_text.text())
            gate.set()
            self.wait_until(lambda: self.window.worker is None)
        self.assertEqual(self.window.fetch_progress.maximum(), 1)
        self.assertEqual(self.window.fetch_progress.value(), 1)

    def prepare_analysis(self):
        self.window.snapshot = self.result
        self.window.analyze_button.setEnabled(True)
        self.window.api_key.setText("fake-key-only")
        self.window.api_key.setModified(True)
        self.window.analysis_notice_accepted = True

    def test_analysis_notice_rejection_blocks_attempt_and_model(self):
        self.window.snapshot = self.result
        self.window.analyze_button.setEnabled(True)
        self.window.api_key.setText("fake-key-only")
        self.window.api_key.setModified(True)
        with patch.object(
            app.QMessageBox,
            "question",
            return_value=app.QMessageBox.StandardButton.No,
        ) as question, patch.object(app, "AnalysisAttempt") as attempt:
            self.window.start_analysis()
        question.assert_called_once()
        attempt.assert_not_called()
        self.store.save.assert_not_called()
        self.assertFalse(self.result.analysis_attempted)
        self.assertIsNone(self.window.worker)
        self.assertTrue(self.window.analyze_button.isEnabled())
        self.assertIn("未调用模型", self.window.status.text())

    def test_analysis_notice_is_only_requested_once_after_acceptance(self):
        with patch.object(
            app.QMessageBox,
            "question",
            return_value=app.QMessageBox.StandardButton.Yes,
        ) as question:
            self.assertTrue(self.window.confirm_analysis_notice())
            self.assertTrue(self.window.confirm_analysis_notice())
        question.assert_called_once()
        self.assertIn("论文标题、摘要", app.ANALYSIS_NOTICE)
        self.assertIn("研究边界", app.ANALYSIS_NOTICE)
        self.assertIn("可能产生费用", app.ANALYSIS_NOTICE)
        self.assertIn("Windows DPAPI", app.ANALYSIS_NOTICE)
        self.assertIn("PDF、SQLite 和缓存", app.ANALYSIS_NOTICE)
        self.assertIn("没有维护者服务器中转或遥测", app.ANALYSIS_NOTICE)

    def test_analysis_consumes_snapshot_off_thread_and_renders_markdown(self):
        self.prepare_analysis()
        gate = threading.Event()
        seen = []

        def analyze(attempt, key, progress):
            seen.append((attempt.snapshot, key, QThread.currentThread()))
            progress(ProgressEvent(
                task_type="analysis", stage="round1", state="running",
                message="Round 1 分析中…",
            ))
            progress(ProgressEvent(
                task_type="analysis", stage="round1", state="completed",
                message="Round 1 完成 · 入围 1 篇", result_count=1,
            ))
            progress(ProgressEvent(
                task_type="analysis", stage="pdf", state="skipped",
                message="PDF 下载已跳过",
            ))
            progress(ProgressEvent(
                task_type="analysis", stage="report", state="completed",
                message="日报生成完成",
            ))
            gate.wait(3)
            return app.AnalysisResult(1, "# 测试日报\n\n**推荐内容**", 1)

        with patch.object(app.AnalysisAttempt, "run", analyze):
            self.window.show()
            self.window.analyze_button.click()
            self.assertTrue(self.result.analysis_attempted)
            self.assertFalse(self.window.analyze_button.isEnabled())
            self.assertFalse(self.window.fetch_button.isEnabled())
            self.assertFalse(self.window.close())
            self.window.start_fetch()
            self.window.start_analysis()
            self.wait_until(lambda: bool(seen))
            gate.set()
            self.wait_until(lambda: self.window.worker is None)
        self.assertIs(seen[0][0], self.result)
        self.assertEqual(seen[0][1], "fake-key-only")
        self.assertNotEqual(seen[0][2], self.application.thread())
        self.store.save.assert_called_once_with("fake-key-only")
        self.assertEqual(self.window.status.text(), "分析完成\n最终推荐 1 篇")
        self.assertTrue(self.window.analysis_steps["round1"].text().startswith("✓"))
        self.assertTrue(self.window.analysis_steps["pdf"].text().startswith("—"))
        self.assertTrue(self.window.analysis_steps["report"].text().startswith("✓"))
        self.assertEqual(self.window.analysis_steps["round1"].thread(), self.application.thread())
        self.assertIn("测试日报", self.window.report.toPlainText())
        self.assertNotIn("**", self.window.report.toPlainText())
        self.assertIn("font-weight", self.window.report.toHtml())
        self.assertTrue(self.window.fetch_button.isEnabled())
        self.assertFalse(self.window.analyze_button.isEnabled())
        with patch.object(app.AnalysisAttempt, "run") as again:
            self.window.start_analysis()
            again.assert_not_called()
        with patch("desktop.app.fetch_latest_candidates", return_value=self.result):
            self.window.start_fetch()
            self.assertEqual(self.window.report.toPlainText(), "")
            self.wait_until(lambda: self.window.worker is None)

    def test_analysis_failure_is_safe_consumed_and_not_retried(self):
        self.prepare_analysis()
        with patch.object(app.AnalysisAttempt, "run", side_effect=RuntimeError("synthetic-private-response")) as run:
            self.window.start_analysis()
            self.wait_until(lambda: self.window.worker is None)
            self.window.start_analysis()
            self.assertEqual(run.call_count, 1)
        self.assertTrue(self.result.analysis_attempted)
        self.assertTrue(self.window.fetch_button.isEnabled())
        self.assertFalse(self.window.analyze_button.isEnabled())
        self.assertNotIn("private", self.window.status.text())
        self.assertTrue(self.window.task_panel.expanded)
        self.assertIn("AKD-PREPARE-WORKSPACE_FAILED", self.window.diagnostic_text.text())

    def test_markdown_render_failure_preserves_analysis_success_semantics(self):
        self.prepare_analysis()
        result = app.AnalysisResult(
            7, "# synthetic", 1,
            operation_id="operation-safe-id",
            snapshot_id=self.result.snapshot_id,
        )
        with patch.object(app.AnalysisAttempt, "run", return_value=result), patch.object(
            self.window.report, "setMarkdown", side_effect=RuntimeError("private renderer")
        ):
            self.window.start_analysis()
            self.wait_until(lambda: self.window.worker is None)
        self.assertIn("分析成功", self.window.status.text())
        self.assertIn("日报展示失败", self.window.status.text())
        self.assertNotIn("private", self.window.diagnostic_text.text())
        self.assertFalse(self.window.analyze_button.isEnabled())

    def test_key_save_failure_blocks_analysis_without_consuming(self):
        self.prepare_analysis()
        self.store.save.side_effect = SecretError("private")
        with patch.object(app.AnalysisAttempt, "run") as run:
            self.window.start_analysis()
            run.assert_not_called()
        self.assertIsNone(self.window.worker)
        self.assertFalse(self.result.analysis_attempted)

    def test_report_only_opens_arxiv_https_links(self):
        from PySide6.QtCore import QUrl
        with patch("desktop.app.QDesktopServices.openUrl") as open_url:
            for url in ("file:///tmp/example", "https://example.org/abs/123", "javascript:alert(1)"):
                self.window.open_report_link(QUrl(url))
            open_url.assert_not_called()
            self.window.open_report_link(QUrl("https://arxiv.org/pdf/2609.00001v1"))
            open_url.assert_called_once()

    def test_large_font_small_window_keeps_buttons_and_panels_separate(self):
        from PySide6.QtGui import QFont
        window = self.window
        card = window.home_page.layout().itemAt(0).widget()
        card.setFont(QFont('Microsoft YaHei UI', 12))
        window.resize(850, 680)
        window.show()
        window.task_panel.begin('fetch')
        QTest.qWait(100)
        self.assertLess(window.fetch_button.geometry().bottom(), card.height())
        self.assertGreater(window.task_panel.geometry().top(), card.geometry().bottom())
        self.assertGreaterEqual(window.home_page.report_stack.height(), 70)
