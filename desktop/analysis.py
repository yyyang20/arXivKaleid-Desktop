from __future__ import annotations

import logging
import os
import sqlite3
from dataclasses import dataclass
from decimal import Decimal
from threading import Lock
from typing import Callable

import build_round2_inputs
import main
import model_usage
import pdf_processing
import round2_fulltext_state
import run_round2
from desktop import pipeline
from desktop import paths as desktop_paths
from desktop.config import batch_cost_limit
from desktop.diagnostics import DesktopDiagnostics, StageTimer, new_id
from desktop.errors import (
    DesktopIssue,
    DesktopOperationError,
    DesktopOutcome,
    FailureBoundary,
    classify_failure_scope,
    make_issue,
    outcome,
)
from desktop.pipeline import CandidateSnapshot, runtime_path
from desktop.progress import ProgressCallback, ProgressEvent, emit_progress


class AnalysisError(DesktopOperationError):
    """兼容旧调用名；实际错误由 DesktopIssue 表达。"""


@dataclass(frozen=True)
class AnalysisResult:
    run_id: int
    markdown: str
    recommendation_count: int = 0
    operation_id: str = ""
    snapshot_id: str = ""
    fetch_operation_id: str = ""
    paper_issues: tuple[DesktopIssue, ...] = ()
    outcomes: tuple[DesktopOutcome, ...] = ()
    log_relative_path: str | None = None
    diagnostics_persistent: bool = False


def _call_details(result) -> dict[str, object]:
    attempts = tuple(getattr(result, "attempts", ()) or ())
    attempt = attempts[-1] if attempts else None
    provider = getattr(attempt, "provider_error", None) if attempt is not None else None
    return {
        "error_type": getattr(result, "error_type", None),
        "http_status": getattr(attempt, "http_status", None),
        "finish_reason": getattr(attempt, "finish_reason", None),
        "provider_code": getattr(provider, "code", None),
        "provider_type": getattr(provider, "error_type", None),
        "provider_param": getattr(provider, "param", None),
        "attempt": getattr(attempt, "attempt_no", None),
    }


def _model_issue(stage: str, result) -> DesktopIssue:
    error_type = str(getattr(result, "error_type", None) or "request_failed")
    attempt = tuple(getattr(result, "attempts", ()) or ())[-1:] or (None,)
    http_status = getattr(attempt[0], "http_status", None)
    prefix = "AKD-R1" if stage == "round1" else "AKD-R2"
    if error_type == "auth_failed":
        suffix = "AUTH_FAILED"
    elif error_type == "timeout":
        suffix = "TIMEOUT"
    elif error_type == "output_truncated":
        suffix = "OUTPUT_TRUNCATED"
    elif error_type == "content_filtered":
        suffix = "CONTENT_FILTERED"
    elif error_type == "empty_content":
        suffix = "EMPTY_CONTENT"
    elif error_type == "responses_not_completed":
        suffix = "RESPONSE_INCOMPLETE"
    elif error_type == "json_parse_failed":
        suffix = "OUTPUT_INVALID"
    elif http_status is not None:
        suffix = "HTTP_RESPONSE_ERROR"
    elif error_type == "api_request_failed":
        suffix = "NETWORK_FAILED"
    else:
        suffix = "REQUEST_FAILED"
    return make_issue(
        f"{prefix}-{suffix}",
        details=_call_details(result),
        transient=(error_type == "timeout" or http_status == 429 or (
            isinstance(http_status, int) and 500 <= http_status <= 599
        )),
        automatic_retry_permitted=False,
    )


def _sqlite_intact(*connections: sqlite3.Connection) -> bool:
    try:
        return all(connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok" for connection in connections)
    except Exception:
        return False


def _analysis_issue(stage: str, exc: BaseException) -> DesktopIssue:
    if isinstance(exc, DesktopOperationError):
        return exc.issue
    code = str(exc).split(":", 1)[0]
    if stage == "prepare":
        if code == "desktop_missing_key":
            return make_issue("AKD-KEY-EMPTY")
        return make_issue("AKD-PREPARE-WORKSPACE_FAILED")
    if stage == "round1":
        if isinstance(exc, sqlite3.Error) or code.endswith("save_failed"):
            return make_issue("AKD-R1-STORAGE_FAILED")
        return make_issue("AKD-R1-UNEXPECTED")
    if stage == "pdf":
        if isinstance(exc, (sqlite3.Error, OSError)):
            return make_issue("AKD-PDF-STORAGE_FAILED")
        return make_issue("AKD-PDF-UNEXPECTED")
    if stage == "fulltext":
        if isinstance(exc, (sqlite3.Error, OSError, ImportError)):
            return make_issue("AKD-FULLTEXT-COMPONENT_FAILED")
        return make_issue("AKD-FULLTEXT-UNEXPECTED")
    if stage == "round2":
        if code.startswith("round2_validation_failed"):
            return make_issue("AKD-R2-OUTPUT_INVALID")
        return make_issue("AKD-R2-UNEXPECTED")
    if stage == "report":
        return make_issue("AKD-REPORT-GENERATION_FAILED")
    return make_issue("AKD-PREPARE-WORKSPACE_FAILED")


def lock_work_directory():
    """用操作系统文件锁保护固定工作库；进程退出自动释放，不实现恢复。"""
    work = runtime_path("work")
    path = runtime_path("work", "analysis.lock")
    if work != runtime_path() / "work" or path != work / "analysis.lock":
        raise RuntimeError("desktop_work_path_alias")
    work.mkdir(parents=True, exist_ok=True)
    stream = path.open("a+b")
    try:
        if path.stat().st_size == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return stream
    except Exception:
        stream.close()
        raise RuntimeError("desktop_analysis_already_running") from None


def initialize_work_paths():
    """只重置两个已知工作库及 sidecar；保留 Secret、缓存和 PDF。"""
    names = ("arxiv_kaleid.sqlite", "round2_inputs.sqlite")
    targets = []
    work = runtime_path("work")
    for name in names:
        for suffix in ("", "-journal", "-wal", "-shm"):
            path = runtime_path("work", name + suffix)
            if path != work / (name + suffix):
                raise RuntimeError("desktop_work_path_alias")
            if path.exists() and not path.is_file():
                raise RuntimeError("desktop_work_path_not_file")
            targets.append(path)
    work.mkdir(parents=True, exist_ok=True)
    for path in targets:
        path.unlink(missing_ok=True)
    return tuple(runtime_path("work", name) for name in names)


def validate_budget(connection, run_id, config, stage, messages):
    """沿用保守估算和峰值价格，并把前一轮实际费用计入同批上限。"""
    stage_config = main.deepseek_stage_config(config, stage)
    tokens = round2_fulltext_state.conservative_request_token_estimate(messages)
    if stage == "round2":
        tokens += round2_fulltext_state.conservative_value_token_estimate(
            run_round2.build_round2_result_tool(config)
        )
    output = int(stage_config["max_output_tokens"])
    if (
        stage_config["max_retries"] != 0
        or tokens > int(config["limits"][f"max_{stage}_request_tokens"])
        or tokens + output > config["limits"]["model_context_tokens"] - config["limits"]["context_safety_margin_tokens"]
        or output > config["limits"]["model_max_output_tokens"]
    ):
        raise RuntimeError("desktop_token_budget_exceeded")
    price = model_usage.price_snapshot_from_config(config, stage=stage, conservative=True)
    if price is None or price.currency != "CNY":
        raise RuntimeError("desktop_price_unavailable")
    usage = checked_usage(connection, run_id, config)
    spent = usage.known_cost if usage else Decimal(0)
    worst = (
        Decimal(tokens) * price.cache_miss_input_price_per_million
        + Decimal(output) * price.output_price_per_million
    ) / Decimal(1_000_000)
    cap = batch_cost_limit(config)
    if not worst.is_finite() or spent + worst > cap:
        raise RuntimeError("desktop_cost_budget_exceeded")


def checked_usage(connection, run_id, config):
    usage = model_usage.load_run_usage_summary(connection, run_id)
    if usage is not None and (
        not usage.token_complete or not usage.cost_complete
        or usage.currency != "CNY" or not usage.known_cost.is_finite()
        or usage.known_cost > batch_cost_limit(config)
    ):
        raise RuntimeError("desktop_usage_or_cost_unconfirmed")
    return usage


def run_round1(
    connection, run_id, papers, config, profile, prompt, api_key,
    diagnostic_observer: Callable[[str, object], None] | None = None,
):
    if not papers:
        main.save_round1_screening_results(
            connection, run_id, [], config, selection_audit=main.empty_screening_stage_audit()
        )
        return []
    messages = main.build_round1_messages(prompt, profile, papers, config)
    validate_budget(connection, run_id, config, "round1", messages)
    stage = main.deepseek_stage_config(config, "round1")
    client = main.DeepSeekClient(
        api_key=api_key, model=stage["model"], endpoint_url=stage["base_url"],
        timeout_seconds=float(stage["timeout_seconds"]), max_retries=0,
        thinking_mode=stage["thinking_mode"], reasoning_effort=stage["reasoning_effort"],
    )
    price = model_usage.price_snapshot_from_config(config, stage="round1")
    result = client.request_json(messages, max_tokens=stage["max_output_tokens"])
    if diagnostic_observer is not None:
        try:
            diagnostic_observer("call_result", result)
        except Exception:
            pass
    call_id = model_usage.record_api_call(
        connection, run_id=run_id, task_type="round1_abstract_screening",
        call_purpose="screening", model_requested=stage["model"],
        prompt_version=config["versions"]["round1_prompt_version"],
        research_profile_version=config["versions"]["research_profile_version"],
        selection_policy_version=config["round1_selection_policy_version"],
        request_hash=main.stable_json_hash(messages), local_cache_status="miss",
        call_result=result, price_snapshot=price,
    )
    if not result.ok or result.data is None:
        raise DesktopOperationError(_model_issue("round1", result))
    try:
        validated, _ = main.validate_round1_result(
            result.data, papers, max_selected=config["round1_max_selected_n"],
            profile_version=config["versions"]["research_profile_version"],
            prompt_version=config["versions"]["round1_prompt_version"],
            selection_policy_version=config["round1_selection_policy_version"],
        )
        if not validated["batch_valid"]:
            if diagnostic_observer is not None:
                try:
                    diagnostic_observer(
                        "validation_diagnostic", validated.get("validation_diagnostic")
                    )
                except Exception:
                    pass
            raise DesktopOperationError(make_issue(
                "AKD-R1-OUTPUT_INVALID",
                details={
                    "validation_code": validated.get("validation_error_code"),
                    "validation_stage": (
                        validated.get("validation_diagnostic") or {}
                    ).get("stage"),
                },
            ))
        if diagnostic_observer is not None:
            try:
                diagnostic_observer(
                    "validation_diagnostic", validated.get("validation_diagnostic")
                )
            except Exception:
                pass
    except Exception:
        model_usage.update_call_status(connection, call_id, "validation_failed", "round1_validation_failed")
        raise
    try:
        selected = validated["selected_papers"]
        main.save_round1_screening_results(
            connection, run_id, selected, config, selection_audit=validated["selection_audit"]
        )
        main.save_round1_completion_statuses(connection, run_id, papers, selected, config)
    except Exception:
        model_usage.update_call_status(connection, call_id, "save_failed", "round1_save_failed")
        raise
    model_usage.update_call_status(connection, call_id, "completed")
    checked_usage(connection, run_id, config)
    return selected


class AnalysisAttempt:
    def __init__(
        self,
        snapshot: CandidateSnapshot,
        diagnostics: DesktopDiagnostics | None = None,
    ):
        if not snapshot.claim_analysis():
            raise AnalysisError(make_issue("AKD-PREPARE-WORKSPACE_FAILED"))
        self.snapshot = snapshot
        self.diagnostics = diagnostics
        self.operation_id = diagnostics.operation_id() if diagnostics else new_id()
        self.run_id: int | None = None
        self._started = Lock()

    def run(
        self,
        api_key: str,
        progress: ProgressCallback | None = None,
    ) -> AnalysisResult:
        if not self._started.acquire(blocking=False):
            raise AnalysisError(make_issue("AKD-PREPARE-WORKSPACE_FAILED"))
        connection = None
        fulltext = None
        work_lock = None
        run_id = None
        stage = "prepare"
        operation_timer = StageTimer()
        stage_timer = StageTimer()
        paper_issues: list[DesktopIssue] = []
        outcomes: list[DesktopOutcome] = []
        identities = {
            "snapshot_id": self.snapshot.snapshot_id,
            "fetch_operation_id": self.snapshot.fetch_operation_id or None,
        }
        try:
            if self.diagnostics:
                self.diagnostics.event(
                    operation_id=self.operation_id, operation_type="analysis",
                    stage="analysis", state="start", **identities,
                )
            emit_progress(progress, ProgressEvent(
                task_type="analysis", stage="prepare", state="running",
                message="正在准备分析…",
            ))
            if not api_key.strip():
                raise RuntimeError("desktop_missing_key")
            root = desktop_paths.application_root(pipeline.PROJECT_ROOT)
            resources = desktop_paths.resource_root(pipeline.PROJECT_ROOT)
            config, paths, profile, round2_prompt = run_round2.read_round2_context(resources)
            if (config["round1_max_selected_n"] > 10 or config["final_max_recommendations"] > 5
                    or config["limits"]["max_round2_pdf_pages"] != 60
                    or config["deepseek"]["max_retries"] != 0):
                raise RuntimeError("desktop_shared_limits_invalid")
            # 唯一候选来源就是此对象；复制扁平 metadata，不查询或重排历史。
            papers = [dict(p) for p in self.snapshot.papers]
            keys = [(p["arxiv_id"], p["version"]) for p in papers]
            if len(papers) > pipeline.MAX_CANDIDATES or len(set(keys)) != len(keys):
                raise RuntimeError("desktop_snapshot_invalid")
            work_lock = lock_work_directory()
            database, fulltext_database = initialize_work_paths()
            connection = main.init_database(database)
            fulltext = sqlite3.connect(fulltext_database)
            fulltext.row_factory = sqlite3.Row
            round2_fulltext_state.initialize_fulltext_schema(fulltext)
            run_id = main.start_run(connection, main.current_time_iso())
            self.run_id = run_id
            if self.diagnostics:
                self.diagnostics.event(
                    operation_id=self.operation_id, operation_type="analysis",
                    stage="prepare", state="complete", run_id=run_id,
                    elapsed_ms=stage_timer.elapsed_ms, **identities,
                )
                self.diagnostics.event(
                    operation_id=self.operation_id, operation_type="analysis",
                    stage="run_bound", state="complete", run_id=run_id, **identities,
                )
            main.insert_papers(connection, papers)
            logger = logging.Logger("arxivkaleid.desktop.analysis")
            logger.addHandler(logging.NullHandler())
            logger.propagate = False
            stage = "round1"
            stage_timer = StageTimer()
            if self.diagnostics:
                self.diagnostics.event(
                    operation_id=self.operation_id, operation_type="analysis",
                    stage="round1", state="start", run_id=run_id,
                    counts={"candidate_count": len(papers)}, **identities,
                )
            emit_progress(progress, ProgressEvent(
                task_type="analysis", stage="round1", state="running",
                message="Round 1 分析中…", processed=0, total=len(papers),
            ))
            def observe_round1(kind: str, value: object) -> None:
                if not self.diagnostics:
                    return
                if kind == "call_result":
                    details = _call_details(value)
                    observed_stage = "round1_request"
                    observed_state = "complete" if getattr(value, "ok", False) else "fail"
                elif isinstance(value, dict):
                    details = {
                        "validation_code": value.get("code"),
                        "validation_stage": value.get("stage"),
                        "validation_diagnostic": value,
                    }
                    observed_stage = "round1_validation"
                    observed_state = "complete" if value.get("code") in {None, "ok"} else "fail"
                else:
                    details = {}
                    observed_stage = "round1_validation"
                    observed_state = "complete"
                self.diagnostics.event(
                    operation_id=self.operation_id, operation_type="analysis",
                    stage=observed_stage, state=observed_state, run_id=run_id,
                    details=details, **identities,
                )

            selected = run_round1(
                connection, run_id, papers, config, profile,
                main.load_prompt(paths["round1_prompt"]), api_key,
                diagnostic_observer=observe_round1,
            )
            if not selected:
                normal = outcome(
                    "AKO-R1-NO_SELECTED", "round1", "Round 1 正常完成，零篇入围。",
                    selected_count=0,
                )
                outcomes.append(normal)
                if self.diagnostics:
                    self.diagnostics.outcome(
                        operation_id=self.operation_id, operation_type="analysis",
                        outcome=normal, run_id=run_id, **identities,
                    )
            if self.diagnostics:
                self.diagnostics.event(
                    operation_id=self.operation_id, operation_type="analysis",
                    stage="round1", state="complete", run_id=run_id,
                    elapsed_ms=stage_timer.elapsed_ms,
                    counts={"candidate_count": len(papers), "selected_count": len(selected)},
                    **identities,
                )
            emit_progress(progress, ProgressEvent(
                task_type="analysis", stage="round1", state="completed",
                message=f"Round 1 完成 · 入围 {len(selected)} 篇",
                processed=len(papers), total=len(papers), result_count=len(selected),
            ))
            stage = "pdf"
            stage_timer = StageTimer()
            download_results = []
            if selected:
                pdfs_dir = runtime_path("pdfs")
                pdfs = [pdf_processing.SelectedPaper(p["arxiv_id"], p["version"], p["title"]) for p in selected]
                # 下载器仍执行单篇失败隔离；预先核验日期目录和每个文件的 runtime 边界。
                for paper in pdfs:
                    dest = pdf_processing.build_pdf_destination(pdfs_dir, self.snapshot.candidate_date.isoformat(), paper)
                    if runtime_path(*dest.relative_to(runtime_path()).parts) != dest:
                        raise RuntimeError("desktop_pdf_path_alias")
                pdf_processed = 0
                if self.diagnostics:
                    self.diagnostics.event(
                        operation_id=self.operation_id, operation_type="analysis",
                        stage="pdf", state="start", run_id=run_id,
                        counts={"selected_count": len(pdfs)}, **identities,
                    )
                emit_progress(progress, ProgressEvent(
                    task_type="analysis", stage="pdf", state="running",
                    message=f"PDF 下载中 · 已处理 0 / {len(pdfs)}",
                    processed=0, total=len(pdfs),
                ))

                def pdf_progress(_result):
                    nonlocal pdf_processed
                    pdf_processed += 1
                    emit_progress(progress, ProgressEvent(
                        task_type="analysis", stage="pdf", state="running",
                        message=f"PDF 下载中 · 已处理 {pdf_processed} / {len(pdfs)}",
                        processed=pdf_processed, total=len(pdfs),
                    ))

                download_results = pdf_processing.download_selected_papers(
                    root, connection, pdfs_dir, pdfs, self.snapshot.candidate_date.isoformat(), logger,
                    timeout_seconds=float(config["request_timeout_seconds"]),
                    progress_callback=pdf_progress,
                )
                if not _sqlite_intact(connection):
                    raise DesktopOperationError(make_issue("AKD-PDF-STORAGE_FAILED"))
                recorded_downloads = connection.execute(
                    "SELECT COUNT(*) FROM pdf_downloads WHERE (arxiv_id, version) IN ("
                    + ",".join("(?, ?)" for _ in pdfs) + ")",
                    [value for paper in pdfs for value in (paper.arxiv_id, paper.version)],
                ).fetchone()[0]
                if recorded_downloads != len(pdfs):
                    raise DesktopOperationError(make_issue("AKD-PDF-STORAGE_FAILED"))
                for result in download_results:
                    if result.status in pdf_processing.SUCCESSFUL_PDF_DOWNLOAD_STATUSES:
                        continue
                    if result.error_type == "file_write_failed":
                        raise DesktopOperationError(make_issue(
                            "AKD-PDF-STORAGE_FAILED",
                            details={"error_type": result.error_type},
                        ))
                    scope = classify_failure_scope(FailureBoundary(
                        paper_boundary_established=True,
                        shared_components_ready=True,
                        shared_state_intact=True,
                        paper_result_persisted=True,
                    ))
                    issue = make_issue(
                        "AKD-PDF-PAPER_PROCESSING_FAILED", scope=scope,
                        details={
                            "error_type": (
                                "http_response_error"
                                if str(result.error_type or "").startswith("http_")
                                else result.error_type
                            ),
                            "http_status": result.http_status,
                            "network_attempted": result.network_attempted,
                            "size_bytes": result.size_bytes,
                        },
                        transient=result.transient,
                        automatic_retry_permitted=False,
                    )
                    paper_issues.append(issue)
                    if self.diagnostics:
                        self.diagnostics.issue(
                            operation_id=self.operation_id, operation_type="analysis",
                            issue=issue, run_id=run_id,
                            arxiv_id=result.paper.arxiv_id, version=result.paper.version,
                            **identities,
                        )
                emit_progress(progress, ProgressEvent(
                    task_type="analysis", stage="pdf", state="completed",
                    message=f"PDF 下载完成 · 已处理 {len(download_results)} / {len(pdfs)}",
                    processed=len(download_results), total=len(pdfs),
                ))
                successful_pdf_keys = {
                    (result.paper.arxiv_id, result.paper.version)
                    for result in download_results
                    if result.status in pdf_processing.SUCCESSFUL_PDF_DOWNLOAD_STATUSES
                }
                if not successful_pdf_keys:
                    normal = outcome(
                        "AKO-PDF-NO_SUCCESS", "pdf",
                        "所有 PDF 均因确认的单篇原因被排除。",
                        paper_issue_count=len(paper_issues),
                    )
                    outcomes.append(normal)
                    if self.diagnostics:
                        self.diagnostics.outcome(
                            operation_id=self.operation_id, operation_type="analysis",
                            outcome=normal, run_id=run_id, **identities,
                        )
                if self.diagnostics:
                    self.diagnostics.event(
                        operation_id=self.operation_id, operation_type="analysis",
                        stage="pdf", state="complete", run_id=run_id,
                        elapsed_ms=stage_timer.elapsed_ms,
                        counts={"selected_count": len(pdfs), "eligible_count": len(successful_pdf_keys),
                                "paper_issue_count": len([i for i in paper_issues if i.stage == "pdf"])},
                        **identities,
                    )
                stage = "fulltext"
                stage_timer = StageTimer()
                fulltext_processed = 0
                if successful_pdf_keys:
                    if self.diagnostics:
                        self.diagnostics.event(
                            operation_id=self.operation_id, operation_type="analysis",
                            stage="fulltext", state="start", run_id=run_id,
                            counts={"candidate_count": len(successful_pdf_keys)}, **identities,
                        )
                    emit_progress(progress, ProgressEvent(
                        task_type="analysis", stage="fulltext", state="running",
                        message=f"全文提取中 · 已处理 0 / {len(successful_pdf_keys)}",
                        processed=0, total=len(successful_pdf_keys),
                    ))

                def fulltext_progress(result):
                    nonlocal fulltext_processed
                    if (result.arxiv_id, result.version) not in successful_pdf_keys:
                        return
                    fulltext_processed += 1
                    emit_progress(progress, ProgressEvent(
                        task_type="analysis", stage="fulltext", state="running",
                        message=(f"全文提取中 · 已处理 {fulltext_processed} / "
                                 f"{len(successful_pdf_keys)}"),
                        processed=fulltext_processed, total=len(successful_pdf_keys),
                    ))

                fulltext_results = round2_fulltext_state.process_downloaded_pdfs(
                    connection, fulltext, root, logger, max_pdf_pages=config["limits"]["max_round2_pdf_pages"],
                    target_papers={(p["arxiv_id"], p["version"]) for p in selected},
                    progress_callback=fulltext_progress,
                    isolate_unexpected_paper_errors=True,
                )
                if not _sqlite_intact(connection, fulltext):
                    raise DesktopOperationError(make_issue("AKD-FULLTEXT-COMPONENT_FAILED"))
                for result in fulltext_results:
                    if (result.arxiv_id, result.version) not in successful_pdf_keys:
                        continue
                    if result.extraction_status != round2_fulltext_state.EXTRACTION_FAILED:
                        continue
                    scope = classify_failure_scope(FailureBoundary(
                        paper_boundary_established=True,
                        shared_components_ready=True,
                        shared_state_intact=True,
                        paper_result_persisted=True,
                    ))
                    issue = make_issue(
                        "AKD-FULLTEXT-PAPER_EXTRACTION_FAILED", scope=scope,
                        details={"failure_reason": result.failure_reason, "page_count": result.page_count},
                    )
                    paper_issues.append(issue)
                    if self.diagnostics:
                        self.diagnostics.issue(
                            operation_id=self.operation_id, operation_type="analysis",
                            issue=issue, run_id=run_id, arxiv_id=result.arxiv_id,
                            version=result.version, **identities,
                        )
                if successful_pdf_keys:
                    emit_progress(progress, ProgressEvent(
                        task_type="analysis", stage="fulltext", state="completed",
                        message=(f"全文提取完成 · 已处理 {fulltext_processed} / "
                                 f"{len(successful_pdf_keys)}"),
                        processed=fulltext_processed, total=len(successful_pdf_keys),
                    ))
                else:
                    emit_progress(progress, ProgressEvent(
                        task_type="analysis", stage="fulltext", state="skipped",
                        message="全文提取已跳过 · 没有成功下载的 PDF",
                        processed=0, total=0,
                    ))
                if self.diagnostics:
                    self.diagnostics.event(
                        operation_id=self.operation_id, operation_type="analysis",
                        stage="fulltext", state="complete" if successful_pdf_keys else "skip",
                        run_id=run_id, elapsed_ms=stage_timer.elapsed_ms,
                        counts={"candidate_count": len(successful_pdf_keys),
                                "paper_issue_count": len([i for i in paper_issues if i.stage == "fulltext"])},
                        **identities,
                    )
            else:
                if self.diagnostics:
                    self.diagnostics.event(
                        operation_id=self.operation_id, operation_type="analysis",
                        stage="pdf", state="skip", run_id=run_id,
                        code="AKO-R1-NO_SELECTED", scope="outcome", **identities,
                    )
                    self.diagnostics.event(
                        operation_id=self.operation_id, operation_type="analysis",
                        stage="fulltext", state="skip", run_id=run_id,
                        code="AKO-R1-NO_SELECTED", scope="outcome", **identities,
                    )
                emit_progress(progress, ProgressEvent(
                    task_type="analysis", stage="pdf", state="skipped",
                    message="PDF 下载已跳过 · Round 1 零入围", processed=0, total=0,
                ))
                emit_progress(progress, ProgressEvent(
                    task_type="analysis", stage="fulltext", state="skipped",
                    message="全文提取已跳过 · 没有 PDF 任务", processed=0, total=0,
                ))
            # 共享 Round 2 选择器要求 Round 1 已成功；后续任何异常改写为 failed。
            main.finish_run(connection, run_id, "success", "Desktop Round 1 完成", main.current_time_iso())
            stage = "round2"
            stage_timer = StageTimer()
            if papers:
                bundle = run_round2.build_round2_input_bundle(
                    connection, config, profile, round2_prompt,
                    run_id=run_id, fulltext_connection=fulltext,
                )
            else:
                bundle = run_round2.Round2InputBundle(
                    build_round2_inputs.Round2RunSelection(run_id, main.current_time_iso(), 0),
                    [], [], {}, input_mode="no_candidates", uses_fulltext_state=True,
                )
            run_round2.persist_round2_input_decisions(fulltext, bundle)
            if not _sqlite_intact(connection, fulltext):
                raise DesktopOperationError(make_issue("AKD-FULLTEXT-COMPONENT_FAILED"))
            decision_count = fulltext.execute(
                "SELECT COUNT(*) FROM round2_input_decisions"
            ).fetchone()[0]
            if decision_count != len(selected):
                raise DesktopOperationError(make_issue("AKD-FULLTEXT-COMPONENT_FAILED"))
            for code, summary, count in (
                ("AKO-FULLTEXT-LONG_PAPER_EXCLUDED", "长论文按 60 页门控正常排除。", len(bundle.long_reading_papers)),
                ("AKO-FULLTEXT-TOKEN_EXCLUDED", "个别论文按 Token 门控正常排除。", len(bundle.token_budget_excluded_papers)),
            ):
                if count:
                    normal = outcome(code, "fulltext", summary, outcome_count=count)
                    outcomes.append(normal)
                    if self.diagnostics:
                        self.diagnostics.outcome(
                            operation_id=self.operation_id, operation_type="analysis",
                            outcome=normal, run_id=run_id, **identities,
                        )
            if not bundle.papers and selected:
                normal = outcome(
                    "AKO-FULLTEXT-NO_ELIGIBLE", "fulltext",
                    "没有合格全文输入，Round 2 正常跳过。",
                    eligible_count=0,
                )
                outcomes.append(normal)
                if self.diagnostics:
                    self.diagnostics.outcome(
                        operation_id=self.operation_id, operation_type="analysis",
                        outcome=normal, run_id=run_id, **identities,
                    )
            if bundle.papers:
                if self.diagnostics:
                    self.diagnostics.event(
                        operation_id=self.operation_id, operation_type="analysis",
                        stage="round2", state="start", run_id=run_id,
                        counts={"eligible_count": len(bundle.papers)}, **identities,
                    )
                validate_budget(connection, run_id, config, "round2", bundle.messages)
                emit_progress(progress, ProgressEvent(
                    task_type="analysis", stage="round2", state="running",
                    message=f"Round 2 分析中 · 全文输入 {len(bundle.papers)} 篇",
                    processed=0, total=len(bundle.papers),
                ))
            round2_call_result = None

            def observe_round2(kind: str, value: object) -> None:
                nonlocal round2_call_result
                if kind == "call_result":
                    round2_call_result = value
                    details = _call_details(value)
                    observed_stage = "round2_request"
                    observed_state = "complete" if getattr(value, "ok", False) else "fail"
                elif isinstance(value, dict):
                    details = {
                        "validation_code": value.get("code"),
                        "validation_stage": value.get("stage"),
                        "input_mode": value.get("input_mode"),
                        "validation_diagnostic": value,
                    }
                    if kind == "outcome":
                        observed_stage = "round2"
                        observed_state = "outcome"
                    else:
                        observed_stage = (
                            "round2_configuration" if kind == "configuration_failure"
                            else "round2_validation"
                        )
                        observed_state = (
                            "fail" if kind == "configuration_failure" or value.get("code") not in {None, "ok"}
                            else "complete"
                        )
                else:
                    details = {}
                    observed_stage = "round2_validation"
                    observed_state = "complete"
                if self.diagnostics:
                    self.diagnostics.event(
                        operation_id=self.operation_id, operation_type="analysis",
                        stage=observed_stage, state=observed_state, run_id=run_id,
                        details=details, **identities,
                    )

            try:
                round2_result, _, _ = run_round2.run_round2_model_and_save(
                connection, root, config, bundle, api_key_override=api_key,
                    diagnostic_observer=observe_round2,
                )
            except Exception:
                if round2_call_result is not None and (
                    not getattr(round2_call_result, "ok", False)
                    or getattr(round2_call_result, "data", None) is None
                ):
                    raise DesktopOperationError(
                        _model_issue("round2", round2_call_result)
                    ) from None
                raise
            recommendation_count = len(round2_result["final_recommendations"])
            if bundle.papers and recommendation_count == 0:
                normal = outcome(
                    "AKO-R2-NO_RECOMMENDATIONS", "round2",
                    "Round 2 正常完成，零篇推荐。", recommendation_count=0,
                )
                outcomes.append(normal)
                if self.diagnostics:
                    self.diagnostics.outcome(
                        operation_id=self.operation_id, operation_type="analysis",
                        outcome=normal, run_id=run_id, **identities,
                    )
            if bundle.papers:
                emit_progress(progress, ProgressEvent(
                    task_type="analysis", stage="round2", state="completed",
                    message=f"Round 2 完成 · 推荐 {recommendation_count} 篇",
                    processed=len(bundle.papers), total=len(bundle.papers),
                    result_count=recommendation_count,
                ))
            else:
                emit_progress(progress, ProgressEvent(
                    task_type="analysis", stage="round2", state="skipped",
                    message="Round 2 已跳过 · 没有合格全文输入",
                    processed=0, total=0, result_count=0,
                ))
            if self.diagnostics:
                self.diagnostics.event(
                    operation_id=self.operation_id, operation_type="analysis",
                    stage="round2", state="complete" if bundle.papers else "skip",
                    run_id=run_id, elapsed_ms=stage_timer.elapsed_ms,
                    counts={"eligible_count": len(bundle.papers),
                            "recommendation_count": recommendation_count},
                    **identities,
                )
            checked_usage(connection, run_id, config)
            stage = "report"
            stage_timer = StageTimer()
            if self.diagnostics:
                self.diagnostics.event(
                    operation_id=self.operation_id, operation_type="analysis",
                    stage="report", state="start", run_id=run_id, **identities,
                )
            emit_progress(progress, ProgressEvent(
                task_type="analysis", stage="report", state="running",
                message="正在生成日报…",
            ))
            from desktop.report import build_desktop_report
            markdown = build_desktop_report(connection, fulltext_database, run_id, self.snapshot)
            main.finish_run(connection, run_id, "success", "Desktop 分析完成", main.current_time_iso())
            if self.diagnostics:
                self.diagnostics.event(
                    operation_id=self.operation_id, operation_type="analysis",
                    stage="report", state="complete", run_id=run_id,
                    elapsed_ms=stage_timer.elapsed_ms, **identities,
                )
            emit_progress(progress, ProgressEvent(
                task_type="analysis", stage="report", state="completed",
                message="日报生成完成",
            ))
            emit_progress(progress, ProgressEvent(
                task_type="analysis", stage="complete", state="completed",
                message=f"分析完成 · 最终推荐 {recommendation_count} 篇",
                result_count=recommendation_count,
                operation_id=self.operation_id,
                snapshot_id=self.snapshot.snapshot_id,
                fetch_operation_id=self.snapshot.fetch_operation_id or None,
                run_id=run_id,
            ))
            if self.diagnostics:
                self.diagnostics.event(
                    operation_id=self.operation_id, operation_type="analysis",
                    stage="analysis", state="complete", run_id=run_id,
                    elapsed_ms=operation_timer.elapsed_ms,
                    counts={"recommendation_count": recommendation_count,
                            "paper_issue_count": len(paper_issues),
                            "outcome_count": len(outcomes)},
                    **identities,
                )
            return AnalysisResult(
                run_id, markdown, recommendation_count,
                operation_id=self.operation_id,
                snapshot_id=self.snapshot.snapshot_id,
                fetch_operation_id=self.snapshot.fetch_operation_id,
                paper_issues=tuple(paper_issues), outcomes=tuple(outcomes),
                log_relative_path=(self.diagnostics.relative_log_path if self.diagnostics else None),
                diagnostics_persistent=(self.diagnostics.persistent if self.diagnostics else False),
            )
        except Exception as exc:
            issue = _analysis_issue(stage, exc)
            if connection is not None and run_id is not None:
                try:
                    main.finish_run(connection, run_id, "failed", f"Desktop {stage}失败", main.current_time_iso())
                except Exception:
                    pass
            if self.diagnostics:
                self.diagnostics.event(
                    operation_id=self.operation_id, operation_type="analysis",
                    stage=issue.stage, state="fail", code=issue.code, scope=issue.scope,
                    run_id=run_id, elapsed_ms=operation_timer.elapsed_ms,
                    details=issue.details, transient=issue.transient,
                    automatic_retry_permitted=issue.automatic_retry_permitted,
                    unexpected=exc if issue.code.endswith("-UNEXPECTED") else None,
                    **identities,
                )
            emit_progress(progress, ProgressEvent(
                task_type="analysis", stage=stage, state="failed", message=issue.reason,
                code=issue.code, scope=issue.scope,
                operation_id=self.operation_id,
                snapshot_id=self.snapshot.snapshot_id,
                fetch_operation_id=self.snapshot.fetch_operation_id or None,
                run_id=run_id,
            ))
            raise AnalysisError(issue) from None
        finally:
            if fulltext is not None:
                fulltext.close()
            if connection is not None:
                connection.close()
            if work_lock is not None:
                work_lock.close()
