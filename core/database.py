import sqlite3
import threading
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BASE_DIR / "aegis.db"


class Database:
    """
    SQLite access with one connection *per thread*.

    The app runs background QThreads (scanning, backups, device detection) that
    hit the database while the GUI thread is also querying it. A single shared
    sqlite3 connection serializes every call behind one mutex, so a long read or
    write on a worker freezes the UI thread's next query. Giving each thread its
    own connection lets WAL readers run concurrently; writers still serialize at
    the file level, handled by busy_timeout instead of a hard lock.
    """

    def __init__(self):
        self._local = threading.local()
        # Touch the connection once on the creating thread and run the one-time
        # schema setup there.
        self.create_tables()
        self._ensure_columns()

    def _new_connection(self):
        conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30.0)
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA busy_timeout=30000;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        return conn

    @property
    def conn(self):
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._new_connection()
            self._local.conn = conn
        return conn

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

    def execute_many(self, query, param_list):
        cur = self.conn.cursor()
        cur.executemany(query, param_list)
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
        self._ensure_device_tables()

    def _ensure_device_tables(self):
        """Create device-related tables for backup organization."""
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS devices (
                id TEXT PRIMARY KEY,
                device_id TEXT UNIQUE NOT NULL,
                device_name TEXT NOT NULL,
                device_type TEXT NOT NULL,
                detected_name TEXT,
                last_backup TEXT,
                is_active INTEGER DEFAULT 1,
                created_at TEXT,
                metadata TEXT
            )
            """
        )

        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS device_mappings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                device_id TEXT NOT NULL,
                drive_letter TEXT,
                path TEXT,
                is_phone INTEGER DEFAULT 0,
                phone_os TEXT,
                auto_detected INTEGER DEFAULT 1,
                first_seen TEXT,
                FOREIGN KEY (device_id) REFERENCES devices(device_id)
            )
            """
        )

        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS device_backup_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                device_id TEXT NOT NULL,
                backup_path TEXT,
                backup_time TEXT,
                file_count INTEGER,
                size_bytes INTEGER,
                status TEXT,
                FOREIGN KEY (device_id) REFERENCES devices(device_id)
            )
            """
        )

        self.conn.commit()

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
            "external_id": "TEXT",
            "source": "TEXT",
        }

        for column_name, column_type in task_columns.items():
            if column_name not in columns:
                cur.execute(f"ALTER TABLE tasks ADD COLUMN {column_name} {column_type}")

        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_tasks_external_id ON tasks(external_id)"
        )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_tasks_source ON tasks(source)")

        self.conn.commit()
