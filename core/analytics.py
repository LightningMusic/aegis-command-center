from PyQt6.QtCore import QDate


class AnalyticsEngine:
    def __init__(self, task_manager):
        self.task_manager = task_manager

    def _get_tasks(self):
        return self.task_manager.get_all_tasks(include_completed=True)

    # -------------------------
    # BASIC SUMMARY
    # -------------------------

    def get_summary_stats(self):
        tasks = self._get_tasks()
        today = QDate.currentDate()

        total = len(tasks)
        completed = sum(1 for t in tasks if t["completed"] == 1)
        active = total - completed

        overdue = 0
        due_today = 0

        for t in tasks:
            if t.get("due_date") and t["completed"] == 0:
                due = QDate.fromString(t["due_date"], "yyyy-MM-dd")
                if due < today:
                    overdue += 1
                elif due == today:
                    due_today += 1

        completion_rate = round((completed / total) * 100, 1) if total else 0

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
        tasks = self._get_tasks()

        weekly = {}

        for t in tasks:
            created = QDate.fromString(t["created_at"][:10], "yyyy-MM-dd")
            week_number = created.weekNumber()[0]
            key = f"{created.year()}-W{week_number}"

            if key not in weekly:
                weekly[key] = {"created": 0, "completed": 0}

            weekly[key]["created"] += 1

            if t["completed"]:
                weekly[key]["completed"] += 1

        return weekly

    # -------------------------
    # MONTHLY BREAKDOWN
    # -------------------------

    def get_monthly_stats(self):
        tasks = self._get_tasks()

        monthly = {}

        for t in tasks:
            created = QDate.fromString(t["created_at"][:10], "yyyy-MM-dd")
            key = f"{created.year()}-{created.month():02d}"

            if key not in monthly:
                monthly[key] = {"created": 0, "completed": 0}

            monthly[key]["created"] += 1

            if t["completed"]:
                monthly[key]["completed"] += 1

        return monthly

    # -------------------------
    # AVERAGE COMPLETION TIME
    # -------------------------

    def get_average_completion_days(self):
        tasks = self._get_tasks()

        total_days = 0
        count = 0

        for t in tasks:
            if t["completed"] and t.get("completed_at"):
                created = QDate.fromString(t["created_at"][:10], "yyyy-MM-dd")
                completed = QDate.fromString(t["completed_at"][:10], "yyyy-MM-dd")

                days = created.daysTo(completed)
                total_days += days
                count += 1

        return round(total_days / count, 1) if count else 0

    # -------------------------
    # CURRENT COMPLETION STREAK
    # -------------------------

    def get_completion_streak(self):
        tasks = self._get_tasks()

        completed_dates = sorted(
            {
                t["completed_at"][:10]
                for t in tasks
                if t["completed"] and t.get("completed_at")
            },
            reverse=True,
        )

        streak = 0
        today = QDate.currentDate()

        for date_str in completed_dates:
            date = QDate.fromString(date_str, "yyyy-MM-dd")

            if date == today.addDays(-streak):
                streak += 1
            else:
                break

        return streak

    # -------------------------
    # NEXT 7 DAYS WORKLOAD
    # -------------------------

    def get_upcoming_7_days(self):
        tasks = self._get_tasks()
        today = QDate.currentDate()

        count = 0

        for t in tasks:
            if t.get("due_date") and not t["completed"]:
                due = QDate.fromString(t["due_date"], "yyyy-MM-dd")
                if 0 <= today.daysTo(due) <= 7:
                    count += 1

        return count