# Copyright (c) 2026 yyyang20
# SPDX-License-Identifier: GPL-3.0-only
# See LICENSE in the project root for the full license text.

"""首页日期弹层：复用 Qt 月历与 Fluent 控件，不接触业务网络。"""
from datetime import date

from PySide6.QtCore import QDate, QEvent, QLocale, QPoint, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QPainter, QPen, QTextCharFormat
from PySide6.QtWidgets import QCalendarWidget, QHBoxLayout, QLabel, QVBoxLayout
from qfluentwidgets import FluentIcon, ToolButton
from desktop.style import SurfaceCard
from desktop.date_policy import MIN_SELECTABLE_DATE, midnight_delay_ms, selectable_date


def qt_date(day: date) -> QDate:
    return QDate(day.year, day.month, day.day)


class DayCalendar(QCalendarWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.states = {}

    def set_states(self, states):
        self.states = dict(states)
        self.updateCells()

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() == QEvent.Type.FontChange:
            # 字体放大时增加行高，确保数字、圆点和今天边框之间仍有间距。
            metrics = self.fontMetrics()
            self.setMinimumSize(max(320, (metrics.horizontalAdvance("88") + 12) * 7),
                                max(248, (metrics.height() + 14) * 7))

    def paintCell(self, painter, rect, day):
        # 日期排布、命中与禁用仍由 Qt 处理；这里只区分四种视觉状态。
        painter.save()
        valid = self.isEnabled() and self.minimumDate() <= day <= self.maximumDate()
        selected = valid and day == self.selectedDate()
        box = QRectF(rect).adjusted(3, 3, -3, -3)
        painter.setPen(Qt.PenStyle.NoPen)
        if selected:
            painter.setBrush(QColor("#1677ff"))
            painter.drawRoundedRect(box, 6, 6)
        if day == self.maximumDate():
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(QColor("#1677ff") if not selected else QColor("#ffffff"), 1.5))
            painter.drawRoundedRect(box.adjusted(1, 1, -1, -1), 6, 6)
        painter.setPen(QColor("#ffffff" if selected else "#17243b" if valid else "#aab4c2"))
        # 全部日期统一预留圆点区域，字体和圆点均使用逻辑像素。
        text_box = QRectF(rect).adjusted(0, 0, 0, -8)
        painter.drawText(text_box, Qt.AlignmentFlag.AlignCenter, str(day.day()))
        state = self.states.get(day.toPython()) if valid else None
        if state is not None:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            center_y = min(box.bottom() - 4, text_box.center().y() + painter.fontMetrics().height() / 2 + 4)
            painter.setBrush(QColor("#1677ff" if state else "#8796a8"))
            painter.setPen(QPen(QColor("#ffffff"), 1.2) if selected else QPen(Qt.PenStyle.NoPen))
            painter.drawEllipse(QRectF(box.center().x() - 2.5, center_y - 2.5, 5, 5))
        painter.restore()


class DatePickerPopup(SurfaceCard):
    date_chosen = Signal(object)
    refresh_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags(Qt.WindowType.Popup)
        self.setObjectName("datePickerPopup")
        self._range_updating = False
        self._anchor = None
        self.range_ready = False
        self.midnight_timer = QTimer(self)
        self.midnight_timer.setSingleShot(True)
        self.midnight_timer.setTimerType(Qt.TimerType.PreciseTimer)
        self.midnight_timer.timeout.connect(self.refresh_live_date)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        header = QHBoxLayout()
        self.previous = ToolButton(FluentIcon.LEFT_ARROW, self)
        self.next = ToolButton(FluentIcon.RIGHT_ARROW, self)
        self.month = QLabel()
        self.month.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.month.setStyleSheet("font-weight: 600;")
        header.addWidget(self.previous)
        header.addWidget(self.month, 1)
        header.addWidget(self.next)
        layout.addLayout(header)
        self.calendar = DayCalendar(self)
        self.calendar.setMinimumSize(320, 248)
        self.calendar.setLocale(QLocale(QLocale.Language.Chinese, QLocale.Country.China))
        self.calendar.setFirstDayOfWeek(Qt.DayOfWeek.Monday)
        self.calendar.setVerticalHeaderFormat(QCalendarWidget.VerticalHeaderFormat.NoVerticalHeader)
        self.calendar.setNavigationBarVisible(False)
        self.calendar.setGridVisible(False)
        # 覆盖 Qt 默认周末红色，星期标题与首页灰蓝色调保持一致。
        weekday_style = QTextCharFormat()
        weekday_style.setForeground(QColor("#5f6f82"))
        weekday_style.setBackground(QColor("#eef4f9"))
        for weekday in Qt.DayOfWeek:
            self.calendar.setWeekdayTextFormat(weekday, weekday_style)
        self.calendar.setStyleSheet("QCalendarWidget QWidget {background: #f8fbff; color: #17243b;} QCalendarWidget QAbstractItemView {border: none; selection-background-color: #1677ff;}")
        layout.addWidget(self.calendar)
        self.hint = QLabel("可选 2025-01-01 至北京时间今天；未来日期不可选")
        self.hint.setWordWrap(True)
        self.hint.setStyleSheet("color: #66758a;")
        layout.addWidget(self.hint)
        self.history_status = QLabel()
        self.history_status.setWordWrap(True)
        self.history_status.setStyleSheet("color: #66758a;")
        layout.addWidget(self.history_status)
        self.previous.clicked.connect(self.calendar.showPreviousMonth)
        self.next.clicked.connect(self.calendar.showNextMonth)
        self.calendar.currentPageChanged.connect(self._page_changed)
        self.calendar.clicked.connect(self._choose)
        self.calendar.activated.connect(self._choose)

    def prepare(self, today: date, selected: date):
        self.refresh_range(today, selected, preserve_page=False)

    def refresh_range(self, today: date, selected: date, *, preserve_page=True):
        page = self.calendar.yearShown(), self.calendar.monthShown()
        if preserve_page and qt_date(today) == self.calendar.maximumDate():
            # 未跨日时保留键盘正在浏览的日期，不把选中位置拉回应用的已确认日期。
            selected = self.calendar.selectedDate().toPython()
        self.range_ready = selectable_date(today, today)
        self.calendar.setEnabled(self.range_ready)
        if not self.range_ready:
            self.previous.setEnabled(False)
            self.next.setEnabled(False)
            self.hint.setText("本机日期异常：北京时间今天早于 2025-01-01，暂不可选择。")
            self.calendar.set_states({})
            return
        self.hint.setText("可选 2025-01-01 至北京时间今天；未来日期不可选")
        selected = selected if selectable_date(selected, today) else today
        self._range_updating = True
        try:
            self.calendar.setDateRange(qt_date(MIN_SELECTABLE_DATE), qt_date(today))
            if self.calendar.selectedDate() != qt_date(selected):
                self.calendar.setSelectedDate(qt_date(selected))
            year, month = page if preserve_page else (selected.year, selected.month)
            self.calendar.setCurrentPage(year, month)
            self._page_changed(year, month)
            self.calendar.updateCells()
        finally:
            self._range_updating = False

    def refresh_live_date(self):
        if self.isVisible():
            self.refresh_requested.emit()
            self.midnight_timer.start(midnight_delay_ms())

    def showEvent(self, event):
        super().showEvent(event)
        self.refresh_live_date()

    def hideEvent(self, event):
        self.midnight_timer.stop()
        self._anchor = None
        super().hideEvent(event)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        # 字体或提示文字可能在显示后增高弹层；用新尺寸重新约束位置。
        anchor = getattr(self, "_anchor", None)
        if self.isVisible() and anchor is not None:
            self._position_at(anchor)

    def begin_history(self):
        self.calendar.set_states({})
        self.history_status.setText("正在读取日报状态…")

    def finish_history(self, states, issue=None):
        self.calendar.set_states({} if issue else states)
        self.history_status.setText(
            f"日报状态暂不可用（{issue.code}）；可重新打开日历查询。" if issue else
            "蓝点：有推荐日报　灰点：仅零推荐日报"
        )

    def _page_changed(self, year, month):
        if not self._range_updating:
            self.refresh_live_date()
        if not self.range_ready:
            return
        # currentPageChanged 同时约束按钮、键盘、滚轮及 Qt 的内部翻页行为。
        first = QDate(year, month, 1)
        minimum = self.calendar.minimumDate()
        maximum = self.calendar.maximumDate()
        lower = QDate(minimum.year(), minimum.month(), 1)
        upper = QDate(maximum.year(), maximum.month(), 1)
        bounded = max(lower, min(first, upper))
        if bounded != first:
            self.calendar.setCurrentPage(bounded.year(), bounded.month())
            return
        self.previous.setEnabled(first > lower)
        self.next.setEnabled(first < upper)
        self.month.setText(f"{year} 年 {month} 月")

    def _choose(self, chosen):
        self.refresh_live_date()
        if self.range_ready and self.calendar.minimumDate() <= chosen <= self.calendar.maximumDate():
            self.hide()
            self.date_chosen.emit(chosen.toPython())

    def show_at(self, anchor):
        self._anchor = anchor
        self.adjustSize()
        self._position_at(anchor)
        self.show()
        self.calendar.setFocus()

    def _position_at(self, anchor):
        # 所有尺寸为逻辑像素；限制到客户区与当前屏幕的交集，避免高 DPI 溢出。
        window = anchor.window()
        available = window.screen().availableGeometry().intersected(
            window.rect().translated(window.mapToGlobal(QPoint(0, 0)))
        )
        if available.isEmpty():
            return
        below = anchor.mapToGlobal(QPoint(0, anchor.height() + 4))
        above = anchor.mapToGlobal(QPoint(0, -self.height() - 4))
        x = max(available.left(), min(below.x() - self.width() // 2, available.right() - self.width() + 1))
        y = below.y() if below.y() + self.height() <= available.bottom() + 1 else above.y()
        y = max(available.top(), min(y, available.bottom() - self.height() + 1))
        self.move(x, y)
