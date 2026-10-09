# Copyright (c) 2026 yyyang20
# SPDX-License-Identifier: GPL-3.0-only
# See LICENSE in the project root for the full license text.

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import build_round2_inputs
import main


ROUND1_TASK_TYPE = "round1_abstract_screening"
ROUND2_TASK_TYPE = "round2_batch_ranking"
REPORT_LINK_MODE_LOCAL = "local"
REPORT_LINK_MODE_CLOUD = "cloud"
REPORT_LINK_MODES = (REPORT_LINK_MODE_LOCAL, REPORT_LINK_MODE_CLOUD)
ABSTRACT_URL_PATTERN = re.compile(r"https?://[^\s<>]+")
UNESCAPED_TILDE_PATTERN = re.compile(r"(?<!\\)~")
LATEX_NONBREAKING_SPACE = "\u00a0"


@dataclass(frozen=True)
class Round2ReportData:
    run_id: int
    started_at: str
    run_status: str
    status: str
    reasons: tuple[str, ...]
    recommendations: tuple[dict[str, Any], ...]
    selection_audit: dict[str, Any] | None = None
    candidate_count: int = 0


def submission_date_utc(value: object) -> str:
    """把当前 arXiv 版本的 updated 时间严格转换为 UTC 日期。"""
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError("report_submission_timestamp_missing")
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise RuntimeError("report_submission_timestamp_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise RuntimeError("report_submission_timestamp_missing_timezone")
    return parsed.astimezone(timezone.utc).date().isoformat()


def open_readonly_database(database_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(
        build_round2_inputs.sqlite_readonly_uri(database_path), uri=True, timeout=10
    )
    connection.row_factory = sqlite3.Row
    return connection


def validate_schema(connection: sqlite3.Connection) -> None:
    required = {
        "runs": {"run_id", "started_at", "status"},
        "papers": {
            "arxiv_id",
            "version",
            "title",
            "authors",
            "categories",
            "summary",
            "abs_url",
            "pdf_url",
            "updated",
        },
        "screening_results": {
            "run_id",
            "task_type",
            "arxiv_id",
            "version",
            "result_rank",
            "recommendation_level",
            "score",
            "is_selected",
            "reason",
            "model_name",
            "prompt_version",
            "research_profile_version",
        },
    }
    for table, columns in required.items():
        rows = connection.execute(f"PRAGMA table_info({table})").fetchall()
        existing = {str(row["name"]) for row in rows}
        if not rows:
            raise RuntimeError(f"missing_table:{table}")
        missing = sorted(columns - existing)
        if missing:
            raise RuntimeError(f"missing_columns:{table}:{','.join(missing)}")


def select_run(connection: sqlite3.Connection, *, run_id: int) -> sqlite3.Row:
    """只读取当前 Desktop 明确指定的运行。"""
    if type(run_id) is not int or run_id <= 0:
        raise RuntimeError("report_run_invalid")
    row = connection.execute(
        "SELECT run_id, started_at, status FROM runs WHERE run_id = ?", (run_id,)
    ).fetchone()
    if row is None:
        raise RuntimeError("report_run_not_found")
    return row


def load_and_validate_round2(
    connection: sqlite3.Connection,
    *,
    run_id: int,
    eligible_paper_keys: set[tuple[str, int]] | None = None,
) -> Round2ReportData:
    validate_schema(connection)
    run = select_run(connection, run_id=run_id)
    selected_rows = connection.execute(
        """
        SELECT arxiv_id, version, result_rank
        FROM screening_results
        WHERE run_id = ? AND task_type = ? AND is_selected = 1
        """,
        (run["run_id"], ROUND1_TASK_TYPE),
    ).fetchall()
    selected_ids = {
        (str(row["arxiv_id"]), int(row["version"])) for row in selected_rows
    }
    candidate_ids = (
        selected_ids
        if eligible_paper_keys is None
        else selected_ids & eligible_paper_keys
    )
    tables = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }
    audit: dict[str, Any] | None = None
    audit_prompt_version: str | None = None
    if "screening_stage_audits" in tables:
        audit_row = connection.execute(
            """
            SELECT prompt_version, audit_json
            FROM screening_stage_audits
            WHERE run_id = ? AND task_type = ?
            """,
            (run["run_id"], ROUND2_TASK_TYPE),
        ).fetchone()
        if audit_row is not None:
            audit_prompt_version = str(audit_row["prompt_version"] or "")
            try:
                parsed_audit = json.loads(str(audit_row["audit_json"]))
            except json.JSONDecodeError:
                parsed_audit = None
            if isinstance(parsed_audit, dict):
                audit = parsed_audit
    rows = connection.execute(
        """
        SELECT s.arxiv_id, s.version, s.result_rank,
               s.recommendation_level, s.score, s.confidence, s.reason,
               s.details_json,
               s.model_name, s.prompt_version,
               s.research_profile_version, p.title, p.authors,
               p.categories, p.summary, p.abs_url, p.pdf_url, p.updated
        FROM screening_results AS s
        JOIN papers AS p
          ON p.arxiv_id = s.arxiv_id AND p.version = s.version
        WHERE s.run_id = ? AND s.task_type = ? AND s.is_selected = 1
        ORDER BY s.result_rank, s.arxiv_id
        """,
        (run["run_id"], ROUND2_TASK_TYPE),
    ).fetchall()
    if not rows:
        if (
            audit_prompt_version == main.CURRENT_ROUND2_PROMPT_VERSION
            and main.screening_stage_audit_valid(audit, expected_accepted_count=0)
        ):
            return Round2ReportData(
                run_id=int(run["run_id"]),
                started_at=str(run["started_at"]),
                run_status=str(run["status"]),
                status="round2_results_valid",
                reasons=(),
                recommendations=(),
                selection_audit=audit,
                candidate_count=len(candidate_ids),
            )
        if not candidate_ids:
            return Round2ReportData(
                run_id=int(run["run_id"]),
                started_at=str(run["started_at"]),
                run_status=str(run["status"]),
                status="round2_no_candidates",
                reasons=(
                    "没有论文通过全文页数、解析质量与 Token 预算门控；"
                    "第二轮未调用模型。",
                ),
                recommendations=(),
                candidate_count=0,
            )
        return Round2ReportData(
            run_id=int(run["run_id"]),
            started_at=str(run["started_at"]),
            run_status=str(run["status"]),
            status="round2_not_executed",
            reasons=("该 run 没有已保存的第二轮结果。",),
            recommendations=(),
            candidate_count=len(candidate_ids),
        )

    reasons: list[str] = []
    prompt_versions = {str(row["prompt_version"] or "") for row in rows}
    if prompt_versions != {main.CURRENT_ROUND2_PROMPT_VERSION}:
        reasons.append("Desktop 仅支持当前版本的第二轮技术协议结果。")
    ranks = [row["result_rank"] for row in rows]
    if ranks != list(range(1, len(rows) + 1)):
        reasons.append("final rank 不是从 1 开始的连续整数。")
    if not main.screening_stage_audit_valid(audit, expected_accepted_count=len(rows)):
        reasons.append("缺少与有效推荐数量一致的技术选择审计。")

    for row in rows:
        key = (str(row["arxiv_id"]), int(row["version"]))
        if key not in candidate_ids:
            reasons.append(
                f"{key[0]}v{key[1]} 不属于该 run 通过页数门控的第二轮候选集合。"
            )

    for field in ("model_name", "prompt_version", "research_profile_version"):
        values = {str(row[field] or "") for row in rows}
        if len(values) != 1 or "" in values:
            reasons.append(f"{field} 在同一批结果中不一致或为空。")
    recommendations_list: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        try:
            details = json.loads(str(item.get("details_json") or "{}"))
        except json.JSONDecodeError:
            details = {}
        if not isinstance(details, dict):
            details = {}
        item["evaluation"] = str(item.get("reason") or "")
        model_position = details.get("model_position")
        item["model_position"] = (
            model_position
            if isinstance(model_position, int) and not isinstance(model_position, bool)
            else None
        )
        item["submission_date_utc"] = submission_date_utc(item.get("updated"))
        recommendations_list.append(item)
    recommendations = tuple(recommendations_list)
    return Round2ReportData(
        run_id=int(run["run_id"]),
        started_at=str(run["started_at"]),
        run_status=str(run["status"]),
        status="round2_results_invalid" if reasons else "round2_results_valid",
        reasons=tuple(reasons),
        recommendations=recommendations if not reasons else (),
        selection_audit=audit,
        candidate_count=len(candidate_ids),
    )


def validate_report_link_mode(link_mode: str) -> None:
    """只接受显式的本地或云端链接模式，避免根据运行环境猜测。"""
    if link_mode not in REPORT_LINK_MODES:
        raise RuntimeError(f"invalid_report_link_mode:{link_mode}")


def render_abstract_markdown_text(value: str) -> str:
    """转义摘要中的 Markdown 反引号，并安全转换 LaTeX 间隔。"""
    # arXiv 摘要常用 ``...'' 表示引号；若直接写入 GitHub Markdown，
    # 两组 `` 会跨段形成行内代码。实体编码保留可见字符但取消分隔符语义。
    text = str(value).replace("`", "&#96;")
    rendered: list[str] = []
    cursor = 0
    for match in ABSTRACT_URL_PATTERN.finditer(text):
        prefix = text[cursor : match.start()]
        rendered.append(
            UNESCAPED_TILDE_PATTERN.sub(LATEX_NONBREAKING_SPACE, prefix)
        )
        # URL 中的波浪号不是 LaTeX 间隔；百分号编码可保持链接语义并避免
        # GitHub 把跨 URL 的两个波浪号识别为删除线。
        rendered.append(match.group(0).replace("~", "%7E"))
        cursor = match.end()
    rendered.append(
        UNESCAPED_TILDE_PATTERN.sub(LATEX_NONBREAKING_SPACE, text[cursor:])
    )
    return "".join(rendered)


def build_round2_section(
    data: Round2ReportData, *, link_mode: str = REPORT_LINK_MODE_LOCAL
) -> str:
    """渲染可复用的第二轮 Markdown 区块。"""
    validate_report_link_mode(link_mode)
    lines = ["## Round 2 最终推荐", ""]
    if data.status == "round2_no_candidates":
        lines.extend([data.reasons[0], ""])
        return "\n".join(lines)
    if data.status != "round2_results_valid":
        lines.extend(["### 降级说明", ""])
        lines.extend(f"- {reason}" for reason in data.reasons)
        lines.append("")
        return "\n".join(lines)

    if not data.recommendations:
        lines.extend(["本批没有通过完整全文筛选的论文，生成 0 篇日报。", ""])
        return "\n".join(lines)

    for row in data.recommendations:
        abstract_lines = str(row.get("summary") or "未提供").splitlines() or [
            "未提供"
        ]
        abstract_quote = [
            f"> {render_abstract_markdown_text(line)}" if line else ">"
            for line in abstract_lines
        ]
        local_pdf_line = (
            build_local_pdf_line(row)
            if link_mode == REPORT_LINK_MODE_LOCAL
            else None
        )
        page_count = row.get("page_count")
        page_count_text = (
            str(page_count)
            if isinstance(page_count, int)
            and not isinstance(page_count, bool)
            and page_count > 0
            else "未记录"
        )
        lines.extend(
            [
                f"### Rank {row['result_rank']}：{row['title']}",
                "",
                f"- arXiv ID：{row['arxiv_id']}v{row['version']}",
                "- 提交日期（UTC）："
                f"{submission_date_utc(row.get('updated'))}",
                f"- 作者：{row.get('authors') or '未知'}",
                f"- 分类：{row.get('categories') or '未知'}",
                f"- PDF 实际页数：{page_count_text}",
                *([f"- Round 2 评价：{row['evaluation']}"] if row.get("evaluation") else []),
                "- arXiv 摘要页："
                f"[打开摘要页]({row.get('abs_url') or '未知'})",
                "- 在线 PDF："
                f"[打开 PDF]({row.get('pdf_url') or '未知'})",
                *([local_pdf_line] if local_pdf_line else []),
                "",
                "**摘要全文**",
                "",
                *abstract_quote,
                "",
                "---",
                "",
            ]
        )
    return "\n".join(lines)


def build_local_pdf_line(row: dict[str, Any]) -> str | None:
    """生成项目内相对链接，避免在报告中暴露本机绝对路径。"""
    if "local_pdf_report_path" not in row:
        return None
    relative_path = str(row.get("local_pdf_report_path") or "")
    if not relative_path:
        return "- 本地 PDF：未下载"
    if not bool(row.get("local_pdf_exists")):
        return "- 本地 PDF：文件不存在"
    return f"- 本地 PDF：[打开本地 PDF](<../../{relative_path}>)"


def load_eligible_paper_keys(
    fulltext_database_path: Path,
) -> set[tuple[str, int]] | None:
    """新产物只返回最终决定为完整全文输入的论文。"""
    if not fulltext_database_path.is_file():
        return None
    connection = open_readonly_database(fulltext_database_path)
    try:
        import round2_fulltext_state

        round2_fulltext_state.validate_fulltext_schema(connection)
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        if "round2_input_decisions" in tables:
            return {
                (str(row[0]), int(row[1]))
                for row in connection.execute(
                    """
                    SELECT arxiv_id, version
                    FROM round2_input_decisions
                    WHERE decision = 'full_text'
                    """
                ).fetchall()
            }
        raise RuntimeError("desktop_round2_decisions_missing")
    finally:
        connection.close()


def load_fulltext_page_counts(
    fulltext_database_path: Path,
) -> dict[tuple[str, int], int]:
    """读取当前独立全文状态库中的实际物理页数。"""
    if not fulltext_database_path.is_file():
        return {}
    connection = open_readonly_database(fulltext_database_path)
    try:
        import round2_fulltext_state

        round2_fulltext_state.validate_fulltext_schema(connection)
        return {
            (str(row["arxiv_id"]), int(row["version"])): int(row["page_count"])
            for row in connection.execute(
                """
                SELECT arxiv_id, version, page_count
                FROM pdf_fulltext_documents
                """
            ).fetchall()
        }
    finally:
        connection.close()
