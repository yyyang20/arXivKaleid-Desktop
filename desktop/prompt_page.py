# Copyright (c) 2026 yyyang20
# SPDX-License-Identifier: GPL-3.0-only
# See LICENSE in the project root for the full license text.

"""提示词页面只编辑研究要求，固定协议始终由程序管理。"""
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QHBoxLayout, QMessageBox, QPlainTextEdit, QStackedWidget, QVBoxLayout, QWidget
from qfluentwidgets import FluentIcon, PrimaryPushButton, PushButton, ScrollArea, TitleLabel

from desktop.pages import card
from desktop.research_requirements import RequirementsError, RequirementsStore
from desktop.style import ACCENT, CARD_BACKGROUND, CARD_BORDER, PAGE_BACKGROUND, TEXT_PRIMARY, TEXT_SECONDARY
from desktop.task_panel import wrapping_label


class PromptPage(QWidget):
    TITLES = {"round1": "第一轮研究要求", "round2": "第二轮研究要求"}
    PURPOSES = {"round1": "根据题目和摘要，筛选进入全文分析的论文。", "round2": "根据题目、摘要和合格全文，确定最终推荐论文。"}

    def __init__(self, store: RequirementsStore, parent=None):
        super().__init__(parent)
        self.setObjectName("promptPage")
        self.setStyleSheet(f"QWidget#promptPage {{background: {PAGE_BACKGROUND}; color: {TEXT_PRIMARY};}}")
        self.store = store
        self.stage = None
        self.saved = None
        self.editing = False
        self.busy = False
        self.baseline = ""
        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 18, 24, 18)
        self.stack = QStackedWidget()
        outer.addWidget(self.stack)
        overview = QWidget()
        overview.setObjectName("promptOverview")
        overview.setStyleSheet(f"QWidget#promptOverview {{background: {PAGE_BACKGROUND};}}")
        root = QVBoxLayout(overview)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(12)
        root.addWidget(TitleLabel("提示词"))
        root.addWidget(wrapping_label("分别管理两轮研究要求；输出协议和安全边界由程序固定。"))
        self.states = {}
        for stage in self.TITLES:
            surface, layout = card(self.TITLES[stage])
            layout.addWidget(wrapping_label(self.PURPOSES[stage]))
            row = QHBoxLayout()
            state = wrapping_label("")
            state.setStyleSheet(f"color: {TEXT_SECONDARY};")
            self.states[stage] = state
            row.addWidget(state, 1)
            button = PushButton(FluentIcon.CHEVRON_RIGHT, "查看")
            button.clicked.connect(lambda _checked=False, s=stage: self.open_round(s))
            row.addWidget(button)
            layout.addLayout(row)
            root.addWidget(surface)
        root.addStretch()
        scroll = ScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet("QScrollArea {border: none; background: transparent;}")
        scroll.setWidget(overview)
        self.stack.addWidget(scroll)
        detail = QWidget()
        layout = QVBoxLayout(detail)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        self.back_button = PushButton(FluentIcon.LEFT_ARROW, "返回提示词")
        self.back_button.clicked.connect(self.go_back)
        back_row = QHBoxLayout()
        back_row.addWidget(self.back_button)
        back_row.addStretch()
        layout.addLayout(back_row)
        self.title = TitleLabel("")
        layout.addWidget(self.title)
        self.purpose = wrapping_label("")
        layout.addWidget(self.purpose)
        self.state = wrapping_label("")
        layout.addWidget(self.state)
        self.message = wrapping_label("仅编辑研究要求；不会改变标签、数量、输出字段或费用保护。")
        layout.addWidget(self.message)
        for label in (self.purpose, self.state, self.message):
            label.setStyleSheet(f"color: {TEXT_SECONDARY};")
        # 使用与历史正文一致的 Qt 文本表面，避免只读聚焦也出现 Fluent 蓝色底边。
        self.editor = QPlainTextEdit()
        editor_font = QFont("Microsoft YaHei UI")
        editor_font.setPixelSize(14)
        self.editor.setFont(editor_font)
        self.editor.setReadOnly(True)
        self.editor.setMinimumHeight(160)
        self._editor_editable = None
        self.editor.textChanged.connect(self.update_controls)
        layout.addWidget(self.editor, 1)
        row = QHBoxLayout()
        self.edit_button = PrimaryPushButton(FluentIcon.EDIT, "编辑")
        self.save_button = PrimaryPushButton(FluentIcon.SAVE, "保存")
        self.cancel_button = PushButton("取消")
        self.restore_button = PushButton(FluentIcon.SYNC, "恢复默认")
        for button in (self.edit_button, self.save_button, self.cancel_button, self.restore_button):
            row.addWidget(button)
        row.addStretch()
        layout.addLayout(row)
        self.edit_button.clicked.connect(self.begin_edit)
        self.save_button.clicked.connect(self.save)
        self.cancel_button.clicked.connect(self.cancel)
        self.restore_button.clicked.connect(self.restore_default)
        self.stack.addWidget(detail)
        self.refresh_states()
        self.update_controls()

    @property
    def dirty(self):
        return self.editing and self.editor.toPlainText() != self.baseline

    def refresh_states(self):
        for stage, label in self.states.items():
            try:
                saved = self.store.load(stage)
                label.setText("当前使用自定义研究要求" if saved.custom else "当前使用默认研究要求")
            except (RequirementsError, RuntimeError, OSError, ValueError):
                label.setText("研究要求不可用，请进入详情处理")

    def open_round(self, stage):
        if not self.protect_unsaved():
            return False
        self.stage = stage
        self.title.setText(self.TITLES[stage])
        self.purpose.setText(self.PURPOSES[stage])
        self.editing = False
        self.saved = None
        try:
            self.saved = self.store.load(stage)
            self.baseline = self.saved.text
            self.editor.setPlainText(self.saved.text)
            self.message.setText("仅编辑研究要求；不会改变标签、数量、输出字段或费用保护。")
        except (RequirementsError, RuntimeError, OSError, ValueError):
            self.baseline = ""
            self.editor.clear()
            self.message.setText("已保存内容或资源不可用；分析已阻止。可确认恢复本轮默认；资源损坏时请重新解压软件。")
        self.stack.setCurrentIndex(1)
        self.update_controls()
        return True

    def update_controls(self):
        editable = self.editing and not self.busy
        self.editor.setReadOnly(not editable)
        if editable != self._editor_editable:
            style = f"QPlainTextEdit {{background:{CARD_BACKGROUND}; color:{TEXT_PRIMARY}; border:1px solid {CARD_BORDER}; border-radius:8px; padding:16px;}}"
            if editable:
                style += f"QPlainTextEdit:focus {{border:1px solid {ACCENT};}}"
            self.editor.setStyleSheet(style)
            self._editor_editable = editable
        self.edit_button.setVisible(not self.editing)
        self.save_button.setVisible(self.editing)
        self.cancel_button.setVisible(self.editing)
        self.edit_button.setEnabled(not self.busy and self.saved is not None)
        self.save_button.setEnabled(not self.busy and self.dirty)
        self.cancel_button.setEnabled(not self.busy)
        self.restore_button.setEnabled(not self.busy and self.stage is not None)
        status = "当前使用自定义研究要求" if self.saved and self.saved.custom else "当前使用默认研究要求" if self.saved else "研究要求不可用"
        if self.dirty:
            status += "  ·  未保存修改"
        elif self.editing:
            status += "  ·  编辑中"
        if self.busy:
            status += "  ·  分析中，仅可查看"
        self.state.setText(status)

    def set_busy(self, value):
        self.busy = value
        self.update_controls()

    def begin_edit(self):
        if self.busy or self.saved is None:
            return
        self.baseline = self.saved.text
        self.editor.setPlainText(self.baseline)
        self.editing = True
        self.message.setText("编辑研究目标、关注/排除条件和阅读偏好，保存后才生效。")
        self.update_controls()
        self.editor.setFocus()

    def save(self):
        if self.busy or not self.editing:
            return False
        try:
            saved = self.store.save(self.stage, self.editor.toPlainText())
        except (RequirementsError, RuntimeError, OSError, ValueError) as exc:
            self.message.setText(str(exc) if isinstance(exc, RequirementsError) else "保存失败；原来的生效内容未改变。")
            return False
        self.saved = saved
        self.cancel()
        self.message.setText("研究要求已保存；下次分析使用这份内容。")
        self.refresh_states()
        return True

    def cancel(self):
        if self.busy:
            return
        self.editing = False
        self.baseline = self.saved.text if self.saved else ""
        self.editor.setPlainText(self.baseline)
        self.message.setText("仅编辑研究要求；不会改变标签、数量、输出字段或费用保护。")
        self.update_controls()

    def restore_default(self):
        if self.busy or self.stage is None:
            return False
        answer = QMessageBox.question(self, "恢复默认研究要求", "确认恢复本轮内置默认内容？本轮自定义内容和尚未保存的修改将被替换，另一轮不受影响。", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
        if answer != QMessageBox.StandardButton.Yes:
            return False
        try:
            self.saved = self.store.save(self.stage, None)
        except (RequirementsError, RuntimeError, OSError, ValueError):
            self.message.setText("恢复默认失败；原来的保存内容和草稿未改变。")
            return False
        self.cancel()
        self.message.setText("已恢复本轮内置默认研究要求。")
        self.refresh_states()
        return True

    def protect_unsaved(self):
        if not self.dirty:
            if self.editing and not self.busy:
                self.cancel()
            return True
        box = QMessageBox(self)
        box.setWindowTitle("未保存的研究要求")
        box.setText("当前修改尚未保存，请选择如何继续。")
        save = box.addButton("保存并继续", QMessageBox.ButtonRole.AcceptRole)
        discard = box.addButton("放弃修改", QMessageBox.ButtonRole.DestructiveRole)
        stay = box.addButton("留在此页", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(stay)
        box.setEscapeButton(stay)
        box.exec()
        if box.clickedButton() is save:
            return self.save()
        if box.clickedButton() is discard:
            self.cancel()
            return True
        return False

    def go_back(self):
        if self.protect_unsaved():
            self.stack.setCurrentIndex(0)
            self.refresh_states()
