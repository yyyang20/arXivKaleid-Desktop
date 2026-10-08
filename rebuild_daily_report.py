# Copyright (c) 2026 yyyang20
# SPDX-License-Identifier: GPL-3.0-only
# See LICENSE in the project root for the full license text.

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import generate_round2_report
import round2_fulltext_state


def load_round1_papers(
    connection: sqlite3.Connection, run_id: int
) -> tuple[dict[str, Any], ...]:
    rows = connection.execute(
        """
        SELECT s.arxiv_id, s.version, s.result_rank, s.score, s.confidence,
               s.reason, s.details_json, s.model_name, s.prompt_version,
               s.research_profile_version,
               p.title, p.authors, p.categories, p.published, p.updated,
               p.abs_url, p.pdf_url, d.local_pdf_path,
               COALESCE(d.download_status, 'not_recorded') AS download_status,
               d.failure_reason AS download_failure_reason
        FROM screening_results AS s
        JOIN papers AS p
          ON p.arxiv_id = s.arxiv_id AND p.version = s.version
        LEFT JOIN pdf_downloads AS d
          ON d.arxiv_id = s.arxiv_id AND d.version = s.version
        WHERE s.run_id = ? AND s.task_type = ? AND s.is_selected = 1
        ORDER BY s.result_rank, s.arxiv_id
        """,
        (run_id, generate_round2_report.ROUND1_TASK_TYPE),
    ).fetchall()
    papers: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        try:
            details = json.loads(str(item.get("details_json") or "{}"))
        except json.JSONDecodeError:
            details = {}
        if not isinstance(details, dict):
            details = {}
        model_position = details.get("model_position")
        item["model_position"] = (
            model_position
            if isinstance(model_position, int) and not isinstance(model_position, bool)
            else None
        )
        item["evaluation"] = str(item.get("reason") or "")
        papers.append(item)
    return tuple(papers)


def add_round2_page_gate_state(
    papers: tuple[dict[str, Any], ...], fulltext_database: Path
) -> tuple[dict[str, Any], ...]:
    """为本次工作库中的论文加入全文门控事实。"""
    if not fulltext_database.is_file():
        return papers
    uri = fulltext_database.resolve().as_uri() + "?mode=ro"
    connection = sqlite3.connect(uri, timeout=10, uri=True)
    connection.row_factory = sqlite3.Row
    try:
        round2_fulltext_state.validate_fulltext_schema(connection)
        states = {
            (str(row["arxiv_id"]), int(row["version"])): dict(row)
            for row in connection.execute(
                """
                SELECT arxiv_id, version, page_count, page_gate_status,
                       extraction_status, failure_reason
                FROM pdf_fulltext_documents
                """
            ).fetchall()
        }
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        decisions = (
            {
                (str(row["arxiv_id"]), int(row["version"])): dict(row)
                for row in connection.execute(
                    """
                    SELECT arxiv_id, version,
                           decision AS round2_decision,
                           estimated_tokens AS round2_estimated_tokens,
                           failure_reason AS round2_decision_failure_reason
                    FROM round2_input_decisions
                    """
                ).fetchall()
            }
            if "round2_input_decisions" in tables
            else {}
        )
    finally:
        connection.close()
    return tuple(
        {
            **paper,
            **states.get(
                (str(paper["arxiv_id"]), int(paper["version"])),
                {
                    "page_count": None,
                    "page_gate_status": "not_recorded",
                    "extraction_status": "not_recorded",
                    "failure_reason": None,
                },
            ),
            **decisions.get(
                (str(paper["arxiv_id"]), int(paper["version"])),
                {},
            ),
        }
        for paper in papers
    )
