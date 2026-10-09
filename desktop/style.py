# Copyright (c) 2026 yyyang20
# SPDX-License-Identifier: GPL-3.0-only
# See LICENSE in the project root for the full license text.

"""alpha 7 的共享视觉常量与低对比度卡片。"""
from __future__ import annotations

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPen
from qfluentwidgets import CardWidget


PAGE_BACKGROUND = "#eef3f8"
CARD_BACKGROUND = "#f7fafd"
CARD_BORDER = "#d5e0eb"
SUBTLE_BACKGROUND = "#eef4f9"
SUBTLE_BORDER = "#d8e2ec"
TEXT_PRIMARY = "#17243b"
TEXT_SECONDARY = "#5f6f82"
ACCENT = "#1677ff"


class SurfaceCard(CardWidget):
    """固定浅灰蓝表面，避免 Fluent 默认白色卡片形成视觉断层。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._surface = QColor(CARD_BACKGROUND)
        self._outline = QColor(CARD_BORDER)
        self.setBorderRadius(8)

    def set_surface(self, background: str, border: str) -> None:
        self._surface = QColor(background)
        self._outline = QColor(border)
        self.update()

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setBrush(self._surface)
        painter.setPen(QPen(self._outline, 1))
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        painter.drawRoundedRect(rect, self.borderRadius, self.borderRadius)


PAGE_STYLE = f"""
QWidget#homePage, QWidget#historyPage, QWidget#settingsContent, QWidget#userGuidePage {{
    background: {PAGE_BACKGROUND};
    color: {TEXT_PRIMARY};
}}
QScrollArea#settingsPage {{
    background: {PAGE_BACKGROUND};
    border: none;
}}
"""


API_KEY_STYLE = f"""
QLineEdit#apiKeyInput {{
    min-height: 36px;
    padding: 0 10px;
    color: {TEXT_PRIMARY};
    background: {SUBTLE_BACKGROUND};
    border: 1px solid #cbd8e5;
    border-radius: 6px;
}}
QLineEdit#apiKeyInput:hover {{
    border-color: #b7c8d9;
}}
QLineEdit#apiKeyInput:focus {{
    background: #f4f8fc;
    border: 1px solid {ACCENT};
}}
QLineEdit#apiKeyInput:disabled {{
    color: #8491a2;
    background: #e8eef4;
    border-color: #d6e0e9;
}}
"""
