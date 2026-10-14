# Copyright (c) 2026 yyyang20
# SPDX-License-Identifier: GPL-3.0-only
# See LICENSE in the project root for the full license text.

"""独立页面只负责布局；业务操作由主窗口连接。"""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFormLayout, QHBoxLayout, QLabel, QSizePolicy, QStackedWidget, QTextBrowser, QVBoxLayout, QWidget,
)
from qfluentwidgets import (
    BodyLabel, FluentIcon, PasswordLineEdit, PrimaryPushButton,
    ScrollArea, SubtitleLabel, TitleLabel, ToolButton, TransparentToolButton,
)
from desktop import __version__
from desktop.style import (
    API_KEY_STYLE, PAGE_STYLE, TEXT_PRIMARY, TEXT_SECONDARY, SurfaceCard,
)
from desktop.task_panel import TaskPanel, wrapping_label
from desktop.date_picker import DatePickerPopup
from desktop.history_page import HistoryPage


def card(title, parent=None):
    widget = SurfaceCard(parent)
    layout = QVBoxLayout(widget)
    layout.setContentsMargins(20, 16, 20, 16)
    layout.setSpacing(10)
    layout.addWidget(SubtitleLabel(title))
    return widget, layout


class HomePage(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("homePage")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 18, 24, 18)
        layout.setSpacing(12)
        fetch_card, fetch = card("抓取信息")
        # 分类常驻后紧凑排列日期与统计，仍按字体的 sizeHint 保住操作和日报空间。
        fetch.setContentsMargins(16, 10, 16, 10)
        fetch.setSpacing(4)
        self.statistics = {}
        dates = QHBoxLayout()
        for name in ("抓取日期（北京时间）", "抓取完成时间（北京时间）"):
            label = wrapping_label("—")
            self.statistics[name] = label
            form = QFormLayout()
            form.addRow(name, label)
            dates.addLayout(form, 1)
        fetch.addLayout(dates)
        category_card = QWidget()
        category_card.setObjectName("categoryCard")
        categories = QVBoxLayout(category_card)
        categories.setContentsMargins(0, 0, 0, 0)
        categories.setSpacing(3)
        category_header = QHBoxLayout()
        category_header.addWidget(BodyLabel("抓取分类"))
        self.category_label = wrapping_label()
        self.category_label.setObjectName("categorySelection")
        category_header.addWidget(self.category_label, 1)
        # 分类文字优先使用行内空间，次要操作使用现有 Fluent 无边框图标。
        self.modify_categories_button = TransparentToolButton(FluentIcon.EDIT)
        self.restore_categories_button = TransparentToolButton(FluentIcon.SYNC)
        for button, description in ((self.modify_categories_button, "修改抓取分类"),
                                    (self.restore_categories_button, "恢复默认抓取分类")):
            button.setToolTip(description)
            button.setAccessibleName(description)
            button.setFixedSize(28, 28)
        category_header.addWidget(self.modify_categories_button)
        category_header.addWidget(self.restore_categories_button)
        categories.addLayout(category_header)
        hint = wrapping_label("Physics · 选择 1–3 个具体分类 · 当前设置用于下次抓取")
        hint.setStyleSheet(f"color: {TEXT_SECONDARY};")
        categories.addWidget(hint)
        self.category_status = wrapping_label()
        self.category_status.setStyleSheet("color: #a4262c;")
        self.category_status.hide()
        categories.addWidget(self.category_status)
        fetch.addWidget(category_card)
        counts = QHBoxLayout()
        for name in ("原始条目数", "去重后候选数"):
            tile = QWidget()
            tile.setObjectName("statTile")
            tile.setMinimumHeight(54)
            column = QVBoxLayout(tile)
            column.setContentsMargins(0, 2, 0, 2)
            column.setSpacing(2)
            caption = wrapping_label(name)
            caption.setStyleSheet(f"color: {TEXT_SECONDARY};")
            caption.setAlignment(Qt.AlignmentFlag.AlignCenter)
            column.addWidget(caption)
            value = wrapping_label("—")
            value.setObjectName("statValue")
            value.setAlignment(Qt.AlignmentFlag.AlignCenter)
            column.addWidget(value)
            self.statistics[name] = value
            counts.addWidget(tile, 1)
        fetch.addLayout(counts)
        buttons = QHBoxLayout()
        self.fetch_button = PrimaryPushButton(FluentIcon.DOWNLOAD, "抓取今天")
        self.calendar_button = ToolButton(FluentIcon.CALENDAR)
        self.calendar_button.setFixedSize(38, 38)
        self.calendar_button.setToolTip("选择抓取日期（北京时间）")
        self.calendar_button.setAccessibleName("选择抓取日期（北京时间）")
        self.date_picker = DatePickerPopup(self)
        left = QHBoxLayout()
        left.addWidget(self.fetch_button, 1)
        left.addWidget(self.calendar_button)
        buttons.addLayout(left, 1)
        self.analyze_button = PrimaryPushButton(FluentIcon.PLAY, "开始两轮分析")
        self.analyze_button.setEnabled(False)
        for button in (self.fetch_button, self.analyze_button):
            button.setMinimumHeight(38)
        buttons.addWidget(self.analyze_button, 1)
        fetch.addLayout(buttons)
        # 使用当前字体/DPI 的实时 sizeHint，避免父窗口字体变化后旧高度裁切按钮。
        fetch_card.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        layout.addWidget(fetch_card)
        self.task_panel = TaskPanel()
        layout.addWidget(self.task_panel)

        report_card, report_layout = card("本次日报")
        self.report_stack = QStackedWidget()
        empty = QWidget()
        empty_layout = QVBoxLayout(empty)
        empty_layout.addStretch()
        title = SubtitleLabel("暂无日报")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty_layout.addWidget(title)
        hint = BodyLabel("请先获取候选，再开始两轮分析")
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty_layout.addWidget(hint)
        empty_layout.addStretch()
        self.report = QTextBrowser()
        self.report.setOpenLinks(False)
        self.report.setOpenExternalLinks(False)
        # 日报始终获取剩余空间；小窗口/高 DPI 允许缩小可滚动正文以保住顶部操作。
        self.report.setMinimumHeight(70)
        self.report.setStyleSheet(
            "QTextBrowser {background: transparent; border: none; color: #17243b; padding: 8px;"
            "font-family: 'Microsoft YaHei UI'; font-size: 14px;}"
        )
        self.report_stack.addWidget(empty)
        self.report_stack.addWidget(self.report)
        self.report.textChanged.connect(
            lambda: self.report_stack.setCurrentIndex(1 if not self.report.document().isEmpty() else 0)
        )
        report_layout.addWidget(self.report_stack, 1)
        layout.addWidget(report_card, 1)
        self.setStyleSheet(PAGE_STYLE + (
            "QWidget#categoryCard {background: transparent; border: none;}"
            "QLabel#categorySelection {color: #1559a7; font-weight: 600;}"
            "QWidget#statTile {background: transparent; border: none;}"
            f"QLabel#statValue {{font-size: 21px; font-weight: 600; color: {TEXT_PRIMARY};}}"
        ))


class SettingsPage(ScrollArea):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("settingsPage")
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        content = QWidget()
        content.setObjectName("settingsContent")
        layout = QVBoxLayout(content)
        layout.setContentsMargins(24, 18, 24, 18)
        layout.setSpacing(12)
        layout.addWidget(TitleLabel("设置"))
        key_card, key = card("DeepSeek API Key")
        self.api_key = PasswordLineEdit()
        self.api_key.setObjectName("apiKeyInput")
        self.api_key.setFixedHeight(38)
        self.api_key.setCustomFocusedBorderColor("#1677ff", "#1677ff")
        self.api_key.setStyleSheet(API_KEY_STYLE)
        self.api_key.setPlaceholderText("可留空，不影响候选抓取")
        self.api_key.setAccessibleName("DeepSeek API Key")
        key.addWidget(self.api_key)
        key.addWidget(wrapping_label("失去焦点或按回车自动保存；使用当前 Windows 用户 DPAPI 加密。"))
        self.secret_status = wrapping_label()
        key.addWidget(self.secret_status)
        layout.addWidget(key_card)
        about_card, about = card("关于 arXivKaleid Desktop")
        self.version_label = SubtitleLabel(f"v{__version__}")
        about.addWidget(self.version_label)
        about.addWidget(wrapping_label("按用户两轮完整研究提示词筛选 arXiv 论文的工具。"))
        about.addWidget(wrapping_label(
            "两轮分析使用你自己的 DeepSeek API。\n"
            "没有维护者服务器中转或遥测；本地运行数据与凭据由你管理。\n"
            "自有应用：GPL-3.0-only。使用与数据说明见随附 EULA.txt / PRIVACY.md。"
        ))
        layout.addWidget(about_card)
        layout.addStretch()
        self.setWidget(content)
        self.setStyleSheet(PAGE_STYLE)
        self.viewport().setObjectName("settingsViewport")
        self.viewport().setStyleSheet(
            "QWidget#settingsViewport {background: #eef3f8; border: none;}"
        )
