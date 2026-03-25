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
        self.protected_paths = [
            "windows",
            "program files",
            "steamapps",
            "vmware",
            "virtualbox"
        ]
        
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
                depth,
                drive,
                last_accessed
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            file_data["id"],
            file_data["absolute_path"],
            file_data["name"],
            file_data["extension"],
            file_data["size_bytes"],
            file_data["modified_at"],
            file_data["parent_directory"],
            0,
            file_data["depth"],
            file_data["drive"],
            file_data["last_accessed"]
        ))

    def remove_missing_files(self):
        records = self.db.fetchall("""
            SELECT id, absolute_path FROM files
        """)

        removed = 0

        for file_id, path in records:
            if not os.path.exists(path):
                self.db.execute("DELETE FROM files WHERE id = ?", (file_id,))
                removed += 1

        print(f"Removed {removed} missing files")

    def get_available_drives(self):
        drives = []
        for letter in string.ascii_uppercase:
            drive = f"{letter}:\\"
            if os.path.exists(drive):
                drives.append(drive)
        return drives


    def full_scan(self, root_path):

        files_indexed = []

        for entry in os.scandir(root_path):

            try:

                if entry.is_dir(follow_symlinks=False):

                    files_indexed += self.full_scan(entry.path)

                else:

                    stat = entry.stat()

                    file_data = {
                        "id": str(uuid.uuid4()),
                        "absolute_path": entry.path,
                        "name": entry.name,
                        "extension": os.path.splitext(entry.name)[1],
                        "size_bytes": stat.st_size,
                        "modified_at": datetime.fromtimestamp(stat.st_mtime).isoformat(),
                        "last_accessed": stat.st_atime,
                        "parent_directory": os.path.dirname(entry.path),
                        "depth": entry.path.count(os.sep)
                    }

                    self._save_file_record(file_data)

                    files_indexed.append(file_data)

            except PermissionError:
                pass
            except Exception as e:
                print("Scan error:", e)

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
    
    def get_duplicate_files(self, drive):
        return self.db.fetchall("""
            SELECT absolute_path, size_bytes
            FROM files
            WHERE drive = ?
            AND size_bytes IN (
                SELECT size_bytes
                FROM files
                WHERE drive = ?
                GROUP BY size_bytes
                HAVING COUNT(*) > 1
            )
            ORDER BY size_bytes DESC
            LIMIT 50
        """, (drive, drive))
    
    def get_storage_by_folder(self, drive, limit=10):
        return self.db.fetchall("""
            SELECT parent_directory, SUM(size_bytes)
            FROM files
            WHERE is_directory = 0 AND drive = ?
            GROUP BY parent_directory
            ORDER BY SUM(size_bytes) DESC
            LIMIT ?
        """, (drive, limit))


    def get_steam_games_usage(self, drive):
        results = self.db.fetchall("""
            SELECT absolute_path, size_bytes
            FROM files
            WHERE absolute_path LIKE '%steamapps%common%'
            AND drive = ?
        """, (drive,))

        games = {}

        for path, size in results:
            parts = path.lower().split("steamapps\\common\\")
            
            if len(parts) < 2:
                continue

            remainder = parts[1]
            game_name = remainder.split("\\")[0].strip()
            game_name = game_name.replace("_", " ").title()

            if game_name not in games:
                games[game_name] = 0

            games[game_name] += size

        # Convert to sorted list
        sorted_games = sorted(
            games.items(),
            key=lambda x: x[1],
            reverse=True
        )

        return sorted_games[:20]
    
    def get_cleanup_suggestions(self, drive):
        suggestions = []

        two_years_ago = (datetime.now() - timedelta(days=730)).isoformat()

        candidates = self.db.fetchall("""
            SELECT absolute_path, size_bytes, last_accessed
            FROM files
            WHERE is_directory = 0
            AND size_bytes > ?
            AND last_accessed < ?
            ORDER BY size_bytes DESC
            LIMIT 100
        """, (300 * 1024 * 1024, two_years_ago))  # >300MB unused 2+ years

        for path, size, last_accessed in candidates:
            lower = path.lower()

            # 🚫 Exclusions
            if any(x in lower for x in [
                "windows",
                "program files",
                "programdata",
                "steamapps",
                "$recycle.bin",
                "system volume information",
                "virtualbox",
                "vmware"
            ]):
                continue

            if lower.endswith((".sys", ".dll", ".exe", ".vmdk", ".vdi", ".iso")):
                continue

            # 🟢 Prefer user folders
            if not any(x in lower for x in [
                "downloads",
                "desktop",
                "documents",
                "videos",
                "pictures"
            ]):
                continue

            size_gb = round(size / (1024**3), 2)
            suggestions.append(
                f"{size_gb} GB — Unused for 2+ years: {path}"
            )

        return suggestions
    
    def get_top_folders(self):

        return self.db.fetchall("""

            SELECT parent_directory, SUM(size_bytes)

            FROM files

            GROUP BY parent_directory

            ORDER BY SUM(size_bytes) DESC

            LIMIT 20

        """)
    def get_filetype_storage(self):

        return self.db.fetchall("""

            SELECT extension, COUNT(*), SUM(size_bytes)

            FROM files

            WHERE is_directory = 0

            GROUP BY extension

            ORDER BY SUM(size_bytes) DESC

            LIMIT 15

        """)