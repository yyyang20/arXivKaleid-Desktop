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


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CATEGORIES = ("gr-qc", "astro-ph.HE", "astro-ph.GA")
LOOKBACK_DAYS = 14
MAX_CANDIDATES = 100
PAGE_SIZE = 1000


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
    _analysis_claim: LockType = field(default_factory=Lock, init=False, repr=False, compare=False)

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


def fetch_latest_candidates(
    start_date_utc: date,
    progress: ProgressCallback | None = None,
    *,
    diagnostics: DesktopDiagnostics | None = None,
    operation_id: str | None = None,
) -> CandidateFetchResult:
    """完整获取最近非空 UTC 提交日，再冻结前 100 篇；不读取历史状态。"""
    import main

    # 独立且不向根 logger 传播；Desktop 不创建日志文件。
    logger = logging.Logger("arxivkaleid.desktop")
    logger.addHandler(logging.NullHandler())
    logger.propagate = False
    operation_id = operation_id or (diagnostics.operation_id() if diagnostics else new_id())
    timer = StageTimer()
    current_day = start_date_utc
    categories = tuple(CATEGORIES)
    category_positions = {category: index for index, category in enumerate(categories, start=1)}
    current_day_count = 0
    last_transport_failure: dict[str, object] = {}
    try:
        if diagnostics:
            diagnostics.event(
                operation_id=operation_id, operation_type="fetch", stage="fetch",
                state="start", counts={"lookback_days": LOOKBACK_DAYS + 1},
            )
        emit_progress(progress, ProgressEvent(
            task_type="fetch", stage="date_scan", state="running",
            message=f"候选抓取中 · 正在检查 UTC {start_date_utc.isoformat()}",
            current_date=start_date_utc,
        ))
        # 先检查时区数据，缺失时在发出网络请求前安全失败。
        beijing = ZoneInfo("Asia/Shanghai")
        curl = paths.curl_executable(PROJECT_ROOT)
        cache = runtime_path("cache", "arxiv")
        cache.mkdir(parents=True, exist_ok=True)

        def fetch_page(category: str, day: date, start: int, size: int):
            # 每次写请求间隔文件前检查其最终路径，沿用已有 curl 传输。
            runtime_path("cache", "arxiv", "last_request_time.txt")
            def observe(kind: str, payload: Mapping[str, object]) -> None:
                if kind == "curl_failure":
                    last_transport_failure.clear()
                    last_transport_failure.update(payload)
                if diagnostics is not None:
                    diagnostics.event(
                        operation_id=operation_id, operation_type="fetch",
                        stage="http", state="complete" if payload.get("http_status") == 200 else "fail",
                        code=(None if payload.get("http_status") == 200 else "AKD-FETCH-HTTP_RESPONSE_ERROR"),
                        scope=(None if payload.get("http_status") == 200 else "system"),
                        details=payload,
                        transient=(payload.get("http_status") == 429 or (
                            isinstance(payload.get("http_status"), int)
                            and 500 <= int(payload["http_status"]) <= 599
                        )),
                        automatic_retry_permitted=False,
                    )

            xml = main.fetch_arxiv_metadata(
                url=main.build_arxiv_query_url(category, day, size, start=start),
                timeout_seconds=90,
                cache_dir=cache,
                interval_seconds=5,
                logger=logger,
                curl_executable=curl,
                curl_extra_args=("--disable",) if paths.frozen() else (),
                subprocess_creationflags=0x08000000 if paths.frozen() else 0,
                diagnostic_observer=observe,
                emit_success_transport_diagnostic=True,
            )
            return main.parse_arxiv_feed(xml, logger)

        def fetch_category(category: str, day: date):
            nonlocal current_day, current_day_count
            if day != current_day:
                current_day = day
                current_day_count = 0
                emit_progress(progress, ProgressEvent(
                    task_type="fetch", stage="date_scan", state="running",
                    message=f"候选抓取中 · 正在检查 UTC {day.isoformat()}",
                    current_date=day,
                ))
            category_index = category_positions[category]
            emit_progress(progress, ProgressEvent(
                task_type="fetch", stage="category", state="running",
                message=(f"候选抓取中 · UTC {day.isoformat()} · "
                         f"分类 {category_index} / {len(categories)}：{category} · "
                         f"当前日期已获取 {current_day_count} 篇"),
                current_date=day, category=category,
                category_index=category_index, category_total=len(categories),
                processed=current_day_count,
            ))
            category_papers = main.fetch_category_submission_pages(
                category=category,
                submission_date=day,
                page_size=PAGE_SIZE,
                fetch_page=fetch_page,
                logger=logger,
            )
            current_day_count += sum(
                main.parse_utc_submission_date(paper.get("published")) == day
                for paper in category_papers
            )
            emit_progress(progress, ProgressEvent(
                task_type="fetch", stage="category", state="running",
                message=(f"候选抓取中 · UTC {day.isoformat()} · "
                         f"分类 {category_index} / {len(categories)}：{category} · "
                         f"当前日期已获取 {current_day_count} 篇"),
                current_date=day, category=category,
                category_index=category_index, category_total=len(categories),
                processed=current_day_count,
            ))
            return category_papers

        day, papers, duplicates, _, _ = main.find_recent_nonempty_submission_batch(
            start_date_utc=start_date_utc,
            lookback_days=LOOKBACK_DAYS,
            categories=list(categories),
            fetch_category=fetch_category,
        )
        if day is None or not papers:
            normal = outcome(
                "AKO-FETCH-NO_CANDIDATES", "fetch",
                "当天及向前 14 天内没有可用候选。",
                candidate_count=0,
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
        frozen = tuple(MappingProxyType(dict(p)) for p in papers[:MAX_CANDIDATES])
        snapshot = CandidateSnapshot(
            candidate_date=day,
            completed_at=datetime.now(timezone.utc).astimezone(beijing),
            raw_count=len(papers) + duplicates,
            unique_count=len(papers),
            papers=frozen,
            fetch_operation_id=operation_id,
        )
        emit_progress(progress, ProgressEvent(
            task_type="fetch", stage="complete", state="completed",
            message=(f"候选抓取完成 · UTC {day.isoformat()} · "
                     f"锁定 {snapshot.round1_count} 篇"),
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
