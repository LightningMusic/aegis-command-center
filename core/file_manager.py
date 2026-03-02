import os
import string
from unittest import result
import uuid

from core.database import Database
from datetime import datetime
from core.drive_indexer import DriveIndexer
from core.organization_engine import OrganizationEngine
from datetime import datetime, timedelta





class FileManager:
    def __init__(self):
        self.db = Database()
        self.indexer = DriveIndexer()
        self.file_index = []
        self.organizer = None

    def _save_file_record(self, file_data):
        self.db.execute("""
            INSERT OR REPLACE INTO files (
                id,
                absolute_path,
                name,
                extension,
                size_bytes,
                modified_at,
                parent_directory,
                is_directory,
                depth
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            file_data["id"],
            file_data["absolute_path"],
            file_data["name"],
            file_data["extension"],
            file_data["size_bytes"],
            file_data["modified_at"],
            file_data["parent_directory"],
            0,
            file_data["depth"]
        ))

    def get_available_drives(self):
        drives = []
        for letter in string.ascii_uppercase:
            drive = f"{letter}:\\"
            if os.path.exists(drive):
                drives.append(drive)
        return drives


    def full_scan(self, root_path):
        files_indexed = []

        for root, dirs, files in os.walk(root_path):
            for name in files:
                try:
                    full_path = os.path.join(root, name)
                    size = os.path.getsize(full_path)
                    modified = datetime.fromtimestamp(
                        os.path.getmtime(full_path)
                    ).isoformat()

                    file_data = {
                        "id": str(uuid.uuid4()),
                        "absolute_path": full_path,
                        "name": name,
                        "extension": os.path.splitext(name)[1],
                        "size_bytes": size,
                        "modified_at": modified,
                        "parent_directory": root,
                        "depth": full_path.count(os.sep)
                    }

                    self._save_file_record(file_data)
                    files_indexed.append(file_data)

                except Exception as e:
                    print("Error indexing file:", e)
                    continue

        return files_indexed

    def get_largest_files(self, limit=10):
        return self.db.fetchall("""
            SELECT absolute_path, size_bytes
            FROM files
            WHERE is_directory = 0
            ORDER BY size_bytes DESC
            LIMIT ?
        """, (limit,))

    def get_duplicates(self):
        if not self.organizer:
            return []
        return self.organizer.find_duplicates()

    def get_extension_breakdown(self):
        return self.db.fetchall("""
            SELECT extension, COUNT(*), SUM(size_bytes)
            FROM files
            WHERE is_directory = 0
            GROUP BY extension
            ORDER BY COUNT(*) DESC
            LIMIT 15
        """)
    
    def get_indexed_file_count(self):
        result = self.db.fetchall("SELECT COUNT(*) FROM files")
        return result[0][0] if result else 0

    def get_total_storage_used(self):
        result = self.db.fetchall("""
            SELECT SUM(size_bytes) FROM files
            WHERE is_directory = 0
        """)
        total = result[0][0] if result and result[0][0] else 0
        return total
    
    def get_duplicate_files(self):
        return self.db.fetchall("""
            SELECT absolute_path, size_bytes, hash
            FROM files
            WHERE hash IS NOT NULL
            AND hash IN (
                SELECT hash
                FROM files
                WHERE hash IS NOT NULL
                GROUP BY hash
                HAVING COUNT(*) > 1
            )
            ORDER BY hash
        """)
    
    def get_storage_by_folder(self, limit=10):
        return self.db.fetchall("""
            SELECT parent_directory, SUM(size_bytes)
            FROM files
            WHERE is_directory = 0
            GROUP BY parent_directory
            ORDER BY SUM(size_bytes) DESC
            LIMIT ?
        """, (limit,))


    def get_steam_games_usage(self):
        return self.db.fetchall("""
            SELECT parent_directory, SUM(size_bytes)
            FROM files
            WHERE absolute_path LIKE '%Steam%steamapps%common%'
            GROUP BY parent_directory
            ORDER BY SUM(size_bytes) DESC
        """)



    def get_cleanup_suggestions(self):
        suggestions = []

        one_year_ago = (datetime.now() - timedelta(days=365)).isoformat()

        candidates = self.db.fetchall("""
            SELECT absolute_path, size_bytes, modified_at
            FROM files
            WHERE is_directory = 0
            AND size_bytes > ?
            AND modified_at < ?
            ORDER BY size_bytes DESC
            LIMIT 50
        """, (500 * 1024 * 1024, one_year_ago))  # >500MB and older than 1 year

        for path, size, modified in candidates:

            lower_path = path.lower()

            # 🚫 Exclusion rules
            if any(x in lower_path for x in [
                "windows",
                "program files",
                "programdata",
                "steamapps",
                "$recycle.bin",
                "system volume information"
            ]):
                continue

            if lower_path.endswith((".sys", ".dll", ".exe", ".vmdk", ".vdi", ".iso")):
                continue

            size_gb = round(size / (1024**3), 2)
            suggestions.append(f"{size_gb} GB — Possibly unused: {path}")

        return suggestions