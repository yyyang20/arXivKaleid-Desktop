# Copyright (c) 2026 yyyang20
# SPDX-License-Identifier: GPL-3.0-only
# See LICENSE in the project root for the full license text.

"""用变换输入而非复刻实现，验证两轮研究独立性和最小技术协议。"""
from contextlib import closing
import json
import sqlite3
import unittest
from unittest.mock import patch

import main
import round2_fulltext_state
import run_round2
from desktop.research_requirements import RequirementsStore
import test_desktop_analysis as fixtures


class ResearchPromptProtocolTests(unittest.TestCase):
    def setUp(self):
        self.case = fixtures.DesktopAnalysisTests()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.config, self.paths, self.profile, self.prompt = run_round2.read_round2_context(self.case.root)

    def bundle(self, connection, fulltext, run_id):
        return run_round2.build_round2_input_bundle(
            connection, self.config, self.profile, self.prompt, run_id=run_id,
            fulltext_connection=fulltext, research_prompt='独立研究方法和实验结果',
        )

    def test_round1_input_only_identity_title_and_abstract(self):
        store = RequirementsStore(self.case.root)
        custom = '只选数值方法，按计算复杂度排序。可以选择其他领域，不要求理由。'
        store.save('round1', custom)
        self.case.run_snapshot()
        task = json.loads(self.case.requests[0][1]['messages'][1]['content'])
        self.assertEqual(task['research_prompt'], custom)
        self.assertEqual(set(task), {'task_type', 'profile_version', 'prompt_version',
            'selection_policy', 'selection_policy_version', 'research_prompt', 'candidate_papers'})
        self.assertTrue(all(set(p) == {'candidate_index', 'arxiv_id', 'version', 'title', 'summary'}
                             for p in task['candidate_papers']))

    def test_more_than_ten_round1_papers_reach_pdf_and_independent_round2(self):
        # Prompt 的数字不由程序解释；模型返回全部合法项，必须完整下载并交接。
        RequirementsStore(self.case.root).save('round1', '选择最多 3 篇机器学习论文，不需要排序。')
        self.case.selected_indices = list(range(12, 0, -1))
        self.case.evaluation_by_candidate = {12: '机器学习 / score 0 / QNM', 11: 'x' * 1200}
        result = self.case.run_snapshot(self.case.snapshot(13))
        self.assertEqual(len(self.case.pdf_urls), 12)
        self.assertEqual(result.recommendation_count, 5)
        first = result.markdown.split('## Round 1 入围论文', 1)[1]
        self.assertIn('### 论文 12：', first)
        self.assertNotIn('Rank ', first)
        self.assertIn('机器学习 / score 0 / QNM', first)
        self.assertIn('x' * 1200, first)
        self.assertEqual(first.count('### 论文 '), 12)
        task = json.loads(self.case.requests[1][1]['input'][1]['content'])
        self.assertEqual(len(task['candidate_papers']), 12)
        for key in ('final_max_recommendations', 'max_recommendation_count', 'target_recommendation_count', 'research_profile', 'research_profile_version'):
            self.assertNotIn(key, task)
        self.assertNotIn('score 0', json.dumps(task, ensure_ascii=False))

    def test_round1_full_list_order_zero_and_old_protocol_rejection(self):
        papers = [fixtures.paper(i) for i in range(1, 14)]
        self.case.selected_indices = list(range(13, 0, -1))
        self.case.evaluation_by_candidate = {1: {'bad': 'type'}, 13: 'unrelated domain'}
        checked, _ = self.case.validate_round1(papers)
        self.assertEqual([p['candidate_index'] for p in checked['selected_papers']], self.case.selected_indices)
        self.assertEqual(checked['actual_selected_count'], 13)
        self.assertTrue(main.screening_stage_audit_valid(checked['selection_audit'], expected_accepted_count=13))
        self.case.selected_indices = []
        checked, _ = self.case.validate_round1(papers)
        self.assertTrue(checked['batch_valid'])
        self.assertEqual(checked['actual_selected_count'], 0)
        raw = self.case.round1_payload()
        raw.update(prompt_version='round1_v22', selection_policy='top_k_daily_budget',
                   selection_policy_version='top_k_daily_budget_v5')
        checked, _ = main.validate_round1_result(raw, papers, profile_version='profile_v2',
            prompt_version=main.CURRENT_ROUND1_PROMPT_VERSION,
            selection_policy_version=self.config['round1_selection_policy_version'])
        self.assertFalse(checked['batch_valid'])

    def test_new_default_and_existing_saved_prompts_remain_independent(self):
        store = RequirementsStore(self.case.root)
        store.save('round1', '已保存的其他领域研究要求')
        store.save('round2', '第二轮保持独立')
        self.assertEqual(RequirementsStore(self.case.root).load('round1').text, '已保存的其他领域研究要求')
        store.save('round1', None)
        default = store.load('round1').text
        for text in ('最多 **10** 篇', '黑洞阴影', '# 入选说明', '1—3 句话', '`evaluation`',
                     '# 公式及文本格式注意事项', '不要求评分或排序'):
            self.assertIn(text, default)
        self.assertEqual(store.load('round2').text, '第二轮保持独立')

    def test_round2_is_invariant_to_round1_order_evaluation_and_legacy_fields(self):
        result = self.case.run_snapshot()
        connection = self.case.connect()
        with closing(sqlite3.connect(self.case.root / '.desktop-runtime/work/round2_inputs.sqlite')) as fulltext:
            fulltext.row_factory = sqlite3.Row
            before = self.bundle(connection, fulltext, result.run_id)
            # 同一入围集合改变全部第一轮研究信号，第二轮请求和缓存仍必须完全一致。
            connection.execute("UPDATE screening_results SET result_rank = 100-result_rank, reason = ?, score=999, confidence=999, details_json=? WHERE task_type='round1_abstract_screening'",
                               ('PRIVATE-R1-EVALUATION', json.dumps({'content_label': '新解', 'evidence': 'PRIVATE-R1-EVIDENCE'})))
            connection.commit()
            after = self.bundle(connection, fulltext, result.run_id)
        self.assertEqual(before.messages, after.messages)
        self.assertEqual(run_round2.build_round2_cache_identity(before, self.config),
                         run_round2.build_round2_cache_identity(after, self.config))
        task = json.loads(after.messages[1]['content'])
        identities = [(p['arxiv_id'], p['version']) for p in task['candidate_papers']]
        self.assertEqual(identities, sorted(identities))
        self.assertTrue(all(set(p) == {'arxiv_id', 'version', 'title', 'abstract_source', 'metadata_abstract', 'pdf_full_text'}
                            for p in task['candidate_papers']))
        for canary in ('PRIVATE-R1-EVALUATION', 'PRIVATE-R1-EVIDENCE', 'confidence', 'round1_rank'):
            self.assertNotIn(canary, after.messages[1]['content'])

    def test_token_tie_exclusion_is_independent_of_round1_research_rank(self):
        result = self.case.run_snapshot()
        connection = self.case.connect()
        with closing(sqlite3.connect(self.case.root / '.desktop-runtime/work/round2_inputs.sqlite')) as fulltext:
            fulltext.row_factory = sqlite3.Row
            baseline = self.bundle(connection, fulltext, result.run_id)
            self.config['limits']['max_round2_request_tokens'] = baseline.estimated_request_tokens - 1
            # 将大小估算固定为相同值，专门检查并列裁决不再依赖第一轮排名。
            original_estimate = round2_fulltext_state.conservative_value_token_estimate
            def tied_paper_estimate(value):
                return 100 if isinstance(value, dict) and 'pdf_full_text' in value else original_estimate(value)
            with patch('round2_fulltext_state.conservative_value_token_estimate', side_effect=tied_paper_estimate):
                before = self.bundle(connection, fulltext, result.run_id)
                connection.execute("UPDATE screening_results SET result_rank=100-result_rank, reason='changed' WHERE task_type='round1_abstract_screening'")
                connection.commit()
                after = self.bundle(connection, fulltext, result.run_id)
            self.assertEqual(before.messages, after.messages)
            self.assertEqual(before.token_budget_excluded_papers, after.token_budget_excluded_papers)
            self.assertTrue(before.token_budget_excluded_papers)
            self.assertEqual(before.token_budget_excluded_papers[0]['arxiv_id'], '2609.00002')

    def test_round2_optional_evaluations_do_not_filter_or_truncate(self):
        papers = [fixtures.paper(i) for i in range(1, 5)]
        values = ['其他 / QNM / English evaluation\n' + 'long ' * 500, '', {'bad': 'type'}, None]
        raw = dict(task_type=main.ROUND2_TASK_TYPE, selection_policy=main.ROUND2_SELECTION_POLICY,
                   profile_version='profile_v2', prompt_version=main.CURRENT_ROUND2_PROMPT_VERSION,
                   final_recommendations=[dict(arxiv_id=p['arxiv_id'], version=p['version'], evaluation=v)
                                          for p, v in zip(papers, values)])
        checked, _ = main.validate_round2_result(raw, papers,
                    profile_version='profile_v2', prompt_version=main.CURRENT_ROUND2_PROMPT_VERSION)
        self.assertTrue(checked['batch_valid'])
        self.assertEqual(len(checked['final_recommendations']), 4)
        self.assertEqual(checked['final_recommendations'][0]['evaluation'], values[0])
        self.assertEqual(checked['selection_audit']['evaluation_discarded_count'], 2)
        self.assertEqual(checked['selection_audit']['excluded_count'], 0)

    def test_round2_user_count_ranking_cache_and_history_end_to_end(self):
        from desktop.history import HistoryStore
        store = RequirementsStore(self.case.root)
        self.case.selected_indices = list(range(12, 0, -1))
        order = [8, 2, 11, 0, 7, 4, 10, 3, 9, 1, 6, 5]
        self.case.recommendations_order = order
        # 用户数量只是给模型的文字，程序不解析、截取或自动补选。
        for requested, returned in ((0, 0), (1, 1), (3, 6), (20, 12)):
            with self.subTest(requested=requested, returned=returned):
                custom = f'选择最多 {requested} 篇机器学习论文，按实验可复现性排序。'
                store.save('round2', custom)
                self.case.recommendations_limit = returned
                self.case.round2_evaluations = {'2609.00009': '跨领域 / score -999 / ' + '长评价' * 300,
                                                '2609.00003': {'invalid': True}}
                result = self.case.run_snapshot(self.case.snapshot(12))
                connection = self.case.connect()
                expected = [f'2609.{i+1:05d}' for i in order[:returned]]
                rows = connection.execute("SELECT arxiv_id,result_rank,reason FROM screening_results WHERE task_type=? ORDER BY result_rank", (main.ROUND2_TASK_TYPE,)).fetchall()
                self.assertEqual([row['arxiv_id'] for row in rows], expected)
                self.assertEqual([row['result_rank'] for row in rows], list(range(1, returned+1)))
                self.assertEqual(result.recommendation_count, returned)
                section = result.markdown.split('## Round 1 入围论文')[0]
                self.assertEqual(section.count('### Rank '), returned)
                ranks = [section.index(f'### Rank {i+1}：') for i in range(returned)]
                self.assertEqual(ranks, sorted(ranks))
                if returned:
                    self.assertIn('长评价' * 300, result.markdown)
                with closing(sqlite3.connect(self.case.root / '.desktop-runtime/work/round2_inputs.sqlite')) as fulltext:
                    fulltext.row_factory = sqlite3.Row
                    bundle = run_round2.build_round2_input_bundle(connection, self.config, self.profile, self.prompt,
                        run_id=result.run_id, fulltext_connection=fulltext, research_prompt=custom)
                task = json.loads(bundle.messages[1]['content'])
                self.assertEqual(task['research_prompt'], custom)
                self.assertNotIn('final_max_recommendations', task)
                state, cached, _ = run_round2.load_valid_round2_cache(connection, bundle, self.config)
                self.assertEqual(state, 'cache_hit')
                self.assertEqual([p['arxiv_id'] for p in cached['final_recommendations']], expected)
                self.assertEqual(cached['selection_audit']['ignored_over_budget_count'], 0)
                history = HistoryStore(self.case.root)
                history.save(result)
                self.assertEqual(history.read(result.operation_id).markdown, result.markdown)
                self.assertEqual(history.read(result.operation_id).summary.recommendation_count, returned)
                # 即使旧缓存伪造了当前 cache_key，旧协议/策略也不能复用。
                connection.execute("UPDATE batch_cache SET prompt_version='round2_v17', selection_policy_version='full_text_budget_exclusion_v4' WHERE task_type=?", (main.ROUND2_TASK_TYPE,))
                connection.commit()
                self.assertEqual(run_round2.load_valid_round2_cache(connection, bundle, self.config)[0], 'cache_miss')
                # Windows 下须释放上一轮工作库连接，下一 attempt 才能重置工作库。
                connection.close()

    def test_round2_invalid_items_after_fifth_do_not_hide_later_valid_papers(self):
        papers = [fixtures.paper(i) for i in range(1, 13)]
        entries = [dict(arxiv_id=p['arxiv_id'], version='v1') for p in reversed(papers)]
        entries[0]['evaluation'] = '任意标签 / score 0'
        entries[1]['evaluation'] = '\x00'
        entries[2]['evaluation'] = '\ud800'
        entries[3]['evaluation'] = 'x' * 2000
        entries.insert(6, dict(entries[0]))
        entries.insert(8, dict(arxiv_id='outside', version='v1'))
        entries.insert(10, False)
        raw = dict(task_type=main.ROUND2_TASK_TYPE, selection_policy=main.ROUND2_SELECTION_POLICY,
                   profile_version='profile_v2', prompt_version=main.CURRENT_ROUND2_PROMPT_VERSION,
                   final_recommendations=entries)
        checked, _ = main.validate_round2_result(raw, papers, profile_version='profile_v2', prompt_version=main.CURRENT_ROUND2_PROMPT_VERSION)
        self.assertTrue(checked['batch_valid'])
        self.assertEqual([p['arxiv_id'] for p in checked['final_recommendations']], [p['arxiv_id'] for p in reversed(papers)])
        self.assertEqual([p['final_rank'] for p in checked['final_recommendations']], list(range(1, 13)))
        self.assertEqual(checked['selection_audit']['excluded_count'], 3)
        self.assertEqual(checked['selection_audit']['evaluation_discarded_count'], 2)
        raw['prompt_version'] = 'round2_v17'
        checked, _ = main.validate_round2_result(raw, papers, profile_version='profile_v2', prompt_version=main.CURRENT_ROUND2_PROMPT_VERSION)
        self.assertFalse(checked['batch_valid'])

    def test_round2_default_is_editable_and_tool_has_no_count_cap(self):
        store = RequirementsStore(self.case.root)
        default = store.default('round2')
        for text in ('最多 **5** 篇', '# 目标', '# 边界', '# 入选说明', '1—3 句话', '优先阅读顺序', 'pypdf', '并非原始 LaTeX'):
            self.assertIn(text, default)
        custom = '最多 20 篇计算机论文，按实验质量排序。'
        store.save('round2', custom)
        self.assertEqual(RequirementsStore(self.case.root).load('round2').text, custom)
        store.save('round2', None)
        self.assertEqual(store.load('round2').text, default)
        self.assertNotIn('final_max_recommendations', self.config)
        schema = run_round2.build_round2_result_tool(self.config)['parameters']['properties']['final_recommendations']
        self.assertNotIn('maxItems', schema)
        fixed = self.paths['round2_prompt'].read_text(encoding='utf-8')
        for forbidden in ('黑洞', '1—3', '最多 5', 'final_max_recommendations'):
            self.assertNotIn(forbidden, fixed)

    def test_both_rounds_check_all_items_without_filling_exclusions(self):
        papers = [fixtures.paper(i) for i in range(1, 13)]
        self.case.selected_indices = [1, 1, True, 999, 2, 3, 4, 5, 6, 7, 8, 9]
        raw = self.case.round1_payload()
        checked, _ = main.validate_round1_result(raw, papers,
            profile_version='profile_v2', prompt_version=main.CURRENT_ROUND1_PROMPT_VERSION,
            selection_policy_version=self.config['round1_selection_policy_version'])
        self.assertEqual([p['candidate_index'] for p in checked['selected_papers']], [1, 2, 3, 4, 5, 6, 7, 8, 9])
        self.assertEqual(checked['selection_audit']['ignored_over_budget_count'], 0)
        self.assertEqual(checked['selection_audit']['excluded_count'], 3)
        raw2 = dict(task_type=main.ROUND2_TASK_TYPE, selection_policy=main.ROUND2_SELECTION_POLICY,
                    profile_version='profile_v2', prompt_version=main.CURRENT_ROUND2_PROMPT_VERSION,
                    final_recommendations=[dict(arxiv_id=p['arxiv_id'], version=p['version'])
                                           for p in [papers[0], papers[0], dict(papers[1], version=99), papers[2], papers[3], papers[4]]])
        checked, _ = main.validate_round2_result(raw2, papers,
            profile_version='profile_v2', prompt_version=main.CURRENT_ROUND2_PROMPT_VERSION)
        self.assertEqual([p['arxiv_id'] for p in checked['final_recommendations']],
                         [papers[0]['arxiv_id'], papers[2]['arxiv_id'], papers[3]['arxiv_id'], papers[4]['arxiv_id']])
        self.assertEqual(checked['selection_audit']['ignored_over_budget_count'], 0)

    def test_default_format_reminders_can_be_deleted_saved_and_restored(self):
        store = RequirementsStore(self.case.root)
        for stage in ('round1', 'round2'):
            default = store.default(stage)
            self.assertIn('公式及文本格式注意事项', default)
            store.save(stage, '只关注我的研究目标')
            self.assertNotIn('格式', RequirementsStore(self.case.root).load(stage).text)
            store.save(stage, None)
            self.assertEqual(store.load(stage).text, default)

    def test_named_tool_has_only_identity_required_and_no_research_enums(self):
        tool = run_round2.build_round2_result_tool(self.config)
        item = tool['parameters']['properties']['final_recommendations']['items']
        self.assertEqual(item['required'], ['arxiv_id', 'version'])
        self.assertEqual(set(item['properties']), {'arxiv_id', 'version', 'evaluation'})
        for resource in (self.paths['round1_prompt'], self.paths['round2_prompt']):
            text = resource.read_text(encoding='utf-8')
            for forbidden in ('成像', '新解', 'deep_read', '查准率优先', 'pypdf', '宁可不选'):
                self.assertNotIn(forbidden, text)

    def test_evaluation_is_saved_exactly_but_rendered_as_safe_text(self):
        evaluation = '任意标签\n[unsafe](file:///C:/private) <script>bad</script>\n' + 'x' * 600
        self.case.evaluation_by_candidate[1] = evaluation
        result = self.case.run_snapshot()
        connection = self.case.connect()
        self.assertEqual(connection.execute("SELECT reason FROM screening_results WHERE task_type='round1_abstract_screening' AND result_rank=1").fetchone()[0], evaluation)
        self.assertIn('x' * 600, result.markdown)
        self.assertNotIn('[unsafe](', result.markdown)
        self.assertNotIn('<script>', result.markdown)
