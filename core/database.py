import sqlite3
from pathlib import Path


DB_PATH = Path("aegis.db")


class Database:
    def __init__(self):
        self.conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL;")
        self.create_tables()
        self._ensure_columns()

    def create_tables(self):
        self.conn.execute("""
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
        """)

        self.conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_parent_directory
        ON files(parent_directory);
        """)

        self.conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_size_bytes
        ON files(size_bytes);
        """)

        self.conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_modified_at
        ON files(modified_at);
        """)

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
        columns = [row[1] for row in cur.fetchall()]

        if "last_accessed" not in columns:
            cur.execute("ALTER TABLE files ADD COLUMN last_accessed TEXT")
            self.conn.commit()