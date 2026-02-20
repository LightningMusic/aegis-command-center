import sqlite3
from pathlib import Path
from typing import Optional


class Database:
    def __init__(self, db_name: str = "aegis.db"):
        self.db_path = Path(db_name)
        self.connection: Optional[sqlite3.Connection] = None

    def connect(self) -> sqlite3.Connection:
        if self.connection is None:
            self.connection = sqlite3.connect(self.db_path)
            self.connection.execute("PRAGMA foreign_keys = ON;")
        return self.connection

    def close(self):
        if self.connection is not None:
            self.connection.close()
            self.connection = None

    def initialize(self):
        conn = self.connect()
        cursor = conn.cursor()

        # Tasks Table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                description TEXT,
                category TEXT,
                importance INTEGER,
                estimated_minutes INTEGER,
                due_date TEXT,
                created_at TEXT,
                completed INTEGER DEFAULT 0,
                completed_at TEXT
            );
        """)

        # Study Sessions Table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS study_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id INTEGER,
                start_time TEXT,
                end_time TEXT,
                duration_minutes INTEGER,
                FOREIGN KEY(task_id) REFERENCES tasks(id)
            );
        """)

        # Monitored Folders Table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS monitored_folders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                path TEXT NOT NULL,
                sort_rule TEXT,
                active INTEGER DEFAULT 1
            );
        """)

        # File Logs Table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS file_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                original_path TEXT,
                new_path TEXT,
                action_type TEXT,
                timestamp TEXT,
                file_hash TEXT
            );
        """)

        conn.commit()
        print("Database initialized.")
