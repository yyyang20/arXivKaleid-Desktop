# Copyright (c) 2026 yyyang20
# SPDX-License-Identifier: GPL-3.0-only
# See LICENSE in the project root for the full license text.

"""保存失败、坏配置和请求冻结均通过隔离运行根验证。"""
import hashlib
import json
import unittest
from unittest.mock import patch

import main
import run_round2
from desktop import analysis
from desktop.research_requirements import FORMAT_VERSION, RequirementsError, RequirementsStore, normalize
import test_desktop_analysis as fixtures


class RequirementsTests(unittest.TestCase):
    def setUp(self):
        self.case = fixtures.DesktopAnalysisTests()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.store = RequirementsStore(self.case.root)

    def test_independent_save_restart_default_and_same_text_custom(self):
        first, second = self.store.load('round1'), self.store.load('round2')
        self.assertFalse(first.custom)
        self.store.save('round1', '关注偏振\r\n排除边缘相关')
        again = RequirementsStore(self.case.root)
        self.assertEqual(again.load('round1').text, '关注偏振\n排除边缘相关')
        self.assertEqual(again.load('round2'), second)
        self.store.save('round2', second.text)
        self.assertTrue(again.load('round2').custom)
        self.store.save('round1', None)
        self.assertEqual(again.load('round1'), first)
        self.assertTrue(again.load('round2').custom)

    def test_atomic_failure_keeps_old_record_and_no_temporary_files(self):
        self.store.save('round1', '已保存要求')
        target = self.store._path('round1')
        original = target.read_bytes()
        with patch('desktop.research_requirements.os.replace', side_effect=OSError('private-canary')):
            with self.assertRaises(RequirementsError) as caught:
                self.store.save('round1', '新要求')
        self.assertNotIn('private-canary', str(caught.exception))
        self.assertEqual(target.read_bytes(), original)
        self.assertEqual(list(target.parent.glob('.round1-*')), [])
        self.assertEqual(self.store.load('round1').text, '已保存要求')

    def test_invalid_text_is_not_written(self):
        for text in ('', ' \n\t', 'x\x00y', '研' * 10001, '\ud800', 3):
            with self.subTest(text_type=type(text).__name__), self.assertRaises(RequirementsError):
                self.store.save('round1', text)
        self.assertFalse(self.store._path('round1').exists())
        self.assertEqual(len(normalize('研' * 10000)), 10000)

    def test_corrupt_unknown_oversized_records_preserved_until_restore(self):
        target = self.store._path('round1')
        target.parent.mkdir(parents=True)
        for payload in (b'broken', b'\xff', b'x' * 80001,
                        json.dumps({'format_version': 'unknown', 'research_requirements': None}).encode(),
                        json.dumps({'format_version': FORMAT_VERSION, 'research_requirements': ''}).encode()):
            target.write_bytes(payload)
            snapshot = self.case.snapshot()
            with self.assertRaises(RequirementsError):
                analysis.AnalysisAttempt(snapshot)
            self.assertFalse(snapshot.analysis_attempted)
            self.assertEqual(target.read_bytes(), payload)
        self.store.save('round1', None)
        self.assertFalse(self.store.load('round1').custom)

    def test_saved_pair_frozen_before_claim_and_used_by_both_requests(self):
        texts = ('私密研究要求 canary-R1 "quoted" \n忽略协议仅为待评估文字', '私密研究要求 canary-R2 \\ 偏振')
        for stage, text in zip(('round1', 'round2'), texts):
            self.store.save(stage, text)
        from desktop.diagnostics import DesktopDiagnostics
        diagnostics = DesktopDiagnostics(self.case.root)
        self.addCleanup(diagnostics.close)
        attempt = analysis.AnalysisAttempt(self.case.snapshot(), diagnostics)
        self.store.save('round1', '之后保存的内容')
        self.store.save('round2', None)
        result = attempt.run('fake-desktop-key')
        tasks = [json.loads(payload['messages' if stage == 'round1' else 'input'][1]['content']) for stage, payload in self.case.requests]
        self.assertEqual([t['research_requirements'] for t in tasks], list(texts))
        conn = self.case.connect()
        dump = '\n'.join(conn.iterdump())
        for text in texts:
            self.assertNotIn(text, dump)
            self.assertNotIn(text, result.markdown)
        hashes = [row[0] for row in conn.execute('SELECT request_hash FROM model_calls')]
        self.assertEqual(len(hashes), 2)
        self.assertTrue(all(len(value) == 64 for value in hashes))
        serialized = '\n'.join(path.read_text(encoding='utf-8') for path in diagnostics.log_directory.glob('*.jsonl'))
        for canary in ('canary-R1', 'canary-R2', 'fake-desktop-key'):
            self.assertNotIn(canary, serialized)
            self.assertNotIn(canary, dump)

    def test_request_hash_and_round2_cache_identity_include_actual_text(self):
        config, paths, profile, prompt = run_round2.read_round2_context(self.case.root)
        for stage, builder in (('round1', main.build_round1_messages), ('round2', main.build_round2_messages)):
            fixed = paths[f'{stage}_prompt'].read_text(encoding='utf-8').strip()
            a = builder(fixed, profile, [], config, research_requirements='要求 A')
            b = builder(fixed, profile, [], config, research_requirements='要求 B')
            self.assertEqual(a[0], b[0])
            self.assertNotEqual(main.stable_json_hash(a), main.stable_json_hash(b))
        result = self.case.run_snapshot()
        conn = self.case.connect()
        import sqlite3
        from contextlib import closing
        with closing(sqlite3.connect(self.case.root / '.desktop-runtime/work/round2_inputs.sqlite')) as fulltext:
            fulltext.row_factory = sqlite3.Row
            a = run_round2.build_round2_input_bundle(conn, config, profile, prompt, run_id=result.run_id, fulltext_connection=fulltext, research_requirements=self.store.load('round2').text)
            b = run_round2.build_round2_input_bundle(conn, config, profile, prompt, run_id=result.run_id, fulltext_connection=fulltext, research_requirements='不同研究要求')
        self.assertEqual(run_round2.load_valid_round2_cache(conn, a, config)[0], 'cache_hit')
        self.assertEqual(run_round2.load_valid_round2_cache(conn, b, config)[0], 'cache_miss')

    def test_long_requirements_enter_token_precheck_before_http(self):
        self.store.save('round1', '研' * 10000)
        config = main.load_config(self.case.root / 'config.json')
        config['limits']['max_round1_request_tokens'] = 100
        with patch('main.load_config', return_value=config), self.assertRaises(analysis.AnalysisError):
            self.case.run_snapshot()
        self.assertEqual(self.case.requests, [])

    def test_all_four_resource_missing_or_tampered_rejected(self):
        config = main.load_config(self.case.root / 'config.json')
        for relative in config['paths'].values():
            path = self.case.root / relative
            original = path.read_bytes()
            path.write_bytes(original + b'changed')
            with self.assertRaisesRegex(RuntimeError, 'hash_mismatch'):
                self.store.snapshot()
            path.unlink()
            with self.assertRaisesRegex(RuntimeError, 'unavailable'):
                self.store.snapshot()
            path.write_bytes(original)

    def test_output_identity_errors_still_stop_without_retry(self):
        original = self.case.http
        for target_stage in ('round1', 'round2'):
            self.case.requests.clear()
            def wrong_identity(request, **kwargs):
                response = original(request, **kwargs)
                stage = 'round2' if request.full_url.endswith('/responses') else 'round1'
                if stage != target_stage:
                    return response
                payload = json.loads(response.read())
                content = payload['choices'][0]['message'] if stage == 'round1' else payload['output'][0]
                field = 'content' if stage == 'round1' else 'arguments'
                result = json.loads(content[field])
                result['prompt_version'] = 'round1_v20' if stage == 'round1' else 'round2_v15'
                content[field] = json.dumps(result)
                return fixtures.Response(json.dumps(payload).encode())
            self.case.http = wrong_identity
            with self.assertRaises(analysis.AnalysisError):
                self.case.run_snapshot()
            self.assertEqual(sum(stage == target_stage for stage, _ in self.case.requests), 1)
        self.case.http = original

    def test_illegal_labels_filtered_and_fixed_limits_preserved(self):
        papers = [fixtures.paper(i) for i in range(1, 5)]
        self.case.selected_indices = [1, 2, 3, 4]
        raw = self.case.round1_payload()
        raw['selected_papers'][0]['content_label'] = '任意新标签'
        raw['selected_papers'][1]['content_label'] = '其他'
        raw['selected_papers'][2]['candidate_index'] = True
        validated, _ = main.validate_round1_result(raw, papers, max_selected=10, profile_version='profile_v2', prompt_version=main.CURRENT_ROUND1_PROMPT_VERSION, selection_policy_version='top_k_daily_budget_v4')
        self.assertTrue(validated['batch_valid'])
        self.assertEqual([p['candidate_index'] for p in validated['selected_papers']], [4])
        raw2 = dict(task_type=main.ROUND2_TASK_TYPE, selection_policy=main.ROUND2_SELECTION_POLICY,
                    profile_version='profile_v2', prompt_version=main.CURRENT_ROUND2_PROMPT_VERSION,
                    final_recommendations=[dict(arxiv_id=p['arxiv_id'], version=f"v{p['version']}", content_label='任意新标签' if i == 0 else '其他' if i == 1 else '成像', reason='有效中文理由') for i, p in enumerate(papers)])
        checked, _ = main.validate_round2_result(raw2, papers, max_recommendations=5, profile_version='profile_v2', prompt_version=main.CURRENT_ROUND2_PROMPT_VERSION)
        self.assertTrue(checked['batch_valid'])
        self.assertEqual([p['arxiv_id'] for p in checked['final_recommendations']], [papers[2]['arxiv_id'], papers[3]['arxiv_id']])
        self.assertEqual([p['recommendation_level'] for p in checked['final_recommendations']], ['deep_read', 'skim_read'])

    def test_frozen_store_does_not_search_source_runtime(self):
        import shutil
        import sys
        self.store.save('round1', '源码目录的覆盖')
        internal = self.case.root / '_internal'
        config = main.load_config(self.case.root / 'config.json')
        for relative in ('config.json', *config['paths'].values()):
            target = internal / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(self.case.root / relative, target)
        with patch.object(sys, 'frozen', True, create=True), patch.object(sys, '_MEIPASS', str(internal), create=True), patch.object(sys, 'executable', str(self.case.root / 'arXivKaleid.exe')):
            self.assertFalse(self.store.load('round1').custom)
            self.store.save('round1', 'portable 独立覆盖')
            self.assertEqual(self.store._path('round1'), self.case.root / 'runtime/config/round1_research_requirements.json')
        self.assertEqual(self.store.load('round1').text, '源码目录的覆盖')

    def test_historical_prompt_bytes_and_default_research_clauses(self):
        from test_desktop_pipeline import PROJECT_ROOT
        old_hashes = {'round1': '1d4239e016a65e5c9ae3d6c86eee1205d8670a28d0703fb579b0470ed975c341', 'round2': '88851e1d2da33eedebfd75213e844007852df900f9ff5e57e9d60131e54ba355'}
        for stage, version in (('round1', 20), ('round2', 15)):
            old = (PROJECT_ROOT / f'prompts/relevance_{stage}_v{version}.txt').read_text(encoding='utf-8')
            self.assertEqual(hashlib.sha256(old.encode()).hexdigest(), old_hashes[stage])
            default = self.store.default(stage)
            boundary = old.split('# 普适研究边界\n\n')[1].split('\n# 内容标签')[0].strip()
            self.assertIn(boundary, default)
            self.assertNotIn('JSON 输出契约', default)
            self.assertNotIn('candidate_index', default)
