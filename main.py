from __future__ import annotations

import argparse
import copy
import difflib
import hashlib
import json
import logging
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
import unicodedata
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, TextIO

import arxiv_transport_evidence
import content_labels
import model_usage
import round2_fulltext_state
import selection_nature
from deepseek_client import DeepSeekClient, load_local_api_key
from pdf_processing import (
    PdfSectionExtractionResult,
    MAX_FIRST_ROUND_PDF_DOWNLOADS,
    PdfDownloadResult,
    SelectedPaper,
    build_online_pdf_url,
    build_pdf_destination,
    download_selected_papers,
    extract_pdf_sections_for_existing_downloads,
    save_pdf_download_result,
)


PROJECT_ROOT = Path(__file__).resolve().parent
CONFIG_PATH = PROJECT_ROOT / "config.json"
ARXIV_API_URL = "https://export.arxiv.org/api/query"
ROUND2_TASK_TYPE = "round2_batch_ranking"
ROUND2_ABSTRACT_SOURCE = "papers.summary"
ROUND2_PDF_SECTION_FIELDS = ("introduction_text", "conclusion_text")
ROUND2_SELECTION_POLICY = "full_text_budget_exclusion_v3"
ROUND2_OUTPUT_TRANSPORT = "responses_named_tool_auto_v2"
CURRENT_ROUND1_PROMPT_VERSION = "round1_v20"
CURRENT_ROUND2_PROMPT_VERSION = "round2_v15"
ROUND1_PRECISION_PROMPT_VERSIONS = {"round1_v19", CURRENT_ROUND1_PROMPT_VERSION}
ROUND2_PRECISION_PROMPT_VERSIONS = {"round2_v14", CURRENT_ROUND2_PROMPT_VERSION}
SELF_CONTAINED_ROUND1_PROMPT_VERSIONS = {CURRENT_ROUND1_PROMPT_VERSION}
SELF_CONTAINED_ROUND2_PROMPT_VERSIONS = {CURRENT_ROUND2_PROMPT_VERSION}
ROUND2_FINAL_RECOMMENDATION_LEVELS = ("deep_read", "skim_read", "backup")
ROUND2_RECOMMENDATION_BUDGETS = {
    "deep_read": 1,
    "skim_read": 3,
    "backup": 1,
}


def round2_selection_policy_for_prompt(prompt_version: str) -> str:
    """按历史提示词还原当时的策略身份，避免用当前版本改写旧产物。"""
    match = re.fullmatch(r"round2_v(\d+)", str(prompt_version or ""))
    number = int(match.group(1)) if match else None
    if number == 1:
        return "recommend_up_to_5_with_quality_threshold"
    if number is not None and 2 <= number <= 8:
        return "top_k_final_reading_budget"
    if number is not None and 9 <= number <= 12:
        return "full_text_budget_exclusion_v1"
    if number == 13:
        return "full_text_budget_exclusion_v2"
    return ROUND2_SELECTION_POLICY


STALE_RUNNING_RUN_AFTER = timedelta(hours=24)

ATOM_NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "arxiv": "http://arxiv.org/schemas/atom",
}

DEFAULT_CONFIG: dict[str, Any] = {
    "project_name": "arXivKaleid",
    # 用户可修改：此处控制 arXiv 抓取分类。
    "arxiv_categories": ["gr-qc", "astro-ph.HE", "astro-ph.GA"],
    # 用户可修改：从 UTC 当天向前查找最近非空 submittedDate 的最大天数。
    "submitted_date_lookback_days": 14,
    "max_results": 100,
    "top_n": 5,
    # 用户可修改：以下字段控制第一轮候选池上限、策略版本和后续最终推荐预算。
    "round1_max_selected_n": 10,
    "round1_selection_policy": "top_k_daily_budget",
    "round1_selection_policy_version": "top_k_daily_budget_v4",
    "final_max_recommendations": 5,
    "request_interval_seconds": 3,
    "request_timeout_seconds": 30,
    "deepseek": {
        # 用户可修改：实际模型名称和完整 API 接口 URL 以官方文档为准。
        "round1_model": "deepseek-flash",
        "round2_model": "deepseek-flash",
        "base_url": "https://api.deepseek.com/chat/completions",
        # 用户可修改：如需更换 API key，请手动修改 config/local_secret.json。
        # 注意：不要把真实 API key 写入代码、日志、日报、数据库或聊天记录。
        "api_key_file": "config/local_secret.json",
        "api_key_json_field": "deepseek_api_key",
        "timeout_seconds": 60,
        "max_retries": 0,
        "enable_mock_when_missing_key": False,
        "round1_max_output_tokens": 128000,
        "round2_max_output_tokens": 128000,
        "round1_thinking_mode": "enabled",
        "round1_reasoning_effort": "high",
        "round2_thinking_mode": "enabled",
        "round2_reasoning_effort": "high",
        "round2_output_transport": ROUND2_OUTPUT_TRANSPORT,
    },
    "versions": {
        # profile_v2 仅作为数据库和历史协议的兼容身份。
        "research_profile_version": "profile_v2",
        "round1_prompt_version": CURRENT_ROUND1_PROMPT_VERSION,
        "round2_prompt_version": CURRENT_ROUND2_PROMPT_VERSION,
    },
    "limits": {
        "max_round1_request_tokens": 800000,
        "max_intro_chars_per_paper": 6000,
        "max_conclusion_chars_per_paper": 5000,
        "max_round2_pdf_pages": 60,
        "max_round2_request_tokens": 800000,
        "max_feedback_relevant": 3,
        "max_feedback_irrelevant": 3,
        "max_feedback_read_later": 2,
    },
    "keywords": [
        "black hole shadow",
        "black-hole shadow",
        "photon ring",
        "photon sphere",
        "critical curve",
        "polarization",
        "polarized image",
        "polarimetric image",
        "EVPA",
        "Stokes",
        "accretion disk",
        "accretion disc",
        "accretion flow",
        "ray tracing",
        "radiative transfer",
        "polarized radiative transfer",
        "Kerr",
        "modified gravity",
        "compact object",
        "Event Horizon Telescope",
        "EHT",
    ],
    "negative_keywords": [
        "cosmological perturbation",
        "dark energy",
        "inflation",
        "pure mathematics",
        "particle collider",
        "LIGO data analysis",
    ],
    "core_keywords": [
        "shadow",
        "polarization",
        "polarized",
        "accretion disk",
        "accretion disc",
        "ray tracing",
        "radiative transfer",
        "photon ring",
        "EVPA",
        "Stokes",
    ],
    "paths": {
        "database": "data/arxiv_kaleid.sqlite",
        "reports_dir": "reports/daily",
        "logs_dir": "logs",
        "cache_dir": "cache/arxiv",
        "pdfs_dir": "data/pdfs",
        "round1_prompt": "prompts/relevance_round1_v20.txt",
        "round2_prompt": "prompts/relevance_round2_v15.txt",
    },
}

DEFAULT_PROFILE = """# 研究画像

当前研究方向是不同吸积盘照射下的黑洞及致密天体阴影图像和偏振图像数值模拟。

重点关注黑洞阴影、光子环、临界曲线、吸积盘辐射、测地线追迹、辐射转移、偏振输运、EVPA 和 Stokes 参数。

优先关注 gr-qc 和 astro-ph.HE 中与黑洞、致密天体、吸积盘、阴影、偏振成像和 EHT 相关的论文。

暂时降低优先级的主题包括纯宇宙学、纯粒子物理、与黑洞图像无关的数值相对论、纯引力波数据分析和纯数学形式的量子引力模型。

用于英文元数据匹配的重点概念包括 black hole shadow、photon ring、critical curve、polarization、accretion disk、ray tracing、radiative transfer、compact object 和 Event Horizon Telescope。
"""


def current_time_iso() -> str:
    """返回包含本地时区的 ISO 时间。"""
    return datetime.now().astimezone().isoformat(timespec="seconds")


def create_default_config(config_path: Path) -> bool:
    """配置不存在时在项目根目录创建默认模板，已有文件绝不覆盖。"""
    if config_path.exists():
        return False
    with config_path.open("w", encoding="utf-8", newline="\n") as file:
        json.dump(DEFAULT_CONFIG, file, ensure_ascii=False, indent=2)
        file.write("\n")
    return True


def load_config(config_path: Path) -> dict[str, Any]:
    """读取并校验 JSON 配置。"""
    try:
        with config_path.open("r", encoding="utf-8-sig") as file:
            config = json.load(file)
    except FileNotFoundError as exc:
        raise RuntimeError("配置文件不存在：config.json") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"config.json 格式错误：第 {exc.lineno} 行，第 {exc.colno} 列，{exc.msg}"
        ) from exc

    if not isinstance(config, dict):
        raise RuntimeError("config.json 顶层必须是 JSON 对象。")

    categories = config.get("arxiv_categories")
    if (
        not isinstance(categories, list)
        or not categories
        or not all(isinstance(item, str) and item.strip() for item in categories)
    ):
        raise RuntimeError("config.json 中的 arxiv_categories 必须是非空字符串列表。")

    # V1 关键词配置继续保留用于历史回溯，但 V2 主流程不再读取它们进行评分。
    for field in ("keywords", "negative_keywords", "core_keywords"):
        value = config.get(field)
        if value is not None and (
            not isinstance(value, list)
            or not all(isinstance(item, str) and item.strip() for item in value)
        ):
            raise RuntimeError(f"config.json 中的 {field} 必须是字符串列表。")

    max_results = config.get("max_results")
    submitted_date_lookback_days = config.get("submitted_date_lookback_days")
    round1_max_selected = config.get("round1_max_selected_n")
    round1_selection_policy = config.get("round1_selection_policy")
    round1_selection_policy_version = config.get(
        "round1_selection_policy_version"
    )
    final_max_recommendations = config.get("final_max_recommendations")
    interval = config.get("request_interval_seconds")
    timeout = config.get("request_timeout_seconds", 30)
    if not isinstance(max_results, int) or not 1 <= max_results <= 2000:
        raise RuntimeError("max_results 必须是 1 至 2000 之间的整数。")
    if (
        not isinstance(submitted_date_lookback_days, int)
        or isinstance(submitted_date_lookback_days, bool)
        or not 0 <= submitted_date_lookback_days <= 366
    ):
        raise RuntimeError(
            "submitted_date_lookback_days 必须是 0 至 366 之间的整数。"
        )
    if (
        not isinstance(round1_max_selected, int)
        or not 1 <= round1_max_selected <= MAX_FIRST_ROUND_PDF_DOWNLOADS
    ):
        raise RuntimeError(
            "round1_max_selected_n 必须是 1 至 "
            f"{MAX_FIRST_ROUND_PDF_DOWNLOADS} 之间的整数。"
        )
    if (
        not isinstance(round1_selection_policy, str)
        or not round1_selection_policy.strip()
    ):
        raise RuntimeError("round1_selection_policy 必须是非空字符串。")
    if (
        not isinstance(round1_selection_policy_version, str)
        or not round1_selection_policy_version.strip()
    ):
        raise RuntimeError("round1_selection_policy_version 必须是非空字符串。")
    if (
        not isinstance(final_max_recommendations, int)
        or final_max_recommendations < 1
    ):
        raise RuntimeError("final_max_recommendations 必须是大于 0 的整数。")
    if not isinstance(interval, (int, float)) or interval < 3:
        raise RuntimeError("request_interval_seconds 不得小于 3 秒。")
    if not isinstance(timeout, (int, float)) or timeout <= 0:
        raise RuntimeError("request_timeout_seconds 必须大于 0。")

    deepseek = config.get("deepseek")
    if isinstance(deepseek, dict):
        # 旧测试夹具和历史本地配置继续沿用当前已授权的第二轮上限。
        deepseek.setdefault("round2_max_output_tokens", 128000)
    required_deepseek_fields = {
        "round1_model",
        "round2_model",
        "base_url",
        "api_key_file",
        "api_key_json_field",
        "timeout_seconds",
        "max_retries",
        "enable_mock_when_missing_key",
        "round1_max_output_tokens",
        "round2_max_output_tokens",
        "round1_thinking_mode",
        "round1_reasoning_effort",
        "round2_thinking_mode",
        "round2_reasoning_effort",
        "round2_output_transport",
    }
    available_deepseek_fields = set(deepseek) if isinstance(deepseek, dict) else set()
    if not required_deepseek_fields.issubset(available_deepseek_fields):
        missing = sorted(required_deepseek_fields - available_deepseek_fields)
        raise RuntimeError(f"config.json 缺少 DeepSeek 配置：{', '.join(missing)}")
    for field in (
        "round1_model",
        "round2_model",
        "base_url",
        "api_key_file",
        "api_key_json_field",
    ):
        if not isinstance(deepseek[field], str) or not deepseek[field].strip():
            raise RuntimeError(f"deepseek.{field} 必须是非空字符串。")
    if (
        not isinstance(deepseek["timeout_seconds"], (int, float))
        or deepseek["timeout_seconds"] <= 0
    ):
        raise RuntimeError("deepseek.timeout_seconds 必须大于 0。")
    if (
        not isinstance(deepseek["max_retries"], int)
        or isinstance(deepseek["max_retries"], bool)
        or deepseek["max_retries"] != 0
    ):
        raise RuntimeError(
            "deepseek.max_retries 必须为 0；付费模型调用禁止自动重试。"
        )
    for stage in ("round1", "round2"):
        thinking_mode = deepseek[f"{stage}_thinking_mode"]
        reasoning_effort = deepseek[f"{stage}_reasoning_effort"]
        if thinking_mode not in {"enabled", "disabled"}:
            raise RuntimeError(
                f"deepseek.{stage}_thinking_mode 只能是 enabled 或 disabled。"
            )
        if thinking_mode == "enabled" and reasoning_effort not in {"high", "max"}:
            raise RuntimeError(
                f"deepseek.{stage}_reasoning_effort 在思考模式下只能是 high 或 max。"
            )
        if thinking_mode == "disabled" and reasoning_effort is not None:
            raise RuntimeError(
                f"deepseek.{stage}_reasoning_effort 在非思考模式下必须为 null。"
            )
    if deepseek["round2_output_transport"] != ROUND2_OUTPUT_TRANSPORT:
        raise RuntimeError(
            "deepseek.round2_output_transport 必须为 "
            f"{ROUND2_OUTPUT_TRANSPORT}。"
        )
    if not isinstance(deepseek["enable_mock_when_missing_key"], bool):
        raise RuntimeError("deepseek.enable_mock_when_missing_key 必须是布尔值。")
    for field in ("round1_max_output_tokens", "round2_max_output_tokens"):
        if (
            not isinstance(deepseek[field], int)
            or isinstance(deepseek[field], bool)
            or deepseek[field] <= 0
        ):
            raise RuntimeError(f"deepseek.{field} 必须是大于 0 的整数。")

    versions = config.get("versions")
    required_version_fields = {
        "research_profile_version",
        "round1_prompt_version",
        "round2_prompt_version",
    }
    available_version_fields = set(versions) if isinstance(versions, dict) else set()
    if not required_version_fields.issubset(available_version_fields):
        missing = sorted(required_version_fields - available_version_fields)
        raise RuntimeError(f"config.json 缺少版本配置：{', '.join(missing)}")
    for field in required_version_fields:
        if not isinstance(versions[field], str) or not versions[field].strip():
            raise RuntimeError(f"versions.{field} 必须是非空字符串。")

    limits = config.get("limits")
    if isinstance(limits, dict):
        # 旧本地配置可在不改文件的情况下使用新安全默认值；正式配置仍显式记录。
        limits.setdefault("max_round2_pdf_pages", 60)
        limits.setdefault("max_round1_request_tokens", 800000)
        limits.setdefault("max_round2_request_tokens", 800000)
    required_limit_fields = {
        "max_intro_chars_per_paper",
        "max_conclusion_chars_per_paper",
        "max_round2_pdf_pages",
        "max_round1_request_tokens",
        "max_round2_request_tokens",
        "max_feedback_relevant",
        "max_feedback_irrelevant",
        "max_feedback_read_later",
    }
    available_limit_fields = set(limits) if isinstance(limits, dict) else set()
    if not required_limit_fields.issubset(available_limit_fields):
        missing = sorted(required_limit_fields - available_limit_fields)
        raise RuntimeError(f"config.json 缺少限制配置：{', '.join(missing)}")
    for field in required_limit_fields:
        if not isinstance(limits[field], int) or limits[field] < 0:
            raise RuntimeError(f"limits.{field} 必须是非负整数。")
    for field in (
        "max_round1_request_tokens",
        "max_round2_pdf_pages",
        "max_round2_request_tokens",
    ):
        if limits[field] <= 0:
            raise RuntimeError(f"limits.{field} 必须大于 0。")

    required_path_fields = {
        "database",
        "reports_dir",
        "logs_dir",
        "cache_dir",
        "pdfs_dir",
        "round1_prompt",
        "round2_prompt",
    }
    paths = config.get("paths")
    available_path_fields = set(paths) if isinstance(paths, dict) else set()
    if not required_path_fields.issubset(available_path_fields):
        missing = sorted(required_path_fields - available_path_fields)
        raise RuntimeError(f"config.json 缺少路径配置：{', '.join(missing)}")
    return config


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
        "api_key_file": deepseek["api_key_file"],
        "api_key_json_field": deepseek["api_key_json_field"],
        "timeout_seconds": deepseek["timeout_seconds"],
        "max_retries": deepseek["max_retries"],
        "enable_mock_when_missing_key": deepseek["enable_mock_when_missing_key"],
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
            "research_profile_markdown",
            "research_profile_json",
            "database",
            "reports_dir",
            "logs_dir",
            "cache_dir",
            "pdfs_dir",
            "round1_prompt",
            "round2_prompt",
        }
    }


def ensure_directories(project_root: Path, paths: dict[str, Path]) -> None:
    """创建当前阶段需要的目录，PDF 目录留到下载阶段按需创建。"""
    directories = {
        paths["round1_prompt"].parent,
        paths["database"].parent,
        paths["reports_dir"],
        paths["logs_dir"],
        paths["cache_dir"],
    }
    for directory in directories:
        directory.mkdir(parents=True, exist_ok=True)


def create_default_profile(profile_path: Path) -> bool:
    """研究画像不存在时创建模板，已有用户内容绝不覆盖。"""
    if profile_path.exists():
        return False
    profile_path.write_text(DEFAULT_PROFILE, encoding="utf-8", newline="\n")
    return True


def load_research_profile(profile_path: Path) -> str:
    """读取研究画像并拒绝空文件。"""
    try:
        profile = profile_path.read_text(encoding="utf-8-sig").strip()
    except FileNotFoundError as exc:
        raise RuntimeError("长版研究画像文件不存在。") from exc
    if not profile:
        raise RuntimeError("长版研究画像文件为空。")
    return profile


def load_research_profile_json(profile_path: Path) -> dict[str, Any]:
    """读取实际用于模型调用的短版研究画像。"""
    try:
        with profile_path.open("r", encoding="utf-8-sig") as file:
            profile = json.load(file)
    except FileNotFoundError as exc:
        raise RuntimeError("短版研究画像 JSON 不存在。") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"短版研究画像 JSON 格式错误：第 {exc.lineno} 行，第 {exc.colno} 列"
        ) from exc

    if not isinstance(profile, dict):
        raise RuntimeError("短版研究画像 JSON 顶层必须是对象。")
    profile_version = profile.get("profile_version")
    if not isinstance(profile_version, str) or not profile_version.strip():
        raise RuntimeError("短版研究画像缺少 profile_version。")
    return profile


def validate_research_profile_version(
    config: dict[str, Any], profile: dict[str, Any]
) -> bool:
    """配置版本与画像版本必须完全一致，避免缓存和提示词语义混用。"""
    return (
        config["versions"]["research_profile_version"]
        == profile["profile_version"]
    )


def research_profile_identity(config: dict[str, Any]) -> dict[str, str]:
    """返回仅用于历史协议兼容的画像身份，不包含研究内容。"""
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


def setup_logging(logs_dir: Path) -> tuple[logging.Logger, Path]:
    """同时记录 UTF-8 日志文件并输出到终端。"""
    log_path = logs_dir / f"run_{datetime.now().astimezone().date().isoformat()}.log"
    logger = logging.getLogger("arxiv_kaleid")
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
    return logger, log_path


def initialize_database_schema(connection: sqlite3.Connection) -> None:
    """以只增不删方式初始化 V1 表和 V2 扩展表。"""
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
        CREATE TABLE IF NOT EXISTS scores (
            arxiv_id TEXT NOT NULL,
            version INTEGER NOT NULL,
            score REAL NOT NULL,
            matched_keywords TEXT,
            reason TEXT,
            scored_at TEXT NOT NULL,
            PRIMARY KEY (arxiv_id, version)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS feedback (
            arxiv_id TEXT NOT NULL,
            version INTEGER NOT NULL,
            feedback TEXT,
            note TEXT,
            created_at TEXT NOT NULL
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
        """
        CREATE TABLE IF NOT EXISTS pdf_sections (
            arxiv_id TEXT NOT NULL,
            version INTEGER NOT NULL,
            abstract_text TEXT,
            introduction_text TEXT,
            conclusion_text TEXT,
            extraction_status TEXT NOT NULL,
            extracted_at TEXT,
            failure_reason TEXT,
            PRIMARY KEY (arxiv_id, version)
        )
        """,
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
        """,
        """
        CREATE TABLE IF NOT EXISTS daily_reports (
            report_id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL,
            report_path TEXT NOT NULL UNIQUE,
            candidate_count INTEGER NOT NULL,
            inserted_count INTEGER NOT NULL,
            duplicate_count INTEGER NOT NULL,
            round1_selected_count INTEGER NOT NULL,
            final_recommendation_count INTEGER NOT NULL,
            run_status TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """,
    ]

    try:
        # 关键逻辑：整个迁移在一个事务中执行，失败时不删除旧表或旧数据。
        with connection:
            for statement in statements:
                connection.execute(statement)

            screening_info = connection.execute(
                "PRAGMA table_info(screening_results)"
            ).fetchall()
            score_info = next(
                (row for row in screening_info if row["name"] == "score"), None
            )
            if score_info is not None and int(score_info["notnull"]) == 1:
                # v19/v14 不再要求模型分数；迁移为可空列，绝不写入伪造占位值。
                connection.execute(
                    "DROP TABLE IF EXISTS screening_results_migration_old"
                )
                connection.execute(
                    "ALTER TABLE screening_results "
                    "RENAME TO screening_results_migration_old"
                )
                connection.execute(
                    """
                    CREATE TABLE screening_results (
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
                    """
                )
                connection.execute(
                    """
                    INSERT INTO screening_results (
                        result_id, run_id, task_type, arxiv_id, version,
                        result_rank, recommendation_level, score, confidence,
                        is_selected, reason, details_json, model_name,
                        prompt_version, research_profile_version, created_at
                    )
                    SELECT result_id, run_id, task_type, arxiv_id, version,
                           result_rank, recommendation_level, score, confidence,
                           is_selected, reason, details_json, model_name,
                           prompt_version, research_profile_version, created_at
                    FROM screening_results_migration_old
                    """
                )
                connection.execute("DROP TABLE screening_results_migration_old")

            feedback_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(feedback)").fetchall()
            }
            feedback_additions = {
                "report_date": "TEXT",
                "report_path": "TEXT",
                "source_updated_at": "TEXT",
            }
            for column_name, column_type in feedback_additions.items():
                if column_name not in feedback_columns:
                    connection.execute(
                        f"ALTER TABLE feedback ADD COLUMN {column_name} {column_type}"
                    )

            batch_cache_columns = {
                row["name"]
                for row in connection.execute(
                    "PRAGMA table_info(batch_cache)"
                ).fetchall()
            }
            if "request_summary" not in batch_cache_columns:
                connection.execute(
                    """
                    ALTER TABLE batch_cache
                    ADD COLUMN request_summary TEXT NOT NULL DEFAULT '{}'
                    """
                )
            if "selection_policy_version" not in batch_cache_columns:
                connection.execute(
                    """
                    ALTER TABLE batch_cache
                    ADD COLUMN selection_policy_version TEXT NOT NULL DEFAULT ''
                    """
                )

            completion_info = connection.execute(
                "PRAGMA table_info(screening_completion)"
            ).fetchall()
            completion_columns = {row["name"] for row in completion_info}
            completion_pk = [
                row["name"]
                for row in sorted(completion_info, key=lambda row: row["pk"])
                if row["pk"]
            ]
            expected_completion_pk = [
                "task_type",
                "arxiv_id",
                "version",
                "prompt_version",
                "research_profile_version",
                "selection_policy_version",
            ]
            if completion_pk != expected_completion_pk:
                policy_expr = (
                    "COALESCE(selection_policy_version, 'legacy_unversioned')"
                    if "selection_policy_version" in completion_columns
                    else "'legacy_unversioned'"
                )
                connection.execute(
                    "DROP TABLE IF EXISTS screening_completion_migration_old"
                )
                connection.execute(
                    """
                    ALTER TABLE screening_completion
                    RENAME TO screening_completion_migration_old
                    """
                )
                connection.execute(
                    """
                    CREATE TABLE screening_completion (
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
                    """
                )
                connection.execute(
                    f"""
                    INSERT OR IGNORE INTO screening_completion (
                        task_type, arxiv_id, version, run_id,
                        completion_status, selection_status, model_name,
                        prompt_version, research_profile_version,
                        selection_policy_version, completed_at
                    )
                    SELECT task_type, arxiv_id, version, run_id,
                           completion_status, selection_status, model_name,
                           prompt_version, research_profile_version,
                           {policy_expr}, completed_at
                    FROM screening_completion_migration_old
                    """
                )
                connection.execute("DROP TABLE screening_completion_migration_old")

            connection.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS ux_feedback_report_entry
                ON feedback (arxiv_id, version, report_path)
                WHERE report_path IS NOT NULL
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS ix_screening_results_paper
                ON screening_results (arxiv_id, version, task_type)
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS ix_screening_completion_task
                ON screening_completion (task_type, completion_status)
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS ix_screening_completion_policy
                ON screening_completion (
                    task_type, completion_status, prompt_version,
                    research_profile_version, selection_policy_version
                )
                """
            )
            # 关键逻辑：旧库只能可靠回填明确属于 round1_v1 的历史入围论文；
            # screening_results 没有策略版本列时，不得把 round1_v2 伪造成 legacy。
            connection.execute(
                """
                INSERT OR IGNORE INTO screening_completion (
                    task_type, arxiv_id, version, run_id, completion_status,
                    selection_status, model_name, prompt_version,
                    research_profile_version, selection_policy_version,
                    completed_at
                )
                SELECT task_type, arxiv_id, version, run_id, 'completed',
                       CASE WHEN is_selected = 1 THEN 'selected'
                            ELSE 'not_selected' END,
                       model_name, prompt_version, research_profile_version,
                       'legacy_unversioned', created_at
                FROM screening_results
                WHERE task_type = 'round1_abstract_screening'
                  AND prompt_version = 'round1_v1'
                """
            )
            model_usage.initialize_model_usage_schema(
                connection, manage_transaction=False
            )
    except sqlite3.Error as exc:
        raise RuntimeError(f"SQLite 数据库迁移失败：{exc}") from exc


def init_database(database_path: Path) -> sqlite3.Connection:
    """打开本地 SQLite，并执行兼容 V1 数据的增量迁移。"""
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


def mark_stale_running_runs_interrupted(
    connection: sqlite3.Connection,
    *,
    current_started_at: str,
) -> int:
    """只收口超过 24 小时的 running 记录，避免误伤并发中的正常运行。"""
    try:
        current_time = datetime.fromisoformat(current_started_at)
    except ValueError as exc:
        raise RuntimeError("当前运行时间格式无效") from exc
    stale_run_ids: list[int] = []
    rows = connection.execute(
        """
        SELECT run_id, started_at
        FROM runs
        WHERE status = 'running' AND finished_at IS NULL
        ORDER BY run_id
        """
    ).fetchall()
    for row in rows:
        try:
            prior_time = datetime.fromisoformat(str(row["started_at"]))
            elapsed = current_time - prior_time
        except (TypeError, ValueError):
            continue
        if elapsed >= STALE_RUNNING_RUN_AFTER:
            stale_run_ids.append(int(row["run_id"]))
    for run_id in stale_run_ids:
        connection.execute(
            """
            UPDATE runs
            SET finished_at = ?, status = 'interrupted',
                message = '此前进程异常中断；由后续运行自动收口'
            WHERE run_id = ? AND status = 'running' AND finished_at IS NULL
            """,
            (current_started_at, run_id),
        )
    return len(stale_run_ids)


def start_run(connection: sqlite3.Connection, started_at: str) -> int:
    """插入 running 状态，并保留此前最后一次成功时间。"""
    mark_stale_running_runs_interrupted(
        connection, current_started_at=started_at
    )
    previous = connection.execute(
        """
        SELECT last_success_time
        FROM runs
        WHERE status = 'success' AND last_success_time IS NOT NULL
        ORDER BY run_id DESC
        LIMIT 1
        """
    ).fetchone()
    previous_success = previous["last_success_time"] if previous else None
    cursor = connection.execute(
        """
        INSERT INTO runs (started_at, status, message, last_success_time)
        VALUES (?, 'running', ?, ?)
        """,
        (started_at, "程序正在运行", previous_success),
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
    """为单个分类和 UTC submittedDate 日历日构造查询 URL。"""
    date_prefix = submission_date.strftime("%Y%m%d")
    parameters = {
        "search_query": (
            f"cat:{category.strip()} AND "
            f"submittedDate:[{date_prefix}0000 TO {date_prefix}2359]"
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


def parse_utc_submission_date(value: Any) -> date | None:
    """把 Atom published 时间解析为 UTC 日期；无效值返回 None。"""
    if not isinstance(value, str) or not value.strip():
        return None
    normalized = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).date()


def parse_target_submission_date_argument(value: str) -> date:
    """严格解析 CLI 的 UTC submittedDate，并拒绝未来日期。"""
    normalized = value.strip()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", normalized):
        raise argparse.ArgumentTypeError("--date 必须使用 YYYY-MM-DD 格式。")
    try:
        parsed = date.fromisoformat(normalized)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("--date 不是有效日历日期。") from exc
    if parsed > datetime.now(timezone.utc).date():
        raise argparse.ArgumentTypeError("--date 不得晚于当前 UTC 日期。")
    return parsed


def fetch_category_submission_pages(
    *,
    category: str,
    submission_date: date,
    page_size: int,
    fetch_page: Callable[[str, date, int, int], list[dict[str, Any]]],
    logger: logging.Logger,
) -> list[dict[str, Any]]:
    """分页读取单分类目标 submittedDate；分页失败时抛出异常，不返回部分结果。"""
    if page_size < 1:
        raise RuntimeError("arXiv 分页大小必须大于 0。")

    papers: list[dict[str, Any]] = []
    start = 0
    while True:
        page_papers = fetch_page(category, submission_date, start, page_size)
        if not page_papers:
            logger.info(
                "日期 %s 分类 %s 分页 start=%d 返回空页，停止分页。",
                submission_date.isoformat(),
                category,
                start,
            )
            break

        papers.extend(page_papers)
        exact_date_count = sum(
            parse_utc_submission_date(paper.get("published"))
            == submission_date
            for paper in page_papers
        )
        off_date_count = len(page_papers) - exact_date_count
        logger.info(
            "日期 %s 分类 %s 分页 start=%d 获取并解析论文数量：%d，"
            "目标日期数量：%d，非目标日期数量：%d",
            submission_date.isoformat(),
            category,
            start,
            len(page_papers),
            exact_date_count,
            off_date_count,
        )

        if off_date_count:
            logger.warning(
                "日期 %s 分类 %s 分页 start=%d 出现非目标 submittedDate "
                "论文 %d 篇，停止继续分页。",
                submission_date.isoformat(),
                category,
                start,
                off_date_count,
            )
            break
        if len(page_papers) < page_size:
            logger.info(
                "日期 %s 分类 %s 分页 start=%d 返回 %d 篇，小于分页大小 %d，"
                "停止分页。",
                submission_date.isoformat(),
                category,
                start,
                len(page_papers),
                page_size,
            )
            break

        start += page_size

    return papers


def find_recent_nonempty_submission_batch(
    *,
    start_date_utc: date,
    lookback_days: int,
    categories: list[str],
    fetch_category: Callable[[str, date], list[dict[str, Any]]],
) -> tuple[date | None, list[dict[str, Any]], int, int, int]:
    """从 UTC 当天向前找最近非空提交日，只返回该日跨分类合并结果。"""
    checked_date_count = 0
    discarded_off_date_count = 0
    for offset in range(lookback_days + 1):
        candidate_date = start_date_utc - timedelta(days=offset)
        checked_date_count += 1
        paper_groups: list[list[dict[str, Any]]] = []
        for category in categories:
            category_papers = fetch_category(category, candidate_date)
            exact_date_papers: list[dict[str, Any]] = []
            for paper in category_papers:
                if parse_utc_submission_date(paper.get("published")) == candidate_date:
                    exact_date_papers.append(paper)
                else:
                    discarded_off_date_count += 1
            paper_groups.append(exact_date_papers)

        merged, duplicate_count = merge_papers_by_identity(paper_groups)
        if merged:
            return (
                candidate_date,
                merged,
                duplicate_count,
                checked_date_count,
                discarded_off_date_count,
            )

    return None, [], 0, checked_date_count, discarded_off_date_count


def load_recent_papers(
    connection: sqlite3.Connection, limit: int
) -> list[dict[str, Any]]:
    """arXiv 全部失败时读取最近历史元数据，仅用于明确标记的降级简报。"""
    rows = connection.execute(
        """
        SELECT id, arxiv_id, version, title, authors, summary, categories,
               primary_category, published, updated, abs_url, pdf_url
        FROM papers
        ORDER BY updated DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [dict(row) for row in rows]


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


def load_completed_round1_keys(
    connection: sqlite3.Connection,
    config: dict[str, Any],
) -> set[tuple[str, int]]:
    """读取当前提示词、画像和策略版本下已完成第一轮的精确论文键。"""
    try:
        rows = connection.execute(
            """
            SELECT arxiv_id, version
            FROM screening_completion
            WHERE task_type = 'round1_abstract_screening'
              AND completion_status = 'completed'
              AND prompt_version = ?
              AND research_profile_version = ?
              AND selection_policy_version = ?
            """,
            (
                config["versions"]["round1_prompt_version"],
                config["versions"]["research_profile_version"],
                config["round1_selection_policy_version"],
            ),
        ).fetchall()
    except sqlite3.Error as exc:
        raise RuntimeError(f"SQLite 读取第一轮完成状态失败：{exc}") from exc
    return {(str(row["arxiv_id"]), int(row["version"])) for row in rows}


def filter_completed_round1_candidates(
    papers: list[dict[str, Any]],
    completed_keys: set[tuple[str, int]],
) -> tuple[list[dict[str, Any]], int]:
    """逐篇排除历史已完成的相同 ID+version；新版本仍保留为新候选。"""
    candidates = [
        paper
        for paper in papers
        if (paper["arxiv_id"], int(paper["version"])) not in completed_keys
    ]
    return candidates, len(papers) - len(candidates)


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
    """构造第二轮单篇公开输入；同一批只使用全文或章节降级之一。"""
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
    # 仅供历史报告/测试载荷解析；当前 v10 执行入口必须提供全文状态库。
    input_mode = str(paper.get("round2_input_mode") or "section_fallback")
    if input_mode == "full_text":
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
    elif input_mode == "section_fallback":
        payload["pdf_sections"] = {
            "extraction_status": paper.get(
                "pdf_extraction_status", paper.get("extraction_status", "")
            ),
            "failure_reason": paper.get(
                "pdf_failure_reason", paper.get("failure_reason")
            ),
            "introduction_text": paper.get(
                "pdf_introduction_text", paper.get("introduction_text", "")
            ),
            "conclusion_text": paper.get(
                "pdf_conclusion_text", paper.get("conclusion_text", "")
            ),
        }
    else:
        raise RuntimeError(f"unsupported_round2_input_mode:{input_mode}")
    return payload


def build_round2_messages(
    prompt: str,
    research_profile: dict[str, Any],
    papers: list[dict[str, Any]],
    config: dict[str, Any],
) -> list[dict[str, str]]:
    """构造第二轮同批排序输入；不包含本地路径和被页数门控的长文。"""
    modes = {
        str(paper.get("round2_input_mode") or "section_fallback")
        for paper in papers
    }
    if len(modes) > 1:
        raise RuntimeError("mixed_round2_input_modes")
    input_mode = next(iter(modes), "section_fallback")
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
        "pdf_input_fields": (
            ["page_number", "text"]
            if input_mode == "full_text"
            else list(ROUND2_PDF_SECTION_FIELDS)
        ),
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
    # 保留旧版章节输入字段，便于已有离线工具继续识别降级模式。
    if input_mode == "section_fallback":
        task_input["pdf_section_fields"] = list(ROUND2_PDF_SECTION_FIELDS)
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
            pdf_sections = candidate.get("pdf_sections")
            if not isinstance(pdf_sections, dict):
                pdf_sections = {}
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
                    "pdf_section_fields": sorted(pdf_sections.keys()),
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
                        "pdf_sections.introduction_text": len(
                            str(pdf_sections.get("introduction_text") or "")
                        ),
                        "pdf_sections.conclusion_text": len(
                            str(pdf_sections.get("conclusion_text") or "")
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


def safe_model_string_list(
    value: Any, *, max_items: int = 8, max_chars: int = 500
) -> list[str]:
    """限制模型文本字段的数量和长度，避免异常输出污染日报。"""
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for item in value:
        if isinstance(item, str) and item.strip():
            result.append(item.strip()[:max_chars])
        if len(result) >= max_items:
            break
    return result


def strict_round2_text(value: Any, *, max_chars: int) -> str | None:
    """v11 短文本字段必须是非空字符串，禁止类型强转或静默截断。"""
    if not isinstance(value, str):
        return None
    normalized = " ".join(value.split())
    if not normalized or len(normalized) > max_chars:
        return None
    return normalized


def strict_round2_string_list(
    value: Any, *, min_items: int, max_items: int = 3, max_chars: int = 300
) -> list[str] | None:
    """v11 文本数组严格执行类型、数量、长度和去重边界。"""
    if not isinstance(value, list) or not min_items <= len(value) <= max_items:
        return None
    result: list[str] = []
    for item in value:
        normalized = strict_round2_text(item, max_chars=max_chars)
        if normalized is None:
            return None
        result.append(normalized)
    if len({item.casefold() for item in result}) != len(result):
        return None
    return result


ROUND2_STRUCTURED_EVIDENCE_MIN_PROMPT_VERSION = 5
ROUND2_EVIDENCE_SOURCES = (
    "pdf_full_text",
    "metadata_abstract",
    "pdf_introduction",
    "pdf_conclusion",
)


def round2_prompt_requires_structured_evidence(prompt_version: str) -> bool:
    """仅历史 round2_v5/v6 要求结构化证据。"""
    match = re.fullmatch(r"round2_v(\d+)", str(prompt_version or "").strip())
    return bool(match and int(match.group(1)) in {5, 6})


def round2_prompt_allows_evidence_repair(prompt_version: str) -> bool:
    """仅历史 round2_v6 允许保守的同页连续原文重建。"""
    match = re.fullmatch(r"round2_v(\d+)", str(prompt_version or "").strip())
    return bool(match and int(match.group(1)) == 6)


def round2_prompt_uses_ranking_only(prompt_version: str) -> bool:
    """round2_v7 起只要求排序与简短判断，不接收模型生成的证据字段。"""
    match = re.fullmatch(r"round2_v(\d+)", str(prompt_version or "").strip())
    return bool(match and int(match.group(1)) >= 7)


def normalize_round2_evidence_text(value: object) -> str:
    """消除 PDF 常见断词、标点和空白差异，用于保守的原文匹配。"""
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    # 拼接字母数字片段，使 black-hole、black hole 和 PDF 跨行断词可等价核验。
    return "".join(re.findall(r"\w+", text, flags=re.UNICODE))


ROUND2_EVIDENCE_REPAIR_CODE = "evidence_reanchored_from_claimed_pdf_page"
ROUND2_EVIDENCE_UNGROUNDED_ERROR = "evidence 原文不在标注来源或页码中"


def build_round2_deterministic_evidence_quote(
    source_text: str,
    model_quote: str,
    *,
    max_quote_chars: int = 500,
) -> tuple[str | None, str | None]:
    """只在同一页内把高度近似的模型引文锚定到连续 PDF 原文。"""
    source_with_lines = unicodedata.normalize("NFKC", str(source_text or ""))
    cleaned_model_quote = " ".join(str(model_quote or "").split())
    normalized_model = normalize_round2_evidence_text(cleaned_model_quote)
    model_tokens = re.findall(
        r"\w+", unicodedata.normalize("NFKC", cleaned_model_quote).casefold()
    )
    if len(normalized_model) < 20 or len(model_tokens) < 5:
        return None, "模型引文不足以进行保守的页内重建"

    # 只比较单个连续句段；禁止跨页、跨不相邻片段或自由拼接。
    raw_segments = [
        segment.strip()
        for segment in re.split(r"(?<=[.!?。！？])\s+|[\r\n]+", source_with_lines)
        if segment.strip()
    ]
    candidates: list[tuple[float, float, str]] = []
    model_token_set = set(model_tokens)
    model_numbers = set(re.findall(r"\d+(?:\.\d+)?", cleaned_model_quote))
    for start in range(len(raw_segments)):
        combined = ""
        for end in range(start, min(start + 3, len(raw_segments))):
            combined = f"{combined} {raw_segments[end]}".strip()
            if len(combined) > max_quote_chars:
                break
            normalized_candidate = normalize_round2_evidence_text(combined)
            if len(normalized_candidate) < 20:
                continue
            candidate_tokens = set(
                re.findall(
                    r"\w+",
                    unicodedata.normalize("NFKC", combined).casefold(),
                )
            )
            common_tokens = model_token_set & candidate_tokens
            token_coverage = len(common_tokens) / max(len(model_token_set), 1)
            if len(common_tokens) < 5 or token_coverage < 0.75:
                continue
            candidate_numbers = set(re.findall(r"\d+(?:\.\d+)?", combined))
            if model_numbers and not model_numbers.issubset(candidate_numbers):
                continue
            similarity = difflib.SequenceMatcher(
                None, normalized_model, normalized_candidate
            ).ratio()
            if similarity >= 0.88:
                candidates.append((similarity, token_coverage, combined))

    if not candidates:
        return None, "声明页不存在可唯一确定的高度近似连续原文"
    candidates.sort(key=lambda item: (item[0], item[1], -len(item[2])), reverse=True)
    best = candidates[0]
    if len(candidates) > 1 and (
        best[0] - candidates[1][0] < 0.03
        and best[1] - candidates[1][1] < 0.10
        and best[2] != candidates[1][2]
    ):
        return None, "声明页存在多个近似片段，无法唯一确定原文"
    return best[2], None


def validate_or_repair_round2_structured_evidence(
    value: object,
    paper: dict[str, Any],
    *,
    allow_repair: bool,
    max_items: int = 3,
    max_quote_chars: int = 500,
) -> tuple[list[dict[str, Any]], str | None, list[int]]:
    """先严格核验；仅对同一 PDF 页内高度近似引文做确定性重建。"""
    evidence, error = validate_round2_structured_evidence(
        value,
        paper,
        max_items=max_items,
        max_quote_chars=max_quote_chars,
    )
    if error is None or not allow_repair or error != ROUND2_EVIDENCE_UNGROUNDED_ERROR:
        return evidence, error, []
    if not isinstance(value, list):
        return [], error, []

    pages = paper.get("pdf_pages")
    page_map = {
        int(page.get("page_number") or 0): str(
            page.get("page_text") or page.get("text") or ""
        )
        for page in (pages if isinstance(pages, list) else [])
        if isinstance(page, dict)
        and not isinstance(page.get("page_number"), bool)
        and isinstance(page.get("page_number"), int)
        and int(page["page_number"]) > 0
    }
    repaired_value = copy.deepcopy(value)
    repair_indices: list[int] = []
    for evidence_index, item in enumerate(repaired_value, start=1):
        if not isinstance(item, dict):
            return [], error, []
        page_number = item.get("page_number")
        quote = item.get("quote")
        if (
            item.get("source") != "pdf_full_text"
            or isinstance(page_number, bool)
            or not isinstance(page_number, int)
            or page_number not in page_map
            or not isinstance(quote, str)
        ):
            return [], error, []
        normalized_quote = normalize_round2_evidence_text(quote)
        if normalized_quote in normalize_round2_evidence_text(page_map[page_number]):
            continue
        repaired_quote, repair_error = build_round2_deterministic_evidence_quote(
            page_map[page_number],
            quote,
            max_quote_chars=max_quote_chars,
        )
        if repair_error is not None or repaired_quote is None:
            return [], error, []
        item["quote"] = repaired_quote
        repair_indices.append(evidence_index)

    repaired, repaired_error = validate_round2_structured_evidence(
        repaired_value,
        paper,
        max_items=max_items,
        max_quote_chars=max_quote_chars,
    )
    if repaired_error is not None:
        return [], error, []
    return repaired, None, repair_indices


def validate_round2_structured_evidence(
    value: object,
    paper: dict[str, Any],
    *,
    max_items: int = 3,
    max_quote_chars: int = 500,
) -> tuple[list[dict[str, Any]], str | None]:
    """核对证据来源、物理页码及原文片段，避免保存不可追溯证据。"""
    if not isinstance(value, list) or not 1 <= len(value) <= max_items:
        return [], "evidence 必须是包含 1 至 3 项的数组"

    input_mode = str(paper.get("round2_input_mode") or "section_fallback")
    pages = paper.get("pdf_pages")
    page_map = {
        int(page.get("page_number") or 0): str(
            page.get("page_text") or page.get("text") or ""
        )
        for page in (pages if isinstance(pages, list) else [])
        if isinstance(page, dict)
        and not isinstance(page.get("page_number"), bool)
        and isinstance(page.get("page_number"), int)
        and int(page["page_number"]) > 0
    }
    section_sources = {
        "metadata_abstract": str(paper.get("summary") or ""),
        "pdf_introduction": str(paper.get("pdf_introduction_text") or ""),
        "pdf_conclusion": str(paper.get("pdf_conclusion_text") or ""),
    }

    validated: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            return [], "evidence 每项必须是对象"
        if set(item) != {"source", "page_number", "quote"}:
            return [], "evidence 每项字段必须严格为 source、page_number、quote"

        source = item.get("source")
        page_number = item.get("page_number")
        quote = item.get("quote")
        if source not in ROUND2_EVIDENCE_SOURCES:
            return [], "evidence source 非法"
        if not isinstance(quote, str) or not quote.strip():
            return [], "evidence quote 为空或类型非法"
        cleaned_quote = " ".join(quote.split())
        if len(cleaned_quote) > max_quote_chars:
            return [], "evidence quote 超过长度限制"
        normalized_quote = normalize_round2_evidence_text(cleaned_quote)
        if len(normalized_quote.replace(" ", "")) < 20:
            return [], "evidence quote 过短，无法可靠核验"

        if input_mode == "full_text":
            if source != "pdf_full_text":
                return [], "全文模式 evidence 必须来自 pdf_full_text"
            if (
                isinstance(page_number, bool)
                or not isinstance(page_number, int)
                or page_number not in page_map
            ):
                return [], "evidence PDF 物理页码不存在"
            source_text = page_map[page_number]
        elif input_mode == "section_fallback":
            if source not in section_sources:
                return [], "章节降级模式 evidence source 非法"
            if page_number is not None:
                return [], "章节降级模式 evidence page_number 必须为 null"
            source_text = section_sources[source]
        else:
            return [], "evidence 对应的第二轮输入模式非法"

        if normalized_quote not in normalize_round2_evidence_text(source_text):
            return [], ROUND2_EVIDENCE_UNGROUNDED_ERROR
        validated.append(
            {
                "source": str(source),
                "page_number": page_number,
                "quote": cleaned_quote,
            }
        )
    return validated, None


ROUND1_REQUIRED_TOP_LEVEL_KEYS = (
    "task_type",
    "profile_version",
    "research_profile_version",
    "prompt_version",
    "selection_policy",
    "selection_policy_version",
    "actual_selected_count",
    "candidate_rankings",
    "selected_papers",
)
ROUND1_REQUIRED_RANKING_ITEM_KEYS = (
    "candidate_index",
    "overall_rank",
    "score",
    "confidence",
    "relevance_band",
    "reason_codes",
)
ROUND1_REQUIRED_SELECTED_ITEM_KEYS = (
    "candidate_index",
    "round1_rank",
    "score",
    "confidence",
    "relevance_band",
    "recommendation_hint",
    "matched_reasons",
    "negative_reasons",
    "evidence",
)
ROUND1_V18_REQUIRED_RANKING_ITEM_KEYS = (
    "candidate_index",
    "overall_rank",
    "score",
    "confidence",
    "content_label",
)
ROUND1_V18_REQUIRED_SELECTED_ITEM_KEYS = (
    "candidate_index",
    "round1_rank",
    "score",
    "confidence",
    "content_label",
    "recommendation_hint",
    "matched_reasons",
    "negative_reasons",
    "evidence",
)
ROUND1_RELEVANCE_BANDS = {"direct", "adjacent", "backup", "off_profile"}
ROUND1_REASON_CODES = {
    "strong_gravity_imaging",
    "polarization_imaging",
    "imaging_ready_spacetime_solution",
    "accretion_radiation",
    "reusable_accretion_model",
    "radiation_hydrodynamics",
    "ray_tracing_transfer",
    "eht_vlbi",
    "compact_object_spacetime",
    "adjacent_black_hole_astrophysics",
    "gravity_or_wave_only",
    "formal_gravity_only",
    "unrelated_astrophysics",
    "insufficient_metadata",
}
ROUND1_PROFILE_DEPRIORITY_CODES = {
    "formal_gravity_only",
    "gravity_or_wave_only",
}
ROUND1_PROFILE_PRIORITY_CONTEXT_CODES = {
    "strong_gravity_imaging",
    "polarization_imaging",
    "imaging_ready_spacetime_solution",
    "accretion_radiation",
    "reusable_accretion_model",
    "ray_tracing_transfer",
    "eht_vlbi",
}
ROUND1_PROFILE_EXCLUSIVE_POSITIVE_CODES = (
    ROUND1_PROFILE_PRIORITY_CONTEXT_CODES | {"radiation_hydrodynamics"}
)
ROUND1_RAY_TRACING_ANCHOR_CODES = {
    "strong_gravity_imaging",
    "polarization_imaging",
    "accretion_radiation",
    "reusable_accretion_model",
    "radiation_hydrodynamics",
    "eht_vlbi",
}
ROUND1_RADIATION_HYDRODYNAMICS_ANCHOR_CODES = {
    "strong_gravity_imaging",
    "polarization_imaging",
    "accretion_radiation",
    "reusable_accretion_model",
    "eht_vlbi",
}
ROUND1_DIRECT_PRIORITY_CODES = {
    "strong_gravity_imaging",
    "polarization_imaging",
    "imaging_ready_spacetime_solution",
}
ROUND1_V16_CORE_PRIORITY_CODES = ROUND1_DIRECT_PRIORITY_CODES | {"eht_vlbi"}
ROUND1_IMAGING_METHOD_CODES = {
    "accretion_radiation",
    "reusable_accretion_model",
    "radiation_hydrodynamics",
    "ray_tracing_transfer",
}
ROUND1_FULL_SEMANTIC_GUARD_PROMPT_VERSIONS = {
    "round1_v14",
    "round1_v15",
    "round1_v16",
    "round1_v17",
}
ROUND1_STRICT_LABEL_PROMPT_VERSIONS = {"round1_v16", "round1_v17"}
ROUND1_EVIDENCE_SOURCES = {"metadata_title", "metadata_abstract"}
ROUND1_EVIDENCE_UNGROUNDED_ERROR = "evidence quote 不在标注的标题或摘要中"
ROUND1_EVIDENCE_TOO_SHORT_ERROR = "evidence quote 过短"
ROUND1_EVIDENCE_TOO_LONG_ERROR = "evidence quote 过长"
ROUND1_EVIDENCE_REPAIR_CODE = "evidence_reanchored_from_metadata"
ROUND1_EVIDENCE_REPAIR_REASON_CODES = {
    ROUND1_EVIDENCE_UNGROUNDED_ERROR: "evidence_quote_not_in_source",
    ROUND1_EVIDENCE_TOO_SHORT_ERROR: "evidence_quote_too_short",
    ROUND1_EVIDENCE_TOO_LONG_ERROR: "evidence_quote_too_long",
}
ROUND1_EVIDENCE_REPAIR_DETAIL_KEYS = {
    "index",
    "evidence_item_index",
    "reason_code",
    "source",
    "normalized_quote_length",
}
ROUND1_CACHE_EVIDENCE_REPAIR_KEY = "_arxivkaleid_evidence_repair_indices"
ROUND1_CACHE_EVIDENCE_REPAIR_DETAILS_KEY = (
    "_arxivkaleid_evidence_repair_details"
)
ROUND1_RECOMMENDATION_HINT_MAX_CHARS = 300
ROUND1_RECOMMENDATION_HINT_TRUNCATION_CODE = "recommendation_hint_truncated"
ROUND1_CACHE_HINT_TRUNCATION_KEY = "_arxivkaleid_hint_truncation_indices"


def strict_round1_string_list(
    value: object,
    *,
    min_items: int,
    max_items: int = 3,
    max_chars: int = 300,
) -> tuple[list[str], str | None]:
    """严格校验第一轮短文本数组，不静默截断或丢弃非法项。"""
    if not isinstance(value, list) or not min_items <= len(value) <= max_items:
        return [], f"必须是包含 {min_items} 至 {max_items} 项的数组"
    cleaned: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            return [], "包含空项或非字符串项"
        text = " ".join(item.split())
        if len(text) > max_chars:
            return [], "包含超过长度限制的项"
        cleaned.append(text)
    if len({item.casefold() for item in cleaned}) != len(cleaned):
        return [], "包含重复项"
    return cleaned, None


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


def build_round1_deterministic_evidence(
    paper: dict[str, Any],
) -> tuple[list[dict[str, str]], str | None]:
    """从可信元数据生成连续原文证据，不使用模型改写内容。"""
    for source, field in (
        ("metadata_title", "title"),
        ("metadata_abstract", "summary"),
    ):
        source_text = normalize_round1_evidence_text(paper.get(field))
        if len(source_text) < 8:
            continue
        # 标题通常完整保留；超长标题或摘要只截取连续的前 300 字符。
        quote = source_text[:300]
        return [{"source": source, "quote": quote}], None
    return [], "可信标题和摘要不足 8 字符，无法生成确定性证据"


def round1_evidence_repair_details_valid(
    value: object,
    *,
    repair_indices: list[int],
    selected_count: int,
) -> bool:
    """校验只含排名、来源、原因码和长度的安全修复审计明细。"""
    if not isinstance(value, list) or len(value) != len(repair_indices):
        return False
    if [detail.get("index") for detail in value if isinstance(detail, dict)] != (
        repair_indices
    ):
        return False
    return all(
        isinstance(detail, dict)
        and set(detail) == ROUND1_EVIDENCE_REPAIR_DETAIL_KEYS
        and not isinstance(detail.get("index"), bool)
        and isinstance(detail.get("index"), int)
        and 1 <= detail["index"] <= selected_count
        and not isinstance(detail.get("evidence_item_index"), bool)
        and isinstance(detail.get("evidence_item_index"), int)
        and 1 <= detail["evidence_item_index"] <= 3
        and detail.get("reason_code")
        in set(ROUND1_EVIDENCE_REPAIR_REASON_CODES.values())
        and detail.get("source") in ROUND1_EVIDENCE_SOURCES
        and not isinstance(detail.get("normalized_quote_length"), bool)
        and isinstance(detail.get("normalized_quote_length"), int)
        and detail["normalized_quote_length"] >= 0
        for detail in value
    )


def safe_round1_diagnostic_field_names(
    values: set[Any], limit: int = 20
) -> dict[str, Any]:
    """限制模型提供的字段名，避免把任意响应文本带入审计状态。"""
    names = sorted(
        (
            value
            if isinstance(value, str)
            and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,79}", value)
            else "<unsafe_field_name>"
        )
        for value in values
    )
    return {
        "count": len(names),
        "names": names[:limit],
        "truncated": len(names) > limit,
    }


def diagnose_round1_validation_result(
    raw_result: Any,
    papers: list[dict[str, Any]],
    *,
    max_selected: int,
    profile_version: str,
    prompt_version: str,
    selection_policy_version: str,
) -> dict[str, Any]:
    """返回验证器本身生成的安全诊断，避免维护第二套漂移规则。"""
    validated, _warnings = validate_round1_result(
        raw_result,
        papers,
        max_selected=max_selected,
        profile_version=profile_version,
        prompt_version=prompt_version,
        selection_policy_version=selection_policy_version,
    )
    diagnostic = validated.get("validation_diagnostic")
    if isinstance(diagnostic, dict):
        return diagnostic
    return {
        "code": "validation_diagnostic_unavailable",
        "stage": "internal",
    }


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
    if prompt_version in ROUND1_PRECISION_PROMPT_VERSIONS:
        return validate_round1_v19_result(
            raw_result,
            papers,
            max_selected=max_selected,
            profile_version=profile_version,
            prompt_version=prompt_version,
            selection_policy_version=selection_policy_version,
        )
    """强制执行全候选排序、Top K 身份一致性及元数据证据可追溯性。"""
    warnings: list[str] = []
    uses_core_content_label = content_labels.round1_prompt_uses_core_content_label(
        prompt_version
    )
    validated = {
        "task_type": "round1_abstract_screening",
        "batch_valid": False,
        "actual_selected_count": 0,
        "candidate_rankings": [],
        "selected_papers": [],
        "cacheable_response": None,
        "evidence_repair_count": 0,
        "evidence_repair_codes": [],
        "evidence_repair_details": [],
        "recommendation_hint_truncation_count": 0,
        "recommendation_hint_truncation_codes": [],
        "validation_error_code": None,
        "validation_diagnostic": None,
    }

    def reject(
        code: str,
        stage: str,
        message: str,
        **safe_details: Any,
    ) -> tuple[dict[str, Any], list[str]]:
        """只记录结构、数量、索引和字段名，不记录模型正文。"""
        warnings.append(message)
        diagnostic = {"code": code, "stage": stage, **safe_details}
        validated["validation_error_code"] = code
        validated["validation_diagnostic"] = diagnostic
        return validated, warnings

    expected_count = min(max_selected, len(papers))
    if not isinstance(raw_result, dict):
        return reject(
            "response_not_object",
            "response",
            "模型输出不是 JSON 对象，已拒绝该批次。",
            actual_type=type(raw_result).__name__,
        )
    missing_top = [
        key for key in ROUND1_REQUIRED_TOP_LEVEL_KEYS if key not in raw_result
    ]
    if missing_top:
        return reject(
            "missing_top_level_fields",
            "identity",
            "模型输出缺少第一轮必需顶层字段，已拒绝该批次。",
            missing_fields=missing_top,
        )
    if uses_core_content_label:
        extra_top = safe_round1_diagnostic_field_names(
            set(raw_result) - set(ROUND1_REQUIRED_TOP_LEVEL_KEYS)
        )
        if extra_top["count"]:
            return reject(
                "top_level_field_set_invalid",
                "identity",
                "模型输出包含当前第一轮版本不接受的额外顶层字段。",
                extra_fields=extra_top["names"],
                extra_field_count=extra_top["count"],
                extra_fields_truncated=extra_top["truncated"],
            )
    if raw_result.get("task_type") != "round1_abstract_screening":
        return reject(
            "task_type_mismatch",
            "identity",
            "模型输出 task_type 不正确，已拒绝全部入围结果。",
            mismatch_fields=["task_type"],
        )
    mismatch_fields = [
        field
        for field, actual, expected in (
            ("profile_version", raw_result.get("profile_version"), profile_version),
            (
                "research_profile_version",
                raw_result.get("research_profile_version"),
                profile_version,
            ),
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
    if mismatch_fields:
        return reject(
            "identity_field_mismatch",
            "identity",
            "模型输出版本或策略标识不一致，已拒绝全部入围结果。",
            mismatch_fields=mismatch_fields,
        )

    raw_rankings = raw_result.get("candidate_rankings")
    raw_selected = raw_result.get("selected_papers")
    actual_selected_count = raw_result.get("actual_selected_count")
    if not isinstance(raw_rankings, list) or not isinstance(raw_selected, list):
        non_list_fields = [
            field
            for field, value in (
                ("candidate_rankings", raw_rankings),
                ("selected_papers", raw_selected),
            )
            if not isinstance(value, list)
        ]
        return reject(
            "ranking_arrays_invalid",
            "counts",
            "模型输出缺少第一轮排名数组，已拒绝该批次。",
            non_list_fields=non_list_fields,
        )
    if (
        isinstance(actual_selected_count, bool)
        or not isinstance(actual_selected_count, int)
        or actual_selected_count != expected_count
    ):
        return reject(
            "actual_selected_count_mismatch",
            "counts",
            "模型输出 actual_selected_count 与固定 Top K 预算不一致。",
            expected_count=expected_count,
            actual_count=(
                actual_selected_count
                if isinstance(actual_selected_count, int)
                and not isinstance(actual_selected_count, bool)
                else None
            ),
            actual_type=type(actual_selected_count).__name__,
        )
    if expected_count == 0:
        if raw_rankings or raw_selected:
            return reject(
                "unexpected_results_for_empty_batch",
                "counts",
                "候选数量为 0 但模型返回了排名，已拒绝该批次。",
                candidate_rankings_count=len(raw_rankings),
                selected_papers_count=len(raw_selected),
            )
        validated["batch_valid"] = True
        validated["validation_diagnostic"] = {
            "code": None,
            "stage": "accepted",
            "candidate_rankings_count": 0,
            "selected_papers_count": 0,
            "evidence_repair_count": 0,
            "evidence_repair_indices": [],
            "evidence_repair_details": [],
        }
        cacheable_response = copy.deepcopy(raw_result)
        cacheable_response.pop(ROUND1_CACHE_EVIDENCE_REPAIR_KEY, None)
        cacheable_response.pop(
            ROUND1_CACHE_EVIDENCE_REPAIR_DETAILS_KEY, None
        )
        validated["cacheable_response"] = cacheable_response
        return validated, warnings
    if len(raw_rankings) != len(papers):
        return reject(
            "candidate_rankings_count_mismatch",
            "counts",
            "candidate_rankings 未完整覆盖全部候选，已拒绝该批次。",
            expected_count=len(papers),
            actual_count=len(raw_rankings),
        )
    if len(raw_selected) != expected_count:
        return reject(
            "selected_papers_count_mismatch",
            "counts",
            "selected_papers 数量不等于固定 Top K 预算，已拒绝该批次。",
            expected_count=expected_count,
            actual_count=len(raw_selected),
        )

    candidate_map = {
        (paper["arxiv_id"], int(paper["version"])): paper for paper in papers
    }
    if len(candidate_map) != len(papers):
        return reject(
            "input_candidate_identity_duplicate",
            "input",
            "输入候选身份存在重复，已拒绝该批次。",
            expected_count=len(papers),
            unique_count=len(candidate_map),
        )
    candidate_index_map = {
        candidate_index: paper
        for candidate_index, paper in enumerate(papers, start=1)
    }

    # 关键逻辑：模型仅回传短整数索引，真实论文身份由可信输入确定性映射。
    ranking_seen: set[int] = set()
    ranking_records: list[dict[str, Any]] = []
    previous_score: int | None = None
    for expected_rank, item in enumerate(raw_rankings, start=1):
        if not isinstance(item, dict):
            return reject(
                "ranking_item_not_object",
                "ranking",
                "candidate_rankings 包含缺字段或非法结构。",
                index=expected_rank,
                actual_type=type(item).__name__,
            )
        actual_fields = set(item)
        expected_fields = set(
            ROUND1_V18_REQUIRED_RANKING_ITEM_KEYS
            if uses_core_content_label
            else ROUND1_REQUIRED_RANKING_ITEM_KEYS
        )
        if actual_fields != expected_fields:
            extra_fields = safe_round1_diagnostic_field_names(
                actual_fields - expected_fields
            )
            return reject(
                "ranking_field_set_invalid",
                "ranking",
                "candidate_rankings 包含缺字段或非法结构。",
                index=expected_rank,
                missing_fields=sorted(expected_fields - actual_fields),
                extra_fields=extra_fields["names"],
                extra_field_count=extra_fields["count"],
                extra_fields_truncated=extra_fields["truncated"],
            )
        candidate_index = item.get("candidate_index")
        overall_rank = item.get("overall_rank")
        score = item.get("score")
        confidence = item.get("confidence")
        content_label = item.get("content_label")
        relevance_band = item.get("relevance_band")
        if uses_core_content_label:
            reason_codes: list[str] = []
            reason_code_error = None
        else:
            reason_codes, reason_code_error = strict_round1_string_list(
                item.get("reason_codes"), min_items=1, max_items=3, max_chars=80
            )
        if (
            isinstance(candidate_index, bool)
            or not isinstance(candidate_index, int)
            or candidate_index not in candidate_index_map
        ):
            return reject(
                "ranking_candidate_index_invalid",
                "ranking",
                "candidate_rankings 的候选索引、排名、分数或语义标签非法。",
                index=expected_rank,
            )
        if candidate_index in ranking_seen:
            return reject(
                "ranking_candidate_index_duplicate",
                "ranking",
                "candidate_rankings 的候选索引、排名、分数或语义标签非法。",
                index=expected_rank,
            )
        if (
            isinstance(overall_rank, bool)
            or not isinstance(overall_rank, int)
            or overall_rank != expected_rank
        ):
            return reject(
                "ranking_position_invalid",
                "ranking",
                "candidate_rankings 的身份、排名、分数或语义标签非法。",
                index=expected_rank,
                actual_type=type(overall_rank).__name__,
            )
        if isinstance(score, bool) or not isinstance(score, int) or not 0 <= score <= 100:
            return reject(
                "ranking_score_invalid",
                "ranking",
                "candidate_rankings 的身份、排名、分数或语义标签非法。",
                index=expected_rank,
                actual_type=type(score).__name__,
            )
        if (
            isinstance(confidence, bool)
            or not isinstance(confidence, int)
            or not 0 <= confidence <= 100
        ):
            return reject(
                "ranking_confidence_invalid",
                "ranking",
                "candidate_rankings 的身份、排名、分数或语义标签非法。",
                index=expected_rank,
                actual_type=type(confidence).__name__,
            )
        if uses_core_content_label and content_label not in content_labels.CORE_CONTENT_LABEL_SET:
            return reject(
                "ranking_content_label_invalid",
                "ranking",
                "candidate_rankings 的 content_label 不属于四个合法值。",
                index=expected_rank,
            )
        if not uses_core_content_label and relevance_band not in ROUND1_RELEVANCE_BANDS:
            return reject(
                "ranking_relevance_band_invalid",
                "ranking",
                "candidate_rankings 的身份、排名、分数或语义标签非法。",
                index=expected_rank,
            )
        invalid_reason_code_count = sum(
            code not in ROUND1_REASON_CODES for code in reason_codes
        )
        if not uses_core_content_label and (
            reason_code_error is not None or invalid_reason_code_count
        ):
            return reject(
                "ranking_reason_codes_invalid",
                "ranking",
                "candidate_rankings 的身份、排名、分数或语义标签非法。",
                index=expected_rank,
                reason_code_error=reason_code_error,
                invalid_code_count=invalid_reason_code_count,
            )
        paper = candidate_index_map[candidate_index]
        reason_code_set = set(reason_codes)
        if not uses_core_content_label and (
            reason_code_set & ROUND1_PROFILE_DEPRIORITY_CODES
            and reason_code_set & ROUND1_PROFILE_EXCLUSIVE_POSITIVE_CODES
        ):
            return reject(
                "ranking_reason_code_combination_invalid",
                "ranking",
                "排他降级标签与成像、吸积或辐射优先标签同时出现。",
                index=expected_rank,
                candidate={
                    "candidate_index": candidate_index,
                    "arxiv_id": paper["arxiv_id"],
                    "version": int(paper["version"]),
                    "overall_rank": overall_rank,
                    "score": score,
                    "relevance_band": relevance_band,
                    "reason_codes": reason_codes,
                },
            )
        if (
            prompt_version in ROUND1_STRICT_LABEL_PROMPT_VERSIONS
            and "imaging_ready_spacetime_solution" in reason_code_set
            and "compact_object_spacetime" in reason_code_set
        ):
            return reject(
                "ranking_new_solution_boundary_invalid",
                "ranking",
                "可成像新时空解与已有或未证明为新解的时空标签不得同时出现。",
                index=expected_rank,
                candidate={
                    "candidate_index": candidate_index,
                    "arxiv_id": paper["arxiv_id"],
                    "version": int(paper["version"]),
                    "overall_rank": overall_rank,
                    "score": score,
                    "relevance_band": relevance_band,
                    "reason_codes": reason_codes,
                },
            )
        if (
            prompt_version in ROUND1_STRICT_LABEL_PROMPT_VERSIONS
            and reason_code_set & {"unrelated_astrophysics", "insufficient_metadata"}
            and len(reason_code_set) > 1
        ):
            return reject(
                "ranking_unmapped_label_combination_invalid",
                "ranking",
                "无关或元数据不足标签不得与其他内容标签同时出现。",
                index=expected_rank,
                candidate={
                    "candidate_index": candidate_index,
                    "arxiv_id": paper["arxiv_id"],
                    "version": int(paper["version"]),
                    "overall_rank": overall_rank,
                    "score": score,
                    "relevance_band": relevance_band,
                    "reason_codes": reason_codes,
                },
            )
        if (
            prompt_version in ROUND1_FULL_SEMANTIC_GUARD_PROMPT_VERSIONS
            and
            "ray_tracing_transfer" in reason_code_set
            and not reason_code_set & ROUND1_RAY_TRACING_ANCHOR_CODES
        ):
            return reject(
                "ranking_ray_tracing_context_invalid",
                "ranking",
                "光线追迹或辐射转移标签缺少成像、偏振、吸积辐射或可复用方法目标标签。",
                index=expected_rank,
                candidate={
                    "candidate_index": candidate_index,
                    "arxiv_id": paper["arxiv_id"],
                    "version": int(paper["version"]),
                    "overall_rank": overall_rank,
                    "score": score,
                    "relevance_band": relevance_band,
                    "reason_codes": reason_codes,
                },
            )
        if (
            prompt_version in ROUND1_STRICT_LABEL_PROMPT_VERSIONS
            and "radiation_hydrodynamics" in reason_code_set
            and not reason_code_set
            & ROUND1_RADIATION_HYDRODYNAMICS_ANCHOR_CODES
        ):
            return reject(
                "ranking_radiation_hydrodynamics_context_invalid",
                "ranking",
                "辐射流体标签缺少实际成像、近致密天体吸积或可复用模型目标标签。",
                index=expected_rank,
                candidate={
                    "candidate_index": candidate_index,
                    "arxiv_id": paper["arxiv_id"],
                    "version": int(paper["version"]),
                    "overall_rank": overall_rank,
                    "score": score,
                    "relevance_band": relevance_band,
                    "reason_codes": reason_codes,
                },
            )
        if (
            not uses_core_content_label
            and previous_score is not None
            and score > previous_score
        ):
            return reject(
                "ranking_score_order_invalid",
                "ranking",
                "candidate_rankings 未按 score 非递增排列。",
                index=expected_rank,
            )
        previous_score = score

        ranking_seen.add(candidate_index)
        ranking_records.append(
            {
                "candidate_index": candidate_index,
                "arxiv_id": paper["arxiv_id"],
                "version": int(paper["version"]),
                "overall_rank": overall_rank,
                "score": score,
                "confidence": confidence,
                **(
                    {"content_label": content_label}
                    if uses_core_content_label
                    else {
                        "relevance_band": relevance_band,
                        "reason_codes": reason_codes,
                    }
                ),
            }
        )
    if ranking_seen != set(candidate_index_map):
        return reject(
            "ranking_candidate_set_mismatch",
            "ranking",
            "candidate_rankings 缺少候选或混入错误候选。",
            expected_count=len(candidate_map),
            actual_count=len(ranking_seen),
        )
    # 研究画像边界：可复用吸积/辐射模型不得落在纯形式引力或引力波候选之后。
    top_rankings = ranking_records[:expected_count]
    outside_rankings = ranking_records[expected_count:]
    unmapped_top = (
        None
        if uses_core_content_label
        else next(
            (
                item
                for item in top_rankings
                if set(item["reason_codes"])
                & {"unrelated_astrophysics", "insufficient_metadata"}
            ),
            None,
        )
    )
    if prompt_version in ROUND1_STRICT_LABEL_PROMPT_VERSIONS and unmapped_top is not None:
        return reject(
            "content_label_unmapped",
            "ranking",
            "Top 10 候选无法从理由码生成可信内容标签。",
            candidate={
                key: unmapped_top[key]
                for key in (
                    "candidate_index",
                    "arxiv_id",
                    "version",
                    "overall_rank",
                    "score",
                    "relevance_band",
                    "reason_codes",
                )
            },
        )
    if prompt_version == "round1_v17":
        previous_tier_priority: int | None = None
        for item in top_rankings:
            contribution_tier = content_labels.contribution_tier_from_reason_codes(
                item["reason_codes"]
            )
            tier_priority = content_labels.CONTRIBUTION_TIERS.index(
                contribution_tier
            )
            if (
                previous_tier_priority is not None
                and tier_priority < previous_tier_priority
            ):
                return reject(
                    "round1_top10_contribution_tier_order_invalid",
                    "ranking",
                    "Top 10 未按 A、B、C、D 贡献层级排序，已拒绝保存。",
                    index=item["overall_rank"],
                    contribution_tier=contribution_tier,
                )
            previous_tier_priority = tier_priority

    def is_profile_priority(item: dict[str, Any]) -> bool:
        codes = set(item.get("reason_codes", []))
        return "reusable_accretion_model" in codes or (
            "radiation_hydrodynamics" in codes
            and bool(codes & ROUND1_PROFILE_PRIORITY_CONTEXT_CODES)
        )

    priority_outside = next(
        (
            item
            for item in outside_rankings
            if is_profile_priority(item)
        ),
        None,
    )
    deprioritized_inside = next(
        (
            item
            for item in reversed(top_rankings)
            if set(item.get("reason_codes", [])) & ROUND1_PROFILE_DEPRIORITY_CODES
        ),
        None,
    )
    if priority_outside is not None and deprioritized_inside is not None:
        return reject(
            "profile_priority_order_invalid",
            "ranking",
            "可复用吸积或辐射模型被排在纯形式引力或引力波候选之后。",
            priority_outside={
                key: priority_outside[key]
                for key in (
                    "candidate_index",
                    "arxiv_id",
                    "version",
                    "overall_rank",
                    "score",
                    "relevance_band",
                    "reason_codes",
                )
            },
            deprioritized_inside={
                key: deprioritized_inside[key]
                for key in (
                    "candidate_index",
                    "arxiv_id",
                    "version",
                    "overall_rank",
                    "score",
                    "relevance_band",
                    "reason_codes",
                )
            },
        )
    # v16 新边界：任何合格成像相关方法都不得被纯基础相关候选挤出 Top 10。
    def is_imaging_method(item: dict[str, Any]) -> bool:
        codes = set(item.get("reason_codes", []))
        return bool(codes & {"accretion_radiation", "reusable_accretion_model"}) or (
            "radiation_hydrodynamics" in codes
            and bool(codes & ROUND1_RADIATION_HYDRODYNAMICS_ANCHOR_CODES)
        ) or (
            "ray_tracing_transfer" in codes
            and bool(codes & ROUND1_RAY_TRACING_ANCHOR_CODES)
        )

    method_outside = next(
        (
            item
            for item in outside_rankings
            if is_imaging_method(item)
        ),
        None,
    )
    foundation_inside = next(
        (
            item
            for item in reversed(top_rankings)
            if not set(item.get("reason_codes", []))
            & ROUND1_PROFILE_EXCLUSIVE_POSITIVE_CODES
        ),
        None,
    )
    # Top 10边界：直接成像或偏振候选不得落在仅有普通致密天体标签的候选之后。
    direct_priority_outside = next(
        (
            item
            for item in outside_rankings
            if set(item.get("reason_codes", []))
            & (
                ROUND1_V16_CORE_PRIORITY_CODES
                if prompt_version in ROUND1_STRICT_LABEL_PROMPT_VERSIONS
                else ROUND1_DIRECT_PRIORITY_CODES
            )
        ),
        None,
    )
    weak_inside = next(
        (
            item
            for item in reversed(top_rankings)
            if not set(item.get("reason_codes", []))
            & ROUND1_PROFILE_EXCLUSIVE_POSITIVE_CODES
        ),
        None,
    )
    if (
        prompt_version in ROUND1_FULL_SEMANTIC_GUARD_PROMPT_VERSIONS
        and direct_priority_outside is not None
        and weak_inside is not None
    ):
        return reject(
            "profile_direct_priority_order_invalid",
            "ranking",
            "直接强引力成像、偏振或可成像新时空解被排在仅有普通致密天体标签的候选之后。",
            priority_outside={
                key: direct_priority_outside[key]
                for key in (
                    "candidate_index",
                    "arxiv_id",
                    "version",
                    "overall_rank",
                    "score",
                    "relevance_band",
                    "reason_codes",
                )
            },
            weak_inside={
                key: weak_inside[key]
                for key in (
                    "candidate_index",
                    "arxiv_id",
                    "version",
                    "overall_rank",
                    "score",
                    "relevance_band",
                    "reason_codes",
                )
            },
        )
    if (
        prompt_version in ROUND1_STRICT_LABEL_PROMPT_VERSIONS
        and method_outside is not None
        and foundation_inside is not None
    ):
        return reject(
            "profile_method_priority_order_invalid",
            "ranking",
            "合格成像相关方法被排在仅有相关基础研究标签的候选之后。",
            priority_outside={
                key: method_outside[key]
                for key in (
                    "candidate_index",
                    "arxiv_id",
                    "version",
                    "overall_rank",
                    "score",
                    "relevance_band",
                    "reason_codes",
                )
            },
            foundation_inside={
                key: foundation_inside[key]
                for key in (
                    "candidate_index",
                    "arxiv_id",
                    "version",
                    "overall_rank",
                    "score",
                    "relevance_band",
                    "reason_codes",
                )
            },
        )
    if prompt_version == "round1_v17" and top_rankings and outside_rankings:
        weakest_top_priority = max(
            content_labels.CONTRIBUTION_TIERS.index(
                content_labels.contribution_tier_from_reason_codes(
                    item["reason_codes"]
                )
            )
            for item in top_rankings
        )
        stronger_outside = next(
            (
                (
                    item,
                    content_labels.contribution_tier_from_reason_codes(
                        item["reason_codes"]
                    ),
                )
                for item in outside_rankings
                if content_labels.CONTRIBUTION_TIERS.index(
                    content_labels.contribution_tier_from_reason_codes(
                        item["reason_codes"]
                    )
                )
                < weakest_top_priority
            ),
            None,
        )
        if stronger_outside is not None:
            return reject(
                "round1_top10_contribution_tier_boundary_invalid",
                "ranking",
                "候选区存在高于 Top 10 最弱条目的贡献层级，已拒绝保存。",
                outside_rank=stronger_outside[0]["overall_rank"],
                outside_contribution_tier=stronger_outside[1],
            )

    accepted: list[dict[str, Any]] = []
    evidence_repair_indices: list[int] = []
    evidence_repair_details: list[dict[str, Any]] = []
    hint_truncation_indices: list[int] = []
    for expected_rank, item in enumerate(raw_selected, start=1):
        if not isinstance(item, dict):
            return reject(
                "selected_item_not_object",
                "selection",
                "selected_papers 包含缺字段或非法结构。",
                index=expected_rank,
                actual_type=type(item).__name__,
            )
        selected_required_keys = (
            ROUND1_V18_REQUIRED_SELECTED_ITEM_KEYS
            if uses_core_content_label
            else ROUND1_REQUIRED_SELECTED_ITEM_KEYS
        )
        missing_fields = [key for key in selected_required_keys if key not in item]
        if missing_fields:
            return reject(
                "selected_item_missing_fields",
                "selection",
                "selected_papers 包含缺字段或非法结构。",
                index=expected_rank,
                missing_fields=missing_fields,
            )
        if uses_core_content_label and set(item) != set(selected_required_keys):
            extra_fields = safe_round1_diagnostic_field_names(
                set(item) - set(selected_required_keys)
            )
            return reject(
                "selected_item_field_set_invalid",
                "selection",
                "selected_papers 包含当前版本不接受的字段。",
                index=expected_rank,
                extra_fields=extra_fields["names"],
                extra_field_count=extra_fields["count"],
            )
        candidate_index = item.get("candidate_index")
        score = item.get("score")
        confidence = item.get("confidence")
        raw_rank = item.get("round1_rank")
        content_label = item.get("content_label")
        relevance_band = item.get("relevance_band")
        recommendation_hint = item.get("recommendation_hint")
        matched_reasons, matched_error = strict_round1_string_list(
            item.get("matched_reasons"), min_items=1
        )
        negative_reasons, negative_error = strict_round1_string_list(
            item.get("negative_reasons"), min_items=0
        )
        if (
            isinstance(candidate_index, bool)
            or not isinstance(candidate_index, int)
            or candidate_index not in candidate_index_map
        ):
            return reject(
                "selected_candidate_index_invalid",
                "selection",
                "selected_papers 的候选索引、格式、理由或语义标签非法。",
                index=expected_rank,
            )
        if (
            isinstance(raw_rank, bool)
            or not isinstance(raw_rank, int)
            or raw_rank != expected_rank
        ):
            return reject(
                "selected_rank_invalid",
                "selection",
                "selected_papers 的身份、格式、理由或语义标签非法。",
                index=expected_rank,
                actual_type=type(raw_rank).__name__,
            )
        if isinstance(score, bool) or not isinstance(score, int) or not 0 <= score <= 100:
            return reject(
                "selected_score_invalid",
                "selection",
                "selected_papers 的身份、格式、理由或语义标签非法。",
                index=expected_rank,
                actual_type=type(score).__name__,
            )
        if (
            isinstance(confidence, bool)
            or not isinstance(confidence, int)
            or not 0 <= confidence <= 100
        ):
            return reject(
                "selected_confidence_invalid",
                "selection",
                "selected_papers 的身份、格式、理由或语义标签非法。",
                index=expected_rank,
                actual_type=type(confidence).__name__,
            )
        if uses_core_content_label and content_label not in content_labels.CORE_CONTENT_LABEL_SET:
            return reject(
                "selected_content_label_invalid",
                "selection",
                "selected_papers 的 content_label 不属于四个合法值。",
                index=expected_rank,
            )
        if not uses_core_content_label and relevance_band not in ROUND1_RELEVANCE_BANDS:
            return reject(
                "selected_relevance_band_invalid",
                "selection",
                "selected_papers 的身份、格式、理由或语义标签非法。",
                index=expected_rank,
            )
        if not isinstance(recommendation_hint, str) or not recommendation_hint.strip():
            return reject(
                "recommendation_hint_invalid",
                "selection",
                "selected_papers 的身份、格式、理由或语义标签非法。",
                index=expected_rank,
                actual_type=type(recommendation_hint).__name__,
                normalized_length=None,
            )
        normalized_hint = recommendation_hint.strip()
        hint_original_length = len(normalized_hint)
        hint_truncated = hint_original_length > ROUND1_RECOMMENDATION_HINT_MAX_CHARS
        if hint_truncated:
            if uses_core_content_label:
                return reject(
                    "recommendation_hint_invalid",
                    "selection",
                    "selected_papers 的 recommendation_hint 超过长度限制。",
                    index=expected_rank,
                    normalized_length=hint_original_length,
                    max_length=ROUND1_RECOMMENDATION_HINT_MAX_CHARS,
                )
            normalized_hint = normalized_hint[
                :ROUND1_RECOMMENDATION_HINT_MAX_CHARS
            ].rstrip()
            hint_truncation_indices.append(expected_rank)
            warnings.append(
                f"模型第一轮第 {expected_rank} 项 recommendation_hint 超过"
                f" {ROUND1_RECOMMENDATION_HINT_MAX_CHARS} 字符，已确定性截断。"
            )
        if matched_error is not None:
            return reject(
                "matched_reasons_invalid",
                "selection",
                "selected_papers 的身份、格式、理由或语义标签非法。",
                index=expected_rank,
                reason=matched_error,
            )
        if negative_error is not None:
            return reject(
                "negative_reasons_invalid",
                "selection",
                "selected_papers 的身份、格式、理由或语义标签非法。",
                index=expected_rank,
                reason=negative_error,
            )

        selected_paper = candidate_index_map[candidate_index]
        key = (selected_paper["arxiv_id"], int(selected_paper["version"]))
        ranking = ranking_records[expected_rank - 1]
        top_k_mismatch_fields = []
        if key != (ranking["arxiv_id"], ranking["version"]):
            top_k_mismatch_fields.append("identity")
        if score != ranking["score"]:
            top_k_mismatch_fields.append("score")
        if confidence != ranking["confidence"]:
            top_k_mismatch_fields.append("confidence")
        if uses_core_content_label:
            if content_label != ranking["content_label"]:
                top_k_mismatch_fields.append("content_label")
        elif relevance_band != ranking["relevance_band"]:
            top_k_mismatch_fields.append("relevance_band")
        if top_k_mismatch_fields:
            return reject(
                "selected_top_k_mismatch",
                "selection",
                "selected_papers 与全候选排名的前 Top K 不一致。",
                index=expected_rank,
                mismatch_fields=top_k_mismatch_fields,
            )
        if (
            not uses_core_content_label
            and (relevance_band in {"backup", "off_profile"} or score < 50)
            and not negative_reasons
        ):
            return reject(
                "negative_reasons_required",
                "selection",
                "低相关或低分 Top K 候选缺少 negative_reasons。",
                index=expected_rank,
            )
        generic_reasons = {"相关", "值得阅读", "top 候选", "top candidate", "candidate"}
        if any(reason.casefold() in generic_reasons for reason in matched_reasons):
            return reject(
                "matched_reasons_generic",
                "selection",
                "selected_papers 包含空泛 matched_reasons。",
                index=expected_rank,
            )
        evidence, evidence_error, evidence_error_detail = (
            validate_round1_evidence(
                item.get("evidence"), candidate_map[key]
            )
        )
        evidence_repair_code: str | None = None
        evidence_repair_detail: dict[str, Any] | None = None
        if (
            not uses_core_content_label
            and evidence_error in ROUND1_EVIDENCE_REPAIR_REASON_CODES
        ):
            evidence, fallback_error = build_round1_deterministic_evidence(
                candidate_map[key]
            )
            if fallback_error is not None:
                return reject(
                    "evidence_repair_failed",
                    "evidence",
                    "selected_papers 证据无法从可信元数据确定性修复。",
                    index=expected_rank,
                    reason=fallback_error,
                    **(evidence_error_detail or {}),
                )
            evidence_repair_code = ROUND1_EVIDENCE_REPAIR_CODE
            evidence_repair_indices.append(expected_rank)
            evidence_repair_detail = {
                "index": expected_rank,
                **(evidence_error_detail or {}),
            }
            evidence_repair_details.append(evidence_repair_detail)
            warnings.append(
                f"模型第一轮第 {expected_rank} 项证据校验问题"
                f"（{evidence_error}），"
                "已从可信元数据确定性重建。"
            )
            evidence_error = None
        if evidence_error is not None:
            return reject(
                "evidence_invalid",
                "evidence",
                f"selected_papers 证据校验失败：{evidence_error}。",
                index=expected_rank,
                reason=evidence_error,
                **(evidence_error_detail or {}),
            )

        accepted.append(
            {
                **candidate_map[key],
                "round1_rank": expected_rank,
                "is_relevant": (
                    content_label != "其他"
                    if uses_core_content_label
                    else relevance_band in {"direct", "adjacent"}
                ),
                "score": score,
                "confidence": confidence,
                **(
                    {"content_label": content_label}
                    if uses_core_content_label
                    else {
                        "relevance_band": relevance_band,
                        "reason_codes": ranking["reason_codes"],
                    }
                ),
                "recommendation_hint": normalized_hint,
                "recommendation_hint_truncated": hint_truncated,
                "recommendation_hint_original_length": hint_original_length,
                "matched_reasons": matched_reasons,
                "negative_reasons": negative_reasons,
                "evidence": evidence,
                "evidence_repaired": evidence_repair_code is not None,
                "evidence_repair_code": evidence_repair_code,
                "evidence_repair_reason_code": (
                    evidence_repair_detail.get("reason_code")
                    if evidence_repair_detail
                    else None
                ),
                "evidence_repair_source": (
                    evidence_repair_detail.get("source")
                    if evidence_repair_detail
                    else None
                ),
                "evidence_repair_quote_length": (
                    evidence_repair_detail.get("normalized_quote_length")
                    if evidence_repair_detail
                    else None
                ),
                "evidence_repair_item_index": (
                    evidence_repair_detail.get("evidence_item_index")
                    if evidence_repair_detail
                    else None
                ),
                # 保留旧消费字段；其内容来自已经通过来源核验的结构化证据。
                "evidence_from_title_or_abstract": [
                    evidence_item["quote"] for evidence_item in evidence
                ],
            }
        )

    validated["batch_valid"] = True
    validated["candidate_rankings"] = ranking_records
    validated["selected_papers"] = accepted
    validated["actual_selected_count"] = len(accepted)
    validated["evidence_repair_count"] = len(evidence_repair_indices)
    validated["evidence_repair_codes"] = (
        [ROUND1_EVIDENCE_REPAIR_CODE] if evidence_repair_indices else []
    )
    validated["evidence_repair_details"] = evidence_repair_details
    validated["recommendation_hint_truncation_count"] = len(
        hint_truncation_indices
    )
    validated["recommendation_hint_truncation_codes"] = (
        [ROUND1_RECOMMENDATION_HINT_TRUNCATION_CODE]
        if hint_truncation_indices
        else []
    )
    validated["validation_diagnostic"] = {
        "code": None,
        "stage": "accepted",
        "candidate_rankings_count": len(ranking_records),
        "selected_papers_count": len(accepted),
        "evidence_repair_count": len(evidence_repair_indices),
        "evidence_repair_indices": evidence_repair_indices,
        "evidence_repair_details": evidence_repair_details,
        "recommendation_hint_truncation_count": len(hint_truncation_indices),
        "recommendation_hint_truncation_indices": hint_truncation_indices,
    }
    cacheable_response = copy.deepcopy(raw_result)
    cacheable_response.pop(ROUND1_CACHE_EVIDENCE_REPAIR_KEY, None)
    cacheable_response.pop(ROUND1_CACHE_EVIDENCE_REPAIR_DETAILS_KEY, None)
    cacheable_response.pop(ROUND1_CACHE_HINT_TRUNCATION_KEY, None)
    for index, paper in enumerate(accepted):
        cacheable_response["selected_papers"][index]["evidence"] = paper[
            "evidence"
        ]
        cacheable_response["selected_papers"][index]["recommendation_hint"] = (
            paper["recommendation_hint"]
        )
    if evidence_repair_indices:
        cacheable_response[ROUND1_CACHE_EVIDENCE_REPAIR_KEY] = (
            evidence_repair_indices
        )
        cacheable_response[ROUND1_CACHE_EVIDENCE_REPAIR_DETAILS_KEY] = (
            evidence_repair_details
        )
    if hint_truncation_indices:
        cacheable_response[ROUND1_CACHE_HINT_TRUNCATION_KEY] = (
            hint_truncation_indices
        )
    validated["cacheable_response"] = cacheable_response
    return validated, warnings


ROUND2_REQUIRED_TOP_LEVEL_KEYS = (
    "task_type",
    "selection_policy",
    "profile_version",
    "prompt_version",
    "max_recommendation_count",
    "target_recommendation_count",
    "actual_recommendation_count",
    "final_recommendations",
)
ROUND2_REQUIRED_RECOMMENDATION_KEYS = (
    "arxiv_id",
    "version",
    "final_rank",
    "recommendation_level",
    "score",
    "confidence",
    "reason",
    "matched_reasons",
    "negative_reasons",
    "suggested_reading_scope",
)
ROUND2_V13_REQUIRED_RECOMMENDATION_KEYS = (
    "arxiv_id",
    "version",
    "final_rank",
    "content_label",
    "score",
    "confidence",
    "reason",
    "matched_reasons",
    "negative_reasons",
    "suggested_reading_scope",
)


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
    if prompt_version in ROUND2_PRECISION_PROMPT_VERSIONS:
        return validate_round2_v14_result(
            raw_result,
            papers,
            max_recommendations=max_recommendations,
            profile_version=profile_version,
            prompt_version=prompt_version,
        )
    """校验第二轮 Top-K，并返回不包含模型正文的安全失败诊断。"""
    warnings: list[str] = []
    uses_core_content_label = content_labels.round2_prompt_uses_core_content_label(
        prompt_version
    )
    validated: dict[str, Any] = {
        "task_type": ROUND2_TASK_TYPE,
        "batch_valid": False,
        "actual_recommendation_count": 0,
        "final_recommendations": [],
        "quality_note": "",
        "validation_error_code": None,
        "validation_diagnostic": None,
        "evidence_repair_count": 0,
        "evidence_repair_codes": [],
        "evidence_repair_positions": [],
    }

    def reject(
        code: str,
        stage: str,
        message: str,
        **safe_details: Any,
    ) -> tuple[dict[str, Any], list[str]]:
        # 诊断仅记录结构、数量、索引和固定类别，避免泄露全文或模型响应。
        warnings.append(message)
        validated["validation_error_code"] = code
        validated["validation_diagnostic"] = {
            "code": code,
            "stage": stage,
            **safe_details,
        }
        return validated, warnings

    if not isinstance(raw_result, dict):
        return reject(
            "round2_response_not_object",
            "response",
            "模型第二轮输出顶层不是 JSON 对象，已拒绝保存。",
            actual_type=type(raw_result).__name__,
        )

    required_top_level_keys = list(ROUND2_REQUIRED_TOP_LEVEL_KEYS)
    if prompt_version in {"round2_v11", "round2_v12", "round2_v13"}:
        required_top_level_keys.append("quality_note")
    missing_top = [key for key in required_top_level_keys if key not in raw_result]
    if missing_top:
        return reject(
            "round2_missing_top_level_fields",
            "identity",
            "模型第二轮输出缺少必需顶层字段，已拒绝保存。",
            missing_fields=missing_top,
        )
    if prompt_version in {"round2_v11", "round2_v12", "round2_v13"}:
        extra_top = sorted(set(raw_result) - set(required_top_level_keys))
        if extra_top:
            return reject(
                "round2_top_level_extra_fields",
                "identity",
                "模型第二轮输出包含当前版本不接受的额外顶层字段，已拒绝保存。",
                extra_fields=extra_top,
            )

    raw_profile_version = raw_result.get(
        "research_profile_version", raw_result.get("profile_version")
    )
    mismatch_fields = [
        field
        for field, actual, expected in (
            ("task_type", raw_result.get("task_type"), ROUND2_TASK_TYPE),
            (
                "selection_policy",
                raw_result.get("selection_policy"),
                round2_selection_policy_for_prompt(prompt_version),
            ),
            ("profile_version", raw_profile_version, profile_version),
            ("prompt_version", raw_result.get("prompt_version"), prompt_version),
        )
        if actual != expected
    ]
    if mismatch_fields:
        return reject(
            "round2_identity_mismatch",
            "identity",
            "模型第二轮输出任务类型、策略或版本标识不一致，已拒绝保存。",
            mismatch_fields=mismatch_fields,
        )

    if raw_result.get("max_recommendation_count") != max_recommendations:
        return reject(
            "round2_max_recommendation_count_mismatch",
            "count",
            "模型第二轮输出推荐预算不一致，已拒绝保存。",
            expected_count=max_recommendations,
            actual_type=type(raw_result.get("max_recommendation_count")).__name__,
        )

    expected_count = min(max_recommendations, len(papers))
    target_count = raw_result.get("target_recommendation_count")
    if (
        isinstance(target_count, bool)
        or not isinstance(target_count, int)
        or target_count != expected_count
    ):
        return reject(
            "round2_target_count_mismatch",
            "count",
            "模型第二轮 target_recommendation_count 不一致，已拒绝保存。",
            expected_count=expected_count,
            actual_count=target_count if isinstance(target_count, int) else None,
            actual_type=type(target_count).__name__,
        )

    raw_recommendations = raw_result.get("final_recommendations")
    if not isinstance(raw_recommendations, list):
        return reject(
            "round2_recommendations_not_array",
            "count",
            "模型第二轮输出缺少 final_recommendations 数组，已拒绝保存。",
            actual_type=type(raw_recommendations).__name__,
        )
    if len(raw_recommendations) != expected_count:
        return reject(
            "round2_recommendation_count_mismatch",
            "count",
            "模型第二轮 final_recommendations 数量不等于 "
            f"min({max_recommendations}, N)={expected_count}，已拒绝保存。",
            expected_count=expected_count,
            actual_count=len(raw_recommendations),
        )

    actual_count = raw_result.get("actual_recommendation_count")
    if (
        isinstance(actual_count, bool)
        or not isinstance(actual_count, int)
        or actual_count != expected_count
    ):
        return reject(
            "round2_actual_count_mismatch",
            "count",
            "模型第二轮 actual_recommendation_count 不一致，已拒绝保存。",
            expected_count=expected_count,
            actual_count=actual_count if isinstance(actual_count, int) else None,
            actual_type=type(actual_count).__name__,
        )

    candidate_map = {
        (paper["arxiv_id"], int(paper["version"])): paper for paper in papers
    }
    seen: set[tuple[str, int]] = set()
    accepted: list[dict[str, Any]] = []
    level_counts = {level: 0 for level in ROUND2_FINAL_RECOMMENDATION_LEVELS}
    previous_score: int | None = None
    previous_tier_priority: int | None = None
    evidence_repair_positions: list[dict[str, int]] = []

    for index, item in enumerate(raw_recommendations, start=1):
        if not isinstance(item, dict):
            return reject(
                "round2_item_not_object",
                "recommendation",
                "模型第二轮推荐项不是 JSON 对象，已拒绝保存。",
                index=index,
                actual_type=type(item).__name__,
            )
        required_item_keys = list(
            ROUND2_V13_REQUIRED_RECOMMENDATION_KEYS
            if uses_core_content_label
            else ROUND2_REQUIRED_RECOMMENDATION_KEYS
        )
        requires_contribution_tier = (
            selection_nature.round2_prompt_requires_contribution_tier(prompt_version)
        )
        if requires_contribution_tier:
            required_item_keys.append("contribution_tier")
        if round2_prompt_requires_structured_evidence(prompt_version):
            required_item_keys.append("evidence")
        missing_item = [key for key in required_item_keys if key not in item]
        if missing_item:
            return reject(
                "round2_item_missing_fields",
                "recommendation",
                "模型第二轮推荐项缺少必需字段，已拒绝保存。",
                index=index,
                missing_fields=missing_item,
            )
        if round2_prompt_uses_ranking_only(prompt_version):
            extra_item = sorted(set(item) - set(required_item_keys))
            if extra_item:
                return reject(
                    "round2_item_extra_fields",
                    "recommendation",
                    "模型第二轮推荐项包含当前版本不接受的额外字段，已拒绝保存。",
                    index=index,
                    extra_fields=extra_item,
                )

        arxiv_id = item.get("arxiv_id")
        version = parse_model_version(item.get("version"))
        final_rank = item.get("final_rank")
        level = (
            round2_recommendation_level_for_rank(index)
            if uses_core_content_label
            else item.get("recommendation_level")
        )
        content_label = item.get("content_label")
        score = item.get("score")
        confidence = item.get("confidence")
        contribution_tier = item.get("contribution_tier")
        if (
            not isinstance(arxiv_id, str)
            or version is None
            or isinstance(final_rank, bool)
            or not isinstance(final_rank, int)
            or final_rank != index
            or level not in ROUND2_FINAL_RECOMMENDATION_LEVELS
            or (
                uses_core_content_label
                and content_label not in content_labels.CORE_CONTENT_LABEL_SET
            )
            or isinstance(score, bool)
            or not isinstance(score, int)
            or not 0 <= score <= 100
            or isinstance(confidence, bool)
            or not isinstance(confidence, int)
            or not 0 <= confidence <= 100
            or (
                requires_contribution_tier
                and contribution_tier
                not in selection_nature.ROUND2_CONTRIBUTION_TIERS
            )
        ):
            return reject(
                "round2_item_identity_or_score_invalid",
                "recommendation",
                "模型第二轮推荐项身份、排序或分数格式非法，已拒绝保存。",
                index=index,
                arxiv_id_type=type(arxiv_id).__name__,
                version_type=type(item.get("version")).__name__,
                rank_type=type(final_rank).__name__,
                score_type=type(score).__name__,
                confidence_type=type(confidence).__name__,
                contribution_tier_type=type(contribution_tier).__name__,
            )

        key = (arxiv_id, version)
        if key not in candidate_map or key in seen:
            return reject(
                (
                    "round2_duplicate_candidate"
                    if key in seen
                    else "round2_candidate_not_in_batch"
                ),
                "recommendation",
                "模型第二轮推荐项不属于本批候选或存在重复，已拒绝保存。",
                index=index,
            )
        seen.add(key)

        level_counts[level] += 1
        if (
            not uses_core_content_label
            and level_counts[level] > ROUND2_RECOMMENDATION_BUDGETS[level]
        ):
            return reject(
                "round2_level_budget_exceeded",
                "recommendation",
                "模型第二轮推荐级别超过阅读预算，已拒绝保存。",
                index=index,
                level=level,
                allowed_count=ROUND2_RECOMMENDATION_BUDGETS[level],
                actual_count=level_counts[level],
            )
        if (
            not uses_core_content_label
            and previous_score is not None
            and score > previous_score
        ):
            return reject(
                "round2_score_order_invalid",
                "recommendation",
                "模型第二轮 final_recommendations 未按 score 降序排列，已拒绝保存。",
                index=index,
            )
        previous_score = score

        if requires_contribution_tier:
            tier_priority = selection_nature.ROUND2_CONTRIBUTION_TIERS.index(
                contribution_tier
            )
            if (
                previous_tier_priority is not None
                and tier_priority < previous_tier_priority
            ):
                return reject(
                    "round2_contribution_tier_order_invalid",
                    "recommendation",
                    "模型第二轮未按 A、B、C、D、E 贡献层级排序，已拒绝保存。",
                    index=index,
                    contribution_tier=contribution_tier,
                )
            previous_tier_priority = tier_priority

        if prompt_version in {"round2_v11", "round2_v12", "round2_v13"}:
            reason = strict_round2_text(item.get("reason"), max_chars=300)
            matched_reasons = strict_round2_string_list(
                item.get("matched_reasons"), min_items=1
            )
            negative_reasons = strict_round2_string_list(
                item.get("negative_reasons"), min_items=0
            )
            suggested_reading_scope = strict_round2_text(
                item.get("suggested_reading_scope"), max_chars=300
            )
            if (
                reason is None
                or matched_reasons is None
                or negative_reasons is None
                or suggested_reading_scope is None
            ):
                return reject(
                    "round2_item_text_fields_invalid",
                    "recommendation",
                    "模型第二轮推荐项的短文本字段类型、数量或长度非法，已拒绝保存。",
                    index=index,
                )
            if contribution_tier in selection_nature.ROUND2_AUTOFILL_TIERS and not (
                negative_reasons
            ):
                return reject(
                    "round2_autofill_negative_reason_required",
                    "recommendation",
                    "模型第二轮固定名额补位项缺少负面边界，已拒绝保存。",
                    index=index,
                    contribution_tier=contribution_tier,
                )
        else:
            reason = str(item.get("reason", ""))[:1000]
            matched_reasons = safe_model_string_list(item.get("matched_reasons"))
            negative_reasons = safe_model_string_list(item.get("negative_reasons"))
            suggested_reading_scope = str(
                item.get("suggested_reading_scope", "")
            )[:500]

        repaired_evidence_indices: list[int] = []
        if round2_prompt_requires_structured_evidence(prompt_version):
            evidence, evidence_error, repaired_evidence_indices = (
                validate_or_repair_round2_structured_evidence(
                    item.get("evidence"),
                    candidate_map[key],
                    allow_repair=round2_prompt_allows_evidence_repair(
                        prompt_version
                    ),
                )
            )
            if repaired_evidence_indices:
                evidence_repair_positions.extend(
                    {
                        "recommendation_index": index,
                        "evidence_index": evidence_index,
                    }
                    for evidence_index in repaired_evidence_indices
                )
                warnings.append(
                    f"模型第二轮第 {index} 项的证据未逐字匹配，"
                    "已从其声明的 PDF 物理页确定性重建。"
                )
            if evidence_error:
                evidence_error_codes = {
                    "evidence 必须是包含 1 至 3 项的数组": "evidence_count_invalid",
                    "evidence 每项必须是对象": "evidence_item_not_object",
                    "evidence 每项字段必须严格为 source、page_number、quote": "evidence_fields_invalid",
                    "evidence source 非法": "evidence_source_invalid",
                    "evidence quote 为空或类型非法": "evidence_quote_invalid",
                    "evidence quote 超过长度限制": "evidence_quote_too_long",
                    "evidence quote 过短，无法可靠核验": "evidence_quote_too_short",
                    "全文模式 evidence 必须来自 pdf_full_text": "evidence_fulltext_source_invalid",
                    "evidence PDF 物理页码不存在": "evidence_page_invalid",
                    "章节降级模式 evidence source 非法": "evidence_section_source_invalid",
                    "章节降级模式 evidence page_number 必须为 null": "evidence_section_page_invalid",
                    "evidence 对应的第二轮输入模式非法": "evidence_input_mode_invalid",
                    "evidence 原文不在标注来源或页码中": "evidence_quote_not_on_page",
                }
                return reject(
                    evidence_error_codes.get(
                        evidence_error, "evidence_validation_failed"
                    ),
                    "evidence",
                    f"模型第二轮第 {index} 项证据校验失败："
                    f"{evidence_error}，已拒绝保存。",
                    index=index,
                )
        else:
            evidence = []

        accepted.append(
            {
                **candidate_map[key],
                "final_rank": final_rank,
                "recommendation_level": str(level),
                "score": score,
                "confidence": confidence,
                **(
                    {"contribution_tier": contribution_tier}
                    if requires_contribution_tier
                    else {}
                ),
                **(
                    {"content_label": content_label}
                    if uses_core_content_label
                    else {}
                ),
                "reason": reason,
                "matched_reasons": matched_reasons,
                "negative_reasons": negative_reasons,
                "evidence": evidence,
                "evidence_repaired": bool(repaired_evidence_indices),
                "evidence_repair_code": (
                    ROUND2_EVIDENCE_REPAIR_CODE
                    if repaired_evidence_indices
                    else None
                ),
                "evidence_repair_indices": repaired_evidence_indices,
                "suggested_reading_scope": suggested_reading_scope,
            }
        )

    validated["batch_valid"] = True
    validated["actual_recommendation_count"] = len(accepted)
    validated["final_recommendations"] = accepted
    if prompt_version in {"round2_v11", "round2_v12", "round2_v13"}:
        quality_note = strict_round2_text(raw_result.get("quality_note"), max_chars=300)
        if quality_note is None:
            return reject(
                "round2_quality_note_invalid",
                "response",
                "模型第二轮 quality_note 类型或长度非法，已拒绝保存。",
            )
    else:
        quality_note = str(raw_result.get("quality_note", ""))[:1000]
    validated["quality_note"] = quality_note
    validated["evidence_repair_count"] = len(evidence_repair_positions)
    validated["evidence_repair_codes"] = (
        [ROUND2_EVIDENCE_REPAIR_CODE] if evidence_repair_positions else []
    )
    validated["evidence_repair_positions"] = evidence_repair_positions
    validated["validation_diagnostic"] = {
        "code": None,
        "stage": "accepted",
        "recommendation_count": len(accepted),
        "evidence_repair_count": len(evidence_repair_positions),
        "evidence_repair_positions": evidence_repair_positions,
    }
    cacheable_response = copy.deepcopy(raw_result)
    if round2_prompt_requires_structured_evidence(prompt_version):
        for index, paper in enumerate(accepted):
            cacheable_response["final_recommendations"][index]["evidence"] = paper[
                "evidence"
            ]
    validated["cacheable_response"] = cacheable_response
    return validated, warnings


def stable_json_hash(value: Any) -> str:
    """对 JSON 兼容对象生成稳定 SHA-256；只保存摘要，不保存原始输入。"""
    canonical = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def build_round1_batch_cache_identity(
    *,
    messages: list[dict[str, str]],
    papers: list[dict[str, Any]],
    feedback_samples: list[dict[str, Any]],
    input_char_count: int,
    config: dict[str, Any],
) -> dict[str, Any]:
    """构造第一轮批量缓存身份；只返回哈希和安全摘要，不返回完整 prompt。"""
    task_type = "round1_abstract_screening"
    round1_deepseek = deepseek_stage_config(config, "round1")
    model_name = round1_deepseek["model"]
    prompt_version = config["versions"]["round1_prompt_version"]
    research_profile_version = config["versions"]["research_profile_version"]
    selection_policy_version = config["round1_selection_policy_version"]
    input_hash = stable_json_hash(
        {
            "messages": messages,
            "thinking_mode": round1_deepseek["thinking_mode"],
            "reasoning_effort": round1_deepseek["reasoning_effort"],
            "max_output_tokens": round1_deepseek["max_output_tokens"],
        }
    )
    candidate_id_list = [
        {
            "arxiv_id": paper["arxiv_id"],
            "version": int(paper["version"]),
        }
        for paper in papers
    ]
    candidate_id_list_hash = stable_json_hash(candidate_id_list)
    feedback_sample_hash = stable_json_hash(feedback_samples)
    cache_identity = {
        "task_type": task_type,
        "model_name": model_name,
        "prompt_version": prompt_version,
        "research_profile_version": research_profile_version,
        "selection_policy_version": selection_policy_version,
        "max_output_tokens": round1_deepseek["max_output_tokens"],
        "input_hash": input_hash,
        "candidate_id_list_hash": candidate_id_list_hash,
        "feedback_sample_hash": feedback_sample_hash,
    }
    request_summary = {
        "candidate_count": len(papers),
        "input_char_count": input_char_count,
        "prompt_version": prompt_version,
        "selection_policy": config["round1_selection_policy"],
        "selection_policy_version": selection_policy_version,
        "model_name": model_name,
        "max_output_tokens": round1_deepseek["max_output_tokens"],
    }
    return {
        **cache_identity,
        "cache_key": stable_json_hash(cache_identity),
        "request_summary": request_summary,
        "request_summary_json": json.dumps(
            request_summary,
            ensure_ascii=False,
            separators=(",", ":"),
        ),
    }


def save_round1_batch_cache(
    connection: sqlite3.Connection,
    *,
    raw_response: dict[str, Any] | None,
    batch_valid: bool,
    messages: list[dict[str, str]],
    papers: list[dict[str, Any]],
    feedback_samples: list[dict[str, Any]],
    input_char_count: int,
    config: dict[str, Any],
) -> bool:
    """只为完整验收的第一轮批次保存规范化 JSON 和安全摘要。"""
    if not batch_valid or not isinstance(raw_response, dict):
        return False

    identity = build_round1_batch_cache_identity(
        messages=messages,
        papers=papers,
        feedback_samples=feedback_samples,
        input_char_count=input_char_count,
        config=config,
    )

    try:
        with connection:
            connection.execute(
                """
                INSERT INTO batch_cache (
                    cache_key, task_type, model_name, prompt_version,
                    research_profile_version, selection_policy_version, input_hash,
                    candidate_id_list_hash, feedback_sample_hash,
                    request_summary, response_json, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(cache_key) DO UPDATE SET
                    request_summary = excluded.request_summary,
                    response_json = excluded.response_json,
                    created_at = excluded.created_at
                """,
                (
                    identity["cache_key"],
                    identity["task_type"],
                    identity["model_name"],
                    identity["prompt_version"],
                    identity["research_profile_version"],
                    identity["selection_policy_version"],
                    identity["input_hash"],
                    identity["candidate_id_list_hash"],
                    identity["feedback_sample_hash"],
                    identity["request_summary_json"],
                    json.dumps(
                        raw_response,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                    current_time_iso(),
                ),
            )
    except sqlite3.Error as exc:
        raise RuntimeError(f"SQLite 保存第一轮批量缓存失败：{exc}") from exc
    return True


def load_valid_round1_batch_cache(
    connection: sqlite3.Connection,
    *,
    messages: list[dict[str, str]],
    papers: list[dict[str, Any]],
    feedback_samples: list[dict[str, Any]],
    input_char_count: int,
    config: dict[str, Any],
) -> tuple[str, dict[str, Any] | None, list[str]]:
    """读取并校验第一轮批量缓存；无效缓存统一按 cache_miss 处理。"""
    identity = build_round1_batch_cache_identity(
        messages=messages,
        papers=papers,
        feedback_samples=feedback_samples,
        input_char_count=input_char_count,
        config=config,
    )
    try:
        row = connection.execute(
            """
            SELECT
                task_type, model_name, prompt_version, research_profile_version,
                selection_policy_version, input_hash, candidate_id_list_hash,
                feedback_sample_hash,
                request_summary, response_json
            FROM batch_cache
            WHERE cache_key = ?
            LIMIT 1
            """,
            (identity["cache_key"],),
        ).fetchone()
    except sqlite3.Error as exc:
        raise RuntimeError(f"SQLite 读取第一轮批量缓存失败：{exc}") from exc

    if row is None:
        return "cache_miss", None, []

    expected_fields = (
        "task_type",
        "model_name",
        "prompt_version",
        "research_profile_version",
        "selection_policy_version",
        "input_hash",
        "candidate_id_list_hash",
        "feedback_sample_hash",
    )
    for field in expected_fields:
        if row[field] != identity[field]:
            return "cache_miss", None, [
                "第一轮缓存身份字段不一致，已忽略缓存。"
            ]
    if row["request_summary"] != identity["request_summary_json"]:
        return "cache_miss", None, [
            "第一轮缓存请求摘要不一致，已忽略缓存。"
        ]

    response_json = row["response_json"]
    if not isinstance(response_json, str) or not response_json.strip():
        return "cache_miss", None, [
            "第一轮缓存 response_json 为空，已忽略缓存。"
        ]
    try:
        raw_response = json.loads(response_json)
    except json.JSONDecodeError:
        return "cache_miss", None, [
            "第一轮缓存 response_json 不是合法 JSON，已忽略缓存。"
        ]
    if not isinstance(raw_response, dict):
        return "cache_miss", None, [
            "第一轮缓存 response_json 顶层不是对象，已忽略缓存。"
        ]

    validated_result, validation_warnings = validate_round1_result(
        raw_response,
        papers,
        max_selected=config["round1_max_selected_n"],
        profile_version=config["versions"]["research_profile_version"],
        prompt_version=config["versions"]["round1_prompt_version"],
        selection_policy_version=config["round1_selection_policy_version"],
    )
    if not validated_result["batch_valid"]:
        return "cache_miss", None, validation_warnings
    if config["versions"]["round1_prompt_version"] in ROUND1_PRECISION_PROMPT_VERSIONS:
        cached_audit = raw_response.get(ROUND1_V19_CACHE_AUDIT_KEY)
        selected = validated_result["selected_papers"]
        cached_items = raw_response.get("selected_papers")
        if not screening_stage_audit_valid(
            cached_audit, expected_accepted_count=len(selected)
        ) or not isinstance(cached_items, list):
            return "cache_miss", None, [
                "第一轮 v19 缓存缺少合法的逐篇选择审计，已忽略缓存。"
            ]
        accepted_positions = cached_audit["accepted_positions"]
        for index, (paper, cached_item, position) in enumerate(
            zip(selected, cached_items, accepted_positions, strict=True), start=1
        ):
            if not isinstance(cached_item, dict) or not isinstance(position, dict):
                return "cache_miss", None, ["第一轮 v19 缓存位置审计非法。"]
            model_position = cached_item.get("_arxivkaleid_model_position")
            evidence_status = cached_item.get("_arxivkaleid_evidence_status")
            evidence_invalid_count = cached_item.get(
                "_arxivkaleid_evidence_invalid_count"
            )
            if (
                isinstance(model_position, bool)
                or not isinstance(model_position, int)
                or model_position < 1
                or position.get("model_position") != model_position
                or position.get("final_rank") != index
                or position.get("candidate_index") != paper["candidate_index"]
                or evidence_status
                not in {"missing", "empty", "valid", "partial", "invalid"}
                or isinstance(evidence_invalid_count, bool)
                or not isinstance(evidence_invalid_count, int)
                or evidence_invalid_count < 0
            ):
                return "cache_miss", None, ["第一轮 v19 缓存安全字段非法。"]
            paper["model_position"] = model_position
            paper["evidence_status"] = evidence_status
            paper["evidence_invalid_count"] = evidence_invalid_count
        validated_result["selection_audit"] = cached_audit
        validated_result["validation_diagnostic"] = cached_audit
        return "cache_hit", validated_result, validation_warnings
    repair_indices = raw_response.get(ROUND1_CACHE_EVIDENCE_REPAIR_KEY, [])
    repair_details = raw_response.get(
        ROUND1_CACHE_EVIDENCE_REPAIR_DETAILS_KEY, []
    )
    hint_truncation_indices = raw_response.get(
        ROUND1_CACHE_HINT_TRUNCATION_KEY, []
    )
    selected = validated_result["selected_papers"]
    if (
        not isinstance(repair_indices, list)
        or any(
            isinstance(index, bool)
            or not isinstance(index, int)
            or not 1 <= index <= len(selected)
            for index in repair_indices
        )
        or repair_indices != sorted(set(repair_indices))
    ):
        return "cache_miss", None, [
            "第一轮缓存证据修复审计字段非法，已忽略缓存。"
        ]
    if not round1_evidence_repair_details_valid(
        repair_details,
        repair_indices=repair_indices,
        selected_count=len(selected),
    ):
        return "cache_miss", None, [
            "第一轮缓存证据修复明细非法，已忽略缓存。"
        ]
    if (
        not isinstance(hint_truncation_indices, list)
        or any(
            isinstance(index, bool)
            or not isinstance(index, int)
            or not 1 <= index <= len(selected)
            for index in hint_truncation_indices
        )
        or hint_truncation_indices != sorted(set(hint_truncation_indices))
    ):
        return "cache_miss", None, [
            "第一轮缓存阅读提示截断审计字段非法，已忽略缓存。"
        ]
    repair_detail_map = {
        int(detail["index"]): detail for detail in repair_details
    }
    for index in repair_indices:
        detail = repair_detail_map[index]
        selected[index - 1]["evidence_repaired"] = True
        selected[index - 1]["evidence_repair_code"] = (
            ROUND1_EVIDENCE_REPAIR_CODE
        )
        selected[index - 1]["evidence_repair_reason_code"] = detail[
            "reason_code"
        ]
        selected[index - 1]["evidence_repair_source"] = detail["source"]
        selected[index - 1]["evidence_repair_quote_length"] = detail[
            "normalized_quote_length"
        ]
        selected[index - 1]["evidence_repair_item_index"] = detail[
            "evidence_item_index"
        ]
    validated_result["evidence_repair_count"] = len(repair_indices)
    validated_result["evidence_repair_codes"] = (
        [ROUND1_EVIDENCE_REPAIR_CODE] if repair_indices else []
    )
    validated_result["evidence_repair_details"] = repair_details
    validated_result["validation_diagnostic"]["evidence_repair_count"] = len(
        repair_indices
    )
    validated_result["validation_diagnostic"]["evidence_repair_indices"] = (
        repair_indices
    )
    validated_result["validation_diagnostic"]["evidence_repair_details"] = (
        repair_details
    )
    for index in hint_truncation_indices:
        selected[index - 1]["recommendation_hint_truncated"] = True
    validated_result["recommendation_hint_truncation_count"] = len(
        hint_truncation_indices
    )
    validated_result["recommendation_hint_truncation_codes"] = (
        [ROUND1_RECOMMENDATION_HINT_TRUNCATION_CODE]
        if hint_truncation_indices
        else []
    )
    validated_result["validation_diagnostic"][
        "recommendation_hint_truncation_count"
    ] = len(hint_truncation_indices)
    validated_result["validation_diagnostic"][
        "recommendation_hint_truncation_indices"
    ] = hint_truncation_indices
    return "cache_hit", validated_result, validation_warnings


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
    """保存经过程序校验的第一轮单篇结果，不复用 V1 scores 表。"""
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


def relative_project_path(path: Path) -> str:
    """将项目内路径转换为可移植的正斜杠相对路径。"""
    try:
        return path.resolve().relative_to(PROJECT_ROOT.resolve()).as_posix()
    except ValueError as exc:
        raise RuntimeError("目标路径不在项目目录内。") from exc


def run_round1_pdf_download_stage(
    connection: sqlite3.Connection,
    paths: dict[str, Path],
    config: dict[str, Any],
    logger: logging.Logger,
    selected_papers: list[dict[str, Any]],
    *,
    round1_batch_succeeded: bool,
    run_date: str | None = None,
    download_function: Callable[..., list[PdfDownloadResult]] | None = None,
) -> tuple[str, list[PdfDownloadResult]]:
    """仅为本轮成功入围论文执行 PDF 下载；任何阶段异常都不撤销第一轮。"""
    if not round1_batch_succeeded:
        logger.info(
            "PDF 下载阶段跳过：第一轮未成功执行或未产生有效批次。"
        )
        return "skipped_round1_not_successful", []
    if not selected_papers:
        logger.info("PDF 下载阶段跳过：本轮第一轮入围论文数量为 0。")
        return "skipped_no_selected_papers", []

    limit = min(
        MAX_FIRST_ROUND_PDF_DOWNLOADS,
        int(config["round1_max_selected_n"]),
    )
    current_selected = selected_papers[:limit]
    pdf_papers = [
        SelectedPaper(
            arxiv_id=str(paper["arxiv_id"]),
            version=int(paper["version"]),
            title=str(paper["title"]),
        )
        for paper in current_selected
    ]
    effective_run_date = run_date or datetime.now().astimezone().date().isoformat()
    run_downloads = download_function or download_selected_papers

    try:
        results = run_downloads(
            PROJECT_ROOT,
            connection,
            paths["pdfs_dir"],
            pdf_papers,
            effective_run_date,
            logger,
            timeout_seconds=float(
                config.get("request_timeout_seconds", 30)
            ),
        )
    except Exception:
        # PDF 阶段不能让已成功保存的第一轮结果或主日报失败。
        logger.error(
            "PDF 下载阶段异常：status=pdf_download_failed；"
            "第一轮结果已保留，继续生成日报。"
        )
        results = []
        for paper in pdf_papers:
            try:
                destination = build_pdf_destination(
                    paths["pdfs_dir"], effective_run_date, paper
                )
                local_path = relative_project_path(destination)
                online_url = build_online_pdf_url(paper)
            except RuntimeError:
                local_path = ""
                online_url = ""
            results.append(
                PdfDownloadResult(
                    paper=paper,
                    online_pdf_url=online_url,
                    local_relative_path=local_path,
                    status="failed",
                    size_bytes=0,
                    network_attempted=False,
                    error_type="pdf_stage_failed",
                )
            )
            # 整体下载器异常也按逐篇失败落库，保证后续全文门控可以安全排除。
            save_pdf_download_result(connection, results[-1])
        return "completed_with_failures", results

    if any(result.status == "failed" for result in results):
        return "completed_with_failures", results
    return "completed", results


def run_pdf_sections_extraction_stage(
    connection: sqlite3.Connection,
    logger: logging.Logger,
    *,
    target_papers: set[tuple[str, int]],
    force: bool = False,
    extraction_function: Callable[..., list[PdfSectionExtractionResult]] | None = None,
) -> tuple[str, list[PdfSectionExtractionResult]]:
    """只解析当前第一轮入围 PDF；阶段异常不影响已保存结果。"""
    if not target_papers:
        logger.info("PDF 正文提取阶段跳过：本轮第一轮入围论文数量为 0。")
        return "skipped_no_selected_papers", []

    run_extraction = extraction_function or extract_pdf_sections_for_existing_downloads
    try:
        results = run_extraction(
            connection,
            PROJECT_ROOT,
            logger,
            force=force,
            target_papers=target_papers,
        )
    except Exception as exc:
        logger.error(
            "PDF 正文提取阶段异常：status=pdf_sections_failed error_type=%s；"
            "第一轮结果、PDF 下载记录和日报生成继续保留。",
            safe_error_message(exc),
        )
        return "failed", []

    if not results:
        logger.info("PDF 正文提取阶段跳过：没有已下载或已复用的本地 PDF。")
        return "skipped_no_successful_pdf", []
    if any(result.extraction_status == "failed" for result in results):
        return "completed_with_failures", results
    if any(result.extraction_status == "partial" for result in results):
        return "completed_with_partial", results
    return "completed", results


def run_pdf_fulltext_extraction_stage(
    connection: sqlite3.Connection,
    fulltext_database_path: Path,
    project_root: Path,
    logger: logging.Logger,
    *,
    target_papers: set[tuple[str, int]],
    max_pdf_pages: int,
    page_extractor: Callable[..., tuple[int, tuple[str, ...]]] | None = None,
) -> tuple[str, list[round2_fulltext_state.PdfFullTextResult]]:
    """在独立状态库中保存实际页数和合格论文逐页全文。"""
    if connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'pdf_downloads'"
    ).fetchone() is None:
        logger.error(
            "PDF 页数门控与全文提取阶段停止：主数据库缺少 pdf_downloads 表。"
        )
        return "failed", []
    database_row = connection.execute("PRAGMA database_list").fetchone()
    database_file = str(database_row[2] if database_row is not None else "")
    if (
        not database_file
        or Path(database_file).resolve().parent
        != fulltext_database_path.resolve().parent
    ):
        logger.error(
            "PDF 页数门控与全文提取阶段停止：主数据库与全文状态库目录不一致。"
        )
        return "failed", []
    try:
        fulltext_database_path.parent.mkdir(parents=True, exist_ok=True)
        fulltext_connection = sqlite3.connect(fulltext_database_path, timeout=30)
        fulltext_connection.row_factory = sqlite3.Row
        try:
            # 0篇也是合法终态，仍需生成可验证的空全文状态库供Round 2跳过模型。
            round2_fulltext_state.initialize_fulltext_schema(fulltext_connection)
            results = (
                round2_fulltext_state.process_downloaded_pdfs(
                    connection,
                    fulltext_connection,
                    project_root,
                    logger,
                    max_pdf_pages=max_pdf_pages,
                    target_papers=target_papers,
                    page_extractor=page_extractor,
                )
                if target_papers
                else []
            )
        finally:
            fulltext_connection.close()
    except Exception as exc:
        logger.error(
            "PDF 页数门控与全文提取阶段异常：status=pdf_fulltext_failed "
            "error_type=%s；第一轮结果继续保留，第二轮必须停止。",
            safe_error_message(exc),
        )
        return "failed", []
    if not target_papers:
        return "skipped_no_selected_papers", []
    if not results:
        return "skipped_no_pdf_rows", []
    if any(
        result.page_gate_status == round2_fulltext_state.PAGE_GATE_UNRESOLVED
        for result in results
    ):
        return "completed_with_unresolved", results
    if any(
        result.extraction_status == round2_fulltext_state.EXTRACTION_FAILED
        for result in results
    ):
        return "completed_with_fallback_required", results
    return "completed", results


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


def classify_arxiv_failure(error: Exception) -> str:
    """将 arXiv 异常归一化为稳定状态，不把完整异常写入日报。"""
    message = safe_error_message(error).lower()
    if "超时" in message or "timed out" in message or "timeout" in message:
        return "timeout"
    http_match = re.search(r"http(?:\s+|_)(\d{3})", message)
    if http_match:
        return f"http_{http_match.group(1)}"
    if "xml" in message or "解析失败" in message:
        return "xml_parse_failed"
    return "fetch_failed"


def inspect_deepseek_configuration(
    project_root: Path, deepseek_config: dict[str, Any]
) -> tuple[Any, list[str]]:
    """独立检查 DeepSeek 配置，不受 arXiv 抓取结果影响。"""
    key_result = load_local_api_key(project_root, deepseek_config)
    errors: list[str] = []
    if key_result.error_type:
        errors.append(key_result.error_type)
    if not str(deepseek_config.get("model", "")).strip():
        errors.append("missing_model_name")

    endpoint = str(deepseek_config.get("base_url", "")).strip()
    parsed_endpoint = urllib.parse.urlparse(endpoint)
    if parsed_endpoint.scheme != "https" or not parsed_endpoint.netloc:
        errors.append("missing_base_url")
    return key_result, errors


def normalize_for_matching(text: str) -> str:
    """统一大小写、换行、连续空白和常见连字符，仅用于匹配。"""
    normalized = text.lower()
    normalized = re.sub(r"[-‐‑‒–—]", " ", normalized)
    return re.sub(r"\s+", " ", normalized).strip()


def unique_keyword_pairs(keywords: list[str]) -> list[tuple[str, str]]:
    """按归一化结果去重，避免 black-hole 与 black hole 重复计分。"""
    pairs: list[tuple[str, str]] = []
    seen: set[str] = set()
    for keyword in keywords:
        normalized = normalize_for_matching(keyword)
        if normalized and normalized not in seen:
            seen.add(normalized)
            pairs.append((normalized, keyword))
    return pairs


def find_matches(
    normalized_text: str, keyword_pairs: list[tuple[str, str]]
) -> list[str]:
    """返回在文本中至少出现一次的关键词。"""
    return [original for normalized, original in keyword_pairs if normalized in normalized_text]


def extract_profile_terms(
    profile: str, config: dict[str, Any]
) -> list[tuple[str, str]]:
    """从配置关键词中选出研究画像明确提到的英文概念。"""
    normalized_profile = normalize_for_matching(profile)
    candidates = unique_keyword_pairs(config["keywords"] + config["core_keywords"])
    return [
        (normalized, original)
        for normalized, original in candidates
        if normalized in normalized_profile
    ]


def score_paper(
    paper: dict[str, Any],
    config: dict[str, Any],
    profile_terms: list[tuple[str, str]],
) -> dict[str, Any]:
    """使用可解释的关键词规则为单篇论文评分。"""
    title = normalize_for_matching(paper["title"])
    summary = normalize_for_matching(paper["summary"])
    combined = f"{title} {summary}".strip()

    keyword_pairs = unique_keyword_pairs(config["keywords"])
    core_pairs = unique_keyword_pairs(config["core_keywords"])
    negative_pairs = unique_keyword_pairs(config["negative_keywords"])

    title_keywords = find_matches(title, keyword_pairs)
    summary_keywords = find_matches(summary, keyword_pairs)
    title_core = find_matches(title, core_pairs)
    summary_core = find_matches(summary, core_pairs)
    negative_matches = find_matches(combined, negative_pairs)
    profile_matches = find_matches(combined, profile_terms)

    score = 0.0
    reason_parts: list[str] = []
    if paper["primary_category"] in config["arxiv_categories"]:
        score += 5
        reason_parts.append(f"目标主分类 {paper['primary_category']} +5")

    if title_keywords:
        points = 8 * len(title_keywords)
        score += points
        reason_parts.append(f"标题普通关键词 {', '.join(title_keywords)} +{points}")
    if summary_keywords:
        points = 3 * len(summary_keywords)
        score += points
        reason_parts.append(f"摘要普通关键词 {', '.join(summary_keywords)} +{points}")
    if title_core:
        points = 6 * len(title_core)
        score += points
        reason_parts.append(f"标题核心关键词 {', '.join(title_core)} +{points}")
    if summary_core:
        points = 3 * len(summary_core)
        score += points
        reason_parts.append(f"摘要核心关键词 {', '.join(summary_core)} +{points}")

    # 研究画像只提供少量附加分，并设置上限，避免与关键词规则重复放大。
    profile_bonus = min(len(profile_matches), 5)
    if profile_bonus:
        score += profile_bonus
        reason_parts.append(
            f"研究画像概念 {', '.join(profile_matches[:5])} +{profile_bonus}"
        )

    if negative_matches:
        penalty = 5 * len(negative_matches)
        score -= penalty
        reason_parts.append(f"负面关键词 {', '.join(negative_matches)} -{penalty}")

    score = max(score, 0.0)
    matched_groups = []
    if title_keywords or title_core:
        matched_groups.append(
            "标题：" + ", ".join(dict.fromkeys(title_keywords + title_core))
        )
    if summary_keywords or summary_core:
        matched_groups.append(
            "摘要：" + ", ".join(dict.fromkeys(summary_keywords + summary_core))
        )
    if profile_matches:
        matched_groups.append("画像：" + ", ".join(profile_matches[:5]))
    if negative_matches:
        matched_groups.append("负面：" + ", ".join(negative_matches))

    return {
        **paper,
        "score": score,
        "matched_keywords": "；".join(matched_groups) if matched_groups else "无",
        "reason": "；".join(reason_parts) if reason_parts else "未命中评分规则",
    }


def save_scores(
    connection: sqlite3.Connection, scored_papers: list[dict[str, Any]]
) -> None:
    """保存评分；同一论文版本重复评分时覆盖旧结果。"""
    scored_at = current_time_iso()
    try:
        connection.executemany(
            """
            INSERT INTO scores (
                arxiv_id, version, score, matched_keywords, reason, scored_at
            )
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(arxiv_id, version) DO UPDATE SET
                score = excluded.score,
                matched_keywords = excluded.matched_keywords,
                reason = excluded.reason,
                scored_at = excluded.scored_at
            """,
            [
                (
                    paper["arxiv_id"],
                    paper["version"],
                    paper["score"],
                    paper["matched_keywords"],
                    paper["reason"],
                    scored_at,
                )
                for paper in scored_papers
            ],
        )
        connection.commit()
    except sqlite3.Error as exc:
        connection.rollback()
        raise RuntimeError(f"SQLite 保存评分失败：{exc}") from exc


def main(
    *,
    force_pdf_extraction: bool = False,
    requested_submission_date: date | None = None,
    stdout: TextIO = sys.stdout,
) -> int:
    """运行 V2 第一阶段：宽抓取、第一轮筛选或严格降级、PDF 处理。"""
    started_at = current_time_iso()
    logger: logging.Logger | None = None
    connection: sqlite3.Connection | None = None
    run_id: int | None = None
    date_mode = "explicit" if requested_submission_date is not None else "automatic"
    requested_date_text = (
        requested_submission_date.isoformat()
        if requested_submission_date is not None
        else "none"
    )
    target_submission_date: date | None = None
    target_paper_count = 0

    try:
        if (
            requested_submission_date is not None
            and requested_submission_date > datetime.now(timezone.utc).date()
        ):
            raise ValueError("指定 submittedDate 不得晚于当前 UTC 日期。")
        config_created = create_default_config(CONFIG_PATH)
        config = load_config(CONFIG_PATH)
        # 历史提示词只供既有产物回读；新任务必须使用当前契约。
        if (
            config["versions"]["round1_prompt_version"]
            != CURRENT_ROUND1_PROMPT_VERSION
            or config["round1_selection_policy_version"]
            != "top_k_daily_budget_v4"
        ):
            raise RuntimeError("legacy_round1_execution_forbidden")
        round1_deepseek = deepseek_stage_config(config, "round1")
        paths = resolve_configured_paths(PROJECT_ROOT, config)
        ensure_directories(PROJECT_ROOT, paths)
        logger, log_path = setup_logging(paths["logs_dir"])

        logger.info("程序启动时间：%s", started_at)
        logger.info("项目根目录：.")
        logger.info("读取配置成功：%s", relative_project_path(CONFIG_PATH))
        if config_created:
            logger.info("已创建默认 V2 配置模板。")
        connection = init_database(paths["database"])
        # 外部调用即使替换初始化入口，usage 表也按幂等方式补齐。
        model_usage.initialize_model_usage_schema(connection)
        logger.info(
            "数据库兼容迁移成功：%s", relative_project_path(paths["database"])
        )
        run_id = start_run(connection, started_at)

        research_profile = research_profile_identity(config)
        round1_prompt = load_prompt(paths["round1_prompt"])
        logger.info(
            "使用提示词内置研究边界，兼容身份：%s",
            research_profile["profile_version"],
        )
        logger.info(
            "读取第一轮提示词成功：%s",
            relative_project_path(paths["round1_prompt"]),
        )
        logger.info("历史反馈解析数量：0（第一阶段未执行）")

        arxiv_statuses: list[str] = []
        data_source = "arxiv"
        checked_date_count = 0
        discarded_off_date_count = 0
        cross_category_duplicates = 0

        def fetch_category_for_date(
            category: str, submission_date: date
        ) -> list[dict[str, Any]]:
            def fetch_page(
                page_category: str,
                page_submission_date: date,
                start: int,
                page_size: int,
            ) -> list[dict[str, Any]]:
                url = build_arxiv_query_url(
                    page_category,
                    page_submission_date,
                    page_size,
                    start=start,
                )
                logger.info(
                    "查询 UTC submittedDate：%s，分类：%s，start=%d",
                    page_submission_date.isoformat(),
                    page_category,
                    start,
                )
                try:
                    xml_data = fetch_arxiv_metadata(
                        url=url,
                        timeout_seconds=float(
                            config.get("request_timeout_seconds", 30)
                        ),
                        cache_dir=paths["cache_dir"],
                        interval_seconds=float(
                            config["request_interval_seconds"]
                        ),
                        logger=logger,
                    )
                    return parse_arxiv_feed(xml_data, logger)
                except RuntimeError as exc:
                    failure_status = classify_arxiv_failure(exc)
                    logger.error(
                        "日期 %s 分类 %s 分页 start=%d 抓取失败（%s）：%s",
                        page_submission_date.isoformat(),
                        page_category,
                        start,
                        failure_status,
                        safe_error_message(exc),
                    )
                    raise

            category_papers = fetch_category_submission_pages(
                category=category,
                submission_date=submission_date,
                page_size=config["max_results"],
                fetch_page=fetch_page,
                logger=logger,
            )
            logger.info(
                "日期 %s 分类 %s 分页合计获取并解析论文数量：%d",
                submission_date.isoformat(),
                category,
                len(category_papers),
            )
            return category_papers

        try:
            (
                found_submission_date,
                papers,
                cross_category_duplicates,
                checked_date_count,
                discarded_off_date_count,
            ) = find_recent_nonempty_submission_batch(
                start_date_utc=(
                    requested_submission_date
                    if requested_submission_date is not None
                    else datetime.now(timezone.utc).date()
                ),
                lookback_days=(
                    0
                    if requested_submission_date is not None
                    else config["submitted_date_lookback_days"]
                ),
                categories=config["arxiv_categories"],
                fetch_category=fetch_category_for_date,
            )
            if requested_submission_date is not None:
                target_submission_date = requested_submission_date
                if found_submission_date not in (None, requested_submission_date):
                    raise RuntimeError("explicit_submitted_date_mismatch")
                if discarded_off_date_count:
                    raise RuntimeError("explicit_submitted_date_mismatch")
                if papers:
                    arxiv_statuses.append(
                        f"target_submitted_date: {target_submission_date.isoformat()}"
                    )
                    logger.info(
                        "指定目标 UTC submittedDate：%s",
                        target_submission_date.isoformat(),
                    )
                else:
                    arxiv_statuses.append("explicit_submitted_date_empty")
                    logger.info(
                        "指定 UTC submittedDate %s 没有论文。",
                        target_submission_date.isoformat(),
                    )
            elif found_submission_date is None:
                arxiv_statuses.append(
                    "no_nonempty_submitted_date_within_lookback"
                )
                logger.info(
                    "UTC 当天及向前 %d 天内未找到非空 submittedDate。",
                    config["submitted_date_lookback_days"],
                )
            else:
                target_submission_date = found_submission_date
                arxiv_statuses.append(
                    f"target_submitted_date: {target_submission_date.isoformat()}"
                )
                logger.info(
                    "目标 UTC submittedDate：%s",
                    target_submission_date.isoformat(),
                )
        except RuntimeError as exc:
            failure_status = classify_arxiv_failure(exc)
            arxiv_statuses.append(f"submitted_date_search: {failure_status}")
            if requested_submission_date is not None:
                logger.error(
                    "指定 submittedDate 抓取失败，禁止使用 SQLite 历史数据降级。"
                )
                raise
            history_limit = config["max_results"] * len(config["arxiv_categories"])
            papers = load_recent_papers(connection, history_limit)
            data_source = "history"
            arxiv_statuses.append("history_fallback")
            logger.warning(
                "submittedDate 查找失败，改用 SQLite 最近历史元数据生成降级简报：%d 篇",
                len(papers),
            )
        logger.info("submittedDate 检查日期数量：%d", checked_date_count)
        logger.info("非目标日期论文丢弃数量：%d", discarded_off_date_count)
        logger.info("目标日期跨分类合并后论文数量：%d", len(papers))
        logger.info("跨分类重复论文数量：%d", cross_category_duplicates)
        logger.info("arXiv 数据状态：%s", "; ".join(arxiv_statuses))
        target_paper_count = len(papers)

        if data_source == "arxiv":
            inserted_count, database_duplicates = insert_papers(connection, papers)
            duplicate_count = cross_category_duplicates + database_duplicates
            completed_round1_keys = load_completed_round1_keys(connection, config)
            round1_candidates, historical_completed_count = (
                filter_completed_round1_candidates(
                    papers, completed_round1_keys
                )
            )
        else:
            inserted_count = 0
            duplicate_count = 0
            round1_candidates = []
            historical_completed_count = 0
        logger.info("新增论文数量：%d", inserted_count)
        logger.info("跳过重复论文数量：%d", duplicate_count)
        logger.info(
            "历史已完成第一轮过滤数量：%d", historical_completed_count
        )
        logger.info("本轮第一轮新增候选数量：%d", len(round1_candidates))

        selected_papers: list[dict[str, Any]] = []
        validation_warnings: list[str] = []
        model_status = "degraded"
        deepseek_self_check_status = "api_self_check_skipped"
        real_deepseek_executed = False
        round1_batch_succeeded = False
        round1_cache_status = "not_checked"

        # 关键逻辑：DeepSeek 配置检查与 arXiv 抓取状态相互独立。
        # 即使使用 SQLite 历史数据，也必须明确记录缺少的模型配置。
        key_result, deepseek_configuration_errors = inspect_deepseek_configuration(
            PROJECT_ROOT, round1_deepseek
        )
        logger.info(
            "DeepSeek API key 是否已从本地密钥文件读取：%s",
            "是" if key_result.api_key else "否",
        )
        logger.info(
            "DeepSeek 配置状态：%s",
            ", ".join(deepseek_configuration_errors) or "complete",
        )

        if data_source == "history":
            logger.info("历史数据仅用于降级简报，本次不调用 DeepSeek。")
        elif not round1_candidates:
            model_status = "success"
            logger.info(
                "本轮没有新增第一轮候选，跳过 DeepSeek API 自检和第一轮调用。"
            )
        else:
            round1_messages = build_round1_messages(
                round1_prompt, research_profile, round1_candidates, config
            )
            prompt_char_count = sum(
                len(message["content"]) for message in round1_messages
            )
            logger.info("第一轮调用论文数量：%d", len(round1_candidates))
            logger.info("第一轮输入字符数：%d", prompt_char_count)
            logger.info(
                "第一轮提示词版本：%s",
                config["versions"]["round1_prompt_version"],
            )
            cache_status, cached_result, cache_warnings = (
                load_valid_round1_batch_cache(
                    connection,
                    messages=round1_messages,
                    papers=round1_candidates,
                    feedback_samples=[],
                    input_char_count=prompt_char_count,
                    config=config,
                )
            )
            round1_cache_status = cache_status
            logger.info("第一轮缓存命中情况：%s", round1_cache_status)
            round1_usage_identity = {
                "run_id": run_id,
                "task_type": "round1_abstract_screening",
                "call_purpose": "screening",
                "model_requested": round1_deepseek["model"],
                "prompt_version": config["versions"]["round1_prompt_version"],
                "research_profile_version": config["versions"][
                    "research_profile_version"
                ],
                "selection_policy_version": config[
                    "round1_selection_policy_version"
                ],
                "request_hash": stable_json_hash(
                    {
                        "messages": round1_messages,
                        "thinking_mode": round1_deepseek["thinking_mode"],
                        "reasoning_effort": round1_deepseek[
                            "reasoning_effort"
                        ],
                    }
                ),
            }

            if cache_status == "cache_hit" and cached_result is not None:
                validation_warnings.extend(cache_warnings)
                selected_papers = cached_result["selected_papers"]
                cache_call_id = model_usage.record_cache_reuse(
                    connection, **round1_usage_identity
                )
                try:
                    save_round1_screening_results(
                        connection,
                        run_id,
                        selected_papers,
                        config,
                        selection_audit=cached_result.get("selection_audit"),
                    )
                    save_round1_completion_statuses(
                        connection,
                        run_id,
                        round1_candidates,
                        selected_papers,
                        config,
                    )
                except Exception as exc:
                    model_usage.update_call_status(
                        connection,
                        cache_call_id,
                        "save_failed",
                        type(exc).__name__,
                    )
                    raise
                round1_batch_succeeded = True
                model_status = "success"
                logger.info(
                    "第一轮命中 batch_cache，跳过 DeepSeek API 自检和第一轮调用。"
                )
                logger.info("第一轮缓存结果已保存全部候选完成状态。")
            else:
                logger.info("第一轮缓存未复用，按正常 DeepSeek 路径继续。")
            if not round1_batch_succeeded:
                if deepseek_configuration_errors:
                    model_usage.record_no_api_event(
                        connection,
                        **round1_usage_identity,
                        local_cache_status="miss",
                        call_status="configuration_failed",
                        error_type=deepseek_configuration_errors[0],
                    )
                    logger.warning(
                        "DeepSeek 进入严格降级模式：%s",
                        ", ".join(deepseek_configuration_errors),
                    )
                else:
                    client = DeepSeekClient(
                        api_key=key_result.api_key,
                        model=round1_deepseek["model"],
                        endpoint_url=round1_deepseek["base_url"],
                        timeout_seconds=float(
                            round1_deepseek["timeout_seconds"]
                        ),
                        max_retries=round1_deepseek["max_retries"],
                        thinking_mode=round1_deepseek["thinking_mode"],
                        reasoning_effort=round1_deepseek["reasoning_effort"],
                    )
                    try:
                        self_check_price = model_usage.price_snapshot_from_config(
                            config, stage="round1"
                        )
                    except Exception as exc:
                        model_usage.record_no_api_event(
                            connection,
                            **round1_usage_identity,
                            local_cache_status="miss",
                            call_status="configuration_failed",
                            error_type=type(exc).__name__,
                        )
                        raise
                    self_check = client.self_check()
                    self_check_call_id = model_usage.record_api_call(
                        connection,
                        run_id=run_id,
                        task_type="round1_abstract_screening",
                        call_purpose="self_check",
                        model_requested=round1_deepseek["model"],
                        prompt_version=config["versions"]["round1_prompt_version"],
                        research_profile_version=config["versions"][
                            "research_profile_version"
                        ],
                        selection_policy_version=config[
                            "round1_selection_policy_version"
                        ],
                        request_hash=stable_json_hash(
                            {
                                "purpose": "deepseek_self_check",
                                "model": round1_deepseek["model"],
                                "thinking_mode": round1_deepseek["thinking_mode"],
                                "reasoning_effort": round1_deepseek[
                                    "reasoning_effort"
                                ],
                            }
                        ),
                        local_cache_status="miss",
                        call_result=self_check,
                        price_snapshot=self_check_price,
                    )
                    if not self_check.ok:
                        model_usage.update_call_status(
                            connection,
                            self_check_call_id,
                            "self_check_failed",
                            self_check.error_type,
                        )
                        deepseek_self_check_status = "api_self_check_failed"
                        logger.warning(
                            "DeepSeek API 自检失败：%s", self_check.error_type
                        )
                    else:
                        model_usage.update_call_status(
                            connection, self_check_call_id, "completed"
                        )
                        deepseek_self_check_status = "api_self_check_success"
                        real_deepseek_executed = True

                        # 关键逻辑：本地与云端从同一配置读取第一轮输出上限。
                        round1_price = model_usage.price_snapshot_from_config(
                            config, stage="round1"
                        )
                        round1_call = client.request_json(
                            round1_messages,
                            max_tokens=round1_deepseek["max_output_tokens"],
                        )
                        round1_call_id = model_usage.record_api_call(
                            connection,
                            **round1_usage_identity,
                            local_cache_status="miss",
                            call_result=round1_call,
                            price_snapshot=round1_price,
                        )
                        if not round1_call.ok or round1_call.data is None:
                            logger.warning(
                                "DeepSeek 第一轮调用失败：%s",
                                round1_call.error_type,
                            )
                        else:
                            try:
                                validated_result, validation_warnings = (
                                    validate_round1_result(
                                        round1_call.data,
                                        round1_candidates,
                                        max_selected=config[
                                            "round1_max_selected_n"
                                        ],
                                        profile_version=config["versions"][
                                            "research_profile_version"
                                        ],
                                        prompt_version=config["versions"][
                                            "round1_prompt_version"
                                        ],
                                        selection_policy_version=config[
                                            "round1_selection_policy_version"
                                        ],
                                    )
                                )
                            except Exception as exc:
                                model_usage.update_call_status(
                                    connection,
                                    round1_call_id,
                                    "validation_failed",
                                    type(exc).__name__,
                                )
                                raise
                            if not validated_result["batch_valid"]:
                                model_usage.update_call_status(
                                    connection,
                                    round1_call_id,
                                    "validation_failed",
                                    "round1_validation_failed",
                                )
                                diagnostic = validated_result.get(
                                    "validation_diagnostic"
                                )
                                logger.warning(
                                    "DeepSeek 第一轮顶层输出校验失败，未保存完成状态。"
                                )
                                logger.warning(
                                    "DeepSeek 第一轮安全诊断：%s",
                                    json.dumps(
                                        diagnostic,
                                        ensure_ascii=False,
                                        sort_keys=True,
                                        separators=(",", ":"),
                                    ),
                                )
                            else:
                                try:
                                    save_round1_batch_cache(
                                        connection,
                                        raw_response=validated_result[
                                            "cacheable_response"
                                        ],
                                        batch_valid=validated_result["batch_valid"],
                                        messages=round1_messages,
                                        papers=round1_candidates,
                                        feedback_samples=[],
                                        input_char_count=prompt_char_count,
                                        config=config,
                                    )
                                    selected_papers = validated_result[
                                        "selected_papers"
                                    ]
                                    save_round1_screening_results(
                                        connection,
                                        run_id,
                                        selected_papers,
                                        config,
                                        selection_audit=validated_result.get(
                                            "selection_audit"
                                        ),
                                    )
                                    save_round1_completion_statuses(
                                        connection,
                                        run_id,
                                        round1_candidates,
                                        selected_papers,
                                        config,
                                    )
                                except Exception as exc:
                                    model_usage.update_call_status(
                                        connection,
                                        round1_call_id,
                                        "save_failed",
                                        type(exc).__name__,
                                    )
                                    raise
                                model_usage.update_call_status(
                                    connection, round1_call_id, "completed"
                                )
                                logger.info(
                                    "DeepSeek 第一轮规范化 JSON 已写入 SQLite batch_cache。"
                                )
                                round1_batch_succeeded = True
                                model_status = "success"
                                logger.info(
                                    "DeepSeek 第一轮调用成功，已保存全部候选完成状态。"
                                )

        logger.info("DeepSeek API 自检状态：%s", deepseek_self_check_status)
        logger.info(
            "第一轮是否调用 DeepSeek：%s",
            "是" if real_deepseek_executed else "否",
        )
        logger.info("第一轮入围数量：%d", len(selected_papers))

        pdf_stage_status, pdf_download_results = run_round1_pdf_download_stage(
            connection,
            paths,
            config,
            logger,
            selected_papers,
            round1_batch_succeeded=round1_batch_succeeded,
        )
        logger.info(
            "PDF 下载尝试数量：%d",
            sum(result.network_attempted for result in pdf_download_results),
        )
        logger.info(
            "PDF 新下载成功数量：%d",
            sum(
                result.status == "downloaded"
                for result in pdf_download_results
            ),
        )
        logger.info(
            "PDF 复用已有文件数量：%d",
            sum(
                result.status == "reused_existing_pdf"
                for result in pdf_download_results
            ),
        )
        logger.info(
            "PDF 下载失败数量：%d",
            sum(result.status == "failed" for result in pdf_download_results),
        )
        selected_pdf_keys = {
            (result.paper.arxiv_id, result.paper.version)
            for result in pdf_download_results
        }
        fulltext_stage_status, fulltext_results = (
            run_pdf_fulltext_extraction_stage(
                connection,
                paths["database"].with_name("round2_inputs.sqlite"),
                PROJECT_ROOT,
                logger,
                target_papers=selected_pdf_keys,
                max_pdf_pages=int(config["limits"]["max_round2_pdf_pages"]),
            )
        )
        logger.info("PDF 60页门控与逐页全文状态：%s", fulltext_stage_status)
        logger.info(
            "PDF 长篇专项阅读数量：%d",
            sum(
                result.page_gate_status
                == round2_fulltext_state.PAGE_GATE_LONG_READING
                for result in fulltext_results
            ),
        )
        # v9 新流程只保留逐页全文状态，不再生成 Introduction/Conclusion 降级产物。
        logger.info("PDF 章节降级提取：否（当前 Round 2 全文专用）")
        logger.info("第二轮是否调用 DeepSeek：否（未执行）")
        logger.info("最终推荐数量：0")

        run_outcome_status = "success" if model_status == "success" else "degraded"
        logger.info("正式日报未在主流程中生成；请使用独立日报入口读取 SQLite。")

        finished_at = current_time_iso()
        success_message = (
            f"流程完成，目标日期论文 {len(papers)} 篇，第一轮新增候选 "
            f"{len(round1_candidates)} 篇，第一轮入围 "
            f"{len(selected_papers)} 篇，状态 {run_outcome_status}，"
            "正式日报待独立入口生成"
        )
        finish_run(connection, run_id, "success", success_message, finished_at)
        logger.info("运行成功：%s", success_message)
        logger.info("日志路径：%s", relative_project_path(log_path))
        main_run_status = (
            "no_new_candidates"
            if run_outcome_status == "success" and not round1_candidates
            else "completed"
            if run_outcome_status == "success"
            else "degraded"
        )
        print(f"MAIN_RUN_STATUS={main_run_status}", file=stdout)
        print(f"RUN_ID={run_id}", file=stdout)
        print(f"DATE_MODE={date_mode}", file=stdout)
        print(f"REQUESTED_DATE={requested_date_text}", file=stdout)
        print(
            "TARGET_SUBMITTED_DATE="
            + (
                target_submission_date.isoformat()
                if target_submission_date is not None
                else "none"
            ),
            file=stdout,
        )
        print(f"TARGET_PAPER_COUNT={len(papers)}", file=stdout)
        print(f"CANDIDATE_COUNT={len(round1_candidates)}", file=stdout)
        print(f"ROUND1_SELECTED_COUNT={len(selected_papers)}", file=stdout)
        return 0

    except Exception as exc:
        error_message = safe_error_message(exc)
        if connection is not None and run_id is not None:
            try:
                finish_run(
                    connection,
                    run_id,
                    "failed",
                    error_message,
                    current_time_iso(),
                )
            except Exception as database_exc:
                error_message += (
                    "；同时无法记录失败状态："
                    f"{safe_error_message(database_exc)}"
                )

        if logger is not None:
            # 关键逻辑：不写 traceback，避免 Python 把本地绝对源码路径写入日志。
            logger.error("运行失败：%s", error_message)
        else:
            print(f"运行失败：{error_message}", file=sys.stderr)
        print("MAIN_RUN_STATUS=failed", file=stdout)
        print(f"RUN_ID={run_id if run_id is not None else 'none'}", file=stdout)
        print(f"DATE_MODE={date_mode}", file=stdout)
        print(f"REQUESTED_DATE={requested_date_text}", file=stdout)
        print(
            "TARGET_SUBMITTED_DATE="
            + (
                target_submission_date.isoformat()
                if target_submission_date is not None
                else "none"
            ),
            file=stdout,
        )
        print(f"TARGET_PAPER_COUNT={target_paper_count}", file=stdout)
        print(f"ERROR_TYPE={type(exc).__name__}", file=stdout)
        return 1

    finally:
        if connection is not None:
            connection.close()
        if logger is not None:
            logger.info("程序结束时间：%s", current_time_iso())


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="运行 arXivKaleid 主流程。")
    parser.add_argument(
        "--date",
        type=parse_target_submission_date_argument,
        help="仅处理指定 UTC submittedDate，格式 YYYY-MM-DD。",
    )
    parser.add_argument(
        "--force-pdf-extraction",
        action="store_true",
        help="忽略 PDF 章节复用身份，强制重新提取所有可用 PDF。",
    )
    return parser.parse_args(argv)


def main_cli(
    argv: list[str] | None = None,
    *,
    stdout: TextIO = sys.stdout,
) -> int:
    args = parse_args(argv)
    return main(
        force_pdf_extraction=bool(args.force_pdf_extraction),
        requested_submission_date=args.date,
        stdout=stdout,
    )


if __name__ == "__main__":
    raise SystemExit(main_cli())
