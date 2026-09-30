from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TextIO

import build_round2_inputs
import content_labels
import daily_report_template
import selection_nature


PROJECT_ROOT = Path(__file__).resolve().parent
ROUND1_TASK_TYPE = "round1_abstract_screening"
ROUND2_TASK_TYPE = "round2_batch_ranking"
LEVEL_BUDGETS = {"deep_read": 1, "skim_read": 3, "backup": 1}
# 当前正式日报和独立第二轮报告均不再生成反馈区块。
DAILY_REPORT_TEMPLATE_VERSION = daily_report_template.DAILY_REPORT_TEMPLATE_VERSION
ROUND2_REPORT_TEMPLATE_VERSION = "round2_report_v9_submission_dates"
COMPACT_DAILY_REPORT_TEMPLATE_VERSIONS = (
    "daily_report_v11_compact",
    "daily_report_v12_submission_dates",
    daily_report_template.LEGACY_TIMING_REPORT_TEMPLATE_VERSION,
    DAILY_REPORT_TEMPLATE_VERSION,
)
RECOMMENDATION_LEVEL_DISPLAY = {
    "deep_read": "Deep Read",
    "skim_read": "Skim Read",
    "backup": "Backup",
}
REPORT_LINK_MODE_LOCAL = "local"
REPORT_LINK_MODE_CLOUD = "cloud"
REPORT_LINK_MODES = (REPORT_LINK_MODE_LOCAL, REPORT_LINK_MODE_CLOUD)
EVIDENCE_SOURCE_LABELS = {
    "pdf_full_text": "PDF 全文",
    "metadata_abstract": "arXiv 摘要",
    "pdf_introduction": "PDF 引言",
    "pdf_conclusion": "PDF 结论",
}
# 仅供旧日报导入器兼容历史文件，当前渲染器不得使用。
FEEDBACK_BLOCK_START = "<!-- ARXIVKALEID_FEEDBACK_START"
FEEDBACK_BLOCK_END = "<!-- ARXIVKALEID_FEEDBACK_END -->"
ABSTRACT_HEADING = "**摘要全文**"
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


def recommendation_level_display(value: object) -> str:
    try:
        return RECOMMENDATION_LEVEL_DISPLAY[str(value)]
    except KeyError as exc:
        raise RuntimeError("report_recommendation_level_invalid") from exc


def prompt_requires_structured_evidence(prompt_version: object) -> bool:
    """仅历史 round2_v5/v6 日报要求已保存的结构化证据。"""
    match = re.fullmatch(r"round2_v(\d+)", str(prompt_version or "").strip())
    return bool(match and int(match.group(1)) in {5, 6})


def valid_persisted_evidence(value: object) -> bool:
    """检查入库证据结构；原文页码匹配已在模型结果保存前完成。"""
    if not isinstance(value, list) or not value:
        return False
    for item in value:
        if not isinstance(item, dict) or set(item) != {
            "source",
            "page_number",
            "quote",
        }:
            return False
        source = item.get("source")
        page_number = item.get("page_number")
        quote = item.get("quote")
        if source not in EVIDENCE_SOURCE_LABELS:
            return False
        if not isinstance(quote, str) or not quote.strip():
            return False
        if source == "pdf_full_text":
            if (
                isinstance(page_number, bool)
                or not isinstance(page_number, int)
                or page_number <= 0
            ):
                return False
        elif page_number is not None:
            return False
    return True


def load_database_and_daily_reports_dir(project_root: Path) -> tuple[Path, Path]:
    """读取数据库和正式日报目录，供所有只读报告入口共用。"""
    try:
        config = json.loads(
            (project_root / "config.json").read_text(encoding="utf-8-sig")
        )
        paths = config["paths"]
        database = (project_root / paths["database"]).resolve()
        daily_reports = (project_root / paths["reports_dir"]).resolve()
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError("cannot_read_report_configuration") from exc
    return database, daily_reports


def load_paths(project_root: Path) -> tuple[Path, Path]:
    """兼容独立第二轮报告入口，继续返回其专用输出目录。"""
    database, daily_reports = load_database_and_daily_reports_dir(project_root)
    return database, daily_reports.parent / "round2"


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


def select_run(
    connection: sqlite3.Connection, *, run_id: int | None, latest: bool
) -> sqlite3.Row:
    if latest == (run_id is not None):
        raise RuntimeError("choose_exactly_one_of_run_id_or_latest")
    if latest:
        row = connection.execute(
            """
            SELECT run_id, started_at, status
            FROM runs
            WHERE status = 'success'
            ORDER BY run_id DESC
            LIMIT 1
            """
        ).fetchone()
    else:
        row = connection.execute(
            "SELECT run_id, started_at, status FROM runs WHERE run_id = ?",
            (run_id,),
        ).fetchone()
    if row is None:
        raise RuntimeError("report_run_not_found")
    return row


def load_and_validate_round2(
    connection: sqlite3.Connection,
    *,
    run_id: int | None,
    latest: bool,
    eligible_paper_keys: set[tuple[str, int]] | None = None,
) -> Round2ReportData:
    validate_schema(connection)
    run = select_run(connection, run_id=run_id, latest=latest)
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
    round1_ranks = {
        (str(row["arxiv_id"]), int(row["version"])): int(row["result_rank"])
        for row in selected_rows
    }
    round1_labels: dict[tuple[str, int], str | None] = {}
    for row in connection.execute(
        """
        SELECT arxiv_id, version, prompt_version, details_json
        FROM screening_results
        WHERE run_id = ? AND task_type = ? AND is_selected = 1
        """,
        (run["run_id"], ROUND1_TASK_TYPE),
    ).fetchall():
        key = (str(row["arxiv_id"]), int(row["version"]))
        round1_labels[key] = content_labels.content_label_from_details_json(
            row["details_json"], prompt_version=row["prompt_version"]
        )
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
            audit_prompt_version in {"round2_v14", "round2_v15"}
            and isinstance(audit, dict)
            and audit.get("accepted_count") == 0
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
    uses_core_content_label = (
        len(prompt_versions) == 1
        and content_labels.round2_prompt_uses_core_content_label(
            next(iter(prompt_versions))
        )
    )
    uses_tolerant_contract = bool(prompt_versions) and prompt_versions.issubset(
        {"round2_v14", "round2_v15"}
    )
    expected_count = min(5, len(candidate_ids))
    if not uses_tolerant_contract and len(rows) != expected_count:
        reasons.append(
            f"第二轮结果数量应为 {expected_count}，实际为 {len(rows)}。"
        )
    ranks = [row["result_rank"] for row in rows]
    if ranks != list(range(1, len(rows) + 1)):
        reasons.append("final rank 不是从 1 开始的连续整数。")
    scores = [row["score"] for row in rows]
    if not uses_tolerant_contract and any(
        isinstance(score, bool)
        or not isinstance(score, int)
        or not 0 <= score <= 100
        for score in scores
    ):
        reasons.append("score 不是 0 至 100 的合法整数。")
    elif (
        not uses_tolerant_contract
        and not uses_core_content_label
        and scores != sorted(scores, reverse=True)
    ):
        reasons.append("历史第二轮 score 不是合法的降序整数序列。")

    if uses_tolerant_contract and (
        not isinstance(audit, dict) or audit.get("accepted_count") != len(rows)
    ):
        reasons.append("查准率优先契约缺少与有效推荐数量一致的选择审计。")

    level_counts = {level: 0 for level in LEVEL_BUDGETS}
    for row in rows:
        key = (str(row["arxiv_id"]), int(row["version"]))
        if key not in candidate_ids:
            reasons.append(
                f"{key[0]}v{key[1]} 不属于该 run 通过页数门控的第二轮候选集合。"
            )
        level = str(row["recommendation_level"] or "")
        if uses_core_content_label:
            expected_level = (
                "deep_read"
                if int(row["result_rank"]) == 1
                else "skim_read"
                if 2 <= int(row["result_rank"]) <= 4
                else "backup"
            )
            if level != expected_level:
                reasons.append(
                    f"Rank {row['result_rank']} 的确定性推荐级别应为 {expected_level}。"
                )
        elif level not in level_counts:
            reasons.append(f"存在非法 recommendation_level：{level or 'empty'}。")
        else:
            level_counts[level] += 1
    if not uses_core_content_label:
        for level, count in level_counts.items():
            if count > LEVEL_BUDGETS[level]:
                reasons.append(f"{level} 数量 {count} 超过预算 {LEVEL_BUDGETS[level]}。")

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
        item["suggested_reading_scope"] = str(
            details.get("suggested_reading_scope") or ""
        )
        model_position = details.get("model_position")
        item["model_position"] = (
            model_position
            if isinstance(model_position, int) and not isinstance(model_position, bool)
            else None
        )
        item["submission_date_utc"] = submission_date_utc(item.get("updated"))
        key = (str(item["arxiv_id"]), int(item["version"]))
        round1_result_rank = round1_ranks.get(key)
        if round1_result_rank is None or round1_result_rank < 1:
            reasons.append(
                f"{item['arxiv_id']}v{item['version']} 无法连接对应的 Round 1 Rank。"
            )
        item["round1_result_rank"] = round1_result_rank
        if key not in round1_labels:
            reasons.append(
                f"{item['arxiv_id']}v{item['version']} 无法连接对应的第一轮内容标签。"
            )
        if uses_core_content_label:
            try:
                item["content_label"] = (
                    content_labels.round2_content_label_from_details_json(
                        item.get("details_json"),
                        prompt_version=item.get("prompt_version"),
                    )
                )
            except RuntimeError:
                reasons.append(
                    f"{item['arxiv_id']}v{item['version']} 缺少合法的第二轮全文标签。"
                )
        else:
            item["content_label"] = round1_labels.get(key)
        if selection_nature.round2_prompt_requires_contribution_tier(
            item.get("prompt_version")
        ):
            item["contribution_tier"] = details.get("contribution_tier")
            try:
                item["selection_nature"] = (
                    selection_nature.round2_selection_nature_from_tier(
                        item["contribution_tier"],
                        prompt_version=item.get("prompt_version"),
                    )
                )
            except RuntimeError:
                reasons.append(
                    f"{item['arxiv_id']}v{item['version']} 缺少合法的第二轮贡献层级。"
                )
        item["evidence"] = details.get("evidence", [])
        if prompt_requires_structured_evidence(item.get("prompt_version")) and not (
            valid_persisted_evidence(item["evidence"])
        ):
            reasons.append(
                f"{item['arxiv_id']}v{item['version']} 缺少合法的结构化证据。"
            )
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


def repair_report_abstract_markdown(report_text: str) -> tuple[str, int]:
    """只修复日报摘要引用块，返回修复文本与发生变化的行数。"""
    lines = report_text.splitlines(keepends=True)
    in_abstract = False
    changed_lines = 0
    repaired: list[str] = []
    for line in lines:
        stripped = line.rstrip("\r\n")
        if stripped == ABSTRACT_HEADING:
            in_abstract = True
            repaired.append(line)
            continue
        if in_abstract and stripped == "---":
            in_abstract = False
            repaired.append(line)
            continue
        if in_abstract and stripped.startswith(">"):
            rendered = render_abstract_markdown_text(line)
            if rendered != line:
                changed_lines += 1
            repaired.append(rendered)
            continue
        repaired.append(line)
    return "".join(repaired), changed_lines


def unsafe_abstract_tilde_count(report_text: str) -> int:
    """统计摘要引用块中仍可能触发 GitHub 删除线的裸波浪号。"""
    count = 0
    in_abstract = False
    for line in report_text.splitlines():
        if line == ABSTRACT_HEADING:
            in_abstract = True
            continue
        if in_abstract and line == "---":
            in_abstract = False
            continue
        if in_abstract and line.startswith(">"):
            count += len(UNESCAPED_TILDE_PATTERN.findall(line))
    return count


def unsafe_abstract_backtick_count(report_text: str) -> int:
    """统计摘要引用块中仍可能触发 GitHub 代码段的原始反引号。"""
    count = 0
    in_abstract = False
    for line in report_text.splitlines():
        if line == ABSTRACT_HEADING:
            in_abstract = True
            continue
        if in_abstract and line == "---":
            in_abstract = False
            continue
        if in_abstract and line.startswith(">"):
            count += line.count("`")
    return count


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
        round1_result_rank = row.get("round1_result_rank")
        round1_rank_text = (
            f"Rank {round1_result_rank}"
            if isinstance(round1_result_rank, int)
            and not isinstance(round1_result_rank, bool)
            and round1_result_rank > 0
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
                *(
                    [f"- 内容标签：{row['content_label']}"]
                    if row.get("content_label")
                    else []
                ),
                f"- PDF 实际页数：{page_count_text}",
                f"- Round 1 原始位置：{round1_rank_text}",
                "- 推荐级别："
                f"{recommendation_level_display(row['recommendation_level'])}",
                f"- Round 2 理由：{row['reason'] or '未提供'}",
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


def build_report_text(
    data: Round2ReportData,
    generated_at: datetime,
    *,
    link_mode: str = REPORT_LINK_MODE_LOCAL,
) -> str:
    validate_report_link_mode(link_mode)
    lines = [
        "# arXiv Round 2 最终推荐",
        "",
        f"报告生成时间：{generated_at.strftime('%Y-%m-%d %H:%M')}",
        "",
        "<!-- ARXIVKALEID_REPORT_META",
        f"report_run_id: {data.run_id}",
        f"report_generated_at: {generated_at.isoformat(timespec='seconds')}",
        f"report_template_version: {ROUND2_REPORT_TEMPLATE_VERSION}",
        "-->",
        f"运行开始时间：{data.started_at}",
        f"主流程状态：{data.run_status}",
        "",
        build_round2_section(data, link_mode=link_mode),
    ]
    return "\n".join(lines)


def write_new_report(
    reports_dir: Path,
    data: Round2ReportData,
    *,
    link_mode: str = REPORT_LINK_MODE_LOCAL,
) -> tuple[Path, datetime]:
    validate_report_link_mode(link_mode)
    reports_dir.mkdir(parents=True, exist_ok=True)
    generated_at = datetime.now().astimezone()
    stem = generated_at.strftime(f"round2_run-{data.run_id}_%Y-%m-%d_%H%M%S")
    for suffix in range(100):
        name = f"{stem}.md" if suffix == 0 else f"{stem}_{suffix}.md"
        path = reports_dir / name
        try:
            with path.open("x", encoding="utf-8", newline="\n") as file:
                file.write(
                    build_report_text(data, generated_at, link_mode=link_mode)
                )
                file.write("\n")
            return path, generated_at
        except FileExistsError:
            continue
        except OSError as exc:
            raise RuntimeError("cannot_write_round2_report") from exc
    raise RuntimeError("cannot_allocate_unique_round2_report_name")


def generate_report_command(
    project_root: Path,
    *,
    run_id: int | None,
    latest: bool,
    link_mode: str = REPORT_LINK_MODE_LOCAL,
    stdout: TextIO = sys.stdout,
) -> int:
    try:
        database_path, reports_dir = load_paths(project_root)
        connection = open_readonly_database(database_path)
        try:
            fulltext_database = database_path.with_name("round2_inputs.sqlite")
            eligible_paper_keys = load_eligible_paper_keys(fulltext_database)
            data = load_and_validate_round2(
                connection,
                run_id=run_id,
                latest=latest,
                eligible_paper_keys=eligible_paper_keys,
            )
            page_counts = load_fulltext_page_counts(fulltext_database)
            if page_counts:
                data = replace(
                    data,
                    recommendations=tuple(
                        {
                            **row,
                            "page_count": page_counts.get(
                                (str(row["arxiv_id"]), int(row["version"]))
                            ),
                        }
                        for row in data.recommendations
                    ),
                )
        finally:
            connection.close()
        report_path, _ = write_new_report(
            reports_dir, data, link_mode=link_mode
        )
    except Exception as exc:
        print("ROUND2_REPORT_STATUS=failed", file=stdout)
        print(f"ERROR_TYPE={type(exc).__name__}", file=stdout)
        print(f"ERROR_REASON={exc}", file=stdout)
        return 1
    print(f"ROUND2_REPORT_STATUS={data.status}", file=stdout)
    print(f"RUN_ID={data.run_id}", file=stdout)
    print(f"REPORT_PATH={report_path.relative_to(project_root)}", file=stdout)
    return 0


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
        # 旧 artifact 没有决策表，仅供历史报告回读。
        return {
            (str(row[0]), int(row[1]))
            for row in connection.execute(
                """
                SELECT arxiv_id, version
                FROM pdf_fulltext_documents
                WHERE page_gate_status = ?
                """,
                (round2_fulltext_state.PAGE_GATE_ELIGIBLE,),
            ).fetchall()
        }
    finally:
        connection.close()


def load_fulltext_page_counts(
    fulltext_database_path: Path,
) -> dict[tuple[str, int], int]:
    """读取独立全文状态库中的实际物理页数；缺少旧状态库时返回空映射。"""
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


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="从 SQLite 已保存结果生成独立第二轮日报；不调用模型。"
    )
    selector = parser.add_mutually_exclusive_group(required=True)
    selector.add_argument("--run-id", type=int, help="指定主流程 RUN_ID。")
    selector.add_argument("--latest", action="store_true", help="选择最新成功 run。")
    parser.add_argument(
        "--link-mode",
        choices=REPORT_LINK_MODES,
        default=REPORT_LINK_MODE_LOCAL,
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
    return generate_report_command(
        project_root,
        run_id=args.run_id,
        latest=bool(args.latest),
        link_mode=args.link_mode,
        stdout=stdout,
    )


if __name__ == "__main__":
    raise SystemExit(main_cli())
