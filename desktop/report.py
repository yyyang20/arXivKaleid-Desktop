# Copyright (c) 2026 yyyang20
# SPDX-License-Identifier: GPL-3.0-only
# See LICENSE in the project root for the full license text.

from __future__ import annotations

from dataclasses import replace
from html import escape
from pathlib import Path
import sqlite3

import generate_round2_report
import model_usage
import rebuild_daily_report
from desktop.pipeline import CandidateSnapshot


def text(value: object, *, empty: str = "未提供") -> str:
    """元数据按文本呈现，避免标题和理由变成额外 Markdown 链接。"""
    value = escape(str(value or empty), quote=False).replace("\n", " ")
    for char in ("\\", "`", "*", "_", "[", "]", "#", "~", "|"):
        value = value.replace(char, "\\" + char)
    return value


def build_desktop_report(
    connection: sqlite3.Connection, fulltext_database: Path,
    run_id: int, snapshot: CandidateSnapshot,
) -> str:
    """只装载当前工作库事实；不构造正式自动化的发布或调度身份。"""
    round1 = rebuild_daily_report.add_round2_page_gate_state(
        rebuild_daily_report.load_round1_papers(connection, run_id), fulltext_database
    )
    eligible = generate_round2_report.load_eligible_paper_keys(fulltext_database)
    if eligible is None:
        raise RuntimeError("desktop_report_fulltext_missing")
    round2 = generate_round2_report.load_and_validate_round2(
        connection, run_id=run_id, eligible_paper_keys=eligible
    )
    if round2.status not in {"round2_results_valid", "round2_no_candidates"}:
        raise RuntimeError("desktop_report_round2_invalid")
    counts = generate_round2_report.load_fulltext_page_counts(fulltext_database)
    recommendations = []
    for row in round2.recommendations:
        item = dict(row)
        item["page_count"] = counts.get((row["arxiv_id"], row["version"]))
        for field in ("title", "authors", "categories"):
            item[field] = text(item.get(field))
        # 自由评价按纯文本逐行转义，不生成模型提供的可点击链接或 HTML。
        item["evaluation"] = "\n  ".join(text(line, empty="") for line in str(item.get("evaluation") or "").splitlines())
        identity = f"{row['arxiv_id']}v{row['version']}"
        item.update(abs_url=f"https://arxiv.org/abs/{identity}", pdf_url=f"https://arxiv.org/pdf/{identity}")
        recommendations.append(item)
    round2 = replace(round2, recommendations=tuple(recommendations))
    usage = model_usage.load_run_usage_summary(connection, run_id)
    if usage and (not usage.token_complete or not usage.cost_complete):
        raise RuntimeError("desktop_report_usage_incomplete")
    models = model_usage.load_run_model_names(connection, run_id)
    completed = connection.execute(
        "SELECT COUNT(*) FROM screening_completion WHERE run_id = ?", (run_id,)
    ).fetchone()[0]
    if completed != snapshot.round1_count:
        raise RuntimeError("desktop_report_snapshot_count_mismatch")
    lines = [
        "# arXivKaleid Desktop 日报", "",
        f"- 抓取日期（北京时间）：{snapshot.candidate_date.isoformat()}",
        f"- 候选数量：{snapshot.round1_count}",
        f"- Round 1 输入数量：{completed}",
        f"- Round 1 入围数量：{len(round1)}",
        f"- Round 2 输入数量：{len(eligible)}",
        f"- Round 2 最终推荐数量：{len(round2.recommendations)}",
        f"- 实际模型名称：{' / '.join(models) if models else '未调用'}",
        f"- Token 使用量：输入 {usage.known_input_tokens if usage else 0} / 输出 {usage.known_output_tokens if usage else 0} / 合计 {usage.known_total_tokens if usage else 0}",
        f"- 实际费用：¥{format(usage.known_cost, 'f') if usage else '0'}", "",
    ]
    if not eligible:
        lines.extend(["## Round 2 最终推荐", "", "没有合格全文输入，未调用 Round 2。", ""])
    else:
        lines.append(generate_round2_report.build_round2_section(
            round2, link_mode=generate_round2_report.REPORT_LINK_MODE_CLOUD
        ))
    lines.extend(["## Round 1 入围论文", ""])
    if not round1:
        lines.extend(["本批没有入围论文。", ""])
    statuses = {
        "full_text": "完整全文通过门控",
        "long_reading": "超过 60 页，未进入 Round 2",
        "fulltext_extraction_failed": "PDF 下载、校验或全文提取失败，未进入 Round 2",
        "token_budget_excluded": "Token 预算超限，未进入 Round 2",
    }
    for row in round1:
        identity = f"{row['arxiv_id']}v{row['version']}"
        lines.extend([
            f"### Rank {row['result_rank']}：{text(row['title'])}", "",
            f"- arXiv ID：{identity}", f"- 作者：{text(row['authors'])}",
            f"- 分类：{text(row['categories'])}",
            *(["- Round 1 评价：" + "\n  ".join(text(line, empty="") for line in row['evaluation'].splitlines())] if row['evaluation'] else []),
            f"- PDF 实际页数：{row.get('page_count') or '未获取'}",
            f"- Round 2 门控状态：{statuses.get(row.get('round2_decision'), '未通过门控')}",
            f"- arXiv 摘要页：[打开摘要页](https://arxiv.org/abs/{identity})",
            f"- 在线 PDF：[打开 PDF](https://arxiv.org/pdf/{identity})",
            f"- 本地 PDF：{text(row.get('local_pdf_path'))}（{text(row['download_status'])}）", "",
        ])
    return "\n".join(lines)
