from __future__ import annotations

import json
import re
from typing import Any


CORE_CONTENT_LABELS = ("成像", "新解", "成像｜新解", "其他")
CORE_CONTENT_LABEL_SET = frozenset(CORE_CONTENT_LABELS)

# 以下理由码与 A–E 映射只用于 round1_v16/v17 和 round2_v12 历史产物。
IMAGING_REASON_CODES = {
    "strong_gravity_imaging",
    "polarization_imaging",
    "eht_vlbi",
}
NEW_SOLUTION_REASON_CODE = "imaging_ready_spacetime_solution"
IMAGING_METHOD_REASON_CODES = {
    "accretion_radiation",
    "reusable_accretion_model",
    "radiation_hydrodynamics",
    "ray_tracing_transfer",
}
RADIATION_HYDRODYNAMICS_ANCHOR_CODES = IMAGING_REASON_CODES | {
    "accretion_radiation",
    "reusable_accretion_model",
}
FOUNDATION_REASON_CODES = {
    "compact_object_spacetime",
    "adjacent_black_hole_astrophysics",
    "gravity_or_wave_only",
    "formal_gravity_only",
}
UNMAPPED_REASON_CODES = {"unrelated_astrophysics", "insufficient_metadata"}
KNOWN_REASON_CODES = (
    IMAGING_REASON_CODES
    | IMAGING_METHOD_REASON_CODES
    | FOUNDATION_REASON_CODES
    | UNMAPPED_REASON_CODES
    | {
        NEW_SOLUTION_REASON_CODE,
    }
)
CONTRIBUTION_TIERS = ("A", "B", "C", "D", "E")


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


def prompt_requires_content_label(prompt_version: Any) -> bool:
    """Round 1 v16 起，日报必须显示由理由码派生的内容标签。"""
    match = re.fullmatch(r"round1_v(\d+)", str(prompt_version or "").strip())
    return bool(match and int(match.group(1)) >= 16)


def content_label_from_reason_codes(reason_codes: Any) -> str:
    """从已验证的 Round 1 理由码确定性生成日报标签。"""
    if (
        not isinstance(reason_codes, list)
        or not reason_codes
        or len(reason_codes) > 3
        or any(not isinstance(code, str) or not code for code in reason_codes)
        or len(set(reason_codes)) != len(reason_codes)
        or any(code not in KNOWN_REASON_CODES for code in reason_codes)
    ):
        raise RuntimeError("content_label_reason_codes_invalid")

    codes = set(reason_codes)
    positive_codes = IMAGING_REASON_CODES | IMAGING_METHOD_REASON_CODES | {
        NEW_SOLUTION_REASON_CODE
    }
    if codes & UNMAPPED_REASON_CODES:
        raise RuntimeError("content_label_reason_codes_unmapped")
    if (
        codes & {"formal_gravity_only", "gravity_or_wave_only"}
        and codes & positive_codes
    ) or (
        NEW_SOLUTION_REASON_CODE in codes and "compact_object_spacetime" in codes
    ):
        raise RuntimeError("content_label_reason_codes_invalid")
    if "ray_tracing_transfer" in codes and not codes & (
        IMAGING_REASON_CODES
        | {
            "accretion_radiation",
            "reusable_accretion_model",
            "radiation_hydrodynamics",
        }
    ):
        raise RuntimeError("content_label_reason_codes_invalid")
    if (
        "radiation_hydrodynamics" in codes
        and not codes & RADIATION_HYDRODYNAMICS_ANCHOR_CODES
    ):
        raise RuntimeError("content_label_reason_codes_invalid")

    labels: list[str] = []
    if codes & IMAGING_REASON_CODES:
        labels.append("成像类")
    if NEW_SOLUTION_REASON_CODE in codes:
        labels.append("新解类")
    if labels:
        return "｜".join(labels)
    if codes & IMAGING_METHOD_REASON_CODES:
        return "成像相关方法"
    if codes <= FOUNDATION_REASON_CODES:
        return "相关基础研究"
    raise RuntimeError("content_label_reason_codes_unmapped")


def contribution_tier_from_reason_codes(reason_codes: Any) -> str:
    """将已验证的 Round 1 理由码确定性映射为贡献优先级。"""
    try:
        label = content_label_from_reason_codes(reason_codes)
    except RuntimeError as exc:
        codes = set(reason_codes) if isinstance(reason_codes, list) else set()
        if codes and codes <= UNMAPPED_REASON_CODES:
            return "E"
        raise exc
    if label in {"成像类", "成像类｜新解类"}:
        return "A"
    if label == "新解类":
        return "B"
    if label == "成像相关方法":
        return "C"
    if label == "相关基础研究":
        return "D"
    raise RuntimeError("content_label_contribution_tier_invalid")


def content_label_from_details_json(
    details_json: Any, *, prompt_version: Any
) -> str | None:
    """按提示词版本读取确定性旧标签或模型直接返回的四类标签。"""
    if not prompt_requires_content_label(prompt_version):
        return None
    try:
        details = json.loads(str(details_json))
    except (TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError("content_label_details_json_invalid") from exc
    if not isinstance(details, dict):
        raise RuntimeError("content_label_details_json_invalid")
    if round1_prompt_uses_core_content_label(prompt_version):
        return validate_core_content_label(details.get("content_label"))
    return content_label_from_reason_codes(details.get("reason_codes"))


def round2_content_label_from_details_json(
    details_json: Any, *, prompt_version: Any
) -> str | None:
    """读取 Round 2 v13+ 的全文标签；历史版本继续由调用方使用 Round 1 标签。"""
    if not round2_prompt_uses_core_content_label(prompt_version):
        return None
    try:
        details = json.loads(str(details_json))
    except (TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError("content_label_details_json_invalid") from exc
    if not isinstance(details, dict):
        raise RuntimeError("content_label_details_json_invalid")
    return validate_core_content_label(details.get("content_label"))
