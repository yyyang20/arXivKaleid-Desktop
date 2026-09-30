from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO
from urllib.parse import quote

from pdf_processing import load_runtime_paths, validate_pdf_sections_schema


PROJECT_ROOT = Path(__file__).resolve().parent
ROUND1_TASK_TYPE = "round1_abstract_screening"
ROUND2_ABSTRACT_SOURCE = "papers.summary"
ROUND2_USABLE_STATUS = "usable"
ROUND2_MISSING_REQUIRED_STATUS = "missing_required_fields"
# 只提示正文段可能混入的终止章节，不因此阻断 round2 usable 状态。
TERMINAL_SECTION_HEADING_PATTERN = re.compile(
    r"(?im)(?:^|\n)\s*(?:\d+\.?\s*)?"
    r"(?P<marker>references|bibliography|acknowledg(?:e)?ments|"
    r"data availability|appendix|supplementary material)\b"
)

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
        "task_type",
        "arxiv_id",
        "version",
        "completion_status",
        "selection_status",
        "prompt_version",
        "research_profile_version",
        "selection_policy_version",
    },
    "pdf_sections": {
        "arxiv_id",
        "version",
        "abstract_text",
        "introduction_text",
        "conclusion_text",
        "extraction_status",
        "failure_reason",
    },
}


@dataclass(frozen=True)
class Round2RunSelection:
    run_id: int
    started_at: str
    selected_count: int


@dataclass(frozen=True)
class Round2InputPreview:
    arxiv_id: str
    version: int
    result_rank: int
    score: int | None
    confidence: int | None
    pdf_extraction_status: str
    pdf_failure_reason: str | None
    abstract_source: str
    round2_input_status: str
    round1_score_present: bool
    round1_confidence_present: bool
    round1_reason_present: bool
    title_len: int
    authors_len: int
    categories_len: int
    primary_category_len: int
    metadata_abstract_len: int
    round1_reason_len: int
    pdf_introduction_len: int
    pdf_conclusion_len: int
    estimated_chars: int
    missing_fields: tuple[str, ...]
    contamination_warnings: tuple[str, ...]


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
    validate_pdf_sections_schema(connection)


def load_config(project_root: Path) -> dict[str, object]:
    try:
        return json.loads(
            (project_root / "config.json").read_text(encoding="utf-8-sig")
        )
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("cannot_read_config") from exc


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


def select_round1_run(
    connection: sqlite3.Connection,
    *,
    prompt_version: str,
    profile_version: str,
    policy_version: str,
    latest: bool,
    target_date: str | None,
    run_id: int | None = None,
) -> Round2RunSelection:
    selector_count = sum(
        (bool(latest), target_date is not None, run_id is not None)
    )
    if selector_count != 1:
        raise RuntimeError("choose_exactly_one_of_run_id_latest_or_date")
    if run_id is not None:
        if isinstance(run_id, bool) or run_id <= 0:
            raise RuntimeError("invalid_round1_run_id")
        run = connection.execute(
            "SELECT started_at, status FROM runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        if run is None:
            raise RuntimeError("round1_run_not_found")
        if str(run["status"]) != "success":
            raise RuntimeError("round1_run_not_successful")

    params: list[object] = [
        ROUND1_TASK_TYPE,
        prompt_version,
        profile_version,
        policy_version,
    ]
    date_filter = ""
    if target_date is not None:
        date_filter = "AND substr(r.started_at, 1, 10) = ?"
        params.append(target_date)
    run_filter = ""
    if run_id is not None:
        run_filter = "AND c.run_id = ?"
        params.append(run_id)
    selected_having = (
        ""
        if prompt_version in {"round1_v19", "round1_v20"}
        else "AND SUM(CASE WHEN c.selection_status = 'selected' THEN 1 ELSE 0 END) > 0"
    )

    row = connection.execute(
        f"""
        SELECT c.run_id, r.started_at,
               SUM(CASE WHEN c.selection_status = 'selected' THEN 1 ELSE 0 END)
                   AS selected_count
        FROM screening_completion AS c
        JOIN runs AS r ON r.run_id = c.run_id
        WHERE c.task_type = ?
          AND c.completion_status = 'completed'
          AND c.prompt_version = ?
          AND c.research_profile_version = ?
          AND c.selection_policy_version = ?
          AND r.status = 'success'
          {date_filter}
          {run_filter}
        GROUP BY c.run_id, r.started_at
        HAVING COUNT(*) > 0 {selected_having}
        ORDER BY c.run_id DESC
        LIMIT 1
        """,
        params,
    ).fetchone()
    if row is None:
        if run_id is not None:
            raise RuntimeError("round1_run_has_no_selected_papers")
        raise RuntimeError("round1_selected_run_not_found")
    return Round2RunSelection(
        run_id=int(row["run_id"]),
        started_at=str(row["started_at"]),
        selected_count=int(row["selected_count"]),
    )


def get_limit(config: dict[str, object], key: str, fallback: int) -> int:
    limits = config.get("limits")
    if not isinstance(limits, dict):
        return fallback
    value = limits.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return fallback
    return value


def clipped_text_length(value: object, max_chars: int | None = None) -> int:
    return len(clipped_text(value, max_chars))


def clipped_text(value: object, max_chars: int | None = None) -> str:
    text = str(value or "")
    if max_chars is not None:
        text = text[:max_chars]
    return text


def has_text(value: object) -> bool:
    return bool(str(value or "").strip())


def sqlite_readonly_uri(database_path: Path) -> str:
    normalized_path = database_path.resolve().as_posix()
    return f"file:{quote(normalized_path, safe='/:')}?mode=ro"


def round2_input_status(
    *,
    metadata_abstract_present: bool,
    pdf_introduction_present: bool,
    pdf_conclusion_present: bool,
) -> str:
    if (
        metadata_abstract_present
        and pdf_introduction_present
        and pdf_conclusion_present
    ):
        return ROUND2_USABLE_STATUS
    return ROUND2_MISSING_REQUIRED_STATUS


def contamination_warnings_for_section(section_name: str, text: str) -> tuple[str, ...]:
    warnings: list[str] = []
    seen_markers: set[str] = set()
    for match in TERMINAL_SECTION_HEADING_PATTERN.finditer(text):
        marker = "_".join(match.group("marker").lower().split())
        if marker in seen_markers:
            continue
        seen_markers.add(marker)
        warnings.append(f"{section_name}_contains_terminal_heading:{marker}")
    return tuple(warnings)


def contamination_warnings_for_preview(
    *, pdf_introduction: str, pdf_conclusion: str
) -> tuple[str, ...]:
    return (
        contamination_warnings_for_section("pdf_introduction", pdf_introduction)
        + contamination_warnings_for_section("pdf_conclusion", pdf_conclusion)
    )


def missing_fields_for_preview(
    *,
    title_present: bool,
    authors_present: bool,
    categories_present: bool,
    primary_category_present: bool,
    metadata_abstract_present: bool,
    round1_reason_present: bool,
    pdf_introduction_present: bool,
    pdf_conclusion_present: bool,
) -> tuple[str, ...]:
    missing: list[str] = []
    if not title_present:
        missing.append("title")
    if not authors_present:
        missing.append("authors")
    if not categories_present:
        missing.append("categories")
    if not primary_category_present:
        missing.append("primary_category")
    if not metadata_abstract_present:
        missing.append("metadata_abstract")
    if not round1_reason_present:
        missing.append("round1_reason")
    if not pdf_introduction_present:
        missing.append("pdf_introduction")
    if not pdf_conclusion_present:
        missing.append("pdf_conclusion")
    return tuple(missing)


def load_round2_input_previews(
    connection: sqlite3.Connection,
    config: dict[str, object],
    *,
    run_id: int,
    prompt_version: str,
    profile_version: str,
    policy_version: str,
) -> list[Round2InputPreview]:
    max_intro_chars = get_limit(config, "max_intro_chars_per_paper", 6000)
    max_conclusion_chars = get_limit(
        config, "max_conclusion_chars_per_paper", 5000
    )
    rows = connection.execute(
        f"""
        SELECT s.arxiv_id, s.version, s.result_rank, s.score,
               s.confidence, s.reason,
               p.title, p.authors, p.categories, p.primary_category, p.summary,
               COALESCE(ps.introduction_text, '') AS pdf_introduction,
               COALESCE(ps.conclusion_text, '') AS pdf_conclusion,
               COALESCE(ps.extraction_status, 'missing') AS extraction_status,
               ps.failure_reason
        FROM screening_results AS s
        JOIN papers AS p
          ON p.arxiv_id = s.arxiv_id AND p.version = s.version
        LEFT JOIN pdf_sections AS ps
          ON ps.arxiv_id = s.arxiv_id AND ps.version = s.version
        WHERE {round1_selected_exists_sql()}
          AND s.run_id = ?
          AND s.task_type = ?
          AND s.is_selected = 1
          AND s.prompt_version = ?
          AND s.research_profile_version = ?
        ORDER BY s.result_rank ASC, s.score DESC, s.arxiv_id
        """,
        [
            policy_version,
            run_id,
            ROUND1_TASK_TYPE,
            prompt_version,
            profile_version,
        ],
    ).fetchall()

    previews: list[Round2InputPreview] = []
    for row in rows:
        # 第二轮摘要固定使用 arXiv metadata；PDF abstract 不参与输入估算。
        pdf_introduction = clipped_text(row["pdf_introduction"], max_intro_chars)
        pdf_conclusion = clipped_text(row["pdf_conclusion"], max_conclusion_chars)
        title_present = has_text(row["title"])
        authors_present = has_text(row["authors"])
        categories_present = has_text(row["categories"])
        primary_category_present = has_text(row["primary_category"])
        metadata_abstract_present = has_text(row["summary"])
        round1_score_present = row["score"] is not None
        round1_confidence_present = row["confidence"] is not None
        round1_reason_present = has_text(row["reason"])
        pdf_introduction_present = has_text(row["pdf_introduction"])
        pdf_conclusion_present = has_text(row["pdf_conclusion"])
        title_len = clipped_text_length(row["title"])
        authors_len = clipped_text_length(row["authors"])
        categories_len = clipped_text_length(row["categories"])
        primary_category_len = clipped_text_length(row["primary_category"])
        metadata_abstract_len = clipped_text_length(row["summary"])
        round1_reason_len = clipped_text_length(row["reason"])
        pdf_introduction_len = len(pdf_introduction)
        pdf_conclusion_len = len(pdf_conclusion)
        status = round2_input_status(
            metadata_abstract_present=metadata_abstract_present,
            pdf_introduction_present=pdf_introduction_present,
            pdf_conclusion_present=pdf_conclusion_present,
        )
        estimated_chars = (
            title_len
            + authors_len
            + categories_len
            + primary_category_len
            + metadata_abstract_len
            + round1_reason_len
            + pdf_introduction_len
            + pdf_conclusion_len
        )
        missing_fields = missing_fields_for_preview(
            title_present=title_present,
            authors_present=authors_present,
            categories_present=categories_present,
            primary_category_present=primary_category_present,
            metadata_abstract_present=metadata_abstract_present,
            round1_reason_present=round1_reason_present,
            pdf_introduction_present=pdf_introduction_present,
            pdf_conclusion_present=pdf_conclusion_present,
        )
        contamination_warnings = contamination_warnings_for_preview(
            pdf_introduction=pdf_introduction,
            pdf_conclusion=pdf_conclusion,
        )
        previews.append(
            Round2InputPreview(
                arxiv_id=str(row["arxiv_id"]),
                version=int(row["version"]),
                result_rank=int(row["result_rank"] or 0),
                score=(int(row["score"]) if row["score"] is not None else None),
                confidence=(
                    int(row["confidence"])
                    if row["confidence"] is not None
                    else None
                ),
                pdf_extraction_status=str(row["extraction_status"]),
                pdf_failure_reason=row["failure_reason"],
                abstract_source=ROUND2_ABSTRACT_SOURCE,
                round2_input_status=status,
                round1_score_present=round1_score_present,
                round1_confidence_present=round1_confidence_present,
                round1_reason_present=round1_reason_present,
                title_len=title_len,
                authors_len=authors_len,
                categories_len=categories_len,
                primary_category_len=primary_category_len,
                metadata_abstract_len=metadata_abstract_len,
                round1_reason_len=round1_reason_len,
                pdf_introduction_len=pdf_introduction_len,
                pdf_conclusion_len=pdf_conclusion_len,
                estimated_chars=estimated_chars,
                missing_fields=missing_fields,
                contamination_warnings=contamination_warnings,
            )
        )
    return previews


def print_round2_preview(
    *,
    run_selection: Round2RunSelection,
    prompt_version: str,
    profile_version: str,
    policy_version: str,
    previews: list[Round2InputPreview],
    stream: TextIO,
) -> None:
    total_estimated_chars = sum(item.estimated_chars for item in previews)
    print("ROUND2_INPUT_PREVIEW_STATUS=dry_run_ok", file=stream)
    print(f"RUN_ID={run_selection.run_id}", file=stream)
    print(f"RUN_STARTED_AT={run_selection.started_at}", file=stream)
    print(f"ROUND1_PROMPT_VERSION={prompt_version}", file=stream)
    print(f"RESEARCH_PROFILE_VERSION={profile_version}", file=stream)
    print(f"SELECTION_POLICY_VERSION={policy_version}", file=stream)
    print(f"CANDIDATE_COUNT={len(previews)}", file=stream)
    print(f"TOTAL_ESTIMATED_CHARS={total_estimated_chars}", file=stream)
    for item in previews:
        missing = ",".join(item.missing_fields) if item.missing_fields else "none"
        contamination = (
            ",".join(item.contamination_warnings)
            if item.contamination_warnings
            else "none"
        )
        print("---", file=stream)
        print(
            f"rank={item.result_rank} arxiv_id={item.arxiv_id}v{item.version} "
            f"score={item.score} confidence={item.confidence}",
            file=stream,
        )
        print(
            "round2_input "
            f"status={item.round2_input_status} "
            f"abstract_source={item.abstract_source}",
            file=stream,
        )
        print(
            "lengths "
            f"title={item.title_len} authors={item.authors_len} "
            f"categories={item.categories_len} "
            f"primary_category={item.primary_category_len} "
            f"metadata_abstract={item.metadata_abstract_len} "
            f"round1_reason={item.round1_reason_len} "
            f"pdf_introduction={item.pdf_introduction_len} "
            f"pdf_conclusion={item.pdf_conclusion_len} "
            f"estimated={item.estimated_chars}",
            file=stream,
        )
        print(
            "round1_fields "
            f"score_present={item.round1_score_present} "
            f"confidence_present={item.round1_confidence_present} "
            f"reason_present={item.round1_reason_present}",
            file=stream,
        )
        print(
            "pdf_sections "
            f"status={item.pdf_extraction_status} "
            f"failure_reason={item.pdf_failure_reason or 'none'}",
            file=stream,
        )
        print(f"missing_fields={missing}", file=stream)
        print(f"contamination_warnings={contamination}", file=stream)


def run_round2_preview_command(
    project_root: Path,
    *,
    latest: bool,
    target_date: str | None,
    stdout: TextIO = sys.stdout,
) -> list[Round2InputPreview]:
    config = load_config(project_root)
    _, paths = load_runtime_paths(project_root, project_root / "config.json")
    prompt_version, profile_version, policy_version = get_round1_identity(config)
    connection = sqlite3.connect(
        sqlite_readonly_uri(paths["database"]), timeout=10, uri=True
    )
    connection.row_factory = sqlite3.Row
    try:
        validate_required_schema(connection)
        run_selection = select_round1_run(
            connection,
            prompt_version=prompt_version,
            profile_version=profile_version,
            policy_version=policy_version,
            latest=latest,
            target_date=target_date,
        )
        previews = load_round2_input_previews(
            connection,
            config,
            run_id=run_selection.run_id,
            prompt_version=prompt_version,
            profile_version=profile_version,
            policy_version=policy_version,
        )
        print_round2_preview(
            run_selection=run_selection,
            prompt_version=prompt_version,
            profile_version=profile_version,
            policy_version=policy_version,
            previews=previews,
            stream=stdout,
        )
        return previews
    finally:
        connection.close()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="离线预览第二轮输入，不调用模型、不写数据库。"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只读取并预览第二轮输入，不写任何表。",
    )
    selector = parser.add_mutually_exclusive_group(required=True)
    selector.add_argument(
        "--latest",
        action="store_true",
        help="选择最近一个当前 round1 策略下有 selected 结果的 run。",
    )
    selector.add_argument(
        "--date",
        help="选择指定本地日期 YYYY-MM-DD 内最近一个有 selected 结果的 run。",
    )
    return parser.parse_args(argv)


def main(
    argv: list[str] | None = None,
    *,
    project_root: Path = PROJECT_ROOT,
    stdout: TextIO = sys.stdout,
) -> int:
    args = parse_args(argv)
    if not args.dry_run:
        print("ROUND2_INPUT_PREVIEW_STATUS=failed", file=stdout)
        print("ERROR_REASON=dry_run_required", file=stdout)
        return 1
    try:
        run_round2_preview_command(
            project_root,
            latest=bool(args.latest),
            target_date=args.date,
            stdout=stdout,
        )
    except Exception as exc:
        print("ROUND2_INPUT_PREVIEW_STATUS=failed", file=stdout)
        print(f"ERROR_TYPE={type(exc).__name__}", file=stdout)
        print(f"ERROR_REASON={exc}", file=stdout)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
