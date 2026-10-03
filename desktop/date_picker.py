# Copyright (c) 2026 yyyang20
# SPDX-License-Identifier: GPL-3.0-only
# See LICENSE in the project root for the full license text.

"""首页日期弹层：复用 Qt 月历与 Fluent 控件，不接触业务网络。"""
from datetime import date

from PySide6.QtCore import QDate, QLocale, QPoint, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QPen, QTextCharFormat
from PySide6.QtWidgets import QCalendarWidget, QHBoxLayout, QLabel, QVBoxLayout
from qfluentwidgets import FluentIcon, ToolButton
from desktop.style import SurfaceCard


def qt_date(day: date) -> QDate:
    return QDate(day.year, day.month, day.day)


class DayCalendar(QCalendarWidget):
    def paintCell(self, painter, rect, day):
        # 日期排布、命中与禁用仍由 Qt 处理；这里只区分四种视觉状态。
        painter.save()
        valid = self.minimumDate() <= day <= self.maximumDate()
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
        painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, str(day.day()))
        painter.restore()


class DatePickerPopup(SurfaceCard):
    date_chosen = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags(Qt.WindowType.Popup)
        self.setObjectName("datePickerPopup")
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
        hint = QLabel("可选今天及此前 365 天；未来日期不可选")
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #66758a;")
        layout.addWidget(hint)
        self.previous.clicked.connect(self.calendar.showPreviousMonth)
        self.next.clicked.connect(self.calendar.showNextMonth)
        self.calendar.currentPageChanged.connect(self._page_changed)
        self.calendar.clicked.connect(self._choose)
        self.calendar.activated.connect(self._choose)

    def prepare(self, today: date, selected: date):
        latest = qt_date(today)
        self.calendar.setDateRange(latest.addDays(-365), latest)
        self.calendar.setSelectedDate(qt_date(selected))
        self.calendar.setCurrentPage(selected.year, selected.month)
        self._page_changed(selected.year, selected.month)
        self.calendar.update()

    def _page_changed(self, year, month):
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
        if self.calendar.minimumDate() <= chosen <= self.calendar.maximumDate():
            self.hide()
            self.date_chosen.emit(chosen.toPython())

    def show_at(self, anchor):
        # 所有尺寸为逻辑像素；限制到客户区与当前屏幕的交集，避免高 DPI 溢出。
        window = anchor.window()
        available = window.screen().availableGeometry().intersected(
            window.rect().translated(window.mapToGlobal(QPoint(0, 0)))
        )
        self.adjustSize()
        below = anchor.mapToGlobal(QPoint(0, anchor.height() + 4))
        above = anchor.mapToGlobal(QPoint(0, -self.height() - 4))
        x = max(available.left(), min(below.x() - self.width() // 2, available.right() - self.width() + 1))
        y = below.y() if below.y() + self.height() <= available.bottom() + 1 else above.y()
        y = max(available.top(), min(y, available.bottom() - self.height() + 1))
        self.move(x, y)
        self.show()
        self.calendar.setFocus()
