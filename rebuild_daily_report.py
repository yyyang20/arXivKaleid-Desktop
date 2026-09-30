from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, TextIO
from zoneinfo import ZoneInfo

import generate_round2_report
import content_labels
import daily_report_template
import model_usage
import round2_fulltext_state
import selection_nature


PROJECT_ROOT = Path(__file__).resolve().parent
SHANGHAI_TIMEZONE = ZoneInfo("Asia/Shanghai")
CURRENT_AUTOMATIC_CUTOFF = (18, 3, 0)
LEGACY_AUTOMATIC_CUTOFF = (18, 33, 0)
REPORT_MODE_AUTOMATIC = "automatic"
REPORT_MODE_MANUAL = "manual_backfill"
REPORT_MODE_CONTROLLED = "controlled_replay"
REPORT_MODES = {
    REPORT_MODE_AUTOMATIC,
    REPORT_MODE_MANUAL,
    REPORT_MODE_CONTROLLED,
}


@dataclass(frozen=True)
class ReportTimingContext:
    """把日报归属时间与实际执行时间分开，避免跨日延迟改变报告日期。"""

    report_mode: str
    report_date: str
    data_cutoff_at: str
    program_started_at: str
    schedule_delay_seconds: int | None
    source_run_id: int | None = None
    source_batch_id: int | None = None


@dataclass(frozen=True)
class RebuiltDailyReportData:
    run_id: int
    started_at: str
    run_status: str
    run_message: str
    main_runtime_seconds: int
    round2_runtime_seconds: int | None
    target_submission_date: str
    statistics: dict[str, Any]
    usage_summary: model_usage.RunUsageSummary | None
    round1_papers: tuple[dict[str, Any], ...]
    round2: generate_round2_report.Round2ReportData
    round1_selection_audit: dict[str, Any] | None = None
    timing_context: ReportTimingContext | None = None
    model_names: tuple[str, ...] = ()


def _parse_timing_timestamp(value: str, *, code: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise RuntimeError(code) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise RuntimeError(code)
    return parsed.astimezone(timezone.utc)


def validate_report_timing_context(
    value: ReportTimingContext,
) -> tuple[datetime, datetime]:
    if value.report_mode not in REPORT_MODES:
        raise RuntimeError("report_timing_mode_invalid")
    try:
        report_date = datetime.strptime(value.report_date, "%Y-%m-%d").date()
    except (TypeError, ValueError) as exc:
        raise RuntimeError("report_timing_date_invalid") from exc
    cutoff = _parse_timing_timestamp(
        value.data_cutoff_at, code="report_data_cutoff_invalid"
    )
    started = _parse_timing_timestamp(
        value.program_started_at, code="report_program_started_at_invalid"
    )
    if cutoff.astimezone(SHANGHAI_TIMEZONE).date() != report_date:
        raise RuntimeError("report_timing_date_cutoff_mismatch")
    if value.report_mode == REPORT_MODE_AUTOMATIC:
        cutoff_local = cutoff.astimezone(SHANGHAI_TIMEZONE)
        if (cutoff_local.hour, cutoff_local.minute, cutoff_local.second) not in {
            CURRENT_AUTOMATIC_CUTOFF,
            LEGACY_AUTOMATIC_CUTOFF,
        }:
            raise RuntimeError("report_automatic_cutoff_invalid")
        delay = value.schedule_delay_seconds
        expected_delay = int((started - cutoff).total_seconds())
        if (
            isinstance(delay, bool)
            or not isinstance(delay, int)
            or delay < 0
            or expected_delay != delay
        ):
            raise RuntimeError("report_schedule_delay_invalid")
        if value.source_run_id is not None or value.source_batch_id is not None:
            raise RuntimeError("report_timing_source_invalid")
    elif value.report_mode == REPORT_MODE_MANUAL:
        cutoff_local = cutoff.astimezone(SHANGHAI_TIMEZONE)
        if (
            (cutoff_local.hour, cutoff_local.minute, cutoff_local.second)
            != (23, 59, 59)
            or
            value.schedule_delay_seconds is not None
            or value.source_run_id is not None
            or value.source_batch_id is not None
        ):
            raise RuntimeError("report_timing_manual_context_invalid")
    else:
        if (
            value.schedule_delay_seconds is not None
            or isinstance(value.source_run_id, bool)
            or not isinstance(value.source_run_id, int)
            or value.source_run_id < 1
            or isinstance(value.source_batch_id, bool)
            or not isinstance(value.source_batch_id, int)
            or value.source_batch_id < 1
        ):
            raise RuntimeError("report_timing_controlled_context_invalid")
    return cutoff, started


def default_report_timing_context(
    data: RebuiltDailyReportData, generated_at: datetime
) -> ReportTimingContext:
    """本地旧数据重建无自动账本时，归入 run 启动前最近的计划槽位。"""
    started = _parse_timing_timestamp(
        data.started_at, code="report_program_started_at_invalid"
    )
    local_started = started.astimezone(SHANGHAI_TIMEZONE)
    day = local_started.date()
    cutoff = datetime(
        day.year,
        day.month,
        day.day,
        *LEGACY_AUTOMATIC_CUTOFF,
        tzinfo=SHANGHAI_TIMEZONE,
    )
    if cutoff > local_started:
        day = day.fromordinal(day.toordinal() - 1)
        cutoff = datetime(
            day.year,
            day.month,
            day.day,
            *LEGACY_AUTOMATIC_CUTOFF,
            tzinfo=SHANGHAI_TIMEZONE,
        )
    cutoff_utc = cutoff.astimezone(timezone.utc)
    return ReportTimingContext(
        report_mode=REPORT_MODE_AUTOMATIC,
        report_date=day.isoformat(),
        data_cutoff_at=cutoff_utc.isoformat(timespec="seconds").replace(
            "+00:00", "Z"
        ),
        program_started_at=started.isoformat(timespec="seconds").replace(
            "+00:00", "Z"
        ),
        schedule_delay_seconds=int((started - cutoff_utc).total_seconds()),
    )


def elapsed_iso_seconds(started_at: str, finished_at: str) -> int:
    """计算两个带时区 ISO 时间之间的非负秒数。"""
    try:
        started = datetime.fromisoformat(started_at)
        finished = datetime.fromisoformat(finished_at)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("invalid_run_runtime_timestamp") from exc
    if (
        started.tzinfo is None
        or started.utcoffset() is None
        or finished.tzinfo is None
        or finished.utcoffset() is None
    ):
        raise RuntimeError("run_runtime_timestamp_missing_timezone")
    seconds = (finished - started).total_seconds()
    if seconds < 0:
        raise RuntimeError("negative_run_runtime")
    return int(round(seconds))


def format_runtime(seconds: int) -> str:
    if seconds < 0:
        raise RuntimeError("negative_program_runtime")
    hours, remainder = divmod(seconds, 3600)
    minutes, remaining_seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}小时{minutes}分{remaining_seconds}秒"
    return f"{minutes}分{remaining_seconds}秒"


def format_decimal_without_trailing_zeros(value: Decimal) -> str:
    """按定点格式保留全部有效数字，仅删除小数末尾的零。"""
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def format_date_range(values: set[str]) -> str:
    """单日直接显示日期，多日显示稳定的起止范围。"""
    ordered = sorted(value for value in values if value)
    if not ordered:
        return "未记录"
    if len(ordered) == 1:
        return ordered[0]
    return f"{ordered[0]} → {ordered[-1]}"


def validate_date_range_text(value: str) -> str:
    match = re.fullmatch(
        r"(\d{4}-\d{2}-\d{2})(?: → (\d{4}-\d{2}-\d{2}))?", value
    )
    if match is None:
        raise RuntimeError("report_submission_date_range_invalid")
    try:
        start = datetime.strptime(match.group(1), "%Y-%m-%d").date()
        end = datetime.strptime(match.group(2) or match.group(1), "%Y-%m-%d").date()
    except ValueError as exc:
        raise RuntimeError("report_submission_date_range_invalid") from exc
    if end < start:
        raise RuntimeError("report_submission_date_range_invalid")
    return value


def validate_rebuild_schema(connection: sqlite3.Connection) -> None:
    required = {
        "screening_completion": {
            "run_id",
            "task_type",
            "arxiv_id",
            "version",
        },
        "pdf_downloads": {
            "arxiv_id",
            "version",
            "download_status",
            "failure_reason",
        },
        "pdf_sections": {
            "arxiv_id",
            "version",
            "extraction_status",
            "failure_reason",
        },
    }
    generate_round2_report.validate_schema(connection)
    for table, columns in required.items():
        rows = connection.execute(f"PRAGMA table_info({table})").fetchall()
        existing = {str(row["name"]) for row in rows}
        if not rows:
            raise RuntimeError(f"missing_table:{table}")
        missing = sorted(columns - existing)
        if missing:
            raise RuntimeError(f"missing_columns:{table}:{','.join(missing)}")


def load_statistics(
    connection: sqlite3.Connection, run_id: int
) -> dict[str, Any]:
    """直接从第一轮完成记录统计处理数量，不依赖历史日报索引。"""
    row = connection.execute(
        """
        SELECT COUNT(*) AS candidate_count
        FROM (
            SELECT arxiv_id, version
            FROM screening_completion
            WHERE run_id = ? AND task_type = ?
            GROUP BY arxiv_id, version
        )
        """,
        (run_id, generate_round2_report.ROUND1_TASK_TYPE),
    ).fetchone()
    return {"candidate_count": int(row["candidate_count"])}


def load_stage_selection_audit(
    connection: sqlite3.Connection, run_id: int, task_type: str
) -> dict[str, Any] | None:
    tables = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }
    if "screening_stage_audits" not in tables:
        return None
    row = connection.execute(
        """
        SELECT audit_json
        FROM screening_stage_audits
        WHERE run_id = ? AND task_type = ?
        """,
        (run_id, task_type),
    ).fetchone()
    if row is None:
        return None
    try:
        audit = json.loads(str(row["audit_json"]))
    except json.JSONDecodeError:
        return None
    return audit if isinstance(audit, dict) else None


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
               d.failure_reason AS download_failure_reason,
               COALESCE(ps.extraction_status, 'not_recorded') AS extraction_status,
               ps.failure_reason AS extraction_failure_reason
        FROM screening_results AS s
        JOIN papers AS p
          ON p.arxiv_id = s.arxiv_id AND p.version = s.version
        LEFT JOIN pdf_downloads AS d
          ON d.arxiv_id = s.arxiv_id AND d.version = s.version
        LEFT JOIN pdf_sections AS ps
          ON ps.arxiv_id = s.arxiv_id AND ps.version = s.version
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
        item["content_label"] = content_labels.content_label_from_details_json(
            item["details_json"], prompt_version=item["prompt_version"]
        )
        item["selection_nature"] = (
            selection_nature.round1_selection_nature_from_content_label(
                item["content_label"], prompt_version=item["prompt_version"]
            )
        )
        papers.append(item)
    return tuple(papers)


def load_candidate_submission_date_range(
    connection: sqlite3.Connection, run_id: int
) -> str:
    rows = connection.execute(
        """
        SELECT c.arxiv_id, c.version, p.updated
        FROM (
            SELECT arxiv_id, version
            FROM screening_completion
            WHERE run_id = ? AND task_type = ?
            GROUP BY arxiv_id, version
        ) AS c
        JOIN papers AS p
          ON p.arxiv_id = c.arxiv_id AND p.version = c.version
        ORDER BY c.arxiv_id, c.version
        """,
        (run_id, generate_round2_report.ROUND1_TASK_TYPE),
    ).fetchall()
    expected_count = int(
        connection.execute(
            """
            SELECT COUNT(*) FROM (
                SELECT arxiv_id, version
                FROM screening_completion
                WHERE run_id = ? AND task_type = ?
                GROUP BY arxiv_id, version
            )
            """,
            (run_id, generate_round2_report.ROUND1_TASK_TYPE),
        ).fetchone()[0]
    )
    if not rows or len(rows) != expected_count:
        raise RuntimeError("report_candidate_submission_timestamp_missing")
    dates = {
        generate_round2_report.submission_date_utc(row["updated"]) for row in rows
    }
    return validate_date_range_text(format_date_range(dates))


def add_round2_page_gate_state(
    papers: tuple[dict[str, Any], ...], fulltext_database: Path
) -> tuple[dict[str, Any], ...]:
    """旧报告可在无全文状态库时继续生成；新状态存在时增加页数标记。"""
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


def load_rebuilt_report_data(
    connection: sqlite3.Connection,
    *,
    run_id: int | None,
    latest: bool,
    project_root: Path | None = None,
    fulltext_database: Path | None = None,
) -> RebuiltDailyReportData:
    validate_rebuild_schema(connection)
    run = generate_round2_report.select_run(
        connection, run_id=run_id, latest=latest
    )
    selected_run_id = int(run["run_id"])
    run_details = connection.execute(
        "SELECT message, finished_at FROM runs WHERE run_id = ?", (selected_run_id,)
    ).fetchone()
    if run_details is None or not run_details["finished_at"]:
        raise RuntimeError("run_finished_at_missing")
    resolved_fulltext_database = fulltext_database
    if resolved_fulltext_database is None and project_root is not None:
        resolved_fulltext_database = project_root / "data/round2_inputs.sqlite"
    eligible_paper_keys = (
        None
        if resolved_fulltext_database is None
        else generate_round2_report.load_eligible_paper_keys(
            resolved_fulltext_database
        )
    )
    round2 = generate_round2_report.load_and_validate_round2(
        connection,
        run_id=selected_run_id,
        latest=False,
        eligible_paper_keys=eligible_paper_keys,
    )
    round1_papers = load_round1_papers(connection, selected_run_id)
    if resolved_fulltext_database is not None:
        round1_papers = add_round2_page_gate_state(
            round1_papers, resolved_fulltext_database
        )
    round1_by_key = {
        (str(row["arxiv_id"]), int(row["version"])): row for row in round1_papers
    }
    enriched_round2: list[dict[str, Any]] = []
    for row in round2.recommendations:
        key = (str(row["arxiv_id"]), int(row["version"]))
        round1_row = round1_by_key.get(key)
        if round1_row is None:
            raise RuntimeError("round2_report_round1_identity_missing")
        round1_rank = round1_row.get("result_rank")
        if (
            isinstance(round1_rank, bool)
            or not isinstance(round1_rank, int)
            or round1_rank < 1
        ):
            raise RuntimeError("round2_report_round1_rank_invalid")
        page_count = round1_row.get("page_count")
        if (
            resolved_fulltext_database is not None
            and resolved_fulltext_database.is_file()
            and (
                isinstance(page_count, bool)
                or not isinstance(page_count, int)
                or page_count < 1
            )
        ):
            raise RuntimeError("round2_report_page_count_invalid")
        enriched_round2.append(
            {
                **row,
                "round1_result_rank": round1_rank,
                "page_count": page_count,
            }
        )
    round2 = replace(round2, recommendations=tuple(enriched_round2))
    if project_root is not None:
        round1_papers = tuple(
            add_local_pdf_state(row, project_root) for row in round1_papers
        )
        local_pdf_by_paper = {
            (str(row["arxiv_id"]), int(row["version"])): {
                "local_pdf_report_path": row["local_pdf_report_path"],
                "local_pdf_exists": row["local_pdf_exists"],
            }
            for row in round1_papers
        }
        round2 = replace(
            round2,
            recommendations=tuple(
                {
                    **row,
                    **local_pdf_by_paper.get(
                        (str(row["arxiv_id"]), int(row["version"])),
                        {"local_pdf_report_path": "", "local_pdf_exists": False},
                    ),
                }
                for row in round2.recommendations
            ),
        )
    return RebuiltDailyReportData(
        run_id=selected_run_id,
        started_at=str(run["started_at"]),
        run_status=str(run["status"]),
        run_message=str(run_details["message"] or "") if run_details else "",
        main_runtime_seconds=elapsed_iso_seconds(
            str(run["started_at"]), str(run_details["finished_at"])
        ),
        round2_runtime_seconds=model_usage.load_run_stage_duration_seconds(
            connection, selected_run_id, "round2"
        ),
        target_submission_date=load_candidate_submission_date_range(
            connection, selected_run_id
        ),
        statistics=load_statistics(connection, selected_run_id),
        usage_summary=model_usage.load_run_usage_summary(
            connection, selected_run_id
        ),
        round1_papers=round1_papers,
        round2=round2,
        round1_selection_audit=load_stage_selection_audit(
            connection,
            selected_run_id,
            generate_round2_report.ROUND1_TASK_TYPE,
        ),
        model_names=model_usage.load_run_model_names(connection, selected_run_id),
    )


def add_local_pdf_state(
    row: dict[str, Any], project_root: Path
) -> dict[str, Any]:
    """把数据库路径约束为项目内相对路径，并核查对应文件是否存在。"""
    item = dict(row)
    raw_path = str(item.get("local_pdf_path") or "").strip()
    item["local_pdf_report_path"] = ""
    item["local_pdf_exists"] = False
    if not raw_path:
        return item
    root = project_root.resolve()
    candidate = Path(raw_path)
    full_path = candidate if candidate.is_absolute() else root / candidate
    try:
        relative_path = full_path.resolve().relative_to(root)
    except (OSError, ValueError):
        return item
    item["local_pdf_report_path"] = relative_path.as_posix()
    item["local_pdf_exists"] = full_path.is_file()
    return item


def report_mode_display(report_mode: str) -> str:
    displays = {
        REPORT_MODE_AUTOMATIC: "自动",
        REPORT_MODE_MANUAL: "手动回查",
        REPORT_MODE_CONTROLLED: "受控重做",
    }
    try:
        return displays[report_mode]
    except KeyError as exc:
        raise RuntimeError("report_timing_mode_invalid") from exc


def format_report_timestamp(value: datetime) -> str:
    return value.astimezone(SHANGHAI_TIMEZONE).strftime("%Y-%m-%d %H:%M:%S")


def build_rebuilt_report_text(
    data: RebuiltDailyReportData,
    generated_at: datetime,
    report_runtime_seconds: int = 0,
    *,
    link_mode: str = generate_round2_report.REPORT_LINK_MODE_LOCAL,
    template_version: str | None = None,
) -> str:
    generate_round2_report.validate_report_link_mode(link_mode)
    if report_runtime_seconds < 0:
        raise RuntimeError("negative_report_runtime")
    round2_runtime_seconds = data.round2_runtime_seconds or 0
    program_runtime_seconds = (
        data.main_runtime_seconds
        + round2_runtime_seconds
        + report_runtime_seconds
    )
    legacy_model_names = {
        str(row.get("model_name") or "").strip()
        for row in (*data.round1_papers, *data.round2.recommendations)
        if str(row.get("model_name") or "").strip()
    }
    model_display = (
        "、".join(data.model_names)
        if data.model_names
        else "、".join(sorted(legacy_model_names))
        if data.usage_summary is None and legacy_model_names
        else "未调用"
    )
    if data.usage_summary is None:
        token_text = "未记录"
        cost_text = "未记录"
    else:
        usage = data.usage_summary
        token_text = (
            str(usage.known_total_tokens)
            if usage.token_complete
            else f"已知 {usage.known_total_tokens}（统计不完整）"
        )
        cost_value = format_decimal_without_trailing_zeros(usage.known_cost)
        currency = usage.currency or "币种未知"
        if usage.cost_complete:
            cost_text = f"¥{cost_value}"
        elif usage.known_cost > 0:
            cost_text = f"已知 ¥{cost_value}（{currency}；统计不完整）"
        else:
            cost_text = "未记录（usage 或价格信息不完整）"
    parsed_count = data.statistics["candidate_count"]
    round1_count = len(data.round1_papers)
    round2_count = len(data.round2.recommendations)
    round1_audit = data.round1_selection_audit or {}
    round2_audit = data.round2.selection_audit or {}
    round1_model_output_count = int(
        round1_audit.get("model_output_count", round1_count)
    )
    round2_model_output_count = int(
        round2_audit.get("model_output_count", round2_count)
    )
    anomaly_text = (
        "无"
        if data.round2.status in {"round2_results_valid", "round2_no_candidates"}
        else "；".join(data.round2.reasons)
    )
    submission_date_range = validate_date_range_text(data.target_submission_date)
    timing = data.timing_context or default_report_timing_context(data, generated_at)
    cutoff_utc, started_utc = validate_report_timing_context(timing)
    generated_utc = generated_at.astimezone(timezone.utc)
    if generated_utc < started_utc:
        raise RuntimeError("report_generated_before_program_start")
    selected_template_version = template_version
    if (
        selected_template_version
        == daily_report_template.LEGACY_TIMING_REPORT_TEMPLATE_VERSION
    ):
        label_counts = daily_report_template.Round2LabelCounts(
            imaging=0,
            new_solution=0,
            imaging_new_solution=0,
            not_recommended=data.round2.candidate_count,
        )
    else:
        try:
            label_counts = daily_report_template.build_round2_label_counts(
                data.round2.recommendations,
                round2_input=data.round2.candidate_count,
            )
        except RuntimeError as exc:
            if template_version is not None:
                raise
            if str(exc) != "daily_report_round2_content_label_invalid":
                raise
            # 历史理由码标签不映射为新版三类统计，继续生成 v13 兼容报告。
            selected_template_version = (
                daily_report_template.LEGACY_TIMING_REPORT_TEMPLATE_VERSION
            )
            label_counts = daily_report_template.Round2LabelCounts(
                imaging=0,
                new_solution=0,
                imaging_new_solution=0,
                not_recommended=data.round2.candidate_count,
            )
    if selected_template_version is None:
        selected_template_version = daily_report_template.DAILY_REPORT_TEMPLATE_VERSION
    header = daily_report_template.DailyReportHeaderView(
        report_run_id=data.run_id,
        report_generated_at=generated_at,
        program_runtime_seconds=program_runtime_seconds,
        main_runtime_seconds=data.main_runtime_seconds,
        round2_runtime_seconds=round2_runtime_seconds,
        report_runtime_seconds=report_runtime_seconds,
        report_mode=timing.report_mode,
        report_date=timing.report_date,
        data_cutoff_at=timing.data_cutoff_at,
        program_started_at=timing.program_started_at,
        schedule_delay_seconds=timing.schedule_delay_seconds,
        source_run_id=timing.source_run_id,
        source_batch_id=timing.source_batch_id,
        submission_date_range=submission_date_range,
        update_count=parsed_count,
        model_display=model_display,
        token_text=token_text,
        cost_text=cost_text,
        anomaly_text=anomaly_text,
        round1=daily_report_template.StageCounts(
            parsed_count, round1_model_output_count, round1_count
        ),
        round2=daily_report_template.StageCounts(
            data.round2.candidate_count,
            round2_model_output_count,
            round2_count,
        ),
        round2_labels=label_counts,
    )
    lines = daily_report_template.render_daily_report_header(
        header, template_version=selected_template_version
    )

    if (
        not data.round1_papers
        and data.round2.candidate_count == 0
        and data.round1_selection_audit is not None
    ):
        lines.append(
            "## Round 2 最终推荐\n\n"
            "Round 1 没有入围论文，因此未调用 Round 2。\n"
        )
    elif data.round1_papers and data.round2.candidate_count == 0:
        lines.append(
            "## Round 2 最终推荐\n\n"
            "没有论文通过 PDF/全文门控，因此未调用 Round 2。\n"
        )
    else:
        lines.append(
            generate_round2_report.build_round2_section(
                data.round2, link_mode=link_mode
            )
        )
    lines.extend(["## Round 1 入围论文", ""])
    if not data.round1_papers:
        empty_round1_text = (
            "Round 1 已完成，本批没有论文入围。"
            if data.round1_selection_audit is not None
            else "该 run 没有已保存的 Round 1 入围结果。"
        )
        lines.extend([empty_round1_text, ""])
    for row in data.round1_papers:
        decision = str(row.get("round2_decision") or "")
        if decision == "long_reading":
            round2_status = "超过 60 页，未进入 Round 2"
        elif decision == "fulltext_extraction_failed":
            reason = row.get("round2_decision_failure_reason") or row.get(
                "failure_reason"
            )
            round2_status = f"全文解析失败，未进入 Round 2：{reason or '未知原因'}"
        elif decision == "token_budget_excluded":
            round2_status = "全文体量导致整批 Token 预算超限，未进入 Round 2"
        elif decision == "full_text":
            round2_status = "使用完整全文进入 Round 2"
        elif row.get("download_status") == "failed":
            round2_status = (
                "PDF 下载失败，未进入 Round 2："
                f"{row.get('download_failure_reason') or '未知原因'}"
            )
        elif row.get("page_gate_status") == round2_fulltext_state.PAGE_GATE_LONG_READING:
            # 历史 artifact 无决策表时保留只读渲染。
            round2_status = "超过 60 页，未进入 Round 2"
        else:
            round2_status = "页数或 Round 2 状态未记录"
        page_count = row.get("page_count")
        page_count_text = (
            str(page_count)
            if isinstance(page_count, int)
            and not isinstance(page_count, bool)
            and page_count > 0
            else "未获取"
        )
        pdf_page_line = (
            f"- PDF 实际页数：{page_count_text}（{round2_status}）"
        )
        local_pdf_line = (
            generate_round2_report.build_local_pdf_line(row)
            if link_mode == generate_round2_report.REPORT_LINK_MODE_LOCAL
            else None
        )
        lines.extend(
            [
                f"### Rank {row['result_rank']}：{row['title']}",
                "",
                f"- arXiv ID：{row['arxiv_id']}v{row['version']}",
                "- 提交日期（UTC）："
                f"{generate_round2_report.submission_date_utc(row.get('updated'))}",
                f"- 作者：{row['authors'] or '未知'}",
                f"- 分类：{row['categories'] or '未知'}",
                f"- 内容标签：{row.get('content_label') or '未记录'}",
                pdf_page_line,
                f"- Round 1 理由：{row['reason'] or '未提供'}",
                "- arXiv 摘要页："
                f"[打开摘要页]({row['abs_url'] or '未知'})",
                "- 在线 PDF："
                f"[打开 PDF]({row['pdf_url'] or '未知'})",
                *([local_pdf_line] if local_pdf_line else []),
                "",
                "---",
                "",
            ]
        )
    return "\n".join(lines)


def write_rebuilt_report(
    reports_dir: Path,
    data: RebuiltDailyReportData,
    *,
    report_started_perf: float | None = None,
    link_mode: str = generate_round2_report.REPORT_LINK_MODE_LOCAL,
) -> tuple[Path, datetime]:
    generate_round2_report.validate_report_link_mode(link_mode)
    reports_dir.mkdir(parents=True, exist_ok=True)
    generated_at = datetime.now().astimezone()
    stem = generated_at.strftime(f"daily_run-{data.run_id}_%Y-%m-%d_%H%M%S")
    for suffix in range(100):
        name = f"{stem}.md" if suffix == 0 else f"{stem}_{suffix}.md"
        path = reports_dir / name
        try:
            with path.open("x", encoding="utf-8", newline="\n") as file:
                report_runtime_seconds = (
                    max(0, int(round(time.perf_counter() - report_started_perf)))
                    if report_started_perf is not None
                    else 0
                )
                file.write(
                    build_rebuilt_report_text(
                        data,
                        generated_at,
                        report_runtime_seconds,
                        link_mode=link_mode,
                    )
                )
                file.write("\n")
            return path, generated_at
        except FileExistsError:
            continue
        except OSError as exc:
            raise RuntimeError("cannot_write_rebuilt_daily_report") from exc
    raise RuntimeError("cannot_allocate_unique_rebuilt_report_name")


def rebuild_report_command(
    project_root: Path,
    *,
    run_id: int | None,
    latest: bool,
    link_mode: str = generate_round2_report.REPORT_LINK_MODE_LOCAL,
    stdout: TextIO = sys.stdout,
) -> int:
    report_started_perf = time.perf_counter()
    try:
        database_path, daily_reports_dir = (
            generate_round2_report.load_database_and_daily_reports_dir(project_root)
        )
        connection = generate_round2_report.open_readonly_database(database_path)
        try:
            data = load_rebuilt_report_data(
                connection,
                run_id=run_id,
                latest=latest,
                project_root=(
                    project_root
                    if link_mode
                    == generate_round2_report.REPORT_LINK_MODE_LOCAL
                    else None
                ),
            )
        finally:
            connection.close()
        report_path, _ = write_rebuilt_report(
            daily_reports_dir,
            data,
            report_started_perf=report_started_perf,
            link_mode=link_mode,
        )
    except Exception as exc:
        print("REBUILT_REPORT_STATUS=failed", file=stdout)
        print(f"ERROR_TYPE={type(exc).__name__}", file=stdout)
        print(f"ERROR_REASON={exc}", file=stdout)
        return 1
    print(f"REBUILT_REPORT_STATUS={data.round2.status}", file=stdout)
    print(f"RUN_ID={data.run_id}", file=stdout)
    print(f"REPORT_PATH={report_path.relative_to(project_root)}", file=stdout)
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="从 SQLite 已保存数据重建完整日报；不调用模型。"
    )
    selector = parser.add_mutually_exclusive_group(required=True)
    selector.add_argument("--run-id", type=int, help="指定主流程 RUN_ID。")
    selector.add_argument("--latest", action="store_true", help="选择最新成功 run。")
    parser.add_argument(
        "--link-mode",
        choices=generate_round2_report.REPORT_LINK_MODES,
        default=generate_round2_report.REPORT_LINK_MODE_LOCAL,
        help="链接模式；cloud 明确省略本地 PDF 链接（默认：local）。",
    )
    return parser.parse_args(argv)


def main_cli(
    argv: list[str] | None = None,
    *,
    project_root: Path = PROJECT_ROOT,
    stdout: TextIO = sys.stdout,
) -> int:
    args = parse_args(argv)
    return rebuild_report_command(
        project_root,
        run_id=args.run_id,
        latest=bool(args.latest),
        link_mode=args.link_mode,
        stdout=stdout,
    )


if __name__ == "__main__":
    raise SystemExit(main_cli())
