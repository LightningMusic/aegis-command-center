import sqlite3
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BASE_DIR / "aegis.db"


class Database:
    def __init__(self):
        self.conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL;")
        self.create_tables()
        self._ensure_columns()

    def create_tables(self):
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS files (
                id TEXT PRIMARY KEY,
                absolute_path TEXT UNIQUE,
                name TEXT,
                extension TEXT,
                size_bytes INTEGER,
                created_at TEXT,
                modified_at TEXT,
                last_seen TEXT,
                last_accessed TEXT,
                parent_directory TEXT,
                is_directory INTEGER,
                hash TEXT,
                depth INTEGER
            );
            """
        )

        self.conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_parent_directory
            ON files(parent_directory);
            """
        )

        self.conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_size_bytes
            ON files(size_bytes);
            """
        )

        self.conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_modified_at
            ON files(modified_at);
            """
        )

        self.execute(
            """
            CREATE TABLE IF NOT EXISTS scan_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scan_time TEXT,
                total_files INTEGER,
                total_size INTEGER
            )
            """
        )

        self.execute(
            """
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                description TEXT DEFAULT '',
                category TEXT DEFAULT '',
                importance INTEGER DEFAULT 1,
                estimated_minutes INTEGER,
                due_date TEXT,
                created_at TEXT,
                completed INTEGER DEFAULT 0,
                completed_at TEXT
            )
            """
        )

        self.conn.commit()

    def execute(self, query, params=()):
        cur = self.conn.cursor()
        cur.execute(query, params)
        self.conn.commit()
        return cur

    def fetchall(self, query, params=()):
        cur = self.conn.cursor()
        cur.execute(query, params)
        return cur.fetchall()

    def _ensure_columns(self):
        cur = self.conn.cursor()
        cur.execute("PRAGMA table_info(files)")
        columns = {row[1] for row in cur.fetchall()}

        if "last_accessed" not in columns:
            cur.execute("ALTER TABLE files ADD COLUMN last_accessed TEXT")

        if "drive" not in columns:
            cur.execute("ALTER TABLE files ADD COLUMN drive TEXT")

        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_drive
            ON files(drive);
            """
        )

        self.conn.commit()

        self._ensure_task_columns()

    def _ensure_task_columns(self):
        cur = self.conn.cursor()
        cur.execute("PRAGMA table_info(tasks)")
        columns = {row[1] for row in cur.fetchall()}

        task_columns = {
            "description": "TEXT DEFAULT ''",
            "category": "TEXT DEFAULT ''",
            "importance": "INTEGER DEFAULT 1",
            "estimated_minutes": "INTEGER",
            "due_date": "TEXT",
            "created_at": "TEXT",
            "completed": "INTEGER DEFAULT 0",
            "completed_at": "TEXT",
        }

        for column_name, column_type in task_columns.items():
            if column_name not in columns:
                cur.execute(f"ALTER TABLE tasks ADD COLUMN {column_name} {column_type}")

        self.conn.commit()
