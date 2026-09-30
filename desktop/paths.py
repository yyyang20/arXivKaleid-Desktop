"""Desktop 的资源与运行目录边界，不改变共享 CLI 的项目根定义。"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import zoneinfo


def frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def application_root(source_root: Path) -> Path:
    return Path(sys.executable).resolve().parent if frozen() else source_root.resolve()


def checked_path(root: Path, *parts: str) -> Path:
    """拒绝路径逃逸及所有目录链接/reparse point，包括指向边界内部的链接。"""
    root = root.resolve()
    for part in parts:
        p = Path(part)
        if p.is_absolute() or p.drive or ".." in p.parts or ":" in part:
            raise ValueError("Desktop 路径无效。")
    target = root.joinpath(*parts)
    if not target.resolve().is_relative_to(root):
        raise ValueError("Desktop 路径超出运行边界。")
    current = root
    for part in target.relative_to(root).parts:
        current = current / part
        if current.resolve() != current:
            raise ValueError("Desktop 路径不能包含目录链接。")
        try:
            info = current.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError("Desktop 路径不能包含目录链接。")
    if target.resolve() != target:
        raise ValueError("Desktop 路径不能包含目录链接。")
    return target


def resource_root(source_root: Path) -> Path:
    if not frozen():
        return source_root.resolve()
    app = application_root(source_root)
    bundled = checked_path(app, "_internal")
    if Path(getattr(sys, "_MEIPASS", "")).resolve() != bundled:
        raise ValueError("Desktop 资源目录无效。")
    return bundled


def runtime_path(source_root: Path, *parts: str) -> Path:
    name = "runtime" if frozen() else ".desktop-runtime"
    return checked_path(application_root(source_root), name, *parts)


def prepare_runtime(source_root: Path) -> None:
    """启动即核验可写目录；失败不回退到用户目录，探针只在 runtime 中。"""
    for name in ("config", "work", "cache/arxiv", "pdfs", "logs"):
        runtime_path(source_root, name).mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryFile(dir=runtime_path(source_root, "work")) as stream:
        stream.write(b"portable-write-check")
        stream.flush()
        os.fsync(stream.fileno())


def configure_timezone(source_root: Path) -> None:
    if frozen():
        root = resource_root(source_root)
        zoneinfo.reset_tzpath([str(checked_path(root, "zoneinfo"))])
        zoneinfo.ZoneInfo.clear_cache()
    zoneinfo.ZoneInfo("Asia/Shanghai")


def curl_executable(source_root: Path) -> str:
    if not frozen():
        return "curl"
    # 只读取发行物内的校验元数据，缺失或损坏时绝不搜索 PATH。
    root = resource_root(source_root)
    manifest = json.loads(checked_path(root, "vendor/curl/files.json").read_text(encoding="utf-8"))
    if "bin/curl.exe" not in manifest:
        raise ValueError("Desktop bundled curl 无效。")
    for relative, digest in manifest.items():
        path = checked_path(root, "vendor/curl", relative)
        if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise ValueError("Desktop bundled curl 校验失败。")
    return str(checked_path(root, "vendor/curl/bin/curl.exe"))
