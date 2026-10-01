"""arXiv curl 传输证据的纯解析与白名单边界。"""
from __future__ import annotations

import ipaddress
import json
import re
from pathlib import Path
from typing import Collection


HEADER_LIMIT_BYTES = 65536
HEADER_VALUE_LIMIT = 256
HEADER_VALUE_COUNT_LIMIT = 4
RESPONSE_HEADER_ALLOWLIST = (
    "date",
    "server",
    "via",
    "age",
    "content-type",
    "content-length",
    "retry-after",
    "x-cache",
    "x-cache-hits",
    "x-served-by",
    "x-timer",
    "cf-ray",
)
CURL_WRITE_OUT = (
    '{"http_status":"%{response_code}","remote_ip":"%{remote_ip}",'
    '"remote_port":"%{remote_port}","http_version":"%{http_version}",'
    '"ssl_verify_result":"%{ssl_verify_result}","num_redirects":"%{num_redirects}",'
    '"time_namelookup":"%{time_namelookup}","time_connect":"%{time_connect}",'
    '"time_appconnect":"%{time_appconnect}","time_starttransfer":"%{time_starttransfer}",'
    '"time_total":"%{time_total}"}'
)


def parse_response_headers(
    path: Path,
    *,
    allowlist: Collection[str] = RESPONSE_HEADER_ALLOWLIST,
    header_limit_bytes: int = HEADER_LIMIT_BYTES,
    header_value_limit: int = HEADER_VALUE_LIMIT,
    header_value_count_limit: int = HEADER_VALUE_COUNT_LIMIT,
) -> tuple[str, dict[str, list[str]], list[str]]:
    """只从最后一个 HTTP 头块提取白名单 ASCII 字段。"""
    try:
        size = path.stat().st_size
        if size > header_limit_bytes:
            return "oversize", {}, []
        raw = path.read_bytes()
    except OSError:
        return "unavailable", {}, []
    blocks = [block for block in raw.replace(b"\r\n", b"\n").split(b"\n\n") if block]
    candidates = [block for block in blocks if block.split(b"\n", 1)[0].startswith(b"HTTP/")]
    if not candidates:
        return "invalid", {}, []
    headers: dict[str, list[str]] = {}
    rejected: set[str] = set()
    allowed = set(allowlist)
    for line in candidates[-1].split(b"\n")[1:]:
        if not line or b":" not in line:
            continue
        name_raw, value_raw = line.split(b":", 1)
        try:
            name = name_raw.decode("ascii").strip().lower()
        except UnicodeError:
            continue
        if name not in allowed:
            continue
        value_raw = value_raw.strip()
        if (
            not value_raw
            or len(value_raw) > header_value_limit
            or any(byte < 0x20 or byte > 0x7E for byte in value_raw)
        ):
            rejected.add(name)
            continue
        values = headers.setdefault(name, [])
        if len(values) >= header_value_count_limit:
            rejected.add(name)
            continue
        values.append(value_raw.decode("ascii"))
    return "captured", headers, sorted(rejected)


def safe_float(value: object, *, timeout_seconds: float) -> float | None:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", value):
        return None
    try:
        result = float(value)
    except ValueError:
        return None
    if not 0 <= result <= timeout_seconds + 15:
        return None
    return round(result, 6)


def parse_write_out(payload: bytes, *, timeout_seconds: float = 90) -> dict | None:
    """只接受完整匹配的固定 curl write-out schema，并校验每个值。"""
    try:
        data = json.loads(payload.decode("ascii"))
    except (UnicodeError, ValueError, TypeError):
        return None
    expected = {
        "http_status",
        "remote_ip",
        "remote_port",
        "http_version",
        "ssl_verify_result",
        "num_redirects",
        "time_namelookup",
        "time_connect",
        "time_appconnect",
        "time_starttransfer",
        "time_total",
    }
    if not isinstance(data, dict) or set(data) != expected:
        return None
    status_text = data["http_status"]
    if not isinstance(status_text, str) or not re.fullmatch(r"[0-9]{3}", status_text):
        return None
    status = int(status_text)
    if not (status == 0 or 100 <= status <= 599):
        return None
    remote_ip = None
    remote_ip_version = None
    if isinstance(data["remote_ip"], str) and data["remote_ip"]:
        try:
            parsed_ip = ipaddress.ip_address(data["remote_ip"])
            remote_ip = str(parsed_ip)
            remote_ip_version = parsed_ip.version
        except ValueError:
            pass
    remote_port_text = data["remote_port"]
    remote_port = (
        int(remote_port_text)
        if isinstance(remote_port_text, str)
        and re.fullmatch(r"[0-9]{1,5}", remote_port_text)
        and 0 <= int(remote_port_text) <= 65535
        else None
    )
    http_version = data["http_version"]
    if not isinstance(http_version, str) or not re.fullmatch(
        r"(?:0|1\.0|1\.1|2|3)", http_version
    ):
        http_version = "unavailable"
    ssl_text = data["ssl_verify_result"]
    ssl_verify = (
        int(ssl_text)
        if isinstance(ssl_text, str)
        and re.fullmatch(r"[0-9]{1,10}", ssl_text)
        and 0 <= int(ssl_text) <= 2**31 - 1
        else None
    )
    redirects_text = data["num_redirects"]
    redirects = (
        int(redirects_text)
        if isinstance(redirects_text, str)
        and re.fullmatch(r"[0-9]{1,2}", redirects_text)
        and 0 <= int(redirects_text) <= 10
        else None
    )
    timings = {
        key: safe_float(data[key], timeout_seconds=timeout_seconds)
        for key in (
            "time_namelookup",
            "time_connect",
            "time_appconnect",
            "time_starttransfer",
            "time_total",
        )
    }
    return {
        "http_status": status,
        "remote_ip": remote_ip,
        "remote_ip_version": remote_ip_version,
        "remote_port": remote_port,
        "http_version": http_version,
        "ssl_verify_result": ssl_verify,
        "num_redirects": redirects,
        "timings_seconds": timings,
    }
