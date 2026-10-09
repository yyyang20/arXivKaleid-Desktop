# Copyright (c) 2026 yyyang20
# SPDX-License-Identifier: GPL-3.0-only
# See LICENSE in the project root for the full license text.

"""复用两轮编辑窗口管理完整研究 Prompt，程序仅保留技术和安全边界。"""
from PySide6.QtCore import QRect, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import QAbstractButton, QHBoxLayout, QMessageBox, QPlainTextEdit, QSizePolicy, QStackedWidget, QVBoxLayout, QWidget
from qfluentwidgets import FluentIcon, PrimaryPushButton, PushButton, ScrollArea, TitleLabel

from desktop.research_requirements import RequirementsError, RequirementsStore
from desktop.style import ACCENT, CARD_BACKGROUND, CARD_BORDER, PAGE_BACKGROUND, SUBTLE_BACKGROUND, TEXT_PRIMARY, TEXT_SECONDARY
from desktop.task_panel import wrapping_label


class PromptEntryCard(QAbstractButton):
    """整卡是单一按钮；标签透传鼠标，文字换行决定实际高度。"""

    def __init__(self, title, purpose, parent=None):
        super().__init__(parent)
        self.setText(title)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        # 历史行高 104，绘制上下各留 5：实际外框高 94，间隔 10。
        self.setMinimumHeight(94)
        policy = QSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        policy.setHeightForWidth(True)
        self.setSizePolicy(policy)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(72, 19, 54, 19)
        layout.setSpacing(10)
        heading = QWidget(self)
        heading.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        row = QHBoxLayout(heading)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(18)
        self.title = wrapping_label(title)
        font = QFont("Microsoft YaHei UI", 10)
        font.setBold(True)
        self.title.setFont(font)
        self.title.setStyleSheet(f"color: {TEXT_PRIMARY};")
        self.state = wrapping_label("")
        font = QFont("Microsoft YaHei UI")
        font.setPixelSize(12)
        self.state.setFont(font)
        self.state.setStyleSheet(f"color: {TEXT_SECONDARY};")
        # 完整 Prompt 标题较长，均分标题与状态空间；大字体仍按需换行。
        row.addWidget(self.title, 1)
        row.addWidget(self.state, 1)
        layout.addWidget(heading)
        self.purpose = wrapping_label(purpose)
        self.purpose.setFont(QFont("Microsoft YaHei UI", 10))
        self.purpose.setStyleSheet(f"color: {TEXT_SECONDARY};")
        layout.addWidget(self.purpose)
        for label in (self.title, self.state, self.purpose):
            label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)

    def sizeHint(self):
        return QSize(300, max(94, self.layout().sizeHint().height()))

    def minimumSizeHint(self):
        return QSize(0, 94)

    def heightForWidth(self, width):
        return max(94, self.layout().heightForWidth(width))

    def icon_rect(self):
        # 与历史 DOCUMENT 图标同为 36px，左侧距外框 18px，垂直居中。
        frame = self.rect().adjusted(1, 0, -1, 0)
        return QRect(frame.left() + 18, frame.center().y() - 18, 36, 36)

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        hover = self.underMouse()
        background = SUBTLE_BACKGROUND if self.isDown() else "#f4f8fc" if hover else CARD_BACKGROUND
        border = ACCENT if self.hasFocus() else "#b7c8d9" if hover else CARD_BORDER
        painter.setBrush(QColor(background))
        painter.setPen(QPen(QColor(border), 1))
        painter.drawRoundedRect(QRectF(self.rect()).adjusted(1, .5, -1, -.5), 8, 8)
        FluentIcon.EDIT.icon(color=QColor(ACCENT)).paint(painter, self.icon_rect())
        FluentIcon.CHEVRON_RIGHT.icon(color=QColor(TEXT_SECONDARY)).paint(
            painter, QRect(self.width() - 36, (self.height() - 16) // 2, 16, 16))

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            if not event.isAutoRepeat():
                self.click()
            event.accept()
            return
        super().keyPressEvent(event)

    def enterEvent(self, event):
        super().enterEvent(event)
        self.update()

    def leaveEvent(self, event):
        super().leaveEvent(event)
        self.update()


class PromptPage(QWidget):
    TITLES = {"round1": "第一轮完整研究提示词", "round2": "第二轮完整研究提示词"}
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
        root.addWidget(wrapping_label("分别编写两轮完整研究提示词，自定义研究判断、数量和评价；第二轮按提示词要求排序。"))
        self.states = {}
        self.entries = {}
        entries = QWidget()
        entry_layout = QVBoxLayout(entries)
        entry_layout.setContentsMargins(0, 0, 0, 0)
        entry_layout.setSpacing(10)
        for stage in self.TITLES:
            entry = PromptEntryCard(self.TITLES[stage], self.PURPOSES[stage])
            self.entries[stage] = entry
            self.states[stage] = entry.state
            entry.clicked.connect(lambda _checked=False, s=stage: self.open_round(s))
            entry_layout.addWidget(entry)
        root.addWidget(entries)
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
        self.message = wrapping_label("研究判断由本轮完整提示词决定；程序保留身份、JSON 结构和安全保护。")
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

    def boundary_message(self):
        # 两轮数量均由用户决定，第二轮数组顺序用于推荐排名。
        if self.stage == "round1":
            return "入选数量和评价由本轮完整提示词决定；程序保留身份、JSON 结构和安全保护，不要求研究排序。"
        return "入选数量、评价和排序依据由本轮完整提示词决定；程序保留身份、JSON 结构和安全保护，日报按模型顺序显示排名。"

    def refresh_states(self):
        for stage, label in self.states.items():
            try:
                saved = self.store.load(stage)
                label.setText("当前使用自定义提示词" if saved.custom else "当前使用默认提示词")
            except (RequirementsError, RuntimeError, OSError, ValueError):
                label.setText("提示词不可用，请进入详情处理")
            self.entries[stage].setAccessibleDescription(f"{self.PURPOSES[stage]} {label.text()}；回车或空格查看。")

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
            self.message.setText(self.boundary_message())
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
        status = "当前使用自定义提示词" if self.saved and self.saved.custom else "当前使用默认提示词" if self.saved else "提示词不可用"
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
        self.message.setText("编辑完整研究提示词：方向、标准、排除、数量、评价和严格程度；保存后生效。" if self.stage == "round1" else "编辑完整研究提示词：方向、标准、排除、数量、评价、严格程度和排序；保存后生效。")
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
        self.message.setText("完整研究提示词已保存；下次分析使用这份内容。")
        self.refresh_states()
        return True

    def cancel(self):
        if self.busy:
            return
        self.editing = False
        self.baseline = self.saved.text if self.saved else ""
        self.editor.setPlainText(self.baseline)
        self.message.setText(self.boundary_message())
        self.update_controls()

    def restore_default(self):
        if self.busy or self.stage is None:
            return False
        answer = QMessageBox.question(self, "恢复默认提示词", "确认恢复本轮内置默认内容？本轮自定义内容和尚未保存的修改将被替换，另一轮不受影响。", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
        if answer != QMessageBox.StandardButton.Yes:
            return False
        try:
            self.saved = self.store.save(self.stage, None)
        except (RequirementsError, RuntimeError, OSError, ValueError):
            self.message.setText("恢复默认失败；原来的保存内容和草稿未改变。")
            return False
        self.cancel()
        self.message.setText("已恢复本轮内置默认提示词。")
        self.refresh_states()
        return True

    def protect_unsaved(self):
        if not self.dirty:
            if self.editing and not self.busy:
                self.cancel()
            return True
        box = QMessageBox(self)
        box.setWindowTitle("未保存的研究提示词")
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
