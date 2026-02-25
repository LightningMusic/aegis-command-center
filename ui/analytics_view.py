from PyQt6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QLabel,
    QFrame,
    QScrollArea,
)
from PyQt6.QtCore import Qt

from PyQt6.QtGui import QPainter, QColor, QPaintEvent
from typing import Optional

class SimpleBar(QWidget):
    def __init__(self, value, max_value, label_text=""):
        super().__init__()
        self.value = value
        self.max_value = max_value
        self.label_text = label_text
        self.setMinimumHeight(22)

    def paintEvent(self, a0: Optional[QPaintEvent]):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        width = self.width()
        height = self.height()

        # Background
        painter.setBrush(QColor("#333333"))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawRoundedRect(0, 0, width, height, 6, 6)

        # Fill
        if self.max_value > 0:
            ratio = self.value / self.max_value
            fill_width = int(width * ratio)

            painter.setBrush(QColor("#4CAF50"))
            painter.drawRoundedRect(0, 0, fill_width, height, 6, 6)

        painter.end()
class AnalyticsView(QWidget):
    def __init__(self, analytics_engine):
        super().__init__()

        self.analytics_engine = analytics_engine

        self.main_layout = QVBoxLayout()
        self.main_layout.setContentsMargins(30, 30, 30, 30)
        self.main_layout.setSpacing(20)
        self.setLayout(self.main_layout)

        title = QLabel("Analytics")
        title.setStyleSheet("font-size: 26px; font-weight: 600;")
        self.main_layout.addWidget(title)

        # Scroll area (so it scales as analytics grows)
        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self.scroll_content = QWidget()
        self.scroll_layout = QVBoxLayout()
        self.scroll_layout.setSpacing(20)
        self.scroll_content.setLayout(self.scroll_layout)
        self.scroll_area.setWidget(self.scroll_content)

        self.main_layout.addWidget(self.scroll_area)

        self.refresh()

    # -------------------------
    # REFRESH
    # -------------------------

    def refresh(self):
        # Clear old content
        while self.scroll_layout.count():
            item = self.scroll_layout.takeAt(0)
            if item is not None:
                widget = item.widget()
                if widget is not None:
                    widget.deleteLater()

        self._build_summary_section()
        self._build_time_section()
        self._build_productivity_section()

    # -------------------------
    # SUMMARY SECTION
    # -------------------------

    def _build_summary_section(self):
        stats = self.analytics_engine.get_summary_stats()

        section, layout = self._create_section("Overview")

        labels = [
            f"Total Tasks: {stats['total']}",
            f"Completed: {stats['completed']}",
            f"Active: {stats['active']}",
            f"Overdue: {stats['overdue']}",
            f"Due Today: {stats['due_today']}",
            f"Completion Rate: {stats['completion_rate']}%",
        ]

        for text in labels:
            label = QLabel(text)
            label.setStyleSheet("font-size: 14px;")
            layout.addWidget(label)

        self.scroll_layout.addWidget(section)

        completion_bar = SimpleBar(
            stats["completed"],
            stats["total"],
        )
        layout.addWidget(completion_bar)

    # -------------------------
    # TIME SECTION
    # -------------------------

    def _build_time_section(self):

        weekly = self.analytics_engine.get_weekly_stats()
        monthly = self.analytics_engine.get_monthly_stats()

        section, layout = self._create_section("Time Trends")

        layout.addWidget(QLabel("Weekly:"))

        max_week_value = max(
            (data["created"] for data in weekly.values()),
            default=0,
        )

        for week, data in sorted(weekly.items()):
            label = QLabel(f"{week}")
            label.setStyleSheet("font-size: 13px;")
            layout.addWidget(label)

            bar = SimpleBar(data["created"], max_week_value)
            layout.addWidget(bar)

        layout.addSpacing(10)
        layout.addWidget(QLabel("Monthly:"))

        max_month_value = max(
            (data["created"] for data in monthly.values()),
            default=0,
        )

        for month, data in sorted(monthly.items()):
            label = QLabel(f"{month}")
            label.setStyleSheet("font-size: 13px;")
            layout.addWidget(label)

            bar = SimpleBar(data["created"], max_month_value)
            layout.addWidget(bar)

        self.scroll_layout.addWidget(section)

    # -------------------------
    # PRODUCTIVITY SECTION
    # -------------------------

    def _build_productivity_section(self):
        avg_days = self.analytics_engine.get_average_completion_days()
        streak = self.analytics_engine.get_completion_streak()
        upcoming_7 = self.analytics_engine.get_upcoming_7_days()

        section, layout = self._create_section("Productivity Insights")

        labels = [
            f"Average Completion Time: {avg_days} days",
            f"Current Completion Streak: {streak} days",
            f"Tasks Due Next 7 Days: {upcoming_7}",
        ]

        for text in labels:
            label = QLabel(text)
            label.setStyleSheet("font-size: 14px;")
            layout.addWidget(label)

        self.scroll_layout.addWidget(section)

    # -------------------------
    # HELPER
    # -------------------------

    def _create_section(self, title_text):
        section = QFrame()
        section.setStyleSheet("""
            QFrame {
                background-color: #252525;
                border-radius: 8px;
                padding: 12px;
            }
        """)

        layout = QVBoxLayout()
        layout.setSpacing(8)
        section.setLayout(layout)

        title = QLabel(title_text)
        title.setStyleSheet("font-size: 18px; font-weight: 600;")
        layout.addWidget(title)

        return section, layout