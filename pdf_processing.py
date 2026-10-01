# Copyright (c) 2026 yyyang20
# SPDX-License-Identifier: GPL-3.0-only
# See LICENSE in the project root for the full license text.

from __future__ import annotations

import hashlib
import logging
import re
import socket
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable


MIN_PDF_SIZE_BYTES = 1024
MAX_PDF_SIZE_BYTES = 100 * 1024 * 1024
PDF_REQUEST_INTERVAL_SECONDS = 3
PDF_REQUEST_TIMEOUT_SECONDS = 60
MAX_FIRST_ROUND_PDF_DOWNLOADS = 10
SUCCESSFUL_PDF_DOWNLOAD_STATUSES = ("downloaded", "reused_existing_pdf")


@dataclass(frozen=True)
class SelectedPaper:
    arxiv_id: str
    version: int
    title: str


@dataclass(frozen=True)
class PdfDownloadResult:
    paper: SelectedPaper
    online_pdf_url: str
    local_relative_path: str
    status: str
    size_bytes: int
    network_attempted: bool
    error_type: str | None = None
    http_status: int | None = None
    transport_error_type: str | None = None
    transient: bool = False


class PdfDownloadError(RuntimeError):
    """只携带可安全写入日志的错误类型。"""

    def __init__(
        self,
        error_type: str,
        *,
        http_status: int | None = None,
        transport_error_type: str | None = None,
        transient: bool = False,
    ) -> None:
        super().__init__(error_type)
        self.error_type = error_type
        self.http_status = http_status
        self.transport_error_type = transport_error_type
        self.transient = transient


class PdfTextExtractionError(RuntimeError):
    """只携带可安全写入数据库的 PDF 文本提取错误类型。"""

    def __init__(self, error_type: str) -> None:
        super().__init__(error_type)
        self.error_type = error_type


def current_time_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def relative_project_path(project_root: Path, path: Path) -> str:
    """生成适合日志和数据库保存的项目相对路径。"""
    try:
        return path.resolve().relative_to(project_root.resolve()).as_posix()
    except ValueError as exc:
        raise RuntimeError("目标路径超出项目目录。") from exc


def validate_arxiv_identity(arxiv_id: str, version: int) -> None:
    """限制 URL 和文件名输入，避免数据库异常内容影响路径。"""
    if not re.fullmatch(r"[A-Za-z0-9._/-]+", arxiv_id) or version < 1:
        raise RuntimeError("入围论文包含无效 arXiv ID 或 version。")
    if re.search(r"v\d+$", arxiv_id, flags=re.IGNORECASE):
        raise RuntimeError(
            "数据库 arxiv_id 必须是不含版本后缀的基础 ID。"
        )


def safe_arxiv_id(arxiv_id: str) -> str:
    """将旧格式 ID 中的斜杠转换为本地文件名安全字符。"""
    return re.sub(r"[^A-Za-z0-9._-]", "_", arxiv_id)


def build_online_pdf_url(paper: SelectedPaper) -> str:
    validate_arxiv_identity(paper.arxiv_id, paper.version)
    encoded_id = urllib.parse.quote(paper.arxiv_id, safe="/")
    return f"https://arxiv.org/pdf/{encoded_id}v{paper.version}"


def build_pdf_destination(
    pdfs_dir: Path, run_date: str, paper: SelectedPaper
) -> Path:
    validate_arxiv_identity(paper.arxiv_id, paper.version)
    filename = f"{safe_arxiv_id(paper.arxiv_id)}v{paper.version}.pdf"
    return pdfs_dir / run_date / filename


def validate_pdf_bytes(content: bytes) -> None:
    """仅验证文件签名和基本大小，不解析 PDF 正文。"""
    if len(content) < MIN_PDF_SIZE_BYTES:
        raise PdfDownloadError("pdf_too_small")
    if len(content) > MAX_PDF_SIZE_BYTES:
        raise PdfDownloadError("pdf_too_large")
    if not content.startswith(b"%PDF-"):
        raise PdfDownloadError("invalid_pdf_signature")


def is_valid_pdf_file(path: Path) -> bool:
    """已有文件只有在大小合理且具有 PDF 签名时才可复用。"""
    try:
        if not path.is_file():
            return False
        size = path.stat().st_size
        if not MIN_PDF_SIZE_BYTES <= size <= MAX_PDF_SIZE_BYTES:
            return False
        with path.open("rb") as file:
            return file.read(5) == b"%PDF-"
    except OSError:
        return False


def resolve_stored_pdf_path(project_root: Path, local_pdf_path: str) -> Path:
    """解析数据库中的项目相对 PDF 路径，拒绝越界路径。"""
    if not local_pdf_path or not str(local_pdf_path).strip():
        raise PdfTextExtractionError("missing_local_pdf_path")
    relative = Path(str(local_pdf_path))
    if relative.is_absolute():
        raise PdfTextExtractionError("absolute_pdf_path_not_allowed")
    root = project_root.resolve()
    target = (root / relative).resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise PdfTextExtractionError("pdf_path_outside_project") from exc
    return target


def pdf_file_sha256(pdf_path: Path) -> str:
    """流式计算 PDF 内容哈希，避免把大文件一次性读入内存。"""
    digest = hashlib.sha256()
    with pdf_path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def fetch_pdf_bytes(
    url: str,
    *,
    timeout_seconds: float = PDF_REQUEST_TIMEOUT_SECONDS,
    opener: Callable[..., Any] = urllib.request.urlopen,
) -> bytes:
    """只下载指定 PDF 到内存；不访问 arXiv 元数据 API。"""
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "arXivKaleid/2.0 (local academic research tool)",
            "Accept": "application/pdf",
        },
    )
    try:
        with opener(request, timeout=timeout_seconds) as response:
            status = getattr(response, "status", None)
            if status is None:
                status = response.getcode()
            if status != 200:
                raise PdfDownloadError(
                    f"http_{status}",
                    http_status=int(status),
                    transient=(status == 429 or 500 <= int(status) <= 599),
                )
            content = response.read(MAX_PDF_SIZE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        raise PdfDownloadError(
            f"http_{exc.code}",
            http_status=exc.code,
            transient=(exc.code == 429 or 500 <= exc.code <= 599),
        ) from None
    except (TimeoutError, socket.timeout):
        raise PdfDownloadError(
            "timeout", transport_error_type="timeout", transient=True
        ) from None
    except (urllib.error.URLError, OSError):
        raise PdfDownloadError(
            "download_failed", transport_error_type="connection", transient=True
        ) from None

    validate_pdf_bytes(content)
    return content


def download_or_reuse_pdf(
    project_root: Path,
    paper: SelectedPaper,
    destination: Path,
    *,
    timeout_seconds: float = PDF_REQUEST_TIMEOUT_SECONDS,
    opener: Callable[..., Any] = urllib.request.urlopen,
) -> PdfDownloadResult:
    """复用有效文件，否则执行一次网络下载并做非解析式校验。"""
    online_url = build_online_pdf_url(paper)
    local_relative_path = relative_project_path(project_root, destination)
    if is_valid_pdf_file(destination):
        return PdfDownloadResult(
            paper=paper,
            online_pdf_url=online_url,
            local_relative_path=local_relative_path,
            status="reused_existing_pdf",
            size_bytes=destination.stat().st_size,
            network_attempted=False,
        )
    if destination.exists():
        return PdfDownloadResult(
            paper=paper,
            online_pdf_url=online_url,
            local_relative_path=local_relative_path,
            status="failed",
            size_bytes=destination.stat().st_size,
            network_attempted=False,
            error_type="existing_invalid_pdf",
        )

    http_status = None
    transport_error_type = None
    transient = False
    try:
        content = fetch_pdf_bytes(
            online_url,
            timeout_seconds=timeout_seconds,
            opener=opener,
        )
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open("xb") as file:
            file.write(content)
        if not is_valid_pdf_file(destination):
            raise PdfDownloadError("saved_pdf_validation_failed")
        return PdfDownloadResult(
            paper=paper,
            online_pdf_url=online_url,
            local_relative_path=local_relative_path,
            status="downloaded",
            size_bytes=destination.stat().st_size,
            network_attempted=True,
        )
    except FileExistsError:
        if is_valid_pdf_file(destination):
            return PdfDownloadResult(
                paper=paper,
                online_pdf_url=online_url,
                local_relative_path=local_relative_path,
                status="reused_existing_pdf",
                size_bytes=destination.stat().st_size,
                network_attempted=False,
            )
        error_type = "existing_invalid_pdf"
    except PdfDownloadError as exc:
        error_type = exc.error_type
        http_status = exc.http_status
        transport_error_type = exc.transport_error_type
        transient = exc.transient
    except OSError:
        error_type = "file_write_failed"
        http_status = None
        transport_error_type = None
        transient = False

    size = destination.stat().st_size if destination.exists() else 0
    return PdfDownloadResult(
        paper=paper,
        online_pdf_url=online_url,
        local_relative_path=local_relative_path,
        status="failed",
        size_bytes=size,
        network_attempted=True,
        error_type=error_type,
        http_status=http_status,
        transport_error_type=transport_error_type,
        transient=transient,
    )


def save_pdf_download_result(
    connection: sqlite3.Connection, result: PdfDownloadResult
) -> None:
    """将下载状态写入现有 pdf_downloads 表。"""
    downloaded_at = (
        current_time_iso()
        if result.status in {"downloaded", "reused_existing_pdf"}
        else None
    )
    failure_reason = result.error_type if result.status == "failed" else None
    with connection:
        connection.execute(
            """
            INSERT INTO pdf_downloads (
                arxiv_id, version, local_pdf_path, download_status,
                downloaded_at, failure_reason
            )
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(arxiv_id, version) DO UPDATE SET
                local_pdf_path = excluded.local_pdf_path,
                download_status = excluded.download_status,
                downloaded_at = excluded.downloaded_at,
                failure_reason = excluded.failure_reason
            """,
            (
                result.paper.arxiv_id,
                result.paper.version,
                result.local_relative_path,
                result.status,
                downloaded_at,
                failure_reason,
            ),
        )


def download_selected_papers(
    project_root: Path,
    connection: sqlite3.Connection,
    pdfs_dir: Path,
    papers: list[SelectedPaper],
    run_date: str,
    logger: logging.Logger,
    *,
    timeout_seconds: float = PDF_REQUEST_TIMEOUT_SECONDS,
    interval_seconds: float = PDF_REQUEST_INTERVAL_SECONDS,
    opener: Callable[..., Any] = urllib.request.urlopen,
    downloader: Callable[..., PdfDownloadResult] | None = None,
    progress_callback: Callable[[PdfDownloadResult], None] | None = None,
) -> list[PdfDownloadResult]:
    """下载或复用本轮入围 PDF；单篇失败时记录状态并继续后续论文。"""
    selected = list(papers[:MAX_FIRST_ROUND_PDF_DOWNLOADS])
    download_one = downloader or download_or_reuse_pdf
    results: list[PdfDownloadResult] = []
    previous_network_attempt = False

    for paper in selected:
        destination: Path | None = None
        online_pdf_url = ""
        local_relative_path = ""
        will_attempt_network = False
        try:
            destination = build_pdf_destination(pdfs_dir, run_date, paper)
            online_pdf_url = build_online_pdf_url(paper)
            local_relative_path = relative_project_path(project_root, destination)
            will_attempt_network = not destination.exists()
            if previous_network_attempt and will_attempt_network:
                time.sleep(interval_seconds)
            result = download_one(
                project_root,
                paper,
                destination,
                timeout_seconds=timeout_seconds,
                opener=opener,
            )
        except Exception as exc:
            # PDF 是第一轮后的附加阶段；单篇异常只能降级为失败记录。
            if isinstance(exc, PdfDownloadError):
                error_type = exc.error_type
                http_status = exc.http_status
                transport_error_type = exc.transport_error_type
                transient = exc.transient
            elif isinstance(exc, RuntimeError):
                error_type = "invalid_pdf_identity"
                http_status = None
                transport_error_type = None
                transient = False
            else:
                error_type = "pdf_download_exception"
                http_status = None
                transport_error_type = None
                transient = False
            result = PdfDownloadResult(
                paper=paper,
                online_pdf_url=online_pdf_url,
                local_relative_path=local_relative_path,
                status="failed",
                size_bytes=0,
                network_attempted=will_attempt_network,
                error_type=error_type,
                http_status=http_status,
                transport_error_type=transport_error_type,
                transient=transient,
            )

        save_pdf_download_result(connection, result)
        results.append(result)
        previous_network_attempt = (
            previous_network_attempt or result.network_attempted
        )

        if result.status == "downloaded":
            logger.info(
                "PDF 下载结果：arxiv_id=%s version=v%d status=downloaded_pdf",
                paper.arxiv_id,
                paper.version,
            )
        elif result.status == "reused_existing_pdf":
            logger.info(
                "PDF 下载结果：arxiv_id=%s version=v%d status=reused_existing_pdf",
                paper.arxiv_id,
                paper.version,
            )
        else:
            logger.warning(
                "PDF 下载结果：arxiv_id=%s version=v%d "
                "status=pdf_download_failed error_type=%s",
                paper.arxiv_id,
                paper.version,
                result.error_type or "unknown",
            )
        if progress_callback is not None:
            try:
                progress_callback(result)
            except Exception:
                # 进度只观察既有下载流程，不能改变单篇结果或后续请求。
                pass

    return results
