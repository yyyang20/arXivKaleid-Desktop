# Copyright (c) 2026 yyyang20
# SPDX-License-Identifier: GPL-3.0-only
# See LICENSE in the project root for the full license text.

"""仅用于全新发行副本的零模型费用诊断，不接受用户 Key。"""
import io
import ctypes
import json
import hashlib
import os
import socket
import urllib.request
from datetime import date, datetime, timezone
from pathlib import Path
import sqlite3
import subprocess
import sys
import time

from desktop import paths, pipeline


SYNTHETIC_KEY = 'synthetic-portable-check-not-an-api-key'


def disable_diagnostic_ime():
    """仅隔离当前诊断线程的输入法；不切换系统输入法或影响正式 GUI。"""
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.GetCurrentThreadId.argtypes = []
    kernel.GetCurrentThreadId.restype = ctypes.c_uint32
    imm = ctypes.WinDLL('imm32', use_last_error=True)
    imm.ImmDisableIME.argtypes = [ctypes.c_uint32]
    imm.ImmDisableIME.restype = ctypes.c_int
    # 必须先于诊断窗口创建，防止第三方输入法 DLL 干扰严格来源检查。
    if not imm.ImmDisableIME(kernel.GetCurrentThreadId()):
        raise RuntimeError('diagnostic_ime_isolation_failed')


def run_recovery():
    """仅接受验证器新建副本的随机诊断令牌，不对用户 runtime 做恢复探测。"""
    root = paths.application_root(pipeline.PROJECT_ROOT)
    token = os.environ.get('ARXIVKALEID_DIAGNOSTIC_TOKEN', '')
    if not paths.frozen() or len(token) != 32:
        return 2
    marker = root / 'runtime/work/portable-check.json'
    if not marker.is_file():
        return 2
    previous = json.loads(marker.read_text(encoding='utf-8'))
    if not previous.get('ok') or previous.get('diagnostic_token_sha256') != hashlib.sha256(token.encode()).hexdigest():
        return 2
    result = {'ok': False}
    try:
        disable_diagnostic_ime()
        paths.configure_timezone(pipeline.PROJECT_ROOT)
        from PySide6.QtWidgets import QApplication
        from desktop import app as desktop_app
        application = QApplication([])
        window = desktop_app.DesktopWindow()
        assert window.api_key.text() == SYNTHETIC_KEY and not window.api_key.isModified()
        window.show()
        application.processEvents()
        assert window.isVisible()
        if previous.get('visual_qa'):
            # 跨进程验证持久化后的原始正文和删除；不生成或重新分析日报。
            from desktop.history import HistoryStore
            store = HistoryStore()
            expected = previous['visual_qa']['history']
            assert store.read(expected['deleted_id']) is None
            assert len(store.list_records()) == len(expected['records'])
            window.switch_page(window.history_page)

            def wait_history():
                end = time.monotonic() + 10
                while window.history_worker is not None and time.monotonic() < end:
                    application.processEvents()
                    time.sleep(0.01)
                assert window.history_worker is None

            wait_history()
            for item in expected['records']:
                record = store.read(item['id'])
                assert record.summary.recommendation_count == item['recommendation_count']
                assert record.summary.candidate_date.isoformat() == item['candidate_date']
                assert record.summary.fetch_completed_at_us == item['fetch_completed_at_us']
                assert record.summary.completed_at_us == item['completed_at_us']
                assert hashlib.sha256(record.markdown.encode('utf-8')).hexdigest() == item['sha256']
                window.open_history_record(item['id'])
                wait_history()
                assert 'arXivKaleid Desktop 日报' in window.history_page.report.toPlainText()
            result['history_restored_exactly'] = True
            result['history_dates_restored_exactly'] = True
            result['deleted_history_stays_deleted'] = True
        window.close()
        result.update(ok=True, synthetic_dpapi_recovered=True)
    except Exception as exc:
        result['failure_type'] = type(exc).__name__
    (root / 'runtime/work/recovery-check.json').write_text(json.dumps(result), encoding='utf-8')
    return 0 if result['ok'] else 1


def run(*, network=False, visual=False):
    # 已使用过的目录绝不运行诊断，避免读取任何用户 Secret。
    root = paths.application_root(pipeline.PROJECT_ROOT)
    runtime = paths.runtime_path(pipeline.PROJECT_ROOT)
    if not paths.frozen() or runtime.exists():
        return 2
    paths.prepare_runtime(pipeline.PROJECT_ROOT)
    report = {'ok': False, 'model_http_attempts': 0, 'model_cost_cny': '0'}
    try:
        disable_diagnostic_ime()
        report['diagnostic_ime_thread_only'] = True
        paths.configure_timezone(pipeline.PROJECT_ROOT)
        from zoneinfo import ZoneInfo
        assert str(ZoneInfo('Asia/Shanghai')) == 'Asia/Shanghai'
        import deepseek_client
        # 诊断进程中的两个模型入口均硬失败。
        def forbidden(*args, **kwargs):
            raise RuntimeError('model_forbidden_in_portable_check')
        deepseek_client.DeepSeekClient.request_json = forbidden
        deepseek_client.DeepSeekClient.request_responses_json = forbidden
        if not network:
            # 禁止所有 Python 网络入口；curl 仅允许本地 --version 检查。
            urllib.request.urlopen = forbidden
            socket.socket.connect = forbidden
            socket.socket.connect_ex = forbidden
            original_popen = subprocess.Popen
            def offline_popen(command, *args, **kwargs):
                if not isinstance(command, (list, tuple)) or list(command[1:]) != ['--disable', '--version']:
                    raise RuntimeError('network_subprocess_forbidden_in_portable_check')
                return original_popen(command, *args, **kwargs)
            subprocess.Popen = offline_popen
        import run_round2
        config, resources, profile, prompt = run_round2.read_round2_context(paths.resource_root(pipeline.PROJECT_ROOT))
        assert config['versions']['research_profile_version'] == profile['profile_version'] and prompt
        import main
        # 在冻结程序中执行非空证据路径，防止缺失标准库导入被零结果掩盖。
        evidence = [{'source': 'metadata_title', 'quote': 'Synthetic paper'},
                    {'source': 'metadata_abstract', 'quote': 'Ｓｙｎｔｈｅｔｉｃ　abstract'}]
        checked, status, discarded = main.validate_optional_round1_evidence(
            evidence, {'title': 'Ｓｙｎｔｈｅｔｉｃ　paper', 'summary': 'Synthetic\n  abstract'})
        assert checked == [{'source': 'metadata_title', 'quote': 'Synthetic paper'},
                           {'source': 'metadata_abstract', 'quote': 'Synthetic abstract'}]
        assert status == 'valid' and discarded == 0
        report['round1_nonempty_evidence'] = True
        payload = json.loads(main.build_round2_messages(prompt, profile, [], config)[1]['content'])
        assert 'research_profile' not in payload
        from desktop.secrets import SecretStore
        store = SecretStore()
        store.save('synthetic-portable-check-not-an-api-key')
        assert store.load() == 'synthetic-portable-check-not-an-api-key'
        from desktop.analysis import lock_work_directory, initialize_work_paths
        with lock_work_directory():
            database, fulltext_database = initialize_work_paths()
            conn = main.init_database(database)
            try:
                assert conn.execute('PRAGMA user_version').fetchone()[0] == main.DESKTOP_SCHEMA_VERSION
                assert not {'scores', 'feedback', 'pdf_sections', 'daily_reports'} & {
                    row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
                }
                import build_round2_inputs
                build_round2_inputs.validate_required_schema(conn)
            finally:
                conn.close()
            import round2_fulltext_state
            conn = sqlite3.connect(fulltext_database)
            try:
                round2_fulltext_state.initialize_fulltext_schema(conn)
                round2_fulltext_state.validate_fulltext_schema(conn)
            finally:
                conn.close()
        import pypdf
        output = io.BytesIO()
        writer = pypdf.PdfWriter()
        writer.add_blank_page(width=100, height=100)
        writer.write(output)
        assert len(pypdf.PdfReader(io.BytesIO(output.getvalue())).pages) == 1
        curl = paths.curl_executable(pipeline.PROJECT_ROOT)
        version = subprocess.check_output([curl, '--disable', '--version'], timeout=15, creationflags=0x08000000).decode()
        assert 'https' in version.split('Protocols:')[-1].splitlines()[0]
        from PySide6.QtCore import QTimer
        from PySide6.QtWidgets import QApplication, QMessageBox
        from desktop import app as desktop_app
        from desktop.diagnostics import DesktopDiagnostics
        from desktop.pipeline import CandidateSnapshot
        application = QApplication([])
        diagnostics = DesktopDiagnostics(pipeline.PROJECT_ROOT)
        assert diagnostics.persistent
        assert diagnostics.log_directory == root / 'runtime/logs'
        diagnostics.event(
            operation_id=diagnostics.operation_id(), operation_type='portable_check',
            stage='startup', state='complete',
        )
        assert diagnostics.relative_log_path.startswith('runtime/logs/desktop-')
        assert diagnostics.relative_log_path.endswith('.jsonl')
        window = desktop_app.DesktopWindow(diagnostics=diagnostics)
        assert window.api_key.text() == SYNTHETIC_KEY
        assert window.open_logs_button.isEnabled()
        assert window.pages.count() == 3 and window.task_panel.isHidden()
        assert window.settings_page.isAncestorOf(window.api_key)
        assert desktop_app.__version__ in window.settings_page.version_label.text()
        from PySide6.QtGui import QIcon
        icon = QIcon(':/qfluentwidgets/images/icons/Home_black.svg')
        assert not icon.isNull() and not icon.pixmap(24, 24).isNull()
        window.switch_page(window.history_page)
        assert window.pages.currentWidget() is window.history_page
        window.switch_page(window.home_page)
        window.report.setMarkdown('# Portable verification\n\n**Markdown rendered**')
        assert 'Markdown rendered' in window.report.toPlainText()
        assert not window.fetch_progress.isTextVisible()
        assert window.fetch_progress.maximum() == 1
        assert tuple(window.analysis_steps) == tuple(
            stage for stage, _title in desktop_app.ANALYSIS_STAGES
        )
        assert all(label.text().startswith('○') for label in window.analysis_steps.values())

        # 在冻结 EXE 内直接验证默认拒绝路径：不消费快照，不创建分析尝试或线程。
        notice_calls = []
        original_question = QMessageBox.question
        original_attempt = desktop_app.AnalysisAttempt
        def refuse_notice(parent, title, text, buttons, default_button):
            notice_calls.append((title, text, buttons, default_button))
            return QMessageBox.StandardButton.No
        def forbidden_attempt(*args, **kwargs):
            raise RuntimeError('analysis_attempt_forbidden_after_refusal')
        try:
            QMessageBox.question = refuse_notice
            desktop_app.AnalysisAttempt = forbidden_attempt
            snapshot = CandidateSnapshot(
                candidate_date=date(2000, 1, 1),
                completed_at=datetime(2000, 1, 1, tzinfo=timezone.utc),
                raw_count=0,
                unique_count=0,
                papers=(),
            )
            window.snapshot = snapshot
            window.api_key.setText('synthetic-portable-check-not-an-api-key')
            window.api_key.setModified(True)
            window.analyze_button.setEnabled(True)
            window.start_analysis()
            assert len(notice_calls) == 1
            assert notice_calls[0][0] == '分析前告知'
            assert notice_calls[0][1] == desktop_app.ANALYSIS_NOTICE
            assert notice_calls[0][3] == QMessageBox.StandardButton.No
            assert not snapshot.analysis_attempted
            assert window.worker is None
            assert window.analyze_button.isEnabled()
            assert '未调用模型' in window.status.text()
        finally:
            QMessageBox.question = original_question
            desktop_app.AnalysisAttempt = original_attempt
            window.api_key.setModified(False)
        window.show()
        if visual:
            from portable_visual import exercise
            report['visual_qa'] = exercise(application, window, root, SYNTHETIC_KEY)
        report.update(resources=True, timezone=True, dpapi=True, sqlite=True, pypdf=True,
                      gui=True, curl=Path(curl).relative_to(root).as_posix(),
                      curl_version=version.splitlines()[0], runtime='runtime/',
                      progress_ui=True, fluent_pages=True, fluent_svg=True,
                      analysis_notice=True, analysis_refusal_no_snapshot_claim=True,
                      analysis_refusal_no_worker=True)
        if network:
            # 使用真正 GUI/QThread 候选入口；分析按钮不会被点击。
            def done():
                if window.worker is not None:
                    QTimer.singleShot(100, done)
                    return
                report['arxiv'] = window.snapshot is not None
                if window.snapshot:
                    report['candidate_date'] = window.snapshot.candidate_date.isoformat()
                    report['candidate_count'] = window.snapshot.unique_count
                    report['ok'] = True
                window.close()
                application.quit()
            QTimer.singleShot(100, window.start_fetch)
            QTimer.singleShot(200, done)
        else:
            report['ok'] = True
            def close_diagnostic():
                window.close()
                # 生命周期验收会隐藏窗口，不能依赖 lastWindowClosed 自动退出。
                application.quit()
            QTimer.singleShot(500, close_diagnostic)
        application.exec()
        diagnostics.close()
        log_files = tuple((root / 'runtime/logs').glob('desktop-*.jsonl'))
        assert len(log_files) == 1 and 'portable_check' in log_files[0].read_text(encoding='utf-8')
        report.update(diagnostics_jsonl=True, diagnostics_runtime_only=True)
        token = os.environ.get('ARXIVKALEID_DIAGNOSTIC_TOKEN', '')
        report['diagnostic_token_sha256'] = hashlib.sha256(token.encode()).hexdigest()
    except Exception as exc:
        # 后置日志等检查失败也必须失败关闭；早期通过不能覆盖最终失败。
        report['ok'] = False
        report['failure_type'] = type(exc).__name__
        # 只记录源码位置，绝不写底层消息、局部变量或绝对路径。
        trace = exc.__traceback__
        report['failure_sites'] = []
        while trace is not None:
            report['failure_sites'].append({'module': Path(trace.tb_frame.f_code.co_filename).name,
                                           'function': trace.tb_frame.f_code.co_name, 'line': trace.tb_lineno})
            trace = trace.tb_next
    paths.runtime_path(pipeline.PROJECT_ROOT, 'work/portable-check.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    return 0 if report['ok'] else 1
