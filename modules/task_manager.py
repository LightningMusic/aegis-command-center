from datetime import datetime
from typing import Dict, List, Optional

from core.database import Database


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
        return {
            "id": row[0],
            "title": row[1],
            "description": row[2],
            "category": row[3],
            "importance": row[4],
            "estimated_minutes": row[5],
            "due_date": row[6],
            "created_at": row[7],
            "completed": row[8],
            "completed_at": row[9],
        }

    def get_all_tasks(self, include_completed: bool = True) -> List[Dict]:
        query = """
            SELECT
                id,
                title,
                description,
                category,
                importance,
                estimated_minutes,
                due_date,
                created_at,
                completed,
                completed_at
            FROM tasks
        """

        if not include_completed:
            query += " WHERE completed = 0"

        rows = self.db.fetchall(query)
        return [self._row_to_task(row) for row in rows]

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
