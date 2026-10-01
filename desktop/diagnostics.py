"""Desktop 脱敏 JSONL 运行轨迹与安全内存降级。"""
from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
import json
from pathlib import Path
import threading
import time
import traceback
from typing import Any, Mapping
from uuid import uuid4

from desktop import paths
from desktop.errors import DesktopIssue, DesktopOutcome


SCHEMA_VERSION = 1
_DETAIL_KEYS = {
    "attempt", "max_attempts", "http_status", "curl_exit_code", "error_type",
    "provider_code", "provider_type", "provider_param", "finish_reason",
    "body_bytes", "sample_bytes", "sample_truncated", "sample_sha256", "markers",
    "write_out_capture", "remote_ip", "remote_ip_version", "remote_port",
    "http_version", "ssl_verify_result", "num_redirects", "timings_seconds",
    "response_header_capture", "response_headers", "response_headers_rejected",
    "validation_code", "validation_stage", "status", "failure_reason",
    "validation_diagnostic",
    "page_count", "size_bytes", "network_attempted", "input_mode",
    "candidate_count", "selected_count", "eligible_count", "recommendation_count",
    "paper_issue_count", "outcome_count", "log_failure_kind",
}
_HEADER_KEYS = {
    "date", "server", "via", "age", "content-type", "content-length",
    "retry-after", "x-cache", "x-cache-hits", "x-served-by", "x-timer", "cf-ray",
}


def new_id() -> str:
    return str(uuid4())


def _safe_scalar(value: Any) -> str | int | float | bool | None:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str) and len(value) <= 256 and all(ord(ch) >= 32 for ch in value):
        return value
    return None


def sanitize_details(details: Mapping[str, Any] | None) -> dict[str, Any]:
    safe: dict[str, Any] = {}
    for key, value in dict(details or {}).items():
        if key not in _DETAIL_KEYS:
            continue
        if key == "validation_diagnostic" and isinstance(value, Mapping):
            diagnostic: dict[str, Any] = {}
            for diagnostic_key, raw in value.items():
                name = str(diagnostic_key)
                if not (
                    name in {"code", "stage", "actual_type"}
                    or name.endswith(("_count", "_index", "_indices", "_fields", "_truncated"))
                ):
                    continue
                if isinstance(raw, (list, tuple)):
                    items = [_safe_scalar(item) for item in raw[:32]]
                    diagnostic[name] = [item for item in items if item is not None]
                else:
                    scalar = _safe_scalar(raw)
                    if scalar is not None:
                        diagnostic[name] = scalar
            safe[key] = diagnostic
        elif key == "response_headers" and isinstance(value, Mapping):
            safe[key] = {
                str(header): scalar
                for header, raw in value.items()
                if str(header).lower() in _HEADER_KEYS
                and (scalar := _safe_scalar(raw)) is not None
            }
        elif isinstance(value, Mapping):
            nested = {
                str(nested_key): scalar
                for nested_key, raw in value.items()
                if len(str(nested_key)) <= 64
                and (scalar := _safe_scalar(raw)) is not None
            }
            safe[key] = nested
        elif isinstance(value, (list, tuple)):
            items = [_safe_scalar(item) for item in value[:32]]
            safe[key] = [item for item in items if item is not None]
        else:
            scalar = _safe_scalar(value)
            if scalar is not None:
                safe[key] = scalar
    return safe


def safe_traceback(exc: BaseException) -> list[dict[str, Any]]:
    """只保留模块身份、函数和行号；不保留路径、消息、参数或 locals。"""
    frames: list[dict[str, Any]] = []
    for frame in traceback.extract_tb(exc.__traceback__)[-20:]:
        filename = Path(frame.filename)
        parts = [part for part in filename.with_suffix("").parts[-2:] if part not in {"", "."}]
        module = ".".join(parts).replace("-", "_")
        frames.append({"module": module[-128:], "function": frame.name[-128:], "line": frame.lineno})
    return frames


class DesktopDiagnostics:
    """每个进程一个 session；写盘失败时最多保留 512 条内存事件。"""

    def __init__(self, source_root: Path) -> None:
        self.session_id = new_id()
        self._source_root = source_root.resolve()
        self._lock = threading.Lock()
        self._sequence = 0
        self._memory: deque[dict[str, Any]] = deque(maxlen=512)
        self._stream = None
        self._persistent = False
        self._log_path: Path | None = None
        self._log_failure_kind: str | None = None
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        try:
            directory = paths.runtime_path(self._source_root, "logs")
            directory.mkdir(parents=True, exist_ok=True)
            target = paths.runtime_path(
                self._source_root, "logs", f"desktop-{timestamp}-{self.session_id}.jsonl"
            )
            self._stream = target.open("x", encoding="utf-8", newline="\n")
            self._log_path = target
            self._persistent = True
        except Exception as exc:
            self._stream = None
            self._persistent = False
            self._log_path = None
            self._log_failure_kind = type(exc).__name__

    @property
    def persistent(self) -> bool:
        return self._persistent

    @property
    def log_directory(self) -> Path | None:
        return self._log_path.parent if self._log_path is not None else None

    @property
    def relative_log_path(self) -> str | None:
        if self._log_path is None:
            return None
        root = paths.application_root(self._source_root)
        return self._log_path.relative_to(root).as_posix()

    @property
    def memory_events(self) -> tuple[dict[str, Any], ...]:
        return tuple(self._memory)

    def operation_id(self) -> str:
        return new_id()

    def close(self) -> None:
        with self._lock:
            if self._stream is not None:
                try:
                    self._stream.close()
                except Exception:
                    pass
                self._stream = None

    def event(
        self,
        *,
        operation_id: str,
        operation_type: str,
        stage: str,
        state: str,
        code: str | None = None,
        scope: str | None = None,
        snapshot_id: str | None = None,
        fetch_operation_id: str | None = None,
        run_id: int | None = None,
        arxiv_id: str | None = None,
        version: int | None = None,
        elapsed_ms: int | None = None,
        counts: Mapping[str, int] | None = None,
        details: Mapping[str, Any] | None = None,
        transient: bool | None = None,
        automatic_retry_permitted: bool | None = None,
        unexpected: BaseException | None = None,
    ) -> None:
        with self._lock:
            self._sequence += 1
            record: dict[str, Any] = {
                "schema_version": SCHEMA_VERSION,
                "sequence": self._sequence,
                "timestamp_utc": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                "session_id": self.session_id,
                "operation_id": operation_id,
                "operation_type": operation_type,
                "stage": stage,
                "state": state,
            }
            optional = {
                "code": code, "scope": scope, "snapshot_id": snapshot_id,
                "fetch_operation_id": fetch_operation_id, "run_id": run_id,
                "arxiv_id": arxiv_id, "version": version, "elapsed_ms": elapsed_ms,
                "transient": transient,
                "automatic_retry_permitted": automatic_retry_permitted,
            }
            record.update({key: value for key, value in optional.items() if value is not None})
            if counts:
                record["counts"] = {
                    str(key): int(value) for key, value in counts.items()
                    if isinstance(value, int) and not isinstance(value, bool) and value >= 0
                }
            safe = sanitize_details(details)
            if safe:
                record["details"] = safe
            if unexpected is not None:
                record["traceback"] = safe_traceback(unexpected)
            self._write_locked(record)

    def issue(self, *, operation_id: str, operation_type: str, issue: DesktopIssue, **identities: Any) -> None:
        self.event(
            operation_id=operation_id, operation_type=operation_type,
            stage=issue.stage, state="fail", code=issue.code, scope=issue.scope,
            details=issue.details, transient=issue.transient,
            automatic_retry_permitted=issue.automatic_retry_permitted,
            **identities,
        )

    def outcome(self, *, operation_id: str, operation_type: str, outcome: DesktopOutcome, **identities: Any) -> None:
        self.event(
            operation_id=operation_id, operation_type=operation_type,
            stage=outcome.stage, state="outcome", code=outcome.code, scope="outcome",
            details=outcome.details, **identities,
        )

    def _write_locked(self, record: dict[str, Any]) -> None:
        self._memory.append(record)
        if self._stream is None:
            return
        try:
            line = json.dumps(record, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
            self._stream.write(line + "\n")
            self._stream.flush()
        except Exception as exc:
            self._persistent = False
            self._log_path = None
            self._log_failure_kind = type(exc).__name__
            try:
                self._stream.close()
            except Exception:
                pass
            self._stream = None


class StageTimer:
    def __init__(self) -> None:
        self._started = time.monotonic()

    @property
    def elapsed_ms(self) -> int:
        return max(0, round((time.monotonic() - self._started) * 1000))
