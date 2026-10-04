# Copyright (c) 2026 yyyang20
# SPDX-License-Identifier: GPL-3.0-only
# See LICENSE in the project root for the full license text.

"""独立日报快照存储；不依赖 Qt、工作库、凭据或报告生成器。"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
from zoneinfo import ZoneInfo

from desktop import paths, pipeline
from desktop.errors import DesktopOperationError, make_issue


SCHEMA_VERSION = 1
PAGE_SIZE = 50
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


class HistoryError(DesktopOperationError):
    """只向外提供固定错误，绝不携带 SQLite、路径或正文内容。"""


def timestamp_us(value: datetime) -> int:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("history_time_requires_timezone")
    delta = value.astimezone(timezone.utc) - EPOCH
    return (delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds


@dataclass(frozen=True)
class HistorySummary:
    sequence: int
    record_id: str
    completed_at_us: int
    candidate_count: int
    recommendation_count: int

    @property
    def time_text(self) -> str:
        value = EPOCH + timedelta(microseconds=self.completed_at_us)
        return value.astimezone(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M:%S")

    @property
    def cursor(self) -> tuple[int, int]:
        return self.completed_at_us, self.sequence


@dataclass(frozen=True)
class HistoryRecord:
    summary: HistorySummary
    markdown: str


class HistoryStore:
    def __init__(self, source_root: Path | None = None):
        self.source_root = source_root if source_root is not None else pipeline.PROJECT_ROOT

    @property
    def database(self) -> Path:
        return paths.runtime_path(self.source_root, "history", "history.sqlite")

    @contextmanager
    def _connection(self, *, create: bool, operation: str):
        connection = None
        try:
            path = self.database
            if not path.exists() and not create:
                yield None
                return
            if create:
                path.parent.mkdir(parents=True, exist_ok=True)
            # URI 的 rw/ro 禁止读取操作意外创建空库；每次操作独立连接。
            mode = "rwc" if create else ("rw" if operation == "DELETE" else "ro")
            connection = sqlite3.connect(path.as_uri() + "?mode=" + mode, uri=True, timeout=5)
            connection.row_factory = sqlite3.Row
            if create:
                connection.execute("BEGIN IMMEDIATE")
                version = connection.execute("PRAGMA user_version").fetchone()[0]
                if version == 0:
                    if connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchone():
                        raise HistoryError(make_issue("AKD-HISTORY-INCOMPATIBLE"))
                    connection.execute("""CREATE TABLE history (
                        sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                        record_id TEXT NOT NULL UNIQUE,
                        completed_at_us INTEGER NOT NULL,
                        candidate_count INTEGER NOT NULL CHECK(candidate_count >= 0),
                        recommendation_count INTEGER NOT NULL CHECK(recommendation_count >= 0),
                        markdown TEXT NOT NULL CHECK(length(markdown) > 0)
                    )""")
                    connection.execute("CREATE INDEX history_completed ON history(completed_at_us DESC, sequence DESC)")
                    connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            if connection.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
                raise HistoryError(make_issue("AKD-HISTORY-INCOMPATIBLE"))
            columns = {row[1] for row in connection.execute("PRAGMA table_info(history)")}
            if columns != {"sequence", "record_id", "completed_at_us", "candidate_count", "recommendation_count", "markdown"}:
                raise HistoryError(make_issue("AKD-HISTORY-INCOMPATIBLE"))
            yield connection
            if create or operation == "DELETE":
                connection.commit()
        except HistoryError:
            raise
        except Exception:
            raise HistoryError(make_issue(f"AKD-HISTORY-{operation}_FAILED")) from None
        finally:
            if connection is not None:
                connection.close()

    def save(self, result) -> None:
        """元数据和正文同一事务；只接受已完成的不可变分析结果。"""
        with self._connection(create=True, operation="SAVE") as connection:
            if (not isinstance(result.operation_id, str) or not result.operation_id.strip()
                    or type(result.candidate_count) is not int or result.candidate_count < 0
                    or type(result.recommendation_count) is not int or result.recommendation_count < 0
                    or not isinstance(result.markdown, str) or not result.markdown.strip()):
                raise ValueError("history_result_invalid")
            completed = timestamp_us(result.report_completed_at)
            connection.execute("""INSERT INTO history
                (record_id, completed_at_us, candidate_count, recommendation_count, markdown)
                VALUES (?, ?, ?, ?, ?)""", (
                result.operation_id, completed, result.candidate_count,
                result.recommendation_count, result.markdown,
            ))

    @staticmethod
    def _summary(row) -> HistorySummary:
        values = tuple(row[k] for k in ("sequence", "record_id", "completed_at_us", "candidate_count", "recommendation_count"))
        if (type(values[0]) is not int or values[0] <= 0 or not isinstance(values[1], str) or not values[1]
                or any(type(values[i]) is not int for i in (2, 3, 4)) or min(values[3:]) < 0):
            raise ValueError("history_row_invalid")
        summary = HistorySummary(*values)
        summary.time_text  # 提前验证时间范围；不让非法数据进入 GUI。
        return summary

    def list_records(self, cursor: tuple[int, int] | None = None) -> tuple[HistorySummary, ...]:
        with self._connection(create=False, operation="READ") as connection:
            if connection is None:
                return ()
            where = ""
            values = ()
            if cursor is not None:
                where = "WHERE (completed_at_us, sequence) < (?, ?)"
                values = cursor
            rows = connection.execute(f"""SELECT sequence, record_id, completed_at_us,
                candidate_count, recommendation_count FROM history {where}
                ORDER BY completed_at_us DESC, sequence DESC LIMIT ?""", (*values, PAGE_SIZE)).fetchall()
            return tuple(self._summary(row) for row in rows)

    def read(self, record_id: str) -> HistoryRecord | None:
        with self._connection(create=False, operation="READ") as connection:
            if connection is None:
                return None
            row = connection.execute("SELECT * FROM history WHERE record_id=?", (record_id,)).fetchone()
            if row is None:
                return None
            if not isinstance(row["markdown"], str) or not row["markdown"].strip():
                raise ValueError("history_markdown_invalid")
            return HistoryRecord(self._summary(row), row["markdown"])

    def delete(self, record_id: str) -> bool:
        with self._connection(create=False, operation="DELETE") as connection:
            if connection is None:
                return False
            return connection.execute("DELETE FROM history WHERE record_id=?", (record_id,)).rowcount == 1
