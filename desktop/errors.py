# Copyright (c) 2026 yyyang20
# SPDX-License-Identifier: GPL-3.0-only
# See LICENSE in the project root for the full license text.

"""Desktop 面向 GUI 与诊断日志的稳定错误及正常结果模型。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping


SYSTEM_SCOPE = "system"
PAPER_SCOPE = "paper"
OUTCOME_SCOPE = "outcome"


@dataclass(frozen=True)
class DesktopIssue:
    code: str
    stage: str
    scope: str
    reason: str
    impact: str
    action: str
    details: Mapping[str, Any] = field(default_factory=dict)
    transient: bool = False
    automatic_retry_permitted: bool = False


@dataclass(frozen=True)
class DesktopOutcome:
    code: str
    stage: str
    summary: str
    details: Mapping[str, Any] = field(default_factory=dict)


class DesktopOperationError(RuntimeError):
    """只携带稳定、安全、可直接显示的 DesktopIssue。"""

    def __init__(self, issue: DesktopIssue) -> None:
        super().__init__(issue.reason)
        self.issue = issue


@dataclass(frozen=True)
class FailureBoundary:
    """按实际影响而不是异常类名判定 PDF/全文失败范围。"""

    paper_boundary_established: bool
    shared_components_ready: bool
    shared_state_intact: bool
    paper_result_persisted: bool
    shared_storage_failure: bool = False
    sqlite_failure: bool = False
    global_state_failure: bool = False


def classify_failure_scope(boundary: FailureBoundary) -> str:
    if (
        boundary.shared_storage_failure
        or boundary.sqlite_failure
        or boundary.global_state_failure
        or not boundary.shared_components_ready
    ):
        return SYSTEM_SCOPE
    if (
        boundary.paper_boundary_established
        and boundary.shared_state_intact
        and boundary.paper_result_persisted
    ):
        return PAPER_SCOPE
    # 无法证明只影响当前论文时失败关闭。
    return SYSTEM_SCOPE


_ISSUE_TEXT: dict[str, tuple[str, str, str, str]] = {
    "AKD-STARTUP-RUNTIME_NOT_WRITABLE": (
        "startup", "运行目录不可写；请将完整程序目录放在可写位置。", "应用无法安全保存本次运行数据。",
        "请把完整程序目录移到当前用户可写位置后重新启动。",
    ),
    "AKD-STARTUP-RESOURCE_INVALID": (
        "startup", "程序资源缺失或校验失败。", "应用已禁用候选与分析操作。",
        "请重新下载并完整解压官方 portable ZIP。",
    ),
    "AKD-KEY-EMPTY": (
        "api_key", "尚未输入 API Key。", "分析没有启动，候选快照未消费。",
        "输入 API Key 并确认已加密保存后重试。",
    ),
    "AKD-KEY-SAVE_FAILED": (
        "api_key", "API Key 无法加密保存。", "分析没有启动，候选快照未消费。",
        "请检查运行目录权限后重新输入。",
    ),
    "AKD-KEY-LOAD_FAILED": (
        "api_key", "已保存的 API Key 无法恢复。", "候选抓取仍可使用，分析需要重新输入 Key。",
        "请重新输入 API Key；不要发送 secret.dat 排查。",
    ),
    "AKD-FETCH-CURL_UNAVAILABLE": (
        "fetch", "curl 不可用或 portable 内置 curl 校验失败。", "本次没有生成候选快照。",
        "源码模式请检查 curl；portable 请重新下载并完整解压。",
    ),
    "AKD-FETCH-CURL_EXECUTION_FAILED": (
        "fetch", "curl 请求执行失败。", "本次没有生成候选快照。",
        "请检查网络连接后重新获取候选。",
    ),
    "AKD-FETCH-DNS_FAILED": (
        "fetch", "arXiv 域名解析失败。", "本次没有生成候选快照。",
        "请检查 DNS 或网络连接后手动重试。",
    ),
    "AKD-FETCH-CONNECTION_FAILED": (
        "fetch", "无法连接 arXiv 服务。", "本次没有生成候选快照。",
        "请检查网络连接后手动重试。",
    ),
    "AKD-FETCH-TLS_FAILED": (
        "fetch", "arXiv TLS 连接或证书校验失败。", "本次没有生成候选快照。",
        "请检查系统时间、网络中间设备或 portable 完整性。",
    ),
    "AKD-FETCH-TIMEOUT": (
        "fetch", "arXiv 请求超时。", "本次没有生成候选快照。",
        "稍后可手动重新获取候选；应用不会自动重试。",
    ),
    "AKD-FETCH-HTTP_RESPONSE_ERROR": (
        "fetch", "arXiv 返回非成功 HTTP 响应。", "本次没有生成候选快照。",
        "请根据诊断日志中的 HTTP 状态稍后重试。",
    ),
    "AKD-FETCH-ATOM_INVALID": (
        "fetch", "arXiv Atom XML 无法安全解析。", "本次没有生成候选快照。",
        "请稍后重新获取；若持续出现，请提供诊断日志。",
    ),
    "AKD-FETCH-UNEXPECTED": (
        "fetch", "候选抓取出现未预见错误。", "本次没有生成候选快照。",
        "请保留诊断日志并重新启动应用。",
    ),
    "AKD-PREPARE-WORKSPACE_FAILED": (
        "prepare", "本地分析工作区初始化失败。", "分析已在模型调用前停止。",
        "请检查 runtime 工作目录、SQLite 文件和是否有其他分析正在运行。",
    ),
    "AKD-R1-REQUEST_FAILED": (
        "round1", "Round 1 模型请求失败。", "分析已停止，当前快照不能重试。",
        "请根据诊断代码检查网络、Key 或服务状态，再重新获取候选。",
    ),
    "AKD-R1-NETWORK_FAILED": (
        "round1", "Round 1 网络请求失败。", "分析已停止，应用不会自动重试。",
        "请检查网络连接后重新获取候选。",
    ),
    "AKD-R1-AUTH_FAILED": (
        "round1", "Round 1 API 认证失败。", "分析已停止，当前快照不能重试。",
        "请核对 API Key 后重新获取候选。",
    ),
    "AKD-R1-TIMEOUT": (
        "round1", "Round 1 请求超时。", "分析已停止，应用不会自动重试。",
        "稍后重新获取候选并重新开始分析。",
    ),
    "AKD-R1-HTTP_RESPONSE_ERROR": (
        "round1", "Round 1 收到非成功 HTTP 响应。", "分析已停止，应用不会自动重试。",
        "请根据日志中的 HTTP 状态检查服务或账户状态。",
    ),
    "AKD-R1-OUTPUT_TRUNCATED": (
        "round1", "Round 1 输出被截断。", "分析已停止，截断内容未作为筛选结果。",
        "请保留诊断日志并重新获取候选。",
    ),
    "AKD-R1-CONTENT_FILTERED": (
        "round1", "Round 1 输出被供应商安全策略终止。", "分析已停止，未形成筛选结果。",
        "请保留诊断日志并检查供应商账户或内容策略。",
    ),
    "AKD-R1-EMPTY_CONTENT": (
        "round1", "Round 1 返回空内容。", "分析已停止，空内容未作为零入围。",
        "请保留诊断日志并重新获取候选。",
    ),
    "AKD-R1-RESPONSE_INCOMPLETE": (
        "round1", "Round 1 响应未正常完成。", "分析已停止，未完成响应未作为结果。",
        "请保留诊断日志并重新获取候选。",
    ),
    "AKD-R1-OUTPUT_INVALID": (
        "round1", "Round 1 输出未通过结构校验。", "分析已停止，未进入 PDF 阶段。",
        "请保留诊断日志并重新获取候选。",
    ),
    "AKD-R1-STORAGE_FAILED": (
        "round1", "Round 1 结果无法写入 SQLite。", "分析已停止，结果未被当作正常零入围。",
        "请检查 runtime 工作目录与磁盘状态。",
    ),
    "AKD-R1-UNEXPECTED": (
        "round1", "Round 1 出现未预见错误。", "分析已停止，当前快照不能重试。",
        "请保留诊断日志并重新获取候选。",
    ),
    "AKD-PDF-PAPER_PROCESSING_FAILED": (
        "pdf", "个别论文 PDF 处理失败。", "仅排除对应论文，其余论文继续处理。",
        "无需立即操作；可在诊断日志中查看聚合原因。",
    ),
    "AKD-PDF-STORAGE_FAILED": (
        "pdf", "PDF 共享存储或状态写入失败。", "整个分析批次已停止。",
        "请检查 runtime/pdfs、runtime/work 与磁盘状态。",
    ),
    "AKD-PDF-UNEXPECTED": (
        "pdf", "PDF 阶段影响范围无法安全确认。", "整个分析批次已失败关闭。",
        "请保留诊断日志并检查本地存储。",
    ),
    "AKD-FULLTEXT-PAPER_EXTRACTION_FAILED": (
        "fulltext", "个别论文全文提取或门控失败。", "仅排除对应论文，其余论文继续处理。",
        "无需立即操作；可在诊断日志中查看聚合原因。",
    ),
    "AKD-FULLTEXT-COMPONENT_FAILED": (
        "fulltext", "全文共享组件、数据库或存储失败。", "整个分析批次已停止。",
        "请检查程序资源和 runtime 工作目录。",
    ),
    "AKD-FULLTEXT-UNEXPECTED": (
        "fulltext", "全文阶段影响范围无法安全确认。", "整个分析批次已失败关闭。",
        "请保留诊断日志并重新启动应用。",
    ),
    "AKD-R2-REQUEST_FAILED": (
        "round2", "Round 2 模型请求失败。", "分析已停止，已有单篇门控结果仍保留。",
        "请根据诊断代码检查网络、Key 或服务状态，再重新获取候选。",
    ),
    "AKD-R2-NETWORK_FAILED": (
        "round2", "Round 2 网络请求失败。", "分析已停止，应用不会自动重试。",
        "请检查网络连接后重新获取候选。",
    ),
    "AKD-R2-AUTH_FAILED": (
        "round2", "Round 2 API 认证失败。", "分析已停止，已有门控结果仍保留。",
        "请核对 API Key 后重新获取候选。",
    ),
    "AKD-R2-TIMEOUT": (
        "round2", "Round 2 请求超时。", "分析已停止，应用不会自动重试。",
        "稍后重新获取候选并重新开始分析。",
    ),
    "AKD-R2-HTTP_RESPONSE_ERROR": (
        "round2", "Round 2 收到非成功 HTTP 响应。", "分析已停止，应用不会自动重试。",
        "请根据日志中的 HTTP 状态检查服务或账户状态。",
    ),
    "AKD-R2-OUTPUT_TRUNCATED": (
        "round2", "Round 2 输出被截断。", "分析已停止，截断内容未作为零推荐。",
        "请保留诊断日志并重新获取候选。",
    ),
    "AKD-R2-CONTENT_FILTERED": (
        "round2", "Round 2 输出被供应商安全策略终止。", "分析已停止，不能解释为零推荐。",
        "请保留诊断日志并检查供应商账户或内容策略。",
    ),
    "AKD-R2-EMPTY_CONTENT": (
        "round2", "Round 2 返回空内容。", "分析已停止，空内容未作为零推荐。",
        "请保留诊断日志并重新获取候选。",
    ),
    "AKD-R2-RESPONSE_INCOMPLETE": (
        "round2", "Round 2 响应未正常完成。", "分析已停止，不能解释为零推荐。",
        "请保留诊断日志并重新获取候选。",
    ),
    "AKD-R2-OUTPUT_INVALID": (
        "round2", "Round 2 输出未通过结构校验。", "分析已停止，不能解释为零推荐。",
        "请保留诊断日志并重新获取候选。",
    ),
    "AKD-R2-UNEXPECTED": (
        "round2", "Round 2失败：出现未预见错误。", "分析已停止，不能解释为零推荐。",
        "请保留诊断日志并重新获取候选。",
    ),
    "AKD-REPORT-GENERATION_FAILED": (
        "report", "日报生成失败。", "分析结果没有形成可展示日报。",
        "请保留诊断日志并检查 SQLite 完整性。",
    ),
    "AKD-DISPLAY-MARKDOWN_RENDER_FAILED": (
        "display", "日报 Markdown 无法在界面渲染。", "分析和 SQLite run 已成功，仅展示失败。",
        "请保留诊断日志；无需重新调用模型。",
    ),
}


def make_issue(
    code: str,
    *,
    scope: str = SYSTEM_SCOPE,
    details: Mapping[str, Any] | None = None,
    transient: bool = False,
    automatic_retry_permitted: bool = False,
) -> DesktopIssue:
    try:
        stage, reason, impact, action = _ISSUE_TEXT[code]
    except KeyError as exc:
        raise ValueError("unknown_desktop_issue_code") from exc
    return DesktopIssue(
        code=code,
        stage=stage,
        scope=scope,
        reason=reason,
        impact=impact,
        action=action,
        details=dict(details or {}),
        transient=transient,
        automatic_retry_permitted=automatic_retry_permitted,
    )


def outcome(code: str, stage: str, summary: str, **details: Any) -> DesktopOutcome:
    if not code.startswith("AKO-"):
        raise ValueError("desktop_outcome_code_invalid")
    return DesktopOutcome(code=code, stage=stage, summary=summary, details=details)
