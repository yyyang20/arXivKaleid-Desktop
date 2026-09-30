from __future__ import annotations

import re
from typing import Any


SELECTION_NATURE_FIXED_SLOT_FILL = "固定名额补位"
ROUND2_CONTRIBUTION_TIERS = ("A", "B", "C", "D", "E")
ROUND2_AUTOFILL_TIERS = {"D", "E"}


def round1_prompt_requires_selection_nature(prompt_version: Any) -> bool:
    """仅历史 Round 1 v17 产物显示独立的入选性质。"""
    match = re.fullmatch(r"round1_v(\d+)", str(prompt_version or "").strip())
    return bool(match and int(match.group(1)) == 17)


def round2_prompt_requires_contribution_tier(prompt_version: Any) -> bool:
    """仅历史 Round 2 v12 产物要求受控 A–E 贡献层级。"""
    match = re.fullmatch(r"round2_v(\d+)", str(prompt_version or "").strip())
    return bool(match and int(match.group(1)) == 12)


def round1_selection_nature_from_content_label(
    content_label: str | None, *, prompt_version: Any
) -> str | None:
    """Top 10 只将基础研究标为固定名额补位。"""
    if not round1_prompt_requires_selection_nature(prompt_version):
        return None
    if content_label == "相关基础研究":
        return SELECTION_NATURE_FIXED_SLOT_FILL
    if content_label in {
        "成像类",
        "新解类",
        "成像类｜新解类",
        "成像相关方法",
    }:
        return None
    raise RuntimeError("selection_nature_content_label_invalid")


def round2_selection_nature_from_tier(
    contribution_tier: Any, *, prompt_version: Any
) -> str | None:
    """Top 5 根据全文 A–E 层级确定补位性质。"""
    if not round2_prompt_requires_contribution_tier(prompt_version):
        return None
    if contribution_tier not in ROUND2_CONTRIBUTION_TIERS:
        raise RuntimeError("selection_nature_round2_tier_invalid")
    return (
        SELECTION_NATURE_FIXED_SLOT_FILL
        if contribution_tier in ROUND2_AUTOFILL_TIERS
        else None
    )
