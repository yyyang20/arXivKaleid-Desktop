# Copyright (c) 2026 yyyang20
# SPDX-License-Identifier: GPL-3.0-only
from __future__ import annotations

import ast
from contextlib import ExitStack, closing
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import re
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


def search_page(ids, start=0, total=None):
    total = len(ids) if total is None else total
    heading = (f"Showing {start+1}&ndash;{start+len(ids)} of {total} results" if total else
               "Sorry, your query returned no results")
    return ("<html><h1>" + heading + "</h1><ol>" + ''.join(
        f'<li class="arxiv-result"><a href="https://arxiv.org/abs/{i}">arXiv:{i}</a></li>' for i in ids)
        + "</ol></html>").encode()


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
    def install(self, metadata, *, overrides=None):
        self.calls = []
        self.metadata = metadata
        self.overrides = overrides or {}
        def request(**kwargs):
            self.assertEqual(kwargs['timeout_seconds'], 90)
            self.assertEqual(kwargs['subprocess_timeout_margin_seconds'], 15)
            self.assertEqual(kwargs.get('max_attempts', 1), 1)
            self.assertEqual(kwargs['interval_seconds'], 5)
            self.assertEqual(kwargs['cache_dir'], self.root / '.desktop-runtime/cache/arxiv')
            kwargs['diagnostic_observer']('transport', {'http_status': 200, 'attempt': 1,
                'max_attempts': 1, 'remote_ip': '151.101.3.42', 'timings_seconds': {'time_total': 0.25}})
            parsed = urlparse(kwargs['url'])
            self.assertNotIn('oai', parsed.hostname)
            params = parse_qs(parsed.query)
            self.assertNotIn('verb', params)
            self.calls.append(params)
            if 'id_list' in params:
                ids = params['id_list'][0].split(',')
                self.assertLessEqual(len(ids), 100)
                self.assertTrue(all(not re.search(r'v\d+$', i) for i in ids))
                return self.overrides.get(('LatestMetadata', '', 0), feed([metadata[i] for i in ids]))
            start = int(params.get('start', ['0'])[0])
            if parsed.path == '/search/advanced':
                category = params['terms-0-term'][0]
                self.assertNotIn('include_older_versions', params)
                lower = date.fromisoformat(params['date-from_date'][0])
                upper = date.fromisoformat(params['date-to_date'][0])
                ids = sorted(i for i, p in metadata.items() if category in p['categories'].split(', ')
                             and lower <= arxiv_versions.timestamp(p['updated']).date() <= upper)
                page_ids = ids[start:start+arxiv_versions.SEARCH_PAGE_SIZE]
                return self.overrides.get(('AdvancedSearch', category, start), search_page(page_ids, start, len(ids)))
            category = re.search(r'cat:([^ ]+)', params['search_query'][0])[1]
            papers = [p for p in metadata.values() if category in p['categories'].split(', ')]
            source = 'LatestUpdates' if params['sortBy'] == ['lastUpdatedDate'] else 'FirstSubmissions'
            if source == 'FirstSubmissions':
                bounds = re.findall(r'\d{12}', params['search_query'][0])
                lower, upper = [datetime.strptime(t, '%Y%m%d%H%M').replace(tzinfo=timezone.utc) for t in bounds]
                papers = [p for p in papers if lower <= arxiv_versions.timestamp(p['published']) <= upper]
                papers.sort(key=lambda p: p['published'], reverse=True)
            else:
                self.assertNotIn('submittedDate:', params['search_query'][0])
                papers.sort(key=lambda p: arxiv_versions.timestamp(p['updated']), reverse=True)
            size = int(params['max_results'][0])
            return self.overrides.get((source, category, start), feed(papers[start:start+size], start=start, total=len(papers), size=size))
        self.stack.enter_context(patch('main.fetch_arxiv_metadata', side_effect=request))

    def single(self, version=1, published=None, updated=None, categories='gr-qc'):
        p = dict(paper(version=version, updated=updated), categories=categories, primary_category=categories.split(', ')[0])
        if published:
            p['published'] = published
        self.install({p['arxiv_id']: p})
        return p

    def test_first_latest_and_saved_history_refresh_to_new_version(self):
        first, update, next_update = date(2026, 1, 1), date(2026, 4, 1), date(2026, 5, 1)
        p = self.single(4, f'{first}T01:00:00Z', f'{update}T01:00:00Z')
        self.assertEqual(pipeline.fetch_candidates_for_date(first).papers[0]['version'], 4)
        self.assertIsNone(pipeline.fetch_candidates_for_date(date(2026, 2, 1)).snapshot)
        old = pipeline.fetch_candidates_for_date(update).snapshot
        self.assertEqual(old.papers[0]['version'], 4)
        self.metadata[p['arxiv_id']] = dict(paper(version=5, updated=f'{next_update}T01:00:00Z'), published=p['published'])
        self.assertEqual(pipeline.fetch_candidates_for_date(next_update).papers[0]['version'], 5)
        recovered = pipeline.fetch_candidates_for_date(update)
        self.assertEqual(recovered.papers[0]['version'], 5)
        self.assertEqual(recovered.reused_count, 1)
        self.assertEqual(old.papers[0]['version'], 4)
        self.assertIsNotNone(VersionStore(self.root).get((p['arxiv_id'], 4)))
        self.assertEqual(pipeline.fetch_candidates_for_date(first).papers[0]['version'], 5)
        self.assert_no_analysis()

    def test_other_date_record_cannot_fill_missing_middle_date(self):
        p = self.single(5, '2026-01-01T01:00:00Z', '2026-05-01T01:00:00Z')
        pipeline.fetch_candidates_for_date(date(2026, 5, 1))
        self.assertIsNone(pipeline.fetch_candidates_for_date(date(2026, 4, 1)).snapshot)

    def test_today_updates_old_paper_and_stops_before_day(self):
        p = self.single(4, '2026-01-01T01:00:00Z', f'{DAY}T01:00:00Z')
        self.metadata['2609.00002'] = dict(paper(2), updated='2026-09-23T01:00:00Z', published='2026-09-23T01:00:00Z')
        with patch.object(arxiv_versions, 'datetime') as clock:
            clock.now.return_value.date.return_value = DAY
            result = pipeline.fetch_candidates_for_date(DAY)
        self.assertEqual(result.unique_count, 1)
        self.assertEqual(result.papers[0]['version'], 4)
        self.assertEqual(result.page_count, 3)
        self.assertFalse(any('submittedDate:' in c.get('search_query', [''])[0] for c in self.calls))
        self.assertFalse(any('terms-0-term' in c for c in self.calls))

    def test_cross_category_dedupe_and_immutable(self):
        self.single(categories='gr-qc, astro-ph.HE')
        result = pipeline.fetch_candidates_for_date(DAY)
        self.assertEqual((result.raw_count, result.unique_count), (2, 1))
        with self.assertRaises(TypeError):
            result.papers[0]['title'] = 'changed'

    def test_beijing_boundaries_and_old_id(self):
        times = ['2026-09-24T15:59:59Z', '2026-09-24T16:00:00Z',
                 '2026-09-25T15:59:59Z', '2026-09-25T16:00:00Z']
        metadata = {p['arxiv_id']: p for n, t in enumerate(times) for p in [paper(n, updated=t)]}
        identity = 'gr-qc/9901001'
        metadata[identity] = dict(paper(), arxiv_id=identity, id=f'https://arxiv.org/abs/{identity}v1',
            abs_url=f'https://arxiv.org/abs/{identity}v1', pdf_url=f'https://arxiv.org/pdf/{identity}v1',
            published='2026-09-25T00:00:00+08:00', updated='2026-09-25T00:00:00+08:00')
        self.install(metadata)
        result = pipeline.fetch_candidates_for_date(DAY)
        self.assertEqual({p['arxiv_id'] for p in result.papers}, {'2609.00001', '2609.00002', identity})

    def test_all_pages_and_more_than_100_latest_ids(self):
        metadata = {p['arxiv_id']: p for n in range(205) for p in [paper(n)]}
        self.install(metadata)
        result = pipeline.fetch_candidates_for_date(DAY)
        self.assertEqual(result.unique_count, 205)
        self.assertEqual([len(c['id_list'][0].split(',')) for c in self.calls if 'id_list' in c], [100, 100, 5])
        self.assertTrue(any(c.get('start') == ['200'] and 'terms-0-term' in c for c in self.calls))

    def test_saved_history_does_not_override_current_category(self):
        p = self.single()
        pipeline.fetch_candidates_for_date(DAY)
        self.metadata[p['arxiv_id']] = dict(paper(version=2, updated='2026-10-01T01:00:00Z'), categories='hep-th')
        self.assertIsNone(pipeline.fetch_candidates_for_date(DAY).snapshot)
        self.assertEqual(VersionStore(self.root).previous(DAY, ('gr-qc',))[0]['version'], 1)

    def test_latest_response_missing_identity_wrong_version_or_summary_fails(self):
        for mutation in ('version', 'id', 'missing', 'summary', 'regression'):
            with self.subTest(mutation=mutation):
                p = self.single(2, '2026-01-01T01:00:00Z', f'{DAY}T01:00:00Z')
                bad = dict(p)
                if mutation == 'version':
                    bad['version'] = 3
                    bad['pdf_url'] = bad['pdf_url'].replace('v2', 'v3')
                elif mutation == 'id':
                    bad['id'] = 'https://arxiv.org/abs/2609.00099v2'
                elif mutation == 'summary':
                    bad['summary'] = ''
                elif mutation == 'regression':
                    bad = dict(paper(), published=p['published'])
                self.overrides[('LatestMetadata', '', 0)] = feed([] if mutation == 'missing' else [bad])
                with self.assertRaises(pipeline.CandidateError):
                    pipeline.fetch_candidates_for_date(DAY)
        self.assertEqual(VersionStore(self.root).previous(DAY, pipeline.CATEGORIES), [])

    def test_search_structure_pagination_and_duplicate_fail_closed(self):
        p = self.single()
        cases = [b'blocked', b'<html>Sorry, your query returned no results<li class="arxiv-result"></html>',
                 search_page([p['arxiv_id'], p['arxiv_id']]), search_page([p['arxiv_id']], start=1)]
        for html in cases:
            with self.subTest(html=html[:35]):
                self.overrides[('AdvancedSearch', 'gr-qc', 0)] = html
                with self.assertRaises(pipeline.CandidateError):
                    pipeline.fetch_candidates_for_date(DAY)

    def test_today_sort_and_empty_page_fail_closed(self):
        self.single()
        with patch.object(arxiv_versions, 'datetime') as clock:
            clock.now.return_value.date.return_value = DAY
            for data in [feed([], start=0, total=1, size=100),
                         feed([paper(1, updated='2026-09-24T01:00:00Z'), paper(2)], start=0, total=2, size=100)]:
                self.overrides[('LatestUpdates', 'gr-qc', 0)] = data
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
        self.install({})
        self.assertIsNone(pipeline.fetch_candidates_for_date(DAY).snapshot)
        self.metadata['2609.00001'] = paper()
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
            pipeline.fetch_candidates_for_date(DAY, diagnostics=diagnostics)
        finally:
            diagnostics.close()
        log = next(diagnostics.log_directory.glob('*.jsonl')).read_text(encoding='utf-8')
        self.assertIn('"remote_ip":"151.101.3.42"', log)
        self.assertIn('"time_total":0.25', log)
        self.assertIn('AdvancedSearch', log)
        self.assertNotIn('Synthetic abstract', log)

    def test_snapshot_does_not_change_after_caller_or_database_update(self):
        p = paper()
        snapshot = pipeline.CandidateSnapshot(DAY, arxiv_versions.timestamp(p['updated']), 1, 1, (p,))
        p['title'] = 'changed after freeze'
        self.assertEqual(snapshot.papers[0]['title'], 'Synthetic paper')
        self.single()
        first = pipeline.fetch_candidates_for_date(DAY)
        VersionStore(self.root).save('other-date', DAY+timedelta(days=1), ('gr-qc',), [paper(2)], 1)
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
            with patch.object(arxiv_versions, 'collect', return_value=([], 0, 0, 0, 0)):
                with self.assertRaises(pipeline.CandidateError) as caught:
                    pipeline.fetch_candidates_for_date(DAY)
        self.assertEqual(caught.exception.issue.code, 'AKD-FETCH-METADATA_FAILED')

    def test_legacy_policy_and_query_category_are_checked(self):
        cross = dict(paper(), categories='gr-qc, astro-ph.HE')
        self.store.save('legacy', DAY, ('gr-qc',), [cross], 1)
        with closing(sqlite3.connect(self.store.database)) as c:
            c.execute("UPDATE queries SET policy='beijing_version_day_v1' WHERE query_id='legacy'")
            c.commit()
        self.assertEqual(self.store.previous(DAY, ('gr-qc',)), [cross])
        self.assertEqual(self.store.previous(DAY, ('astro-ph.HE',)), [])
        with closing(sqlite3.connect(self.store.database)) as c:
            c.execute("UPDATE queries SET policy='unknown-policy'")
            c.commit()
        self.assertEqual(self.store.previous(DAY, ('gr-qc',)), [])


if __name__ == '__main__':
    unittest.main()
