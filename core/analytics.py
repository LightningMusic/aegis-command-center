from PyQt6.QtCore import QDate

from datetime import datetime, timedelta

class AnalyticsEngine:
    def __init__(self, task_manager):
        self.task_manager = task_manager

    def _get_tasks(self):
        return self.task_manager.get_all_tasks(include_completed=True)

    # -------------------------
    # BASIC SUMMARY
    # -------------------------

    def get_summary_stats(self):
        tasks = self.task_manager.get_all_tasks() or []

        total = len(tasks)
        completed = sum(1 for t in tasks if t.completed)
        active = sum(1 for t in tasks if not t.completed)
        overdue = sum(1 for t in tasks if not t.completed and t.is_overdue())
        due_today = sum(1 for t in tasks if t.is_due_today())

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

    # -------------------------
    # WEEKLY BREAKDOWN
    # -------------------------

    def get_weekly_stats(self):
        tasks = self.task_manager.get_all_tasks() or []
        weekly = {}

        for task in tasks:
            week_key = task.created_at.strftime("%Y-W%U")

            if week_key not in weekly:
                weekly[week_key] = {"created": 0, "completed": 0}

            weekly[week_key]["created"] += 1

            if task.completed:
                weekly[week_key]["completed"] += 1

        return weekly

    # -------------------------
    # MONTHLY BREAKDOWN
    # -------------------------

    def get_monthly_stats(self):
        tasks = self.task_manager.get_all_tasks() or []
        monthly = {}

        for task in tasks:
            month_key = task.created_at.strftime("%Y-%m")

            if month_key not in monthly:
                monthly[month_key] = {"created": 0, "completed": 0}

            monthly[month_key]["created"] += 1

            if task.completed:
                monthly[month_key]["completed"] += 1

        return monthly

    # -------------------------
    # AVERAGE COMPLETION TIME
    # -------------------------

    def get_average_completion_days(self):
        tasks = self.task_manager.get_all_tasks() or []
        completed_tasks = [t for t in tasks if t.completed]

        if not completed_tasks:
            return 0

        total_days = sum(
            (t.completed_at - t.created_at).days
            for t in completed_tasks
            if t.completed_at and t.created_at
        )

        return round(total_days / len(completed_tasks), 1)

    # -------------------------
    # CURRENT COMPLETION STREAK
    # -------------------------

    def get_completion_streak(self):
        tasks = self.task_manager.get_all_tasks() or []
        completed_dates = sorted(
            {t.completed_at.date() for t in tasks if t.completed and t.completed_at},
            reverse=True
        )

        if not completed_dates:
            return 0

        streak = 1
        for i in range(1, len(completed_dates)):
            if (completed_dates[i - 1] - completed_dates[i]).days == 1:
                streak += 1
            else:
                break

        return streak

    # -------------------------
    # NEXT 7 DAYS WORKLOAD
    # -------------------------



    def get_upcoming_7_days(self):
        tasks = self.task_manager.get_all_tasks() or []
        now = datetime.now()
        future = now + timedelta(days=7)

        return sum(
            1 for t in tasks
            if not t.completed and t.due_date and now <= t.due_date <= future
        )