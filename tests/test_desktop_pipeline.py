# Copyright (c) 2026 yyyang20
# SPDX-License-Identifier: GPL-3.0-only
# See LICENSE in the project root for the full license text.

from __future__ import annotations

import ast
import tempfile
import unittest
from contextlib import ExitStack
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

import main
from desktop import pipeline
from desktop.diagnostics import DesktopDiagnostics


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DAY = date(2026, 9, 25)


def paper(number=1, version=1, day=DAY, updated=None):
    identity = f"2609.{number:05d}"
    return dict(
        id=f"https://arxiv.org/abs/{identity}v{version}", arxiv_id=identity,
        version=version, title="Synthetic paper", authors="Test Author",
        summary="Synthetic abstract", categories="gr-qc", primary_category="gr-qc",
        published=f"{day}T00:00:00Z", updated=updated or f"{day}T01:00:00Z",
        abs_url=f"https://arxiv.org/abs/{identity}v{version}",
        pdf_url=f"https://arxiv.org/pdf/{identity}v{version}",
    )


def feed(papers):
    entries = []
    for p in papers:
        entries.append(
            f"<entry><id>{p['id']}</id><title>{p['title']}</title>"
            f"<published>{p['published']}</published><updated>{p['updated']}</updated>"
            f"<summary>{p['summary']}</summary><author><name>{p['authors']}</name></author>"
            f"<category term='{p['categories']}'/><link title='pdf' href='{p['pdf_url']}'/>"
            "</entry>"
        )
    return ("<feed xmlns='http://www.w3.org/2005/Atom'>" + "".join(entries) + "</feed>").encode()


class IsolatedDesktopTest(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        scratch = PROJECT_ROOT / ".codex-validation"
        self.assertTrue(scratch.resolve().is_relative_to(PROJECT_ROOT))
        scratch.mkdir(exist_ok=True)
        directory = self.stack.enter_context(tempfile.TemporaryDirectory(dir=scratch))
        self.root = Path(directory)
        self.stack.enter_context(patch.object(pipeline, "PROJECT_ROOT", self.root))
        # 真网络、模型、PDF、数据库和旧 Secret 入口一旦触发即失败。
        self.forbidden = [self.stack.enter_context(patch(target, side_effect=AssertionError(target))) for target in (
            "main.DeepSeekClient", "pdf_processing.download_selected_papers",
            "main.sqlite3.connect",
            "main.subprocess.run",
        )]

    def assert_no_analysis(self):
        for operation in self.forbidden:
            operation.assert_not_called()


class DesktopPipelineTests(IsolatedDesktopTest):
    def install_feed(self, source):
        self.calls = []

        def fetch(**kwargs):
            self.assertEqual(kwargs["cache_dir"], self.root / ".desktop-runtime/cache/arxiv")
            self.assertEqual(kwargs["timeout_seconds"], 90)
            self.assertEqual(kwargs["interval_seconds"], 5)
            self.assertNotIn("max_attempts", kwargs)  # submittedDate 沿用单次请求默认值。
            query = parse_qs(urlparse(kwargs["url"]).query)
            self.assertEqual(query["sortBy"], ["submittedDate"])
            self.assertEqual(query["sortOrder"], ["descending"])
            search = query["search_query"][0]
            category = search.split("cat:")[1].split()[0]
            day = date.fromisoformat(search.split("submittedDate:[")[1][:8])
            start = int(query["start"][0])
            self.assertEqual(query["max_results"], [str(pipeline.PAGE_SIZE)])
            self.calls.append((category, day, start))
            return feed(source(category, day, start))

        self.stack.enter_context(patch("main.fetch_arxiv_metadata", side_effect=fetch))

    def test_fixed_categories_identity_counts_order_and_readonly_snapshot(self):
        first = paper(1)
        version2 = paper(1, version=2, updated=f"{DAY}T03:00:00Z")
        newer = paper(1, updated=f"{DAY}T02:00:00Z")
        off_date = paper(2, day=DAY - timedelta(days=1))
        self.install_feed(lambda category, *_: {
            "gr-qc": [first, version2, off_date], "astro-ph.HE": [newer], "astro-ph.GA": [],
        }[category])
        result = pipeline.fetch_latest_candidates(DAY)
        self.assertEqual(self.calls, [(c, DAY, 0) for c in pipeline.CATEGORIES])
        self.assertEqual(pipeline.CATEGORIES, ("gr-qc", "astro-ph.HE", "astro-ph.GA"))
        self.assertEqual((result.raw_count, result.unique_count, result.round1_count), (3, 2, 2))
        self.assertEqual([p["version"] for p in result.papers], [2, 1])
        self.assertEqual(result.papers[1]["updated"], newer["updated"])
        self.assertEqual(dict(result.papers[0]), version2)
        self.assertEqual(result.candidate_date, DAY)
        self.assertEqual(result.completed_at.tzinfo.key, "Asia/Shanghai")
        self.assertTrue(result.completed_time_text.endswith("北京时间"))
        with self.assertRaises(TypeError):
            result.papers[0]["title"] = "changed"
        self.assert_no_analysis()

    def test_stops_at_first_nonempty_day_without_filling_to_100(self):
        target = DAY - timedelta(days=2)
        self.install_feed(lambda c, day, _: [paper(day=day)] if day <= target and c == "gr-qc" else [])
        result = pipeline.fetch_latest_candidates(DAY)
        self.assertEqual(result.candidate_date, target)
        self.assertEqual(result.round1_count, 1)
        self.assertEqual([day for _, day, _ in self.calls], [DAY]*3 + [DAY-timedelta(days=1)]*3 + [target]*3)

    def test_offset_14_is_included_but_offset_15_is_not(self):
        for offset in (14, 15):
            with self.subTest(offset=offset):
                target = DAY - timedelta(days=offset)
                self.install_feed(lambda c, day, _: [paper(day=day)] if day == target else [])
                if offset == 14:
                    self.assertEqual(pipeline.fetch_latest_candidates(DAY).candidate_date, target)
                else:
                    result = pipeline.fetch_latest_candidates(DAY)
                    self.assertIsNone(result.snapshot)
                    self.assertEqual(result.outcome.code, "AKO-FETCH-NO_CANDIDATES")
                self.assertEqual(len(self.calls), 45)
                self.assertEqual(self.calls[-1][1], DAY-timedelta(days=14))

    def test_all_pages_and_categories_complete_before_top_100_freeze(self):
        self.stack.enter_context(patch.object(pipeline, "PAGE_SIZE", 100))
        papers = [paper(i, updated=f"{DAY}T00:{i//60:02d}:{i%60:02d}Z") for i in range(105)]
        self.install_feed(lambda c, _, start: papers[start:start+100] if c == "gr-qc" else [papers[0]])
        result = pipeline.fetch_latest_candidates(DAY)
        self.assertEqual((result.raw_count, result.unique_count, result.round1_count), (107, 105, 100))
        self.assertEqual([p["arxiv_id"] for p in result.papers], [p["arxiv_id"] for p in reversed(papers[5:])])
        self.assertEqual(self.calls, [("gr-qc", DAY, 0), ("gr-qc", DAY, 100), ("astro-ph.HE", DAY, 0), ("astro-ph.GA", DAY, 0)])
        # 同一批次再次抓取不会查询历史或过滤已看过的版本。
        self.assertEqual(pipeline.fetch_latest_candidates(DAY).round1_count, 100)
        self.assert_no_analysis()

    def test_failure_after_partial_page_or_category_returns_no_snapshot(self):
        self.stack.enter_context(patch.object(pipeline, "PAGE_SIZE", 2))
        for fail_page in (True, False):
            def source(category, day, start):
                if start or category == "astro-ph.HE":
                    raise RuntimeError("synthetic private response")
                return [paper(1), paper(2)] if fail_page else [paper(1)]
            self.install_feed(source)
            with self.assertRaises(pipeline.CandidateError) as caught:
                pipeline.fetch_latest_candidates(DAY)
            self.assertNotIn("private", str(caught.exception))
            self.assert_no_analysis()

    def test_structured_progress_uses_dynamic_categories_and_resets_date_count(self):
        events = []
        target = DAY - timedelta(days=1)
        categories = ("gr-qc", "astro-ph.HE")

        def source(category, day, _start):
            if day == DAY and category == "gr-qc":
                return [paper(9, day=target)]  # 非当前日期不能进入当前日期计数。
            if day == target:
                return [paper(1, day=target)]
            return []

        self.install_feed(source)
        with patch.object(pipeline, "CATEGORIES", categories):
            result = pipeline.fetch_latest_candidates(DAY, events.append)

        self.assertEqual(result.candidate_date, target)
        category_events = [event for event in events if event.stage == "category"]
        self.assertTrue(category_events)
        self.assertTrue(all(event.category_total == len(categories) for event in category_events))
        first_target = next(
            event for event in category_events
            if event.current_date == target and event.category_index == 1
        )
        self.assertEqual(first_target.processed, 0)
        self.assertNotIn("3", first_target.message.split("分类", 1)[1].split("：", 1)[0])
        self.assertEqual(events[-1].state, "completed")
        self.assertEqual(events[-1].result_count, 1)

    def test_fetch_failure_emits_safe_failed_event_without_partial_snapshot(self):
        events = []
        self.install_feed(lambda *_: (_ for _ in ()).throw(RuntimeError("private marker")))
        with self.assertRaises(pipeline.CandidateError):
            pipeline.fetch_latest_candidates(DAY, events.append)
        self.assertEqual(events[-1].state, "failed")
        self.assertNotIn("private", events[-1].message)

    def test_existing_structured_transport_diagnostic_reaches_jsonl_directly(self):
        diagnostics = DesktopDiagnostics(self.root)
        self.addCleanup(diagnostics.close)

        def fetch(**kwargs):
            kwargs["diagnostic_observer"](
                "transport",
                {
                    "http_status": 200,
                    "attempt": 1,
                    "max_attempts": 1,
                    "remote_ip": "151.101.3.42",
                    "timings_seconds": {"time_total": 0.25},
                    "response_headers": {"content-type": "application/atom+xml"},
                },
            )
            query = parse_qs(urlparse(kwargs["url"]).query)
            category = query["search_query"][0].split("cat:")[1].split()[0]
            return feed([paper()] if category == "gr-qc" else [])

        with patch("main.fetch_arxiv_metadata", side_effect=fetch):
            result = pipeline.fetch_latest_candidates(DAY, diagnostics=diagnostics)
        self.assertIsNotNone(result.snapshot)
        log = next(diagnostics.log_directory.glob("desktop-*.jsonl")).read_text(encoding="utf-8")
        self.assertIn('"remote_ip":"151.101.3.42"', log)
        self.assertIn('"time_total":0.25', log)
        self.assertNotIn("Authorization", log)

    def test_runtime_paths_cannot_escape(self):
        for parts in (("..", "outside"), (str(self.root.parent),)):
            with self.assertRaises(ValueError):
                pipeline.runtime_path(*parts)
        with patch.object(Path, "resolve", autospec=True, side_effect=lambda p: self.root.parent if p.name == ".desktop-runtime" else p):
            with self.assertRaises(ValueError):
                pipeline.runtime_path("config")

    def test_desktop_imports_only_shared_functions_and_no_automation(self):
        allowed = {"__future__", "ctypes", "datetime", "logging", "dataclasses", "pathlib", "types", "typing", "zoneinfo", "os", "tempfile", "sys", "PySide6", "desktop", "main", "threading", "_thread", "json", "sqlite3", "decimal", "html", "build_round2_inputs", "model_usage", "pdf_processing", "round2_fulltext_state", "run_round2", "generate_round2_report", "rebuild_daily_report", "msvcrt", "fcntl", "hashlib", "stat", "collections", "time", "traceback", "uuid", "re"}
        for source in (PROJECT_ROOT / "desktop").glob("*.py"):
            tree = ast.parse(source.read_text(encoding="utf-8"))
            imports = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imports.update(alias.name.split(".")[0] for alias in node.names)
                elif isinstance(node, ast.ImportFrom):
                    imports.add(node.module.split(".")[0])
            self.assertFalse(imports - allowed, source.name)


if __name__ == "__main__":
    unittest.main()
