# Copyright (c) 2026 yyyang20
# SPDX-License-Identifier: GPL-3.0-only
"""用户分类选择：只在受控运行根保存，不修改发行配置。"""
import json
import os
from pathlib import Path
import tempfile

from desktop import paths
from desktop.categories import DEFAULT_CATEGORIES, validate_categories

FORMAT_VERSION = "category_selection_v1"


class CategorySelectionError(RuntimeError):
    """固定安全信息，不包含原文件内容或绝对路径。"""


class CategorySelectionStore:
    def __init__(self, source_root: Path):
        self.source_root = source_root.resolve()

    @property
    def file(self):
        return paths.runtime_path(self.source_root, "config", "category_selection.json")

    def load(self) -> tuple[str, ...]:
        try:
            target = self.file
            if not target.exists():
                return DEFAULT_CATEGORIES
            if target.stat().st_size > 4096:
                raise ValueError
            data = json.loads(target.read_text(encoding="utf-8"))
            if (not isinstance(data, dict) or set(data) != {"format_version", "categories"}
                    or data["format_version"] != FORMAT_VERSION or not isinstance(data["categories"], list)):
                raise ValueError
            return validate_categories(data["categories"])
        except (OSError, UnicodeError, ValueError):
            raise CategorySelectionError("已保存的分类损坏或格式未知；请修改并保存分类，或确认恢复默认后再抓取。") from None

    def save(self, categories) -> tuple[str, ...]:
        try:
            selected = validate_categories(categories)
        except ValueError:
            raise CategorySelectionError("请选择 1–3 个不重复的 Physics 具体分类。") from None
        temporary = None
        try:
            target = self.file
            target.parent.mkdir(parents=True, exist_ok=True)
            payload = {"format_version": FORMAT_VERSION, "categories": list(selected)}
            # 临时文件与目标同目录；替换成功前不改变正在生效的选择。
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n",
                    dir=target.parent, prefix=".categories-", delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(json.dumps(payload, ensure_ascii=False) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.file)
            temporary = None
        except (OSError, ValueError):
            raise CategorySelectionError("分类保存失败；原来的生效选择未改变，请检查运行目录权限和磁盘空间。") from None
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass
        return selected
