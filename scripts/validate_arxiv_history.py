# Copyright (c) 2026 yyyang20
# SPDX-License-Identifier: GPL-3.0-only
"""获准后的真实 GUI 最新版／历史补查验收；独立 runtime、零 OAI、零模型。"""
import hashlib
import json
import logging
from pathlib import Path
import shutil
import sys
import time
from unittest.mock import patch
from datetime import date, datetime, timedelta
from urllib.parse import urlencode, urlsplit, parse_qs
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import main
from desktop import app, arxiv_versions, pipeline
from desktop.diagnostics import DesktopDiagnostics
from desktop.history import HistoryStore
from desktop.date_policy import selectable_date
from desktop.research_requirements import RequirementsStore
from desktop.version_metadata import VersionStore, encode_paper
from scripts.local_artifacts import ArtifactRun, checked_path, inventory, temporary_environment

PAPER_ID = '2506.15886'
FETCH_LIMIT_SECONDS = 120


class EmptySecretStore:
    def load(self):
        return ''

    def save(self, _value):
        raise RuntimeError('credentials_forbidden_in_history_validation')


def validation_cases(current, today):
    """当日、近期、旧失败日全部实测；期望值来自联网最新版，而非旧版本日映射。"""
    latest_day = arxiv_versions.timestamp(current['updated']).astimezone(ZoneInfo('Asia/Shanghai')).date()
    days = [today, date(2026, 10, 8), date(2026, 8, 3), date(2026, 7, 10)]
    if latest_day not in days:
        days.append(latest_day)
    if any(not selectable_date(d, today) for d in days):
        raise RuntimeError('historical_fixture_outside_gui_range')
    return [(d, current['version'] if d == latest_day else None) for d in days]


def validate(run):
    from PySide6.QtWidgets import QApplication
    import deepseek_client
    from pdf_processing import SelectedPaper, build_online_pdf_url, fetch_pdf_bytes

    def forbidden(*args, **kwargs):
        raise RuntimeError('model_forbidden_in_history_validation')

    # 只隔离运行根、禁用模型；实际窗口、QThread、候选及网络入口保持真实。
    for relative in ('config.json', *json.loads((ROOT / 'config.json').read_text(encoding='utf-8'))['paths'].values()):
        target = checked_path(run.work, relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / relative, target)
    proof = {'ok': False, 'paper_id': PAPER_ID, 'model_http_attempts': 0, 'oai_requests': 0,
             'limit_seconds': FETCH_LIMIT_SECONDS, 'categories': list(pipeline.CATEGORIES), 'cases': []}
    output = run.evidence / 'history-validation.json'

    def save():
        output.write_text(json.dumps(proof, ensure_ascii=False, indent=2), encoding='utf-8')

    calls = []
    original_request = main.fetch_arxiv_metadata

    def observed_request(*args, **kwargs):
        url = kwargs.get('url', args[0] if args else '')
        parsed = urlsplit(url)
        if 'oai' in (parsed.hostname or ''):
            raise AssertionError('oai_request_forbidden')
        params = parse_qs(parsed.query)
        kind = ('AdvancedSearch' if parsed.path == '/search/advanced' else
                'LatestMetadata' if 'id_list' in params else
                'LatestUpdates' if params.get('sortBy') == ['lastUpdatedDate'] else 'FirstSubmissions')
        started = time.monotonic()
        row = {'kind': kind, 'ok': False}
        calls.append(row)
        try:
            data = original_request(*args, **kwargs)
            row.update(ok=True, body_bytes=len(data))
            return data
        finally:
            row['seconds'] = round(time.monotonic()-started, 3)

    diagnostics = DesktopDiagnostics(run.work)
    application = QApplication.instance() or QApplication([])
    window = None
    with patch.object(pipeline, 'PROJECT_ROOT', run.work), \
            patch.object(main, 'fetch_arxiv_metadata', side_effect=observed_request), \
            patch.object(deepseek_client.DeepSeekClient, 'request_json', forbidden), \
            patch.object(deepseek_client.DeepSeekClient, 'request_responses_json', forbidden), \
            patch.object(app.AnalysisAttempt, 'run', forbidden):
        try:
            cache = pipeline.runtime_path('cache', 'arxiv')
            cache.mkdir(parents=True)

            def lookup(identity):
                xml = main.fetch_arxiv_metadata(main.ARXIV_API_URL + '?' + urlencode(
                    {'id_list': identity, 'max_results': 1}), 90, cache, 5, logging.getLogger('history-validation'),
                    subprocess_timeout_margin_seconds=15)
                return arxiv_versions.normalized_papers(xml).papers[0]

            current = lookup(PAPER_ID)
            proof['current_version'] = current['version']
            proof['current_submitted_beijing'] = arxiv_versions.timestamp(current['updated']).astimezone(ZoneInfo('Asia/Shanghai')).isoformat()
            today = datetime.now(ZoneInfo('Asia/Shanghai')).date()
            cases = validation_cases(current, today)
            save()
            window = app.DesktopWindow(EmptySecretStore(), diagnostics,
                history_store=HistoryStore(run.work), requirements_store=RequirementsStore(run.work))
            window.show()
            retained = []

            def fetch(day, expected_version, label, *, fixture_id=PAPER_ID):
                print(f'真实 GUI 抓取：{day} · {label}', flush=True)
                window.select_date(day)
                assert window.refresh_target_date() == day
                before = len(calls)
                started = time.monotonic()
                window.start_fetch()
                worker = window.worker
                while window.worker is not None:
                    application.processEvents()
                    time.sleep(.02)
                elapsed = time.monotonic()-started
                row = {'date': day.isoformat(), 'label': label, 'elapsed_seconds': round(elapsed, 3),
                       'request_count': len(calls)-before, 'requests': calls[before:],
                       'within_limit': elapsed <= FETCH_LIMIT_SECONDS}
                if worker.issue:
                    row['failure_code'] = worker.issue.code
                    proof['cases'].append(row)
                    save()
                    raise AssertionError(worker.issue.code)
                result = window.snapshot
                papers = result.papers if result else ()
                chosen = [p for p in papers if p['arxiv_id'] == fixture_id]
                row.update(candidate_count=len(papers), discovery_pages=result.page_count if result else None,
                           network_count=result.network_count if result else None,
                           history_count=result.reused_count if result else None,
                           fixture_id=fixture_id, expected_version=expected_version,
                           actual_version=chosen[0]['version'] if chosen else None)
                proof['cases'].append(row)
                save()
                assert elapsed <= FETCH_LIMIT_SECONDS, 'fetch_performance_limit_exceeded'
                assert len(chosen) == (0 if expected_version is None else 1), 'fixture_presence_mismatch'
                if chosen:
                    assert chosen[0]['version'] == expected_version
                    retained.append((result, fixture_id, encode_paper(chosen[0])[1]))
                for p in papers:
                    assert arxiv_versions.version_url(p['pdf_url'], 'pdf') == (p['arxiv_id'], p['version'])
                assert window.grab().save(str(run.evidence / f'{len(proof["cases"]):02d}-{day}.png'))
                for snapshot, identity, digest in retained:
                    frozen = next(p for p in snapshot.papers if p['arxiv_id'] == identity)
                    assert encode_paper(frozen)[1] == digest
                save()
                print(json.dumps({k: v for k, v in row.items() if k != 'requests'}, ensure_ascii=False), flush=True)
                run.checkpoint()
                return result

            recent = None
            for day, expected in cases:
                result = fetch(day, expected, 'cold')
                if day == date(2026, 10, 8):
                    recent = result
            # 真实公开论文验证首次提交日；v1 在 GUI 范围外的固定论文不强行扩展日历。
            eligible = [p for p in recent.papers if selectable_date(
                         arxiv_versions.timestamp(p['published']).astimezone(ZoneInfo('Asia/Shanghai')).date(), today)] if recent else []
            assert eligible, 'first_submission_fixture_missing'
            first_fixture = sorted(eligible, key=lambda p: (-p['version'], p['arxiv_id']))[0]
            first_day = arxiv_versions.timestamp(first_fixture['published']).astimezone(ZoneInfo('Asia/Shanghai')).date()
            fetch(first_day, first_fixture['version'], 'first_submission_day', fixture_id=first_fixture['arxiv_id'])

            # 夹具明确来自指定旧版 Atom；不能声称本机过去确实抓过该日期。
            older = lookup(PAPER_ID+'v5')
            old_day = arxiv_versions.timestamp(older['updated']).astimezone(ZoneInfo('Asia/Shanghai')).date()
            assert older['version'] == 5 and old_day == date(2026, 7, 10)
            store = VersionStore(run.work)
            store.save('explicit-old-version-fixture', old_day, ('gr-qc',), [older], 1)
            old_digest = encode_paper(older)[1]
            proof['history_fixture'] = {'synthetic_association': True, 'source': 'explicit_version_atom',
                                       'date': old_day.isoformat(), 'version': 5, 'metadata_sha256': old_digest}
            save()
            recovered = fetch(old_day, current['version'], 'saved_history')
            assert recovered.reused_count >= 1
            assert encode_paper(store.get((PAPER_ID, 5)))[1] == old_digest

            chosen = next(p for p in recovered.papers if p['arxiv_id'] == PAPER_ID)
            url = build_online_pdf_url(SelectedPaper(PAPER_ID, chosen['version'], chosen['title']))
            assert url == chosen['pdf_url']
            data = fetch_pdf_bytes(url)
            proof['latest_pdf'] = {'url': url, 'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()}
            proof.update(ok=True, immutable_snapshots=True, older_versions_preserved=True, requests=calls)
            save()
        except Exception as exc:
            proof['failure_type'] = type(exc).__name__
            proof['requests'] = calls
            save()
            raise
        finally:
            if window is not None:
                if window.worker is not None:
                    window.worker.wait()
                    application.processEvents()
                window.close()
                application.processEvents()
            diagnostics.close()
            inventory(ROOT, run.work)
            for path in diagnostics.log_directory.glob('*.jsonl'):
                run.capture(path, path.name)


if __name__ == '__main__':
    with ArtifactRun('release-check', root=ROOT) as run:
        run.record['purpose'] = 'authorized_pre_freeze_real_gui_latest_history_performance'
        run.save()
        with temporary_environment(run):
            validate(run)
