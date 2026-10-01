from __future__ import annotations

import json
import re
from typing import Any


CORE_CONTENT_LABELS = ("成像", "新解", "成像｜新解", "其他")
CORE_CONTENT_LABEL_SET = frozenset(CORE_CONTENT_LABELS)

# 以下理由码与 A–E 映射只用于 round1_v16/v17 和 round2_v12 历史产物。


def _prompt_number(prompt_version: Any, prefix: str) -> int | None:
    match = re.fullmatch(rf"{re.escape(prefix)}(\d+)", str(prompt_version or "").strip())
    return int(match.group(1)) if match else None


def round1_prompt_uses_core_content_label(prompt_version: Any) -> bool:
    """Round 1 v18 起直接返回四类内容标签。"""
    number = _prompt_number(prompt_version, "round1_v")
    return number is not None and number >= 18


def round2_prompt_uses_core_content_label(prompt_version: Any) -> bool:
    """Round 2 v13 起基于全文直接返回四类内容标签。"""
    number = _prompt_number(prompt_version, "round2_v")
    return number is not None and number >= 13


def validate_core_content_label(value: Any) -> str:
    """严格校验新版公开内容标签，不做别名或语义猜测。"""
    if not isinstance(value, str) or value not in CORE_CONTENT_LABEL_SET:
        raise RuntimeError("content_label_invalid")
    return value


def content_label_from_details_json(
    details_json: Any, *, prompt_version: Any
) -> str | None:
    """读取当前 Desktop 第一轮直接返回的四类标签。"""
    if prompt_version != "round1_v20":
        raise RuntimeError("desktop_round1_prompt_unsupported")
    try:
        details = json.loads(str(details_json))
    except (TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError("content_label_details_json_invalid") from exc
    if not isinstance(details, dict):
        raise RuntimeError("content_label_details_json_invalid")
    return validate_core_content_label(details.get("content_label"))


def round2_content_label_from_details_json(
    details_json: Any, *, prompt_version: Any
) -> str | None:
    """读取当前 Desktop 第二轮基于全文返回的四类标签。"""
    if prompt_version != "round2_v15":
        raise RuntimeError("desktop_round2_prompt_unsupported")
    try:
        details = json.loads(str(details_json))
    except (TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError("content_label_details_json_invalid") from exc
    if not isinstance(details, dict):
        raise RuntimeError("content_label_details_json_invalid")
    return validate_core_content_label(details.get("content_label"))
