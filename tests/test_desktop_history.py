# Copyright (c) 2026 yyyang20
# SPDX-License-Identifier: GPL-3.0-only
# See LICENSE in the project root for the full license text.

from __future__ import annotations

from dataclasses import replace
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from desktop.analysis import AnalysisResult
from desktop.history import HistoryError, HistoryStore, PAGE_SIZE, timestamp_us

ROOT = Path(__file__).resolve().parents[1]
TIME = datetime(2026, 10, 5, 14, 11, 12, 123456, tzinfo=timezone.utc)
FETCH_TIME = datetime(2026, 10, 4, 21, 11, 20, 654321, tzinfo=timezone.utc)
PAPER_DATE = date(2026, 10, 1)
BODY = "# 当次实际日报\n\n## 原有顺序\n\n**中文** 与 `代码`\n\n- [论文](https://arxiv.org/abs/2609.00001v1)\n\n末尾空行\n\n"


class DesktopHistoryTests(unittest.TestCase):
    def setUp(self):
        scratch = ROOT / ".codex-validation"
        scratch.mkdir(exist_ok=True)
        directory = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.store = HistoryStore(self.root)

    def result(self, identity="one", time=TIME, count=3):
        return AnalysisResult(1, BODY, count, operation_id=identity,
                              candidate_count=41, report_completed_at=time,
                              fetch_completed_at=FETCH_TIME, candidate_date=PAPER_DATE)

    def test_exact_body_metadata_and_new_process_restart(self):
        self.store.save(self.result())
        record = self.store.read("one")
        self.assertEqual(record.markdown, BODY)
        self.assertEqual(record.summary.time_text, "2026-10-05 22:11:12")
        self.assertEqual(record.summary.completed_at_us, timestamp_us(TIME))
        self.assertEqual(record.summary.fetch_time_text, "2026-10-05 05:11:20")
        self.assertEqual(record.summary.fetch_completed_at_us, timestamp_us(FETCH_TIME))
        self.assertEqual(record.summary.candidate_date, PAPER_DATE)
        self.assertEqual((record.summary.candidate_count, record.summary.recommendation_count), (41, 3))
        # 新进程真实读库，不依赖当前对象、Qt、Key、工作库或生成器。
        script = "from pathlib import Path; import sys; from desktop.history import HistoryStore; r=HistoryStore(Path(sys.argv[1])).read('one'); print(r.summary.completed_at_us); print(r.summary.fetch_completed_at_us); print(r.summary.candidate_date.isoformat()); print(r.markdown, end='')"
        completed = subprocess.run([sys.executable, "-B", "-X", "utf8", "-c", script, str(self.root)],
                                   cwd=ROOT, capture_output=True, encoding="utf-8", check=True)
        self.assertEqual(completed.stdout, f"{timestamp_us(TIME)}\n{timestamp_us(FETCH_TIME)}\n{PAPER_DATE.isoformat()}\n" + BODY)

    def test_sort_uses_report_time_instead_of_fetch_time_or_paper_date(self):
        self.store.save(replace(self.result("older"), fetch_completed_at=TIME, candidate_date=date(2026, 10, 5)))
        self.store.save(replace(self.result("newer", TIME + timedelta(days=1)),
                                fetch_completed_at=FETCH_TIME - timedelta(days=1), candidate_date=date(2026, 9, 29)))
        rows = self.store.list_records()
        self.assertEqual([row.record_id for row in rows], ["newer", "older"])
        self.assertEqual(rows[0].fetch_time_text, "2026-10-04 05:11:20")

    def test_empty_read_and_delete_do_not_create_database(self):
        self.assertEqual(self.store.list_records(), ())
        self.assertIsNone(self.store.read("missing"))
        self.assertFalse(self.store.delete("missing"))
        self.assertFalse(self.store.database.exists())

    def test_same_day_duplicates_ties_and_cursor_pagination(self):
        for index in range(123):
            self.store.save(self.result(str(index), TIME + timedelta(microseconds=index // 3)))
        rows = []
        cursor = None
        while batch := self.store.list_records(cursor):
            self.assertLessEqual(len(batch), PAGE_SIZE)
            rows.extend(batch)
            cursor = batch[-1].cursor
        self.assertEqual([row.record_id for row in rows], [str(i) for i in reversed(range(123))])
        self.assertTrue(all(row.time_text == "2026-10-05 22:11:12" for row in rows))

    def test_zero_valid_duplicate_id_never_overwrites(self):
        self.store.save(self.result(count=0))
        with self.assertRaises(HistoryError):
            self.store.save(replace(self.result(), markdown="# other"))
        self.assertEqual(self.store.read("one").markdown, BODY)
        self.assertEqual(self.store.read("one").summary.recommendation_count, 0)

    def test_delete_only_target_and_other_runtime_files_unchanged(self):
        self.store.save(self.result())
        self.store.save(self.result("two"))
        markers = []
        for relative in ("config/synthetic-marker", "work/marker.sqlite", "pdfs/marker.pdf", "logs/marker.jsonl"):
            path = self.root / ".desktop-runtime" / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"synthetic untouched")
            markers.append(path)
        self.assertTrue(self.store.delete("one"))
        self.assertFalse(self.store.delete("one"))
        self.assertIsNone(HistoryStore(self.root).read("one"))
        self.assertEqual(HistoryStore(self.root).read("two").markdown, BODY)
        self.assertTrue(all(path.read_bytes() == b"synthetic untouched" for path in markers))

    def test_invalid_results_rollback(self):
        self.store.save(self.result())
        for result in (replace(self.result("two"), markdown=""),
                       replace(self.result("two"), candidate_count=-1),
                       replace(self.result("two"), report_completed_at=None),
                       replace(self.result("two"), report_completed_at=TIME.replace(tzinfo=None)),
                       replace(self.result("two"), fetch_completed_at=None),
                       replace(self.result("two"), fetch_completed_at=FETCH_TIME.replace(tzinfo=None)),
                       replace(self.result("two"), candidate_date=None),
                       replace(self.result("two"), candidate_date="2026-10-01"),
                       replace(self.result("two"), candidate_date=FETCH_TIME)):
            with self.assertRaises(HistoryError):
                self.store.save(result)
        self.assertEqual(len(self.store.list_records()), 1)

    def test_invalid_stored_fetch_metadata_is_rejected(self):
        self.store.save(self.result())
        for field, value in (("candidate_date", "2026-02-30"), ("candidate_date", "20261001"),
                             ("fetch_completed_at_us", "invalid")):
            with self.subTest(field=field, value=value):
                with closing(sqlite3.connect(self.store.database)) as connection, connection:
                    connection.execute(f"UPDATE history SET {field}=?", (value,))
                for operation in (self.store.list_records, lambda: self.store.read("one")):
                    with self.assertRaises(HistoryError):
                        operation()
                with closing(sqlite3.connect(self.store.database)) as connection, connection:
                    connection.execute("UPDATE history SET candidate_date=?, fetch_completed_at_us=?",
                                       (PAPER_DATE.isoformat(), timestamp_us(FETCH_TIME)))

    def test_previous_technical_schema_is_preserved_without_migration(self):
        self.store.database.parent.mkdir(parents=True)
        with closing(sqlite3.connect(self.store.database)) as connection, connection:
            connection.execute("CREATE TABLE history (record_id TEXT, markdown TEXT)")
            connection.execute("INSERT INTO history VALUES ('old', 'preserve old synthetic report')")
            connection.execute("PRAGMA user_version=1")
        before = self.store.database.read_bytes()
        for operation in (self.store.list_records, lambda: self.store.read("old"),
                          lambda: self.store.delete("old"), lambda: self.store.save(self.result())):
            with self.assertRaises(HistoryError) as caught:
                operation()
            self.assertEqual(caught.exception.issue.code, "AKD-HISTORY-INCOMPATIBLE")
            self.assertEqual(self.store.database.read_bytes(), before)

    def test_commit_failure_rolls_back_save_and_delete(self):
        self.store.save(self.result())
        original = sqlite3.connect

        class FailCommit(sqlite3.Connection):
            def commit(self):
                raise OSError("private content must not escape")

        with patch("desktop.history.sqlite3.connect", side_effect=lambda *a, **kw: original(*a, **kw, factory=FailCommit)):
            for operation in (lambda: self.store.save(self.result("two")), lambda: self.store.delete("one")):
                with self.assertRaises(HistoryError) as caught:
                    operation()
                self.assertNotIn("private", str(caught.exception))
        self.assertIsNone(self.store.read("two"))
        self.assertIsNotNone(self.store.read("one"))

    def test_locked_database_fails_without_mutation_and_uses_five_second_timeout(self):
        self.store.save(self.result())
        original = sqlite3.connect
        with closing(original(self.store.database)) as lock:
            lock.execute("BEGIN IMMEDIATE")
            calls = []

            def short_timeout(*args, **kwargs):
                calls.append(kwargs["timeout"])
                kwargs["timeout"] = 0.01  # 真锁冲突；只缩短测试等待。
                return original(*args, **kwargs)

            with patch("desktop.history.sqlite3.connect", side_effect=short_timeout):
                for operation in (lambda: self.store.save(self.result("two")), lambda: self.store.delete("one")):
                    with self.assertRaises(HistoryError):
                        operation()
            lock.rollback()
        self.assertEqual(calls, [5, 5])
        self.assertEqual(len(self.store.list_records()), 1)

    def test_unknown_schema_and_corruption_preserved(self):
        self.store.database.parent.mkdir(parents=True)
        for unknown in (True, False):
            if unknown:
                with closing(sqlite3.connect(self.store.database)) as connection, connection:
                    connection.execute("PRAGMA user_version=99")
            else:
                self.store.database.write_bytes(b"synthetic corrupt SQLite")
            before = self.store.database.read_bytes()
            for operation in (self.store.list_records, lambda: self.store.read("one"),
                              lambda: self.store.delete("one"), lambda: self.store.save(self.result())):
                with self.assertRaises(HistoryError):
                    operation()
                self.assertEqual(self.store.database.read_bytes(), before)

    def test_schema_zero_with_existing_table_is_not_reinitialized(self):
        self.store.database.parent.mkdir(parents=True)
        with closing(sqlite3.connect(self.store.database)) as connection, connection:
            connection.execute("CREATE TABLE unrelated(value TEXT)")
            connection.execute("INSERT INTO unrelated VALUES ('preserve')")
        before = self.store.database.read_bytes()
        with self.assertRaises(HistoryError):
            self.store.save(self.result())
        self.assertEqual(self.store.database.read_bytes(), before)

    def test_path_boundary_failure_is_safe(self):
        with patch("desktop.history.paths.runtime_path", side_effect=ValueError("private path escaped")):
            for operation in (self.store.list_records, lambda: self.store.save(self.result())):
                with self.assertRaises(HistoryError) as caught:
                    operation()
                self.assertNotIn("private", str(caught.exception))
        self.assertFalse(self.store.database.exists())
