from datetime import datetime

from PyQt6.QtWidgets import QGroupBox, QLabel, QVBoxLayout, QWidget


def _format_due_date(value):
    if not value:
        return None

    try:
        return datetime.fromisoformat(value)
    except ValueError:
        try:
            return datetime.strptime(value, "%Y-%m-%d")
        except ValueError:
            return None


class DashboardView(QWidget):
    def __init__(self, task_manager, analytics_engine, brightspace_client):
        super().__init__()

        self.task_manager = task_manager
        self.analytics_engine = analytics_engine
        self.brightspace_client = brightspace_client

        self.main_layout = QVBoxLayout()
        self.main_layout.setContentsMargins(30, 30, 30, 30)
        self.main_layout.setSpacing(20)
        self.setLayout(self.main_layout)

        title = QLabel("Dashboard")
        title.setStyleSheet("font-size: 26px; font-weight: 600;")
        self.main_layout.addWidget(title)

        task_group = QGroupBox("Tasks")
        task_layout = QVBoxLayout()
        task_group.setLayout(task_layout)

        self.total_label = QLabel()
        self.completed_label = QLabel()
        self.active_label = QLabel()
        self.due_today_label = QLabel()
        self.overdue_label = QLabel()
        self.next_task_label = QLabel()

        for label in (
            self.total_label,
            self.completed_label,
            self.active_label,
            self.due_today_label,
            self.overdue_label,
            self.next_task_label,
        ):
            label.setStyleSheet("font-size: 15px;")
            task_layout.addWidget(label)

        self.main_layout.addWidget(task_group)

        brightspace_group = QGroupBox("Brightspace")
        brightspace_layout = QVBoxLayout()
        brightspace_group.setLayout(brightspace_layout)

        self.brightspace_status_label = QLabel()
        self.brightspace_site_label = QLabel()
        self.brightspace_org_label = QLabel()
        self.brightspace_user_label = QLabel()
        self.brightspace_sync_label = QLabel()

        for label in (
            self.brightspace_status_label,
            self.brightspace_site_label,
            self.brightspace_org_label,
            self.brightspace_user_label,
            self.brightspace_sync_label,
        ):
            label.setWordWrap(True)
            label.setStyleSheet("font-size: 15px;")
            brightspace_layout.addWidget(label)

        self.main_layout.addWidget(brightspace_group)
        self.main_layout.addStretch()

        self.refresh()

    def refresh(self):
        stats = self.analytics_engine.get_summary_stats()

        self.total_label.setText(f"Total Tasks: {stats['total']}")
        self.completed_label.setText(f"Completed: {stats['completed']}")
        self.active_label.setText(f"Active: {stats['active']}")
        self.due_today_label.setText(f"Due Today: {stats['due_today']}")
        self.overdue_label.setText(f"Overdue: {stats['overdue']}")

        tasks = self.task_manager.get_all_tasks(include_completed=True)
        upcoming = []
        for task in tasks:
            if task["completed"]:
                continue
            due = _format_due_date(task.get("due_date"))
            if due is not None:
                upcoming.append((due, task["title"]))

        upcoming.sort(key=lambda item: item[0])
        next_task = upcoming[0][1] if upcoming else "None"
        self.next_task_label.setText(f"Next Upcoming: {next_task}")

        brightspace = self.brightspace_client.get_dashboard_snapshot()
        if not brightspace["enabled"]:
            self.brightspace_status_label.setText("Status: Brightspace integration is disabled.")
        elif not brightspace["configured"]:
            self.brightspace_status_label.setText("Status: Brightspace is enabled but not configured yet.")
        else:
            self.brightspace_status_label.setText(
                f"Status: {brightspace['last_sync_status']}"
            )

        self.brightspace_site_label.setText(
            f"Site: {brightspace['base_url'] or 'Not set'}"
        )
        self.brightspace_org_label.setText(
            f"Org Unit ID: {brightspace['org_unit_id'] or 'Not set'}"
        )
        self.brightspace_user_label.setText(
            f"User: {brightspace['username'] or 'Not set'}"
        )
        self.brightspace_sync_label.setText(
            f"Last Checked: {brightspace['last_sync_at'] or 'Never'}"
        )
