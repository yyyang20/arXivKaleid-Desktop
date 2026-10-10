# Copyright (c) 2026 yyyang20
# SPDX-License-Identifier: GPL-3.0-only
from __future__ import annotations

import ast
from contextlib import ExitStack, closing
from datetime import date, timedelta
from email.utils import format_datetime
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse
from xml.sax.saxutils import escape

import main
from desktop.diagnostics import DesktopDiagnostics
from desktop import pipeline, arxiv_versions
from desktop.version_metadata import VersionStore, MetadataError
from scripts.local_artifacts import test_scratch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DAY = date(2026, 9, 25)


def paper(number=1, version=1, day=DAY, updated=None):
    identity = f"2609.{number:05d}"
    return dict(id=f"https://arxiv.org/abs/{identity}v{version}", arxiv_id=identity,
        version=version, title="Synthetic paper", authors="Test Author", summary="Synthetic abstract",
        categories="gr-qc", primary_category="gr-qc", published=(updated if version == 1 and updated else f"{day}T01:00:00Z"),
        updated=updated or f"{day}T01:00:00Z", abs_url=f"https://arxiv.org/abs/{identity}v{version}",
        pdf_url=f"https://arxiv.org/pdf/{identity}v{version}")


def feed(papers, *, start=None, total=None, size=None):
    entries = []
    for p in papers:
        cats = ''.join(f"<category term='{c}'/>" for c in p['categories'].split(', '))
        entries.append(f"<entry><id>{p['id']}</id><title>{escape(p['title'])}</title>"
            f"<published>{p['published']}</published><updated>{p['updated']}</updated>"
            f"<summary>{escape(p['summary'])}</summary><author><name>{p['authors']}</name></author>"
            f"{cats}<link title='pdf' href='{p['pdf_url']}'/></entry>")
    paging = '' if start is None else (f"<s:startIndex>{start}</s:startIndex><s:totalResults>{total}</s:totalResults>"
                                      f"<s:itemsPerPage>{size}</s:itemsPerPage>")
    return ("<feed xmlns='http://www.w3.org/2005/Atom' xmlns:s='http://a9.com/-/spec/opensearch/1.1/'>"
            + paging + ''.join(entries) + '</feed>').encode()


def oai(histories, token='', verb='ListRecords'):
    records = []
    for identity, versions in histories.items():
        items = ''.join(f"<r:version version='v{v}'><r:date>{format_datetime(arxiv_versions.timestamp(t), usegmt=True)}</r:date></r:version>"
                        for v, t in versions.items())
        records.append(f"<record><header><identifier>oai:arXiv.org:{identity}</identifier></header>"
                       f"<metadata><r:arXivRaw><r:id>{identity}</r:id>{items}</r:arXivRaw></metadata></record>")
    return (f"<OAI-PMH xmlns='http://www.openarchives.org/OAI/2.0/' xmlns:r='http://arxiv.org/OAI/arXivRaw/'>"
            f"<{verb}>" + ''.join(records) + f"<resumptionToken>{escape(token)}</resumptionToken></{verb}></OAI-PMH>").encode()


class IsolatedDesktopTest(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        scratch = test_scratch(PROJECT_ROOT)
        scratch.mkdir(exist_ok=True)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory(dir=scratch)))
        self.stack.enter_context(patch.object(pipeline, 'PROJECT_ROOT', self.root))
        self.forbidden = [self.stack.enter_context(patch(target, side_effect=AssertionError(target))) for target in
                          ('main.DeepSeekClient', 'pdf_processing.download_selected_papers', 'main.subprocess.run')]

    def assert_no_analysis(self):
        for operation in self.forbidden:
            operation.assert_not_called()


class DesktopPipelineTests(IsolatedDesktopTest):
    def install(self, histories, metadata, *, selected=None, pages=None):
        self.calls = []
        def request(**kwargs):
            self.assertEqual(kwargs['timeout_seconds'], 90)
            self.assertEqual(kwargs['interval_seconds'], 5)
            self.assertEqual(kwargs['cache_dir'], self.root / '.desktop-runtime/cache/arxiv')
            kwargs['diagnostic_observer']('transport', {'http_status': 200, 'attempt': 1,
                'max_attempts': 1, 'remote_ip': '151.101.3.42', 'timings_seconds': {'time_total': 0.25}})
            params = parse_qs(urlparse(kwargs['url']).query)
            self.calls.append(params)
            if params.get('verb') == ['ListRecords']:
                if 'resumptionToken' in params:
                    return pages[params['resumptionToken'][0]]
                category = next(c for c, s in arxiv_versions.SETS.items() if [s] == params['set'])
                self.assertNotIn('until', params)
                return oai({k: histories[k] for k in (selected.get(category, []) if selected is not None else histories)},
                           token='next' if pages and category == 'gr-qc' else '')
            if params.get('verb') == ['GetRecord']:
                identity = params['identifier'][0].removeprefix('oai:arXiv.org:')
                return oai({identity: histories[identity]}, verb='GetRecord')
            ids = params['id_list'][0].split(',')
            self.assertLessEqual(len(ids), 100)
            self.assertNotIn('search_query', params)
            return feed([metadata[i] for i in ids])
        self.stack.enter_context(patch('main.fetch_arxiv_metadata', side_effect=request))

    def single(self, versions=None, categories='gr-qc'):
        history = versions or {1: f'{DAY}T01:00:00Z'}
        identity = '2609.00001'
        metadata = {f'{identity}v{v}': dict(paper(version=v, updated=t), published=history[1],
                    categories=categories, primary_category=categories.split(', ')[0]) for v, t in history.items()}
        self.install({identity: history}, metadata)
        return metadata

    def test_historical_v1_v5_execute_later_unchanged_and_no_new_version(self):
        dates = [date(2026, m, 1) for m in range(1, 6)]
        self.single({v: f'{d}T01:00:00Z' for v, d in enumerate(dates, 1)})
        for v, day in enumerate(dates, 1):
            self.assertEqual(pipeline.fetch_candidates_for_date(day).papers[0]['version'], v)
        result = pipeline.fetch_candidates_for_date(dates[1])
        self.assertEqual(result.papers[0]['version'], 2)
        self.assertEqual(result.reused_count, 1)
        self.assertIsNone(pipeline.fetch_candidates_for_date(date(2026, 2, 2)).snapshot)
        self.assert_no_analysis()

    def test_last_version_cross_category_dedupe_and_immutable(self):
        self.single({1: f'{DAY}T01:00:00Z', 2: f'{DAY}T02:00:00Z'}, 'gr-qc, astro-ph.HE')
        result = pipeline.fetch_candidates_for_date(DAY)
        self.assertEqual((result.raw_count, result.unique_count), (2, 1))
        self.assertEqual(result.papers[0]['version'], 2)
        self.assertEqual(result.categories, pipeline.CATEGORIES)
        with self.assertRaises(TypeError):
            result.papers[0]['title'] = 'changed'

    def test_beijing_boundaries_and_old_id(self):
        times = ['2026-09-24T15:59:59Z', '2026-09-24T16:00:00Z',
                 '2026-09-25T15:59:59Z', '2026-09-25T16:00:00Z']
        histories = {f'2609.{i:05d}': {1: t} for i, t in enumerate(times)}
        metadata = {f'{i}v1': dict(paper(n, updated=t), published=t) for n, (i, h) in enumerate(histories.items()) for t in h.values()}
        identity = 'gr-qc/9901001'
        histories[identity] = {1: '2026-09-25T00:00:00+08:00'}
        metadata[identity+'v1'] = dict(paper(), arxiv_id=identity, id=f'https://arxiv.org/abs/{identity}v1',
            abs_url=f'https://arxiv.org/abs/{identity}v1', pdf_url=f'https://arxiv.org/pdf/{identity}v1',
            published='2026-09-25T00:00:00+08:00', updated='2026-09-25T00:00:00+08:00')
        self.install(histories, metadata)
        result = pipeline.fetch_candidates_for_date(DAY)
        self.assertEqual({p['arxiv_id'] for p in result.papers}, {'2609.00001', '2609.00002', identity})
        self.assertEqual(self.calls[0]['from'], ['2026-09-24'])

    def test_all_pages_and_over_100_exact_ids(self):
        histories = {f'2609.{i:05d}': {1: f'{DAY}T01:00:00Z'} for i in range(105)}
        metadata = {f'{i}v1': paper(n) for n, i in enumerate(histories)}
        first, rest = dict(list(histories.items())[:100]), dict(list(histories.items())[100:])
        self.install(first, metadata, selected={'gr-qc': list(first)}, pages={'next': oai(rest)})
        result = pipeline.fetch_candidates_for_date(DAY)
        self.assertEqual(result.unique_count, 105)
        self.assertEqual(result.page_count, 4)
        self.assertEqual(set(next(c for c in self.calls if 'resumptionToken' in c)), {'verb', 'resumptionToken'})
        self.assertEqual([len(c['id_list'][0].split(',')) for c in self.calls if 'id_list' in c], [100, 5])

    def test_migration_may_miss_new_discovery_but_preserves_saved_history(self):
        histories = {'2609.00001': {1: f'{DAY}T01:00:00Z', 2: '2026-10-01T01:00:00Z'}}
        selected = {'gr-qc': []}
        self.install(histories, {'2609.00001v1': paper()}, selected=selected)
        self.assertIsNone(pipeline.fetch_candidates_for_date(DAY).snapshot)
        selected['gr-qc'] = ['2609.00001']
        first = pipeline.fetch_candidates_for_date(DAY)
        selected['gr-qc'] = []
        later = pipeline.fetch_candidates_for_date(DAY)
        self.assertEqual(dict(first.papers[0]), dict(later.papers[0]))
        self.assertTrue(any(c.get('verb') == ['GetRecord'] for c in self.calls))

    def test_cached_same_day_v1_is_replaced_by_v2(self):
        history = {1: f'{DAY}T01:00:00Z'}
        metadata = self.single(history)
        self.assertEqual(pipeline.fetch_candidates_for_date(DAY).papers[0]['version'], 1)
        history[2] = f'{DAY}T02:00:00Z'
        metadata['2609.00001v2'] = paper(version=2, updated=history[2])
        self.assertEqual(pipeline.fetch_candidates_for_date(DAY).papers[0]['version'], 2)

    def test_latest_day_version_category_checked_without_fallback(self):
        metadata = self.single({1: f'{DAY}T01:00:00Z', 2: f'{DAY}T02:00:00Z'})
        metadata['2609.00001v2']['categories'] = 'hep-th'
        self.assertIsNone(pipeline.fetch_candidates_for_date(DAY).snapshot)

    def test_wrong_version_missing_metadata_and_conflict_no_partial_save(self):
        for mutation in ('version', 'time', 'missing', 'summary'):
            with self.subTest(mutation=mutation):
                metadata = self.single()
                p = metadata['2609.00001v1']
                if mutation == 'version':
                    p['id'] = 'https://arxiv.org/abs/2609.00001v5'
                elif mutation == 'time':
                    p['updated'] = '2026-09-26T01:00:00Z'
                elif mutation == 'missing':
                    p['title'] = ''
                else:
                    p['summary'] = ''
                with self.assertRaises(pipeline.CandidateError):
                    pipeline.fetch_candidates_for_date(DAY)
        self.assertEqual(VersionStore(self.root).previous(DAY, pipeline.CATEGORIES), [])

    def test_token_errors_repeat_page_xml_and_missing_history(self):
        identity = '2609.00001'
        histories = {identity: {1: f'{DAY}T01:00:00Z'}}
        for xml in (oai(histories, 'next'), b'broken',
                    b"<OAI-PMH xmlns='http://www.openarchives.org/OAI/2.0/'><error code='badResumptionToken'/></OAI-PMH>",
                    oai({identity: {2: f'{DAY}T01:00:00Z'}})):
            with self.subTest(xml=xml[:60]):
                self.install(histories, {identity+'v1': paper()}, selected={'gr-qc': [identity]}, pages={'next': xml})
                with self.assertRaises(pipeline.CandidateError):
                    pipeline.fetch_candidates_for_date(DAY)

    def test_network_failure_records_failed_not_empty_no_retry(self):
        with patch('main.fetch_arxiv_metadata', side_effect=RuntimeError('arxiv_api_timeout')) as request:
            with self.assertRaises(pipeline.CandidateError):
                pipeline.fetch_candidates_for_date(DAY)
        self.assertEqual(request.call_count, 1)
        with closing(sqlite3.connect(VersionStore(self.root).database)) as c:
            self.assertEqual(c.execute('SELECT status,unique_count FROM queries').fetchone(), ('failed', 0))

    def test_zero_then_available_refreshes_and_progress_is_real(self):
        histories, metadata, selected = {}, {}, {'gr-qc': []}
        self.install(histories, metadata, selected=selected)
        self.assertIsNone(pipeline.fetch_candidates_for_date(DAY).snapshot)
        histories['2609.00001'] = {1: f'{DAY}T01:00:00Z'}
        metadata['2609.00001v1'] = paper()
        selected['gr-qc'] = ['2609.00001']
        events = []
        result = pipeline.fetch_candidates_for_date(DAY, events.append, categories=('gr-qc',))
        self.assertEqual(result.unique_count, 1)
        self.assertEqual(events[-1].state, 'completed')
        self.assertTrue(all(e.category_total == 1 for e in events if e.stage == 'category'))

    def test_runtime_paths_cannot_escape(self):
        for parts in (('..', 'outside'), (str(self.root.parent),)):
            with self.assertRaises(ValueError):
                pipeline.runtime_path(*parts)

    def test_transport_evidence_reaches_safe_diagnostics(self):
        self.single()
        diagnostics = DesktopDiagnostics(self.root)
        try:
            result = pipeline.fetch_candidates_for_date(DAY, diagnostics=diagnostics)
            self.assertIsNotNone(result.snapshot)
        finally:
            diagnostics.close()
        log = next(diagnostics.log_directory.glob('*.jsonl')).read_text(encoding='utf-8')
        self.assertIn('"remote_ip":"151.101.3.42"', log)
        self.assertIn('"time_total":0.25', log)
        self.assertNotIn('Synthetic abstract', log)

    def test_snapshot_does_not_change_after_caller_or_database_update(self):
        p = paper()
        snapshot = pipeline.CandidateSnapshot(DAY, arxiv_versions.timestamp(p['updated']), 1, 1, (p,))
        p['title'] = 'changed after freeze'
        self.assertEqual(snapshot.papers[0]['title'], 'Synthetic paper')
        self.single()
        first = pipeline.fetch_candidates_for_date(DAY)
        store = VersionStore(self.root)
        store.save('other-date', DAY+timedelta(days=1), ('gr-qc',), [paper(2)], 1)
        self.assertEqual(first.papers[0]['title'], 'Synthetic paper')

    def test_desktop_has_no_online_automation_dependencies(self):
        for source in (PROJECT_ROOT / 'desktop').glob('*.py'):
            tree = ast.parse(source.read_text(encoding='utf-8'))
            modules = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
            modules.update(a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names)
            self.assertFalse(any(m and ('automation' in m or 'cloud_metadata' in m) for m in modules))


class MetadataStoreTests(IsolatedDesktopTest):
    def setUp(self):
        super().setUp()
        self.store = VersionStore(self.root)

    def test_create_dedupe_restart_zero(self):
        self.store.save('one', DAY, ('gr-qc',), [paper()], 1)
        self.store.save('two', DAY, ('gr-qc',), [paper()], 1)
        self.store.save('zero', DAY+timedelta(days=1), ('gr-qc',), [], 0)
        with closing(sqlite3.connect(self.store.database)) as c:
            self.assertEqual(c.execute('PRAGMA user_version').fetchone()[0], 1)
            self.assertEqual(c.execute('SELECT COUNT(*) FROM versions').fetchone()[0], 1)
            self.assertEqual(c.execute('SELECT COUNT(*) FROM queries').fetchone()[0], 3)
        self.assertEqual(VersionStore(self.root).previous(DAY, ('gr-qc',)), [paper()])
        self.assertEqual(self.store.previous(DAY, ('hep-th',)), [])

    def test_conflict_rolls_back_whole_transaction(self):
        self.store.save('one', DAY, ('gr-qc',), [paper()], 1)
        with self.assertRaises(MetadataError):
            self.store.save('bad', DAY, ('gr-qc',), [paper(2), dict(paper(), title='conflict')], 2)
        with closing(sqlite3.connect(self.store.database)) as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM versions').fetchone()[0], 1)
            self.assertEqual(c.execute('SELECT COUNT(*) FROM queries').fetchone()[0], 1)

    def test_unknown_schema_corrupt_concurrent_write_preserve(self):
        self.store.save('one', DAY, ('gr-qc',), [paper()], 1)
        with self.store.connection(write=True):
            with self.assertRaises(MetadataError):
                self.store.save('busy', DAY, ('gr-qc',), [], 0)
        with closing(sqlite3.connect(self.store.database)) as c:
            c.execute('PRAGMA user_version=99')
            c.commit()
        before = self.store.database.read_bytes()
        with self.assertRaises(MetadataError):
            self.store.save('unknown', DAY, ('gr-qc',), [], 0)
        self.assertEqual(self.store.database.read_bytes(), before)
        self.store.database.write_bytes(b'synthetic damaged sqlite')
        with self.assertRaises(MetadataError):
            self.store.previous(DAY, ('gr-qc',))
        self.assertEqual(self.store.database.read_bytes(), b'synthetic damaged sqlite')

    def test_save_failure_blocks_snapshot(self):
        with patch.object(VersionStore, 'save', side_effect=MetadataError('metadata_storage_failed')):
            with patch('main.fetch_arxiv_metadata', return_value=oai({})):
                with self.assertRaises(pipeline.CandidateError) as caught:
                    pipeline.fetch_candidates_for_date(DAY)
        self.assertEqual(caught.exception.issue.code, 'AKD-FETCH-METADATA_FAILED')


if __name__ == '__main__':
    unittest.main()
