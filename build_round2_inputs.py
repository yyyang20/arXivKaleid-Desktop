# Copyright (c) 2026 yyyang20
# SPDX-License-Identifier: GPL-3.0-only
# See LICENSE in the project root for the full license text.

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote


ROUND1_TASK_TYPE = "round1_abstract_screening"

REQUIRED_TABLE_COLUMNS = {
    "runs": {"run_id", "started_at", "status"},
    "papers": {
        "arxiv_id",
        "version",
        "title",
        "authors",
        "categories",
        "primary_category",
        "summary",
    },
    "screening_results": {
        "run_id",
        "task_type",
        "arxiv_id",
        "version",
        "result_rank",
        "score",
        "confidence",
        "is_selected",
        "reason",
        "details_json",
        "prompt_version",
        "research_profile_version",
    },
    "screening_completion": {
        "run_id",
        "task_type",
        "arxiv_id",
        "version",
        "completion_status",
        "selection_status",
        "prompt_version",
        "research_profile_version",
        "selection_policy_version",
    },
}


@dataclass(frozen=True)
class Round2RunSelection:
    run_id: int
    started_at: str
    selected_count: int


def validate_required_schema(connection: sqlite3.Connection) -> None:
    for table, required_columns in REQUIRED_TABLE_COLUMNS.items():
        rows = connection.execute(f"PRAGMA table_info({table})").fetchall()
        existing_columns = {
            row["name"] if isinstance(row, sqlite3.Row) else row[1]
            for row in rows
        }
        if not rows:
            raise RuntimeError(f"missing_table:{table}")
        missing = sorted(required_columns - existing_columns)
        if missing:
            raise RuntimeError(
                f"missing_columns:{table}:{','.join(missing)}"
            )


def get_round1_identity(config: dict[str, object]) -> tuple[str, str, str]:
    versions = config.get("versions")
    if not isinstance(versions, dict):
        raise RuntimeError("config_missing_versions")
    prompt_version = str(versions.get("round1_prompt_version") or "")
    profile_version = str(versions.get("research_profile_version") or "")
    policy_version = str(config.get("round1_selection_policy_version") or "")
    if not prompt_version or not profile_version or not policy_version:
        raise RuntimeError("config_missing_round1_identity")
    return prompt_version, profile_version, policy_version


def round1_selected_exists_sql() -> str:
    return """
    EXISTS (
        SELECT 1
        FROM screening_completion AS c
        WHERE c.task_type = s.task_type
          AND c.arxiv_id = s.arxiv_id
          AND c.version = s.version
          AND c.completion_status = 'completed'
          AND c.selection_status = 'selected'
          AND c.prompt_version = s.prompt_version
          AND c.research_profile_version = s.research_profile_version
          AND c.selection_policy_version = ?
    )
    """


def select_round1_run(connection: sqlite3.Connection, *, prompt_version: str,
                      profile_version: str, policy_version: str,
                      run_id: int) -> Round2RunSelection:
    """只读取本次 Desktop 工作库中明确指定的第一轮。"""
    if isinstance(run_id, bool) or not isinstance(run_id, int) or run_id <= 0:
        raise RuntimeError("invalid_round1_run_id")
    run = connection.execute(
        "SELECT started_at, status FROM runs WHERE run_id = ?", (run_id,)
    ).fetchone()
    if run is None:
        raise RuntimeError("round1_run_not_found")
    if str(run["status"]) != "success":
        raise RuntimeError("round1_run_not_successful")
    row = connection.execute(
        """
        SELECT COUNT(*) AS completed_count,
               SUM(CASE WHEN selection_status = 'selected' THEN 1 ELSE 0 END) AS selected_count
        FROM screening_completion
        WHERE run_id = ? AND task_type = ? AND completion_status = 'completed'
          AND prompt_version = ? AND research_profile_version = ?
          AND selection_policy_version = ?
        """,
        (run_id, ROUND1_TASK_TYPE, prompt_version, profile_version, policy_version),
    ).fetchone()
    if not row["completed_count"]:
        raise RuntimeError("round1_run_has_no_selected_papers")
    return Round2RunSelection(run_id, str(run["started_at"]), int(row["selected_count"] or 0))


def sqlite_readonly_uri(database_path: Path) -> str:
    normalized_path = database_path.resolve().as_posix()
    return f"file:{quote(normalized_path, safe='/:')}?mode=ro"
