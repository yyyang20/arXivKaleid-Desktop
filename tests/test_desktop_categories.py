# Copyright (c) 2026 yyyang20
# SPDX-License-Identifier: GPL-3.0-only
"""全部 Physics 身份、持久化与动态分类回归；传输均为夹具。"""
import json
import unittest
from datetime import date
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import main
from desktop import pipeline, arxiv_versions
from desktop.categories import ARCHIVES, DEFAULT_CATEGORIES, PHYSICS_CATEGORIES, PHYSICS_IDS, validate_categories
from desktop.category_selection import CategorySelectionError, CategorySelectionStore
from desktop.version_metadata import VersionStore
import test_desktop_pipeline as fixtures
from test_desktop_pipeline import DAY, IsolatedDesktopTest, paper, feed


class CategoryStoreTests(IsolatedDesktopTest):
    def test_all_51_ids_names_archives_and_canonical_rejections(self):
        self.assertEqual(len(PHYSICS_CATEGORIES), 51)
        self.assertEqual(len(PHYSICS_IDS), 51)
        self.assertTrue(set(DEFAULT_CATEGORIES) <= PHYSICS_IDS)
        self.assertEqual({c.archive for c in PHYSICS_CATEGORIES}, {a[0] for a in ARCHIVES})
        self.assertTrue(all(c.name for c in PHYSICS_CATEGORIES))
        self.assertEqual(sum(c.archive == 'physics' for c in PHYSICS_CATEGORIES), 22)
        self.assertEqual(sum(c.archive == 'nlin' for c in PHYSICS_CATEGORIES), 5)
        for bad in [[], (), 'gr-qc', None, ['gr-qc']*2, ['math.MP'], ['astro-ph'], ['physics'],
                    ['math.NA'], ['gr-qc '], ['GR-QC'], [1], ['gr-qc','hep-th','hep-ph','quant-ph']]:
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                validate_categories(bad)

    def test_each_category_query_atom_and_membership(self):
        for category in PHYSICS_CATEGORIES:
            with self.subTest(category=category.id):
                self.assertEqual(validate_categories([category.id]), (category.id,))
                url = main.build_arxiv_query_url(category.id, DAY, 100)
                self.assertTrue(parse_qs(urlsplit(url).query)['search_query'][0].startswith('cat:'+category.id+' AND '))
                fixture = dict(paper(), categories=category.id, primary_category=category.id)
                result = arxiv_versions.normalized_papers(feed([fixture])).papers[0]
                self.assertEqual(result['categories'], category.id)
                self.assertTrue(set(result['categories'].split(', ')) & {category.id})

    def test_restart_atomic_failure_and_restore(self):
        store = CategorySelectionStore(self.root)
        self.assertEqual(store.load(), DEFAULT_CATEGORIES)
        self.assertFalse(store.file.exists())
        choice = ('hep-th', 'physics.optics')
        self.assertEqual(store.save(choice), choice)
        self.assertEqual(CategorySelectionStore(self.root).load(), choice)
        before = store.file.read_bytes()
        with patch('desktop.category_selection.os.replace', side_effect=OSError), self.assertRaises(CategorySelectionError):
            store.save(('quant-ph',))
        self.assertEqual(store.file.read_bytes(), before)
        self.assertEqual(list(store.file.parent.glob('.categories-*')), [])
        store.save(DEFAULT_CATEGORIES)
        self.assertEqual(CategorySelectionStore(self.root).load(), DEFAULT_CATEGORIES)

    def test_invalid_file_preserved_until_explicit_save(self):
        store = CategorySelectionStore(self.root)
        store.file.parent.mkdir(parents=True)
        for data in ['{bad', json.dumps({'format_version':'unknown','categories':['gr-qc']}),
                     json.dumps({'format_version':'category_selection_v1','categories':['math.MP']}),
                     json.dumps({'format_version':'category_selection_v1','categories':[]})]:
            store.file.write_text(data, encoding='utf-8')
            with self.assertRaises(CategorySelectionError):
                store.load()
            self.assertEqual(store.file.read_text(encoding='utf-8'), data)
        store.save(('gr-qc',))
        self.assertEqual(store.load(), ('gr-qc',))

    def test_invalid_pipeline_selection_has_no_storage_or_network_side_effect(self):
        for bad in [(), ('math.MP',), ('gr-qc','gr-qc'), ('hep-th','hep-ex','hep-ph','quant-ph')]:
            with patch('main.fetch_arxiv_metadata') as request, patch.object(VersionStore,'previous') as previous:
                with self.assertRaises(pipeline.CandidateError) as caught:
                    pipeline.fetch_candidates_for_date(DAY, categories=bad)
                self.assertEqual(caught.exception.issue.code, 'AKD-FETCH-CATEGORIES_INVALID')
                request.assert_not_called()
                previous.assert_not_called()
        self.assertFalse((self.root/'.desktop-runtime').exists())


class DynamicPipelineTests(IsolatedDesktopTest):
    # 仅复用假传输安装器，避免重复继承原测试集。
    install = fixtures.DesktopPipelineTests.install
    single = fixtures.DesktopPipelineTests.single

    def test_one_two_three_categories_counts_order_and_cross_list(self):
        A, B, C = 'gr-qc', 'hep-th', 'physics.optics'
        metadata = {p['arxiv_id']:p for p in [dict(paper(1), categories=f'{A}, {B}, {C}'),
            dict(paper(2), categories=B, primary_category=B), dict(paper(3), categories=C, primary_category=C)]}
        self.install(metadata)
        for categories, expected in [((A,), (1,1)), ((A,B),(3,2)), ((A,B,C),(5,3))]:
            result = pipeline.fetch_candidates_for_date(DAY, categories=categories)
            reverse = pipeline.fetch_candidates_for_date(DAY, categories=tuple(reversed(categories)))
            self.assertEqual((result.raw_count,result.unique_count), expected)
            self.assertEqual(result.papers, reverse.papers)
            self.assertEqual(result.categories, categories)
            self.assertEqual(result.network_count+result.reused_count, result.unique_count)
        self.assert_no_analysis()

    def test_v1_v2_v3_first_latest_middle_and_history_refresh(self):
        first, middle, latest = date(2026,1,1), date(2026,2,1), date(2026,3,1)
        p = self.single(3, f'{first}T01:00:00Z', f'{latest}T01:00:00Z')
        selected = ('gr-qc',)
        initial = pipeline.fetch_candidates_for_date(first, categories=selected).snapshot
        self.assertEqual(initial.papers[0]['version'], 3)
        self.assertEqual(pipeline.fetch_candidates_for_date(latest, categories=selected).papers[0]['version'], 3)
        self.assertIsNone(pipeline.fetch_candidates_for_date(middle, categories=selected).snapshot)
        older = dict(paper(version=2, updated=f'{middle}T01:00:00Z'), published=p['published'])
        VersionStore(self.root).save('trusted-middle', middle, selected, [older], 1)
        recovered = pipeline.fetch_candidates_for_date(middle, categories=selected)
        self.assertEqual((recovered.papers[0]['version'], recovered.reused_count), (3,1))
        self.assertEqual(VersionStore(self.root).get((p['arxiv_id'],2))['version'], 2)
        self.assertEqual(initial.papers[0]['version'], 3)
        self.assert_no_analysis()

    def test_selected_cross_list_can_have_non_physics_primary_category(self):
        self.single(categories='math.NA, gr-qc')
        result = pipeline.fetch_candidates_for_date(DAY, categories=('gr-qc',))
        self.assertEqual((result.raw_count,result.unique_count), (1,1))
        self.assertEqual(result.papers[0]['primary_category'], 'math.NA')
        self.assert_no_analysis()

    def test_history_combo_overlap_date_and_latest_category_checks(self):
        store = VersionStore(self.root)
        cross = dict(paper(version=2), categories='gr-qc, hep-th', published='2026-01-01T01:00:00Z')
        store.save('A', DAY, ('gr-qc',), [cross], 1)
        self.assertEqual(store.previous(DAY, ('hep-th',)), [])
        self.assertEqual(store.previous(date(2026,9,24), ('gr-qc',)), [])
        self.assertEqual(store.previous(DAY, ('hep-th','gr-qc')), [cross])
        store.save('AB', DAY, ('gr-qc','hep-th'), [cross], 2)
        self.assertEqual(len(store.previous(DAY, ('gr-qc','hep-th'))), 2)
        newest = dict(paper(version=3, updated='2026-10-01T01:00:00Z'), categories='hep-th', primary_category='hep-th', published=cross['published'])
        self.install({newest['arxiv_id']:newest})
        result = pipeline.fetch_candidates_for_date(DAY, categories=('gr-qc','hep-th'))
        self.assertEqual((result.unique_count,result.raw_count,result.reused_count), (1,1,1))
        self.assertIsNone(pipeline.fetch_candidates_for_date(DAY, categories=('gr-qc',)).snapshot)


if __name__ == '__main__':
    unittest.main()
