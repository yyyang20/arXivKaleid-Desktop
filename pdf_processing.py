from __future__ import annotations

import json
import hashlib
import importlib.util
import logging
import os
import re
import socket
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable


PROJECT_ROOT = Path(__file__).resolve().parent
CONFIG_PATH = PROJECT_ROOT / "config.json"
MIN_PDF_SIZE_BYTES = 1024
MAX_PDF_SIZE_BYTES = 100 * 1024 * 1024
MAX_ARXIV_ABS_PAGE_BYTES = 2 * 1024 * 1024
PDF_REQUEST_INTERVAL_SECONDS = 3
PDF_REQUEST_TIMEOUT_SECONDS = 60
DEFAULT_HISTORY_PDF_TEST_LIMIT = 2
MAX_FIRST_ROUND_PDF_DOWNLOADS = 10
MAX_INTRODUCTION_CHARS = 6000
MAX_CONCLUSION_CHARS = 5000
PDF_SECTION_EXTRACTOR_VERSION = "pdf_sections_v7_20260717"
MIN_REQUIRED_SECTION_CHARS = 40
PDF_SECTION_IDENTITY_TABLE = "pdf_section_extraction_identity"
SUCCESSFUL_PDF_DOWNLOAD_STATUSES = ("downloaded", "reused_existing_pdf")
EXPECTED_PDF_SECTIONS_COLUMNS = (
    "arxiv_id",
    "version",
    "abstract_text",
    "introduction_text",
    "conclusion_text",
    "extraction_status",
    "extracted_at",
    "failure_reason",
)
EXPECTED_PDF_SECTIONS_PRIMARY_KEY = ("arxiv_id", "version")
SECTION_PLURAL_VARIANT_PAIRS = (
    ("conclusion", "conclusions"),
    ("discussion", "discussions"),
    ("direction", "directions"),
    ("perspective", "perspectives"),
    ("remark", "remarks"),
)


def expand_section_alias_variants(aliases: set[str]) -> set[str]:
    """为常见章节标题词补齐单复数变体，避免漏掉保守同义标题。"""
    variants = set(aliases)
    for singular, plural in SECTION_PLURAL_VARIANT_PAIRS:
        for alias in list(variants):
            variants.add(re.sub(rf"\b{re.escape(singular)}\b", plural, alias))
            variants.add(re.sub(rf"\b{re.escape(plural)}\b", singular, alias))
    return variants


ABSTRACT_SECTION_ALIASES = {"abstract"}
KEYWORDS_SECTION_ALIASES = {"keywords", "key words", "index terms"}
INTRODUCTION_SECTION_ALIASES = {"introduction"}
INTRODUCTION_SEMANTIC_SECTION_ALIASES = {
    "background",
    "background and motivation",
    "context",
    "motivation",
    "overview",
    "preliminaries",
    "preliminary",
    "scope and motivation",
}
CONCLUSION_SECTION_ALIASES = expand_section_alias_variants({
    "conclusion",
    "conclusions",
    "conclusion and discussion",
    "conclusions and discussion",
    "discussion and conclusion",
    "discussion and conclusions",
    "concluding remark",
    "concluding remarks",
    "final remark",
    "final remarks",
    "summary",
    "summary and conclusion",
    "summary and conclusions",
    "summary and discussion",
    "discussion and summary",
    "summary and outlook",
    # 兼容以总结和未来应用承担结论功能的主文章节。
    "summary and future application",
    "summary and future applications",
    "summary and prospect",
    "summary and prospects",
    "conclusions and outlook",
    "conclusion and future outlook",
    "conclusions and future directions",
})
DISCUSSION_SECTION_ALIASES = {"discussion", "discussions"}
COMMON_SECTION_BOUNDARY_ALIASES = {
    "analysis",
    "data",
    "materials and methods",
    "method",
    "methodology",
    "methods",
    "model",
    "models",
    "observation",
    "observations",
    "result",
    "results",
    "setup",
    "simulation",
    "simulations",
}
TERMINAL_SECTION_ALIASES = {
    "acknowledgment",
    "acknowledgments",
    "acknowledgement",
    "acknowledgements",
    "data availability",
    "data availability statement",
    "code availability",
    "code availability statement",
    "funding",
    "funding information",
    "funding statement",
    "author contribution",
    "author contributions",
    "author contribution statement",
    "author contributions statement",
    "competing interest",
    "competing interests",
    "conflict of interest",
    "conflicts of interest",
    "appendix",
    "appendices",
    "supplementary material",
    "supplementary materials",
    "references",
    "bibliography",
}
INTRODUCTION_FALLBACK_REJECT_ALIASES = {
    "observation",
    "observations",
    "data",
    "data reduction",
    "data and observations",
    "observations and data reduction",
    "numerical setup",
    "numerical method",
    "numerical methods",
    "model",
    "models",
    "the model",
    "method",
    "methods",
    "methodology",
    "setup",
    "simulation setup",
    "simulations",
    "analysis",
    "results",
}
CONCLUSION_SEMANTIC_CUES = expand_section_alias_variants({
    "conclusion",
    "discussion",
    "final remark",
    "future direction",
    "limitation",
    "outlook",
    "perspective",
    "summary",
})
CONCLUSION_FALLBACK_REJECT_WORDS = {
    "analysis",
    "data",
    "experiment",
    "experiments",
    "method",
    "methodology",
    "methods",
    "model",
    "models",
    "observation",
    "observations",
    "result",
    "results",
    "setup",
    "simulation",
    "simulations",
}
KNOWN_SECTION_ALIASES = (
    ABSTRACT_SECTION_ALIASES
    | KEYWORDS_SECTION_ALIASES
    | INTRODUCTION_SECTION_ALIASES
    | INTRODUCTION_SEMANTIC_SECTION_ALIASES
    | CONCLUSION_SECTION_ALIASES
    | DISCUSSION_SECTION_ALIASES
    | COMMON_SECTION_BOUNDARY_ALIASES
    | TERMINAL_SECTION_ALIASES
)


@dataclass(frozen=True)
class SelectedPaper:
    arxiv_id: str
    version: int
    title: str


@dataclass(frozen=True)
class NumberedSectionHeading:
    """可信编号章节及其层级，用于区分同级章节与更深的子章节。"""

    family: str
    path: tuple[int, ...]
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


@dataclass(frozen=True)
class ExtractedPdfSections:
    abstract_text: str
    introduction_text: str
    conclusion_text: str
    quality_warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class PdfSectionExtractionResult:
    arxiv_id: str
    version: int
    local_pdf_path: str
    extraction_status: str
    abstract_text: str = ""
    introduction_text: str = ""
    conclusion_text: str = ""
    failure_reason: str | None = None
    source_pdf_sha256: str = ""
    extractor_version: str = PDF_SECTION_EXTRACTOR_VERSION
    quality_warnings: tuple[str, ...] = ()
    reused: bool = False


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


def resolve_project_path(project_root: Path, raw_path: str, field_name: str) -> Path:
    """只接受项目目录内的相对路径。"""
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise RuntimeError(f"路径配置 {field_name} 不能为空。")
    relative = Path(raw_path)
    if relative.is_absolute():
        raise RuntimeError(f"路径配置 {field_name} 必须是相对路径。")

    root = project_root.resolve()
    target = (root / relative).resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise RuntimeError(f"路径配置 {field_name} 超出项目目录。") from exc
    return target


def relative_project_path(project_root: Path, path: Path) -> str:
    """生成适合日志和数据库保存的项目相对路径。"""
    try:
        return path.resolve().relative_to(project_root.resolve()).as_posix()
    except ValueError as exc:
        raise RuntimeError("目标路径超出项目目录。") from exc


def load_runtime_paths(
    project_root: Path, config_path: Path
) -> tuple[dict[str, Any], dict[str, Path]]:
    """只读取本流程需要的普通配置，不读取本地密钥文件。"""
    try:
        with config_path.open("r", encoding="utf-8-sig") as file:
            config = json.load(file)
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("无法读取 config.json。") from exc

    raw_paths = config.get("paths")
    if not isinstance(raw_paths, dict):
        raise RuntimeError("config.json 缺少 paths。")
    required = ("database", "pdfs_dir", "reports_dir", "logs_dir")
    paths = {
        name: resolve_project_path(project_root, raw_paths.get(name, ""), name)
        for name in required
    }
    return config, paths


def load_historical_selected_papers(
    connection: sqlite3.Connection,
    limit: int = DEFAULT_HISTORY_PDF_TEST_LIMIT,
) -> list[SelectedPaper]:
    """读取最近历史第一轮入围论文，按论文版本去重并限制数量。"""
    if (
        isinstance(limit, bool)
        or not isinstance(limit, int)
        or not 1 <= limit <= MAX_FIRST_ROUND_PDF_DOWNLOADS
    ):
        raise RuntimeError(
            f"PDF 下载数量必须是 1 至 {MAX_FIRST_ROUND_PDF_DOWNLOADS} 之间的整数。"
        )
    rows = connection.execute(
        """
        SELECT s.run_id, s.result_rank, s.score, s.arxiv_id, s.version, p.title
        FROM screening_results AS s
        JOIN papers AS p
          ON p.arxiv_id = s.arxiv_id AND p.version = s.version
        WHERE s.task_type = 'round1_abstract_screening'
          AND s.is_selected = 1
        ORDER BY s.run_id DESC, s.result_rank ASC, s.score DESC
        """
    ).fetchall()

    selected: list[SelectedPaper] = []
    seen: set[tuple[str, int]] = set()
    for row in rows:
        arxiv_id = str(row["arxiv_id"])
        version = int(row["version"])
        key = (arxiv_id, version)
        if key in seen:
            continue
        seen.add(key)
        selected.append(
            SelectedPaper(
                arxiv_id=arxiv_id,
                version=version,
                title=str(row["title"]),
            )
        )
        if len(selected) >= limit:
            break
    return selected


def validate_arxiv_identity(arxiv_id: str, version: int) -> None:
    """限制 URL 和文件名输入，避免数据库异常内容影响路径。"""
    if not re.fullmatch(r"[A-Za-z0-9._/-]+", arxiv_id) or version < 1:
        raise RuntimeError("历史入围论文包含无效 arXiv ID 或 version。")
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


def build_online_abs_url(paper: SelectedPaper) -> str:
    validate_arxiv_identity(paper.arxiv_id, paper.version)
    encoded_id = urllib.parse.quote(paper.arxiv_id, safe="/")
    return f"https://arxiv.org/abs/{encoded_id}v{paper.version}"


def confirm_withdrawn_without_pdf(
    paper: SelectedPaper,
    *,
    timeout_seconds: float = PDF_REQUEST_TIMEOUT_SECONDS,
    opener: Callable[..., Any] = urllib.request.urlopen,
) -> bool:
    """仅在精确PDF为404后核验官方摘要页；任何不明确状态都返回False。"""
    request = urllib.request.Request(
        build_online_abs_url(paper),
        headers={
            "User-Agent": "arXivKaleid/2.0 (local academic research tool)",
            "Accept": "text/html",
        },
    )
    try:
        with opener(request, timeout=timeout_seconds) as response:
            status = getattr(response, "status", None)
            if status is None:
                status = response.getcode()
            if status != 200:
                return False
            content = response.read(MAX_ARXIV_ABS_PAGE_BYTES + 1)
    except (TimeoutError, socket.timeout, urllib.error.URLError, OSError):
        return False
    if len(content) > MAX_ARXIV_ABS_PAGE_BYTES:
        return False
    text = content.decode("utf-8", errors="replace").casefold()
    identity = f"arxiv:{paper.arxiv_id}v{paper.version}".casefold()
    return (
        identity in text
        and "this paper has been withdrawn" in text
        and "no pdf available" in text
    )


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


def normalize_pdf_text(text: str) -> list[str]:
    """清理 PDF 提取文本的行级噪声，保留段落边界供章节识别使用。"""
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    # 常见 PDF 抽文本会把行尾断词保留为 "-\n"，这里合并为同一个词。
    normalized = re.sub(r"(?<=\w)-\n(?=\w)", "", normalized)
    lines: list[str] = []
    previous_blank = False
    for raw_line in normalized.split("\n"):
        line = re.sub(r"\s+", " ", raw_line.strip())
        if not line:
            if lines and not previous_blank:
                lines.append("")
            previous_blank = True
            continue
        lines.append(line)
        previous_blank = False
    while lines and not lines[-1]:
        lines.pop()
    return lines


def strip_section_leading_marker(line: str) -> str:
    """移除章节编号，保留标题后的标点和行内正文。"""
    header = re.sub(r"\s+", " ", line.strip())
    header = re.sub(r"^\s*section\s+", "", header, flags=re.IGNORECASE)
    header = re.sub(
        r"^\s*(?:\d+(?:\.\d+)*|[IVXLCDM]+)\s*\.\s*",
        "",
        header,
        flags=re.IGNORECASE,
    )
    header = re.sub(
        r"^\s*(?:\d+(?:\.\d+)*|[IVXLCDM]+)\s+",
        "",
        header,
        flags=re.IGNORECASE,
    )
    header = re.sub(
        r"^\s*[A-Z]\.\s+",
        "",
        header,
        flags=re.IGNORECASE,
    )
    return header


def normalize_section_header(line: str) -> str:
    """把 '1. Introduction'、'IV Conclusions' 等标题归一化为可比较文本。"""
    header = strip_section_leading_marker(line)
    header = re.sub(r"[:.\s]+$", "", header)
    normalized = header.lower()
    normalized = re.sub(r"\bsummar\s+y\b", "summary", normalized)
    normalized = re.sub(r"\s*&\s*", " and ", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return normalized


def match_section_header(
    line: str, aliases: set[str]
) -> tuple[str, str] | None:
    """匹配独立或行内章节标题，并返回别名及标题后的正文。"""
    header = strip_section_leading_marker(line)
    # 保留既有 PDF 抽取容错：SUMMAR Y 是常见的字形间空格噪声。
    header = re.sub(r"\bsummar\s+y\b", "summary", header, flags=re.IGNORECASE)
    for alias in sorted(aliases, key=len, reverse=True):
        alias_pattern = re.escape(alias).replace(r"\ ", r"\s+")
        match = re.match(
            rf"^(?P<alias>{alias_pattern})(?P<tail>.*)$",
            header,
            flags=re.IGNORECASE,
        )
        if match is None:
            continue
        tail = match.group("tail")
        if not tail:
            return alias, ""
        inline = re.match(
            r"^\s*(?:\?|:|\.(?:\s*[\-–—])?|[\-–—])\s*(?P<body>.*)$",
            tail,
        )
        if inline is not None:
            return alias, inline.group("body").strip()
    return None


def match_introduction_section_header(
    line: str, aliases: set[str]
) -> tuple[str, str] | None:
    """Introduction 专用安全匹配；裸 ASCII 复合词不能成为标题。"""
    header = strip_section_leading_marker(line)
    header = re.sub(r"\bsummar\s+y\b", "summary", header, flags=re.IGNORECASE)
    for alias in sorted(aliases, key=len, reverse=True):
        alias_pattern = re.escape(alias).replace(r"\ ", r"\s+")
        match = re.match(
            rf"^(?P<alias>{alias_pattern})(?P<tail>.*)$",
            header,
            flags=re.IGNORECASE,
        )
        if match is None:
            continue
        tail = match.group("tail")
        if not tail:
            return alias, ""
        inline = re.match(
            # ASCII 连字符只有两侧空格时才是分隔符；Unicode 破折号、
            # 冒号、句点、问号和明确的 ``. —`` 形式继续受支持。
            r"^(?:\s*(?:\?|:|\.(?:\s*[\-–—])?|[–—])\s*|\s+-\s+)"
            r"(?P<body>.*)$",
            tail,
        )
        if inline is not None:
            return alias, inline.group("body").strip()
    return None


def section_header_matches(line: str, aliases: set[str]) -> bool:
    """判断标题行是否匹配目标章节。"""
    return match_section_header(line, aliases) is not None


def introduction_section_header_matches(line: str, aliases: set[str]) -> bool:
    """只用于 Introduction 提取路径的安全标题判断。"""
    return match_introduction_section_header(line, aliases) is not None


def is_numbered_main_section_heading(line: str) -> bool:
    """识别带数字或罗马数字编号的主章节标题，尽量避开图表说明。"""
    stripped = line.strip()
    if not stripped or len(stripped) > 140:
        return False
    if stripped.lower().startswith(("figure", "fig.", "table")):
        return False
    return bool(
        re.match(
            r"^\s*(?:\d+(?:\.\d+)*|[IVXLCDM]+)"
            r"(?:\s*\.\s*|\s+)[A-Z]",
            stripped,
            flags=re.IGNORECASE,
        )
    )


def roman_numeral_value(marker: str) -> int | None:
    """把合法罗马数字转换为整数；非法组合不参与章节层级判断。"""
    values = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100, "D": 500, "M": 1000}
    normalized = marker.upper()
    if not normalized or re.fullmatch(r"[IVXLCDM]+", normalized) is None:
        return None
    total = 0
    previous = 0
    for char in reversed(normalized):
        value = values[char]
        if value < previous:
            total -= value
        else:
            total += value
            previous = value
    # 反向格式化可排除 IIX、VX 等非标准组合。
    numerals = (
        (1000, "M"), (900, "CM"), (500, "D"), (400, "CD"),
        (100, "C"), (90, "XC"), (50, "L"), (40, "XL"),
        (10, "X"), (9, "IX"), (5, "V"), (4, "IV"), (1, "I"),
    )
    remaining = total
    rebuilt: list[str] = []
    for value, symbol in numerals:
        while remaining >= value:
            rebuilt.append(symbol)
            remaining -= value
    return total if "".join(rebuilt) == normalized else None


def parse_numbered_section_heading(line: str) -> NumberedSectionHeading | None:
    """按标题白名单解析编号章节，不尝试枚举公式或特殊字符。"""
    stripped = line.strip()
    if not stripped or len(stripped) > 140:
        return None
    match = re.match(
        r"^(?P<marker>\d+(?:\.\d+)*|[IVXLCDM]+)"
        r"(?:\s*\.\s*|\s+)(?P<title>\S.*)$",
        stripped,
        flags=re.IGNORECASE,
    )
    if match is None:
        return None
    title = match.group("title").strip()
    title_prefix = title.lstrip("\"'“‘")
    starts_like_title = bool(
        re.fullmatch(
            r"(?:[A-Z][A-Za-z0-9'’\"“”&(),:+/\-]*|"
            r"\d+-[A-Za-z][A-Za-z0-9'’\"“”&(),:+/\-]*|"
            r"\d+\+\d+)"
            r"(?:\s+[A-Za-z0-9][A-Za-z0-9'’\"“”&(),:+/\-]*)*",
            title_prefix,
        )
    )
    if not starts_like_title:
        return None
    if not is_short_heading_like(line):
        return None

    marker = match.group("marker")
    if marker[0].isdigit():
        path = tuple(int(part) for part in marker.split("."))
        return NumberedSectionHeading("arabic", path, title)
    roman_value = roman_numeral_value(marker)
    if roman_value is None:
        return None
    return NumberedSectionHeading("roman", (roman_value,), title)


def is_numbered_top_level_section_heading(line: str) -> bool:
    """只接受 1、2、IV 等最高层级编号，不把 2.1 子章节当作主章节。"""
    stripped = line.strip()
    if not stripped or len(stripped) > 140:
        return False
    return bool(
        re.match(
            r"^\s*(?:\d+|[IVXLCDM]+)(?:\s*\.\s*|\s+)[A-Z]",
            stripped,
            flags=re.IGNORECASE,
        )
    )


def is_short_heading_like(line: str) -> bool:
    """识别短标题外形，排除普通句子、公式和图表坐标标签。"""
    stripped = strip_section_leading_marker(line).strip()
    if not stripped or len(stripped) > 100:
        return False
    if stripped.endswith((".", "!", "?", ",", ";")):
        return False
    if re.search(r"\[[^\]]*\]|\b\d{2,}\b", stripped):
        return False
    words = re.findall(r"[A-Za-z]+", stripped)
    return 1 <= len(words) <= 10


def has_heading_style_capitalization(line: str) -> bool:
    """要求未知编号标题具有标题式大小写，排除 ``1 We assume ...`` 脚注。"""
    stripped = strip_section_leading_marker(line).strip()
    words = re.findall(r"[A-Za-z]+", stripped)
    if not words:
        return False
    if stripped.isupper():
        return True
    lowercase_connectors = {
        "a",
        "an",
        "and",
        "by",
        "for",
        "from",
        "in",
        "of",
        "on",
        "or",
        "the",
        "to",
        "with",
    }
    return all(
        word.lower() in lowercase_connectors or word[:1].isupper()
        for word in words
    )


def is_trusted_main_section_heading(line: str) -> bool:
    """保守识别主章节；未知短大写标签不能仅凭外观成为章节。"""
    if section_header_matches(line, KNOWN_SECTION_ALIASES):
        return True
    if (
        is_numbered_main_section_heading(line)
        and is_short_heading_like(line)
        and has_heading_style_capitalization(line)
    ):
        return True
    if not is_short_heading_like(line):
        return False
    normalized = normalize_section_header(line)
    semantic_aliases = (
        INTRODUCTION_SEMANTIC_SECTION_ALIASES
        | INTRODUCTION_FALLBACK_REJECT_ALIASES
        | CONCLUSION_SEMANTIC_CUES
    )
    return normalized_header_startswith_alias(normalized, semantic_aliases)


def normalized_header_startswith_alias(
    normalized: str, aliases: set[str]
) -> bool:
    return any(
        normalized == alias or normalized.startswith(f"{alias} ")
        for alias in aliases
    )


def is_terminal_section_heading(line: str) -> bool:
    return section_header_matches(line, TERMINAL_SECTION_ALIASES)


def find_first_terminal_index(
    lines: list[str],
    *,
    start_index: int = 0,
    excluded_indexes: set[int] | None = None,
) -> int | None:
    excluded = excluded_indexes or set()
    candidates: list[int] = []
    for index in range(start_index, len(lines)):
        if index in excluded:
            continue
        if is_terminal_section_heading(lines[index]):
            candidates.append(index)
            break

    bibliography_index = find_numbered_bibliography_index(
        lines,
        start_index=start_index,
        excluded_indexes=excluded,
    )
    if bibliography_index is not None:
        candidates.append(bibliography_index)
        # 有些论文没有致谢标题，只在参考文献前写 "The author thanks..."。
        acknowledgement_start = max(start_index, bibliography_index - 20)
        for index in range(acknowledgement_start, bibliography_index):
            if index in excluded:
                continue
            if re.match(
                r"^(?:the\s+authors?|we|i)\s+"
                r"(?:thank|thanks|acknowledg(?:e|es|ed))\b",
                lines[index].strip(),
                flags=re.IGNORECASE,
            ):
                candidates.append(index)
                break
    return min(candidates) if candidates else None


def find_numbered_bibliography_index(
    lines: list[str],
    *,
    start_index: int = 0,
    excluded_indexes: set[int] | None = None,
) -> int | None:
    """以连续的 [1]、[2]、[3] 条目识别没有 References 标题的文献表。"""
    excluded = excluded_indexes or set()
    pattern = re.compile(r"^\s*\[(?P<number>\d{1,3})\]\s+\S")
    for index in range(start_index, len(lines)):
        if index in excluded:
            continue
        first = pattern.match(lines[index])
        if first is None or int(first.group("number")) != 1:
            continue
        expected = 2
        for candidate_index in range(index + 1, min(len(lines), index + 80)):
            if candidate_index in excluded:
                continue
            candidate = lines[candidate_index]
            match = pattern.match(candidate)
            if match is None:
                continue
            number = int(match.group("number"))
            if number == expected:
                expected += 1
                if expected == 4:
                    return index
            elif number > expected:
                break
    return None


def is_strong_inline_section_heading(line: str) -> bool:
    """识别 ``Title.—Body`` 这类具有明确分隔形态的内联章节。"""
    stripped = line.strip()
    if not stripped or len(stripped) > 120:
        return False
    marker_stripped = strip_section_leading_marker(stripped)
    strong_inline = re.match(
        r"^(?P<title>[A-Z][A-Za-z0-9'’/&(),+\- ]{1,100})\.\s*[–—-]\s*\S",
        marker_stripped,
    )
    if strong_inline is not None:
        words = strong_inline.group("title").split()
        if 1 <= len(words) <= 10:
            return True
    return False


def is_probable_section_heading(line: str) -> bool:
    """保守判断章节标题，用于限制 intro/conclusion 不吞入整篇论文。"""
    stripped = line.strip()
    if not stripped or len(stripped) > 120:
        return False
    if section_header_matches(stripped, KNOWN_SECTION_ALIASES):
        return True
    # PRL 等双栏论文常把短标题和正文写成 ``Heading.—Body``。
    if is_strong_inline_section_heading(stripped):
        return True
    if re.fullmatch(
        r"(?:\d+(?:\.\d+)*|[IVXLCDM]+)\.?\s+"
        r"[A-Z][A-Za-z0-9 ,:/&()\-]{1,100}",
        stripped,
    ):
        return True
    words = stripped.split()
    return stripped.isupper() and 1 <= len(words) <= 8


def find_conclusion_fallback_index(
    lines: list[str],
) -> tuple[int | None, int | None]:
    """没有正式结论时，选择终止标记前最后一个可信科学主章节。"""
    terminal_index = find_first_terminal_index(lines)
    search_end = terminal_index if terminal_index is not None else len(lines)
    candidates: list[int] = []
    for index in range(search_end):
        line = lines[index]
        normalized = normalize_section_header(line)
        has_conclusion_semantics = (
            section_header_matches(
                line, CONCLUSION_SECTION_ALIASES | DISCUSSION_SECTION_ALIASES
            )
            or (
                is_short_heading_like(line)
                and normalized_header_startswith_alias(
                    normalized, CONCLUSION_SEMANTIC_CUES
                )
            )
        )
        is_numbered_top_level = (
            is_numbered_top_level_section_heading(line)
            and is_short_heading_like(line)
            and has_heading_style_capitalization(line)
        )
        if not has_conclusion_semantics and not is_numbered_top_level:
            continue
        if normalized_header_startswith_alias(
            normalized,
            ABSTRACT_SECTION_ALIASES
            | KEYWORDS_SECTION_ALIASES
            | INTRODUCTION_SECTION_ALIASES
            | INTRODUCTION_SEMANTIC_SECTION_ALIASES,
        ):
            continue
        words = set(re.findall(r"[a-z]+", normalized))
        if (
            not has_conclusion_semantics
            and words & CONCLUSION_FALLBACK_REJECT_WORDS
        ):
            continue
        candidates.append(index)

    for index in reversed(candidates):
        inline_match = match_section_header(
            lines[index],
            CONCLUSION_SECTION_ALIASES | DISCUSSION_SECTION_ALIASES,
        )
        inline_body = inline_match[1] if inline_match is not None else ""
        body_lines = ([inline_body] if inline_body else []) + lines[
            index + 1 : search_end
        ]
        body = clean_section_lines(body_lines, MAX_CONCLUSION_CHARS)
        if len(re.findall(r"[A-Za-z]+", body)) >= 3:
            return index, terminal_index
    return None, terminal_index


def is_introduction_fallback_candidate(line: str) -> bool:
    normalized = normalize_section_header(line)
    if not is_numbered_main_section_heading(line):
        return False
    return normalized_header_startswith_alias(
        normalized, INTRODUCTION_SEMANTIC_SECTION_ALIASES
    )


def find_first_trusted_main_heading_index(
    lines: list[str], *, start_index: int, end_index: int | None = None
) -> int | None:
    """查找可信主章节，避免把坐标轴、刻度或对象标签当成章节。"""
    search_end = len(lines) if end_index is None else min(end_index, len(lines))
    for index in range(start_index, search_end):
        if is_trusted_main_section_heading(lines[index]):
            return index
    return None


def is_keywords_continuation(line: str) -> bool:
    """识别 Keywords 标题后的短关键词续行。"""
    stripped = line.strip()
    if not stripped or len(stripped) > 120:
        return False
    if re.search(r"[.!?][\"')\]]*$", stripped):
        return False
    alpha_words = re.findall(r"[A-Za-z]+", stripped)
    return (
        stripped[:1].islower()
        or "," in stripped
        or ";" in stripped
        or len(alpha_words) <= 5
    )


def find_after_keywords_index(lines: list[str], keywords_index: int) -> int:
    """跳过 Keywords 本行及可能的关键词续行，返回后续正文起点。"""
    inline_match = match_section_header(
        lines[keywords_index], KEYWORDS_SECTION_ALIASES
    )
    has_inline_keywords = bool(inline_match and inline_match[1])
    index = keywords_index + 1
    while index < len(lines) and not lines[index].strip():
        index += 1
    if not has_inline_keywords and index < len(lines):
        index += 1
    while index < len(lines) and is_keywords_continuation(lines[index]):
        index += 1
    while index < len(lines) and not lines[index].strip():
        index += 1
    return index


def find_blank_separated_intro_start(
    lines: list[str], *, abstract_index: int, search_end: int
) -> tuple[int, int] | None:
    """无 Keywords 时，仅以明确空行分隔摘要和无标题引言。"""
    seen_abstract_body = False
    for index in range(abstract_index + 1, search_end):
        if lines[index].strip():
            seen_abstract_body = True
            continue
        if not seen_abstract_body:
            continue
        body_start = index + 1
        while body_start < search_end and not lines[body_start].strip():
            body_start += 1
        if body_start < search_end:
            return body_start, index
    return None


def find_unheaded_introduction_range(
    lines: list[str],
    *,
    abstract_index: int | None,
    keywords_index: int | None,
    search_end: int,
) -> tuple[int, int, int] | None:
    """提取 Abstract/Keywords 后到首个可信主章节前的连续说明性正文。"""
    if abstract_index is None:
        return None
    if keywords_index is not None:
        body_start = find_after_keywords_index(lines, keywords_index)
        abstract_end = keywords_index
    else:
        first_main = find_first_trusted_main_heading_index(
            lines, start_index=abstract_index + 1, end_index=search_end
        )
        if first_main is None:
            return None
        blank_split = find_blank_separated_intro_start(
            lines, abstract_index=abstract_index, search_end=first_main
        )
        if blank_split is None:
            return None
        body_start, abstract_end = blank_split

    first_main = find_first_trusted_main_heading_index(
        lines, start_index=body_start, end_index=search_end
    )
    if first_main is None or first_main <= body_start:
        return None
    body = clean_section_lines(
        lines[body_start:first_main], MAX_INTRODUCTION_CHARS
    )
    if (
        len(body.strip()) < MIN_REQUIRED_SECTION_CHARS
        or len(re.findall(r"[A-Za-z]+", body)) < 8
    ):
        return None
    return body_start, first_main, abstract_end


def find_introduction_fallback_index(
    lines: list[str],
    *,
    abstract_index: int | None,
    abstract_end: int | None,
    conclusion_index: int | None,
) -> int | None:
    """没有明确引言时，把 abstract 后早期非方法数据类主章节作为保守备选。"""
    if abstract_index is None:
        return None
    early_limit = max(abstract_index + 1, int(len(lines) * 0.4))
    search_start = abstract_end if abstract_end is not None else abstract_index + 1
    search_end = min(
        early_limit,
        conclusion_index if conclusion_index is not None else len(lines),
    )
    if search_start >= search_end:
        return None
    for index in range(search_start, search_end):
        if is_introduction_fallback_candidate(lines[index]):
            return index
    return None


def find_section_header_index(
    lines: list[str],
    aliases: set[str],
    *,
    start_index: int = 0,
    end_index: int | None = None,
) -> int | None:
    search_end = len(lines) if end_index is None else min(end_index, len(lines))
    for index in range(start_index, search_end):
        if section_header_matches(lines[index], aliases):
            return index
    return None


def find_next_probable_heading_index(
    lines: list[str],
    *,
    start_index: int,
    stop_aliases: set[str] | None = None,
) -> int | None:
    """保留既有宽松边界行为，供 Conclusion 等旧逻辑继续使用。"""
    aliases = stop_aliases or set()
    for index in range(start_index, len(lines)):
        if section_header_matches(
            lines[index], aliases
        ) or is_probable_section_heading(lines[index]):
            return index
    return None


def find_trusted_introduction_end_index(
    lines: list[str],
    *,
    introduction_index: int,
    stop_aliases: set[str] | None = None,
) -> int | None:
    """只以可信编号、已知科学章节或硬终止标记结束 Introduction。"""
    aliases = stop_aliases or set()
    parent_heading = parse_numbered_section_heading(lines[introduction_index])
    known_boundaries = (
        COMMON_SECTION_BOUNDARY_ALIASES
        | CONCLUSION_SECTION_ALIASES
        | DISCUSSION_SECTION_ALIASES
        | TERMINAL_SECTION_ALIASES
    )
    for index in range(introduction_index + 1, len(lines)):
        line = lines[index]
        if introduction_section_header_matches(
            line, aliases | known_boundaries
        ):
            return index
        if is_strong_inline_section_heading(line):
            return index
        candidate = parse_numbered_section_heading(line)
        if parent_heading is None or candidate is None:
            continue
        same_family = candidate.family == parent_heading.family
        is_descendant = (
            same_family
            and len(candidate.path) > len(parent_heading.path)
            and candidate.path[: len(parent_heading.path)]
            == parent_heading.path
        )
        if is_descendant:
            continue
        if len(candidate.path) <= len(parent_heading.path) or same_family:
            return index
    return None


def clean_section_lines(lines: list[str], max_chars: int) -> str:
    cleaned_lines = list(lines)
    while cleaned_lines and re.fullmatch(r"\d{1,3}", cleaned_lines[-1].strip()):
        cleaned_lines.pop()
    text = "\n".join(cleaned_lines).strip()
    text = re.sub(r"\n{3,}", "\n\n", text)
    if len(text) > max_chars:
        text = text[:max_chars].rstrip()
    return text


def extract_section_body(
    lines: list[str],
    header_index: int | None,
    *,
    end_index: int | None,
    max_chars: int,
    header_aliases: set[str],
) -> str:
    if header_index is None:
        return ""
    inline_match = match_section_header(lines[header_index], header_aliases)
    inline_body = inline_match[1] if inline_match is not None else ""
    body_start = header_index + 1
    body_end = end_index if end_index is not None else len(lines)
    while body_start < body_end and not lines[body_start].strip():
        body_start += 1
    while body_end > body_start and not lines[body_end - 1].strip():
        body_end -= 1
    body_lines = ([inline_body] if inline_body else []) + lines[body_start:body_end]
    if not body_lines:
        return ""
    return clean_section_lines(body_lines, max_chars)


TOC_NUMBERED_ENTRY_PATTERN = re.compile(
    r"^\s*(?:\d+(?:\.\d+)*)\s+.+"
    r"(?:(?:\.\s*){3,}|\s)\d{1,4}\s*$"
)
TOC_DOTTED_ENTRY_PATTERN = re.compile(
    r"^\s*\S.*(?:\.\s*){3,}(?:\d{1,4}|[ivxlcdm]+)\s*$",
    flags=re.IGNORECASE,
)
TOC_HEADER_PATTERN = re.compile(
    r"^\s*(?:table\s+of\s+)?contents?\s*[:.]?\s*$",
    flags=re.IGNORECASE,
)


def is_toc_entry_line(line: str) -> bool:
    """识别带点线或尾部页码的目录项，不限定具体章节名称。"""
    stripped = line.strip()
    return bool(
        TOC_NUMBERED_ENTRY_PATTERN.match(stripped)
        or TOC_DOTTED_ENTRY_PATTERN.match(stripped)
    )


def is_toc_header_line(line: str) -> bool:
    """识别显式目录标题。"""
    return TOC_HEADER_PATTERN.match(line.strip()) is not None


def find_toc_line_indexes(lines: list[str]) -> set[int]:
    """定位目录区域，使其中任何标题都不参与 Introduction 边界判断。"""
    excluded: set[int] = set()
    entry_indexes = [
        index for index, line in enumerate(lines) if is_toc_entry_line(line)
    ]

    # 没有显式 Contents 标题时，连续的目录项簇仍作为目录区域处理。
    cluster: list[int] = []
    for index in entry_indexes + [len(lines) + 10]:
        if cluster and index - cluster[-1] > 3:
            if len(cluster) >= 2:
                excluded.update(range(cluster[0], cluster[-1] + 1))
            cluster = []
        cluster.append(index)

    for header_index, line in enumerate(lines):
        if not is_toc_header_line(line):
            continue

        # 优先使用目录项密集区确定目录末尾；章节名、点线形式均不硬编码。
        nearby_entries = [
            index
            for index in entry_indexes
            if header_index < index <= min(len(lines) - 1, header_index + 400)
        ]
        if nearby_entries and nearby_entries[0] <= header_index + 20:
            last_entry = nearby_entries[0]
            for index in nearby_entries[1:]:
                if index - last_entry > 3:
                    break
                last_entry = index
            excluded.update(range(header_index, last_entry + 1))

        # 无点线、无页码目录可通过重复的 Introduction 标题确定正文起点。
        introduction_candidates = [
            index
            for index in range(header_index + 1, min(len(lines), header_index + 500))
            if introduction_section_header_matches(
                lines[index], INTRODUCTION_SECTION_ALIASES
            )
        ]
        if len(introduction_candidates) >= 2:
            excluded.update(range(header_index, introduction_candidates[1]))

    return {index for index in excluded if index < len(lines)}


def is_toc_like_section_text(text: str) -> bool:
    """识别主要由带页码目录项组成的伪章节正文。"""
    nonempty = [line.strip() for line in text.splitlines() if line.strip()]
    if not nonempty:
        return False
    toc_entries = sum(is_toc_entry_line(line) for line in nonempty)
    return toc_entries >= 1 and toc_entries * 2 >= len(nonempty)


def find_best_explicit_introduction_index(
    lines: list[str],
    *,
    search_end: int,
    toc_indexes: set[int] | None = None,
) -> int | None:
    """在目录与正文重复标题中选择后面具有连续正文的 Introduction。"""
    excluded = toc_indexes if toc_indexes is not None else find_toc_line_indexes(lines)
    candidates = [
        index
        for index in range(search_end)
        if index not in excluded
        if introduction_section_header_matches(
            lines[index], INTRODUCTION_SECTION_ALIASES
        )
    ]
    if not candidates:
        return None
    for index in candidates:
        end_index = find_trusted_introduction_end_index(
            lines,
            introduction_index=index,
            stop_aliases=CONCLUSION_SECTION_ALIASES | TERMINAL_SECTION_ALIASES,
        )
        body = extract_section_body(
            lines,
            index,
            end_index=end_index,
            max_chars=MAX_INTRODUCTION_CHARS,
            header_aliases=INTRODUCTION_SECTION_ALIASES,
        )
        if (
            len(body.strip()) >= MIN_REQUIRED_SECTION_CHARS
            and len(re.findall(r"[A-Za-z]+", body)) >= 8
            and not is_toc_like_section_text(body)
        ):
            return index
    # 目录候选不得作为正文；非目录候选即使质量不足，也保留给质量门控审计。
    non_toc_candidates = [index for index in candidates if index not in excluded]
    return non_toc_candidates[0] if non_toc_candidates else None


def extract_sections_from_text(text: str) -> ExtractedPdfSections:
    """从纯文本中提取引言和结论类章节；摘要标题只用于定位正文边界。"""
    lines = normalize_pdf_text(text)
    if not lines:
        return ExtractedPdfSections("", "", "")

    abstract_aliases = ABSTRACT_SECTION_ALIASES
    introduction_aliases = INTRODUCTION_SECTION_ALIASES
    conclusion_aliases = CONCLUSION_SECTION_ALIASES
    terminal_aliases = TERMINAL_SECTION_ALIASES

    abstract_index = find_section_header_index(lines, abstract_aliases)
    toc_indexes = find_toc_line_indexes(lines)
    introduction_terminal_index = find_first_terminal_index(
        lines,
        excluded_indexes=toc_indexes,
    )
    introduction_main_text_end = (
        introduction_terminal_index
        if introduction_terminal_index is not None
        else len(lines)
    )
    # Conclusion 等路径继续使用原终止边界，避免 Introduction 修复改变旧行为。
    terminal_index = find_first_terminal_index(lines)
    main_text_end = terminal_index if terminal_index is not None else len(lines)
    legacy_toc_entry_indexes = {
        index
        for index, line in enumerate(lines)
        if TOC_NUMBERED_ENTRY_PATTERN.match(line.strip()) is not None
    }
    legacy_introduction_index_for_conclusion = (
        find_best_explicit_introduction_index(
            lines,
            search_end=main_text_end,
            toc_indexes=legacy_toc_entry_indexes,
        )
    )
    introduction_index = find_best_explicit_introduction_index(
        lines,
        search_end=introduction_main_text_end,
        toc_indexes=toc_indexes,
    )
    keywords_index = find_section_header_index(
        lines,
        KEYWORDS_SECTION_ALIASES,
        start_index=(abstract_index + 1 if abstract_index is not None else 0),
        end_index=main_text_end,
    )

    conclusion_search_start = (
        legacy_introduction_index_for_conclusion + 1
        if legacy_introduction_index_for_conclusion is not None
        else 0
    )
    conclusion_index = find_section_header_index(
        lines,
        conclusion_aliases,
        start_index=conclusion_search_start,
        end_index=main_text_end,
    )
    conclusion_fallback_end = None
    conclusion_body_aliases = conclusion_aliases
    if conclusion_index is None:
        conclusion_index, conclusion_fallback_end = (
            find_conclusion_fallback_index(lines)
        )
        conclusion_body_aliases = KNOWN_SECTION_ALIASES

    introduction_search_end = min(
        index
        for index in (conclusion_index, terminal_index, len(lines))
        if index is not None
    )
    unheaded_introduction = None
    if introduction_index is None:
        unheaded_introduction = find_unheaded_introduction_range(
            lines,
            abstract_index=abstract_index,
            keywords_index=keywords_index,
            search_end=introduction_search_end,
        )
        if unheaded_introduction is None:
            introduction_index = find_introduction_fallback_index(
                lines,
                abstract_index=abstract_index,
                abstract_end=(
                    find_after_keywords_index(lines, keywords_index)
                    if keywords_index is not None
                    else None
                ),
                conclusion_index=conclusion_index,
            )

    introduction_end = None
    extraction_quality_warnings: list[str] = []
    if introduction_index is not None:
        introduction_end = find_trusted_introduction_end_index(
            lines,
            introduction_index=introduction_index,
            stop_aliases=conclusion_aliases | terminal_aliases,
        )
        if introduction_end is None:
            extraction_quality_warnings.append(
                "missing_trusted_introduction_end"
            )

    conclusion_end = None
    if conclusion_index is not None:
        conclusion_end = conclusion_fallback_end
        if conclusion_end is None:
            next_heading = find_next_probable_heading_index(
                lines,
                start_index=conclusion_index + 1,
                stop_aliases=terminal_aliases,
            )
            terminal_index = find_first_terminal_index(
                lines, start_index=conclusion_index + 1
            )
            boundaries = [
                index
                for index in (next_heading, terminal_index)
                if index is not None
            ]
            conclusion_end = min(boundaries) if boundaries else None

    if unheaded_introduction is not None:
        introduction_text = clean_section_lines(
            lines[unheaded_introduction[0] : unheaded_introduction[1]],
            MAX_INTRODUCTION_CHARS,
        )
    else:
        introduction_text = extract_section_body(
            lines,
            introduction_index,
            end_index=introduction_end,
            max_chars=MAX_INTRODUCTION_CHARS,
            header_aliases=introduction_aliases
            | INTRODUCTION_SEMANTIC_SECTION_ALIASES,
        )

    return ExtractedPdfSections(
        # 摘要统一使用 arXiv metadata；保留空字段仅用于数据库兼容。
        abstract_text="",
        introduction_text=introduction_text,
        conclusion_text=extract_section_body(
            lines,
            conclusion_index,
            end_index=conclusion_end,
            max_chars=MAX_CONCLUSION_CHARS,
            header_aliases=conclusion_body_aliases,
        ),
        quality_warnings=tuple(extraction_quality_warnings),
    )


def extract_text_from_pdf_file(pdf_path: Path) -> str:
    """使用已安装的 PDF 文本库提取文本；缺库时返回稳定错误类型。"""
    attempted = False
    if importlib.util.find_spec("pypdf") is not None:
        attempted = True
        try:
            from pypdf import PdfReader  # type: ignore[import-not-found]

            reader = PdfReader(str(pdf_path))
            return "\n".join(page.extract_text() or "" for page in reader.pages)
        except Exception:
            pass

    if importlib.util.find_spec("pdfplumber") is not None:
        attempted = True
        try:
            import pdfplumber  # type: ignore[import-not-found]

            with pdfplumber.open(pdf_path) as pdf:
                return "\n".join(page.extract_text() or "" for page in pdf.pages)
        except Exception:
            pass

    if not attempted:
        raise PdfTextExtractionError("missing_pdf_text_extractor")
    raise PdfTextExtractionError("pdf_text_extraction_failed")


def validate_pdf_sections_schema(connection: sqlite3.Connection) -> None:
    """只接受当前项目已确认的宽表结构；不在这里做隐式迁移。"""
    rows = connection.execute("PRAGMA table_info(pdf_sections)").fetchall()
    columns = tuple(row["name"] if isinstance(row, sqlite3.Row) else row[1] for row in rows)
    primary_key = tuple(
        row["name"] if isinstance(row, sqlite3.Row) else row[1]
        for row in sorted(
            rows,
            key=lambda row: row["pk"] if isinstance(row, sqlite3.Row) else row[5],
        )
        if (row["pk"] if isinstance(row, sqlite3.Row) else row[5])
    )
    if columns != EXPECTED_PDF_SECTIONS_COLUMNS:
        raise RuntimeError("pdf_sections_schema_mismatch")
    if primary_key != EXPECTED_PDF_SECTIONS_PRIMARY_KEY:
        raise RuntimeError("pdf_sections_primary_key_mismatch")


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


TERMINAL_SECTION_CONTAMINATION_PATTERN = re.compile(
    r"(?im)^\s*(?:\d+\.?\s*)?(?:references|bibliography|appendix|appendices|"
    r"acknowledg(?:e)?ments?|data availability(?: statement)?|"
    r"supplementary materials?)\s*$"
)


def pdf_section_quality_warnings(
    introduction_text: str, conclusion_text: str
) -> tuple[str, ...]:
    """只检查第二轮必需章节；PDF abstract 缺失不影响复用。"""
    warnings: list[str] = []
    for name, text in (
        ("introduction", introduction_text),
        ("conclusion", conclusion_text),
    ):
        stripped = text.strip()
        if not stripped:
            warnings.append(f"missing_{name}")
            continue
        if len(stripped) < MIN_REQUIRED_SECTION_CHARS:
            warnings.append(f"short_{name}")
        if TERMINAL_SECTION_CONTAMINATION_PATTERN.search(stripped):
            warnings.append(f"terminal_heading_in_{name}")
    return tuple(warnings)


def pdf_section_identity_table_exists(connection: sqlite3.Connection) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (PDF_SECTION_IDENTITY_TABLE,),
    ).fetchone()
    return row is not None


def ensure_pdf_section_identity_schema(connection: sqlite3.Connection) -> None:
    """为可写提取入口创建独立来源身份表，不改动既有章节宽表。"""
    with connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS pdf_section_extraction_identity (
                arxiv_id TEXT NOT NULL,
                version INTEGER NOT NULL,
                source_pdf_sha256 TEXT NOT NULL,
                extractor_version TEXT NOT NULL,
                quality_warnings TEXT NOT NULL DEFAULT '[]',
                checked_at TEXT NOT NULL,
                PRIMARY KEY (arxiv_id, version)
            )
            """
        )


def build_pdf_section_result_from_text(
    arxiv_id: str,
    version: int,
    local_pdf_path: str,
    text: str,
    *,
    source_pdf_sha256: str = "",
    extractor_version: str = PDF_SECTION_EXTRACTOR_VERSION,
) -> PdfSectionExtractionResult:
    if not text.strip():
        return PdfSectionExtractionResult(
            arxiv_id=arxiv_id,
            version=version,
            local_pdf_path=local_pdf_path,
            extraction_status="failed",
            failure_reason="empty_pdf_text",
            source_pdf_sha256=source_pdf_sha256,
            extractor_version=extractor_version,
        )

    sections = extract_sections_from_text(text)
    found = {
        "introduction": bool(sections.introduction_text),
        "conclusion": bool(sections.conclusion_text),
    }
    if not any(found.values()):
        return PdfSectionExtractionResult(
            arxiv_id=arxiv_id,
            version=version,
            local_pdf_path=local_pdf_path,
            extraction_status="failed",
            failure_reason="target_sections_not_found",
            source_pdf_sha256=source_pdf_sha256,
            extractor_version=extractor_version,
        )
    missing = [name for name, ok in found.items() if not ok]
    quality_warnings = tuple(
        dict.fromkeys(
            pdf_section_quality_warnings(
                sections.introduction_text, sections.conclusion_text
            )
            + sections.quality_warnings
        )
    )
    failure_reasons = [f"missing_{name}" for name in missing]
    failure_reasons.extend(
        f"quality_{warning}"
        for warning in quality_warnings
        if warning not in failure_reasons
    )
    return PdfSectionExtractionResult(
        arxiv_id=arxiv_id,
        version=version,
        local_pdf_path=local_pdf_path,
        extraction_status="partial" if failure_reasons else "extracted",
        abstract_text=sections.abstract_text,
        introduction_text=sections.introduction_text,
        conclusion_text=sections.conclusion_text,
        failure_reason=",".join(failure_reasons) if failure_reasons else None,
        source_pdf_sha256=source_pdf_sha256,
        extractor_version=extractor_version,
        quality_warnings=quality_warnings,
    )


def save_pdf_section_result(
    connection: sqlite3.Connection,
    result: PdfSectionExtractionResult,
) -> None:
    """按当前宽表结构幂等写入 PDF 章节提取结果。"""
    validate_pdf_sections_schema(connection)
    identity_table_available = pdf_section_identity_table_exists(connection)
    with connection:
        connection.execute(
            """
            INSERT INTO pdf_sections (
                arxiv_id, version, abstract_text, introduction_text,
                conclusion_text, extraction_status, extracted_at,
                failure_reason
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(arxiv_id, version) DO UPDATE SET
                abstract_text = excluded.abstract_text,
                introduction_text = excluded.introduction_text,
                conclusion_text = excluded.conclusion_text,
                extraction_status = excluded.extraction_status,
                extracted_at = excluded.extracted_at,
                failure_reason = excluded.failure_reason
            """,
            (
                result.arxiv_id,
                result.version,
                result.abstract_text,
                result.introduction_text,
                result.conclusion_text,
                result.extraction_status,
                current_time_iso(),
                result.failure_reason,
            ),
        )
        if identity_table_available:
            connection.execute(
                """
                INSERT INTO pdf_section_extraction_identity (
                    arxiv_id, version, source_pdf_sha256, extractor_version,
                    quality_warnings, checked_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(arxiv_id, version) DO UPDATE SET
                    source_pdf_sha256 = excluded.source_pdf_sha256,
                    extractor_version = excluded.extractor_version,
                    quality_warnings = excluded.quality_warnings,
                    checked_at = excluded.checked_at
                """,
                (
                    result.arxiv_id,
                    result.version,
                    result.source_pdf_sha256,
                    result.extractor_version,
                    json.dumps(
                        list(result.quality_warnings),
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                    current_time_iso(),
                ),
            )


def load_reusable_pdf_section_result(
    connection: sqlite3.Connection,
    *,
    arxiv_id: str,
    version: int,
    local_pdf_path: str,
    source_pdf_sha256: str,
    extractor_version: str,
) -> PdfSectionExtractionResult | None:
    """只有来源身份一致且必需章节质量有效时才复用。"""
    if not pdf_section_identity_table_exists(connection):
        return None
    row = connection.execute(
        """
        SELECT s.abstract_text, s.introduction_text, s.conclusion_text,
               s.extraction_status, s.failure_reason,
               i.source_pdf_sha256, i.extractor_version, i.quality_warnings
        FROM pdf_sections AS s
        JOIN pdf_section_extraction_identity AS i
          ON i.arxiv_id = s.arxiv_id AND i.version = s.version
        WHERE s.arxiv_id = ? AND s.version = ?
        """,
        (arxiv_id, version),
    ).fetchone()
    if row is None:
        return None
    get = (
        (lambda key: row[key])
        if isinstance(row, sqlite3.Row)
        else lambda key: row[
            {
                "abstract_text": 0,
                "introduction_text": 1,
                "conclusion_text": 2,
                "extraction_status": 3,
                "failure_reason": 4,
                "source_pdf_sha256": 5,
                "extractor_version": 6,
                "quality_warnings": 7,
            }[key]
        ]
    )
    introduction = str(get("introduction_text") or "")
    conclusion = str(get("conclusion_text") or "")
    try:
        stored_warnings = json.loads(str(get("quality_warnings") or "[]"))
    except json.JSONDecodeError:
        return None
    if (
        get("source_pdf_sha256") != source_pdf_sha256
        or get("extractor_version") != extractor_version
        or get("extraction_status") == "failed"
        or not introduction.strip()
        or not conclusion.strip()
        or stored_warnings != []
        or pdf_section_quality_warnings(introduction, conclusion)
    ):
        return None
    return PdfSectionExtractionResult(
        arxiv_id=arxiv_id,
        version=version,
        local_pdf_path=local_pdf_path,
        extraction_status=str(get("extraction_status")),
        abstract_text=str(get("abstract_text") or ""),
        introduction_text=introduction,
        conclusion_text=conclusion,
        failure_reason=get("failure_reason"),
        source_pdf_sha256=source_pdf_sha256,
        extractor_version=extractor_version,
        reused=True,
    )


def extract_pdf_sections_for_existing_downloads(
    connection: sqlite3.Connection,
    project_root: Path,
    logger: logging.Logger,
    *,
    text_extractor: Callable[[Path], str] | None = None,
    extractor_version: str = PDF_SECTION_EXTRACTOR_VERSION,
    force: bool = False,
    target_papers: set[tuple[str, int]] | None = None,
) -> list[PdfSectionExtractionResult]:
    """离线增量提取 PDF 章节；来源和质量有效时直接复用。"""
    validate_pdf_sections_schema(connection)
    extractor = text_extractor or extract_text_from_pdf_file
    params: list[object] = list(SUCCESSFUL_PDF_DOWNLOAD_STATUSES)
    target_clause = ""
    if target_papers is not None:
        if not target_papers:
            return []
        ordered_targets = sorted(target_papers)
        placeholders = ",".join("(?, ?)" for _ in ordered_targets)
        target_clause = f"AND (arxiv_id, version) IN ({placeholders})"
        for arxiv_id, version in ordered_targets:
            params.extend((arxiv_id, version))
    rows = connection.execute(
        f"""
        SELECT arxiv_id, version, local_pdf_path, download_status
        FROM pdf_downloads
        WHERE download_status IN (?, ?)
          {target_clause}
        ORDER BY arxiv_id, version
        """,
        params,
    ).fetchall()

    results: list[PdfSectionExtractionResult] = []
    for row in rows:
        arxiv_id = row["arxiv_id"] if isinstance(row, sqlite3.Row) else row[0]
        version = int(row["version"] if isinstance(row, sqlite3.Row) else row[1])
        local_pdf_path = (
            row["local_pdf_path"] if isinstance(row, sqlite3.Row) else row[2]
        )
        local_pdf_path = str(local_pdf_path or "")
        source_pdf_sha256 = ""
        try:
            pdf_path = resolve_stored_pdf_path(project_root, local_pdf_path)
            if not pdf_path.exists():
                raise PdfTextExtractionError("pdf_file_missing")
            if not is_valid_pdf_file(pdf_path):
                raise PdfTextExtractionError("invalid_pdf_file")
            source_pdf_sha256 = pdf_file_sha256(pdf_path)
            if not force:
                reusable = load_reusable_pdf_section_result(
                    connection,
                    arxiv_id=str(arxiv_id),
                    version=version,
                    local_pdf_path=local_pdf_path,
                    source_pdf_sha256=source_pdf_sha256,
                    extractor_version=extractor_version,
                )
                if reusable is not None:
                    results.append(reusable)
                    logger.info(
                        "PDF 正文提取复用：arxiv_id=%s version=v%d",
                        reusable.arxiv_id,
                        reusable.version,
                    )
                    continue
            text = extractor(pdf_path)
            result = build_pdf_section_result_from_text(
                str(arxiv_id),
                version,
                local_pdf_path,
                text,
                source_pdf_sha256=source_pdf_sha256,
                extractor_version=extractor_version,
            )
        except PdfTextExtractionError as exc:
            result = PdfSectionExtractionResult(
                arxiv_id=str(arxiv_id),
                version=version,
                local_pdf_path=local_pdf_path,
                extraction_status="failed",
                failure_reason=exc.error_type,
                source_pdf_sha256=source_pdf_sha256,
                extractor_version=extractor_version,
            )
        except OSError:
            result = PdfSectionExtractionResult(
                arxiv_id=str(arxiv_id),
                version=version,
                local_pdf_path=local_pdf_path,
                extraction_status="failed",
                failure_reason="pdf_file_read_failed",
                source_pdf_sha256=source_pdf_sha256,
                extractor_version=extractor_version,
            )
        except Exception:
            result = PdfSectionExtractionResult(
                arxiv_id=str(arxiv_id),
                version=version,
                local_pdf_path=local_pdf_path,
                extraction_status="failed",
                failure_reason="pdf_text_extraction_failed",
                source_pdf_sha256=source_pdf_sha256,
                extractor_version=extractor_version,
            )

        save_pdf_section_result(connection, result)
        results.append(result)
        if result.extraction_status == "extracted":
            logger.info(
                "PDF 正文提取结果：arxiv_id=%s version=v%d status=extracted",
                result.arxiv_id,
                result.version,
            )
        elif result.extraction_status == "partial":
            logger.warning(
                "PDF 正文提取结果：arxiv_id=%s version=v%d "
                "status=partial failure_reason=%s",
                result.arxiv_id,
                result.version,
                result.failure_reason or "unknown",
            )
        else:
            logger.warning(
                "PDF 正文提取结果：arxiv_id=%s version=v%d "
                "status=failed failure_reason=%s",
                result.arxiv_id,
                result.version,
                result.failure_reason or "unknown",
            )

    return results


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


def unique_timestamped_path(directory: Path, stem: str, suffix: str) -> Path:
    """生成不覆盖旧文件的时间戳路径。"""
    directory.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().astimezone().strftime("%Y-%m-%d_run-%H%M%S")
    candidate = directory / f"{stem}_{timestamp}{suffix}"
    if not candidate.exists():
        return candidate
    for number in range(1, 100):
        candidate = directory / f"{stem}_{timestamp}_{number:02d}{suffix}"
        if not candidate.exists():
            return candidate
    raise RuntimeError("无法生成唯一阶段文件名。")


def setup_logging(log_path: Path) -> logging.Logger:
    """创建独立 PDF 测试日志，不复用 main.py 日志。"""
    logger = logging.getLogger("arxiv_kaleid.pdf_download")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    logger.handlers.clear()
    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)s | %(message)s", "%Y-%m-%d %H:%M:%S"
    )
    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(formatter)
    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    logger.addHandler(stream_handler)
    return logger


def build_stage_report(
    run_time: datetime,
    results: list[PdfDownloadResult],
) -> str:
    """生成历史入围论文 PDF 下载测试日报。"""
    attempted = sum(result.network_attempted for result in results)
    downloaded = sum(result.status == "downloaded" for result in results)
    reused = sum(result.status == "reused_existing_pdf" for result in results)
    failed = sum(result.status == "failed" for result in results)
    lines = [
        "# V2 历史入围论文 PDF 下载测试",
        "",
        f"日期：{run_time.date().isoformat()}",
        f"运行时间：{run_time.isoformat(timespec='seconds')}",
        "本次是历史入围论文 PDF 下载测试。",
        "未访问 arXiv API。",
        "未调用 DeepSeek。",
        "未执行第二轮。",
        "未解析 PDF 正文。",
        "PDF 正文提取状态：当前未安装解析库，未进行正文提取。",
        f"历史入围论文数量：{len(results)}",
        f"PDF 下载尝试数量：{attempted}",
        f"PDF 下载成功数量：{downloaded}",
        f"复用已有 PDF 数量：{reused}",
        f"PDF 失败数量：{failed}",
        "",
        "## PDF 下载结果",
        "",
    ]
    for result in results:
        report_link = os.path.relpath(
            PROJECT_ROOT / result.local_relative_path,
            PROJECT_ROOT / "reports" / "daily",
        ).replace("\\", "/")
        lines.extend(
            [
                f"### {result.paper.title}",
                "",
                f"arXiv ID：{result.paper.arxiv_id}",
                f"版本：v{result.paper.version}",
                f"在线 PDF：{result.online_pdf_url}",
                f"本地相对路径：{result.local_relative_path}",
                f"本地 PDF：[打开本地 PDF]({report_link})",
                f"下载状态：{result.status}",
                f"文件大小：{result.size_bytes} bytes",
                "PDF 正文提取状态：当前未安装解析库，未进行正文提取。",
                "",
            ]
        )
        if result.error_type:
            lines.extend([f"错误类型：{result.error_type}", ""])
    return "\n".join(lines)


def run_minimal_history_pdf_download(
    project_root: Path = PROJECT_ROOT,
    *,
    opener: Callable[..., Any] = urllib.request.urlopen,
) -> dict[str, Any]:
    """独立运行最多 2 篇历史入围论文的 PDF 下载测试。"""
    config_path = project_root / "config.json"
    config, paths = load_runtime_paths(project_root, config_path)
    log_path = unique_timestamped_path(
        paths["logs_dir"], "pdf_download", ".log"
    )
    logger = setup_logging(log_path)
    run_time = datetime.now().astimezone()
    logger.info("测试类型：历史入围论文 PDF 下载测试")
    logger.info("arXiv API 访问状态：未访问")
    logger.info("DeepSeek 调用状态：未调用")
    logger.info("第二轮状态：未执行")
    logger.info("PDF 正文解析状态：未执行")

    connection = sqlite3.connect(paths["database"], timeout=10)
    connection.row_factory = sqlite3.Row
    try:
        papers = load_historical_selected_papers(
            connection, limit=DEFAULT_HISTORY_PDF_TEST_LIMIT
        )
        logger.info("历史第一轮入围论文数量：%d", len(papers))
        if not papers:
            raise RuntimeError("数据库中没有历史第一轮入围论文。")

        run_date = run_time.date().isoformat()
        timeout_seconds = float(
            config.get("request_timeout_seconds", PDF_REQUEST_TIMEOUT_SECONDS)
        )
        results = download_selected_papers(
            project_root,
            connection,
            paths["pdfs_dir"],
            papers,
            run_date,
            logger,
            timeout_seconds=timeout_seconds,
            opener=opener,
        )

        report_path = unique_timestamped_path(
            paths["reports_dir"], "pdf_download_test", ".md"
        )
        report_text = build_stage_report(run_time, results)
        with report_path.open("x", encoding="utf-8", newline="\n") as file:
            file.write(report_text)
            file.write("\n")
        logger.info(
            "阶段日报路径：%s", relative_project_path(project_root, report_path)
        )
        logger.info(
            "专用日志路径：%s", relative_project_path(project_root, log_path)
        )

        return {
            "results": results,
            "attempted_count": sum(r.network_attempted for r in results),
            "downloaded_count": sum(r.status == "downloaded" for r in results),
            "reused_count": sum(
                r.status == "reused_existing_pdf" for r in results
            ),
            "failed_count": sum(r.status == "failed" for r in results),
            "report_path": relative_project_path(project_root, report_path),
            "log_path": relative_project_path(project_root, log_path),
        }
    finally:
        connection.close()


def main() -> int:
    """命令行入口：只执行最小历史入围 PDF 下载测试。"""
    try:
        summary = run_minimal_history_pdf_download()
    except Exception as exc:
        print(f"PDF_TEST_STATUS=failed")
        print(f"ERROR_TYPE={type(exc).__name__}")
        return 1

    print("PDF_TEST_STATUS=" + ("success" if summary["failed_count"] == 0 else "partial"))
    print(f"PDF_DOWNLOAD_ATTEMPTS={summary['attempted_count']}")
    print(f"PDF_DOWNLOAD_SUCCESSES={summary['downloaded_count']}")
    print(f"PDF_REUSED={summary['reused_count']}")
    print(f"PDF_FAILED={summary['failed_count']}")
    print(f"REPORT_PATH={summary['report_path']}")
    print(f"LOG_PATH={summary['log_path']}")
    return 0 if summary["failed_count"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
