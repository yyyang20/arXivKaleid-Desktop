# Copyright (c) 2026 yyyang20
# SPDX-License-Identifier: GPL-3.0-only
"""共享的北京时间日期边界；不依赖 GUI、数据库或网络。"""
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo


MIN_SELECTABLE_DATE = date(2025, 1, 1)


def beijing_today() -> date:
    return datetime.now(ZoneInfo("Asia/Shanghai")).date()


def selectable_date(day: date, today: date) -> bool:
    # datetime 是 date 的子类，但不能作为论文自然日输入。
    return type(day) is date and type(today) is date and MIN_SELECTABLE_DATE <= day <= today


def midnight_delay_ms() -> int:
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    tomorrow = datetime.combine(now.date() + timedelta(days=1), time.min, now.tzinfo)
    return max(1, int((tomorrow - now).total_seconds() * 1000) + 1)
