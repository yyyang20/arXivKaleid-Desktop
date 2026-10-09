# Copyright (c) 2026 yyyang20
# SPDX-License-Identifier: GPL-3.0-only
# See LICENSE in the project root for the full license text.

"""两轮完整研究 Prompt：复用保存层，只在当前运行根持久化。"""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import tempfile

from desktop import paths, pipeline
from desktop.config import load_config


FORMAT_VERSION = "research_prompt_v1"
MAX_CHARACTERS = 10_000
ROUNDS = ("round1", "round2")


class RequirementsError(RuntimeError):
    """错误文本只描述原因，绝不包含用户正文或文件内容。"""


def normalize(text: str) -> str:
    if not isinstance(text, str):
        raise RequirementsError("研究提示词必须是纯文本。")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if not text.strip() or "\x00" in text or len(text) > MAX_CHARACTERS:
        raise RequirementsError("研究提示词不能为空、含 NUL 或超过 10,000 字符。")
    try:
        text.encode("utf-8")
    except UnicodeError:
        raise RequirementsError("研究提示词包含无法保存的字符。") from None
    return text


@dataclass(frozen=True)
class SavedRequirements:
    text: str
    custom: bool


@dataclass(frozen=True)
class RequirementsSnapshot:
    round1: str
    round2: str


class RequirementsStore:
    def __init__(self, source_root: Path | None = None):
        self.source_root = (source_root if source_root is not None else pipeline.PROJECT_ROOT).resolve()

    def _path(self, stage: str) -> Path:
        if stage not in ROUNDS:
            raise RequirementsError("研究提示词轮次无效。")
        return paths.runtime_path(self.source_root, "config", f"{stage}_research_prompt.json")

    def default(self, stage: str) -> str:
        self._path(stage)
        root = paths.resource_root(self.source_root)
        config = load_config(paths.checked_path(root, "config.json"))
        return normalize(paths.checked_path(root, config["paths"][f"{stage}_research_prompt"]).read_text(encoding="utf-8"))

    def load(self, stage: str) -> SavedRequirements:
        default = self.default(stage)
        try:
            path = self._path(stage)
            if not path.exists():
                return SavedRequirements(default, False)
            # 文件有界读取，未知格式不覆盖、不静默回退。
            if path.stat().st_size > 80_000:
                raise ValueError
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or set(data) != {"format_version", "research_prompt"}:
                raise ValueError
            if data["format_version"] != FORMAT_VERSION:
                raise ValueError
            text = data["research_prompt"]
            if text is None:
                return SavedRequirements(default, False)
            return SavedRequirements(normalize(text), True)
        except (OSError, UnicodeError, ValueError, RequirementsError):
            raise RequirementsError("本轮已保存的研究提示词损坏或格式未知；请确认恢复默认后再分析。") from None

    def save(self, stage: str, text: str | None) -> SavedRequirements:
        # 先验证默认资源，避免损坏资源下写入或错误宣称已生效。
        default = self.default(stage)
        value = normalize(text) if text is not None else None
        temporary = None
        try:
            target = self._path(stage)
            target.parent.mkdir(parents=True, exist_ok=True)
            payload = json.dumps({"format_version": FORMAT_VERSION, "research_prompt": value}, ensure_ascii=False)
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n", dir=target.parent, prefix=f".{stage}-", delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(payload + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            # 替换前再次核验，拒绝写到链接或其他运行根。
            target = self._path(stage)
            os.replace(temporary, target)
            temporary = None
        except (OSError, ValueError):
            raise RequirementsError("研究提示词保存失败；原来的生效内容未改变，请检查目录权限和磁盘空间。") from None
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass  # 保存失败仍报告原错误，不把临时文件路径或正文带入界面。
        return SavedRequirements(value if value is not None else default, value is not None)

    def snapshot(self) -> RequirementsSnapshot:
        return RequirementsSnapshot(self.load("round1").text, self.load("round2").text)
