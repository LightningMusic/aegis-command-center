from datetime import datetime
from typing import List, Dict, Optional

from core.database import Database


class TaskManager:
    def __init__(self, db: Database):
        self.db = db

    # -------------------------
    # CREATE
    # -------------------------

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

        created_at = datetime.now().isoformat()

        conn = self.db.connect()
        cursor = conn.cursor()

        cursor.execute(
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

        conn.commit()
        task_id = cursor.lastrowid
        if task_id is None:
            raise RuntimeError("Failed to retrieve task ID after insertion.")
        return int(task_id)

    # -------------------------
    # READ
    # -------------------------

    def get_all_tasks(self, include_completed: bool = True) -> List[Dict]:
        conn = self.db.connect()
        cursor = conn.cursor()

        if include_completed:
            cursor.execute("SELECT * FROM tasks ORDER BY created_at DESC")
        else:
            cursor.execute(
                "SELECT * FROM tasks WHERE completed = 0 ORDER BY created_at DESC"
            )

        rows = cursor.fetchall()
        columns = [col[0] for col in cursor.description]

        return [dict(zip(columns, row)) for row in rows]

    # -------------------------
    # COMPLETE
    # -------------------------

    def mark_completed(self, task_id: int):
        completed_at = datetime.now().isoformat()

        conn = self.db.connect()
        cursor = conn.cursor()

        cursor.execute(
            """
            UPDATE tasks
            SET completed = 1,
                completed_at = ?
            WHERE id = ?
            """,
            (completed_at, task_id),
        )

        conn.commit()

    # -------------------------
    # UPDATE
    # -------------------------

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

        conn = self.db.connect()
        cursor = conn.cursor()

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
            SET {', '.join(fields)}
            WHERE id = ?
        """

        cursor.execute(query, values)
        conn.commit()

    # -------------------------
    # DELETE
    # -------------------------

    def delete_task(self, task_id: int):
        conn = self.db.connect()
        cursor = conn.cursor()

        cursor.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        conn.commit()
