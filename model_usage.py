from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any


PROVIDER = "deepseek"
COST_FORMULA_VERSION = "deepseek_cny_usage_cost_v1"
TIME_WINDOW_COST_FORMULA_VERSION = "deepseek_cny_time_window_usage_cost_v2"
SUPPORTED_COST_FORMULA_VERSIONS = {
    COST_FORMULA_VERSION,
    TIME_WINDOW_COST_FORMULA_VERSION,
}
COST_QUANTUM = Decimal("0.000000000001")
BEIJING_TIMEZONE_NAME = "Asia/Shanghai"
BEIJING_TIMEZONE = timezone(timedelta(hours=8), name=BEIJING_TIMEZONE_NAME)
PRICE_FIELDS = (
    "cache_hit_input_price_per_million",
    "cache_miss_input_price_per_million",
    "output_price_per_million",
)


@dataclass(frozen=True)
class PriceSnapshot:
    currency: str
    model_name: str
    cache_hit_input_price_per_million: Decimal
    cache_miss_input_price_per_million: Decimal
    output_price_per_million: Decimal
    source_url: str
    source_title: str
    effective_at: str
    effective_at_basis: str
    captured_at: str
    formula_version: str


@dataclass(frozen=True)
class RunUsageSummary:
    call_count: int
    api_attempt_count: int
    local_cache_hit_count: int
    known_input_tokens: int
    known_output_tokens: int
    known_total_tokens: int
    token_complete: bool
    known_cost: Decimal
    currency: str | None
    cost_complete: bool
    calculated_from_price_snapshot: bool


def current_time_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


def record_run_stage_timing(
    connection: sqlite3.Connection,
    *,
    run_id: int,
    stage_name: str,
    started_at: str,
    finished_at: str,
    duration_seconds: int,
) -> None:
    """保存一次正式阶段耗时；同一 run 的同一阶段只保留首次成功记录。"""
    if run_id <= 0 or not stage_name.strip() or duration_seconds < 0:
        raise RuntimeError("invalid_run_stage_timing")
    _validated_timestamp(started_at, "stage_started_at")
    _validated_timestamp(finished_at, "stage_finished_at")
    with connection:
        connection.execute(
            """
            INSERT OR IGNORE INTO run_stage_timings (
                run_id, stage_name, started_at, finished_at,
                duration_seconds, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                stage_name.strip(),
                started_at,
                finished_at,
                duration_seconds,
                current_time_iso(),
            ),
        )


def load_run_stage_duration_seconds(
    connection: sqlite3.Connection, run_id: int, stage_name: str
) -> int | None:
    """只读加载阶段耗时；旧数据库没有该表时返回 None。"""
    table = connection.execute(
        "SELECT 1 FROM sqlite_master "
        "WHERE type='table' AND name='run_stage_timings'"
    ).fetchone()
    if table is None:
        return None
    row = connection.execute(
        """
        SELECT duration_seconds
        FROM run_stage_timings
        WHERE run_id = ? AND stage_name = ?
        """,
        (run_id, stage_name),
    ).fetchone()
    return int(row[0]) if row is not None else None


def initialize_model_usage_schema(
    connection: sqlite3.Connection, *, manage_transaction: bool = True
) -> None:
    """只增不删地创建模型调用与 HTTP attempt 审计表。"""
    def create_objects() -> None:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS model_calls (
                call_id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id INTEGER NOT NULL,
                provider TEXT NOT NULL,
                task_type TEXT NOT NULL,
                call_purpose TEXT NOT NULL,
                model_requested TEXT NOT NULL,
                prompt_version TEXT,
                research_profile_version TEXT,
                selection_policy_version TEXT,
                request_hash TEXT,
                local_cache_status TEXT NOT NULL,
                api_called INTEGER NOT NULL,
                call_status TEXT NOT NULL,
                error_type TEXT,
                attempt_count INTEGER NOT NULL,
                started_at TEXT NOT NULL,
                finished_at TEXT
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS model_call_attempts (
                attempt_id INTEGER PRIMARY KEY AUTOINCREMENT,
                call_id INTEGER NOT NULL,
                attempt_no INTEGER NOT NULL,
                attempt_status TEXT NOT NULL,
                http_status INTEGER,
                error_type TEXT,
                model_returned TEXT,
                response_id TEXT,
                finish_reason TEXT,
                input_tokens INTEGER,
                output_tokens INTEGER,
                total_tokens INTEGER,
                cache_hit_input_tokens INTEGER,
                cache_miss_input_tokens INTEGER,
                usage_source TEXT NOT NULL,
                usage_complete INTEGER NOT NULL,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                duration_ms INTEGER,
                currency TEXT,
                price_model_name TEXT,
                cache_hit_input_price_per_million TEXT,
                cache_miss_input_price_per_million TEXT,
                output_price_per_million TEXT,
                price_source_url TEXT,
                price_source_title TEXT,
                price_effective_at TEXT,
                price_effective_at_basis TEXT,
                price_captured_at TEXT,
                input_cost TEXT,
                output_cost TEXT,
                total_cost TEXT,
                cost_source TEXT,
                cost_status TEXT NOT NULL,
                calculation_formula_version TEXT,
                UNIQUE(call_id, attempt_no)
            )
            """
        )
        attempt_columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(model_call_attempts)"
            ).fetchall()
        }
        if "finish_reason" not in attempt_columns:
            connection.execute(
                "ALTER TABLE model_call_attempts ADD COLUMN finish_reason TEXT"
            )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS ix_model_calls_run_task
            ON model_calls (run_id, task_type, call_purpose, started_at)
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS ix_model_attempts_call
            ON model_call_attempts (call_id, attempt_no)
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS run_stage_timings (
                timing_id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id INTEGER NOT NULL,
                stage_name TEXT NOT NULL,
                started_at TEXT NOT NULL,
                finished_at TEXT NOT NULL,
                duration_seconds INTEGER NOT NULL CHECK(duration_seconds >= 0),
                created_at TEXT NOT NULL,
                UNIQUE(run_id, stage_name)
            )
            """
        )

    if manage_transaction:
        with connection:
            create_objects()
    else:
        create_objects()


def _price_decimal(value: Any, field: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise RuntimeError(f"invalid_model_price:{field}") from exc
    if not parsed.is_finite() or parsed < 0:
        raise RuntimeError(f"invalid_model_price:{field}")
    return parsed


def _pricing_schedule_rates(
    pricing: dict[str, Any],
    *,
    at: datetime | None,
    conservative: bool,
) -> tuple[dict[str, Any], str | None]:
    schedule = pricing.get("rate_schedule")
    if schedule is None:
        if (
            pricing.get("calculation_formula_version")
            == TIME_WINDOW_COST_FORMULA_VERSION
        ):
            raise RuntimeError("model_price_schedule_missing")
        return pricing, None
    if not isinstance(schedule, dict):
        raise RuntimeError("invalid_model_price_schedule")
    if pricing.get("calculation_formula_version") != TIME_WINDOW_COST_FORMULA_VERSION:
        raise RuntimeError("model_price_formula_version_mismatch")
    if schedule.get("timezone") != BEIJING_TIMEZONE_NAME:
        raise RuntimeError("unsupported_model_price_timezone")

    weekdays = schedule.get("peak_iso_weekdays")
    if (
        not isinstance(weekdays, list)
        or not weekdays
        or any(
            isinstance(day, bool) or not isinstance(day, int) or not 1 <= day <= 7
            for day in weekdays
        )
        or len(set(weekdays)) != len(weekdays)
    ):
        raise RuntimeError("invalid_model_price_peak_weekdays")

    raw_windows = schedule.get("peak_windows")
    if not isinstance(raw_windows, list) or not raw_windows:
        raise RuntimeError("invalid_model_price_peak_windows")
    windows: list[tuple[time, time]] = []
    for raw_window in raw_windows:
        if not isinstance(raw_window, dict) or set(raw_window) != {"start", "end"}:
            raise RuntimeError("invalid_model_price_peak_windows")
        start_text = raw_window.get("start")
        end_text = raw_window.get("end")
        if (
            not isinstance(start_text, str)
            or not isinstance(end_text, str)
            or len(start_text) != 5
            or len(end_text) != 5
        ):
            raise RuntimeError("invalid_model_price_peak_windows")
        try:
            start = time.fromisoformat(start_text)
            end = time.fromisoformat(end_text)
        except ValueError as exc:
            raise RuntimeError("invalid_model_price_peak_windows") from exc
        if start >= end:
            raise RuntimeError("invalid_model_price_peak_windows")
        windows.append((start, end))
    ordered_windows = sorted(windows)
    if any(
        current[0] < previous[1]
        for previous, current in zip(ordered_windows, ordered_windows[1:])
    ):
        raise RuntimeError("invalid_model_price_peak_windows")

    for tier in ("peak", "off_peak"):
        rates = schedule.get(f"{tier}_rates")
        if not isinstance(rates, dict) or any(
            rates.get(field) in (None, "") for field in PRICE_FIELDS
        ):
            raise RuntimeError("invalid_model_price_schedule_rates")
        for field in PRICE_FIELDS:
            _price_decimal(rates[field], f"{tier}_{field}")

    if conservative:
        tier = "peak"
    else:
        selected_at = at or datetime.now().astimezone()
        if selected_at.tzinfo is None or selected_at.utcoffset() is None:
            raise RuntimeError("invalid_model_price_selection_time")
        local_at = selected_at.astimezone(BEIJING_TIMEZONE)
        local_time = local_at.time().replace(tzinfo=None)
        is_peak = local_at.isoweekday() in weekdays and any(
            start <= local_time < end for start, end in windows
        )
        tier = "peak" if is_peak else "off_peak"
    return schedule[f"{tier}_rates"], tier


def price_snapshot_from_config(
    config: dict[str, Any],
    *,
    stage: str = "round1",
    at: datetime | None = None,
    conservative: bool = False,
) -> PriceSnapshot | None:
    """读取指定筛选阶段的明确价格；字段不全时不猜测费用。"""
    if stage not in {"round1", "round2"}:
        raise RuntimeError(f"unsupported_model_price_stage:{stage}")
    deepseek = config.get("deepseek")
    if not isinstance(deepseek, dict):
        return None
    pricing = deepseek.get(f"{stage}_pricing")
    configured_model = str(deepseek.get(f"{stage}_model") or "")
    # 保留旧测试和历史配置的第一轮价格读取能力。
    if stage == "round1" and not isinstance(pricing, dict):
        pricing = deepseek.get("pricing")
        configured_model = str(deepseek.get("model") or "")
    if not isinstance(pricing, dict):
        return None
    required = (
        "model_name",
        "unit",
        "source_url",
        "source_title",
        "effective_at",
        "effective_at_basis",
        "confirmed_at",
        "calculation_formula_version",
    )
    if any(pricing.get(field) in (None, "") for field in required):
        return None
    currency = str(pricing.get("currency") or "").upper()
    if currency != "CNY":
        raise RuntimeError("unsupported_model_price_currency")
    if pricing.get("unit") != "per_million_tokens":
        raise RuntimeError("unsupported_model_price_unit")
    model_name = str(pricing["model_name"])
    if model_name != configured_model:
        raise RuntimeError("model_price_snapshot_mismatch")
    formula_version = str(pricing["calculation_formula_version"])
    if formula_version not in SUPPORTED_COST_FORMULA_VERSIONS:
        raise RuntimeError("model_price_formula_version_mismatch")
    source_url = str(pricing["source_url"])
    if not source_url.startswith("https://api-docs.deepseek.com/"):
        raise RuntimeError("model_price_source_not_official")
    effective_at = _validated_timestamp(pricing["effective_at"], "effective_at")
    confirmed_at = _validated_timestamp(pricing["confirmed_at"], "confirmed_at")
    if datetime.fromisoformat(effective_at) > datetime.fromisoformat(confirmed_at):
        raise RuntimeError("model_price_effective_after_confirmation")
    rates, rate_tier = _pricing_schedule_rates(
        pricing, at=at, conservative=conservative
    )
    if rate_tier is None and any(rates.get(field) in (None, "") for field in PRICE_FIELDS):
        return None
    effective_at_basis = str(pricing["effective_at_basis"])
    if rate_tier is not None:
        selection = "conservative" if conservative else "call_time"
        effective_at_basis = (
            f"{effective_at_basis};rate_tier={rate_tier};"
            f"selection={selection};timezone={BEIJING_TIMEZONE_NAME}"
        )
    return PriceSnapshot(
        currency=currency,
        model_name=model_name,
        cache_hit_input_price_per_million=_price_decimal(
            rates["cache_hit_input_price_per_million"],
            "cache_hit_input_price_per_million",
        ),
        cache_miss_input_price_per_million=_price_decimal(
            rates["cache_miss_input_price_per_million"],
            "cache_miss_input_price_per_million",
        ),
        output_price_per_million=_price_decimal(
            rates["output_price_per_million"], "output_price_per_million"
        ),
        source_url=source_url,
        source_title=str(pricing["source_title"]),
        effective_at=effective_at,
        effective_at_basis=effective_at_basis,
        captured_at=confirmed_at,
        formula_version=formula_version,
    )


def _validated_timestamp(value: Any, field: str) -> str:
    text = str(value)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise RuntimeError(f"invalid_model_price_timestamp:{field}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise RuntimeError(f"invalid_model_price_timestamp:{field}")
    return text


def _decimal_text(value: Decimal) -> str:
    return format(value.quantize(COST_QUANTUM, rounding=ROUND_HALF_UP), "f")


def calculate_attempt_cost(
    usage: Any, price: PriceSnapshot | None
) -> dict[str, str | None]:
    if usage is None:
        return _empty_cost("usage_unavailable")
    if not bool(getattr(usage, "valid", False)):
        return _empty_cost("usage_invalid")
    if not bool(getattr(usage, "complete", False)):
        if (
            getattr(usage, "cache_hit_input_tokens", None) is None
            or getattr(usage, "cache_miss_input_tokens", None) is None
        ):
            return _empty_cost("cache_breakdown_unavailable")
        return _empty_cost("usage_incomplete")
    if price is None:
        return _empty_cost("price_unavailable")
    hit_tokens = Decimal(getattr(usage, "cache_hit_input_tokens"))
    miss_tokens = Decimal(getattr(usage, "cache_miss_input_tokens"))
    output_tokens = Decimal(getattr(usage, "output_tokens"))
    divisor = Decimal(1_000_000)
    input_cost = (
        hit_tokens * price.cache_hit_input_price_per_million
        + miss_tokens * price.cache_miss_input_price_per_million
    ) / divisor
    output_cost = output_tokens * price.output_price_per_million / divisor
    total_cost = input_cost + output_cost
    return {
        "input_cost": _decimal_text(input_cost),
        "output_cost": _decimal_text(output_cost),
        "total_cost": _decimal_text(total_cost),
        "cost_source": "calculated_from_usage_and_price_snapshot",
        "cost_status": "calculated",
        "calculation_formula_version": price.formula_version,
    }


def _empty_cost(status: str) -> dict[str, str | None]:
    return {
        "input_cost": None,
        "output_cost": None,
        "total_cost": None,
        "cost_source": None,
        "cost_status": status,
        "calculation_formula_version": None,
    }


def record_cache_reuse(
    connection: sqlite3.Connection,
    *,
    run_id: int,
    task_type: str,
    call_purpose: str,
    model_requested: str,
    prompt_version: str | None,
    research_profile_version: str | None,
    selection_policy_version: str | None,
    request_hash: str | None,
) -> int:
    return record_no_api_event(
        connection,
        run_id=run_id,
        task_type=task_type,
        call_purpose=call_purpose,
        model_requested=model_requested,
        prompt_version=prompt_version,
        research_profile_version=research_profile_version,
        selection_policy_version=selection_policy_version,
        request_hash=request_hash,
        local_cache_status="hit",
        call_status="cache_reused",
        error_type=None,
    )


def record_no_api_event(
    connection: sqlite3.Connection,
    *,
    run_id: int,
    task_type: str,
    call_purpose: str,
    model_requested: str,
    prompt_version: str | None,
    research_profile_version: str | None,
    selection_policy_version: str | None,
    request_hash: str | None,
    local_cache_status: str,
    call_status: str,
    error_type: str | None,
) -> int:
    now = current_time_iso()
    with connection:
        cursor = connection.execute(
            """
            INSERT INTO model_calls (
                run_id, provider, task_type, call_purpose, model_requested,
                prompt_version, research_profile_version,
                selection_policy_version, request_hash, local_cache_status,
                api_called, call_status, error_type, attempt_count,
                started_at, finished_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, 0, ?, ?)
            """,
            (
                run_id,
                PROVIDER,
                task_type,
                call_purpose,
                model_requested,
                prompt_version,
                research_profile_version,
                selection_policy_version,
                request_hash,
                local_cache_status,
                call_status,
                error_type,
                now,
                now,
            ),
        )
    return int(cursor.lastrowid)


def record_api_call(
    connection: sqlite3.Connection,
    *,
    run_id: int,
    task_type: str,
    call_purpose: str,
    model_requested: str,
    prompt_version: str | None,
    research_profile_version: str | None,
    selection_policy_version: str | None,
    request_hash: str | None,
    local_cache_status: str,
    call_result: Any,
    price_snapshot: PriceSnapshot | None,
) -> int:
    """保存一次逻辑调用及所有 attempt；mock 旧接口按 usage 未知兼容。"""
    attempts = list(getattr(call_result, "attempts", ()) or ())
    now = current_time_iso()
    if not attempts:
        attempts = [
            type(
                "CompatibleAttempt",
                (),
                {
                    "attempt_no": 1,
                    "status": "success" if call_result.ok else call_result.error_type,
                    "error_type": call_result.error_type,
                    "http_status": None,
                    "model_returned": None,
                    "response_id": None,
                    "finish_reason": None,
                    "usage": None,
                    "started_at": now,
                    "finished_at": now,
                    "duration_ms": 0,
                },
            )()
        ]
    call_status = "api_succeeded" if call_result.ok else "api_failed"
    with connection:
        cursor = connection.execute(
            """
            INSERT INTO model_calls (
                run_id, provider, task_type, call_purpose, model_requested,
                prompt_version, research_profile_version,
                selection_policy_version, request_hash, local_cache_status,
                api_called, call_status, error_type, attempt_count,
                started_at, finished_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                PROVIDER,
                task_type,
                call_purpose,
                model_requested,
                prompt_version,
                research_profile_version,
                selection_policy_version,
                request_hash,
                local_cache_status,
                call_status,
                getattr(call_result, "error_type", None),
                len(attempts),
                attempts[0].started_at,
                attempts[-1].finished_at,
            ),
        )
        call_id = int(cursor.lastrowid)
        for attempt in attempts:
            _insert_attempt(connection, call_id, attempt, price_snapshot)
    return call_id


def _insert_attempt(
    connection: sqlite3.Connection,
    call_id: int,
    attempt: Any,
    price: PriceSnapshot | None,
) -> None:
    usage = getattr(attempt, "usage", None)
    cost = calculate_attempt_cost(usage, price)
    connection.execute(
        """
        INSERT INTO model_call_attempts (
            call_id, attempt_no, attempt_status, http_status, error_type,
            model_returned, response_id, finish_reason, input_tokens, output_tokens,
            total_tokens, cache_hit_input_tokens, cache_miss_input_tokens,
            usage_source, usage_complete, started_at, finished_at, duration_ms,
            currency, price_model_name, cache_hit_input_price_per_million,
            cache_miss_input_price_per_million, output_price_per_million,
            price_source_url, price_source_title, price_effective_at,
            price_effective_at_basis, price_captured_at,
            input_cost, output_cost, total_cost, cost_source,
            cost_status, calculation_formula_version
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                  ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            call_id,
            attempt.attempt_no,
            attempt.status,
            attempt.http_status,
            attempt.error_type,
            attempt.model_returned,
            attempt.response_id,
            getattr(attempt, "finish_reason", None),
            getattr(usage, "input_tokens", None),
            getattr(usage, "output_tokens", None),
            getattr(usage, "total_tokens", None),
            getattr(usage, "cache_hit_input_tokens", None),
            getattr(usage, "cache_miss_input_tokens", None),
            "api_response" if usage is not None else "unavailable",
            int(bool(getattr(usage, "complete", False))),
            attempt.started_at,
            attempt.finished_at,
            attempt.duration_ms,
            price.currency if price else None,
            price.model_name if price else None,
            str(price.cache_hit_input_price_per_million) if price else None,
            str(price.cache_miss_input_price_per_million) if price else None,
            str(price.output_price_per_million) if price else None,
            price.source_url if price else None,
            price.source_title if price else None,
            price.effective_at if price else None,
            price.effective_at_basis if price else None,
            price.captured_at if price else None,
            cost["input_cost"],
            cost["output_cost"],
            cost["total_cost"],
            cost["cost_source"],
            cost["cost_status"],
            cost["calculation_formula_version"],
        ),
    )


def update_call_status(
    connection: sqlite3.Connection,
    call_id: int,
    status: str,
    error_type: str | None = None,
) -> None:
    with connection:
        connection.execute(
            """
            UPDATE model_calls
            SET call_status = ?, error_type = ?, finished_at = ?
            WHERE call_id = ?
            """,
            (status, error_type, current_time_iso(), call_id),
        )


def load_run_usage_summary(
    connection: sqlite3.Connection, run_id: int
) -> RunUsageSummary | None:
    table = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='model_calls'"
    ).fetchone()
    if table is None:
        return None
    calls = connection.execute(
        "SELECT api_called, local_cache_status FROM model_calls WHERE run_id = ?",
        (run_id,),
    ).fetchall()
    if not calls:
        return None
    attempts = connection.execute(
        """
        SELECT a.input_tokens, a.output_tokens, a.total_tokens,
               a.usage_complete, a.total_cost, a.cost_status, a.currency
        FROM model_call_attempts AS a
        JOIN model_calls AS c ON c.call_id = a.call_id
        WHERE c.run_id = ?
        """,
        (run_id,),
    ).fetchall()
    known_input = sum(int(row[0]) for row in attempts if row[0] is not None)
    known_output = sum(int(row[1]) for row in attempts if row[1] is not None)
    known_total = sum(int(row[2]) for row in attempts if row[2] is not None)
    known_cost = sum(
        (Decimal(str(row[4])) for row in attempts if row[4] is not None),
        Decimal(0),
    )
    api_attempt_count = len(attempts)
    api_called_count = sum(int(row[0]) for row in calls)
    token_complete = (
        api_attempt_count > 0 and all(bool(row[3]) for row in attempts)
    ) or api_called_count == 0
    cost_complete = (
        api_attempt_count > 0
        and all(row[5] == "calculated" for row in attempts)
    ) or api_called_count == 0
    currencies = {str(row[6]) for row in attempts if row[6] is not None}
    currency = next(iter(currencies)) if len(currencies) == 1 else None
    if len(currencies) > 1:
        cost_complete = False
    return RunUsageSummary(
        call_count=len(calls),
        api_attempt_count=api_attempt_count,
        local_cache_hit_count=sum(row[1] == "hit" for row in calls),
        known_input_tokens=known_input,
        known_output_tokens=known_output,
        known_total_tokens=known_total,
        token_complete=token_complete,
        known_cost=known_cost,
        currency=currency,
        cost_complete=cost_complete,
        calculated_from_price_snapshot=any(
            row[5] == "calculated" for row in attempts
        ),
    )


def load_run_model_names(
    connection: sqlite3.Connection, run_id: int
) -> tuple[str, ...]:
    """从成功调用账本读取实际模型；不依赖是否有论文入选。"""
    table = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='model_calls'"
    ).fetchone()
    if table is None:
        return ()
    rows = connection.execute(
        """
        SELECT DISTINCT trim(model_requested)
        FROM model_calls
        WHERE run_id = ? AND call_status = 'completed'
          AND (api_called = 1 OR local_cache_status = 'hit')
          AND trim(model_requested) != ''
        ORDER BY trim(model_requested)
        """,
        (run_id,),
    ).fetchall()
    return tuple(str(row[0]) for row in rows)
