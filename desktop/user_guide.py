# Copyright (c) 2026 yyyang20
# SPDX-License-Identifier: GPL-3.0-only
# See LICENSE in the project root for the full license text.

"""内置使用说明；与模型输入及用户保存数据完全分离。"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QColor, QTextCharFormat, QTextCursor
from PySide6.QtWidgets import QTextBrowser, QVBoxLayout, QWidget
from qfluentwidgets import TitleLabel

from desktop import paths
from desktop.style import ACCENT, PAGE_STYLE, TEXT_PRIMARY, SurfaceCard
from desktop.task_panel import wrapping_label

GUIDE_RESOURCE = "docs/desktop/USER_GUIDE.md"
GUIDE_ROUTES = {
    "arxivkaleid://home": "home_page",
    "arxivkaleid://prompts": "prompt_page",
    "arxivkaleid://history": "history_page",
    "arxivkaleid://settings": "settings_page",
}


class UserGuideError(RuntimeError):
    pass


def load_user_guide(source_root: Path) -> tuple[str, str, str]:
    """源码与冻结程序使用同一正文；冻结资源绑定构建哈希，不回退外部目录。"""
    try:
        root = paths.resource_root(source_root)
        data = paths.checked_path(root, GUIDE_RESOURCE).read_bytes()
        if paths.frozen():
            info = paths.checked_path(paths.application_root(source_root), "BUILD_INFO.json")
            identity = json.loads(info.read_text(encoding="utf-8"))
            expected = identity["resource_hashes"][GUIDE_RESOURCE]
            if hashlib.sha256(data).hexdigest() != expected:
                raise ValueError
        text = data.decode("utf-8").replace("\r\n", "\n")
        heading, _, rest = text.partition("\n")
        introduction, _, body = rest.strip().partition("\n\n")
        if not heading.startswith("# ") or not introduction or not body.startswith("## "):
            raise ValueError
        return heading[2:].strip(), introduction, body
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        raise UserGuideError("user_guide_unavailable") from None


class GuideBrowser(QTextBrowser):
    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() == QEvent.Type.FontChange:
            # Markdown 文档跟随控件字体，避免高 DPI 或较大字体只改变外框。
            self.document().setDefaultFont(self.font())

    def loadResource(self, resource_type, name):
        # 说明只渲染随包文字，不加载文件、远程图片或其他隐式资源。
        return None


class UserGuidePage(QWidget):
    def __init__(self, source_root: Path, parent=None):
        super().__init__(parent)
        self.setObjectName("userGuidePage")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 18, 24, 18)
        layout.setSpacing(12)
        self.title = TitleLabel("使用说明")
        self.introduction = wrapping_label()
        layout.addWidget(self.title)
        layout.addWidget(self.introduction)
        self.card = SurfaceCard()
        content = QVBoxLayout(self.card)
        content.setContentsMargins(20, 16, 20, 16)
        self.browser = GuideBrowser()
        self.browser.setReadOnly(True)
        self.browser.setOpenLinks(False)
        self.browser.setOpenExternalLinks(False)
        self.browser.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.browser.setStyleSheet(
            f"QTextBrowser {{background: transparent; border: none; color: {TEXT_PRIMARY};"
            "font-family: 'Microsoft YaHei UI';}"
        )
        self.browser.document().setDefaultStyleSheet(
            f"a {{color: {ACCENT}; text-decoration: underline;}}"
            "h2 {margin-top: 18px; margin-bottom: 10px;} p, li {line-height: 140%;}"
        )
        self.browser.setAccessibleName("使用说明正文")
        content.addWidget(self.browser)
        layout.addWidget(self.card, 1)
        self.setStyleSheet(PAGE_STYLE)
        self.loaded = False
        try:
            title, introduction, body = load_user_guide(source_root)
            self.title.setText(title)
            self.introduction.setText(introduction)
            self.browser.setMarkdown(body)
            # Qt Markdown 的链接格式不完全遵循 CSS；显式使用现有强调色。
            block = self.browser.document().begin()
            while block.isValid():
                iterator = block.begin()
                while not iterator.atEnd():
                    fragment = iterator.fragment()
                    if fragment.isValid() and fragment.charFormat().isAnchor():
                        cursor = QTextCursor(self.browser.document())
                        cursor.setPosition(fragment.position())
                        cursor.setPosition(fragment.position() + fragment.length(), QTextCursor.MoveMode.KeepAnchor)
                        style = QTextCharFormat()
                        style.setForeground(QColor(ACCENT))
                        cursor.mergeCharFormat(style)
                    iterator += 1
                block = block.next()
            self.loaded = True
        except UserGuideError:
            self.introduction.setText("使用说明暂时无法加载。")
            self.browser.setPlainText("使用说明资源缺失或校验失败，请重新完整解压程序。")
