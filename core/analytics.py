from datetime import date, datetime, timedelta


def _parse_iso_date(value):
    if not value:
        return None

    for parser in (datetime.fromisoformat,):
        try:
            parsed = parser(value)
            return parsed
        except ValueError:
            continue

    try:
        return datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        return None


class AnalyticsEngine:
    def __init__(self, task_manager):
        self.task_manager = task_manager

    def _get_tasks(self):
        return self.task_manager.get_all_tasks(include_completed=True) or []

    def get_summary_stats(self):
        tasks = self._get_tasks()
        today = date.today()

        total = len(tasks)
        completed = sum(1 for task in tasks if task["completed"])
        active = sum(1 for task in tasks if not task["completed"])

        overdue = 0
        due_today = 0
        for task in tasks:
            due_date = _parse_iso_date(task.get("due_date"))
            if not due_date or task["completed"]:
                continue
            if due_date.date() < today:
                overdue += 1
            if due_date.date() == today:
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

    def get_weekly_stats(self):
        tasks = self._get_tasks()
        weekly = {}

        for task in tasks:
            created_at = _parse_iso_date(task.get("created_at"))
            if not created_at:
                continue

            week_key = created_at.strftime("%Y-W%U")
            weekly.setdefault(week_key, {"created": 0, "completed": 0})
            weekly[week_key]["created"] += 1

            if task["completed"]:
                weekly[week_key]["completed"] += 1

        return weekly

    def get_monthly_stats(self):
        tasks = self._get_tasks()
        monthly = {}

        for task in tasks:
            created_at = _parse_iso_date(task.get("created_at"))
            if not created_at:
                continue

            month_key = created_at.strftime("%Y-%m")
            monthly.setdefault(month_key, {"created": 0, "completed": 0})
            monthly[month_key]["created"] += 1

            if task["completed"]:
                monthly[month_key]["completed"] += 1

        return monthly

    def get_average_completion_days(self):
        tasks = self._get_tasks()
        durations = []

        for task in tasks:
            if not task["completed"]:
                continue

            created_at = _parse_iso_date(task.get("created_at"))
            completed_at = _parse_iso_date(task.get("completed_at"))
            if created_at and completed_at:
                durations.append((completed_at - created_at).days)

        if not durations:
            return 0

        return round(sum(durations) / len(durations), 1)

    def get_completion_streak(self):
        tasks = self._get_tasks()
        completed_dates = sorted(
            {
                completed_at.date()
                for task in tasks
                for completed_at in [_parse_iso_date(task.get("completed_at"))]
                if task["completed"] and completed_at
            },
            reverse=True,
        )

        if not completed_dates:
            return 0

        streak = 1
        for index in range(1, len(completed_dates)):
            if (completed_dates[index - 1] - completed_dates[index]).days == 1:
                streak += 1
            else:
                break

        return streak

    def get_upcoming_7_days(self):
        tasks = self._get_tasks()
        now = datetime.now()
        future = now + timedelta(days=7)

        count = 0
        for task in tasks:
            if task["completed"]:
                continue
            due_date = _parse_iso_date(task.get("due_date"))
            if due_date and now <= due_date <= future:
                count += 1

        return count
