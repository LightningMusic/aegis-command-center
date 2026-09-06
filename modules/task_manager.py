from datetime import datetime
from typing import Dict, List, Optional

from core.database import Database

_TASK_COLUMNS = (
    "id",
    "title",
    "description",
    "category",
    "importance",
    "estimated_minutes",
    "due_date",
    "created_at",
    "completed",
    "completed_at",
    "external_id",
    "source",
)
_TASK_SELECT = ", ".join(_TASK_COLUMNS)


class TaskManager:
    def __init__(self, db: Database):
        self.db = db

    def create_task(
        self,
        title: str,
        description: str = "",
        category: str = "",
        importance: int = 1,
        estimated_minutes: Optional[int] = None,
        due_date: Optional[str] = None,
    ) -> int:
        if not title.strip():
            raise ValueError("Task title cannot be empty.")

        if importance < 1 or importance > 5:
            raise ValueError("Importance must be between 1 and 5.")

        created_at = datetime.now().isoformat(timespec="seconds")

        cursor = self.db.execute(
            """
            INSERT INTO tasks (
                title, description, category,
                importance, estimated_minutes,
                due_date, created_at, completed
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, 0)
            """,
            (
                title,
                description,
                category,
                importance,
                estimated_minutes,
                due_date,
                created_at,
            ),
        )

        task_id = cursor.lastrowid
        if task_id is None:
            raise RuntimeError("Failed to retrieve task ID after insertion.")

        return int(task_id)

    def _row_to_task(self, row) -> Dict:
        return dict(zip(_TASK_COLUMNS, row))

    def get_all_tasks(self, include_completed: bool = True) -> List[Dict]:
        query = f"SELECT {_TASK_SELECT} FROM tasks"

        if not include_completed:
            query += " WHERE completed = 0"

        rows = self.db.fetchall(query)
        return [self._row_to_task(row) for row in rows]

    def get_task_by_external_id(self, external_id: str) -> Optional[Dict]:
        rows = self.db.fetchall(
            f"SELECT {_TASK_SELECT} FROM tasks WHERE external_id = ? LIMIT 1",
            (external_id,),
        )
        return self._row_to_task(rows[0]) if rows else None

    def upsert_external_task(
        self,
        external_id: str,
        source: str,
        title: str,
        due_date: Optional[str] = None,
        description: str = "",
        category: str = "",
        importance: int = 1,
    ) -> str:
        """Insert or update a task owned by an external source (e.g. Brightspace).

        Matching is by ``external_id``. An existing task's completion state is
        never touched. Returns "created", "updated" or "unchanged".
        """
        title = (title or "").strip() or "Untitled item"
        existing = self.get_task_by_external_id(external_id)

        if existing is None:
            created_at = datetime.now().isoformat(timespec="seconds")
            self.db.execute(
                """
                INSERT INTO tasks (
                    title, description, category, importance,
                    due_date, created_at, completed, external_id, source
                )
                VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?)
                """,
                (title, description, category, importance, due_date,
                 created_at, external_id, source),
            )
            return "created"

        changed = (
            (existing.get("title") or "") != title
            or (existing.get("due_date") or "") != (due_date or "")
            or (existing.get("description") or "") != (description or "")
            or (existing.get("category") or "") != (category or "")
        )
        if changed:
            self.db.execute(
                """
                UPDATE tasks
                SET title = ?, description = ?, category = ?, due_date = ?
                WHERE external_id = ?
                """,
                (title, description, category, due_date, external_id),
            )
            return "updated"
        return "unchanged"

    def list_external_ids(self, source: str) -> List[str]:
        rows = self.db.fetchall(
            "SELECT external_id FROM tasks WHERE source = ? AND external_id IS NOT NULL",
            (source,),
        )
        return [row[0] for row in rows]

    def prune_external(self, source: str, keep_ids) -> int:
        """Delete not-yet-completed tasks from *source* whose external id is no
        longer present upstream. Completed ones are kept as history."""
        keep = set(keep_ids)
        removed = 0
        for external_id in self.list_external_ids(source):
            if external_id in keep:
                continue
            task = self.get_task_by_external_id(external_id)
            if task and not task["completed"]:
                self.db.execute(
                    "DELETE FROM tasks WHERE external_id = ?", (external_id,)
                )
                removed += 1
        return removed

    def mark_completed(self, task_id: int):
        completed_at = datetime.now().isoformat(timespec="seconds")
        self.db.execute(
            """
            UPDATE tasks
            SET completed = 1,
                completed_at = ?
            WHERE id = ?
            """,
            (completed_at, task_id),
        )

    def mark_incomplete(self, task_id: int):
        self.db.execute(
            """
            UPDATE tasks
            SET completed = 0,
                completed_at = NULL
            WHERE id = ?
            """,
            (task_id,),
        )

    def update_task(
        self,
        task_id: int,
        title: Optional[str] = None,
        description: Optional[str] = None,
        category: Optional[str] = None,
        importance: Optional[int] = None,
        estimated_minutes: Optional[int] = None,
        due_date: Optional[str] = None,
    ):
        if importance is not None and (importance < 1 or importance > 5):
            raise ValueError("Importance must be between 1 and 5.")

        fields = []
        values = []

        if title is not None:
            fields.append("title = ?")
            values.append(title)

        if description is not None:
            fields.append("description = ?")
            values.append(description)

        if category is not None:
            fields.append("category = ?")
            values.append(category)

        if importance is not None:
            fields.append("importance = ?")
            values.append(importance)

        if estimated_minutes is not None:
            fields.append("estimated_minutes = ?")
            values.append(estimated_minutes)

        if due_date is not None:
            fields.append("due_date = ?")
            values.append(due_date)

        if not fields:
            return

        values.append(task_id)
        query = f"""
            UPDATE tasks
            SET {", ".join(fields)}
            WHERE id = ?
        """
        self.db.execute(query, values)

    def delete_task(self, task_id: int):
        self.db.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
