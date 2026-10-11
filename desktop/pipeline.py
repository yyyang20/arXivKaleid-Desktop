# Copyright (c) 2026 yyyang20
# SPDX-License-Identifier: GPL-3.0-only
# See LICENSE in the project root for the full license text.

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from types import MappingProxyType
from _thread import LockType
from threading import Lock
from typing import Mapping
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo
from desktop import paths
from desktop.diagnostics import DesktopDiagnostics, StageTimer, new_id
from desktop.errors import (
    DesktopOperationError,
    DesktopOutcome,
    make_issue,
    outcome,
)
from desktop.progress import ProgressCallback, ProgressEvent, emit_progress
from desktop.version_metadata import FETCH_POLICY, MetadataError, VersionStore
from desktop import arxiv_versions


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CATEGORIES = ("gr-qc", "astro-ph.HE", "astro-ph.GA")
ATOM_TIMEOUT_SECONDS = 90
SUBPROCESS_TIMEOUT_MARGIN_SECONDS = 15


class CandidateError(DesktopOperationError):
    """兼容旧调用名；实际错误由 DesktopIssue 表达。"""


def runtime_path(*parts: str) -> Path:
    """解析后再校验，拒绝通过绝对路径、.. 或目录链接逃出 runtime。"""
    return paths.runtime_path(PROJECT_ROOT, *parts)


@dataclass(frozen=True)
class CandidateSnapshot:
    candidate_date: date
    completed_at: datetime
    raw_count: int
    unique_count: int
    papers: tuple[Mapping[str, str | int], ...]
    snapshot_id: str = field(default_factory=new_id)
    fetch_operation_id: str = ""
    categories: tuple[str, ...] = CATEGORIES
    fetch_policy: str = FETCH_POLICY
    page_count: int | None = None
    network_count: int | None = None
    reused_count: int | None = None
    _analysis_claim: LockType = field(default_factory=Lock, init=False, repr=False, compare=False)

    def __post_init__(self):
        # 所有入口（包括诊断夹具）均复制元数据，不能留下调用者的可变字典。
        object.__setattr__(self, "papers", tuple(MappingProxyType(dict(p)) for p in self.papers))
        object.__setattr__(self, "categories", tuple(self.categories))

    @property
    def analysis_attempted(self) -> bool:
        return self._analysis_claim.locked()

    def claim_analysis(self) -> bool:
        """只消费一次；失败也不释放。重新抓取产生独立的新快照。"""
        return self._analysis_claim.acquire(blocking=False)

    @property
    def round1_count(self) -> int:
        return len(self.papers)

    @property
    def completed_time_text(self) -> str:
        return self.completed_at.strftime("%Y-%m-%d %H:%M:%S 北京时间")


@dataclass(frozen=True)
class CandidateFetchResult:
    snapshot: CandidateSnapshot | None = None
    outcome: DesktopOutcome | None = None
    operation_id: str = ""

    def __post_init__(self) -> None:
        if (self.snapshot is None) == (self.outcome is None):
            raise ValueError("candidate_fetch_result_requires_snapshot_xor_outcome")

    def __getattr__(self, name: str):
        """成功路径兼容只读快照属性，便于共享调用渐进迁移。"""
        if self.snapshot is not None:
            return getattr(self.snapshot, name)
        raise AttributeError(name)


def _fetch_issue(exc: BaseException, transport: Mapping[str, object] | None = None):
    code = str(exc).split(":", 1)[0]
    transport = dict(transport or {})
    curl_exit = transport.get("curl_exit_code")
    if isinstance(exc, MetadataError):
        return make_issue("AKD-FETCH-METADATA_FAILED")
    if code.startswith("arxiv_search_"):
        return make_issue("AKD-FETCH-HISTORY_INVALID")
    if code.startswith("arxiv_version_"):
        return make_issue("AKD-FETCH-VERSION_INVALID")
    if code == "arxiv_curl_unavailable":
        return make_issue("AKD-FETCH-CURL_UNAVAILABLE")
    if code == "arxiv_api_timeout":
        return make_issue(
            "AKD-FETCH-TIMEOUT", transient=True, automatic_retry_permitted=False
        )
    if code.startswith("arxiv_api_http_"):
        details = {}
        suffix = code.removeprefix("arxiv_api_http_")
        if suffix.isdigit():
            details["http_status"] = int(suffix)
        return make_issue(
            "AKD-FETCH-HTTP_RESPONSE_ERROR",
            details=details,
            transient=(suffix == "429" or suffix.startswith("5")),
            automatic_retry_permitted=False,
        )
    if code in {"arxiv_api_connection_failed", "curl_connection_failed", "curl_status_missing"}:
        if curl_exit == 6:
            stable = "AKD-FETCH-DNS_FAILED"
        elif curl_exit == 7:
            stable = "AKD-FETCH-CONNECTION_FAILED"
        elif curl_exit in {35, 51, 58, 60, 77, 80, 82, 83, 90, 91}:
            stable = "AKD-FETCH-TLS_FAILED"
        else:
            stable = "AKD-FETCH-CURL_EXECUTION_FAILED"
        return make_issue(
            stable, details={"curl_exit_code": curl_exit},
            transient=stable in {"AKD-FETCH-DNS_FAILED", "AKD-FETCH-CONNECTION_FAILED"},
            automatic_retry_permitted=False,
        )
    if isinstance(exc, (ValueError, KeyError)) or "xml" in code.lower() or "atom" in code.lower():
        return make_issue("AKD-FETCH-ATOM_INVALID")
    return make_issue("AKD-FETCH-UNEXPECTED")


def fetch_candidates_for_date(
    target_date: date,
    progress: ProgressCallback | None = None,
    *,
    diagnostics: DesktopDiagnostics | None = None,
    operation_id: str | None = None,
    categories: tuple[str, ...] | None = None,
) -> CandidateFetchResult:
    """首次／最新提交日发现及历史关联补查，联网固定最新版并保存候选。"""
    import main

    # 独立且不向根 logger 传播；Desktop 不创建日志文件。
    logger = logging.Logger("arxivkaleid.desktop")
    logger.addHandler(logging.NullHandler())
    logger.propagate = False
    operation_id = operation_id or (diagnostics.operation_id() if diagnostics else new_id())
    timer = StageTimer()
    current_day = target_date
    categories = tuple(categories if categories is not None else CATEGORIES)
    category_positions = {category: index for index, category in enumerate(categories, start=1)}
    last_transport_failure: dict[str, object] = {}
    store = VersionStore(PROJECT_ROOT)
    try:
        if type(target_date) is not date or not categories or len(set(categories)) != len(categories):
            raise ValueError("arxiv_version_query_invalid")
        # 在联网前核验已有库，损坏或未知身份不自动覆盖。
        store.previous(target_date, categories)
        if diagnostics:
            diagnostics.event(
                operation_id=operation_id, operation_type="fetch", stage="fetch",
                state="start", counts={"date_count": 1},
            )
        emit_progress(progress, ProgressEvent(
            task_type="fetch", stage="date_scan", state="running",
            message=f"候选抓取中 · 北京时间 {target_date.isoformat()}",
            current_date=target_date,
        ))
        # 先检查时区数据，缺失时在发出网络请求前安全失败。
        beijing = ZoneInfo("Asia/Shanghai")
        curl = paths.curl_executable(PROJECT_ROOT)
        cache = runtime_path("cache", "arxiv")
        cache.mkdir(parents=True, exist_ok=True)
        request_index = 0

        def request(url: str):
            nonlocal request_index
            request_index += 1
            is_search = urlsplit(url)[:3] == urlsplit(arxiv_versions.SEARCH_URL)[:3]
            params = parse_qs(urlsplit(url).query)
            timeout_seconds = ATOM_TIMEOUT_SECONDS
            context = {"request_index": request_index, "request_kind":
                       "AdvancedSearch" if is_search else ("LatestMetadata" if "id_list" in params else
                       "LatestUpdates" if params.get("sortBy") == ["lastUpdatedDate"] else "FirstSubmissions"),
                       "candidate_date": target_date.isoformat(),
                       "resumption_token_present": "resumptionToken" in params,
                       "timeout_seconds": timeout_seconds,
                       "process_timeout_seconds": timeout_seconds + SUBPROCESS_TIMEOUT_MARGIN_SECONDS}
            request_timer = StageTimer()
            if diagnostics:
                diagnostics.event(operation_id=operation_id, operation_type="fetch",
                                  stage="http", state="start", details=context)
            # 每次写请求间隔文件前检查其最终路径，沿用已有 curl 传输。
            runtime_path("cache", "arxiv", "last_request_time.txt")
            def observe(kind: str, payload: Mapping[str, object]) -> None:
                if kind == "curl_failure":
                    last_transport_failure.clear()
                    last_transport_failure.update(payload)
                if diagnostics is not None:
                    complete = kind != "curl_failure" and payload.get("http_status") == 200
                    code = ("AKD-FETCH-TIMEOUT" if payload.get("error_type") == "arxiv_api_timeout"
                            else "AKD-FETCH-HTTP_RESPONSE_ERROR")
                    diagnostics.event(
                        operation_id=operation_id, operation_type="fetch",
                        stage="http", state="complete" if complete else "fail",
                        code=None if complete else code,
                        scope=None if complete else "system", elapsed_ms=request_timer.elapsed_ms,
                        details={**context, **payload},
                        transient=(payload.get("http_status") == 429 or (
                            isinstance(payload.get("http_status"), int)
                            and 500 <= int(payload["http_status"]) <= 599
                        )),
                        automatic_retry_permitted=False,
                    )

            xml = main.fetch_arxiv_metadata(
                url=url,
                timeout_seconds=timeout_seconds,
                cache_dir=cache,
                interval_seconds=5,
                logger=logger,
                curl_executable=curl,
                curl_extra_args=("--disable",) if paths.frozen() else (),
                subprocess_creationflags=0x08000000 if paths.frozen() else 0,
                diagnostic_observer=observe,
                emit_success_transport_diagnostic=True,
                subprocess_timeout_margin_seconds=SUBPROCESS_TIMEOUT_MARGIN_SECONDS,
            )
            return xml

        def on_page(category: str, page: int, records: int, source: str):
            category_index = category_positions[category]
            if diagnostics:
                diagnostics.event(operation_id=operation_id, operation_type="fetch", stage="category",
                                  state="complete", counts={"records": records},
                                  details={"category": category, "page_count": page, "request_kind": source})
            emit_progress(progress, ProgressEvent(
                task_type="fetch", stage="category", state="running",
                message=(f"候选发现中 · 北京时间 {target_date.isoformat()} · "
                         f"分类 {category_index} / {len(categories)}：{category} · "
                         f"第 {page} 页 · 本页 {records} 条 · {source}"),
                current_date=target_date, category=category,
                category_index=category_index, category_total=len(categories),
                processed=records,
            ))

        day = target_date
        papers, raw_count, page_count, network_count, reused_count = arxiv_versions.collect(
            day, categories, request, store, on_page,
        )
        # 候选和查询成员同一事务；落库失败不能把未保存候选交给分析。
        store.save(operation_id, day, categories, papers, raw_count)
        completed_at = datetime.now(timezone.utc).astimezone(beijing)
        if not papers:
            normal = outcome(
                "AKO-FETCH-NO_CANDIDATES", "fetch",
                f"北京时间 {day.isoformat()} 无候选。",
                candidate_count=0, target_date=day.isoformat(),
                completed_time=completed_at.strftime("%Y-%m-%d %H:%M:%S 北京时间"),
            )
            if diagnostics:
                diagnostics.outcome(
                    operation_id=operation_id, operation_type="fetch", outcome=normal,
                )
            emit_progress(progress, ProgressEvent(
                task_type="fetch", stage="complete", state="outcome",
                message=normal.summary, code=normal.code, scope="outcome",
                operation_id=operation_id, result_count=0,
            ))
            return CandidateFetchResult(outcome=normal, operation_id=operation_id)
        # 核心解析器 metadata 为扁平字符串/整数；复制并只读包装防止后续修改。
        frozen = tuple(MappingProxyType(dict(p)) for p in papers)
        snapshot = CandidateSnapshot(
            candidate_date=day,
            completed_at=completed_at,
            raw_count=raw_count,
            unique_count=len(papers),
            papers=frozen,
            fetch_operation_id=operation_id,
            categories=categories, fetch_policy=FETCH_POLICY, page_count=page_count,
            network_count=network_count, reused_count=reused_count,
        )
        emit_progress(progress, ProgressEvent(
            task_type="fetch", stage="complete", state="completed",
            message=(f"候选获取完成 · 北京时间 {day.isoformat()} · "
                     f"{snapshot.round1_count} 篇候选"),
            current_date=day, processed=snapshot.round1_count,
            total=snapshot.round1_count, result_count=snapshot.round1_count,
            operation_id=operation_id, snapshot_id=snapshot.snapshot_id,
        ))
        if diagnostics:
            diagnostics.event(
                operation_id=operation_id, operation_type="fetch", stage="fetch",
                state="complete", snapshot_id=snapshot.snapshot_id,
                elapsed_ms=timer.elapsed_ms,
                counts={"raw_count": snapshot.raw_count, "unique_count": snapshot.unique_count,
                        "candidate_count": snapshot.round1_count},
            )
        return CandidateFetchResult(snapshot=snapshot, operation_id=operation_id)
    except Exception as exc:
        issue = (
            exc.issue if isinstance(exc, DesktopOperationError)
            else _fetch_issue(exc, last_transport_failure)
        )
        # 失败记录不包含部分候选。库本身失败时保留原库，不再尝试写入。
        if not isinstance(exc, MetadataError):
            try:
                store.save(operation_id, target_date, categories, [], 0, error_code=issue.code)
            except MetadataError:
                issue = make_issue("AKD-FETCH-METADATA_FAILED")
        if diagnostics:
            diagnostics.event(
                operation_id=operation_id, operation_type="fetch", stage=issue.stage,
                state="fail", code=issue.code, scope=issue.scope,
                elapsed_ms=timer.elapsed_ms, details=issue.details,
                transient=issue.transient,
                automatic_retry_permitted=issue.automatic_retry_permitted,
                unexpected=exc if issue.code.endswith("-UNEXPECTED") else None,
            )
        # 不把底层异常、响应正文或本地路径传到 GUI。
        emit_progress(progress, ProgressEvent(
            task_type="fetch", stage="date_scan", state="failed",
            message=issue.reason, current_date=current_day,
            code=issue.code, scope=issue.scope, operation_id=operation_id,
        ))
        raise CandidateError(issue) from None
