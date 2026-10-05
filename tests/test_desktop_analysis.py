# Copyright (c) 2026 yyyang20
# SPDX-License-Identifier: GPL-3.0-only
# See LICENSE in the project root for the full license text.

from __future__ import annotations

import copy
import io
import json
import sqlite3
import sys
import tempfile
import unittest
from contextlib import ExitStack, nullcontext
from datetime import date, datetime, timezone
from pathlib import Path
from types import MappingProxyType
from unittest.mock import patch

import deepseek_client
import main
import model_usage
import round2_fulltext_state
import run_round2
from desktop import analysis, pipeline
from desktop.diagnostics import DesktopDiagnostics
from test_desktop_pipeline import DAY, PROJECT_ROOT, paper


class Response(io.BytesIO):
    status = 200


class DesktopAnalysisTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        scratch = PROJECT_ROOT / ".codex-validation"
        scratch.mkdir(exist_ok=True)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory(dir=scratch)))
        for name in ("config.json", "prompts/relevance_round1_v20.txt", "prompts/relevance_round2_v15.txt"):
            target = self.root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes((PROJECT_ROOT / name).read_bytes())
        self.stack.enter_context(patch.object(pipeline, "PROJECT_ROOT", self.root))
        self.forbidden = [self.stack.enter_context(patch(target, side_effect=AssertionError("forbidden operation"))) for target in (
            "main.fetch_arxiv_metadata", "desktop.pipeline.fetch_candidates_for_date",
            "urllib.request.urlopen",
        )]
        client_type = deepseek_client.DeepSeekClient
        self.stack.enter_context(patch("main.DeepSeekClient", side_effect=lambda **kw: client_type(**kw, opener=self.http)))
        self.stack.enter_context(patch("pdf_processing.fetch_pdf_bytes", side_effect=self.pdf))
        self.stack.enter_context(patch("pdf_processing.time.sleep"))
        self.stack.enter_context(patch("round2_fulltext_state.extract_pages_with_gate", side_effect=self.pages))
        self.requests = []
        self.pdf_urls = []
        self.selected_indices = [1, 2, 3]
        self.evidence_by_candidate = {}
        self.recommendations_limit = 5
        self.page_counts = {}
        self.fail_pdf = set()
        self.fail_extract = set()
        self.fail_stage = None
        self.invalid_stage = None
        self.missing_usage = False
        self.extra_output_tokens = 0

    def snapshot(self, count=4):
        return pipeline.CandidateSnapshot(
            DAY, datetime.now(timezone.utc), count, count,
            tuple(MappingProxyType(paper(n)) for n in range(count, 0, -1)),
        )

    def http(self, request, **kwargs):
        stage = "round2" if request.full_url.endswith("/responses") else "round1"
        self.assertEqual(request.get_header("Authorization"), "Bearer fake-desktop-key")
        payload = json.loads(request.data)
        self.requests.append((stage, payload))
        if self.fail_stage == stage:
            raise TimeoutError("synthetic-private-response")
        if stage == "round1":
            data = self.round1_payload()
            result = dict(model="deepseek-flash", choices=[dict(message=dict(content=json.dumps(data)), finish_reason="stop")],
                          usage=dict(prompt_tokens=100, completion_tokens=20 + self.extra_output_tokens,
                                     total_tokens=120 + self.extra_output_tokens, prompt_cache_hit_tokens=0, prompt_cache_miss_tokens=100))
        else:
            # 从真正构造的全文消息中读取输入身份，覆盖工具传输和共享校验器。
            task = json.loads(payload["input"][1]["content"])
            candidates = task["candidate_papers"]
            data = dict(task_type=main.ROUND2_TASK_TYPE, selection_policy=main.ROUND2_SELECTION_POLICY,
                        profile_version="profile_v2", prompt_version="round2_v15",
                        final_recommendations=[dict(arxiv_id=p["arxiv_id"], version=p["version"], content_label="成像", reason="全文给出黑洞成像结果。") for p in candidates[:self.recommendations_limit]])
            result = dict(model="deepseek-flash", status="completed",
                          output=[dict(type="function_call", name="submit_round2_results", arguments=json.dumps(data))],
                          usage=dict(input_tokens=200, output_tokens=30, total_tokens=230, input_tokens_details=dict(cached_tokens=0)))
        if self.invalid_stage == stage:
            if stage == "round1":
                result["choices"][0]["message"]["content"] = "synthetic-private-invalid-json"
            else:
                result["output"] = []
        if self.missing_usage:
            result.pop("usage")
        return Response(json.dumps(result).encode())

    def round1_payload(self):
        # 合成响应可携带真实校验器需要的证据；未配置时保留原有测试场景。
        selected = []
        for index in self.selected_indices:
            item = dict(candidate_index=index, content_label="成像", reason="生成黑洞阴影图像。")
            if index in self.evidence_by_candidate:
                item["evidence"] = self.evidence_by_candidate[index]
            selected.append(item)
        return dict(task_type="round1_abstract_screening", profile_version="profile_v2",
                    prompt_version="round1_v20", selection_policy="top_k_daily_budget",
                    selection_policy_version="top_k_daily_budget_v4", selected_papers=selected)

    def validate_round1(self, papers):
        return main.validate_round1_result(
            self.round1_payload(), papers, max_selected=10,
            profile_version="profile_v2", prompt_version="round1_v20",
            selection_policy_version="top_k_daily_budget_v4",
        )

    def test_round1_nonempty_evidence_normalizes_unicode_and_whitespace(self):
        self.selected_indices = [1]
        candidate = dict(paper(1), title="Ｓｙｎｔｈｅｔｉｃ　paper",
                         summary="Synthetic\n  abstract")
        self.evidence_by_candidate[1] = [
            dict(source="metadata_title", quote="  Synthetic   paper  "),
            dict(source="metadata_abstract", quote="Ｓｙｎｔｈｅｔｉｃ　abstract"),
        ]
        validated, warnings = self.validate_round1([candidate])
        self.assertTrue(validated["batch_valid"])
        self.assertEqual(warnings, [])
        selected = validated["selected_papers"][0]
        self.assertEqual(selected["evidence"], [
            dict(source="metadata_title", quote="Synthetic paper"),
            dict(source="metadata_abstract", quote="Synthetic abstract"),
        ])
        self.assertEqual(selected["evidence_status"], "valid")
        self.assertEqual(selected["evidence_invalid_count"], 0)

    def test_round1_invalid_evidence_preserves_selection_and_order(self):
        self.selected_indices = [2, 1]
        self.evidence_by_candidate = {
            2: [dict(source="metadata_title", quote="Synthetic paper"),
                dict(source="metadata_abstract", quote="Unsupported evidence quote")],
            1: [dict(source="metadata_abstract", quote="Another unsupported quote")],
        }
        validated, warnings = self.validate_round1([paper(1), paper(2)])
        self.assertTrue(validated["batch_valid"])
        selected = validated["selected_papers"]
        self.assertEqual([item["candidate_index"] for item in selected], [2, 1])
        self.assertEqual([item["round1_rank"] for item in selected], [1, 2])
        self.assertEqual([item["evidence_status"] for item in selected], ["partial", "invalid"])
        self.assertEqual([item["evidence_invalid_count"] for item in selected], [1, 1])
        self.assertEqual(selected[1]["evidence"], [])
        self.assertEqual(validated["selection_audit"]["evidence_discarded_count"], 2)
        self.assertEqual(validated["selection_audit"]["excluded_count"], 0)
        self.assertTrue(warnings)

    def test_round1_nonempty_evidence_completes_mocked_two_rounds(self):
        self.evidence_by_candidate[1] = [
            dict(source="metadata_title", quote="Synthetic paper"),
            dict(source="metadata_abstract", quote="Synthetic abstract"),
        ]
        result = self.run_snapshot()
        self.assertEqual([stage for stage, _ in self.requests], ["round1", "round2"])
        self.assertEqual(len(self.pdf_urls), 3)
        conn = self.connect()
        self.assertEqual(conn.execute("SELECT status FROM runs").fetchone()[0], "success")
        details = [json.loads(row[0]) for row in conn.execute(
            "SELECT details_json FROM screening_results "
            "WHERE task_type = 'round1_abstract_screening' AND result_rank = 1"
        )]
        self.assertEqual(len(details), 1)
        self.assertEqual(details[0]["evidence_status"], "valid")
        self.assertEqual(details[0]["evidence"], self.evidence_by_candidate[1])
        self.assertEqual(model_usage.load_run_usage_summary(conn, result.run_id).api_attempt_count, 2)
        self.assertIn("Round 1 入围数量：3", result.markdown)
        self.assertIn("Round 2 最终推荐数量：3", result.markdown)
        for operation in self.forbidden:
            operation.assert_not_called()

    def pdf(self, url, **kwargs):
        self.pdf_urls.append(url)
        if url.rsplit("/", 1)[-1] in self.fail_pdf:
            from pdf_processing import PdfDownloadError
            raise PdfDownloadError("download_failed")
        return b"%PDF-1.7\n" + b"x" * 2048

    def pages(self, path, *, max_pdf_pages):
        self.assertTrue(path.is_relative_to(pipeline.runtime_path("pdfs")))
        self.assertEqual(max_pdf_pages, 60)
        if path.stem in self.fail_extract:
            raise round2_fulltext_state.PdfFullTextExtractionError("pdf_page_text_extraction_failed")
        count = self.page_counts.get(path.stem, 2)
        return count, tuple("private-fulltext-marker" for _ in range(count)) if count <= 60 else ()

    def run_snapshot(self, snapshot=None, progress=None):
        return analysis.AnalysisAttempt(snapshot or self.snapshot()).run(
            "fake-desktop-key", progress
        )

    def connect(self):
        conn = sqlite3.connect(pipeline.runtime_path("work", "arxiv_kaleid.sqlite"))
        conn.row_factory = sqlite3.Row
        self.addCleanup(conn.close)
        return conn

    def test_frozen_input_order_current_run_pdf_and_report_facts(self):
        snapshot = self.snapshot(57)
        result = self.run_snapshot(snapshot)
        task = json.loads(self.requests[0][1]["messages"][1]["content"])
        self.assertEqual([(p["arxiv_id"], p["version"]) for p in task["candidate_papers"]],
                         [(p["arxiv_id"], f"v{p['version']}") for p in snapshot.papers])
        self.assertEqual([s for s, _ in self.requests], ["round1", "round2"])
        self.assertEqual(len(self.pdf_urls), 3)
        self.assertEqual(self.pdf_urls, [p["pdf_url"] for p in snapshot.papers[:3]])
        conn = self.connect()
        usage = model_usage.load_run_usage_summary(conn, result.run_id)
        self.assertEqual(usage.api_attempt_count, 2)
        self.assertEqual(usage.known_total_tokens, 350)
        self.assertTrue(usage.cost_complete)
        self.assertIn("Round 1 输入数量：57", result.markdown)
        self.assertIn("Round 2 最终推荐数量：3", result.markdown)
        self.assertIn("PDF 实际页数：2", result.markdown)
        self.assertIn(format(usage.known_cost, "f"), result.markdown)
        for forbidden in ("fake-desktop-key", "private-fulltext-marker", "generation", "GitHub Issue", "automatic"):
            self.assertNotIn(forbidden, result.markdown)
        dump = "\n".join(conn.iterdump())
        self.assertNotIn("fake-desktop-key", dump)
        self.assertNotIn("private-fulltext-marker", dump)
        for operation in self.forbidden:
            operation.assert_not_called()
        self.assertFalse((self.root / "data").exists())
        self.assertFalse((self.root / "reports").exists())
        self.assertTrue(pipeline.runtime_path("work", "round2_inputs.sqlite").is_file())

    def test_two_round_diagnostics_correlate_run_without_sensitive_content(self):
        diagnostics = DesktopDiagnostics(self.root)
        self.addCleanup(diagnostics.close)
        snapshot = self.snapshot()
        snapshot = pipeline.CandidateSnapshot(
            snapshot.candidate_date, snapshot.completed_at, snapshot.raw_count,
            snapshot.unique_count, snapshot.papers,
            snapshot_id="snapshot-safe-id", fetch_operation_id="fetch-safe-id",
        )
        result = analysis.AnalysisAttempt(snapshot, diagnostics).run("fake-desktop-key")
        records = [
            json.loads(line)
            for line in next(diagnostics.log_directory.glob("desktop-*.jsonl"))
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        self.assertTrue(any(item["stage"] == "run_bound" and item["run_id"] == result.run_id for item in records))
        self.assertTrue(all(item["operation_id"] == result.operation_id for item in records))
        self.assertTrue(all(item.get("snapshot_id") == "snapshot-safe-id" for item in records))
        self.assertTrue(all(item.get("fetch_operation_id") == "fetch-safe-id" for item in records))
        serialized = json.dumps(records, ensure_ascii=False)
        for forbidden in ("fake-desktop-key", "private-fulltext-marker", "response_id", "Authorization"):
            self.assertNotIn(forbidden, serialized)

    def test_limits_preserve_model_order_without_promotion(self):
        self.selected_indices = list(range(12, 0, -1))
        self.recommendations_limit = 10
        result = self.run_snapshot(self.snapshot(12))
        self.assertEqual(len(self.pdf_urls), 10)
        self.assertTrue(self.pdf_urls[0].endswith("2609.00001v1"))
        self.assertIn("Round 1 入围数量：10", result.markdown)
        self.assertIn("Round 2 最终推荐数量：5", result.markdown)

    def test_frozen_two_rounds_use_bundled_resources_and_portable_data(self):
        resources = self.root / "_internal"
        resources.mkdir()
        for name in ("config.json", "prompts"):
            (self.root / name).rename(resources / name)
        before = {p.relative_to(resources): p.read_bytes() for p in resources.rglob("*") if p.is_file()}
        with patch.object(sys, "frozen", True, create=True), patch.object(sys, "executable", str(self.root / "arXivKaleid.exe")), patch.object(sys, "_MEIPASS", str(resources), create=True):
            result = self.run_snapshot()
            self.assertIn("Round 2 最终推荐数量：3", result.markdown)
            self.assertEqual([s for s, _ in self.requests], ["round1", "round2"])
            self.assertTrue((self.root / "runtime/work/arxiv_kaleid.sqlite").is_file())
            self.assertEqual(len(list((self.root / "runtime/pdfs").rglob("*.pdf"))), 3)
        self.assertFalse((self.root / ".desktop-runtime").exists())
        self.assertEqual(before, {p.relative_to(resources): p.read_bytes() for p in resources.rglob("*") if p.is_file()})

    def test_zero_round1_and_empty_snapshot_skip_model_as_needed(self):
        self.selected_indices = []
        result = self.run_snapshot()
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.pdf_urls, [])
        self.assertIn("未调用 Round 2", result.markdown)
        self.requests.clear()
        result = self.run_snapshot(self.snapshot(0))
        self.assertEqual(self.requests, [])
        self.assertIn("实际模型名称：未调用", result.markdown)

    def test_zero_round2_is_success(self):
        self.recommendations_limit = 0
        result = self.run_snapshot()
        self.assertIn("Round 2 最终推荐数量：0", result.markdown)
        self.assertEqual(len(self.requests), 2)

    def test_all_normal_zero_outcomes_generate_and_save_exact_reports(self):
        import importlib.util
        if importlib.util.find_spec("qfluentwidgets") is None:
            self.skipTest("Desktop GUI dependencies are absent")
        from desktop.app import AnalysisWorker
        from desktop.history import HistoryStore
        store = HistoryStore(self.root)
        identities = []
        for scenario in ("round1_zero", "round2_zero", "no_fulltext", "empty_snapshot"):
            with self.subTest(scenario=scenario):
                self.selected_indices = [] if scenario == "round1_zero" else [1, 2, 3]
                self.recommendations_limit = 0 if scenario == "round2_zero" else 5
                snapshot = self.snapshot(0 if scenario == "empty_snapshot" else 4)
                self.fail_pdf = {p["pdf_url"] for p in snapshot.papers} if scenario == "no_fulltext" else set()
                self.fail_extract = {f"{p['arxiv_id']}v{p['version']}" for p in snapshot.papers} if scenario == "no_fulltext" else set()
                attempt = analysis.AnalysisAttempt(snapshot)
                worker = AnalysisWorker(attempt, "fake-desktop-key", history_store=store)
                worker.run()
                self.assertIsNone(worker.issue)
                self.assertIsNone(worker.history_issue)
                self.assertTrue(worker.history_saved)
                self.assertEqual(worker.result.recommendation_count, 0)
                self.assertEqual(worker.result.candidate_count, snapshot.round1_count)
                self.assertIsNotNone(worker.result.report_completed_at.utcoffset())
                self.assertEqual(worker.result.fetch_completed_at, snapshot.completed_at)
                self.assertEqual(worker.result.candidate_date, snapshot.candidate_date)
                record = store.read(attempt.operation_id)
                self.assertEqual(record.markdown, worker.result.markdown)
                self.assertNotIn("fake-desktop-key", record.markdown)
                identities.append(attempt.operation_id)
        # 每次真实分析会重置工作库，独立历史仍保留所有日报。
        self.assertEqual({row.record_id for row in store.list_records()}, set(identities))

    def test_history_dates_stay_bound_to_snapshot_across_midnight(self):
        from desktop.history import HistoryStore, timestamp_us
        captured = self.snapshot()
        fetched = datetime(2026, 10, 4, 15, 59, 58, tzinfo=timezone.utc)
        completed = datetime(2026, 10, 4, 16, 11, 20, tzinfo=timezone.utc)
        snapshot = pipeline.CandidateSnapshot(date(2026, 10, 1), fetched, captured.raw_count,
                                              captured.unique_count, captured.papers)
        with patch("desktop.analysis.datetime") as clock:
            clock.now.return_value = completed
            result = self.run_snapshot(snapshot)
        self.assertEqual(result.candidate_date, date(2026, 10, 1))
        self.assertEqual(result.fetch_completed_at, fetched)
        self.assertEqual(result.report_completed_at, completed)
        store = HistoryStore(self.root)
        store.save(result)
        record = store.read(result.operation_id)
        self.assertEqual(record.summary.fetch_time_text, "2026-10-04 23:59:58")
        self.assertEqual(record.summary.time_text, "2026-10-05 00:11:20")
        self.assertEqual(record.summary.completed_at_us, timestamp_us(completed))
        self.assertEqual(record.markdown, result.markdown)

    def test_history_isolated_after_real_analysis_success_and_failed_reports_not_saved(self):
        import importlib.util
        if importlib.util.find_spec("qfluentwidgets") is None:
            self.skipTest("Desktop GUI dependencies are absent")
        from desktop.app import AnalysisWorker
        from desktop.history import HistoryStore
        store = HistoryStore(self.root)
        for scenario in ("round1", "round2", "report"):
            self.fail_stage = scenario if scenario != "report" else None
            boundary = patch("desktop.report.build_desktop_report", side_effect=RuntimeError("synthetic failed report")) if scenario == "report" else nullcontext()
            with self.subTest(scenario=scenario), boundary, patch.object(store, "save") as save:
                worker = AnalysisWorker(analysis.AnalysisAttempt(self.snapshot()), "fake-desktop-key", history_store=store)
                worker.run()
                self.assertIsNone(worker.result)
                self.assertIsNotNone(worker.issue)
                save.assert_not_called()
        self.fail_stage = None
        with patch.object(store, "save", side_effect=OSError("synthetic history failure")) as save:
            worker = AnalysisWorker(analysis.AnalysisAttempt(self.snapshot()), "fake-desktop-key", history_store=store)
            worker.run()
            save.assert_called_once_with(worker.result)
        self.assertIsNone(worker.issue)
        self.assertIsNotNone(worker.history_issue)
        self.assertEqual(worker._api_key, "")
        self.assertIn("Round 2 最终推荐", worker.result.markdown)
        self.assertEqual(self.connect().execute("SELECT status FROM runs WHERE run_id=?", (worker.result.run_id,)).fetchone()[0], "success")

    def test_structured_progress_uses_real_pdf_and_fulltext_work_counts(self):
        events = []
        self.fail_pdf = {"2609.00002v1"}
        result = self.run_snapshot(progress=events.append)
        self.assertEqual(result.recommendation_count, 2)
        pdf_running = [
            event.processed for event in events
            if event.stage == "pdf" and event.state == "running"
        ]
        fulltext_running = [
            event.processed for event in events
            if event.stage == "fulltext" and event.state == "running"
        ]
        self.assertEqual(pdf_running, [0, 1, 2, 3])
        self.assertEqual(fulltext_running, [0, 1, 2])
        self.assertTrue(all(
            "成功下载" not in event.message
            for event in events if event.stage == "pdf"
        ))
        self.assertEqual(events[-1].stage, "complete")
        self.assertEqual(events[-1].result_count, 2)

    def test_zero_round1_emits_skipped_downstream_stages(self):
        self.selected_indices = []
        events = []
        self.run_snapshot(progress=events.append)
        states = {(event.stage, event.state) for event in events}
        self.assertIn(("round1", "completed"), states)
        for stage in ("pdf", "fulltext", "round2"):
            self.assertIn((stage, "skipped"), states)
        self.assertIn(("report", "completed"), states)

    def test_failure_event_stays_at_real_stage_and_is_safe(self):
        self.fail_stage = "round2"
        events = []
        with self.assertRaises(analysis.AnalysisError):
            self.run_snapshot(progress=events.append)
        self.assertEqual(events[-1].stage, "round2")
        self.assertEqual(events[-1].state, "failed")
        self.assertNotIn("private", events[-1].message)
        self.assertNotIn(("report", "completed"), {
            (event.stage, event.state) for event in events
        })

    def test_pdf_fulltext_and_sixty_page_gate_exclude_without_replacement(self):
        self.selected_indices = [1, 2, 3, 4]
        self.page_counts = {"2609.00004v1": 60, "2609.00003v1": 61}
        self.fail_pdf = {"2609.00002v1"}
        self.fail_extract = {"2609.00001v1"}
        result = self.run_snapshot()
        self.assertEqual(len(self.pdf_urls), 4)
        self.assertIn("Round 2 输入数量：1", result.markdown)
        self.assertIn("超过 60 页", result.markdown)
        self.assertIn("PDF 实际页数：60", result.markdown)

    def test_no_eligible_fulltext_skips_round2(self):
        self.page_counts = {f"2609.{n:05d}v1": 61 for n in range(1, 5)}
        result = self.run_snapshot()
        self.assertEqual(len(self.requests), 1)
        self.assertIn("Round 2 输入数量：0", result.markdown)

    def test_failures_consume_snapshot_and_never_retry(self):
        for stage in ("round1", "round2"):
            for invalid in (False, True):
                with self.subTest(stage=stage, invalid=invalid):
                    self.requests.clear()
                    self.fail_stage = None if invalid else stage
                    self.invalid_stage = stage if invalid else None
                    snapshot = self.snapshot()
                    attempt = analysis.AnalysisAttempt(snapshot)
                    with self.assertRaises(analysis.AnalysisError) as caught:
                        attempt.run("fake-desktop-key")
                    self.assertNotIn("private", str(caught.exception))
                    self.assertEqual(sum(s == stage for s, _ in self.requests), 1)
                    with self.assertRaises(analysis.AnalysisError):
                        analysis.AnalysisAttempt(snapshot)
                    with self.assertRaises(analysis.AnalysisError):
                        attempt.run("fake-desktop-key")

    def test_missing_usage_stops_before_round2(self):
        self.missing_usage = True
        with self.assertRaises(analysis.AnalysisError):
            self.run_snapshot()
        self.assertEqual(len(self.requests), 1)

    def test_cost_and_token_preflight_stop_before_http(self):
        config = main.load_config(self.root / "config.json")
        for change in ("tokens", "cost"):
            modified = copy.deepcopy(config)
            if change == "tokens":
                modified["limits"]["max_round1_request_tokens"] = 1
            else:
                modified["deepseek"]["round1_pricing"]["rate_schedule"]["peak_rates"]["output_price_per_million"] = "100"
            with patch("main.load_config", return_value=modified):
                with self.assertRaises(analysis.AnalysisError) as caught:
                    self.run_snapshot()
            self.assertEqual(caught.exception.issue.code, "AKD-R1-INPUT_LIMIT" if change == "tokens" else "AKD-R1-COST_LIMIT")
            self.assertFalse(caught.exception.issue.details["model_request_sent"])
            self.assertEqual(self.requests, [])

    def test_all_38_127_240_candidates_enter_one_round1_and_late_index_is_valid(self):
        hashes = []
        for count in (38, 127, 240):
            self.requests.clear()
            self.pdf_urls.clear()
            self.selected_indices = [count]
            result = self.run_snapshot(self.snapshot(count))
            self.assertEqual([stage for stage, _ in self.requests], ["round1", "round2"])
            message = self.requests[0][1]["messages"]
            candidates = json.loads(message[1]["content"])["candidate_papers"]
            self.assertEqual(len(candidates), count)
            self.assertEqual(candidates[-1]["candidate_index"], count)
            connection = self.connect()
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM screening_completion").fetchone()[0], count)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM screening_results WHERE task_type = 'round1_abstract_screening'").fetchone()[0], 1)
            self.assertIn(f"Round 1 输入数量：{count}", result.markdown)
            hashes.append(main.stable_json_hash(message))
            connection.close()
        self.assertEqual(len(set(hashes)), 3)

    def test_budget_exact_input_context_and_price_boundaries(self):
        config = main.load_config(self.root / "config.json")
        with patch("round2_fulltext_state.conservative_request_token_estimate", return_value=800000), patch("desktop.analysis.checked_usage", return_value=None):
            analysis.validate_budget(None, 1, config, "round1", [])
        for stage, code, tokens, modify in (
            ("round1", "INPUT_LIMIT", 800001, {}),
            ("round1", "CONTEXT_LIMIT", 800000, {"model_context_tokens": 999999}),
            ("round2", "CONTEXT_LIMIT", 1000, {"model_context_tokens": 200000}),
        ):
            changed = copy.deepcopy(config)
            changed["limits"].update(modify)
            with self.subTest(stage=stage, code=code), patch("round2_fulltext_state.conservative_request_token_estimate", return_value=tokens), self.assertRaises(analysis.DesktopOperationError) as caught:
                analysis.validate_budget(None, 1, changed, stage, [])
            self.assertEqual(caught.exception.issue.code, f"AKD-{'R1' if stage == 'round1' else 'R2'}-{code}")
            self.assertFalse(caught.exception.issue.details["model_request_sent"])
        with patch("model_usage.price_snapshot_from_config", return_value=None), self.assertRaises(analysis.DesktopOperationError) as caught:
            analysis.validate_budget(None, 1, config, "round1", [])
        self.assertEqual(caught.exception.issue.code, "AKD-R1-PRICE_UNAVAILABLE")
        self.assertFalse(self.requests)

    def test_round2_budget_includes_actual_round1_cost(self):
        self.extra_output_tokens = 500000
        original_price = model_usage.price_snapshot_from_config
        # 固定谷时实际费用为约 2 元；下一次峰值上界约 1 元，累计必须拒绝。
        def price(config, **kwargs):
            return original_price(config, at=datetime(2026, 9, 25, 0, tzinfo=timezone.utc), **kwargs)
        with patch("model_usage.price_snapshot_from_config", side_effect=price):
            with self.assertRaises(analysis.AnalysisError) as caught:
                self.run_snapshot()
        self.assertEqual(caught.exception.issue.code, "AKD-R2-COST_LIMIT")
        self.assertIn("Round 1", caught.exception.issue.impact)
        connection = self.connect()
        usage = model_usage.load_run_usage_summary(connection, 1)
        self.assertEqual(usage.api_attempt_count, 1)
        self.assertGreater(usage.known_cost, 0)
        self.assertEqual(len(self.requests), 1)

    def test_round2_context_stop_keeps_round1_results_and_usage(self):
        original = analysis.validate_budget
        def validate(connection, run_id, config, stage, messages):
            changed = copy.deepcopy(config)
            if stage == "round2":
                changed["limits"]["model_context_tokens"] = 1
            return original(connection, run_id, changed, stage, messages)
        with patch.object(analysis, "validate_budget", side_effect=validate), self.assertRaises(analysis.AnalysisError) as caught:
            self.run_snapshot()
        self.assertEqual(caught.exception.issue.code, "AKD-R2-CONTEXT_LIMIT")
        self.assertFalse(caught.exception.issue.details["model_request_sent"])
        self.assertEqual([stage for stage, _ in self.requests], ["round1"])
        connection = self.connect()
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM screening_results WHERE task_type = 'round1_abstract_screening'").fetchone()[0], 3)
        self.assertEqual(model_usage.load_run_usage_summary(connection, 1).api_attempt_count, 1)

    def test_token_gate_excludes_all_without_round2_http(self):
        config = main.load_config(self.root / "config.json")
        config["limits"]["max_round2_request_tokens"] = 1
        with patch("main.load_config", return_value=config):
            result = self.run_snapshot()
        self.assertEqual(len(self.requests), 1)
        self.assertIn("Token 预算超限", result.markdown)

    def test_report_failure_preserves_audit_and_consumes_snapshot(self):
        snapshot = self.snapshot()
        with patch("desktop.report.build_desktop_report", side_effect=RuntimeError("private-fulltext-marker")):
            with self.assertRaisesRegex(analysis.AnalysisError, "日报生成失败"):
                self.run_snapshot(snapshot)
        self.assertEqual(len(self.requests), 2)
        with self.assertRaises(analysis.AnalysisError):
            self.run_snapshot(snapshot)
        self.assertEqual(self.connect().execute("SELECT status FROM runs").fetchone()[0], "failed")

    def test_work_lock_blocks_second_attempt_before_reset_or_network(self):
        with analysis.lock_work_directory():
            marker = pipeline.runtime_path("work", "arxiv_kaleid.sqlite")
            marker.write_bytes(b"must-preserve")
            with self.assertRaises(analysis.AnalysisError):
                self.run_snapshot()
            self.assertEqual(marker.read_bytes(), b"must-preserve")
        self.assertEqual(self.requests, [])

    def test_reset_only_known_work_files_and_preserves_pdfs_and_secret(self):
        for parts in (("config", "secret.dat"), ("cache", "keep"), ("pdfs", "keep.pdf"), ("work", "other.sqlite")):
            path = pipeline.runtime_path(*parts)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"synthetic-preserve")
        self.selected_indices = []
        self.run_snapshot()
        self.run_snapshot()  # 新快照不做跨运行历史去重。
        self.assertEqual(len(self.requests), 2)
        for parts in (("config", "secret.dat"), ("cache", "keep"), ("pdfs", "keep.pdf"), ("work", "other.sqlite")):
            self.assertEqual(pipeline.runtime_path(*parts).read_bytes(), b"synthetic-preserve")

    def test_new_exception_inside_paper_boundary_remains_paper_scope(self):
        self.selected_indices = [1, 2]
        calls = []

        def isolated_failure(_root, selected, _destination, **_kwargs):
            calls.append(selected.arxiv_id)
            raise LookupError("novel private paper failure")

        with patch("pdf_processing.download_or_reuse_pdf", side_effect=isolated_failure):
            result = self.run_snapshot()
        self.assertEqual(len(calls), 2)
        self.assertEqual(len(self.requests), 1)
        self.assertTrue(result.paper_issues)
        self.assertTrue(all(issue.scope == "paper" for issue in result.paper_issues))
        self.assertIn("AKO-PDF-NO_SUCCESS", {item.code for item in result.outcomes})
        self.assertIn("AKO-FULLTEXT-NO_ELIGIBLE", {item.code for item in result.outcomes})

    def test_same_new_exception_at_shared_boundary_fails_closed(self):
        with patch(
            "round2_fulltext_state.process_downloaded_pdfs",
            side_effect=LookupError("novel private shared failure"),
        ):
            with self.assertRaises(analysis.AnalysisError) as caught:
                self.run_snapshot()
        self.assertEqual(caught.exception.issue.code, "AKD-FULLTEXT-UNEXPECTED")
        self.assertEqual(caught.exception.issue.scope, "system")
        self.assertNotIn("private", str(caught.exception))

    def test_paper_failure_persistence_error_upgrades_to_system(self):
        with patch(
            "pdf_processing.save_pdf_download_result",
            side_effect=sqlite3.OperationalError("private sqlite failure"),
        ):
            with self.assertRaises(analysis.AnalysisError) as caught:
                self.run_snapshot()
        self.assertEqual(caught.exception.issue.code, "AKD-PDF-STORAGE_FAILED")
        self.assertEqual(caught.exception.issue.scope, "system")

    def test_fulltext_persistence_error_cannot_become_zero_outcome(self):
        with patch(
            "round2_fulltext_state.save_fulltext_result",
            side_effect=sqlite3.OperationalError("private sqlite failure"),
        ):
            with self.assertRaises(analysis.AnalysisError) as caught:
                self.run_snapshot()
        self.assertEqual(caught.exception.issue.code, "AKD-FULLTEXT-COMPONENT_FAILED")
        self.assertEqual(caught.exception.issue.scope, "system")

    def test_key_must_be_explicitly_injected_by_desktop(self):
        config = main.load_config(self.root / "config.json")
        with self.assertRaises(RuntimeError):
            run_round2.create_deepseek_client(project_root=self.root, config=config, client_factory=None, api_key_override="")
        with self.assertRaises(RuntimeError):
            run_round2.create_deepseek_client(project_root=self.root, config=config, client_factory=None)
        result = run_round2.create_deepseek_client(
            project_root=self.root, config=config, client_factory=lambda **kw: kw,
            api_key_override="synthetic-session-key",
        )
        self.assertEqual(result["api_key"], "synthetic-session-key")
