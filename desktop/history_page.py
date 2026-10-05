# Copyright (c) 2026 yyyang20
# SPDX-License-Identifier: GPL-3.0-only
# See LICENSE in the project root for the full license text.

"""历史页面与虚拟卡片；只呈现不可变快照，不读取数据库或生成日报。"""
from PySide6.QtCore import QAbstractListModel, QEvent, QRect, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPen
from PySide6.QtWidgets import (
    QAbstractItemView, QListView, QStackedWidget, QStyledItemDelegate,
    QStyle, QTextBrowser, QVBoxLayout, QWidget,
)
from qfluentwidgets import BodyLabel, FluentIcon, PushButton, TitleLabel

from desktop.history import HistoryRecord, PAGE_SIZE
from desktop.style import ACCENT, CARD_BACKGROUND, CARD_BORDER, PAGE_STYLE, TEXT_PRIMARY, TEXT_SECONDARY


class HistoryModel(QAbstractListModel):
    more_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.rows = []
        self.has_more = False
        self.loading = False
        self.cursor = None

    def rowCount(self, parent=None):
        return 0 if parent is not None and parent.isValid() else len(self.rows)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or not 0 <= index.row() < len(self.rows):
            return None
        row = self.rows[index.row()]
        if role in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.AccessibleTextRole):
            return (f"抓取时间 {row.fetch_time_text} 北京时间，论文日期 {row.candidate_date.isoformat()}，"
                    f"候选 {row.candidate_count} 篇，推荐 {row.recommendation_count} 篇")
        if role == Qt.ItemDataRole.UserRole:
            return row
        return None

    def canFetchMore(self, parent):
        return not parent.isValid() and self.has_more and not self.loading

    def fetchMore(self, parent):
        if self.canFetchMore(parent):
            self.loading = True
            self.more_requested.emit()

    def reset(self):
        self.beginResetModel()
        self.rows.clear()
        self.has_more = False
        self.loading = True
        self.cursor = None
        self.endResetModel()

    def append(self, rows):
        # 游标分页不加载正文；视图只绘制可见项，不逐条创建 QWidget。
        if rows:
            first = len(self.rows)
            self.beginInsertRows(self.index(-1, -1), first, first + len(rows) - 1)
            self.rows.extend(rows)
            self.cursor = rows[-1].cursor
            self.endInsertRows()
        self.has_more = len(rows) == PAGE_SIZE
        self.loading = False

    def remove_record(self, record_id):
        for index, row in enumerate(self.rows):
            if row.record_id == record_id:
                self.beginRemoveRows(self.index(-1, -1), index, index)
                self.rows.pop(index)
                self.endRemoveRows()
                return


class HistoryDelegate(QStyledItemDelegate):
    open_requested = Signal(str)
    delete_requested = Signal(str)

    @staticmethod
    def delete_rect(rect):
        return QRect(rect.right() - 106, rect.center().y() - 20, 88, 40)

    def sizeHint(self, option, index):
        row = index.data(Qt.ItemDataRole.UserRole)
        if row is None:
            return QSize(300, 104)
        width = self.parent().viewport().width()
        _, title, caption = self.text_layout(option, row, width)
        return QSize(300, max(104, caption.bottom() + 26))

    @staticmethod
    def card_text(row):
        return (f"抓取时间 {row.fetch_time_text}",
                f"论文日期 {row.candidate_date.isoformat()}  ·  候选 {row.candidate_count} 篇  ·  推荐 {row.recommendation_count} 篇")

    @classmethod
    def text_layout(cls, option, row, width):
        # 绘制与高度计算使用同一字体、宽度和换行规则，避免窄窗口遮住日期或删除按钮。
        font = QFont(option.font)
        font.setBold(True)
        title_text, caption_text = cls.card_text(row)
        available = max(1, width - 195)
        flags = Qt.AlignmentFlag.AlignLeft | Qt.TextFlag.TextWordWrap
        title_height = QFontMetrics(font).boundingRect(QRect(0, 0, available, 10000), flags, title_text).height()
        caption_height = option.fontMetrics.boundingRect(QRect(0, 0, available, 10000), flags, caption_text).height()
        title = QRect(72, 24, available, title_height)
        caption = QRect(72, title.bottom() + 11, available, caption_height)
        return font, title, caption

    def paint(self, painter, option, index):
        row = index.data(Qt.ItemDataRole.UserRole)
        if row is None:
            return
        painter.save()
        rect = option.rect.adjusted(1, 5, -1, -5)
        painter.setRenderHint(painter.RenderHint.Antialiasing)
        painter.setBrush(QColor(CARD_BACKGROUND))
        painter.setPen(QPen(QColor(ACCENT if option.state & QStyle.StateFlag.State_Selected else CARD_BORDER), 1))
        painter.drawRoundedRect(rect, 8, 8)
        icon = QRect(rect.left() + 18, rect.center().y() - 18, 36, 36)
        FluentIcon.DOCUMENT.icon(color=QColor(ACCENT)).paint(painter, icon)
        button = self.delete_rect(option.rect)
        font, title, caption = self.text_layout(option, row, option.rect.width())
        title.translate(option.rect.topLeft())
        caption.translate(option.rect.topLeft())
        title_text, caption_text = self.card_text(row)
        flags = Qt.AlignmentFlag.AlignLeft | Qt.TextFlag.TextWordWrap
        painter.setFont(font)
        painter.setPen(QColor(TEXT_PRIMARY))
        painter.drawText(title, flags, title_text)
        font.setBold(False)
        painter.setFont(font)
        painter.setPen(QColor(TEXT_SECONDARY))
        painter.drawText(caption, flags, caption_text)
        painter.setBrush(QColor(CARD_BACKGROUND))
        painter.setPen(QPen(QColor(CARD_BORDER), 1))
        painter.drawRoundedRect(button, 5, 5)
        painter.setPen(QColor(TEXT_PRIMARY))
        painter.drawText(button, Qt.AlignmentFlag.AlignCenter, "删除")
        painter.restore()

    def editorEvent(self, event, model, option, index):
        if event.type() == QEvent.Type.MouseButtonRelease and event.button() == Qt.MouseButton.LeftButton:
            row = index.data(Qt.ItemDataRole.UserRole)
            if row is not None:
                # 删除命中区完全消费事件，绝不继续打开卡片正文。
                signal = self.delete_requested if self.delete_rect(option.rect).contains(event.position().toPoint()) else self.open_requested
                signal.emit(row.record_id)
                return True
        return super().editorEvent(event, model, option, index)


class HistoryList(QListView):
    open_requested = Signal(str)
    delete_requested = Signal(str)

    def keyPressEvent(self, event):
        row = self.currentIndex().data(Qt.ItemDataRole.UserRole)
        if row is not None and event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Delete):
            signal = self.delete_requested if event.key() == Qt.Key.Key_Delete else self.open_requested
            signal.emit(row.record_id)
            event.accept()
            return
        super().keyPressEvent(event)


class HistoryPage(QWidget):
    open_requested = Signal(str)
    delete_requested = Signal(str)
    back_requested = Signal()
    more_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("historyPage")
        self.current_record_id = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 18, 24, 18)
        layout.setSpacing(12)
        self.stack = QStackedWidget()
        listing = QWidget()
        column = QVBoxLayout(listing)
        column.setContentsMargins(0, 0, 0, 0)
        column.addWidget(TitleLabel("历史"))
        column.addWidget(BodyLabel("查看已保存的日报记录 · 时间为北京时间"))
        self.message = BodyLabel("暂无历史记录；成功生成的日报将自动保存。")
        self.message.setWordWrap(True)
        column.addWidget(self.message)
        self.model = HistoryModel(self)
        self.list_view = HistoryList()
        self.list_view.setModel(self.model)
        self.list_view.setUniformItemSizes(False)
        self.list_view.setResizeMode(QListView.ResizeMode.Adjust)
        self.list_view.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.list_view.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.list_view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.list_view.setAccessibleName("日报历史；回车查看，Delete 删除")
        self.list_view.setStyleSheet("QListView {background: transparent; border: none; outline: none;}")
        self.delegate = HistoryDelegate(self.list_view)
        self.list_view.setItemDelegate(self.delegate)
        for source in (self.list_view, self.delegate):
            source.open_requested.connect(self.open_requested)
            source.delete_requested.connect(self.delete_requested)
        self.model.more_requested.connect(self.more_requested)
        column.addWidget(self.list_view, 1)
        detail = QWidget()
        body = QVBoxLayout(detail)
        body.setContentsMargins(0, 0, 0, 0)
        self.back_button = PushButton(FluentIcon.LEFT_ARROW, "返回历史")
        self.back_button.clicked.connect(self.back_requested)
        body.addWidget(self.back_button, 0, Qt.AlignmentFlag.AlignLeft)
        body.addWidget(TitleLabel("日报详情"))
        self.detail_meta = BodyLabel()
        self.detail_meta.setWordWrap(True)
        body.addWidget(self.detail_meta)
        self.detail_message = BodyLabel()
        self.detail_message.setWordWrap(True)
        body.addWidget(self.detail_message)
        self.report = QTextBrowser()
        self.report.setOpenLinks(False)
        self.report.setOpenExternalLinks(False)
        self.report.setStyleSheet(
            f"QTextBrowser {{background: {CARD_BACKGROUND}; color: {TEXT_PRIMARY};"
            f"border: 1px solid {CARD_BORDER}; border-radius: 8px; padding: 16px;"
            "font-family: 'Microsoft YaHei UI'; font-size: 14px;}"
        )
        body.addWidget(self.report, 1)
        self.stack.addWidget(listing)
        self.stack.addWidget(detail)
        layout.addWidget(self.stack)
        self.setStyleSheet(PAGE_STYLE)

    def show_list(self):
        self.current_record_id = None
        self.stack.setCurrentIndex(0)
        self.report.clear()

    def begin_detail(self, record_id):
        self.current_record_id = record_id
        self.detail_meta.clear()
        self.report.clear()
        self.detail_message.setText("正在读取日报…")
        self.stack.setCurrentIndex(1)

    def show_record(self, record: HistoryRecord):
        row = record.summary
        self.detail_meta.setText(f"{row.time_text} 北京时间  ·  候选 {row.candidate_count} 篇  ·  推荐 {row.recommendation_count} 篇")
        self.detail_message.clear()
        self.report.setMarkdown(record.markdown)
