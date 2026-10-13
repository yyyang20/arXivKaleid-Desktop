# Copyright (c) 2026 yyyang20
# SPDX-License-Identifier: GPL-3.0-only
"""分类窗口只管理草稿；主窗口负责成功保存后更新生效选择。"""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog, QHBoxLayout, QVBoxLayout, QWidget
from qfluentwidgets import CheckBox, PrimaryPushButton, PushButton, ScrollArea, SubtitleLabel

from desktop.categories import ARCHIVES, PHYSICS_CATEGORIES, validate_categories
from desktop.style import PAGE_BACKGROUND, TEXT_PRIMARY, SurfaceCard
from desktop.task_panel import wrapping_label


class CategoryDialog(QDialog):
    def __init__(self, categories, parent=None):
        super().__init__(parent)
        self._selected = list(validate_categories(categories))
        self.setWindowTitle("选择抓取分类")
        self.setModal(True)
        self.setFont(parent.font() if parent is not None else self.font())
        self.resize(740, 660)
        screen = self.screen().availableGeometry()
        self.resize(min(self.width(), screen.width() - 40), min(self.height(), screen.height() - 60))
        self.setObjectName("categoryDialog")
        self.setStyleSheet(f"QDialog#categoryDialog {{background: {PAGE_BACKGROUND}; color: {TEXT_PRIMARY};}}")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.addWidget(SubtitleLabel("选择抓取分类"))
        self.counter = wrapping_label()
        layout.addWidget(self.counter)
        self.scroll = ScrollArea(self)
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        content = QWidget()
        groups = QVBoxLayout(content)
        groups.setContentsMargins(0, 0, 8, 0)
        groups.setSpacing(8)
        self.checkboxes = {}
        self.group_buttons = {}
        self.group_contents = {}
        for archive, chinese, english in ARCHIVES:
            rows = tuple(c for c in PHYSICS_CATEGORIES if c.archive == archive)
            card = SurfaceCard()
            column = QVBoxLayout(card)
            column.setContentsMargins(12, 8, 12, 8)
            header = PushButton()
            header.setAccessibleName(f"展开或收起 {chinese}")
            header.setCheckable(True)
            expanded = any(c.id in self._selected for c in rows)
            header.setChecked(expanded)
            column.addWidget(header)
            body = QWidget()
            items = QVBoxLayout(body)
            items.setContentsMargins(4, 4, 4, 4)
            for category in rows:
                row = QWidget()
                item = QVBoxLayout(row)
                item.setContentsMargins(0, 3, 0, 3)
                check = CheckBox(category.id)
                check.setAccessibleName(category.id + " " + category.name)
                check.setChecked(category.id in self._selected)
                check.toggled.connect(lambda checked, identity=category.id: self.toggle_category(identity, checked))
                item.addWidget(check)
                description = wrapping_label(category.name)
                description.setContentsMargins(28, 0, 0, 0)
                item.addWidget(description)
                items.addWidget(row)
                self.checkboxes[category.id] = check
            column.addWidget(body)
            body.setVisible(expanded)
            header.toggled.connect(body.setVisible)
            self.group_buttons[archive] = (header, chinese, english, rows)
            self.group_contents[archive] = body
            groups.addWidget(card)
        groups.addStretch()
        self.scroll.setWidget(content)
        self.scroll.setStyleSheet(f"QScrollArea {{border: none; background: {PAGE_BACKGROUND};}}")
        layout.addWidget(self.scroll, 1)
        self.error_label = wrapping_label()
        self.error_label.setStyleSheet("color: #a4262c;")
        layout.addWidget(self.error_label)
        buttons = QHBoxLayout()
        buttons.addStretch()
        self.cancel_button = PushButton("取消")
        self.cancel_button.clicked.connect(self.reject)
        self.confirm_button = PrimaryPushButton("确定并保存")
        # 按钮不直接 accept：保存失败时继续保留本窗口和草稿。
        buttons.addWidget(self.cancel_button)
        buttons.addWidget(self.confirm_button)
        layout.addLayout(buttons)
        self.refresh_selection()

    @property
    def categories(self):
        return validate_categories(self._selected)

    def toggle_category(self, identity, checked):
        if checked and identity not in self._selected:
            self._selected.append(identity)
        elif not checked and identity in self._selected:
            self._selected.remove(identity)
        self.refresh_selection()

    def refresh_selection(self):
        count = len(self._selected)
        self.counter.setText(f"已选择 {count} / 3 · Physics 共 51 个具体分类 · 至少选择 1 个")
        self.confirm_button.setEnabled(1 <= count <= 3)
        for identity, check in self.checkboxes.items():
            check.setEnabled(identity in self._selected or count < 3)
        for header, chinese, english, rows in self.group_buttons.values():
            selected = sum(c.id in self._selected for c in rows)
            header.setText(f"{chinese}（{rows[0].archive}） · {selected} / {len(rows)}")
            header.setToolTip(english)
