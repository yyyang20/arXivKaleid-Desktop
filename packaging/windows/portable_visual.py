# Copyright (c) 2026 yyyang20
# SPDX-License-Identifier: GPL-3.0-only
# See LICENSE in the project root for the full license text.

"""冻结诊断专用合成状态；不提供正式业务入口、不引入测试库。"""
from datetime import date, datetime, timedelta, timezone
import ctypes
import gc
import os
from pathlib import Path
import threading
import time
import hashlib
import sqlite3
from contextlib import closing

from PySide6.QtCore import QCoreApplication, QEvent, QPoint, QTimer, QUrl
from PySide6.QtGui import QDesktopServices, QFont, QInputMethodEvent
from PySide6.QtWidgets import QMessageBox, QStyleOptionViewItem
from desktop import app
from desktop.errors import make_issue
from desktop.progress import ProgressEvent
from desktop.secrets import SecretError
from desktop.date_picker import qt_date
from desktop import paths, pipeline
from desktop.history import HistoryError, timestamp_us
from desktop.report import build_desktop_report
import main
import run_round2
import round2_fulltext_state as fulltext


def synthetic_snapshot(day):
    """完整元数据只用于隔离诊断，不通过任何网络入口取得。"""
    papers = tuple(dict(
        arxiv_id=f"2610.{i:05d}", version=1, id=f"https://arxiv.org/abs/2610.{i:05d}v1",
        title=f"离线合成论文 {i}：Polarized images of compact objects",
        authors="Synthetic Author", summary="Offline synthetic abstract for GUI validation.",
        categories="gr-qc", primary_category="gr-qc",
        published=f"{day}T00:00:00Z", updated=f"{day}T01:00:00Z",
        abs_url=f"https://arxiv.org/abs/2610.{i:05d}v1",
        pdf_url=f"https://arxiv.org/pdf/2610.{i:05d}v1",
    ) for i in range(117))
    # 隔离诊断故意拉开抓取与生成时刻，防止两者错误地复用同一字段。
    return app.CandidateSnapshot(day, datetime.now(timezone.utc) - timedelta(hours=2), 126, 117, papers)


def synthetic_analysis_result(attempt, source_root=None, *, recommendation_count=4):
    """合成 SQLite 事实经真实校验器和日报生成器；不手写日报模板。"""
    source_root = source_root if source_root is not None else pipeline.PROJECT_ROOT
    destination = paths.runtime_path(source_root, "work", "synthetic-report", attempt.operation_id)
    destination.mkdir(parents=True)
    config, *_ = run_round2.read_round2_context(paths.resource_root(pipeline.PROJECT_ROOT))
    papers = [dict(p) for p in attempt.snapshot.papers]
    database = destination / "main.sqlite"
    fulltext_database = destination / "fulltext.sqlite"
    with closing(main.init_database(database)) as connection, closing(sqlite3.connect(fulltext_database)) as inputs:
        inputs.row_factory = sqlite3.Row
        fulltext.initialize_fulltext_schema(inputs)
        run_id = main.start_run(connection, main.current_time_iso())
        main.insert_papers(connection, papers)
        round1, _ = main.validate_round1_result(dict(
            task_type="round1_abstract_screening", profile_version="profile_v2",
            prompt_version=main.CURRENT_ROUND1_PROMPT_VERSION, selection_policy="top_k_daily_budget",
            selection_policy_version="top_k_daily_budget_v4",
            selected_papers=[dict(candidate_index=i + 1, content_label="成像", reason="离线合成验证：黑洞偏振图像。") for i in range(10)],
        ), papers, max_selected=10, profile_version="profile_v2", prompt_version=main.CURRENT_ROUND1_PROMPT_VERSION,
            selection_policy_version="top_k_daily_budget_v4")
        assert round1["batch_valid"]
        selected = round1["selected_papers"]
        main.save_round1_screening_results(connection, run_id, selected, config, selection_audit=round1["selection_audit"])
        main.save_round1_completion_statuses(connection, run_id, papers, selected, config)
        for paper in selected:
            fulltext.save_fulltext_result(inputs, fulltext.PdfFullTextResult(
                paper["arxiv_id"], paper["version"], "0" * 64, 2,
                fulltext.PAGE_GATE_ELIGIBLE, fulltext.EXTRACTION_EXTRACTED,
                ("Synthetic offline page one", "Synthetic offline page two"),
            ))
        fulltext.replace_input_decisions(inputs, [dict(arxiv_id=p["arxiv_id"], version=p["version"],
                                                      decision="full_text", page_count=2) for p in selected])
        round2, _ = main.validate_round2_result(dict(
            task_type=main.ROUND2_TASK_TYPE, selection_policy=main.ROUND2_SELECTION_POLICY,
            profile_version="profile_v2", prompt_version=main.CURRENT_ROUND2_PROMPT_VERSION,
            final_recommendations=[dict(arxiv_id=p["arxiv_id"], version=p["version"],
                                        content_label="成像", reason="离线合成验证：推荐正文与保存快照一致。")
                                   for p in selected[:recommendation_count]],
        ), selected, max_recommendations=5, profile_version="profile_v2", prompt_version=main.CURRENT_ROUND2_PROMPT_VERSION)
        assert round2["batch_valid"]
        recommendations = round2["final_recommendations"]
        main.save_round2_screening_results(connection, run_id, recommendations, config, selection_audit=round2["selection_audit"])
        main.finish_run(connection, run_id, "success", "离线合成验证", main.current_time_iso())
        markdown = build_desktop_report(connection, fulltext_database, run_id, attempt.snapshot)
    return app.AnalysisResult(run_id, markdown, len(recommendations), operation_id=attempt.operation_id,
                              snapshot_id=attempt.snapshot.snapshot_id, candidate_count=attempt.snapshot.round1_count,
                              report_completed_at=datetime.now(timezone.utc),
                              fetch_completed_at=attempt.snapshot.completed_at,
                              candidate_date=attempt.snapshot.candidate_date)


def exercise_history(application, window, capture, wait, source_root=None):
    """源码和冻结 QA 共用真实 QThread/确认对话框；合成数据留在验证目录。"""
    original_run = app.AnalysisAttempt.run
    original_save = window.history_store.save
    try:
        first = window.history_store.list_records()[0].record_id
        window.switch_page(window.home_page)
        # 第二次抓取使用另一个论文日期；历史必须记录各自快照，不能读当前日期控件。
        window.selected_date = window.beijing_today() - timedelta(days=4)
        window.start_fetch()
        wait(lambda: window.worker is None)
        zero_snapshot = window.snapshot
        app.AnalysisAttempt.run = lambda attempt, _key, _progress: synthetic_analysis_result(
            attempt, source_root, recommendation_count=0)
        window.start_analysis()
        wait(lambda: window.worker is None)
        assert "最终推荐 0 篇" in window.status.text()
        window.switch_page(window.history_page)
        wait(lambda: window.history_worker is None and not window._history_jobs)
        assert window.history_page.model.rowCount() == 2
        for row in window.history_page.model.rows:
            text = window.history_page.model.data(window.history_page.model.index(
                window.history_page.model.rows.index(row), 0))
            assert f"抓取时间 {row.fetch_time_text}" in text
            assert f"论文日期 {row.candidate_date.isoformat()}" in text
            assert row.fetch_completed_at_us < row.completed_at_us
        capture("10-history")
        zero = window.history_page.model.rows[0].record_id
        zero_record = window.history_store.read(zero)
        assert zero_record.summary.fetch_completed_at_us == timestamp_us(zero_snapshot.completed_at)
        assert zero_record.summary.candidate_date == zero_snapshot.candidate_date
        assert zero_record.summary.candidate_date != window.history_store.read(first).summary.candidate_date
        for record_id, name in ((first, "10b-history-detail"), (zero, "10c-history-zero")):
            record = window.history_store.read(record_id)
            window.open_history_record(record_id)
            wait(lambda: window.history_worker is None)
            assert "arXivKaleid Desktop 日报" in window.history_page.report.toPlainText()
            assert record.summary.candidate_count == 117
            capture(name)
            window.back_to_history()
            wait(lambda: window.history_worker is None)
        # 第三条真实生成日报仅用于确认删除，不预置到干净 ZIP。
        window.switch_page(window.home_page)
        window.start_fetch()
        wait(lambda: window.worker is None)
        app.AnalysisAttempt.run = original_run
        window.start_analysis()
        wait(lambda: window.worker is None)
        window.switch_page(window.history_page)
        wait(lambda: window.history_worker is None)
        deleted_id = window.history_page.model.rows[0].record_id
        assert window.history_page.model.rowCount() == 3

        def answer(value):
            dialog = application.activeModalWidget()
            assert isinstance(dialog, QMessageBox)
            assert dialog.standardButton(dialog.defaultButton()) == QMessageBox.StandardButton.No
            if value == QMessageBox.StandardButton.No:
                capture("10d-history-delete-confirm")
                image = dialog.grab()
                destination = paths.runtime_path(source_root or pipeline.PROJECT_ROOT, "work", "history-delete-confirm.png")
                assert image.save(str(destination))
            dialog.button(value).click()

        QTimer.singleShot(150, lambda: answer(QMessageBox.StandardButton.No))
        window.delete_history_record(deleted_id)
        assert window.history_page.model.rowCount() == 3 and window.history_store.read(deleted_id) is not None
        QTimer.singleShot(150, lambda: answer(QMessageBox.StandardButton.Yes))
        window.delete_history_record(deleted_id)
        wait(lambda: window.history_worker is None)
        assert window.history_page.model.rowCount() == 2 and window.history_store.read(deleted_id) is None
        capture("10e-history-deleted")
        window.resize(850, 680)
        window.history_page.setFont(QFont("Microsoft YaHei UI", 12))
        window.history_page.list_view.setFont(QFont("Microsoft YaHei UI", 12))
        # 仅验证副本覆盖 Fluent 的列表字体，确保较大字体场景实际生效。
        window.history_page.list_view.setStyleSheet(window.history_page.list_view.styleSheet() +
            "QListView {font-family: 'Microsoft YaHei UI'; font-size: 12pt;}")
        window.history_page.list_view.doItemsLayout()
        application.processEvents()
        for i, row in enumerate(window.history_page.model.rows):
            option = QStyleOptionViewItem()
            window.history_page.list_view.initViewItemOption(option)
            assert option.font.pointSize() == 12
            option.rect = window.history_page.list_view.visualRect(window.history_page.model.index(i, 0))
            _, title, caption = window.history_page.delegate.text_layout(option, row, option.rect.width())
            assert caption.bottom() < option.rect.height() - 5
            assert title.bottom() < caption.top()
            assert caption.right() < window.history_page.delegate.delete_rect(option.rect).left()
        capture("10f-history-large-font-small-window")
        window.open_history_record(first)
        wait(lambda: window.history_worker is None)
        capture("10g-history-detail-small-window")
        window.resize(1060, 820)
        # 保存故障只发生一次；正常分析和当次日报仍完成。
        window.switch_page(window.home_page)
        window.start_fetch()
        wait(lambda: window.worker is None)
        calls = []

        def fail_save(result):
            calls.append(result.operation_id)
            raise HistoryError(make_issue("AKD-HISTORY-SAVE_FAILED"))

        window.history_store.save = fail_save
        window.start_analysis()
        wait(lambda: window.worker is None)
        assert len(calls) == 1 and "分析成功，历史保存失败" in window.status.text()
        assert "arXivKaleid Desktop 日报" in window.report.toPlainText()
        assert window.history_store.read(calls[0]) is None
        capture("10h-history-save-failed")
        records = window.history_store.list_records()
        assert len(records) == 2
        return {"exact_generated_reports": True, "dates_from_snapshot": True,
                "normal_zero_saved": True, "delete_default_cancel": True,
                "delete_committed": True, "save_failure_keeps_success": True, "deleted_id": deleted_id,
                "records": [{"id": row.record_id, "recommendation_count": row.recommendation_count,
                             "candidate_date": row.candidate_date.isoformat(),
                             "fetch_completed_at_us": row.fetch_completed_at_us,
                             "completed_at_us": row.completed_at_us,
                             "sha256": hashlib.sha256(window.history_store.read(row.record_id).markdown.encode("utf-8")).hexdigest()}
                            for row in records]}
    finally:
        app.AnalysisAttempt.run = original_run
        window.history_store.save = original_save


def loaded_libraries(root):
    """只报告相对文件名；拒绝从 Conda、源码或其他开发目录加载 DLL。"""
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.GetCurrentProcess.restype = ctypes.c_void_p
    kernel.K32EnumProcessModules.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p]
    kernel.GetModuleFileNameW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_uint32]
    modules = (ctypes.c_void_p * 2048)()
    needed = ctypes.c_uint32()
    assert kernel.K32EnumProcessModules(kernel.GetCurrentProcess(), modules, ctypes.sizeof(modules), ctypes.byref(needed))
    assert needed.value <= ctypes.sizeof(modules)
    windows = Path(os.environ['SystemRoot']).resolve()
    records = []
    for module in modules[:needed.value // ctypes.sizeof(ctypes.c_void_p)]:
        buffer = ctypes.create_unicode_buffer(32768)
        assert kernel.GetModuleFileNameW(module, buffer, len(buffer))
        path = Path(buffer.value).resolve()
        if path.is_relative_to(root):
            records.append(path.relative_to(root).as_posix())
        elif not path.is_relative_to(windows):
            (root / 'runtime/work/external-library-name.txt').write_text(path.name, encoding='utf-8')
            raise RuntimeError('external_library_loaded:' + path.name)
    return sorted(records)


def memory_bytes():
    class Counters(ctypes.Structure):
        _fields_ = [('cb', ctypes.c_uint32), ('faults', ctypes.c_uint32),
                    *[(name, ctypes.c_size_t) for name in
                      ('peak_ws', 'ws', 'peak_paged', 'paged', 'peak_nonpaged', 'nonpaged', 'pagefile', 'peak_pagefile', 'private')]]
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.GetCurrentProcess.restype = ctypes.c_void_p
    kernel.K32GetProcessMemoryInfo.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32]
    values = Counters()
    values.cb = ctypes.sizeof(values)
    assert kernel.K32GetProcessMemoryInfo(kernel.GetCurrentProcess(), ctypes.byref(values), values.cb)
    return values.private


def exercise_requirements(application, window, capture, output):
    """真实纯文本输入、模态对话框和保存失败；只供隔离验证副本使用。"""
    from desktop.research_requirements import RequirementsError
    page = window.prompt_page
    window.switch_page(page)
    page.go_back()
    capture('13-prompts-overview')

    def answer(text, name):
        def act():
            dialog = application.activeModalWidget()
            assert isinstance(dialog, QMessageBox), 'requirements_dialog_missing'
            assert dialog.grab().save(str(output / (name + '-dialog.png')))
            button = next(b for b in dialog.buttons() if b.text() == text or
                          (text == 'yes' and dialog.standardButton(b) == QMessageBox.StandardButton.Yes) or
                          (text == 'no' and dialog.standardButton(b) == QMessageBox.StandardButton.No))
            button.click()
        QTimer.singleShot(120, act)

    saved = {}
    for stage in ('round1', 'round2'):
        page.open_round(stage)
        assert not page.saved.custom and page.editor.isReadOnly()
        capture('14-' + stage + '-default')
        page.begin_edit()
        page.editor.clear()
        page.editor.setFocus()
        # 通过 Qt 输入法提交事件验证真实控件处理中文，避免仅 setPlainText 冒充输入。
        event = QInputMethodEvent()
        text = '离线合成研究要求 ' + stage + '：重点关注黑洞阴影和偏振图像。\n排除仅关键词相关论文。'
        event.setCommitString(text)
        QCoreApplication.sendEvent(page.editor, event)
        assert page.editor.toPlainText() == text
        assert page.dirty and not page.saved.custom
        capture('15-' + stage + '-editing')
        original = page.store.save
        def fail(*_args, **_kwargs):
            raise RequirementsError('研究要求保存失败；原来的生效内容未改变。')
        page.store.save = fail
        try:
            assert not page.save() and page.dirty and not page.saved.custom
            capture('16-' + stage + '-save-failed')
        finally:
            page.store.save = original
        answer('留在此页', '17-' + stage + '-unsaved')
        page.go_back()
        assert page.stack.currentIndex() == 1 and page.dirty
        assert page.save() and page.saved.custom
        capture('18-' + stage + '-custom')
        page.begin_edit()
        page.editor.appendPlainText('应取消的草稿')
        page.cancel()
        assert page.editor.toPlainText() == text
        answer('no', '19-' + stage + '-restore-cancel')
        assert not page.restore_default() and page.saved.custom
        answer('yes', '20-' + stage + '-restore-confirm')
        assert page.restore_default() and not page.saved.custom
        page.begin_edit()
        page.editor.setPlainText(text)
        assert page.save()
        saved[stage] = hashlib.sha256(text.encode('utf-8')).hexdigest()
    # Fluent 控件有独立字体，不能只放大父页面后宣称较大字体已验证。
    controls = (page.editor, page.purpose, page.state, page.message, page.back_button,
                page.edit_button, page.save_button, page.cancel_button, page.restore_button)
    normal_fonts = [(widget, widget.font()) for widget in controls]
    for widget in controls:
        widget.setFont(QFont('Microsoft YaHei UI', 12))
    window.resize(850, 680)
    page.begin_edit()
    page.editor.appendPlainText('\n'.join('较大字体滚动验证：关注强引力偏振图像。' for _ in range(60)))
    capture('21-prompts-minimum-large-font')
    assert all(widget.font().pointSizeF() == 12 for widget in controls)
    assert page.editor.height() >= 160
    assert page.restore_button.mapTo(page, page.restore_button.rect().bottomRight()).y() < page.height()
    assert page.editor.verticalScrollBar().maximum() > 0
    page.editor.verticalScrollBar().setValue(page.editor.verticalScrollBar().maximum())
    capture('21b-prompts-scroll')
    page.cancel()
    for widget, font in normal_fonts:
        widget.setFont(font)
    window.resize(1060, 820)
    page.go_back()
    capture('22-prompts-custom-overview')
    window.switch_page(window.home_page)
    return {'saved_sha256': saved, 'chinese_input_method_event': True, 'cancel': True,
            'restore_confirmed': True, 'restore_cancelled': True, 'dirty_protection': True,
            'save_failure_kept_draft': True, 'minimum_large_font': True}


def exercise(application, window, root, synthetic_key):
    """通过真实窗口槽、真实 QThread 与合成计算驱动状态，业务算法不改变。"""
    output = root / 'runtime/work/visual-qa'
    output.mkdir()
    gate = threading.Event()
    captures = []
    # 多显示器原生 DPI 不同；诊断窗口固定到主屏，避免启动位置改变倍率基线。
    # 只移动本次隔离窗口，不修改 Windows 显示设置或正式应用的窗口行为。
    screen = application.primaryScreen()
    window.windowHandle().setScreen(screen)
    origin = screen.availableGeometry().topLeft()
    window.move(origin.x() + 40, origin.y() + 40)
    window.resize(1060, 820)

    def pump(seconds=0.18):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            application.processEvents()
            time.sleep(0.01)

    def wait(predicate):
        end = time.monotonic() + 10
        while not predicate() and time.monotonic() < end:
            pump(0.02)
        assert predicate(), 'portable_state_timeout'

    def capture(name):
        pump()
        image = window.grab()
        assert not image.isNull() and image.save(str(output / (name + '.png')))
        assert window.home_page.report_stack.height() >= 70
        card = window.home_page.layout().itemAt(0).widget()
        assert window.fetch_button.geometry().bottom() < card.height()
        if window.task_panel.isVisible():
            assert window.task_panel.geometry().top() > card.geometry().bottom()
        captures.append({'state': name, 'logical_size': [window.width(), window.height()],
                         'pixel_size': [image.width(), image.height()], 'dpr': image.devicePixelRatio(),
                         'screen': window.screen().name(),
                         'report_height': window.home_page.report_stack.height()})
        popup = window.date_picker
        if popup.isVisible():
            available = window.rect().translated(window.mapToGlobal(QPoint(0, 0))).intersected(window.screen().availableGeometry())
            assert available.contains(popup.geometry()), 'calendar_outside_window'
            assert popup.grab().save(str(output / (name + '-popup.png')))
            captures[-1]['calendar_geometry'] = [popup.x(), popup.y(), popup.width(), popup.height()]

    def fetch(day, progress, **kwargs):
        progress(ProgressEvent(task_type='fetch', stage='category', state='running',
                               message=f'正在查询北京时间 {day.isoformat()} · 分类 2 / 3：astro-ph.HE\n当前已获取 67 条有效记录',
                               current_date=day, category='astro-ph.HE', category_index=2, category_total=3, processed=67))
        assert gate.wait(15)
        return synthetic_snapshot(day)

    def analyze(attempt, _key, progress):
        for stage, state, message in [('round1', 'completed', 'Round 1 完成 · 有效入围 10 篇'),
                                      ('pdf', 'completed', 'PDF 下载完成 · 已处理 10 / 10'),
                                      ('fulltext', 'running', '全文提取中 · 已处理 4 / 9')]:
            progress(ProgressEvent(task_type='analysis', stage=stage, state=state, message=message))
        assert gate.wait(15)
        for stage, message in [('fulltext', '全文提取完成 · 已处理 9 / 9'),
                               ('round2', 'Round 2 完成 · 最终推荐 4 篇'), ('report', '日报生成完成')]:
            progress(ProgressEvent(task_type='analysis', stage=stage, state='completed', message=message))
        return synthetic_analysis_result(attempt)

    originals = app.fetch_candidates_for_date, app.AnalysisAttempt.run, QDesktopServices.openUrl
    app.fetch_candidates_for_date = fetch
    app.AnalysisAttempt.run = analyze
    opened = []
    QDesktopServices.openUrl = lambda url: opened.append(url.toString()) or True
    try:
        window.report.clear()
        window.snapshot = None
        window.analyze_button.setEnabled(False)
        window.task_panel.hide()
        window.show()
        capture('01-initial')
        requirements = exercise_requirements(application, window, capture, output)
        window.switch_page(window.history_page)
        wait(lambda: window.history_worker is None)
        assert window.history_page.model.rowCount() == 0
        capture('01e-history-empty')
        window.switch_page(window.home_page)
        window.open_date_picker()
        capture('01b-calendar')
        today = window.beijing_today()
        earliest = qt_date(today).addDays(-365).toPython()
        window.date_picker.prepare(today, earliest)
        assert not window.date_picker.previous.isEnabled()
        window.date_picker.calendar.showPreviousMonth()
        assert (window.date_picker.calendar.yearShown(), window.date_picker.calendar.monthShown()) == (earliest.year, earliest.month)
        capture('01c-calendar-earliest')
        window.date_picker._choose(qt_date(today).addDays(-3))
        assert window.worker is None and window.snapshot is None
        capture('01d-selected-date')
        window.start_fetch()
        wait(lambda: '67' in window.fetch_progress_text.text())
        assert window.fetch_progress.maximum() == 0
        capture('02-fetching')
        gate.set()
        wait(lambda: window.worker is None)
        assert not window.task_panel.expanded and window.analyze_button.isEnabled()
        capture('03-fetched')
        window.task_panel.set_expanded(True)
        capture('04-fetch-details')
        gate.clear()
        window.analysis_notice_accepted = True  # 首次默认拒绝在独立检查中验证。
        window.start_analysis()
        wait(lambda: '4 / 9' in window.analysis_progress_text.text())
        assert not window.api_key.isEnabled()
        capture('05-analyzing')
        window.switch_page(window.prompt_page)
        window.prompt_page.open_round('round1')
        assert window.prompt_page.busy and not window.prompt_page.edit_button.isEnabled()
        capture('05b-prompts-locked')
        window.switch_page(window.settings_page)
        capture('06-settings-locked')
        window.switch_page(window.home_page)
        gate.set()
        wait(lambda: window.worker is None)
        assert window.snapshot.analysis_attempted and not window.task_panel.expanded
        assert 'Round 2 最终推荐' in window.report.toPlainText()
        capture('07-completed')
        window.task_panel.set_expanded(True)
        capture('08-analysis-details')
        window.switch_page(window.settings_page)
        # 通过实际 editingFinished 信号走自动保存，且只使用合成值。
        window.api_key.setText(synthetic_key)
        window.api_key.setModified(True)
        window.api_key.editingFinished.emit()
        assert not window.api_key.isModified() and window.secret_store.load() == synthetic_key
        capture('09-settings')
        history = exercise_history(application, window, capture, wait)
        window.switch_page(window.home_page)
        window.task_panel.set_expanded(False)
        window.resize(850, 680)
        capture('11-minimum-size')
        window.task_panel.set_expanded(True)
        capture('11b-minimum-expanded')
        window.home_page.layout().itemAt(0).widget().setFont(QFont('Microsoft YaHei UI', 12))
        window.open_date_picker()
        capture('11c-minimum-calendar')
        window.date_picker.hide()
        window.resize(1060, 820)
        window.show_issue(make_issue('AKD-FETCH-UNEXPECTED'))
        window.task_panel.finish(failed=True)
        capture('12-failure')
        # 每个安全停止独立还原阶段事实，避免沿用上一场景的红叉或成功日报。
        window.report.clear()
        window.task_panel.begin('analysis')
        window.reset_analysis_progress()
        window.show_issue(make_issue('AKD-R1-INPUT_LIMIT'))
        window.status.setText(make_issue('AKD-R1-INPUT_LIMIT').reason)
        window.task_panel.finish(failed=True)
        capture('12b-round1-safety-stop')
        window.task_panel.begin('analysis')
        window.reset_analysis_progress()
        for stage, message in [('round1', 'Round 1 完成 · 有效入围 10 篇'),
                               ('pdf', 'PDF 下载完成 · 已处理 10 / 10'),
                               ('fulltext', '全文提取完成 · 已处理 9 / 9')]:
            window.handle_progress(ProgressEvent(task_type='analysis', stage=stage,
                                                 state='completed', message=message))
        window.show_issue(make_issue('AKD-R2-COST_LIMIT'))
        window.status.setText(make_issue('AKD-R2-COST_LIMIT').reason)
        window.task_panel.finish(failed=True)
        assert window.analysis_steps['round1'].text().startswith('✓')
        assert window.analysis_steps['round2'].text().startswith('✕')
        assert 'Round 1 已发生的结果与费用仍保留' in window.diagnostic_text.text()
        capture('12c-round2-safety-stop')
        # 保存失败使用隔离的代理，不更改 Windows ACL 或系统配置。
        store = window.secret_store
        class FailingStore:
            def save(self, value):
                raise SecretError('synthetic_save_failure')
        window.secret_store = FailingStore()
        window.api_key.setModified(True)
        assert not window.save_key() and window.api_key.isModified()
        window.secret_store = store
        assert window.save_key()
        for url in ('file:///synthetic', 'http://arxiv.org/abs/2610.00001', 'https://example.invalid/'):
            window.open_report_link(QUrl(url))
        assert not opened
        window.open_report_link(QUrl('https://arxiv.org/abs/2610.00001'))
        assert opened == ['https://arxiv.org/abs/2610.00001']
        opened.clear()
        window.open_logs_button.clicked.emit()
        assert opened == [QUrl.fromLocalFile(str(root / 'runtime/logs')).toString()]
    finally:
        gate.set()
        if window.worker is not None:
            window.worker.wait(20000)
            pump()
        wait(lambda: window.history_worker is None and not window._history_jobs)
        app.fetch_candidates_for_date, app.AnalysisAttempt.run, QDesktopServices.openUrl = originals
    # 预热后重复创建和释放窗口，检查持有对象与 Windows 内存使用；不删除运行数据。
    window.hide()
    samples = []
    for _ in range(12):
        child = app.DesktopWindow(window.secret_store)
        assert child.api_key.text() == synthetic_key
        child.close()
        child.deleteLater()
        del child
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        pump(0.02)
        gc.collect()
        samples.append(memory_bytes())
    assert samples[-1] - samples[3] < 32 * 1024 * 1024, 'portable_window_memory_growth'
    assert not gc.garbage
    return {'captures': captures, 'history': history, 'requirements': requirements, 'qt_platform': application.platformName(),
            'screen': screen.name(),
            'style': application.style().objectName(), 'loaded_libraries': loaded_libraries(root),
            'autosave': True, 'save_failure': True, 'key_locked': True, 'safe_links': True, 'log_directory_action': True,
            'lifecycle_cycles': len(samples), 'private_memory_bytes': samples, 'gc_garbage': len(gc.garbage)}
