# Copyright (c) 2026 yyyang20
# SPDX-License-Identifier: GPL-3.0-only
# See LICENSE in the project root for the full license text.

"""冻结诊断专用合成状态；不提供正式业务入口、不引入测试库。"""
from datetime import date, datetime, timezone
import ctypes
import gc
import os
from pathlib import Path
import threading
import time

from PySide6.QtCore import QCoreApplication, QEvent, QUrl
from PySide6.QtGui import QDesktopServices
from desktop import app
from desktop.errors import make_issue
from desktop.progress import ProgressEvent
from desktop.secrets import SecretError


MARKDOWN = """# arXiv 日报（离线合成验收数据）

## Round 2 最新推荐（4 篇）

本内容仅用于冻结 GUI 验收，不代表真实抓取或模型分析。

1. **Strong gravity imaging — synthetic paper**
   - 内容标签：成像
   - 推荐理由：检查中文、英文、粗体与安全链接的 Markdown 渲染。
   - [arXiv](https://arxiv.org/abs/2610.00001)

## Round 1 入围详情

所有篇数为诊断合成数据；未发送任何业务网络请求。
"""


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


def exercise(application, window, root, synthetic_key):
    """通过真实窗口槽、真实 QThread 与合成计算驱动状态，业务算法不改变。"""
    output = root / 'runtime/work/visual-qa'
    output.mkdir()
    gate = threading.Event()
    captures = []

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
        assert window.home_page.report_stack.height() >= 140
        captures.append({'state': name, 'logical_size': [window.width(), window.height()],
                         'pixel_size': [image.width(), image.height()], 'dpr': image.devicePixelRatio(),
                         'report_height': window.home_page.report_stack.height()})

    def fetch(day, progress, **kwargs):
        progress(ProgressEvent(task_type='fetch', stage='category', state='running',
                               message='正在检查 UTC 2000-01-01 · 分类 2 / 3：astro-ph.HE\n当前已获取 67 条有效记录',
                               current_date=day, category='astro-ph.HE', category_index=2, category_total=3, processed=67))
        assert gate.wait(15)
        return app.CandidateSnapshot(date(2000, 1, 1), datetime(2000, 1, 1, tzinfo=timezone.utc),
                                     126, 117, tuple({'arxiv_id': f'2610.{i:05d}', 'version': 1} for i in range(100)))

    def analyze(attempt, _key, progress):
        for stage, state, message in [('round1', 'completed', 'Round 1 完成 · 有效入围 10 篇'),
                                      ('pdf', 'completed', 'PDF 下载完成 · 已处理 10 / 10'),
                                      ('fulltext', 'running', '全文提取中 · 已处理 4 / 9')]:
            progress(ProgressEvent(task_type='analysis', stage=stage, state=state, message=message))
        assert gate.wait(15)
        for stage, message in [('fulltext', '全文提取完成 · 已处理 9 / 9'),
                               ('round2', 'Round 2 完成 · 最终推荐 4 篇'), ('report', '日报生成完成')]:
            progress(ProgressEvent(task_type='analysis', stage=stage, state='completed', message=message))
        return app.AnalysisResult(1, MARKDOWN, 4, operation_id=attempt.operation_id,
                                  snapshot_id=attempt.snapshot.snapshot_id)

    originals = app.fetch_latest_candidates, app.AnalysisAttempt.run, QDesktopServices.openUrl
    app.fetch_latest_candidates = fetch
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
        window.switch_page(window.settings_page)
        capture('06-settings-locked')
        window.switch_page(window.home_page)
        gate.set()
        wait(lambda: window.worker is None)
        assert window.snapshot.analysis_attempted and not window.task_panel.expanded
        assert 'Round 2 最新推荐' in window.report.toPlainText()
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
        window.switch_page(window.history_page)
        capture('10-history')
        window.switch_page(window.home_page)
        window.task_panel.set_expanded(False)
        window.resize(850, 680)
        capture('11-minimum-size')
        window.task_panel.set_expanded(True)
        capture('11b-minimum-expanded')
        window.resize(1060, 820)
        window.show_issue(make_issue('AKD-FETCH-UNEXPECTED'))
        window.task_panel.finish(failed=True)
        capture('12-failure')
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
        app.fetch_latest_candidates, app.AnalysisAttempt.run, QDesktopServices.openUrl = originals
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
    return {'captures': captures, 'qt_platform': application.platformName(),
            'style': application.style().objectName(), 'loaded_libraries': loaded_libraries(root),
            'autosave': True, 'save_failure': True, 'key_locked': True, 'safe_links': True, 'log_directory_action': True,
            'lifecycle_cycles': len(samples), 'private_memory_bytes': samples, 'gc_garbage': len(gc.garbage)}
