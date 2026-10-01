from __future__ import annotations

import importlib.util
import json
import logging
import math
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import pdf_processing


DEFAULT_MAX_PDF_PAGES = 60
DEFAULT_MAX_REQUEST_TOKENS = 800_000
FULLTEXT_EXTRACTOR_VERSION = "pdf_pages_v1_20260719"
TOKEN_ESTIMATOR_VERSION = "serialized_utf8_bytes_div3_v1"
TOKEN_ESTIMATOR_BYTES_PER_TOKEN = 3

PAGE_GATE_ELIGIBLE = "eligible"
PAGE_GATE_LONG_READING = "long_reading"
PAGE_GATE_UNRESOLVED = "unresolved"

EXTRACTION_EXTRACTED = "extracted"
EXTRACTION_SKIPPED_LONG_READING = "skipped_long_reading"
EXTRACTION_FAILED = "failed"

EXPECTED_DOCUMENT_COLUMNS = (
    "arxiv_id",
    "version",
    "source_pdf_sha256",
    "page_count",
    "page_gate_status",
    "extraction_status",
    "extracted_page_count",
    "total_text_chars",
    "extractor_version",
    "failure_reason",
    "processed_at",
)
EXPECTED_DOCUMENT_PRIMARY_KEY = ("arxiv_id", "version")
EXPECTED_PAGE_COLUMNS = (
    "arxiv_id",
    "version",
    "page_number",
    "page_text",
    "text_chars",
)
EXPECTED_PAGE_PRIMARY_KEY = ("arxiv_id", "version", "page_number")
EXPECTED_DECISION_COLUMNS = (
    "arxiv_id",
    "version",
    "decision",
    "page_count",
    "estimated_tokens",
    "failure_reason",
)
EXPECTED_DECISION_PRIMARY_KEY = ("arxiv_id", "version")


@dataclass(frozen=True)
class PdfFullTextResult:
    arxiv_id: str
    version: int
    source_pdf_sha256: str
    page_count: int
    page_gate_status: str
    extraction_status: str
    pages: tuple[str, ...] = ()
    failure_reason: str | None = None

    @property
    def extracted_page_count(self) -> int:
        return len(self.pages) if self.extraction_status == EXTRACTION_EXTRACTED else 0

    @property
    def total_text_chars(self) -> int:
        return sum(len(page) for page in self.pages)


class PdfFullTextExtractionError(RuntimeError):
    """只保存稳定错误类型；page_count 用于区分页数成功和正文失败。"""

    def __init__(self, error_type: str, *, page_count: int = 0) -> None:
        super().__init__(error_type)
        self.error_type = error_type
        self.page_count = page_count


def initialize_fulltext_schema(connection: sqlite3.Connection) -> None:
    """初始化独立的当前批次全文状态库，不修改累计主数据库。"""
    with connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS pdf_fulltext_documents (
                arxiv_id TEXT NOT NULL,
                version INTEGER NOT NULL,
                source_pdf_sha256 TEXT NOT NULL,
                page_count INTEGER NOT NULL,
                page_gate_status TEXT NOT NULL,
                extraction_status TEXT NOT NULL,
                extracted_page_count INTEGER NOT NULL,
                total_text_chars INTEGER NOT NULL,
                extractor_version TEXT NOT NULL,
                failure_reason TEXT,
                processed_at TEXT NOT NULL,
                PRIMARY KEY (arxiv_id, version)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS round2_input_decisions (
                arxiv_id TEXT NOT NULL,
                version INTEGER NOT NULL,
                decision TEXT NOT NULL,
                page_count INTEGER NOT NULL,
                estimated_tokens INTEGER,
                failure_reason TEXT,
                PRIMARY KEY (arxiv_id, version)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS pdf_fulltext_pages (
                arxiv_id TEXT NOT NULL,
                version INTEGER NOT NULL,
                page_number INTEGER NOT NULL,
                page_text TEXT NOT NULL,
                text_chars INTEGER NOT NULL,
                PRIMARY KEY (arxiv_id, version, page_number)
            )
            """
        )


def _schema_identity(
    connection: sqlite3.Connection, table: str
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    rows = connection.execute(f"PRAGMA table_info({table})").fetchall()
    columns = tuple(
        str(row["name"] if isinstance(row, sqlite3.Row) else row[1]) for row in rows
    )
    primary_key = tuple(
        str(row["name"] if isinstance(row, sqlite3.Row) else row[1])
        for row in sorted(
            rows,
            key=lambda row: row["pk"] if isinstance(row, sqlite3.Row) else row[5],
        )
        if (row["pk"] if isinstance(row, sqlite3.Row) else row[5])
    )
    return columns, primary_key


def validate_fulltext_schema(connection: sqlite3.Connection) -> None:
    documents = _schema_identity(connection, "pdf_fulltext_documents")
    pages = _schema_identity(connection, "pdf_fulltext_pages")
    if documents != (EXPECTED_DOCUMENT_COLUMNS, EXPECTED_DOCUMENT_PRIMARY_KEY):
        raise RuntimeError("pdf_fulltext_documents_schema_mismatch")
    if pages != (EXPECTED_PAGE_COLUMNS, EXPECTED_PAGE_PRIMARY_KEY):
        raise RuntimeError("pdf_fulltext_pages_schema_mismatch")
    # Desktop 每次创建完整工作库；不接受缺少决策表的在线历史产物。
    decisions = _schema_identity(connection, "round2_input_decisions")
    if decisions != (EXPECTED_DECISION_COLUMNS, EXPECTED_DECISION_PRIMARY_KEY):
        raise RuntimeError("round2_input_decisions_schema_mismatch")


def replace_input_decisions(
    connection: sqlite3.Connection, decisions: list[dict[str, Any]]
) -> None:
    """原子替换当前批次的 Round 2 输入取舍记录。"""
    initialize_fulltext_schema(connection)
    allowed = {
        "full_text",
        "long_reading",
        "fulltext_extraction_failed",
        "token_budget_excluded",
    }
    normalized: list[tuple[Any, ...]] = []
    seen: set[tuple[str, int]] = set()
    for item in decisions:
        arxiv_id = str(item.get("arxiv_id") or "")
        version = int(item.get("version") or 0)
        decision = str(item.get("decision") or "")
        page_count = int(item.get("page_count") or 0)
        key = (arxiv_id, version)
        if not arxiv_id or version <= 0 or key in seen or decision not in allowed:
            raise RuntimeError("round2_input_decision_invalid")
        if page_count < 0 or (
            page_count == 0 and decision != "fulltext_extraction_failed"
        ):
            raise RuntimeError("round2_input_decision_page_count_invalid")
        estimated = item.get("estimated_tokens")
        if estimated is not None:
            estimated = int(estimated)
            if estimated <= 0:
                raise RuntimeError("round2_input_decision_tokens_invalid")
        failure_reason = item.get("failure_reason")
        normalized.append(
            (
                arxiv_id,
                version,
                decision,
                page_count,
                estimated,
                str(failure_reason) if failure_reason else None,
            )
        )
        seen.add(key)
    with connection:
        connection.execute("DELETE FROM round2_input_decisions")
        connection.executemany(
            """
            INSERT INTO round2_input_decisions (
                arxiv_id, version, decision, page_count,
                estimated_tokens, failure_reason
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            normalized,
        )


def normalize_page_text(text: str) -> str:
    """仅统一换行和无效空字符，不识别、裁剪或重排论文章节。"""
    normalized = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    normalized = normalized.replace("\x00", "")
    return normalized.strip()


def extract_pages_with_gate(
    pdf_path: Path, *, max_pdf_pages: int
) -> tuple[int, tuple[str, ...]]:
    """先读取物理页数；长文不提取正文，合格论文逐页读取。"""
    if importlib.util.find_spec("pypdf") is None:
        raise PdfFullTextExtractionError("missing_pypdf")
    try:
        from pypdf import PdfReader  # type: ignore[import-not-found]

        reader = PdfReader(str(pdf_path), strict=False)
        if reader.is_encrypted:
            try:
                decrypted = reader.decrypt("")
            except Exception as exc:
                raise PdfFullTextExtractionError("encrypted_pdf") from exc
            if not decrypted:
                raise PdfFullTextExtractionError("encrypted_pdf")
        page_count = len(reader.pages)
    except PdfFullTextExtractionError:
        raise
    except Exception as exc:
        raise PdfFullTextExtractionError("pdf_page_count_failed") from exc

    if page_count <= 0:
        raise PdfFullTextExtractionError("invalid_pdf_page_count")
    if page_count > max_pdf_pages:
        return page_count, ()

    pages: list[str] = []
    try:
        for page in reader.pages:
            pages.append(normalize_page_text(page.extract_text() or ""))
    except Exception as exc:
        raise PdfFullTextExtractionError(
            "pdf_page_text_extraction_failed", page_count=page_count
        ) from exc
    return page_count, tuple(pages)


def build_fulltext_result(
    *,
    arxiv_id: str,
    version: int,
    source_pdf_sha256: str,
    page_count: int,
    pages: tuple[str, ...],
    max_pdf_pages: int,
) -> PdfFullTextResult:
    if page_count > max_pdf_pages:
        return PdfFullTextResult(
            arxiv_id=arxiv_id,
            version=version,
            source_pdf_sha256=source_pdf_sha256,
            page_count=page_count,
            page_gate_status=PAGE_GATE_LONG_READING,
            extraction_status=EXTRACTION_SKIPPED_LONG_READING,
        )
    if len(pages) != page_count:
        return PdfFullTextResult(
            arxiv_id=arxiv_id,
            version=version,
            source_pdf_sha256=source_pdf_sha256,
            page_count=page_count,
            page_gate_status=PAGE_GATE_ELIGIBLE,
            extraction_status=EXTRACTION_FAILED,
            failure_reason="pdf_page_count_text_count_mismatch",
        )
    if not any(page.strip() for page in pages):
        return PdfFullTextResult(
            arxiv_id=arxiv_id,
            version=version,
            source_pdf_sha256=source_pdf_sha256,
            page_count=page_count,
            page_gate_status=PAGE_GATE_ELIGIBLE,
            extraction_status=EXTRACTION_FAILED,
            failure_reason="empty_pdf_text",
        )
    return PdfFullTextResult(
        arxiv_id=arxiv_id,
        version=version,
        source_pdf_sha256=source_pdf_sha256,
        page_count=page_count,
        page_gate_status=PAGE_GATE_ELIGIBLE,
        extraction_status=EXTRACTION_EXTRACTED,
        pages=pages,
    )


def save_fulltext_result(
    connection: sqlite3.Connection, result: PdfFullTextResult
) -> None:
    validate_fulltext_schema(connection)
    with connection:
        connection.execute(
            "DELETE FROM pdf_fulltext_pages WHERE arxiv_id = ? AND version = ?",
            (result.arxiv_id, result.version),
        )
        connection.execute(
            """
            INSERT INTO pdf_fulltext_documents (
                arxiv_id, version, source_pdf_sha256, page_count,
                page_gate_status, extraction_status, extracted_page_count,
                total_text_chars, extractor_version, failure_reason, processed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(arxiv_id, version) DO UPDATE SET
                source_pdf_sha256 = excluded.source_pdf_sha256,
                page_count = excluded.page_count,
                page_gate_status = excluded.page_gate_status,
                extraction_status = excluded.extraction_status,
                extracted_page_count = excluded.extracted_page_count,
                total_text_chars = excluded.total_text_chars,
                extractor_version = excluded.extractor_version,
                failure_reason = excluded.failure_reason,
                processed_at = excluded.processed_at
            """,
            (
                result.arxiv_id,
                result.version,
                result.source_pdf_sha256,
                result.page_count,
                result.page_gate_status,
                result.extraction_status,
                result.extracted_page_count,
                result.total_text_chars,
                FULLTEXT_EXTRACTOR_VERSION,
                result.failure_reason,
                pdf_processing.current_time_iso(),
            ),
        )
        if result.extraction_status == EXTRACTION_EXTRACTED:
            connection.executemany(
                """
                INSERT INTO pdf_fulltext_pages (
                    arxiv_id, version, page_number, page_text, text_chars
                ) VALUES (?, ?, ?, ?, ?)
                """,
                [
                    (
                        result.arxiv_id,
                        result.version,
                        page_number,
                        page_text,
                        len(page_text),
                    )
                    for page_number, page_text in enumerate(result.pages, start=1)
                ],
            )


def process_downloaded_pdfs(
    main_connection: sqlite3.Connection,
    fulltext_connection: sqlite3.Connection,
    project_root: Path,
    logger: logging.Logger,
    *,
    max_pdf_pages: int = DEFAULT_MAX_PDF_PAGES,
    target_papers: set[tuple[str, int]] | None = None,
    page_extractor: Callable[..., tuple[int, tuple[str, ...]]] | None = None,
    progress_callback: Callable[[PdfFullTextResult], None] | None = None,
    isolate_unexpected_paper_errors: bool = False,
) -> list[PdfFullTextResult]:
    """读取下载成功的PDF，保存页数门控与合格论文的逐页全文。"""
    if max_pdf_pages <= 0:
        raise RuntimeError("max_pdf_pages_must_be_positive")
    initialize_fulltext_schema(fulltext_connection)
    validate_fulltext_schema(fulltext_connection)
    params: list[object] = []
    target_clause = ""
    if target_papers is not None:
        if not target_papers:
            return []
        ordered_targets = sorted(target_papers)
        placeholders = ",".join("(?, ?)" for _ in ordered_targets)
        target_clause = f"AND (arxiv_id, version) IN ({placeholders})"
        for arxiv_id, version in ordered_targets:
            params.extend((arxiv_id, version))
    rows = main_connection.execute(
        f"""
        SELECT arxiv_id, version, local_pdf_path, download_status
        FROM pdf_downloads
        WHERE 1 = 1
          {target_clause}
        ORDER BY arxiv_id, version
        """,
        params,
    ).fetchall()

    extract_one = page_extractor or extract_pages_with_gate
    results: list[PdfFullTextResult] = []
    for row in rows:
        arxiv_id = str(row["arxiv_id"] if isinstance(row, sqlite3.Row) else row[0])
        version = int(row["version"] if isinstance(row, sqlite3.Row) else row[1])
        local_pdf_path = str(
            (row["local_pdf_path"] if isinstance(row, sqlite3.Row) else row[2]) or ""
        )
        download_status = str(
            (row["download_status"] if isinstance(row, sqlite3.Row) else row[3]) or ""
        )
        source_pdf_sha256 = ""
        if download_status not in pdf_processing.SUCCESSFUL_PDF_DOWNLOAD_STATUSES:
            result = PdfFullTextResult(
                arxiv_id=arxiv_id,
                version=version,
                source_pdf_sha256="",
                page_count=0,
                page_gate_status=PAGE_GATE_UNRESOLVED,
                extraction_status=EXTRACTION_FAILED,
                failure_reason="pdf_download_not_successful",
            )
            save_fulltext_result(fulltext_connection, result)
            results.append(result)
            logger.warning(
                "PDF 全文门控：arxiv_id=%s version=v%d pages=0 gate=%s status=%s",
                result.arxiv_id,
                result.version,
                result.page_gate_status,
                result.extraction_status,
            )
            if progress_callback is not None:
                try:
                    progress_callback(result)
                except Exception:
                    pass
            continue
        try:
            pdf_path = pdf_processing.resolve_stored_pdf_path(
                project_root, local_pdf_path
            )
            if not pdf_path.exists():
                raise PdfFullTextExtractionError("pdf_file_missing")
            if not pdf_processing.is_valid_pdf_file(pdf_path):
                raise PdfFullTextExtractionError("invalid_pdf_file")
            source_pdf_sha256 = pdf_processing.pdf_file_sha256(pdf_path)
            page_count, pages = extract_one(
                pdf_path, max_pdf_pages=max_pdf_pages
            )
            result = build_fulltext_result(
                arxiv_id=arxiv_id,
                version=version,
                source_pdf_sha256=source_pdf_sha256,
                page_count=page_count,
                pages=pages,
                max_pdf_pages=max_pdf_pages,
            )
        except PdfFullTextExtractionError as exc:
            gate_status = (
                PAGE_GATE_ELIGIBLE
                if 0 < exc.page_count <= max_pdf_pages
                else PAGE_GATE_UNRESOLVED
            )
            result = PdfFullTextResult(
                arxiv_id=arxiv_id,
                version=version,
                source_pdf_sha256=source_pdf_sha256,
                page_count=exc.page_count,
                page_gate_status=gate_status,
                extraction_status=EXTRACTION_FAILED,
                failure_reason=exc.error_type,
            )
        except pdf_processing.PdfTextExtractionError as exc:
            # 非法、缺失或越界的单篇PDF路径只排除该篇，不中断其余论文。
            result = PdfFullTextResult(
                arxiv_id=arxiv_id,
                version=version,
                source_pdf_sha256=source_pdf_sha256,
                page_count=0,
                page_gate_status=PAGE_GATE_UNRESOLVED,
                extraction_status=EXTRACTION_FAILED,
                failure_reason=exc.error_type,
            )
        except OSError:
            result = PdfFullTextResult(
                arxiv_id=arxiv_id,
                version=version,
                source_pdf_sha256=source_pdf_sha256,
                page_count=0,
                page_gate_status=PAGE_GATE_UNRESOLVED,
                extraction_status=EXTRACTION_FAILED,
                failure_reason="pdf_file_read_failed",
            )
        except Exception:
            if not isolate_unexpected_paper_errors:
                raise
            # 已进入单篇边界且共享组件已初始化；未知异常先形成结构化单篇失败。
            # 若保存或后续共享完整性检查失败，Desktop 会升级为系统错误。
            result = PdfFullTextResult(
                arxiv_id=arxiv_id,
                version=version,
                source_pdf_sha256=source_pdf_sha256,
                page_count=0,
                page_gate_status=PAGE_GATE_UNRESOLVED,
                extraction_status=EXTRACTION_FAILED,
                failure_reason="paper_processing_exception",
            )

        save_fulltext_result(fulltext_connection, result)
        results.append(result)
        logger.info(
            "PDF 全文门控：arxiv_id=%s version=v%d pages=%d gate=%s status=%s",
            result.arxiv_id,
            result.version,
            result.page_count,
            result.page_gate_status,
            result.extraction_status,
        )
        if progress_callback is not None:
            try:
                progress_callback(result)
            except Exception:
                # 进度回调不参与全文门控决策。
                pass
    return results


def load_document(
    connection: sqlite3.Connection, arxiv_id: str, version: int
) -> dict[str, Any] | None:
    row = connection.execute(
        """
        SELECT arxiv_id, version, source_pdf_sha256, page_count,
               page_gate_status, extraction_status, extracted_page_count,
               total_text_chars, extractor_version, failure_reason
        FROM pdf_fulltext_documents
        WHERE arxiv_id = ? AND version = ?
        """,
        (arxiv_id, version),
    ).fetchone()
    if row is None:
        return None
    if isinstance(row, sqlite3.Row):
        return dict(row)
    keys = (
        "arxiv_id",
        "version",
        "source_pdf_sha256",
        "page_count",
        "page_gate_status",
        "extraction_status",
        "extracted_page_count",
        "total_text_chars",
        "extractor_version",
        "failure_reason",
    )
    return dict(zip(keys, row, strict=True))


def load_document_pages(
    connection: sqlite3.Connection, arxiv_id: str, version: int
) -> tuple[dict[str, Any], ...]:
    rows = connection.execute(
        """
        SELECT page_number, page_text, text_chars
        FROM pdf_fulltext_pages
        WHERE arxiv_id = ? AND version = ?
        ORDER BY page_number
        """,
        (arxiv_id, version),
    ).fetchall()
    result: list[dict[str, Any]] = []
    for row in rows:
        if isinstance(row, sqlite3.Row):
            result.append(dict(row))
        else:
            result.append(
                {
                    "page_number": int(row[0]),
                    "page_text": str(row[1] or ""),
                    "text_chars": int(row[2]),
                }
            )
    return tuple(result)


def conservative_value_token_estimate(value: Any) -> int:
    """用序列化UTF-8字节数除以3，得到可复现保守估算。"""
    serialized = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return max(
        1,
        math.ceil(
            len(serialized.encode("utf-8")) / TOKEN_ESTIMATOR_BYTES_PER_TOKEN
        ),
    )


def conservative_request_token_estimate(
    messages: list[dict[str, str]],
) -> int:
    """估算完整消息请求，而不是只统计PDF正文。"""
    return conservative_value_token_estimate(messages)
