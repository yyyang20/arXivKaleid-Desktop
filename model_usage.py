# Copyright (c) 2026 yyyang20
# SPDX-License-Identifier: GPL-3.0-only
# See LICENSE in the project root for the full license text.

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Any


PROVIDER = "deepseek"


@dataclass(frozen=True)
class RunUsageSummary:
    call_count: int
    api_attempt_count: int
    local_cache_hit_count: int
    known_input_tokens: int
    known_output_tokens: int
    known_total_tokens: int
    token_complete: bool


def current_time_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


def record_run_stage_timing(
    connection: sqlite3.Connection,
    *,
    run_id: int,
    stage_name: str,
    started_at: str,
    finished_at: str,
    duration_seconds: int,
) -> None:
    """保存一次正式阶段耗时；同一 run 的同一阶段只保留首次成功记录。"""
    if run_id <= 0 or not stage_name.strip() or duration_seconds < 0:
        raise RuntimeError("invalid_run_stage_timing")
    _validated_timestamp(started_at, "stage_started_at")
    _validated_timestamp(finished_at, "stage_finished_at")
    with connection:
        connection.execute(
            """
            INSERT OR IGNORE INTO run_stage_timings (
                run_id, stage_name, started_at, finished_at,
                duration_seconds, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                stage_name.strip(),
                started_at,
                finished_at,
                duration_seconds,
                current_time_iso(),
            ),
        )


def load_run_stage_duration_seconds(
    connection: sqlite3.Connection, run_id: int, stage_name: str
) -> int | None:
    """只读加载阶段耗时；旧数据库没有该表时返回 None。"""
    table = connection.execute(
        "SELECT 1 FROM sqlite_master "
        "WHERE type='table' AND name='run_stage_timings'"
    ).fetchone()
    if table is None:
        return None
    row = connection.execute(
        """
        SELECT duration_seconds
        FROM run_stage_timings
        WHERE run_id = ? AND stage_name = ?
        """,
        (run_id, stage_name),
    ).fetchone()
    return int(row[0]) if row is not None else None


def initialize_model_usage_schema(
    connection: sqlite3.Connection, *, manage_transaction: bool = True
) -> None:
    """只增不删地创建模型调用与 HTTP attempt 审计表。"""
    def create_objects() -> None:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS model_calls (
                call_id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id INTEGER NOT NULL,
                provider TEXT NOT NULL,
                task_type TEXT NOT NULL,
                call_purpose TEXT NOT NULL,
                model_requested TEXT NOT NULL,
                prompt_version TEXT,
                research_profile_version TEXT,
                selection_policy_version TEXT,
                request_hash TEXT,
                local_cache_status TEXT NOT NULL,
                api_called INTEGER NOT NULL,
                call_status TEXT NOT NULL,
                error_type TEXT,
                attempt_count INTEGER NOT NULL,
                started_at TEXT NOT NULL,
                finished_at TEXT
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS model_call_attempts (
                attempt_id INTEGER PRIMARY KEY AUTOINCREMENT,
                call_id INTEGER NOT NULL,
                attempt_no INTEGER NOT NULL,
                attempt_status TEXT NOT NULL,
                http_status INTEGER,
                error_type TEXT,
                model_returned TEXT,
                response_id TEXT,
                finish_reason TEXT,
                input_tokens INTEGER,
                output_tokens INTEGER,
                total_tokens INTEGER,
                cache_hit_input_tokens INTEGER,
                cache_miss_input_tokens INTEGER,
                usage_source TEXT NOT NULL,
                usage_complete INTEGER NOT NULL,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                duration_ms INTEGER,
                UNIQUE(call_id, attempt_no)
            )
            """
        )
        attempt_columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(model_call_attempts)"
            ).fetchall()
        }
        if "finish_reason" not in attempt_columns:
            connection.execute(
                "ALTER TABLE model_call_attempts ADD COLUMN finish_reason TEXT"
            )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS ix_model_calls_run_task
            ON model_calls (run_id, task_type, call_purpose, started_at)
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS ix_model_attempts_call
            ON model_call_attempts (call_id, attempt_no)
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS run_stage_timings (
                timing_id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id INTEGER NOT NULL,
                stage_name TEXT NOT NULL,
                started_at TEXT NOT NULL,
                finished_at TEXT NOT NULL,
                duration_seconds INTEGER NOT NULL CHECK(duration_seconds >= 0),
                created_at TEXT NOT NULL,
                UNIQUE(run_id, stage_name)
            )
            """
        )

    if manage_transaction:
        with connection:
            create_objects()
    else:
        create_objects()


def _validated_timestamp(value: Any, field: str) -> str:
    text = str(value)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise RuntimeError(f"invalid_usage_timestamp:{field}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise RuntimeError(f"invalid_usage_timestamp:{field}")
    return text


def record_cache_reuse(
    connection: sqlite3.Connection,
    *,
    run_id: int,
    task_type: str,
    call_purpose: str,
    model_requested: str,
    prompt_version: str | None,
    research_profile_version: str | None,
    selection_policy_version: str | None,
    request_hash: str | None,
) -> int:
    return record_no_api_event(
        connection,
        run_id=run_id,
        task_type=task_type,
        call_purpose=call_purpose,
        model_requested=model_requested,
        prompt_version=prompt_version,
        research_profile_version=research_profile_version,
        selection_policy_version=selection_policy_version,
        request_hash=request_hash,
        local_cache_status="hit",
        call_status="cache_reused",
        error_type=None,
    )


def record_no_api_event(
    connection: sqlite3.Connection,
    *,
    run_id: int,
    task_type: str,
    call_purpose: str,
    model_requested: str,
    prompt_version: str | None,
    research_profile_version: str | None,
    selection_policy_version: str | None,
    request_hash: str | None,
    local_cache_status: str,
    call_status: str,
    error_type: str | None,
) -> int:
    now = current_time_iso()
    with connection:
        cursor = connection.execute(
            """
            INSERT INTO model_calls (
                run_id, provider, task_type, call_purpose, model_requested,
                prompt_version, research_profile_version,
                selection_policy_version, request_hash, local_cache_status,
                api_called, call_status, error_type, attempt_count,
                started_at, finished_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, 0, ?, ?)
            """,
            (
                run_id,
                PROVIDER,
                task_type,
                call_purpose,
                model_requested,
                prompt_version,
                research_profile_version,
                selection_policy_version,
                request_hash,
                local_cache_status,
                call_status,
                error_type,
                now,
                now,
            ),
        )
    return int(cursor.lastrowid)


def record_api_call(
    connection: sqlite3.Connection,
    *,
    run_id: int,
    task_type: str,
    call_purpose: str,
    model_requested: str,
    prompt_version: str | None,
    research_profile_version: str | None,
    selection_policy_version: str | None,
    request_hash: str | None,
    local_cache_status: str,
    call_result: Any,
) -> int:
    """保存一次逻辑调用及所有 attempt；mock 旧接口按 usage 未知兼容。"""
    attempts = list(getattr(call_result, "attempts", ()) or ())
    now = current_time_iso()
    if not attempts:
        attempts = [
            type(
                "CompatibleAttempt",
                (),
                {
                    "attempt_no": 1,
                    "status": "success" if call_result.ok else call_result.error_type,
                    "error_type": call_result.error_type,
                    "http_status": None,
                    "model_returned": None,
                    "response_id": None,
                    "finish_reason": None,
                    "usage": None,
                    "started_at": now,
                    "finished_at": now,
                    "duration_ms": 0,
                },
            )()
        ]
    call_status = "api_succeeded" if call_result.ok else "api_failed"
    with connection:
        cursor = connection.execute(
            """
            INSERT INTO model_calls (
                run_id, provider, task_type, call_purpose, model_requested,
                prompt_version, research_profile_version,
                selection_policy_version, request_hash, local_cache_status,
                api_called, call_status, error_type, attempt_count,
                started_at, finished_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                PROVIDER,
                task_type,
                call_purpose,
                model_requested,
                prompt_version,
                research_profile_version,
                selection_policy_version,
                request_hash,
                local_cache_status,
                call_status,
                getattr(call_result, "error_type", None),
                len(attempts),
                attempts[0].started_at,
                attempts[-1].finished_at,
            ),
        )
        call_id = int(cursor.lastrowid)
        for attempt in attempts:
            _insert_attempt(connection, call_id, attempt)
    return call_id


def _insert_attempt(
    connection: sqlite3.Connection,
    call_id: int,
    attempt: Any,
) -> None:
    usage = getattr(attempt, "usage", None)
    columns = [
        "call_id", "attempt_no", "attempt_status", "http_status", "error_type",
        "model_returned", "response_id", "finish_reason", "input_tokens", "output_tokens",
        "total_tokens", "cache_hit_input_tokens", "cache_miss_input_tokens",
        "usage_source", "usage_complete", "started_at", "finished_at", "duration_ms",
    ]
    values = [
        call_id, attempt.attempt_no, attempt.status, attempt.http_status,
        attempt.error_type, attempt.model_returned, attempt.response_id,
        getattr(attempt, "finish_reason", None),
        getattr(usage, "input_tokens", None), getattr(usage, "output_tokens", None),
        getattr(usage, "total_tokens", None), getattr(usage, "cache_hit_input_tokens", None),
        getattr(usage, "cache_miss_input_tokens", None),
        "api_response" if usage is not None else "unavailable",
        int(bool(getattr(usage, "complete", False))),
        attempt.started_at, attempt.finished_at, attempt.duration_ms,
    ]
    # 旧表有无默认值的必填兼容列，仅写空值；不读取或迁移旧列和旧数据。
    legacy_columns = {row[1] for row in connection.execute("PRAGMA table_info(model_call_attempts)")}
    if "cost_status" in legacy_columns:
        columns.append("cost_status")
        values.append("")
    connection.execute(
        f"INSERT INTO model_call_attempts ({', '.join(columns)}) "
        f"VALUES ({', '.join('?' for _ in values)})",
        values,
    )


def update_call_status(
    connection: sqlite3.Connection,
    call_id: int,
    status: str,
    error_type: str | None = None,
) -> None:
    with connection:
        connection.execute(
            """
            UPDATE model_calls
            SET call_status = ?, error_type = ?, finished_at = ?
            WHERE call_id = ?
            """,
            (status, error_type, current_time_iso(), call_id),
        )


def load_run_usage_summary(
    connection: sqlite3.Connection, run_id: int
) -> RunUsageSummary | None:
    table = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='model_calls'"
    ).fetchone()
    if table is None:
        return None
    calls = connection.execute(
        "SELECT api_called, local_cache_status FROM model_calls WHERE run_id = ?",
        (run_id,),
    ).fetchall()
    if not calls:
        return None
    attempts = connection.execute(
        """
        SELECT a.input_tokens, a.output_tokens, a.total_tokens,
               a.usage_complete
        FROM model_call_attempts AS a
        JOIN model_calls AS c ON c.call_id = a.call_id
        WHERE c.run_id = ?
        """,
        (run_id,),
    ).fetchall()
    known_input = sum(int(row[0]) for row in attempts if row[0] is not None)
    known_output = sum(int(row[1]) for row in attempts if row[1] is not None)
    known_total = sum(int(row[2]) for row in attempts if row[2] is not None)
    api_attempt_count = len(attempts)
    api_called_count = sum(int(row[0]) for row in calls)
    token_complete = (
        api_attempt_count > 0 and all(bool(row[3]) for row in attempts)
    ) or api_called_count == 0
    return RunUsageSummary(
        call_count=len(calls),
        api_attempt_count=api_attempt_count,
        local_cache_hit_count=sum(row[1] == "hit" for row in calls),
        known_input_tokens=known_input,
        known_output_tokens=known_output,
        known_total_tokens=known_total,
        token_complete=token_complete,
    )


def load_run_model_names(
    connection: sqlite3.Connection, run_id: int
) -> tuple[str, ...]:
    """从成功调用账本读取实际模型；不依赖是否有论文入选。"""
    table = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='model_calls'"
    ).fetchone()
    if table is None:
        return ()
    rows = connection.execute(
        """
        SELECT DISTINCT trim(model_requested)
        FROM model_calls
        WHERE run_id = ? AND call_status = 'completed'
          AND (api_called = 1 OR local_cache_status = 'hit')
          AND trim(model_requested) != ''
        ORDER BY trim(model_requested)
        """,
        (run_id,),
    ).fetchall()
    return tuple(str(row[0]) for row in rows)
