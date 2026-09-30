from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo


DAILY_REPORT_TEMPLATE_VERSION = "daily_report_v14_round2_label_summary"
LEGACY_TIMING_REPORT_TEMPLATE_VERSION = "daily_report_v13_timing_context"
REPORT_MODE_AUTOMATIC = "automatic"
REPORT_MODE_MANUAL = "manual_backfill"
REPORT_MODE_CONTROLLED = "controlled_replay"
REPORT_MODES = {
    REPORT_MODE_AUTOMATIC,
    REPORT_MODE_MANUAL,
    REPORT_MODE_CONTROLLED,
}
POSITIVE_ROUND2_LABELS = ("成像", "新解", "成像｜新解")
SHANGHAI_TIMEZONE = ZoneInfo("Asia/Shanghai")


@dataclass(frozen=True)
class StageCounts:
    """日报展示用的单阶段计数，不承载筛选业务逻辑。"""

    input_count: int
    model_output_count: int
    valid_count: int

    def validate(self) -> None:
        values = (self.input_count, self.model_output_count, self.valid_count)
        if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in values):
            raise RuntimeError("daily_report_stage_counts_invalid")
        if self.valid_count > self.input_count:
            raise RuntimeError("daily_report_stage_counts_invalid")


@dataclass(frozen=True)
class Round2LabelCounts:
    """最终有效推荐的三类标签，以及未进入最终推荐的候选数量。"""

    imaging: int
    new_solution: int
    imaging_new_solution: int
    not_recommended: int

    def validate(self, *, round2: StageCounts) -> None:
        values = (
            self.imaging,
            self.new_solution,
            self.imaging_new_solution,
            self.not_recommended,
        )
        if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in values):
            raise RuntimeError("daily_report_round2_label_counts_invalid")
        if self.imaging + self.new_solution + self.imaging_new_solution != round2.valid_count:
            raise RuntimeError("daily_report_round2_label_counts_invalid")
        if sum(values) != round2.input_count:
            raise RuntimeError("daily_report_round2_label_counts_invalid")

    def as_metadata_json(self) -> str:
        return json.dumps(
            {
                "imaging": self.imaging,
                "new_solution": self.new_solution,
                "imaging_new_solution": self.imaging_new_solution,
                "not_recommended": self.not_recommended,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )


@dataclass(frozen=True)
class DailyReportHeaderView:
    """稳定数据层交给可见模板层的完整运行摘要。"""

    report_run_id: int
    report_generated_at: datetime
    program_runtime_seconds: int
    main_runtime_seconds: int
    round2_runtime_seconds: int
    report_runtime_seconds: int
    report_mode: str
    report_date: str
    data_cutoff_at: str
    program_started_at: str
    schedule_delay_seconds: int | None
    source_run_id: int | None
    source_batch_id: int | None
    submission_date_range: str
    update_count: int
    model_display: str
    token_text: str
    cost_text: str
    anomaly_text: str
    round1: StageCounts
    round2: StageCounts
    round2_labels: Round2LabelCounts


def _parse_timestamp(value: str, *, code: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise RuntimeError(code) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise RuntimeError(code)
    return parsed.astimezone(timezone.utc)


def _format_timestamp(value: datetime) -> str:
    return value.astimezone(SHANGHAI_TIMEZONE).strftime("%Y-%m-%d %H:%M:%S")


def _format_runtime(seconds: int) -> str:
    if isinstance(seconds, bool) or not isinstance(seconds, int) or seconds < 0:
        raise RuntimeError("daily_report_runtime_invalid")
    hours, remainder = divmod(seconds, 3600)
    minutes, remaining_seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}小时{minutes}分{remaining_seconds}秒"
    return f"{minutes}分{remaining_seconds}秒"


def _mode_display(report_mode: str) -> str:
    displays = {
        REPORT_MODE_AUTOMATIC: "自动",
        REPORT_MODE_MANUAL: "手动回查",
        REPORT_MODE_CONTROLLED: "受控重做",
    }
    try:
        return displays[report_mode]
    except KeyError as exc:
        raise RuntimeError("daily_report_mode_invalid") from exc


def build_round2_label_counts(
    recommendations: Sequence[Mapping[str, Any]], *, round2_input: int
) -> Round2LabelCounts:
    """只统计最终有效 Round 2 推荐；“其他”或旧标签不得伪装成新版统计。"""
    if isinstance(round2_input, bool) or not isinstance(round2_input, int) or round2_input < 0:
        raise RuntimeError("daily_report_round2_input_invalid")
    counts = {label: 0 for label in POSITIVE_ROUND2_LABELS}
    for row in recommendations:
        label = row.get("content_label")
        if label not in counts:
            raise RuntimeError("daily_report_round2_content_label_invalid")
        counts[str(label)] += 1
    if len(recommendations) > round2_input:
        raise RuntimeError("daily_report_round2_label_counts_invalid")
    result = Round2LabelCounts(
        imaging=counts["成像"],
        new_solution=counts["新解"],
        imaging_new_solution=counts["成像｜新解"],
        not_recommended=round2_input - len(recommendations),
    )
    result.validate(
        round2=StageCounts(round2_input, len(recommendations), len(recommendations))
    )
    return result


def parse_round2_label_counts_metadata(value: str) -> Round2LabelCounts:
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError("daily_report_round2_label_metadata_invalid") from exc
    expected_keys = {
        "imaging",
        "new_solution",
        "imaging_new_solution",
        "not_recommended",
    }
    if not isinstance(parsed, dict) or set(parsed) != expected_keys:
        raise RuntimeError("daily_report_round2_label_metadata_invalid")
    return Round2LabelCounts(
        imaging=parsed["imaging"],
        new_solution=parsed["new_solution"],
        imaging_new_solution=parsed["imaging_new_solution"],
        not_recommended=parsed["not_recommended"],
    )


def _validate_header_view(view: DailyReportHeaderView) -> tuple[datetime, datetime]:
    if isinstance(view.report_run_id, bool) or not isinstance(view.report_run_id, int) or view.report_run_id < 0:
        raise RuntimeError("daily_report_run_id_invalid")
    if view.report_mode not in REPORT_MODES:
        raise RuntimeError("daily_report_mode_invalid")
    if view.report_generated_at.tzinfo is None or view.report_generated_at.utcoffset() is None:
        raise RuntimeError("daily_report_generated_at_invalid")
    integer_values = (
        view.program_runtime_seconds,
        view.main_runtime_seconds,
        view.round2_runtime_seconds,
        view.report_runtime_seconds,
        view.update_count,
    )
    if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in integer_values):
        raise RuntimeError("daily_report_runtime_invalid")
    view.round1.validate()
    view.round2.validate()
    cutoff = _parse_timestamp(view.data_cutoff_at, code="daily_report_data_cutoff_invalid")
    started = _parse_timestamp(
        view.program_started_at, code="daily_report_program_started_at_invalid"
    )
    if view.report_generated_at.astimezone(timezone.utc) < started:
        raise RuntimeError("daily_report_timeline_invalid")
    if view.report_mode == REPORT_MODE_AUTOMATIC:
        if (
            isinstance(view.schedule_delay_seconds, bool)
            or not isinstance(view.schedule_delay_seconds, int)
            or view.schedule_delay_seconds < 0
        ):
            raise RuntimeError("daily_report_schedule_delay_invalid")
    elif view.schedule_delay_seconds is not None:
        raise RuntimeError("daily_report_schedule_delay_invalid")
    if view.report_mode == REPORT_MODE_CONTROLLED:
        if (
            isinstance(view.source_run_id, bool)
            or not isinstance(view.source_run_id, int)
            or view.source_run_id < 1
            or isinstance(view.source_batch_id, bool)
            or not isinstance(view.source_batch_id, int)
            or view.source_batch_id < 1
        ):
            raise RuntimeError("daily_report_source_invalid")
    elif view.source_run_id is not None or view.source_batch_id is not None:
        raise RuntimeError("daily_report_source_invalid")
    return cutoff, started


def render_daily_report_header(
    view: DailyReportHeaderView,
    *,
    template_version: str = DAILY_REPORT_TEMPLATE_VERSION,
) -> list[str]:
    """集中渲染隐藏元数据和“本次运行”，业务层不再拼接 Markdown。"""
    if template_version not in {
        DAILY_REPORT_TEMPLATE_VERSION,
        LEGACY_TIMING_REPORT_TEMPLATE_VERSION,
    }:
        raise RuntimeError("daily_report_template_version_invalid")
    cutoff, _started = _validate_header_view(view)
    if template_version == DAILY_REPORT_TEMPLATE_VERSION:
        view.round2_labels.validate(round2=view.round2)
    generated = view.report_generated_at.astimezone(timezone.utc)
    lines = [
        "# arXiv 每日论文推荐",
        "",
        "<!-- ARXIVKALEID_REPORT_META",
        f"report_run_id: {view.report_run_id}",
        "report_generated_at: "
        + view.report_generated_at.astimezone(SHANGHAI_TIMEZONE).isoformat(
            timespec="seconds"
        ),
        f"program_runtime_seconds: {view.program_runtime_seconds}",
        f"main_runtime_seconds: {view.main_runtime_seconds}",
        f"round2_runtime_seconds: {view.round2_runtime_seconds}",
        f"report_runtime_seconds: {view.report_runtime_seconds}",
        f"report_mode: {view.report_mode}",
        f"report_date: {view.report_date}",
        f"data_cutoff_at: {view.data_cutoff_at}",
        f"program_started_at: {view.program_started_at}",
        "schedule_delay_seconds: "
        + (
            str(view.schedule_delay_seconds)
            if view.schedule_delay_seconds is not None
            else "null"
        ),
        *(
            [
                f"source_run_id: {view.source_run_id}",
                f"source_batch_id: {view.source_batch_id}",
            ]
            if view.report_mode == REPORT_MODE_CONTROLLED
            else []
        ),
        *(
            [f"round2_label_counts: {view.round2_labels.as_metadata_json()}"]
            if template_version == DAILY_REPORT_TEMPLATE_VERSION
            else []
        ),
        f"report_template_version: {template_version}",
        "-->",
        "",
        "## 本次运行",
        "",
        f"- 运行模式：{_mode_display(view.report_mode)}",
        f"- 数据截止时间（UTC+8）：{_format_timestamp(cutoff)}",
        f"- 实际生成时间（UTC+8）：{_format_timestamp(generated)}",
        *(
            [f"- 调度延迟：{_format_runtime(view.schedule_delay_seconds)}"]
            if view.report_mode == REPORT_MODE_AUTOMATIC
            else []
        ),
        f"- 程序耗时：{_format_runtime(view.program_runtime_seconds)}",
        *(
            [
                f"- 来源：原 Run {view.source_run_id} / "
                f"Batch {view.source_batch_id}"
            ]
            if view.report_mode == REPORT_MODE_CONTROLLED
            else []
        ),
        f"- 提交日期范围（UTC）：{view.submission_date_range}",
        (
            f"- 本期候选：{view.update_count} 篇（恢复原批次候选集合）"
            if view.report_mode == REPORT_MODE_CONTROLLED
            else f"- 本期更新：{view.update_count} 篇（跨分类去重）"
        ),
        f"- 模型：{view.model_display}；Token：{view.token_text}；费用：{view.cost_text}",
        f"- 异常：{view.anomaly_text}",
        "- Round 1：输入 "
        f"{view.round1.input_count}；模型输出 {view.round1.model_output_count}；"
        f"有效结果 {view.round1.valid_count}",
    ]
    round2_line = (
        "- Round 2：输入 "
        f"{view.round2.input_count}；模型输出 {view.round2.model_output_count}；"
        f"有效结果 {view.round2.valid_count}"
    )
    if template_version == DAILY_REPORT_TEMPLATE_VERSION:
        lines.append(
            round2_line
            + "<br>标签："
            + f"成像 {view.round2_labels.imaging}；"
            + f"新解 {view.round2_labels.new_solution}；"
            + f"成像｜新解 {view.round2_labels.imaging_new_solution}；"
            + f"未推荐 {view.round2_labels.not_recommended}"
        )
    else:
        lines.append(round2_line)
    lines.append("")
    return lines


def parse_v14_stage_summary(
    report_text: str,
) -> tuple[StageCounts, StageCounts, Round2LabelCounts]:
    """解析并校验 v14 唯一的阶段摘要，供正式发布器复用。"""
    round1_matches = re.findall(
        r"(?m)^- Round 1：输入 (\d+)；模型输出 (\d+)；有效结果 (\d+)\s*$",
        report_text,
    )
    round2_matches = re.findall(
        r"(?m)^- Round 2：输入 (\d+)；模型输出 (\d+)；有效结果 (\d+)"
        r"<br>标签：成像 (\d+)；新解 (\d+)；成像｜新解 (\d+)；"
        r"未推荐 (\d+)\s*$",
        report_text,
    )
    metadata_matches = re.findall(
        r"(?m)^round2_label_counts:\s*(\{[^\r\n]+\})\s*$", report_text
    )
    if len(round1_matches) != 1 or len(round2_matches) != 1 or len(metadata_matches) != 1:
        raise RuntimeError("daily_report_v14_summary_invalid")
    round1 = StageCounts(*(int(value) for value in round1_matches[0]))
    round2 = StageCounts(*(int(value) for value in round2_matches[0][:3]))
    visible = Round2LabelCounts(*(int(value) for value in round2_matches[0][3:]))
    metadata = parse_round2_label_counts_metadata(metadata_matches[0])
    try:
        round1.validate()
        round2.validate()
        visible.validate(round2=round2)
        metadata.validate(round2=round2)
    except RuntimeError as exc:
        raise RuntimeError("daily_report_v14_summary_invalid") from exc
    if visible != metadata:
        raise RuntimeError("daily_report_v14_summary_invalid")
    return round1, round2, visible


def render_audit_details(lines: Iterable[str]) -> str:
    """统一折叠审计区外壳，调用方只提供已经验证的内容行。"""
    return "\n".join(
        [
            "<details>",
            "<summary>自动化审计信息</summary>",
            "",
            *list(lines),
            "",
            "</details>",
        ]
    )
