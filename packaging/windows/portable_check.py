# Copyright (c) 2026 yyyang20
# SPDX-License-Identifier: GPL-3.0-only
# See LICENSE in the project root for the full license text.

"""仅用于全新发行副本的零模型费用诊断，不接受用户 Key。"""
import io
import json
from datetime import date, datetime, timezone
from pathlib import Path
import sqlite3
import subprocess
import sys

from desktop import paths, pipeline


def run(*, network=False):
    # 已使用过的目录绝不运行诊断，避免读取任何用户 Secret。
    root = paths.application_root(pipeline.PROJECT_ROOT)
    runtime = paths.runtime_path(pipeline.PROJECT_ROOT)
    if not paths.frozen() or runtime.exists():
        return 2
    paths.prepare_runtime(pipeline.PROJECT_ROOT)
    report = {'ok': False, 'model_http_attempts': 0, 'model_cost_cny': '0'}
    try:
        paths.configure_timezone(pipeline.PROJECT_ROOT)
        from zoneinfo import ZoneInfo
        assert str(ZoneInfo('Asia/Shanghai')) == 'Asia/Shanghai'
        import deepseek_client
        # 诊断进程中的两个模型入口均硬失败。
        def forbidden(*args, **kwargs):
            raise RuntimeError('model_forbidden_in_portable_check')
        deepseek_client.DeepSeekClient.request_json = forbidden
        deepseek_client.DeepSeekClient.request_responses_json = forbidden
        import run_round2
        config, resources, profile, prompt = run_round2.read_round2_context(paths.resource_root(pipeline.PROJECT_ROOT))
        assert config['versions']['research_profile_version'] == profile['profile_version'] and prompt
        import main
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
        assert window.open_logs_button.isEnabled()
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
        report.update(resources=True, timezone=True, dpapi=True, sqlite=True, pypdf=True,
                      gui=True, curl=Path(curl).relative_to(root).as_posix(),
                      curl_version=version.splitlines()[0], runtime='runtime/',
                      progress_ui=True,
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
            QTimer.singleShot(500, window.close)
        application.exec()
        diagnostics.close()
        log_files = tuple((root / 'runtime/logs').glob('desktop-*.jsonl'))
        assert len(log_files) == 1 and 'portable_check' in log_files[0].read_text(encoding='utf-8')
        report.update(diagnostics_jsonl=True, diagnostics_runtime_only=True)
    except Exception as exc:
        report['failure_type'] = type(exc).__name__
    paths.runtime_path(pipeline.PROJECT_ROOT, 'work/portable-check.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    return 0 if report['ok'] else 1
