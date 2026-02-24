from PyQt6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QLabel,
    QFrame,
    QScrollArea,
)
from PyQt6.QtCore import Qt


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

    # -------------------------
    # TIME SECTION
    # -------------------------

    def _build_time_section(self):
        weekly = self.analytics_engine.get_weekly_stats()
        monthly = self.analytics_engine.get_monthly_stats()

        section, layout = self._create_section("Time Trends")

        layout.addWidget(QLabel("Weekly:"))

        for week, data in sorted(weekly.items()):
            label = QLabel(
                f"{week} | Created: {data['created']} | Completed: {data['completed']}"
            )
            label.setStyleSheet("font-size: 13px; color: #AAAAAA;")
            layout.addWidget(label)

        layout.addSpacing(10)
        layout.addWidget(QLabel("Monthly:"))

        for month, data in sorted(monthly.items()):
            label = QLabel(
                f"{month} | Created: {data['created']} | Completed: {data['completed']}"
            )
            label.setStyleSheet("font-size: 13px; color: #AAAAAA;")
            layout.addWidget(label)

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