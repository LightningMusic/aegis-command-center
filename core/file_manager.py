import os
import re
import string
import uuid
from datetime import datetime

from core.database import Database
from core.drive_indexer import DriveIndexer
from core.storage_analyzer import StorageAnalyzer
from modules.duplicate_detector import DuplicateDetector


class FileManager:
    def __init__(self):
        self.db = Database()
        self.indexer = DriveIndexer()
        self.file_index = []
        self.organizer = None
        self.storage_analyzer = StorageAnalyzer()
        self.duplicate_detector = DuplicateDetector()

    def _save_file_record(self, file_data):
        self.db.execute(
            """
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
            """,
            (
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
                file_data["last_accessed"],
            ),
        )

    def _fetch_drive_records(self, drive):
        rows = self.db.fetchall(
            """
            SELECT
                absolute_path,
                name,
                extension,
                size_bytes,
                modified_at,
                last_accessed,
                parent_directory,
                drive,
                hash
            FROM files
            WHERE is_directory = 0
            AND drive = ?
            """,
            (drive,),
        )

        records = []
        for row in rows:
            records.append(
                {
                    "absolute_path": row[0],
                    "name": row[1],
                    "extension": row[2] or "",
                    "size_bytes": row[3] or 0,
                    "modified_at": row[4],
                    "last_accessed": row[5],
                    "parent_directory": row[6],
                    "drive": row[7],
                    "hash": row[8],
                }
            )
        return records

    def remove_missing_files(self):
        records = self.db.fetchall(
            """
            SELECT id, absolute_path FROM files
            """
        )

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
                    files_indexed.extend(self.full_scan(entry.path))
                    continue

                stat = entry.stat()
                file_data = {
                    "id": str(uuid.uuid4()),
                    "absolute_path": entry.path,
                    "name": entry.name,
                    "extension": os.path.splitext(entry.name)[1].lower(),
                    "size_bytes": stat.st_size,
                    "modified_at": datetime.fromtimestamp(stat.st_mtime).isoformat(),
                    "last_accessed": datetime.fromtimestamp(stat.st_atime).isoformat(),
                    "parent_directory": os.path.dirname(entry.path),
                    "depth": entry.path.count(os.sep),
                    "drive": entry.path[:3],
                }

                self._save_file_record(file_data)
                files_indexed.append(file_data)
            except PermissionError:
                continue
            except Exception as exc:
                print("Scan error:", exc)

        return files_indexed

    def get_largest_files(self, limit=10):
        return self.db.fetchall(
            """
            SELECT absolute_path, size_bytes
            FROM files
            WHERE is_directory = 0
            ORDER BY size_bytes DESC
            LIMIT ?
            """,
            (limit,),
        )

    def get_duplicates(self):
        if not self.organizer:
            return []
        return self.organizer.find_duplicates()

    def get_extension_breakdown(self):
        return self.db.fetchall(
            """
            SELECT extension, COUNT(*), SUM(size_bytes)
            FROM files
            WHERE is_directory = 0
            GROUP BY extension
            ORDER BY COUNT(*) DESC
            LIMIT 15
            """
        )

    def get_indexed_file_count(self):
        result = self.db.fetchall("SELECT COUNT(*) FROM files")
        return result[0][0] if result else 0

    def get_total_storage_used(self):
        result = self.db.fetchall(
            """
            SELECT SUM(size_bytes) FROM files
            WHERE is_directory = 0
            """
        )
        total = result[0][0] if result and result[0][0] else 0
        return total

    def get_drive_overview(self, drive):
        result = self.db.fetchall(
            """
            SELECT COUNT(*), COALESCE(SUM(size_bytes), 0)
            FROM files
            WHERE is_directory = 0
            AND drive = ?
            """,
            (drive,),
        )
        file_count, total_size = result[0] if result else (0, 0)
        duplicates = self.get_duplicate_files(drive)
        cleanup = self.get_cleanup_suggestions(drive)

        return {
            "drive": drive,
            "file_count": file_count,
            "total_size_bytes": total_size,
            "duplicate_groups": len(duplicates),
            "duplicate_reclaimable_bytes": sum(group["reclaimable_bytes"] for group in duplicates),
            "cleanup_candidates": len(cleanup),
            "cleanup_candidate_bytes": sum(item["size_bytes"] for item in cleanup),
        }

    def get_duplicate_files(self, drive):
        records = self._fetch_drive_records(drive)
        return self.duplicate_detector.build_duplicate_groups(records, limit=20)

    def get_storage_by_folder(self, drive, limit=10):
        return self.db.fetchall(
            """
            SELECT parent_directory, SUM(size_bytes)
            FROM files
            WHERE is_directory = 0 AND drive = ?
            GROUP BY parent_directory
            ORDER BY SUM(size_bytes) DESC
            LIMIT ?
            """,
            (drive, limit),
        )

    def get_steam_games_usage(self, drive):
        steam_paths = self.db.fetchall(
            """
            SELECT absolute_path FROM files
            WHERE name = 'libraryfolders.vdf'
            AND drive = ?
            """,
            (drive,),
        )

        games = {}

        for (vdf_path,) in steam_paths:
            try:
                with open(vdf_path, "r", encoding="utf-8", errors="ignore") as file_handle:
                    content = file_handle.read()
                paths = re.findall(r'"path"\s+"([^"]+)"', content)

                for path in paths:
                    common_path = os.path.join(path, "steamapps", "common")
                    if not os.path.exists(common_path):
                        continue

                    for game in os.listdir(common_path):
                        game_path = os.path.join(common_path, game)
                        total = 0
                        for root, _, files in os.walk(game_path):
                            for file_name in files:
                                try:
                                    total += os.path.getsize(os.path.join(root, file_name))
                                except OSError:
                                    continue

                        games[game] = total
            except OSError:
                continue

        return sorted(games.items(), key=lambda item: item[1], reverse=True)

    def get_cleanup_suggestions(self, drive):
        rows = self.db.fetchall(
            """
            SELECT
                absolute_path,
                name,
                extension,
                size_bytes,
                modified_at,
                last_accessed,
                parent_directory,
                drive,
                hash
            FROM files
            WHERE is_directory = 0
            AND drive = ?
            AND size_bytes >= ?
            ORDER BY size_bytes DESC
            LIMIT 500
            """,
            (drive, 100 * 1024 * 1024),
        )

        records = []
        for row in rows:
            records.append(
                {
                    "absolute_path": row[0],
                    "name": row[1],
                    "extension": row[2] or "",
                    "size_bytes": row[3] or 0,
                    "modified_at": row[4],
                    "last_accessed": row[5],
                    "parent_directory": row[6],
                    "drive": row[7],
                    "hash": row[8],
                }
            )

        return self.storage_analyzer.build_cleanup_suggestions(records, limit=20)

    def get_top_folders(self):
        return self.db.fetchall(
            """
            SELECT parent_directory, SUM(size_bytes)
            FROM files
            GROUP BY parent_directory
            ORDER BY SUM(size_bytes) DESC
            LIMIT 20
            """
        )

    def get_filetype_storage(self):
        return self.db.fetchall(
            """
            SELECT extension, COUNT(*), SUM(size_bytes)
            FROM files
            WHERE is_directory = 0
            GROUP BY extension
            ORDER BY SUM(size_bytes) DESC
            LIMIT 15
            """
        )
