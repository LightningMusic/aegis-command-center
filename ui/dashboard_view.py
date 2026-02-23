from PyQt6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QLabel,
    QFrame
)
from PyQt6.QtCore import Qt


class DashboardView(QWidget):
    def __init__(self, task_manager, analytics_engine):
        super().__init__()

        self.task_manager = task_manager
        self.analytics_engine = analytics_engine
        self.main_layout = QVBoxLayout()
        self.main_layout.setContentsMargins(30, 30, 30, 30)
        self.main_layout.setSpacing(20)
        self.setLayout(self.main_layout)

        title = QLabel("Dashboard")
        title.setStyleSheet("font-size: 26px; font-weight: 600;")
        self.main_layout.addWidget(title)

        # Stats container
        self.stats_layout = QVBoxLayout()
        self.stats_layout.setSpacing(12)

        self.total_label = QLabel()
        self.completed_label = QLabel()
        self.active_label = QLabel()
        self.due_today_label = QLabel()
        self.overdue_label = QLabel()
        self.next_task_label = QLabel()

        for label in [
            self.total_label,
            self.completed_label,
            self.active_label,
            self.due_today_label,
            self.overdue_label,
            self.next_task_label,
        ]:
            label.setStyleSheet("font-size: 15px;")
            self.stats_layout.addWidget(label)

        self.main_layout.addLayout(self.stats_layout)
        self.main_layout.addStretch()

        self.refresh()
    
    def refresh(self):
        stats = self.analytics_engine.get_summary_stats()

        self.total_label.setText(f"Total Tasks: {stats['total']}")
        self.completed_label.setText(f"Completed: {stats['completed']}")
        self.active_label.setText(f"Active: {stats['active']}")
        self.due_today_label.setText(f"Due Today: {stats['due_today']}")
        self.overdue_label.setText(f"Overdue: {stats['overdue']}")

        # Find next upcoming task
        tasks = self.task_manager.get_all_tasks(include_completed=True)

        from PyQt6.QtCore import QDate
        today = QDate.currentDate()

        upcoming = []

        for t in tasks:
            if t.get("due_date") and t["completed"] == 0:
                due = QDate.fromString(t["due_date"], "yyyy-MM-dd")
                if due > today:
                    upcoming.append((due.toJulianDay(), t))

        upcoming.sort(key=lambda x: x[0])
        next_task = upcoming[0][1]["title"] if upcoming else "None"

        self.next_task_label.setText(f"Next Upcoming: {next_task}")