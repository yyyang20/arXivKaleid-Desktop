# Copyright (c) 2026 yyyang20
# SPDX-License-Identifier: GPL-3.0-only
# See LICENSE in the project root for the full license text.

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Callable


@dataclass(frozen=True)
class ProgressEvent:
    """Desktop 后台发出的结构化进度；未知数量保持为 None。"""

    task_type: str
    stage: str
    state: str
    message: str
    current_date: date | None = None
    category: str | None = None
    category_index: int | None = None
    category_total: int | None = None
    processed: int | None = None
    total: int | None = None
    result_count: int | None = None
    code: str | None = None
    scope: str | None = None
    session_id: str | None = None
    operation_id: str | None = None
    snapshot_id: str | None = None
    fetch_operation_id: str | None = None
    run_id: int | None = None


ProgressCallback = Callable[[ProgressEvent], None]


def emit_progress(callback: ProgressCallback | None, event: ProgressEvent) -> None:
    """进度观察器不得改变真实业务流程或把 GUI 异常带回后台。"""
    if callback is None:
        return
    try:
        callback(event)
    except Exception:
        pass
