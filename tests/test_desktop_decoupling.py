# Copyright (c) 2026 yyyang20
# SPDX-License-Identifier: GPL-3.0-only
# See LICENSE in the project root for the full license text.

from __future__ import annotations

import ast
import copy
from contextlib import ExitStack, closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import main
import model_usage
import round2_fulltext_state
import run_round2
import test_desktop_analysis as fixtures

ROOT = Path(__file__).resolve().parents[1]
CORE_MODULES = (
    'main.py', 'build_round2_inputs.py', 'run_round2.py', 'pdf_processing.py',
    'round2_fulltext_state.py', 'model_usage.py', 'deepseek_client.py',
    'generate_round2_report.py', 'rebuild_daily_report.py', 'content_labels.py',
    'arxiv_transport_evidence.py',
)

# 在导入提交 931505e 上、裁剪前记录的合成场景；不保存全文或模型响应。
BASELINE = {
    'normal': 'f368549f95d3b8c657e1909df2ee150bca6a3d4386f44b62c223de44a8080e5c',
    'round1_zero': '977c6efcaff5955549f6efb639ebca607ba33006cf761a8497a14f12965172ee',
    'round2_zero': 'bd30780605b4e02a9c21a22ad728b80b252418989b180c7ef6c613da7b4ac297',
    'empty': '52da27d9fc21d289de95082d7e13dc4c8a8e1ef9215a6e83224e71990987f228',
    'pdf_gates': 'a8be59df6c7d5502e3685c54f3cc082892c05ad672192ff36e435038b9737259',
    'token_gate': 'd9983154ec980247abe96a234ca07aaa3d37e55f5b5dbb4ffd55f8039259f879',
}


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':')).encode('utf-8')).hexdigest()


def legacy_text(value):
    return value.replace('round1_v21', 'round1_v20').replace('round2_v16', 'round2_v15')


def legacy_request(stage, payload):
    # alpha 10 有意改变 Prompt 及请求身份；归一化这两处后核验旧业务回归。
    payload = copy.deepcopy(payload)
    messages = payload['messages' if stage == 'round1' else 'input']
    old = 'prompts/relevance_round1_v20.txt' if stage == 'round1' else 'prompts/relevance_round2_v15.txt'
    original = (ROOT / old).read_text(encoding='utf-8-sig')
    messages[0]['content'] = original.strip() if stage == 'round1' else original
    task = json.loads(messages[1]['content'])
    task.pop('research_requirements')
    messages[1]['content'] = json.dumps(task, ensure_ascii=False, separators=(',', ':'))
    return json.loads(legacy_text(json.dumps(payload, ensure_ascii=False)))


class DesktopDecouplingTests(unittest.TestCase):
    def test_alpha4_requests_reports_results_pdf_order_usage_and_cost_are_identical(self):
        price = model_usage.price_snapshot_from_config
        for scenario, expected in BASELINE.items():
            with self.subTest(scenario=scenario), ExitStack() as stack:
                case = fixtures.DesktopAnalysisTests()
                case.setUp()
                stack.callback(case.doCleanups)
                stack.enter_context(patch('model_usage.price_snapshot_from_config',
                    side_effect=lambda *a, **kw: price(*a, **{**kw, 'at': datetime(2026, 9, 25, tzinfo=timezone.utc)})))
                count = 4
                if scenario == 'round1_zero':
                    case.selected_indices = []
                elif scenario == 'round2_zero':
                    case.recommendations_limit = 0
                elif scenario == 'empty':
                    count = 0
                elif scenario == 'pdf_gates':
                    case.selected_indices = [1, 2, 3, 4]
                    case.page_counts = {'2609.00004v1': 60, '2609.00003v1': 61}
                    case.fail_pdf = {'2609.00002v1'}
                    case.fail_extract = {'2609.00001v1'}
                elif scenario == 'token_gate':
                    config = main.load_config(case.root / 'config.json')
                    config['limits']['max_round2_request_tokens'] = 1
                    stack.enter_context(patch('main.load_config', return_value=config))
                result = case.run_snapshot(case.snapshot(count))
                conn = case.connect()
                rows = conn.execute('''
                    SELECT task_type, arxiv_id, version, result_rank, recommendation_level,
                           score, confidence, reason, details_json, model_name,
                           prompt_version, research_profile_version
                    FROM screening_results ORDER BY task_type, result_rank
                ''').fetchall()
                usage = model_usage.load_run_usage_summary(conn, result.run_id)
                self.assertIn("- 抓取日期（北京时间）：", result.markdown)
                # alpha 8 只改日期标题；归一化这一处后仍核对旧请求、结果、费用与正文基线。
                baseline_markdown = legacy_text(result.markdown).replace(
                    "- 抓取日期（北京时间）：", "- 本次候选日期（UTC）："
                )
                actual = {
                    'requests': [[stage, digest(legacy_request(stage, payload))] for stage, payload in case.requests],
                    'markdown_sha256': hashlib.sha256(baseline_markdown.encode('utf-8')).hexdigest(),
                    'results': [[legacy_text(item) if isinstance(item, str) else item for item in row] for row in rows],
                    'pdf_urls': case.pdf_urls,
                    'usage': {'attempts': usage.api_attempt_count, 'tokens': usage.known_total_tokens,
                              'cost': str(usage.known_cost)} if usage else None,
                }
                self.assertEqual(digest(actual), expected)

    def test_work_schema_contains_only_desktop_tables_and_rejects_unknown_version(self):
        # 连接上下文只管理事务；closing 在事务退出后显式关闭连接，包括异常路径。
        with closing(sqlite3.connect(':memory:')) as conn, conn:
            main.initialize_database_schema(conn)
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertEqual(tables - {'sqlite_sequence'}, {
                'papers', 'runs', 'batch_cache', 'screening_results', 'screening_stage_audits',
                'screening_completion', 'pdf_downloads', 'model_calls', 'model_call_attempts',
                'run_stage_timings',
            })
            self.assertEqual(conn.execute('PRAGMA user_version').fetchone()[0], 1)
            main.initialize_database_schema(conn)
            conn.execute('PRAGMA user_version = 999')
            with self.assertRaisesRegex(RuntimeError, 'schema_version'):
                main.initialize_database_schema(conn)
        with closing(sqlite3.connect(':memory:')) as conn, conn:
            round2_fulltext_state.initialize_fulltext_schema(conn)
            round2_fulltext_state.validate_fulltext_schema(conn)
            conn.execute('DROP TABLE round2_input_decisions')
            with self.assertRaisesRegex(RuntimeError, 'decisions_schema'):
                round2_fulltext_state.validate_fulltext_schema(conn)

    def test_round2_cache_is_revalidated_and_reuse_never_calls_model(self):
        case = fixtures.DesktopAnalysisTests()
        case.setUp()
        try:
            result = case.run_snapshot(case.snapshot(4))
            conn = case.connect()
            config, _paths, profile, prompt = run_round2.read_round2_context(case.root)
            fulltext_path = case.root / '.desktop-runtime/work/round2_inputs.sqlite'
            # 保留事务上下文，并在构建输入结束后关闭全文库连接，避免回收时告警。
            with closing(sqlite3.connect(fulltext_path)) as fulltext, fulltext:
                fulltext.row_factory = sqlite3.Row
                bundle = run_round2.build_round2_input_bundle(
                    conn, config, profile, prompt, run_id=result.run_id,
                    fulltext_connection=fulltext,
                    research_requirements=_paths['round2_requirements'].read_text(encoding='utf-8'),
                )
            status, cached, warnings = run_round2.load_valid_round2_cache(conn, bundle, config)
            self.assertEqual(status, 'cache_hit')
            self.assertEqual(len(cached['final_recommendations']), 3)
            self.assertFalse(warnings)
            with patch('run_round2.create_deepseek_client', side_effect=AssertionError('model')):
                _cached, _warnings, status = run_round2.run_round2_model_and_save(
                    conn, case.root, config, bundle, api_key_override='synthetic-session-key',
                )
            self.assertEqual(status, 'cache_hit')
            self.assertEqual(len(case.requests), 2)
            conn.execute("UPDATE batch_cache SET response_json = '{}' ")
            conn.commit()
            status, cached, _warnings = run_round2.load_valid_round2_cache(conn, bundle, config)
            self.assertEqual(status, 'cache_miss')
            self.assertIsNone(cached)
        finally:
            case.doCleanups()

    def test_desktop_configured_budget_blocks_before_any_model_request(self):
        case = fixtures.DesktopAnalysisTests()
        case.setUp()
        try:
            config = main.load_config(case.root / 'config.json')
            config['budget']['max_batch_cost_cny'] = '0.00001'
            with patch('main.load_config', return_value=config):
                with self.assertRaises(fixtures.analysis.AnalysisError):
                    case.run_snapshot(case.snapshot(4))
            self.assertFalse(case.requests)
        finally:
            case.doCleanups()

    def test_transitive_runtime_imports_and_removed_online_entry_points(self):
        sources = [ROOT / name for name in CORE_MODULES] + list((ROOT / 'desktop').glob('*.py'))
        forbidden = {'daily_report_template', 'selection_nature', 'automation', 'github'}
        for source in sources:
            tree = ast.parse(source.read_text(encoding='utf-8'))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imports = [a.name.split('.')[0] for a in node.names]
                elif isinstance(node, ast.ImportFrom):
                    imports = [(node.module or '').split('.')[0]]
                else:
                    continue
                self.assertFalse(forbidden & set(imports), source.name)
        for name in ('main', 'load_recent_papers', 'filter_completed_round1_candidates',
                     'load_local_api_key', 'inspect_deepseek_configuration', 'DEFAULT_CONFIG'):
            self.assertFalse(hasattr(main, name), name)
        for name in ('config/automation_policy.json', 'config/research_profile.json',
                     'profiles/research_profile.md', 'daily_report_template.py', 'selection_nature.py'):
            self.assertFalse((ROOT / name).exists(), name)

    def test_two_rounds_run_from_isolated_source_copy_without_online_resources_or_docs(self):
        from scripts.local_artifacts import test_scratch
        scratch = test_scratch(ROOT)
        scratch.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=scratch) as directory:
            isolated = Path(directory) / 'standalone'
            outside = Path(directory) / 'outside-source'
            isolated.mkdir()
            outside.mkdir()
            names = list(CORE_MODULES) + ['config.json', 'tests/test_desktop_analysis.py',
                                       'tests/test_desktop_pipeline.py', 'scripts/local_artifacts.py']
            names += [str(p.relative_to(ROOT)) for p in (ROOT / 'desktop').glob('*.py')]
            names += list(json.loads((ROOT / 'config.json').read_text(encoding='utf-8'))['paths'].values())
            for name in names:
                target = isolated / name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(ROOT / name, target)
            # 子进程只有明确复制的运行文件，工作目录也与源码分离。
            code = '''
import sys
from pathlib import Path
root = Path(sys.argv[1])
sys.path[:0] = [str(root), str(root / 'tests')]
import test_desktop_analysis as fixture
case = fixture.DesktopAnalysisTests()
case.setUp()
try:
    result = case.run_snapshot(case.snapshot(4))
    assert [stage for stage, _ in case.requests] == ['round1', 'round2']
    assert result.recommendation_count == 3
    assert 'Round 2 最终推荐数量：3' in result.markdown
    assert not (root / 'docs').exists()
finally:
    case.doCleanups()
print('standalone_two_rounds_ok')
'''
            env = {k: v for k, v in os.environ.items() if not k.upper().startswith('PYTHON')}
            env.update(TEMP=str(outside), TMP=str(outside), QT_QPA_PLATFORM='offscreen')
            # 隔离源码有独立根目录，不能继承父测试入口的相对 scratch。
            env.pop('ARXIVKALEID_TEST_SCRATCH', None)
            result = subprocess.run([sys.executable, '-I', '-B', '-X', 'utf8', '-c', code, str(isolated)],
                                    cwd=outside, env=env, capture_output=True, text=True,
                                    encoding='utf-8', timeout=60)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('standalone_two_rounds_ok', result.stdout)
