# Copyright (c) 2026 yyyang20
# SPDX-License-Identifier: GPL-3.0-only
# See LICENSE in the project root for the full license text.

"""调用表只记录 Token；旧工作库的额外列不参与当前审计。"""
from contextlib import closing
from types import SimpleNamespace
import sqlite3
import unittest

import main
import model_usage


class ModelUsageTests(unittest.TestCase):
    def record(self, connection, *, usage=None, ok=True):
        if usage is None:
            usage = SimpleNamespace(input_tokens=100, output_tokens=20, total_tokens=120,
                                    cache_hit_input_tokens=30, cache_miss_input_tokens=70,
                                    complete=True)
        attempt = SimpleNamespace(attempt_no=1, status='success' if ok else 'api_error',
            http_status=200 if ok else 500, error_type=None if ok else 'api_error',
            model_returned='deepseek-flash', response_id=None, finish_reason='stop',
            usage=usage, started_at='2026-10-14T00:00:00+00:00',
            finished_at='2026-10-14T00:00:01+00:00', duration_ms=1000)
        return model_usage.record_api_call(connection, run_id=1,
            task_type='round1_abstract_screening', call_purpose='screening',
            model_requested='deepseek-flash', prompt_version='round1_v23',
            research_profile_version='profile_v2', selection_policy_version='user_prompt_selection_v1',
            request_hash='synthetic-request', local_cache_status='miss',
            call_result=SimpleNamespace(ok=ok, error_type=attempt.error_type, attempts=(attempt,)))

    def test_new_attempt_schema_and_summary_keep_all_token_fields(self):
        with closing(sqlite3.connect(':memory:')) as connection:
            main.initialize_database_schema(connection)
            columns = {row[1] for row in connection.execute('PRAGMA table_info(model_call_attempts)')}
            self.assertEqual(columns, {
                'attempt_id', 'call_id', 'attempt_no', 'attempt_status', 'http_status',
                'error_type', 'model_returned', 'response_id', 'finish_reason',
                'input_tokens', 'output_tokens', 'total_tokens', 'cache_hit_input_tokens',
                'cache_miss_input_tokens', 'usage_source', 'usage_complete',
                'started_at', 'finished_at', 'duration_ms',
            })
            self.record(connection)
            usage = model_usage.load_run_usage_summary(connection, 1)
            self.assertEqual(vars(usage), dict(call_count=1, api_attempt_count=1,
                local_cache_hit_count=0, known_input_tokens=100, known_output_tokens=20,
                known_total_tokens=120, token_complete=True))
            self.assertEqual(connection.execute(
                'SELECT cache_hit_input_tokens, cache_miss_input_tokens, usage_source, usage_complete '
                'FROM model_call_attempts').fetchone(), (30, 70, 'api_response', 1))

    def test_legacy_required_column_is_neutral_and_old_values_are_ignored(self):
        with closing(sqlite3.connect(':memory:')) as connection:
            main.initialize_database_schema(connection)
            # 模拟 v1 的旧列；非数值旧数据必须原样保留，不能影响 Token 汇总。
            original_sql = connection.execute(
                "SELECT sql FROM sqlite_master WHERE name='model_call_attempts'").fetchone()[0]
            connection.execute('DROP TABLE model_call_attempts')
            connection.execute(original_sql.replace('duration_ms INTEGER,',
                'duration_ms INTEGER, cost_status TEXT NOT NULL, total_cost TEXT, currency TEXT,'))
            self.record(connection)
            connection.execute("UPDATE model_call_attempts SET cost_status='legacy', total_cost='invalid-old-value', currency='legacy'")
            connection.execute('PRAGMA user_version = 1')
            main.initialize_database_schema(connection)
            self.assertEqual(connection.execute('PRAGMA user_version').fetchone()[0], 2)
            self.record(connection)
            self.assertEqual(connection.execute(
                'SELECT cost_status, total_cost, currency FROM model_call_attempts ORDER BY attempt_id'
            ).fetchall(), [('legacy', 'invalid-old-value', 'legacy'), ('', None, None)])
            usage = model_usage.load_run_usage_summary(connection, 1)
            self.assertEqual(usage.known_total_tokens, 240)
            self.assertTrue(usage.token_complete)

    def test_failed_attempt_keeps_tokens_and_incomplete_usage_remains_incomplete(self):
        with closing(sqlite3.connect(':memory:')) as connection:
            main.initialize_database_schema(connection)
            usage = SimpleNamespace(input_tokens=100, output_tokens=None, total_tokens=None,
                                    cache_hit_input_tokens=None, cache_miss_input_tokens=None,
                                    complete=False)
            self.record(connection, usage=usage, ok=False)
            summary = model_usage.load_run_usage_summary(connection, 1)
            self.assertEqual(summary.api_attempt_count, 1)
            self.assertEqual(summary.known_input_tokens, 100)
            self.assertFalse(summary.token_complete)

    def test_cache_reuse_and_stage_timing_do_not_add_api_attempts(self):
        with closing(sqlite3.connect(':memory:')) as connection:
            main.initialize_database_schema(connection)
            model_usage.record_cache_reuse(connection, run_id=1,
                task_type='round2_fulltext_screening', call_purpose='screening',
                model_requested='deepseek-flash', prompt_version='round2_v18',
                research_profile_version='profile_v2', selection_policy_version='full_text_budget_exclusion_v5',
                request_hash='synthetic-cache')
            summary = model_usage.load_run_usage_summary(connection, 1)
            self.assertEqual((summary.api_attempt_count, summary.local_cache_hit_count), (0, 1))
            self.assertEqual(summary.known_total_tokens, 0)
            self.assertTrue(summary.token_complete)
            model_usage.record_run_stage_timing(connection, run_id=1, stage_name='round2',
                started_at='2026-10-14T00:00:00+00:00', finished_at='2026-10-14T00:00:01+00:00',
                duration_seconds=1)
            self.assertEqual(model_usage.load_run_stage_duration_seconds(connection, 1, 'round2'), 1)
