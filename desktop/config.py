# Copyright (c) 2026 yyyang20
# SPDX-License-Identifier: GPL-3.0-only
# See LICENSE in the project root for the full license text.

"""Desktop 的只读配置契约；预算和资源身份不依赖在线自动化策略。"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
import re
from typing import Any

from desktop.paths import checked_path


CONFIG_VERSION = "desktop_config_v5"
MAX_BATCH_COST_CNY = Decimal("3.00")
PROMPTS = {
    "round1_prompt": "prompts/relevance_round1_v23.txt",
    "round2_prompt": "prompts/relevance_round2_v18.txt",
    "round1_research_prompt": "prompts/research_prompt_round1_v2.txt",
    "round2_research_prompt": "prompts/research_prompt_round2_v2.txt",
}


def batch_cost_limit(config: dict[str, Any]) -> Decimal:
    """配置可以收紧预算，但永远不能突破 alpha.4 的单批硬上限。"""
    try:
        value = config["budget"]["max_batch_cost_cny"]
        if not isinstance(value, str):
            raise ValueError
        cap = Decimal(value)
        if not cap.is_finite() or not Decimal(0) < cap <= MAX_BATCH_COST_CNY:
            raise ValueError
    except (KeyError, TypeError, ValueError, InvalidOperation):
        raise RuntimeError("desktop_cost_limit_invalid") from None
    return cap


def load_config(config_path: Path) -> dict[str, Any]:
    """只读应用资源，不生成默认文件，不加载凭据或用户运行数据。"""
    root = config_path.parent.resolve()
    path = checked_path(root, config_path.name)
    try:
        config = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        raise RuntimeError("desktop_config_unavailable") from None
    if not isinstance(config, dict):
        raise RuntimeError("desktop_config_invalid")
    if config.get("config_version") != CONFIG_VERSION:
        raise RuntimeError("desktop_config_version_invalid")
    if set(config) != {
        "config_version", "project_name",
        "round1_selection_policy", "round1_selection_policy_version",
        "request_timeout_seconds", "deepseek",
        "versions", "limits", "budget", "paths", "resource_integrity",
    }:
        raise RuntimeError("desktop_config_fields_invalid")
    if config.get("round1_selection_policy") != "user_prompt_selection" or (
        config.get("round1_selection_policy_version") != "user_prompt_selection_v1"
    ):
        raise RuntimeError("desktop_selection_policy_invalid")
    timeout = config.get("request_timeout_seconds")
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout < float("inf"):
        raise RuntimeError("desktop_pdf_timeout_invalid")

    versions = config.get("versions")
    if versions != {
        "research_profile_version": "profile_v2",
        "round1_prompt_version": "round1_v23",
        "round2_prompt_version": "round2_v18",
    }:
        raise RuntimeError("desktop_protocol_identity_invalid")
    limits = config.get("limits")
    fields = {
        "max_round1_request_tokens", "max_round2_request_tokens", "max_round2_pdf_pages",
        "model_context_tokens", "model_max_output_tokens", "context_safety_margin_tokens",
    }
    if not isinstance(limits, dict) or set(limits) != fields:
        raise RuntimeError("desktop_token_limits_invalid")
    for value in limits.values():
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise RuntimeError("desktop_token_limits_invalid")
    if limits["max_round2_pdf_pages"] != 60 or (
        limits["context_safety_margin_tokens"] >= limits["model_context_tokens"]
    ):
        raise RuntimeError("desktop_token_limits_invalid")
    if not isinstance(config.get("budget"), dict) or set(config["budget"]) != {"max_batch_cost_cny"}:
        raise RuntimeError("desktop_cost_limit_invalid")
    batch_cost_limit(config)

    deepseek = config.get("deepseek")
    fields = {"round1_model", "round2_model", "base_url", "timeout_seconds", "max_retries",
              "round1_max_output_tokens", "round2_max_output_tokens", "round1_thinking_mode",
              "round2_thinking_mode", "round1_reasoning_effort", "round2_reasoning_effort",
              "round2_output_transport", "round1_pricing", "round2_pricing"}
    if not isinstance(deepseek, dict) or set(deepseek) != fields:
        raise RuntimeError("desktop_model_config_invalid")
    if deepseek["base_url"] != "https://api.deepseek.com/chat/completions" or (
        type(deepseek["max_retries"]) is not int or deepseek["max_retries"] != 0
    ) or deepseek["round2_output_transport"] != "responses_named_tool_auto_v2":
        raise RuntimeError("desktop_model_config_invalid")
    timeout = deepseek["timeout_seconds"]
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout < float("inf"):
        raise RuntimeError("desktop_model_config_invalid")
    for stage in ("round1", "round2"):
        output = deepseek[f"{stage}_max_output_tokens"]
        if type(output) is not int or not 0 < output <= limits["model_max_output_tokens"]:
            raise RuntimeError("desktop_model_config_invalid")
        if deepseek[f"{stage}_model"] != "deepseek-flash" or (
            deepseek[f"{stage}_thinking_mode"] != "enabled"
        ) or deepseek[f"{stage}_reasoning_effort"] != "high":
            raise RuntimeError("desktop_model_config_invalid")
        if not isinstance(deepseek[f"{stage}_pricing"], dict):
            raise RuntimeError("desktop_model_config_invalid")

    if config.get("paths") != PROMPTS:
        raise RuntimeError("desktop_prompt_paths_invalid")
    integrity = config.get("resource_integrity")
    if not isinstance(integrity, dict) or set(integrity) != set(PROMPTS.values()):
        raise RuntimeError("desktop_resource_identity_invalid")
    # 保留原构建使用的 UTF-8 文本哈希语义；源码和冻结资源采用同一检查。
    for name, expected in integrity.items():
        if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected):
            raise RuntimeError("desktop_resource_identity_invalid")
        try:
            value = checked_path(root, name).read_text(encoding="utf-8")
        except OSError:
            raise RuntimeError("desktop_prompt_unavailable") from None
        if hashlib.sha256(value.encode("utf-8")).hexdigest() != expected:
            raise RuntimeError("desktop_prompt_hash_mismatch")
    return config
