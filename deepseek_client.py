from __future__ import annotations

import json
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable


@dataclass(frozen=True)
class DeepSeekUsage:
    """API 返回的 token 统计；字段缺失或非法时保留不完整状态。"""

    input_tokens: int | None
    output_tokens: int | None
    total_tokens: int | None
    cache_hit_input_tokens: int | None
    cache_miss_input_tokens: int | None
    valid: bool
    complete: bool


@dataclass(frozen=True)
class ProviderErrorDiagnostic:
    """供应商错误的脱敏白名单字段；不保存原始响应正文。"""

    code: str | None
    error_type: str | None
    param: str | None
    message: str | None


@dataclass(frozen=True)
class DeepSeekAttemptResult:
    """一次真实 HTTP attempt 的安全遥测，不保存请求或响应正文。"""

    attempt_no: int
    status: str
    error_type: str | None
    http_status: int | None
    model_returned: str | None
    response_id: str | None
    usage: DeepSeekUsage | None
    started_at: str
    finished_at: str
    duration_ms: int
    finish_reason: str | None = None
    provider_error: ProviderErrorDiagnostic | None = None


@dataclass(frozen=True)
class DeepSeekCallResult:
    """DeepSeek 调用结果；失败时不保留远端响应正文。"""

    ok: bool
    data: dict[str, Any] | None
    error_type: str | None
    attempts: tuple[DeepSeekAttemptResult, ...] = ()


class DeepSeekClientError(RuntimeError):
    """仅携带可安全写入日志的错误类型。"""

    def __init__(
        self,
        error_type: str,
        *,
        http_status: int | None = None,
        model_returned: str | None = None,
        response_id: str | None = None,
        usage: DeepSeekUsage | None = None,
        finish_reason: str | None = None,
        provider_error: ProviderErrorDiagnostic | None = None,
    ) -> None:
        super().__init__(error_type)
        self.error_type = error_type
        self.http_status = http_status
        self.model_returned = model_returned
        self.response_id = response_id
        self.usage = usage
        self.finish_reason = finish_reason
        self.provider_error = provider_error


@dataclass(frozen=True)
class _ParsedDeepSeekResponse:
    data: dict[str, Any]
    http_status: int
    model_returned: str | None
    response_id: str | None
    usage: DeepSeekUsage | None
    finish_reason: str | None


OUTPUT_TRUNCATED_FINISH_REASONS = {
    "length",
    "max_tokens",
    "max_completion_tokens",
}
WHOLE_JSON_FENCE_PATTERN = re.compile(
    r"\A```json[ \t]*\r?\n(?P<body>[\s\S]*?)\r?\n```[ \t]*\Z",
    flags=re.IGNORECASE,
)
PROVIDER_ERROR_BODY_LIMIT_BYTES = 8192
PROVIDER_ERROR_FIELD_LIMIT = 128
PROVIDER_ERROR_MESSAGE_LIMIT = 512
PROVIDER_ERROR_SECRET_PATTERNS = (
    re.compile(r"(?i)\bbearer\s+[^\s,;]+"),
    re.compile(r"(?i)\bsk-[a-z0-9_-]{6,}\b"),
    re.compile(r"(?i)(api[_ -]?key\s*[:=]\s*)[^\s,;]+"),
)


def sanitize_provider_error_text(value: Any, *, limit: int) -> str | None:
    """清理供应商错误字段，限制长度并移除常见凭据形态。"""
    if not isinstance(value, str):
        return None
    cleaned = "".join(character if ord(character) >= 32 else " " for character in value)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    for pattern in PROVIDER_ERROR_SECRET_PATTERNS:
        cleaned = pattern.sub(
            lambda match: (
                f"{match.group(1)}[REDACTED]"
                if match.lastindex
                else "[REDACTED]"
            ),
            cleaned,
        )
    if not cleaned:
        return None
    return cleaned[:limit]


def parse_provider_error_diagnostic(raw_body: bytes) -> ProviderErrorDiagnostic | None:
    """只从小型 JSON 错误体提取固定字段，拒绝保留其他内容。"""
    if not isinstance(raw_body, bytes) or len(raw_body) > PROVIDER_ERROR_BODY_LIMIT_BYTES:
        return None
    try:
        payload = json.loads(raw_body.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    error = payload.get("error") if isinstance(payload, dict) else None
    if not isinstance(error, dict):
        return None
    diagnostic = ProviderErrorDiagnostic(
        code=sanitize_provider_error_text(
            error.get("code"), limit=PROVIDER_ERROR_FIELD_LIMIT
        ),
        error_type=sanitize_provider_error_text(
            error.get("type"), limit=PROVIDER_ERROR_FIELD_LIMIT
        ),
        param=sanitize_provider_error_text(
            error.get("param"), limit=PROVIDER_ERROR_FIELD_LIMIT
        ),
        message=sanitize_provider_error_text(
            error.get("message"), limit=PROVIDER_ERROR_MESSAGE_LIMIT
        ),
    )
    return diagnostic if any(vars(diagnostic).values()) else None


def provider_error_from_http_error(
    error: urllib.error.HTTPError,
) -> ProviderErrorDiagnostic | None:
    """有界读取 HTTP 400/422 错误体；其他状态不读取正文。"""
    if error.code not in {400, 422}:
        return None
    try:
        raw_body = error.read(PROVIDER_ERROR_BODY_LIMIT_BYTES + 1)
    except (OSError, ValueError):
        return None
    return parse_provider_error_diagnostic(raw_body)


def finish_reason_is_output_truncated(value: str | None) -> bool:
    """识别供应商表示生成预算耗尽的安全终止状态。"""
    return bool(
        isinstance(value, str)
        and value.strip().casefold() in OUTPUT_TRUNCATED_FINISH_REASONS
    )


def finish_reason_error(value: str | None) -> str | None:
    """把供应商安全终止状态映射为可审计错误，不读取响应正文。"""
    if finish_reason_is_output_truncated(value):
        return "output_truncated"
    normalized = value.strip().casefold() if isinstance(value, str) else ""
    if normalized == "content_filter":
        return "content_filtered"
    if normalized == "insufficient_system_resource":
        return "provider_resource_interrupted"
    return None


def parse_json_object_content(value: Any) -> dict[str, Any]:
    """只接受对象 JSON 或包裹整个响应的单一 json 代码块。"""
    if isinstance(value, dict):
        return value
    if value is None or (isinstance(value, str) and not value.strip(" \t\r\n\ufeff")):
        raise DeepSeekClientError("empty_content")
    if not isinstance(value, str):
        raise DeepSeekClientError("json_parse_failed")

    normalized = value.strip()
    if normalized.startswith("\ufeff"):
        normalized = normalized[1:].strip()
    try:
        parsed = json.loads(normalized)
    except json.JSONDecodeError:
        match = WHOLE_JSON_FENCE_PATTERN.fullmatch(normalized)
        if match is None:
            raise DeepSeekClientError("json_parse_failed") from None
        try:
            parsed = json.loads(match.group("body"))
        except json.JSONDecodeError:
            raise DeepSeekClientError("json_parse_failed") from None
    if not isinstance(parsed, dict):
        raise DeepSeekClientError("json_parse_failed")
    return parsed


def _optional_nonnegative_int(value: Any) -> tuple[int | None, bool]:
    if value is None:
        return None, True
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None, False
    return value, True


def parse_deepseek_usage(value: Any) -> DeepSeekUsage | None:
    """只提取安全的 usage 数值，并验证合计关系。"""
    if value is None:
        return None
    if not isinstance(value, dict):
        return DeepSeekUsage(None, None, None, None, None, False, False)
    fields = (
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "prompt_cache_hit_tokens",
        "prompt_cache_miss_tokens",
    )
    parsed: dict[str, int | None] = {}
    valid = True
    for field in fields:
        parsed[field], field_valid = _optional_nonnegative_int(value.get(field))
        valid = valid and field_valid
    input_tokens = parsed["prompt_tokens"]
    output_tokens = parsed["completion_tokens"]
    total_tokens = parsed["total_tokens"]
    cache_hit = parsed["prompt_cache_hit_tokens"]
    cache_miss = parsed["prompt_cache_miss_tokens"]
    if (
        input_tokens is not None
        and output_tokens is not None
        and total_tokens is not None
        and input_tokens + output_tokens != total_tokens
    ):
        valid = False
    if (
        input_tokens is not None
        and cache_hit is not None
        and cache_miss is not None
        and cache_hit + cache_miss != input_tokens
    ):
        valid = False
    complete = valid and all(
        item is not None
        for item in (input_tokens, output_tokens, total_tokens, cache_hit, cache_miss)
    )
    return DeepSeekUsage(
        input_tokens,
        output_tokens,
        total_tokens,
        cache_hit,
        cache_miss,
        valid,
        complete,
    )


def parse_deepseek_responses_usage(value: Any) -> DeepSeekUsage | None:
    """提取 Responses API usage，并还原现有账本需要的缓存命中/未命中数。"""
    if value is None:
        return None
    if not isinstance(value, dict):
        return DeepSeekUsage(None, None, None, None, None, False, False)

    input_tokens, input_valid = _optional_nonnegative_int(value.get("input_tokens"))
    output_tokens, output_valid = _optional_nonnegative_int(
        value.get("output_tokens")
    )
    total_tokens, total_valid = _optional_nonnegative_int(value.get("total_tokens"))
    details = value.get("input_tokens_details")
    if details is None:
        cached_tokens, cached_valid = None, True
    elif isinstance(details, dict):
        cached_tokens, cached_valid = _optional_nonnegative_int(
            details.get("cached_tokens")
        )
    else:
        cached_tokens, cached_valid = None, False

    cache_miss_tokens = (
        input_tokens - cached_tokens
        if input_tokens is not None
        and cached_tokens is not None
        and cached_tokens <= input_tokens
        else None
    )
    valid = input_valid and output_valid and total_valid and cached_valid
    if (
        input_tokens is not None
        and output_tokens is not None
        and total_tokens is not None
        and input_tokens + output_tokens != total_tokens
    ):
        valid = False
    if (
        input_tokens is not None
        and cached_tokens is not None
        and cached_tokens > input_tokens
    ):
        valid = False
    complete = valid and all(
        item is not None
        for item in (
            input_tokens,
            output_tokens,
            total_tokens,
            cached_tokens,
            cache_miss_tokens,
        )
    )
    return DeepSeekUsage(
        input_tokens,
        output_tokens,
        total_tokens,
        cached_tokens,
        cache_miss_tokens,
        valid,
        complete,
    )


def responses_endpoint_url(chat_completions_url: str) -> str | None:
    """从已锁定的 Chat Completions 地址推导同一服务的 Responses 地址。"""
    parsed = urllib.parse.urlparse(chat_completions_url)
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.params
        or parsed.query
        or parsed.fragment
        or not parsed.path.rstrip("/").endswith("/chat/completions")
    ):
        return None
    path = parsed.path.rstrip("/")
    prefix = path[: -len("/chat/completions")]
    return urllib.parse.urlunparse(parsed._replace(path=f"{prefix}/responses"))


class DeepSeekClient:
    """使用 Python 标准库调用 DeepSeek JSON 接口。"""

    def __init__(
        self,
        *,
        api_key: str | None,
        model: str,
        endpoint_url: str,
        timeout_seconds: float,
        max_retries: int,
        thinking_mode: str = "disabled",
        reasoning_effort: str | None = None,
        opener: Callable[..., Any] = urllib.request.urlopen,
    ) -> None:
        self._api_key = api_key or ""
        self.model = model.strip()
        self.endpoint_url = endpoint_url.strip()
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self.thinking_mode = thinking_mode
        self.reasoning_effort = reasoning_effort
        self._opener = opener

    def configuration_error(self) -> str | None:
        """返回首个配置错误；配置不完整时不会构造网络请求。"""
        if not self._api_key:
            return "missing_api_key"
        if not self.model:
            return "missing_model_name"
        parsed = urllib.parse.urlparse(self.endpoint_url)
        if parsed.scheme != "https" or not parsed.netloc:
            return "missing_base_url"
        if self.thinking_mode not in {"enabled", "disabled"}:
            return "api_request_failed"
        if self.thinking_mode == "enabled" and self.reasoning_effort not in {
            "high",
            "max",
        }:
            return "api_request_failed"
        if self.thinking_mode == "disabled" and self.reasoning_effort is not None:
            return "api_request_failed"
        if self.timeout_seconds <= 0:
            return "api_request_failed"
        if self.max_retries != 0:
            return "automatic_retry_forbidden"
        return None


    def request_json(
        self,
        messages: list[dict[str, str]],
        *,
        max_tokens: int,
        forced_tool: dict[str, Any] | None = None,
    ) -> DeepSeekCallResult:
        """请求一个 JSON 对象；每个付费逻辑调用最多发送一次 HTTP 请求。"""
        configuration_error = self.configuration_error()
        if configuration_error:
            return DeepSeekCallResult(False, None, configuration_error)

        forced_tool_name: str | None = None
        if forced_tool is not None:
            function = (
                forced_tool.get("function")
                if isinstance(forced_tool, dict)
                else None
            )
            forced_tool_name = (
                function.get("name")
                if isinstance(forced_tool, dict)
                and forced_tool.get("type") == "function"
                and isinstance(function, dict)
                and isinstance(function.get("name"), str)
                and function.get("name")
                else None
            )
            if forced_tool_name is None:
                return DeepSeekCallResult(False, None, "tool_contract_invalid")

        payload = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "thinking": {"type": self.thinking_mode},
            "max_tokens": max_tokens,
        }
        if forced_tool_name is not None:
            # 关键逻辑：Round 2只允许一个明确命名的结构化结果出口。
            payload["tools"] = [forced_tool]
            payload["tool_choice"] = {
                "type": "function",
                "function": {"name": forced_tool_name},
        }
        if self.thinking_mode == "enabled":
            # 思考模式不叠加 response_format：Round 1由提示词约束 content，
            # 命名工具调用由本地解析器严格验证。
            payload["reasoning_effort"] = self.reasoning_effort
        else:
            payload["response_format"] = {"type": "json_object"}
            # 官方说明思考模式会忽略 temperature，因此只在非思考模式发送。
            payload["temperature"] = 0.1

        started_at = datetime.now().astimezone()
        started_tick = time.perf_counter()
        try:
            response = self._post_json(
                payload, expected_tool_name=forced_tool_name
            )
        except DeepSeekClientError as exc:
            finished_at = datetime.now().astimezone()
            attempt = DeepSeekAttemptResult(
                attempt_no=1,
                status=exc.error_type,
                error_type=exc.error_type,
                http_status=exc.http_status,
                model_returned=exc.model_returned,
                response_id=exc.response_id,
                usage=exc.usage,
                finish_reason=exc.finish_reason,
                provider_error=exc.provider_error,
                started_at=started_at.isoformat(timespec="milliseconds"),
                finished_at=finished_at.isoformat(timespec="milliseconds"),
                duration_ms=max(
                    0, round((time.perf_counter() - started_tick) * 1000)
                ),
            )
            return DeepSeekCallResult(False, None, exc.error_type, (attempt,))

        finished_at = datetime.now().astimezone()
        attempt = DeepSeekAttemptResult(
            attempt_no=1,
            status="success",
            error_type=None,
            http_status=response.http_status,
            model_returned=response.model_returned,
            response_id=response.response_id,
            usage=response.usage,
            started_at=started_at.isoformat(timespec="milliseconds"),
            finished_at=finished_at.isoformat(timespec="milliseconds"),
            duration_ms=max(0, round((time.perf_counter() - started_tick) * 1000)),
            finish_reason=response.finish_reason,
        )
        return DeepSeekCallResult(True, response.data, None, (attempt,))

    def request_responses_json(
        self,
        messages: list[dict[str, str]],
        *,
        max_tokens: int,
        forced_tool: dict[str, Any],
    ) -> DeepSeekCallResult:
        """通过 Responses API 的单一命名函数取得 JSON；最多一次 HTTP 请求。"""
        configuration_error = self.configuration_error()
        endpoint_url = responses_endpoint_url(self.endpoint_url)
        if configuration_error or endpoint_url is None:
            return DeepSeekCallResult(
                False, None, configuration_error or "responses_endpoint_invalid"
            )
        tool_name = (
            forced_tool.get("name")
            if isinstance(forced_tool, dict)
            and forced_tool.get("type") == "function"
            and isinstance(forced_tool.get("name"), str)
            and forced_tool.get("name")
            and isinstance(forced_tool.get("parameters"), dict)
            else None
        )
        if tool_name is None:
            return DeepSeekCallResult(False, None, "tool_contract_invalid")

        payload: dict[str, Any] = {
            "model": self.model,
            "input": messages,
            "stream": False,
            "max_output_tokens": max_tokens,
            "tools": [forced_tool],
        }
        if self.thinking_mode == "enabled":
            # DeepSeek Responses 的思考模式不接受强制 tool_choice；保留唯一工具，
            # 并由本地解析器严格要求返回该命名函数，避免把普通文本当作结果。
            payload["reasoning"] = {"effort": self.reasoning_effort}

        started_at = datetime.now().astimezone()
        started_tick = time.perf_counter()
        try:
            response = self._post_responses_json(
                payload, endpoint_url=endpoint_url, expected_tool_name=tool_name
            )
        except DeepSeekClientError as exc:
            finished_at = datetime.now().astimezone()
            attempt = DeepSeekAttemptResult(
                attempt_no=1,
                status=exc.error_type,
                error_type=exc.error_type,
                http_status=exc.http_status,
                model_returned=exc.model_returned,
                response_id=exc.response_id,
                usage=exc.usage,
                finish_reason=exc.finish_reason,
                provider_error=exc.provider_error,
                started_at=started_at.isoformat(timespec="milliseconds"),
                finished_at=finished_at.isoformat(timespec="milliseconds"),
                duration_ms=max(
                    0, round((time.perf_counter() - started_tick) * 1000)
                ),
            )
            return DeepSeekCallResult(False, None, exc.error_type, (attempt,))

        finished_at = datetime.now().astimezone()
        attempt = DeepSeekAttemptResult(
            attempt_no=1,
            status="success",
            error_type=None,
            http_status=response.http_status,
            model_returned=response.model_returned,
            response_id=response.response_id,
            usage=response.usage,
            finish_reason=response.finish_reason,
            started_at=started_at.isoformat(timespec="milliseconds"),
            finished_at=finished_at.isoformat(timespec="milliseconds"),
            duration_ms=max(0, round((time.perf_counter() - started_tick) * 1000)),
        )
        return DeepSeekCallResult(True, response.data, None, (attempt,))

    def _post_responses_json(
        self,
        payload: dict[str, Any],
        *,
        endpoint_url: str,
        expected_tool_name: str,
    ) -> _ParsedDeepSeekResponse:
        """解析 Responses API；只接受唯一且名称匹配的 function_call。"""
        response_object = self._read_http_json(payload, endpoint_url=endpoint_url)
        model_returned = (
            response_object.get("model")
            if isinstance(response_object.get("model"), str)
            else None
        )
        response_id = (
            response_object.get("id")
            if isinstance(response_object.get("id"), str)
            else None
        )
        usage = parse_deepseek_responses_usage(response_object.get("usage"))
        status = (
            response_object.get("status").strip().casefold()
            if isinstance(response_object.get("status"), str)
            else ""
        )
        if status != "completed":
            error_type = "responses_not_completed"
            details = response_object.get("incomplete_details")
            reason = (
                details.get("reason").strip().casefold()
                if isinstance(details, dict)
                and isinstance(details.get("reason"), str)
                else ""
            )
            if reason in {"max_output_tokens", "max_tokens"}:
                error_type = "output_truncated"
            elif reason == "content_filter":
                error_type = "content_filtered"
            raise DeepSeekClientError(
                error_type,
                http_status=200,
                model_returned=model_returned,
                response_id=response_id,
                usage=usage,
                finish_reason=status or None,
            )

        output = response_object.get("output")
        function_calls = (
            [
                item
                for item in output
                if isinstance(item, dict) and item.get("type") == "function_call"
            ]
            if isinstance(output, list)
            else []
        )
        try:
            if len(function_calls) != 1:
                raise DeepSeekClientError("required_tool_call_invalid")
            function_call = function_calls[0]
            if function_call.get("name") != expected_tool_name:
                raise DeepSeekClientError("required_tool_call_invalid")
            call_status = function_call.get("status")
            if call_status is not None and call_status != "completed":
                raise DeepSeekClientError("required_tool_call_invalid")
            parsed_content = parse_json_object_content(function_call.get("arguments"))
        except DeepSeekClientError as exc:
            raise DeepSeekClientError(
                exc.error_type,
                http_status=200,
                model_returned=model_returned,
                response_id=response_id,
                usage=usage,
                finish_reason=status,
            ) from None

        return _ParsedDeepSeekResponse(
            parsed_content,
            200,
            model_returned,
            response_id,
            usage,
            status,
        )

    def _read_http_json(
        self, payload: dict[str, Any], *, endpoint_url: str
    ) -> dict[str, Any]:
        """发送一次请求并返回对象 JSON；任何异常都不携带响应正文。"""
        request_body = json.dumps(
            payload, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        request = urllib.request.Request(
            endpoint_url,
            data=request_body,
            method="POST",
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json; charset=utf-8",
                "Accept": "application/json",
                "User-Agent": "arXivKaleid/2.0",
            },
        )
        try:
            with self._opener(request, timeout=self.timeout_seconds) as response:
                status = getattr(response, "status", None)
                if status is None:
                    status = response.getcode()
                if status != 200:
                    raise DeepSeekClientError(
                        "api_request_failed", http_status=int(status)
                    )
                raw_response = response.read()
        except urllib.error.HTTPError as exc:
            if exc.code in {401, 403}:
                raise DeepSeekClientError("auth_failed", http_status=exc.code) from None
            raise DeepSeekClientError(
                "api_request_failed",
                http_status=exc.code,
                provider_error=provider_error_from_http_error(exc),
            ) from None
        except (TimeoutError, socket.timeout):
            raise DeepSeekClientError("timeout") from None
        except (urllib.error.URLError, OSError):
            raise DeepSeekClientError("api_request_failed") from None

        try:
            response_object = json.loads(raw_response.decode("utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise DeepSeekClientError("json_parse_failed") from None
        if not isinstance(response_object, dict):
            raise DeepSeekClientError("json_parse_failed")
        return response_object

    def _post_json(
        self,
        payload: dict[str, Any],
        *,
        expected_tool_name: str | None = None,
    ) -> _ParsedDeepSeekResponse:
        """关键逻辑：请求头只在内存中构造，异常中不包含密钥或响应正文。"""
        request_body = json.dumps(
            payload, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        request = urllib.request.Request(
            self.endpoint_url,
            data=request_body,
            method="POST",
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json; charset=utf-8",
                "Accept": "application/json",
                "User-Agent": "arXivKaleid/2.0",
            },
        )

        try:
            with self._opener(request, timeout=self.timeout_seconds) as response:
                status = getattr(response, "status", None)
                if status is None:
                    status = response.getcode()
                if status != 200:
                    raise DeepSeekClientError(
                        "api_request_failed", http_status=int(status)
                    )
                raw_response = response.read()
        except urllib.error.HTTPError as exc:
            if exc.code in {401, 403}:
                raise DeepSeekClientError(
                    "auth_failed", http_status=exc.code
                ) from None
            raise DeepSeekClientError(
                "api_request_failed",
                http_status=exc.code,
                provider_error=provider_error_from_http_error(exc),
            ) from None
        except (TimeoutError, socket.timeout):
            raise DeepSeekClientError("timeout") from None
        except (urllib.error.URLError, OSError):
            raise DeepSeekClientError("api_request_failed") from None

        try:
            response_object = json.loads(raw_response.decode("utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise DeepSeekClientError("json_parse_failed") from None
        if not isinstance(response_object, dict):
            raise DeepSeekClientError("json_parse_failed")

        model_returned = (
            response_object.get("model")
            if isinstance(response_object.get("model"), str)
            else None
        )
        response_id = (
            response_object.get("id")
            if isinstance(response_object.get("id"), str)
            else None
        )
        usage = parse_deepseek_usage(response_object.get("usage"))
        choices = response_object.get("choices")
        first_choice = (
            choices[0]
            if isinstance(choices, list)
            and choices
            and isinstance(choices[0], dict)
            else None
        )
        finish_reason = (
            first_choice.get("finish_reason")
            if isinstance(first_choice, dict)
            and isinstance(first_choice.get("finish_reason"), str)
            else None
        )
        terminal_error = finish_reason_error(finish_reason)
        if terminal_error is not None:
            raise DeepSeekClientError(
                terminal_error,
                http_status=200,
                model_returned=model_returned,
                response_id=response_id,
                usage=usage,
                finish_reason=finish_reason,
            )
        try:
            message = first_choice["message"]
            if expected_tool_name is None:
                if str(finish_reason or "").casefold() == "tool_calls":
                    raise DeepSeekClientError("unexpected_tool_call")
                parsed_content = parse_json_object_content(message["content"])
            else:
                if str(finish_reason or "").casefold() != "tool_calls":
                    # 仍把供应商返回的空正式输出记为 empty_content，保持既有
                    # 受控恢复身份可审计；非空 content 则明确视为工具契约缺失。
                    try:
                        parse_json_object_content(message.get("content"))
                    except DeepSeekClientError as exc:
                        if exc.error_type == "empty_content":
                            raise
                    raise DeepSeekClientError("required_tool_call_missing")
                tool_calls = message["tool_calls"]
                if not isinstance(tool_calls, list) or len(tool_calls) != 1:
                    raise DeepSeekClientError("required_tool_call_invalid")
                tool_call = tool_calls[0]
                function = (
                    tool_call.get("function")
                    if isinstance(tool_call, dict)
                    and tool_call.get("type") == "function"
                    else None
                )
                if (
                    not isinstance(function, dict)
                    or function.get("name") != expected_tool_name
                ):
                    raise DeepSeekClientError("required_tool_call_invalid")
                parsed_content = parse_json_object_content(function.get("arguments"))
        except (KeyError, IndexError, TypeError, DeepSeekClientError) as exc:
            error_type = (
                exc.error_type
                if isinstance(exc, DeepSeekClientError)
                else "json_parse_failed"
            )
            raise DeepSeekClientError(
                error_type,
                http_status=200,
                model_returned=model_returned,
                response_id=response_id,
                usage=usage,
                finish_reason=finish_reason,
            ) from None

        return _ParsedDeepSeekResponse(
            parsed_content,
            200,
            model_returned,
            response_id,
            usage,
            finish_reason,
        )
