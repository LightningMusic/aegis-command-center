from PyQt6.QtCore import QDate


class AnalyticsEngine:
    def __init__(self, task_manager):
        self.task_manager = task_manager

    def get_summary_stats(self):
        tasks = self.task_manager.get_all_tasks(include_completed=True)
        today = QDate.currentDate()

        total = len(tasks)
        completed = sum(1 for t in tasks if t["completed"] == 1)
        active = total - completed

        overdue = 0
        due_today = 0

        for t in tasks:
            if t.get("due_date") and not t["completed"]:
                due = QDate.fromString(t["due_date"], "yyyy-MM-dd")

                if due < today:
                    overdue += 1
                elif due == today:
                    due_today += 1

        completion_rate = 0
        if total > 0:
            completion_rate = round((completed / total) * 100, 1)

        return {
            "total": total,
            "completed": completed,
            "active": active,
            "overdue": overdue,
            "due_today": due_today,
            "completion_rate": completion_rate,
        }

    def get_weekly_breakdown(self):
        tasks = self.task_manager.get_all_tasks(include_completed=True)
        today = QDate.currentDate()

        weekly_data = {}

        for t in tasks:
            if not t.get("created_at"):
                continue

            created = QDate.fromString(t["created_at"][:10], "yyyy-MM-dd")
            week_key = f"{created.year()}-W{created.weekNumber()[0]}"

            if week_key not in weekly_data:
                weekly_data[week_key] = {
                    "created": 0,
                    "completed": 0,
                }

            weekly_data[week_key]["created"] += 1

            if t["completed"] == 1:
                weekly_data[week_key]["completed"] += 1

        return weekly_data