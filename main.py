# Copyright (c) 2026 yyyang20
# SPDX-License-Identifier: GPL-3.0-only
# See LICENSE in the project root for the full license text.

from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
import subprocess
import tempfile
import time
import unicodedata
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import arxiv_transport_evidence
import content_labels
import model_usage
from deepseek_client import DeepSeekClient
from desktop.config import load_config


PROJECT_ROOT = Path(__file__).resolve().parent
ARXIV_API_URL = "https://export.arxiv.org/api/query"
ROUND2_TASK_TYPE = "round2_batch_ranking"
ROUND2_ABSTRACT_SOURCE = "papers.summary"
ROUND2_SELECTION_POLICY = "full_text_budget_exclusion_v3"
ROUND2_OUTPUT_TRANSPORT = "responses_named_tool_auto_v2"
CURRENT_ROUND1_PROMPT_VERSION = "round1_v20"
CURRENT_ROUND2_PROMPT_VERSION = "round2_v15"
ROUND1_PRECISION_PROMPT_VERSIONS = {CURRENT_ROUND1_PROMPT_VERSION}
ROUND2_PRECISION_PROMPT_VERSIONS = {CURRENT_ROUND2_PROMPT_VERSION}
SELF_CONTAINED_ROUND1_PROMPT_VERSIONS = {CURRENT_ROUND1_PROMPT_VERSION}
SELF_CONTAINED_ROUND2_PROMPT_VERSIONS = {CURRENT_ROUND2_PROMPT_VERSION}


def round2_selection_policy_for_prompt(prompt_version: str) -> str:
    """Desktop 只接受 alpha.4 当前全文筛选协议。"""
    if prompt_version != CURRENT_ROUND2_PROMPT_VERSION:
        raise RuntimeError("desktop_prompt_version_unsupported")
    return ROUND2_SELECTION_POLICY


DESKTOP_SCHEMA_VERSION = 1

ATOM_NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "arxiv": "http://arxiv.org/schemas/atom",
}


def current_time_iso() -> str:
    """返回包含本地时区的 ISO 时间。"""
    return datetime.now().astimezone().isoformat(timespec="seconds")


def deepseek_stage_config(
    config: dict[str, Any], stage: str
) -> dict[str, Any]:
    """返回某一筛选阶段的明确模型配置，避免两轮误用同一模型参数。"""
    if stage not in {"round1", "round2"}:
        raise RuntimeError(f"unsupported_deepseek_stage:{stage}")
    deepseek = config.get("deepseek")
    if not isinstance(deepseek, dict):
        raise RuntimeError("deepseek_config_missing")
    stage_config = {
        "model": deepseek[f"{stage}_model"],
        "base_url": deepseek["base_url"],
        "timeout_seconds": deepseek["timeout_seconds"],
        "max_retries": deepseek["max_retries"],
        "thinking_mode": deepseek[f"{stage}_thinking_mode"],
        "reasoning_effort": deepseek[f"{stage}_reasoning_effort"],
        "pricing": deepseek.get(f"{stage}_pricing"),
    }
    if stage == "round1":
        stage_config["max_output_tokens"] = deepseek["round1_max_output_tokens"]
    else:
        stage_config["max_output_tokens"] = deepseek.get(
            "round2_max_output_tokens", 128000
        )
        stage_config["output_transport"] = deepseek.get(
            "round2_output_transport", ROUND2_OUTPUT_TRANSPORT
        )
    return stage_config


def resolve_project_path(project_root: Path, raw_path: str, field_name: str) -> Path:
    """只接受项目根目录内的相对路径，阻止绝对路径和 .. 路径逃逸。"""
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise RuntimeError(f"路径配置 {field_name} 不能为空。")

    relative_path = Path(raw_path)
    if relative_path.is_absolute():
        raise RuntimeError(f"路径配置 {field_name} 必须是项目内的相对路径。")

    project_root = project_root.resolve()
    target = (project_root / relative_path).resolve()
    try:
        target.relative_to(project_root)
    except ValueError as exc:
        raise RuntimeError(
            f"路径配置 {field_name} 指向项目目录之外，已拒绝：{raw_path}"
        ) from exc
    return target


def resolve_configured_paths(
    project_root: Path, config: dict[str, Any]
) -> dict[str, Path]:
    """将配置中的路径解析为经过安全检查的项目内绝对路径。"""
    return {
        name: resolve_project_path(project_root, raw_path, name)
        for name, raw_path in config["paths"].items()
        if name
        in {
            "round1_prompt",
            "round2_prompt",
        }
    }


def research_profile_identity(config: dict[str, Any]) -> dict[str, str]:
    """保留当前请求、缓存与审计协议的画像身份，不读取画像文件。"""
    return {
        "profile_version": str(config["versions"]["research_profile_version"])
    }


def load_prompt(prompt_path: Path) -> str:
    """以 UTF-8 读取提示词，并拒绝空文件。"""
    try:
        prompt = prompt_path.read_text(encoding="utf-8-sig").strip()
    except FileNotFoundError as exc:
        raise RuntimeError("第一轮提示词文件不存在。") from exc
    if not prompt:
        raise RuntimeError("第一轮提示词文件为空。")
    return prompt


def initialize_database_schema(connection: sqlite3.Connection) -> None:
    """初始化 Desktop 当前工作库，不迁移或读取在线历史数据库。"""
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    if version not in (0, DESKTOP_SCHEMA_VERSION):
        raise RuntimeError("desktop_database_schema_version_invalid")
    statements = [
        """
        CREATE TABLE IF NOT EXISTS papers (
            id TEXT,
            arxiv_id TEXT NOT NULL,
            version INTEGER NOT NULL,
            title TEXT NOT NULL,
            authors TEXT,
            summary TEXT,
            categories TEXT,
            primary_category TEXT,
            published TEXT,
            updated TEXT,
            abs_url TEXT,
            pdf_url TEXT,
            created_at TEXT NOT NULL,
            PRIMARY KEY (arxiv_id, version)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS runs (
            run_id INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            status TEXT NOT NULL,
            message TEXT,
            last_success_time TEXT
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS batch_cache (
            cache_key TEXT PRIMARY KEY,
            task_type TEXT NOT NULL,
            model_name TEXT NOT NULL,
            prompt_version TEXT NOT NULL,
            research_profile_version TEXT NOT NULL,
            selection_policy_version TEXT NOT NULL DEFAULT '',
            input_hash TEXT NOT NULL,
            candidate_id_list_hash TEXT NOT NULL,
            feedback_sample_hash TEXT NOT NULL,
            request_summary TEXT NOT NULL DEFAULT '{}',
            response_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS screening_results (
            result_id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL,
            task_type TEXT NOT NULL,
            arxiv_id TEXT NOT NULL,
            version INTEGER NOT NULL,
            result_rank INTEGER,
            recommendation_level TEXT,
            score INTEGER,
            confidence INTEGER,
            is_selected INTEGER NOT NULL,
            reason TEXT,
            details_json TEXT NOT NULL,
            model_name TEXT NOT NULL,
            prompt_version TEXT NOT NULL,
            research_profile_version TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE (run_id, task_type, arxiv_id, version)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS screening_stage_audits (
            run_id INTEGER NOT NULL,
            task_type TEXT NOT NULL,
            prompt_version TEXT NOT NULL,
            selection_policy_version TEXT NOT NULL,
            model_output_count INTEGER NOT NULL,
            considered_count INTEGER NOT NULL,
            accepted_count INTEGER NOT NULL,
            excluded_count INTEGER NOT NULL,
            ignored_over_budget_count INTEGER NOT NULL,
            audit_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (run_id, task_type)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS screening_completion (
            task_type TEXT NOT NULL,
            arxiv_id TEXT NOT NULL,
            version INTEGER NOT NULL,
            run_id INTEGER NOT NULL,
            completion_status TEXT NOT NULL,
            selection_status TEXT NOT NULL,
            model_name TEXT NOT NULL,
            prompt_version TEXT NOT NULL,
            research_profile_version TEXT NOT NULL,
            selection_policy_version TEXT NOT NULL DEFAULT 'legacy_unversioned',
            completed_at TEXT NOT NULL,
            PRIMARY KEY (
                task_type, arxiv_id, version, prompt_version,
                research_profile_version, selection_policy_version
            )
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS pdf_downloads (
            arxiv_id TEXT NOT NULL,
            version INTEGER NOT NULL,
            local_pdf_path TEXT,
            download_status TEXT NOT NULL,
            downloaded_at TEXT,
            failure_reason TEXT,
            PRIMARY KEY (arxiv_id, version)
        )
        """,
    ]
    with connection:
        for statement in statements:
            connection.execute(statement)
        connection.execute("CREATE INDEX IF NOT EXISTS ix_screening_results_paper ON screening_results (arxiv_id, version, task_type)")
        connection.execute("CREATE INDEX IF NOT EXISTS ix_screening_completion_task ON screening_completion (task_type, completion_status)")
        connection.execute("CREATE INDEX IF NOT EXISTS ix_screening_completion_policy ON screening_completion (task_type, completion_status, prompt_version, research_profile_version, selection_policy_version)")
        model_usage.initialize_model_usage_schema(connection, manage_transaction=False)
        connection.execute(f"PRAGMA user_version = {DESKTOP_SCHEMA_VERSION}")


def init_database(database_path: Path) -> sqlite3.Connection:
    """打开本地 SQLite 并初始化当前 Desktop 工作库。"""
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(database_path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 10000")
        initialize_database_schema(connection)
        return connection
    except RuntimeError:
        if connection is not None:
            connection.close()
        raise
    except sqlite3.Error as exc:
        if connection is not None:
            connection.close()
        raise RuntimeError(f"SQLite 数据库初始化失败：{exc}") from exc


def start_run(connection: sqlite3.Connection, started_at: str) -> int:
    """创建当前 attempt 的 run；工作库不用于跨运行恢复或历史回退。"""
    cursor = connection.execute(
        "INSERT INTO runs (started_at, status, message, last_success_time) VALUES (?, 'running', ?, NULL)",
        (started_at, "程序正在运行"),
    )
    connection.commit()
    return int(cursor.lastrowid)


def finish_run(
    connection: sqlite3.Connection,
    run_id: int,
    status: str,
    message: str,
    finished_at: str,
) -> None:
    """更新本次运行状态；成功时刷新 last_success_time。"""
    last_success_time = finished_at if status == "success" else None
    if status == "success":
        connection.execute(
            """
            UPDATE runs
            SET finished_at = ?, status = ?, message = ?, last_success_time = ?
            WHERE run_id = ?
            """,
            (finished_at, status, message, last_success_time, run_id),
        )
    else:
        connection.execute(
            """
            UPDATE runs
            SET finished_at = ?, status = ?, message = ?
            WHERE run_id = ?
            """,
            (finished_at, status, message, run_id),
        )
    connection.commit()


def build_arxiv_query_url(
    category: str, submission_date: date, max_results: int, start: int = 0
) -> str:
    """查询覆盖北京时间自然日的 UTC 区间；结束点由 published 精确过滤。"""
    lower, upper = beijing_submission_bounds(submission_date)
    parameters = {
        "search_query": (
            f"cat:{category.strip()} AND "
            f"submittedDate:[{lower:%Y%m%d%H%M} TO {upper:%Y%m%d%H%M}]"
        ),
        "sortBy": "submittedDate",
        "sortOrder": "descending",
        "start": start,
        "max_results": max_results,
    }
    return f"{ARXIV_API_URL}?{urllib.parse.urlencode(parameters)}"


def merge_papers_by_identity(
    paper_groups: list[list[dict[str, Any]]],
) -> tuple[list[dict[str, Any]], int]:
    """按 arXiv ID 和版本合并多个分类结果，并统计跨分类重复项。"""
    merged: dict[tuple[str, int], dict[str, Any]] = {}
    source_count = 0
    for papers in paper_groups:
        for paper in papers:
            source_count += 1
            key = (paper["arxiv_id"], int(paper["version"]))
            previous = merged.get(key)
            if previous is None or paper.get("updated", "") > previous.get("updated", ""):
                merged[key] = paper
    merged_papers = sorted(
        merged.values(), key=lambda paper: paper.get("updated", ""), reverse=True
    )
    return merged_papers, source_count - len(merged_papers)


def beijing_submission_bounds(day: date) -> tuple[datetime, datetime]:
    """自然日使用时区转换，不将 UTC 日期机械改名为北京时间日期。"""
    from zoneinfo import ZoneInfo
    lower = datetime.combine(day, datetime.min.time(), ZoneInfo("Asia/Shanghai"))
    upper = datetime.combine(day + timedelta(days=1), datetime.min.time(), lower.tzinfo)
    return lower.astimezone(timezone.utc), upper.astimezone(timezone.utc)


def parse_submission_timestamp(value: Any) -> datetime | None:
    """Atom 时间必须携带时区；无法确认时间的条目不进入目标日期。"""
    if not isinstance(value, str) or not value.strip():
        return None
    normalized = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


class ArxivFeedPage:
    """保留原始条目数量，避免无效元数据或日期过滤造成漏页。"""

    def __init__(self, xml_data: bytes, logger: logging.Logger):
        try:
            root = ET.fromstring(xml_data)
        except ET.ParseError:
            raise ValueError("arxiv_atom_invalid") from None
        if root.tag != "{http://www.w3.org/2005/Atom}feed":
            raise ValueError("arxiv_atom_not_feed")
        entries = root.findall("atom:entry", ATOM_NS)
        if any("/api/errors" in element_text(entry.find("atom:id", ATOM_NS)) for entry in entries):
            raise ValueError("arxiv_atom_api_error")
        namespace = "{http://a9.com/-/spec/opensearch/1.1/}"
        values = [root.find(namespace + name) for name in ("startIndex", "totalResults", "itemsPerPage")]
        if any(value is not None for value in values) and not all(value is not None for value in values):
            raise ValueError("arxiv_atom_paging_incomplete")
        self.start = self.total = self.page_size = None
        if all(value is not None for value in values):
            self.start, self.total, self.page_size = (int(value.text) for value in values)
            if min(self.start, self.total, self.page_size) < 0:
                raise ValueError("arxiv_atom_paging_invalid")
        self.raw_count = len(entries)
        self.fingerprint = hashlib.sha256(b"".join(ET.tostring(entry) for entry in entries)).hexdigest()
        self.papers = parse_arxiv_feed(xml_data, logger)


def fetch_category_submission_pages(
    *,
    category: str,
    submission_date: date,
    page_size: int,
    fetch_page: Callable[[str, date, int, int], ArxivFeedPage],
    logger: logging.Logger,
) -> list[dict[str, Any]]:
    """分页读取单分类目标 submittedDate；分页失败时抛出异常，不返回部分结果。"""
    if page_size < 1:
        raise RuntimeError("arXiv 分页大小必须大于 0。")

    papers: list[dict[str, Any]] = []
    lower, upper = beijing_submission_bounds(submission_date)
    seen_pages: set[str] = set()
    start = 0
    while True:
        page = fetch_page(category, submission_date, start, page_size)
        if page.start is not None and page.start != start:
            raise ValueError("arxiv_atom_paging_start_mismatch")
        if (page.raw_count > page_size
                or (page.page_size is not None and page.raw_count > page.page_size)
                or (page.total is not None and start + page.raw_count > page.total)):
            raise ValueError("arxiv_atom_paging_count_invalid")
        if not page.raw_count:
            if page.total is not None and start < page.total:
                raise ValueError("arxiv_atom_paging_empty_before_end")
            logger.info(
                "日期 %s 分类 %s 分页 start=%d 返回空页，停止分页。",
                submission_date.isoformat(),
                category,
                start,
            )
            break

        if page.fingerprint in seen_pages:
            raise ValueError("arxiv_atom_paging_repeated")
        seen_pages.add(page.fingerprint)
        exact = [paper for paper in page.papers
                 if (timestamp := parse_submission_timestamp(paper.get("published"))) is not None
                 and lower <= timestamp < upper]
        papers.extend(exact)
        exact_date_count = len(exact)
        off_date_count = len(page.papers) - exact_date_count
        logger.info(
            "日期 %s 分类 %s 分页 start=%d 获取并解析论文数量：%d，"
            "目标日期数量：%d，非目标日期数量：%d",
            submission_date.isoformat(),
            category,
            start,
            len(page.papers),
            exact_date_count,
            off_date_count,
        )

        # 有分页元数据时按原始 offset/total 读取；无元数据时按原始页长停止。
        start += page.raw_count
        if (page.total is not None and start >= page.total) or (page.total is None and page.raw_count < page_size):
            break

    return papers


def respect_request_interval(
    cache_dir: Path, interval_seconds: float, logger: logging.Logger
) -> None:
    """跨多次运行记录最近请求时间，确保相邻请求至少间隔三秒。"""
    timestamp_path = cache_dir / "last_request_time.txt"
    now = time.time()
    if timestamp_path.exists():
        try:
            previous = float(timestamp_path.read_text(encoding="utf-8").strip())
            remaining = interval_seconds - (now - previous)
            if remaining > 0:
                logger.info("遵守 arXiv 请求间隔，等待 %.1f 秒", remaining)
                time.sleep(remaining)
        except (OSError, ValueError) as exc:
            logger.warning(
                "无法读取上次请求时间，将继续单次请求：%s",
                safe_error_message(exc),
            )

    try:
        timestamp_path.write_text(
            f"{time.time():.6f}\n", encoding="utf-8", newline="\n"
        )
    except OSError as exc:
        raise RuntimeError(f"无法在项目缓存目录记录请求时间：{exc}") from exc


def _arxiv_http_failure_diagnostic(
    body: bytes, status: int, attempt_no: int, max_attempts: int
) -> dict[str, Any]:
    """仅从有限字节样本生成固定字段，绝不把响应原文拼入日志。"""
    sample = body[:8192]
    lowered = sample.lower()
    markers = (
        (b"<html", "html"),
        (b"<?xml", "xml"),
        (b"not acceptable", "not_acceptable"),
        (b"access denied", "access_denied"),
        (b"request blocked", "request_blocked"),
        (b"rate exceeded", "rate_exceeded"),
        (b"captcha", "captcha"),
    )
    return {
        "http_status": status,
        "attempt": attempt_no,
        "max_attempts": max_attempts,
        "body_bytes": len(body),
        "sample_bytes": len(sample),
        "sample_truncated": len(body) > len(sample),
        "sample_sha256": hashlib.sha256(sample).hexdigest(),
        "markers": [label for needle, label in markers if needle in lowered],
    }


def _build_arxiv_http_failure_diagnostic(
    body: bytes, status: int, attempt_no: int, max_attempts: int
) -> str:
    diagnostic = _arxiv_http_failure_diagnostic(body, status, attempt_no, max_attempts)
    message = "ARXIV_METADATA_DIAGNOSTIC=" + json.dumps(
        diagnostic, ensure_ascii=True, separators=(",", ":")
    )
    # 为现有日志级别前缀留出空间；异常由调用处隔离，不改变 HTTP 失败处理。
    if len(message.encode("ascii")) > 1000:
        raise ValueError("arxiv_metadata_diagnostic_too_large")
    return message


def _prepare_arxiv_response_header_capture(cache_dir: Path) -> Path | None:
    """在项目缓存目录内准备单次响应头文件；任何异常都降级为不采集。"""
    stream = None
    path: Path | None = None
    try:
        parent = cache_dir.resolve()
        stream = tempfile.NamedTemporaryFile(
            mode="w+b",
            dir=parent,
            prefix=".arxiv-metadata-headers-",
            suffix=".tmp",
            delete=False,
        )
        path = Path(stream.name).resolve()
        stream.close()
        stream = None
        if path.parent != parent:
            _discard_arxiv_response_header_capture(path)
            return None
        return path
    except Exception:
        try:
            if stream is not None:
                stream.close()
        except Exception:
            pass
        _discard_arxiv_response_header_capture(path)
        return None


def _discard_arxiv_response_header_capture(path: Path | None) -> None:
    """先截断再删除原始响应头；清理失败不得改变业务结果。"""
    if path is None:
        return
    try:
        path.write_bytes(b"")
    except Exception:
        pass
    try:
        path.unlink(missing_ok=True)
    except Exception:
        pass


def _arxiv_transport_diagnostic(
    *,
    transport_payload: bytes | None,
    header_path: Path | None,
    status: int,
    attempt_no: int,
    max_attempts: int,
    timeout_seconds: float,
) -> dict[str, Any]:
    """只输出同次 curl 的定长字段和响应头白名单。"""
    network = (
        arxiv_transport_evidence.parse_write_out(
            transport_payload, timeout_seconds=timeout_seconds
        )
        if transport_payload is not None
        else None
    )
    if network is not None and network["http_status"] != status:
        network = None
    if header_path is None:
        header_status, headers, rejected = "unavailable", {}, []
    else:
        header_status, headers, rejected = (
            arxiv_transport_evidence.parse_response_headers(
                header_path,
                header_value_limit=128,
                header_value_count_limit=1,
            )
        )
    diagnostic = {
        "http_status": status,
        "attempt": attempt_no,
        "max_attempts": max_attempts,
        "write_out_capture": "captured" if network is not None else "invalid",
        "remote_ip": network["remote_ip"] if network is not None else None,
        "remote_ip_version": (
            network["remote_ip_version"] if network is not None else None
        ),
        "remote_port": network["remote_port"] if network is not None else None,
        "http_version": network["http_version"] if network is not None else None,
        "ssl_verify_result": (
            network["ssl_verify_result"] if network is not None else None
        ),
        "num_redirects": network["num_redirects"] if network is not None else None,
        "timings_seconds": network["timings_seconds"] if network is not None else None,
        "response_header_capture": header_status,
        "response_headers": headers,
        "response_headers_rejected": rejected,
    }
    return diagnostic


def _build_arxiv_transport_diagnostic(
    *,
    transport_payload: bytes | None,
    header_path: Path | None,
    status: int,
    attempt_no: int,
    max_attempts: int,
    timeout_seconds: float,
) -> str:
    diagnostic = _arxiv_transport_diagnostic(
        transport_payload=transport_payload,
        header_path=header_path,
        status=status,
        attempt_no=attempt_no,
        max_attempts=max_attempts,
        timeout_seconds=timeout_seconds,
    )
    prefix = "ARXIV_METADATA_TRANSPORT_DIAGNOSTIC="
    message = prefix + json.dumps(
        diagnostic, ensure_ascii=True, separators=(",", ":")
    )
    if len(message.encode("ascii")) > 4096:
        # 保留传输数值，必要时整体丢弃响应头值。
        diagnostic.update(
            response_header_capture="omitted_size",
            response_headers={},
            response_headers_rejected=[],
        )
        message = prefix + json.dumps(
            diagnostic, ensure_ascii=True, separators=(",", ":")
        )
    if len(message.encode("ascii")) > 4096:
        raise ValueError("arxiv_metadata_transport_diagnostic_too_large")
    return message


def fetch_arxiv_metadata(
    url: str,
    timeout_seconds: float,
    cache_dir: Path,
    interval_seconds: float,
    logger: logging.Logger,
    *,
    max_attempts: int = 1,
    retry_backoff_seconds: float = 0,
    curl_executable: str = "curl",
    curl_extra_args: tuple[str, ...] = (),
    subprocess_creationflags: int = 0,
    emit_success_transport_diagnostic: bool = False,
    diagnostic_observer: Callable[[str, dict[str, Any]], None] | None = None,
) -> bytes:
    """通过官方 API 获取 Atom XML；仅对明确的瞬态失败有界重试。"""
    if (
        isinstance(max_attempts, bool)
        or not isinstance(max_attempts, int)
        or not 1 <= max_attempts <= 2
    ):
        raise RuntimeError("arxiv_metadata_max_attempts_invalid")
    if (
        isinstance(retry_backoff_seconds, bool)
        or not isinstance(retry_backoff_seconds, (int, float))
        or retry_backoff_seconds < 0
        or (max_attempts == 2 and retry_backoff_seconds < 3)
    ):
        raise RuntimeError("arxiv_metadata_retry_backoff_invalid")
    curl_status_marker = b"\n__ARXIV_KALEID_CURL_STATUS__:"
    curl_transport_marker = b"\n__ARXIV_KALEID_CURL_TRANSPORT__:"
    for attempt_no in range(1, max_attempts + 1):
        respect_request_interval(cache_dir, interval_seconds, logger)
        logger.info(
            "arXiv 请求 URL（尝试 %s/%s）：%s",
            attempt_no,
            max_attempts,
            url,
        )
        error_code: str | None = None
        retryable = False
        cause: Exception | None = None
        try:
            header_path = _prepare_arxiv_response_header_capture(cache_dir)
        except Exception:
            header_path = None
        try:
            completed = subprocess.run(
                [
                    curl_executable,
                    *curl_extra_args,
                    "--http1.1",
                    "--silent",
                    "--show-error",
                    "--max-time",
                    str(timeout_seconds),
                    "--user-agent",
                    "arXivKaleid/1.0 (local academic research tool)",
                    "--header",
                    "Accept: application/atom+xml",
                    *(
                        ("--dump-header", str(header_path))
                        if header_path is not None
                        else ()
                    ),
                    "--write-out",
                    curl_status_marker.decode("ascii")
                    + "%{http_code}"
                    + curl_transport_marker.decode("ascii")
                    + arxiv_transport_evidence.CURL_WRITE_OUT,
                    url,
                ],
                check=False,
                capture_output=True,
                timeout=timeout_seconds,
                creationflags=subprocess_creationflags,
            )
            if completed.returncode == 28:
                error_code = "arxiv_api_timeout"
                retryable = True
                cause = RuntimeError("curl_timeout")
                if diagnostic_observer is not None:
                    try:
                        diagnostic_observer(
                            "curl_failure",
                            {
                                "attempt": attempt_no,
                                "max_attempts": max_attempts,
                                "curl_exit_code": completed.returncode,
                                "error_type": error_code,
                            },
                        )
                    except Exception:
                        pass
            else:
                if completed.returncode != 0:
                    error_code = "arxiv_api_connection_failed"
                    cause = RuntimeError("curl_connection_failed")
                    if diagnostic_observer is not None:
                        try:
                            diagnostic_observer(
                                "curl_failure",
                                {
                                    "attempt": attempt_no,
                                    "max_attempts": max_attempts,
                                    "curl_exit_code": completed.returncode,
                                    "error_type": error_code,
                                },
                            )
                        except Exception:
                            pass
                else:
                    stdout = completed.stdout or b""
                    body, marker, status_text = stdout.rpartition(curl_status_marker)
                    if not marker:
                        error_code = "arxiv_api_connection_failed"
                        cause = RuntimeError("curl_status_missing")
                    else:
                        try:
                            status = int(status_text.strip().splitlines()[0])
                        except (IndexError, ValueError) as exc:
                            error_code = "arxiv_api_connection_failed"
                            cause = exc
                        else:
                            logger.info("arXiv HTTP 状态：%s", status)
                            if status != 200:
                                error_code = f"arxiv_api_http_{status}"
                                retryable = status == 429 or 500 <= status <= 599
                                try:
                                    diagnostic = _arxiv_http_failure_diagnostic(
                                        body, status, attempt_no, max_attempts
                                    )
                                    if diagnostic_observer is not None:
                                        diagnostic_observer("http_response", diagnostic)
                                except Exception:
                                    pass
                                # 诊断只是附加日志；生成或写日志失败都不得覆盖原错误。
                                try:
                                    logger.warning(
                                        "%s",
                                        _build_arxiv_http_failure_diagnostic(
                                            body, status, attempt_no, max_attempts
                                        ),
                                    )
                                except Exception:
                                    pass
                            if status != 200 or emit_success_transport_diagnostic:
                                try:
                                    _, transport_marker, transport_payload = (
                                        status_text.partition(curl_transport_marker)
                                    )
                                    diagnostic = _arxiv_transport_diagnostic(
                                        transport_payload=(
                                            transport_payload
                                            if transport_marker
                                            else None
                                        ),
                                        header_path=header_path,
                                        status=status,
                                        attempt_no=attempt_no,
                                        max_attempts=max_attempts,
                                        timeout_seconds=timeout_seconds,
                                    )
                                    if diagnostic_observer is not None:
                                        diagnostic_observer("transport", diagnostic)
                                    logger.warning(
                                        "%s",
                                        _build_arxiv_transport_diagnostic(
                                            transport_payload=(
                                                transport_payload
                                                if transport_marker
                                                else None
                                            ),
                                            header_path=header_path,
                                            status=status,
                                            attempt_no=attempt_no,
                                            max_attempts=max_attempts,
                                            timeout_seconds=timeout_seconds,
                                        ),
                                    )
                                except Exception:
                                    pass
                            if status == 200:
                                return body
        except FileNotFoundError as exc:
            error_code = "arxiv_curl_unavailable"
            cause = exc
        except subprocess.TimeoutExpired as exc:
            error_code = "arxiv_api_timeout"
            retryable = True
            cause = exc
        except OSError as exc:
            error_code = "arxiv_api_connection_failed"
            cause = exc
        except TimeoutError as exc:
            error_code = "arxiv_api_timeout"
            retryable = True
            cause = exc
        finally:
            try:
                _discard_arxiv_response_header_capture(header_path)
            except Exception:
                pass

        if retryable and attempt_no < max_attempts:
            logger.warning(
                "arXiv 元数据瞬态失败 %s；%.0f 秒后进行唯一一次重试",
                error_code,
                retry_backoff_seconds,
            )
            time.sleep(retry_backoff_seconds)
            continue
        raise RuntimeError(error_code or "arxiv_api_unknown_failure") from cause

    raise RuntimeError("arxiv_api_attempt_boundary_invalid")


def clean_display_text(text: str) -> str:
    """仅规范换行和连续空白，不翻译或改写论文原文。"""
    return " ".join(text.split())


def element_text(element: ET.Element | None) -> str:
    """读取 XML 元素的全部文本并规范空白。"""
    if element is None:
        return ""
    return clean_display_text("".join(element.itertext()))


def extract_arxiv_id_and_version(
    entry_id: str, logger: logging.Logger
) -> tuple[str, int]:
    """兼容新旧 arXiv ID，并从末尾提取版本号。"""
    parsed = urllib.parse.urlparse(entry_id)
    path = urllib.parse.unquote(parsed.path).strip("/")
    full_id = path.split("abs/", 1)[-1] if "abs/" in path else path
    if not full_id:
        full_id = entry_id.rstrip("/").rsplit("/", 1)[-1]

    match = re.match(r"^(.+?)v(\d+)$", full_id)
    if match:
        return match.group(1), int(match.group(2))

    logger.warning("无法从 arXiv ID 提取版本号，默认使用 v1：%s", entry_id)
    return full_id, 1


def parse_arxiv_feed(xml_data: bytes, logger: logging.Logger) -> list[dict[str, Any]]:
    """解析 arXiv Atom feed 中的论文元数据。"""
    try:
        root = ET.fromstring(xml_data)
    except ET.ParseError as exc:
        raise RuntimeError(f"arXiv Atom XML 解析失败：{exc}") from exc

    papers: list[dict[str, Any]] = []
    for entry in root.findall("atom:entry", ATOM_NS):
        entry_id = element_text(entry.find("atom:id", ATOM_NS))
        title = element_text(entry.find("atom:title", ATOM_NS))
        if not entry_id or not title:
            logger.warning("跳过缺少 ID 或标题的 Atom entry。")
            continue

        arxiv_id, version = extract_arxiv_id_and_version(entry_id, logger)
        authors = [
            element_text(author.find("atom:name", ATOM_NS))
            for author in entry.findall("atom:author", ATOM_NS)
        ]
        authors = [author for author in authors if author]

        categories = [
            category.attrib.get("term", "").strip()
            for category in entry.findall("atom:category", ATOM_NS)
        ]
        categories = [category for category in categories if category]
        primary_element = entry.find("arxiv:primary_category", ATOM_NS)
        primary_category = (
            primary_element.attrib.get("term", "").strip()
            if primary_element is not None
            else ""
        )
        if not primary_category and categories:
            primary_category = categories[0]

        abs_url = ""
        pdf_url = ""
        for link in entry.findall("atom:link", ATOM_NS):
            href = link.attrib.get("href", "").strip()
            rel = link.attrib.get("rel", "").strip()
            link_type = link.attrib.get("type", "").strip()
            title_attribute = link.attrib.get("title", "").strip()
            if rel == "alternate" and href:
                abs_url = href
            if href and (
                title_attribute == "pdf"
                or link_type == "application/pdf"
                or "/pdf/" in href
            ):
                pdf_url = href

        papers.append(
            {
                "id": entry_id,
                "arxiv_id": arxiv_id,
                "version": version,
                "title": title,
                "authors": "; ".join(authors),
                "summary": element_text(entry.find("atom:summary", ATOM_NS)),
                "categories": ", ".join(categories),
                "primary_category": primary_category,
                "published": element_text(entry.find("atom:published", ATOM_NS)),
                "updated": element_text(entry.find("atom:updated", ATOM_NS)),
                "abs_url": abs_url or entry_id,
                "pdf_url": pdf_url,
            }
        )
    return papers


def insert_papers(
    connection: sqlite3.Connection, papers: list[dict[str, Any]]
) -> tuple[int, int]:
    """按 arXiv ID 和版本号插入，重复记录自动跳过。"""
    inserted = 0
    created_at = current_time_iso()
    try:
        for paper in papers:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO papers (
                    id, arxiv_id, version, title, authors, summary, categories,
                    primary_category, published, updated, abs_url, pdf_url, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    paper["id"],
                    paper["arxiv_id"],
                    paper["version"],
                    paper["title"],
                    paper["authors"],
                    paper["summary"],
                    paper["categories"],
                    paper["primary_category"],
                    paper["published"],
                    paper["updated"],
                    paper["abs_url"],
                    paper["pdf_url"],
                    created_at,
                ),
            )
            inserted += max(cursor.rowcount, 0)
        connection.commit()
    except sqlite3.Error as exc:
        connection.rollback()
        raise RuntimeError(f"SQLite 写入论文元数据失败：{exc}") from exc
    return inserted, len(papers) - inserted


def build_round1_messages(
    prompt: str,
    research_profile: dict[str, Any],
    papers: list[dict[str, Any]],
    config: dict[str, Any],
) -> list[dict[str, str]]:
    """构造第一轮同批输入，只包含规范允许发送的公开论文元数据。"""
    candidates = [
        {
            "candidate_index": candidate_index,
            "arxiv_id": paper["arxiv_id"],
            "version": f"v{paper['version']}",
            "title": paper["title"],
            "authors": paper.get("authors", ""),
            "categories": paper.get("categories", ""),
            "summary": paper.get("summary", ""),
        }
        for candidate_index, paper in enumerate(papers, start=1)
    ]
    prompt_version = config["versions"]["round1_prompt_version"]
    target_selected_count = min(config["round1_max_selected_n"], len(candidates))
    task_input = {
        "task_type": "round1_abstract_screening",
        "profile_version": research_profile["profile_version"],
        "research_profile_version": research_profile["profile_version"],
        "prompt_version": prompt_version,
        "selection_policy": config["round1_selection_policy"],
        "selection_policy_version": config["round1_selection_policy_version"],
        "max_selected_count": config["round1_max_selected_n"],
        "target_selected_count": target_selected_count,
        "expected_candidate_ranking_count": len(candidates),
        "research_profile": research_profile,
        "feedback_samples": [],
        "candidate_papers": candidates,
    }
    if prompt_version in SELF_CONTAINED_ROUND1_PROMPT_VERSIONS:
        task_input.pop("research_profile")
    if prompt_version in ROUND1_PRECISION_PROMPT_VERSIONS:
        # v19/v20 将 Top 10 视为上限，输出不再承担全候选排序和精确数量回显。
        task_input.pop("target_selected_count")
        task_input.pop("expected_candidate_ranking_count")
    return [
        {"role": "system", "content": prompt},
        {
            "role": "user",
            "content": json.dumps(task_input, ensure_ascii=False, separators=(",", ":")),
        },
    ]


def safe_round2_string_list(
    value: Any, *, max_items: int = 8, max_chars: int = 500
) -> list[str]:
    """限制进入第二轮 payload 的第一轮解释列表，避免异常长字段膨胀输入。"""
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for item in value:
        if isinstance(item, str) and item.strip():
            result.append(item.strip()[:max_chars])
        if len(result) >= max_items:
            break
    return result


def build_round2_candidate_payload(paper: dict[str, Any]) -> dict[str, Any]:
    """构造当前第二轮完整全文输入。"""
    payload = {
        "arxiv_id": paper["arxiv_id"],
        "version": f"v{paper['version']}",
        "title": paper.get("title", ""),
        "authors": paper.get("authors", ""),
        "categories": paper.get("categories", ""),
        "primary_category": paper.get("primary_category", ""),
        "abstract_source": ROUND2_ABSTRACT_SOURCE,
        "metadata_abstract": paper.get("summary", ""),
        "round1": {
            "rank": paper.get("round1_rank"),
            "reason": paper.get("round1_reason", paper.get("reason", "")),
            "matched_reasons": safe_round2_string_list(
                paper.get("matched_reasons")
            ),
            "negative_reasons": safe_round2_string_list(
                paper.get("negative_reasons")
            ),
            "evidence_from_title_or_abstract": safe_round2_string_list(
                paper.get("evidence_from_title_or_abstract")
            ),
            **(
                {"content_label": paper["content_label"]}
                if paper.get("content_label") in content_labels.CORE_CONTENT_LABEL_SET
                else {}
            ),
        },
    }
    if paper.get("score") is not None:
        payload["round1"]["score"] = paper["score"]
    if paper.get("confidence") is not None:
        payload["round1"]["confidence"] = paper["confidence"]
    if paper.get("round2_input_mode") != "full_text":
        raise RuntimeError("round2_fulltext_required")
    raw_pages = paper.get("pdf_pages")
    pages = raw_pages if isinstance(raw_pages, list) else []
    payload["pdf_full_text"] = {
        "extraction_status": paper.get("pdf_extraction_status", ""),
        "page_count": int(paper.get("pdf_page_count") or 0),
        "pages": [
            {
                "page_number": int(page.get("page_number") or 0),
                "text": str(page.get("page_text") or page.get("text") or ""),
            }
            for page in pages
            if isinstance(page, dict)
        ],
    }
    return payload


def build_round2_messages(
    prompt: str,
    research_profile: dict[str, Any],
    papers: list[dict[str, Any]],
    config: dict[str, Any],
) -> list[dict[str, str]]:
    """构造第二轮同批排序输入；不包含本地路径和被页数门控的长文。"""
    input_mode = "full_text"
    prompt_version = config["versions"]["round2_prompt_version"]
    target_recommendation_count = min(
        config["final_max_recommendations"], len(papers)
    )
    task_input = {
        "task_type": ROUND2_TASK_TYPE,
        "selection_policy": round2_selection_policy_for_prompt(prompt_version),
        "profile_version": research_profile["profile_version"],
        "research_profile_version": research_profile["profile_version"],
        "prompt_version": prompt_version,
        "abstract_source": ROUND2_ABSTRACT_SOURCE,
        "input_mode": input_mode,
        "pdf_input_fields": ["page_number", "text"],
        "final_max_recommendations": config["final_max_recommendations"],
        "max_recommendation_count": config["final_max_recommendations"],
        "target_recommendation_count": target_recommendation_count,
        "research_profile": research_profile,
        "candidate_papers": [
            build_round2_candidate_payload(paper) for paper in papers
        ],
    }
    if prompt_version in SELF_CONTAINED_ROUND2_PROMPT_VERSIONS:
        task_input.pop("research_profile")
    if prompt_version in ROUND2_PRECISION_PROMPT_VERSIONS:
        # v14/v15 允许少选或零选，数量由安全解析后的数组确定。
        task_input.pop("max_recommendation_count")
        task_input.pop("target_recommendation_count")
    return [
        {"role": "system", "content": prompt},
        {
            "role": "user",
            "content": json.dumps(task_input, ensure_ascii=False, separators=(",", ":")),
        },
    ]


def summarize_round2_request_messages(
    messages: list[dict[str, str]]
) -> dict[str, Any]:
    """返回可打印的第二轮请求摘要，只包含结构和长度，不包含正文。"""
    user_payload = json.loads(messages[1]["content"])
    candidates = user_payload.get("candidate_papers", [])
    candidate_summaries: list[dict[str, Any]] = []
    if isinstance(candidates, list):
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            pdf_full_text = candidate.get("pdf_full_text")
            if not isinstance(pdf_full_text, dict):
                pdf_full_text = {}
            raw_pages = pdf_full_text.get("pages")
            pages = raw_pages if isinstance(raw_pages, list) else []
            candidate_summaries.append(
                {
                    "arxiv_id": candidate.get("arxiv_id"),
                    "version": candidate.get("version"),
                    "candidate_fields": sorted(candidate.keys()),
                    "pdf_full_text_fields": sorted(pdf_full_text.keys()),
                    "pdf_page_count": pdf_full_text.get("page_count"),
                    "pdf_page_text_chars": sum(
                        len(str(page.get("text") or ""))
                        for page in pages
                        if isinstance(page, dict)
                    ),
                    "field_lengths": {
                        "title": len(str(candidate.get("title") or "")),
                        "metadata_abstract": len(
                            str(candidate.get("metadata_abstract") or "")
                        ),
                    },
                }
            )
    return {
        "message_roles": [message.get("role") for message in messages],
        "system_prompt_chars": len(messages[0].get("content", "")),
        "user_payload_chars": len(messages[1].get("content", "")),
        "total_chars": sum(len(message.get("content", "")) for message in messages),
        "task_type": user_payload.get("task_type"),
        "selection_policy": user_payload.get("selection_policy"),
        "prompt_version": user_payload.get("prompt_version"),
        "abstract_source": user_payload.get("abstract_source"),
        "input_mode": user_payload.get("input_mode"),
        "pdf_input_fields": user_payload.get("pdf_input_fields"),
        "target_recommendation_count": user_payload.get(
            "target_recommendation_count"
        ),
        "candidate_count": len(candidates) if isinstance(candidates, list) else 0,
        "candidate_summaries": candidate_summaries,
    }


def parse_model_version(value: Any) -> int | None:
    """接受模型输出中的 v1 或整数 1，并统一为数据库整数版本。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value >= 1:
        return value
    if isinstance(value, str):
        match = re.fullmatch(r"v?(\d+)", value.strip(), flags=re.IGNORECASE)
        if match:
            return int(match.group(1))
    return None


def strict_round2_text(value: Any, *, max_chars: int) -> str | None:
    """v11 短文本字段必须是非空字符串，禁止类型强转或静默截断。"""
    if not isinstance(value, str):
        return None
    normalized = " ".join(value.split())
    if not normalized or len(normalized) > max_chars:
        return None
    return normalized


ROUND1_EVIDENCE_SOURCES = {"metadata_title", "metadata_abstract"}
ROUND1_EVIDENCE_UNGROUNDED_ERROR = "evidence quote 不在标注的标题或摘要中"
ROUND1_EVIDENCE_TOO_SHORT_ERROR = "evidence quote 过短"
ROUND1_EVIDENCE_TOO_LONG_ERROR = "evidence quote 过长"
ROUND1_EVIDENCE_REPAIR_REASON_CODES = {
    ROUND1_EVIDENCE_UNGROUNDED_ERROR: "evidence_quote_not_in_source",
    ROUND1_EVIDENCE_TOO_SHORT_ERROR: "evidence_quote_too_short",
    ROUND1_EVIDENCE_TOO_LONG_ERROR: "evidence_quote_too_long",
}


def normalize_round1_evidence_text(value: object) -> str:
    """仅统一 Unicode 与空白；不放宽改写、翻译或跨字段拼接。"""
    return " ".join(unicodedata.normalize("NFKC", str(value or "")).split())


def validate_round1_evidence(
    value: object,
    paper: dict[str, Any],
) -> tuple[list[dict[str, str]], str | None, dict[str, Any] | None]:
    """核对 Top 10 证据确实逐字来自相应标题或摘要字段。"""
    if not isinstance(value, list) or not 1 <= len(value) <= 3:
        return [], "evidence 必须是包含 1 至 3 项的数组", None
    source_texts = {
        "metadata_title": normalize_round1_evidence_text(paper.get("title")),
        "metadata_abstract": normalize_round1_evidence_text(paper.get("summary")),
    }
    validated: list[dict[str, str]] = []
    for evidence_item_index, item in enumerate(value, start=1):
        if not isinstance(item, dict) or set(item) != {"source", "quote"}:
            return [], "evidence 每项字段必须严格为 source、quote", None
        source = item.get("source")
        quote = item.get("quote")
        if source not in ROUND1_EVIDENCE_SOURCES:
            return [], "evidence source 非法", None
        if not isinstance(quote, str) or not quote.strip():
            return [], "evidence quote 为空或类型非法", None
        cleaned_quote = normalize_round1_evidence_text(quote)
        quote_length = len(cleaned_quote)
        safe_detail = {
            "evidence_item_index": evidence_item_index,
            "source": str(source),
            "normalized_quote_length": quote_length,
        }
        if quote_length < 8:
            return [], ROUND1_EVIDENCE_TOO_SHORT_ERROR, {
                **safe_detail,
                "reason_code": ROUND1_EVIDENCE_REPAIR_REASON_CODES[
                    ROUND1_EVIDENCE_TOO_SHORT_ERROR
                ],
            }
        if quote_length > 300:
            return [], ROUND1_EVIDENCE_TOO_LONG_ERROR, {
                **safe_detail,
                "reason_code": ROUND1_EVIDENCE_REPAIR_REASON_CODES[
                    ROUND1_EVIDENCE_TOO_LONG_ERROR
                ],
            }
        if cleaned_quote not in source_texts[str(source)]:
            return [], ROUND1_EVIDENCE_UNGROUNDED_ERROR, {
                **safe_detail,
                "reason_code": ROUND1_EVIDENCE_REPAIR_REASON_CODES[
                    ROUND1_EVIDENCE_UNGROUNDED_ERROR
                ],
            }
        validated.append({"source": str(source), "quote": cleaned_quote})
    evidence_keys = {
        (item["source"], item["quote"].casefold()) for item in validated
    }
    if len(evidence_keys) != len(validated):
        return [], "evidence 包含重复项", None
    return validated, None, None


ROUND1_V19_TOP_LEVEL_KEYS = {
    "task_type",
    "profile_version",
    "prompt_version",
    "selection_policy",
    "selection_policy_version",
    "selected_papers",
}
ROUND1_V19_SELECTED_KEYS = {
    "candidate_index",
    "content_label",
    "reason",
    "evidence",
}
ROUND1_V19_CACHE_AUDIT_KEY = "_arxivkaleid_selection_audit"
CORE_RECOMMENDATION_LABELS = frozenset({"成像", "新解", "成像｜新解"})


def validate_optional_round1_evidence(
    value: Any, paper: dict[str, Any]
) -> tuple[list[dict[str, str]], str, int]:
    """逐项保留可锚定证据；证据错误只进入审计，不影响论文入选。"""
    if value is None:
        return [], "missing", 0
    if not isinstance(value, list):
        return [], "invalid", 1
    if not value:
        return [], "empty", 0

    accepted: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    discarded_count = max(0, len(value) - 3)
    for raw_item in value[:3]:
        checked, error, _detail = validate_round1_evidence([raw_item], paper)
        if error is not None or not checked:
            discarded_count += 1
            continue
        item = checked[0]
        key = (item["source"], item["quote"].casefold())
        if key in seen:
            discarded_count += 1
            continue
        seen.add(key)
        accepted.append(item)

    if accepted and discarded_count == 0:
        status = "valid"
    elif accepted:
        status = "partial"
    else:
        status = "invalid"
    return accepted, status, discarded_count


def validate_round1_v19_result(
    raw_result: Any,
    papers: list[dict[str, Any]],
    *,
    max_selected: int,
    profile_version: str,
    prompt_version: str,
    selection_policy_version: str,
) -> tuple[dict[str, Any], list[str]]:
    """校验精简 Top K；结构身份硬拒绝，单篇问题确定性排除。"""
    warnings: list[str] = []
    validated: dict[str, Any] = {
        "task_type": "round1_abstract_screening",
        "batch_valid": False,
        "actual_selected_count": 0,
        "candidate_rankings": [],
        "selected_papers": [],
        "cacheable_response": None,
        "selection_audit": None,
        "evidence_repair_count": 0,
        "evidence_repair_codes": [],
        "evidence_repair_details": [],
        "recommendation_hint_truncation_count": 0,
        "recommendation_hint_truncation_codes": [],
        "validation_error_code": None,
        "validation_diagnostic": None,
    }

    def reject(
        code: str, stage: str, message: str, **safe_details: Any
    ) -> tuple[dict[str, Any], list[str]]:
        warnings.append(message)
        diagnostic = {"code": code, "stage": stage, **safe_details}
        validated["validation_error_code"] = code
        validated["validation_diagnostic"] = diagnostic
        return validated, warnings

    if not isinstance(raw_result, dict):
        return reject(
            "response_not_object",
            "response",
            "模型第一轮输出不是 JSON 对象，已拒绝该批次。",
            actual_type=type(raw_result).__name__,
        )
    missing = sorted(ROUND1_V19_TOP_LEVEL_KEYS - set(raw_result))
    if missing:
        return reject(
            "missing_top_level_fields",
            "identity",
            "模型第一轮输出缺少关键顶层字段，已拒绝该批次。",
            missing_fields=missing,
        )
    mismatches = [
        field
        for field, actual, expected in (
            ("task_type", raw_result.get("task_type"), "round1_abstract_screening"),
            ("profile_version", raw_result.get("profile_version"), profile_version),
            ("prompt_version", raw_result.get("prompt_version"), prompt_version),
            (
                "selection_policy",
                raw_result.get("selection_policy"),
                "top_k_daily_budget",
            ),
            (
                "selection_policy_version",
                raw_result.get("selection_policy_version"),
                selection_policy_version,
            ),
        )
        if actual != expected
    ]
    if mismatches:
        return reject(
            "identity_mismatch",
            "identity",
            "模型第一轮输出身份与请求不一致，已拒绝该批次。",
            mismatch_fields=mismatches,
        )
    raw_selected = raw_result.get("selected_papers")
    if not isinstance(raw_selected, list):
        return reject(
            "selected_papers_not_array",
            "response",
            "模型第一轮 selected_papers 不是数组，已拒绝该批次。",
            actual_type=type(raw_selected).__name__,
        )

    limit = max(0, int(max_selected))
    considered = raw_selected[:limit]
    ignored_over_budget_count = max(0, len(raw_selected) - limit)
    candidate_map = {index: paper for index, paper in enumerate(papers, start=1)}
    seen_candidate_indices: set[int] = set()
    accepted: list[dict[str, Any]] = []
    exclusions: list[dict[str, Any]] = []
    accepted_positions: list[dict[str, int]] = []
    evidence_discarded_count = 0
    evidence_missing_count = 0
    extra_item_field_count = 0

    def exclude(model_position: int, code: str, candidate_index: Any = None) -> None:
        # 排除审计只记录安全的结构信息；非法或模型自报身份不得写入产物。
        _ = candidate_index
        exclusions.append({"model_position": model_position, "code": code})

    for model_position, item in enumerate(considered, start=1):
        if not isinstance(item, dict):
            exclude(model_position, "item_not_object")
            continue
        extra_item_field_count += len(set(item) - ROUND1_V19_SELECTED_KEYS)
        candidate_index = item.get("candidate_index")
        if (
            isinstance(candidate_index, bool)
            or not isinstance(candidate_index, int)
            or candidate_index not in candidate_map
        ):
            exclude(model_position, "candidate_index_invalid", candidate_index)
            continue
        if candidate_index in seen_candidate_indices:
            exclude(model_position, "candidate_duplicate", candidate_index)
            continue

        label = item.get("content_label")
        if label not in content_labels.CORE_CONTENT_LABEL_SET:
            exclude(model_position, "content_label_invalid", candidate_index)
            continue
        if label not in CORE_RECOMMENDATION_LABELS:
            exclude(model_position, "non_core_content_label", candidate_index)
            continue
        reason = strict_round2_text(item.get("reason"), max_chars=300)
        if reason is None:
            exclude(model_position, "reason_invalid", candidate_index)
            continue

        seen_candidate_indices.add(candidate_index)
        evidence, evidence_status, discarded = validate_optional_round1_evidence(
            item.get("evidence"), candidate_map[candidate_index]
        )
        evidence_discarded_count += discarded
        if evidence_status in {"missing", "empty"}:
            evidence_missing_count += 1
        final_rank = len(accepted) + 1
        accepted_positions.append(
            {
                "model_position": model_position,
                "final_rank": final_rank,
                "candidate_index": candidate_index,
            }
        )
        accepted.append(
            {
                **candidate_map[candidate_index],
                "candidate_index": candidate_index,
                "model_position": model_position,
                "round1_rank": final_rank,
                "content_label": label,
                "reason": reason,
                "recommendation_hint": reason,
                "recommendation_hint_truncated": False,
                "recommendation_hint_original_length": len(reason),
                "matched_reasons": [reason],
                "negative_reasons": [],
                "evidence": evidence,
                "evidence_status": evidence_status,
                "evidence_invalid_count": discarded,
                "evidence_repaired": False,
                "evidence_repair_code": None,
                "evidence_repair_reason_code": None,
                "evidence_repair_source": None,
                "evidence_repair_quote_length": None,
                "evidence_repair_item_index": None,
                "evidence_from_title_or_abstract": [
                    evidence_item["quote"] for evidence_item in evidence
                ],
                "score": None,
                "confidence": None,
            }
        )

    audit = {
        "code": None,
        "stage": "accepted",
        "model_output_count": len(raw_selected),
        "considered_count": len(considered),
        "accepted_count": len(accepted),
        "excluded_count": len(exclusions),
        "ignored_over_budget_count": ignored_over_budget_count,
        "accepted_positions": accepted_positions,
        "exclusions": exclusions,
        "evidence_discarded_count": evidence_discarded_count,
        "evidence_missing_count": evidence_missing_count,
        "extra_top_level_field_count": len(
            set(raw_result) - ROUND1_V19_TOP_LEVEL_KEYS - {ROUND1_V19_CACHE_AUDIT_KEY}
        ),
        "extra_item_field_count": extra_item_field_count,
    }
    cache_selected: list[dict[str, Any]] = []
    for paper in accepted:
        cache_item: dict[str, Any] = {
            "candidate_index": paper["candidate_index"],
            "content_label": paper["content_label"],
            "reason": paper["reason"],
            "_arxivkaleid_model_position": paper["model_position"],
            "_arxivkaleid_evidence_status": paper["evidence_status"],
            "_arxivkaleid_evidence_invalid_count": paper[
                "evidence_invalid_count"
            ],
        }
        if paper["evidence"]:
            cache_item["evidence"] = paper["evidence"]
        cache_selected.append(cache_item)
    validated.update(
        {
            "batch_valid": True,
            "actual_selected_count": len(accepted),
            "selected_papers": accepted,
            "selection_audit": audit,
            "validation_diagnostic": audit,
            "cacheable_response": {
                "task_type": "round1_abstract_screening",
                "profile_version": profile_version,
                "prompt_version": prompt_version,
                "selection_policy": "top_k_daily_budget",
                "selection_policy_version": selection_policy_version,
                "selected_papers": cache_selected,
                ROUND1_V19_CACHE_AUDIT_KEY: audit,
            },
        }
    )
    if exclusions or ignored_over_budget_count or evidence_discarded_count:
        warnings.append(
            "第一轮已按逐篇容错规则排除非法条目或无效证据；有效论文顺序保持不变。"
        )
    return validated, warnings


def validate_round1_result(
    raw_result: Any,
    papers: list[dict[str, Any]],
    *,
    max_selected: int,
    profile_version: str,
    prompt_version: str,
    selection_policy_version: str,
) -> tuple[dict[str, Any], list[str]]:
    """仅执行 alpha.4 的当前协议，保留既有逐篇容错校验器。"""
    if prompt_version != CURRENT_ROUND1_PROMPT_VERSION:
        raise RuntimeError("desktop_prompt_version_unsupported")
    return validate_round1_v19_result(raw_result, papers, max_selected=max_selected, profile_version=profile_version, prompt_version=prompt_version, selection_policy_version=selection_policy_version)


def round2_recommendation_level_for_rank(final_rank: int) -> str:
    """新版阅读级别只由最终名次确定，不接收模型决定。"""
    if final_rank == 1:
        return "deep_read"
    if 2 <= final_rank <= 4:
        return "skim_read"
    if final_rank == 5:
        return "backup"
    raise RuntimeError("round2_final_rank_out_of_budget")


ROUND2_V14_TOP_LEVEL_KEYS = {
    "task_type",
    "selection_policy",
    "profile_version",
    "prompt_version",
    "final_recommendations",
}
ROUND2_V14_RECOMMENDATION_KEYS = {
    "arxiv_id",
    "version",
    "content_label",
    "reason",
}
ROUND2_V14_CACHE_AUDIT_KEY = "_arxivkaleid_selection_audit"


def validate_round2_v14_result(
    raw_result: Any,
    papers: list[dict[str, Any]],
    *,
    max_recommendations: int,
    profile_version: str,
    prompt_version: str,
) -> tuple[dict[str, Any], list[str]]:
    """校验全文精简 Top K，并逐篇排除不可信推荐。"""
    warnings: list[str] = []
    validated: dict[str, Any] = {
        "task_type": ROUND2_TASK_TYPE,
        "batch_valid": False,
        "actual_recommendation_count": 0,
        "final_recommendations": [],
        "quality_note": "",
        "selection_audit": None,
        "validation_error_code": None,
        "validation_diagnostic": None,
        "evidence_repair_count": 0,
        "evidence_repair_codes": [],
        "evidence_repair_positions": [],
        "cacheable_response": None,
    }

    def reject(
        code: str, stage: str, message: str, **safe_details: Any
    ) -> tuple[dict[str, Any], list[str]]:
        warnings.append(message)
        diagnostic = {"code": code, "stage": stage, **safe_details}
        validated["validation_error_code"] = code
        validated["validation_diagnostic"] = diagnostic
        return validated, warnings

    if not isinstance(raw_result, dict):
        return reject(
            "round2_response_not_object",
            "response",
            "模型第二轮输出不是 JSON 对象，已拒绝该批次。",
            actual_type=type(raw_result).__name__,
        )
    missing = sorted(ROUND2_V14_TOP_LEVEL_KEYS - set(raw_result))
    if missing:
        return reject(
            "round2_missing_top_level_fields",
            "identity",
            "模型第二轮输出缺少关键顶层字段，已拒绝该批次。",
            missing_fields=missing,
        )
    mismatches = [
        field
        for field, actual, expected in (
            ("task_type", raw_result.get("task_type"), ROUND2_TASK_TYPE),
            (
                "selection_policy",
                raw_result.get("selection_policy"),
                round2_selection_policy_for_prompt(prompt_version),
            ),
            ("profile_version", raw_result.get("profile_version"), profile_version),
            ("prompt_version", raw_result.get("prompt_version"), prompt_version),
        )
        if actual != expected
    ]
    if mismatches:
        return reject(
            "round2_identity_mismatch",
            "identity",
            "模型第二轮输出身份与请求不一致，已拒绝该批次。",
            mismatch_fields=mismatches,
        )
    raw_recommendations = raw_result.get("final_recommendations")
    if not isinstance(raw_recommendations, list):
        return reject(
            "round2_recommendations_not_array",
            "response",
            "模型第二轮 final_recommendations 不是数组，已拒绝该批次。",
            actual_type=type(raw_recommendations).__name__,
        )

    limit = max(0, int(max_recommendations))
    considered = raw_recommendations[:limit]
    ignored_over_budget_count = max(0, len(raw_recommendations) - limit)
    candidate_map = {
        (paper["arxiv_id"], int(paper["version"])): paper for paper in papers
    }
    seen: set[tuple[str, int]] = set()
    accepted: list[dict[str, Any]] = []
    exclusions: list[dict[str, Any]] = []
    accepted_positions: list[dict[str, Any]] = []
    extra_item_field_count = 0

    def exclude(
        model_position: int,
        code: str,
        *,
        arxiv_id: Any = None,
        version: Any = None,
    ) -> None:
        # 不持久化无效模型字段；有效项的身份只出现在 accepted_positions。
        _ = (arxiv_id, version)
        exclusions.append({"model_position": model_position, "code": code})

    for model_position, item in enumerate(considered, start=1):
        if not isinstance(item, dict):
            exclude(model_position, "item_not_object")
            continue
        extra_item_field_count += len(set(item) - ROUND2_V14_RECOMMENDATION_KEYS)
        arxiv_id = item.get("arxiv_id")
        version = parse_model_version(item.get("version"))
        key = (arxiv_id, version)
        if (
            not isinstance(arxiv_id, str)
            or not arxiv_id
            or version is None
            or key not in candidate_map
        ):
            exclude(
                model_position,
                "candidate_identity_invalid",
                arxiv_id=arxiv_id,
                version=item.get("version"),
            )
            continue
        typed_key = (arxiv_id, version)
        if typed_key in seen:
            exclude(
                model_position,
                "candidate_duplicate",
                arxiv_id=arxiv_id,
                version=version,
            )
            continue

        label = item.get("content_label")
        if label not in content_labels.CORE_CONTENT_LABEL_SET:
            exclude(
                model_position,
                "content_label_invalid",
                arxiv_id=arxiv_id,
                version=version,
            )
            continue
        if label not in CORE_RECOMMENDATION_LABELS:
            exclude(
                model_position,
                "non_core_content_label",
                arxiv_id=arxiv_id,
                version=version,
            )
            continue
        reason = strict_round2_text(item.get("reason"), max_chars=300)
        if reason is None:
            exclude(
                model_position,
                "reason_invalid",
                arxiv_id=arxiv_id,
                version=version,
            )
            continue

        seen.add(typed_key)
        final_rank = len(accepted) + 1
        accepted_positions.append(
            {
                "model_position": model_position,
                "final_rank": final_rank,
                "arxiv_id": arxiv_id,
                "version": version,
            }
        )
        accepted.append(
            {
                **candidate_map[typed_key],
                "model_position": model_position,
                "final_rank": final_rank,
                "recommendation_level": round2_recommendation_level_for_rank(
                    final_rank
                ),
                "content_label": label,
                "reason": reason,
                "matched_reasons": [reason],
                "negative_reasons": [],
                "suggested_reading_scope": "",
                "score": None,
                "confidence": None,
                "evidence": [],
                "evidence_repaired": False,
                "evidence_repair_code": None,
                "evidence_repair_indices": [],
            }
        )

    audit = {
        "code": None,
        "stage": "accepted",
        "model_output_count": len(raw_recommendations),
        "considered_count": len(considered),
        "accepted_count": len(accepted),
        "excluded_count": len(exclusions),
        "ignored_over_budget_count": ignored_over_budget_count,
        "accepted_positions": accepted_positions,
        "exclusions": exclusions,
        "extra_top_level_field_count": len(
            set(raw_result)
            - ROUND2_V14_TOP_LEVEL_KEYS
            - {ROUND2_V14_CACHE_AUDIT_KEY}
        ),
        "extra_item_field_count": extra_item_field_count,
    }
    cache_recommendations = [
        {
            "arxiv_id": paper["arxiv_id"],
            "version": f"v{paper['version']}",
            "content_label": paper["content_label"],
            "reason": paper["reason"],
            "_arxivkaleid_model_position": paper["model_position"],
        }
        for paper in accepted
    ]
    validated.update(
        {
            "batch_valid": True,
            "actual_recommendation_count": len(accepted),
            "final_recommendations": accepted,
            "selection_audit": audit,
            "validation_diagnostic": audit,
            "cacheable_response": {
                "task_type": ROUND2_TASK_TYPE,
                "selection_policy": ROUND2_SELECTION_POLICY,
                "profile_version": profile_version,
                "prompt_version": prompt_version,
                "final_recommendations": cache_recommendations,
                ROUND2_V14_CACHE_AUDIT_KEY: audit,
            },
        }
    )
    if exclusions or ignored_over_budget_count:
        warnings.append(
            "第二轮已按逐篇容错规则排除非法条目；有效论文顺序保持不变。"
        )
    return validated, warnings


def validate_round2_result(
    raw_result: Any,
    papers: list[dict[str, Any]],
    *,
    max_recommendations: int,
    profile_version: str,
    prompt_version: str,
) -> tuple[dict[str, Any], list[str]]:
    """仅执行 alpha.4 的当前协议，保留既有逐篇容错校验器。"""
    if prompt_version != CURRENT_ROUND2_PROMPT_VERSION:
        raise RuntimeError("desktop_prompt_version_unsupported")
    return validate_round2_v14_result(raw_result, papers, max_recommendations=max_recommendations, profile_version=profile_version, prompt_version=prompt_version)


def stable_json_hash(value: Any) -> str:
    """对 JSON 兼容对象生成稳定 SHA-256；只保存摘要，不保存原始输入。"""
    canonical = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


SCREENING_AUDIT_COUNT_KEYS = (
    "model_output_count",
    "considered_count",
    "accepted_count",
    "excluded_count",
    "ignored_over_budget_count",
)
SCREENING_AUDIT_OPTIONAL_COUNT_KEYS = (
    "evidence_discarded_count",
    "evidence_missing_count",
    "extra_top_level_field_count",
    "extra_item_field_count",
)
SCREENING_AUDIT_BASE_KEYS = {
    "code",
    "stage",
    *SCREENING_AUDIT_COUNT_KEYS,
    "accepted_positions",
    "exclusions",
}


def empty_screening_stage_audit() -> dict[str, Any]:
    """生成 0 篇阶段的标准安全审计，不包含任何模型原始字段。"""
    return {
        "code": None,
        "stage": "accepted",
        "model_output_count": 0,
        "considered_count": 0,
        "accepted_count": 0,
        "excluded_count": 0,
        "ignored_over_budget_count": 0,
        "accepted_positions": [],
        "exclusions": [],
    }


def screening_stage_audit_valid(
    audit: Any, *, expected_accepted_count: int | None = None
) -> bool:
    if (
        not isinstance(audit, dict)
        or audit.get("code") is not None
        or audit.get("stage") != "accepted"
    ):
        return False
    counts: dict[str, int] = {}
    for key in SCREENING_AUDIT_COUNT_KEYS:
        value = audit.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return False
        counts[key] = value
    if set(audit) - SCREENING_AUDIT_BASE_KEYS - set(
        SCREENING_AUDIT_OPTIONAL_COUNT_KEYS
    ):
        return False
    for key in SCREENING_AUDIT_OPTIONAL_COUNT_KEYS:
        if key not in audit:
            continue
        value = audit[key]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return False
    if counts["accepted_count"] + counts["excluded_count"] != counts[
        "considered_count"
    ]:
        return False
    if counts["considered_count"] + counts["ignored_over_budget_count"] != counts[
        "model_output_count"
    ]:
        return False
    if (
        expected_accepted_count is not None
        and counts["accepted_count"] != expected_accepted_count
    ):
        return False
    accepted_positions = audit.get("accepted_positions")
    exclusions = audit.get("exclusions")
    if (
        not isinstance(accepted_positions, list)
        or len(accepted_positions) != counts["accepted_count"]
        or not isinstance(exclusions, list)
        or len(exclusions) != counts["excluded_count"]
    ):
        return False
    observed_positions: set[int] = set()
    for final_rank, item in enumerate(accepted_positions, start=1):
        if not isinstance(item, dict):
            return False
        common_valid = (
            isinstance(item.get("model_position"), int)
            and not isinstance(item.get("model_position"), bool)
            and 1 <= item["model_position"] <= counts["considered_count"]
            and item.get("final_rank") == final_rank
        )
        round1_valid = (
            set(item) == {"model_position", "final_rank", "candidate_index"}
            and isinstance(item.get("candidate_index"), int)
            and not isinstance(item.get("candidate_index"), bool)
            and item["candidate_index"] > 0
        )
        round2_valid = (
            set(item)
            == {"model_position", "final_rank", "arxiv_id", "version"}
            and isinstance(item.get("arxiv_id"), str)
            and re.fullmatch(r"[0-9A-Za-z./-]{1,40}", item["arxiv_id"])
            is not None
            and isinstance(item.get("version"), int)
            and not isinstance(item.get("version"), bool)
            and item["version"] > 0
        )
        if not common_valid or not (round1_valid or round2_valid):
            return False
        observed_positions.add(item["model_position"])
    for item in exclusions:
        if (
            not isinstance(item, dict)
            or set(item) != {"model_position", "code"}
            or isinstance(item.get("model_position"), bool)
            or not isinstance(item.get("model_position"), int)
            or not 1 <= item["model_position"] <= counts["considered_count"]
            or not isinstance(item.get("code"), str)
            or re.fullmatch(r"[a-z0-9_]{1,64}", item["code"]) is None
        ):
            return False
        observed_positions.add(item["model_position"])
    return observed_positions == set(range(1, counts["considered_count"] + 1))


def save_screening_stage_audit(
    connection: sqlite3.Connection,
    run_id: int,
    task_type: str,
    prompt_version: str,
    selection_policy_version: str,
    audit: dict[str, Any],
) -> None:
    """只保存验证器产生的有界结构审计，不保存模型原始字段内容。"""
    if not screening_stage_audit_valid(audit):
        raise RuntimeError("screening_stage_audit_invalid")
    counts: dict[str, int] = {}
    for key in SCREENING_AUDIT_COUNT_KEYS:
        value = audit.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise RuntimeError("screening_stage_audit_count_invalid")
        counts[key] = value
    if counts["accepted_count"] + counts["excluded_count"] != counts[
        "considered_count"
    ]:
        raise RuntimeError("screening_stage_audit_balance_invalid")
    encoded = json.dumps(
        audit, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    if len(encoded) > 65536:
        raise RuntimeError("screening_stage_audit_too_large")
    with connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS screening_stage_audits (
                run_id INTEGER NOT NULL,
                task_type TEXT NOT NULL,
                prompt_version TEXT NOT NULL,
                selection_policy_version TEXT NOT NULL,
                model_output_count INTEGER NOT NULL,
                considered_count INTEGER NOT NULL,
                accepted_count INTEGER NOT NULL,
                excluded_count INTEGER NOT NULL,
                ignored_over_budget_count INTEGER NOT NULL,
                audit_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (run_id, task_type)
            )
            """
        )
        connection.execute(
            """
            INSERT INTO screening_stage_audits (
                run_id, task_type, prompt_version, selection_policy_version,
                model_output_count, considered_count, accepted_count,
                excluded_count, ignored_over_budget_count, audit_json, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(run_id, task_type) DO UPDATE SET
                prompt_version = excluded.prompt_version,
                selection_policy_version = excluded.selection_policy_version,
                model_output_count = excluded.model_output_count,
                considered_count = excluded.considered_count,
                accepted_count = excluded.accepted_count,
                excluded_count = excluded.excluded_count,
                ignored_over_budget_count = excluded.ignored_over_budget_count,
                audit_json = excluded.audit_json,
                created_at = excluded.created_at
            """,
            (
                run_id,
                task_type,
                prompt_version,
                selection_policy_version,
                counts["model_output_count"],
                counts["considered_count"],
                counts["accepted_count"],
                counts["excluded_count"],
                counts["ignored_over_budget_count"],
                encoded,
                current_time_iso(),
            ),
        )


def save_round1_screening_results(
    connection: sqlite3.Connection,
    run_id: int,
    selected_papers: list[dict[str, Any]],
    config: dict[str, Any],
    *,
    selection_audit: dict[str, Any] | None = None,
) -> None:
    """保存经过程序校验的第一轮单篇结果及选择审计。"""
    if selection_audit is not None:
        save_screening_stage_audit(
            connection,
            run_id,
            "round1_abstract_screening",
            config["versions"]["round1_prompt_version"],
            config["round1_selection_policy_version"],
            selection_audit,
        )
    created_at = current_time_iso()
    try:
        with connection:
            connection.execute(
                "DELETE FROM screening_results WHERE run_id = ? AND task_type = ?",
                (run_id, "round1_abstract_screening"),
            )
            if not selected_papers:
                return
            connection.executemany(
                """
                INSERT INTO screening_results (
                    run_id, task_type, arxiv_id, version, result_rank,
                    recommendation_level, score, confidence, is_selected,
                    reason, details_json, model_name, prompt_version,
                    research_profile_version, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id, task_type, arxiv_id, version) DO UPDATE SET
                    result_rank = excluded.result_rank,
                    recommendation_level = excluded.recommendation_level,
                    score = excluded.score,
                    confidence = excluded.confidence,
                    is_selected = excluded.is_selected,
                    reason = excluded.reason,
                    details_json = excluded.details_json,
                    created_at = excluded.created_at
                """,
                [
                    (
                        run_id,
                        "round1_abstract_screening",
                        paper["arxiv_id"],
                        paper["version"],
                        paper["round1_rank"],
                        (
                            None
                            if config["versions"]["round1_prompt_version"]
                            in ROUND1_PRECISION_PROMPT_VERSIONS
                            else paper["recommendation_hint"]
                        ),
                        paper["score"],
                        paper["confidence"],
                        1,
                        "；".join(paper["matched_reasons"]),
                        json.dumps(
                            {
                                **(
                                    {"content_label": paper["content_label"]}
                                    if "content_label" in paper
                                    else {
                                        "relevance_band": paper["relevance_band"],
                                        "reason_codes": paper["reason_codes"],
                                    }
                                ),
                                "matched_reasons": paper["matched_reasons"],
                                "negative_reasons": paper["negative_reasons"],
                                "evidence": paper["evidence"],
                                "model_position": paper.get("model_position"),
                                "evidence_status": paper.get("evidence_status"),
                                "evidence_invalid_count": paper.get(
                                    "evidence_invalid_count", 0
                                ),
                                "evidence_repaired": paper["evidence_repaired"],
                                "evidence_repair_code": paper[
                                    "evidence_repair_code"
                                ],
                                "evidence_repair_reason_code": paper[
                                    "evidence_repair_reason_code"
                                ],
                                "evidence_repair_source": paper[
                                    "evidence_repair_source"
                                ],
                                "evidence_repair_quote_length": paper[
                                    "evidence_repair_quote_length"
                                ],
                                "evidence_repair_item_index": paper[
                                    "evidence_repair_item_index"
                                ],
                                "recommendation_hint_truncated": paper[
                                    "recommendation_hint_truncated"
                                ],
                                "recommendation_hint_original_length": paper[
                                    "recommendation_hint_original_length"
                                ],
                                "evidence_from_title_or_abstract": paper[
                                    "evidence_from_title_or_abstract"
                                ],
                            },
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ),
                        deepseek_stage_config(config, "round1")["model"],
                        config["versions"]["round1_prompt_version"],
                        config["versions"]["research_profile_version"],
                        created_at,
                    )
                    for paper in selected_papers
                ],
            )
    except sqlite3.Error as exc:
        raise RuntimeError(f"SQLite 保存第一轮筛选结果失败：{exc}") from exc


def save_round2_screening_results(
    connection: sqlite3.Connection,
    run_id: int,
    final_recommendations: list[dict[str, Any]],
    config: dict[str, Any],
    *,
    selection_audit: dict[str, Any] | None = None,
) -> None:
    """保存第二轮最终推荐；正文节选不写入筛选结果表。"""
    if selection_audit is not None:
        save_screening_stage_audit(
            connection,
            run_id,
            ROUND2_TASK_TYPE,
            config["versions"]["round2_prompt_version"],
            round2_selection_policy_for_prompt(
                config["versions"]["round2_prompt_version"]
            ),
            selection_audit,
        )
    created_at = current_time_iso()
    try:
        with connection:
            # 第二轮结果按 run 整批替换，避免旧提示词版本或旧排名残留。
            connection.execute(
                """
                DELETE FROM screening_results
                WHERE run_id = ? AND task_type = ?
                """,
                (run_id, ROUND2_TASK_TYPE),
            )
            if not final_recommendations:
                return
            connection.executemany(
                """
                INSERT INTO screening_results (
                    run_id, task_type, arxiv_id, version, result_rank,
                    recommendation_level, score, confidence, is_selected,
                    reason, details_json, model_name, prompt_version,
                    research_profile_version, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id, task_type, arxiv_id, version) DO UPDATE SET
                    result_rank = excluded.result_rank,
                    recommendation_level = excluded.recommendation_level,
                    score = excluded.score,
                    confidence = excluded.confidence,
                    is_selected = excluded.is_selected,
                    reason = excluded.reason,
                    details_json = excluded.details_json,
                    created_at = excluded.created_at
                """,
                [
                    (
                        run_id,
                        ROUND2_TASK_TYPE,
                        paper["arxiv_id"],
                        int(paper["version"]),
                        paper["final_rank"],
                        paper["recommendation_level"],
                        paper["score"],
                        paper["confidence"],
                        1,
                        paper["reason"],
                        json.dumps(
                            {
                                "matched_reasons": paper["matched_reasons"],
                                "negative_reasons": paper["negative_reasons"],
                                "evidence": paper["evidence"],
                                "evidence_repaired": paper.get(
                                    "evidence_repaired"
                                )
                                is True,
                                "evidence_repair_code": paper.get(
                                    "evidence_repair_code"
                                ),
                                "evidence_repair_indices": paper.get(
                                    "evidence_repair_indices", []
                                ),
                                "suggested_reading_scope": paper[
                                    "suggested_reading_scope"
                                ],
                                "model_position": paper.get("model_position"),
                                **(
                                    {"content_label": paper["content_label"]}
                                    if "content_label" in paper
                                    else {}
                                ),
                                **(
                                    {
                                        "contribution_tier": paper[
                                            "contribution_tier"
                                        ]
                                    }
                                    if "contribution_tier" in paper
                                    else {}
                                ),
                            },
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ),
                        deepseek_stage_config(config, "round2")["model"],
                        config["versions"]["round2_prompt_version"],
                        config["versions"]["research_profile_version"],
                        created_at,
                    )
                    for paper in final_recommendations
                ],
            )
    except sqlite3.Error as exc:
        raise RuntimeError(f"SQLite 保存第二轮筛选结果失败：{exc}") from exc


def save_round1_completion_statuses(
    connection: sqlite3.Connection,
    run_id: int,
    papers: list[dict[str, Any]],
    selected_papers: list[dict[str, Any]],
    config: dict[str, Any],
) -> None:
    """为成功完成的整批候选保存轻量状态，不为未入围论文伪造分数。"""
    if not papers:
        return
    selected_keys = {
        (paper["arxiv_id"], int(paper["version"])) for paper in selected_papers
    }
    completed_at = current_time_iso()
    try:
        with connection:
            connection.executemany(
                """
                INSERT INTO screening_completion (
                    task_type, arxiv_id, version, run_id, completion_status,
                    selection_status, model_name, prompt_version,
                    research_profile_version, selection_policy_version,
                    completed_at
                )
                VALUES (?, ?, ?, ?, 'completed', ?, ?, ?, ?, ?, ?)
                ON CONFLICT(
                    task_type, arxiv_id, version, prompt_version,
                    research_profile_version, selection_policy_version
                ) DO NOTHING
                """,
                [
                    (
                        "round1_abstract_screening",
                        paper["arxiv_id"],
                        int(paper["version"]),
                        run_id,
                        (
                            "selected"
                            if (paper["arxiv_id"], int(paper["version"]))
                            in selected_keys
                            else "not_selected"
                        ),
                        deepseek_stage_config(config, "round1")["model"],
                        config["versions"]["round1_prompt_version"],
                        config["versions"]["research_profile_version"],
                        config["round1_selection_policy_version"],
                        completed_at,
                    )
                    for paper in papers
                ],
            )
    except sqlite3.Error as exc:
        raise RuntimeError(f"SQLite 保存第一轮完成状态失败：{exc}") from exc


def safe_error_message(error: Exception) -> str:
    """移除项目绝对路径，避免异常文本把本机目录写入日志或数据库。"""
    message = str(error)
    root_variants = {
        str(PROJECT_ROOT),
        str(PROJECT_ROOT.resolve()),
        str(PROJECT_ROOT).replace("\\", "/"),
    }
    for root in root_variants:
        message = message.replace(root, ".")
    return message
