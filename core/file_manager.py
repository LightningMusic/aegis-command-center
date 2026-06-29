import os
import re
import uuid
from datetime import datetime

from core.backup_manager import BackupManager
from core.database import Database
from core.drive_indexer import DriveIndexer
from core.storage_analyzer import StorageAnalyzer
from core.windows_drives import list_connected_drives
from modules.duplicate_detector import DuplicateDetector


def is_unmergeable_program_file(path: str) -> bool:
    if not path:
        return False
    path_lower = path.lower().replace("/", "\\")
    
    # 1. Check critical folders (windows, program files, programdata, appdata, etc.)
    protected_dirs = [
        "\\windows\\",
        "\\program files\\",
        "\\program files (x86)\\",
        "\\programdata\\",
        "\\appdata\\",
        "\\steamapps\\",
        "\\epic games\\",
        "\\system volume information\\",
        "\\$recycle.bin\\",
        "\\perflogs\\"
    ]
    if any(p_dir in f"\\{path_lower}\\" or p_dir in path_lower for p_dir in protected_dirs):
        return True
        
    # 2. Check critical extensions
    critical_extensions = {".dll", ".exe", ".sys", ".drv", ".ocx", ".msi", ".msp", ".cab", ".com"}
    _, ext = os.path.splitext(path_lower)
    if ext in critical_extensions:
        return True
        
    return False


class FileManager:
    def __init__(self, config_manager=None):
        self.db = Database()
        self.indexer = DriveIndexer()
        self.file_index = []
        self.organizer = None
        self.storage_analyzer = StorageAnalyzer()
        self.duplicate_detector = DuplicateDetector()
        self.backup_manager = BackupManager(config_manager) if config_manager else None

    def _save_file_record(self, file_data):
        self.db.execute(
            """
            INSERT INTO files (
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
            ON CONFLICT(absolute_path) DO UPDATE SET
                name = excluded.name,
                extension = excluded.extension,
                size_bytes = excluded.size_bytes,
                modified_at = excluded.modified_at,
                parent_directory = excluded.parent_directory,
                is_directory = excluded.is_directory,
                depth = excluded.depth,
                drive = excluded.drive,
                last_accessed = excluded.last_accessed,
                hash = CASE
                    WHEN files.size_bytes = excluded.size_bytes
                    AND files.modified_at = excluded.modified_at
                    THEN files.hash
                    ELSE NULL
                END
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

    def _save_file_records_batch(self, files_data):
        query = """
        INSERT INTO files (
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
        ON CONFLICT(absolute_path) DO UPDATE SET
            name = excluded.name,
            extension = excluded.extension,
            size_bytes = excluded.size_bytes,
            modified_at = excluded.modified_at,
            parent_directory = excluded.parent_directory,
            is_directory = excluded.is_directory,
            depth = excluded.depth,
            drive = excluded.drive,
            last_accessed = excluded.last_accessed,
            hash = CASE
                WHEN files.size_bytes = excluded.size_bytes
                AND files.modified_at = excluded.modified_at
                THEN files.hash
                ELSE NULL
            END
        """
        params = [
            (
                f["id"],
                f["absolute_path"],
                f["name"],
                f["extension"],
                f["size_bytes"],
                f["modified_at"],
                f["parent_directory"],
                0,
                f["depth"],
                f["drive"],
                f["last_accessed"],
            )
            for f in files_data
        ]
        self.db.execute_many(query, params)

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

    def delete_file(self, path):
        """Safely delete a file from disk and database, verifying it is not protected."""
        if not os.path.exists(path):
            # If it's already gone, just ensure it's removed from DB
            self.db.execute("DELETE FROM files WHERE absolute_path = ?", (path,))
            return True, "File already deleted."
            
        if is_unmergeable_program_file(path):
            return False, f"Permission Denied: '{path}' is a protected program or system file."
            
        try:
            os.remove(path)
            self.db.execute("DELETE FROM files WHERE absolute_path = ?", (path,))
            return True, "Success"
        except Exception as exc:
            return False, f"Failed to delete '{path}': {str(exc)}"

    def get_available_drives(self):
        return [item["root"] for item in self.get_drive_inventory() if item["is_scan_eligible"]]

    def get_drive_inventory(self):
        return list_connected_drives()

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

    def backup_phone(
        self,
        phone_mount_point: str,
        device_name: str,
        destination_root: str,
        progress_callback=None,
        should_stop=None,
    ):
        """
        Backup a USB-connected phone with intelligent file organization.
        
        Args:
            phone_mount_point: Mount point of the phone (e.g., "E:\\")
            device_name: Human-readable device name (e.g., "iPhone_Elijah")
            destination_root: Root backup destination
            progress_callback: Optional callback(progress, message)
            should_stop: Optional callable to check if should stop
            
        Returns:
            Backup results dictionary
        """
        from core.phone_backup import PhoneBackup
        
        phone_backup = PhoneBackup()
        return phone_backup.backup_phone(
            phone_mount_point,
            device_name,
            destination_root,
            organize=True,
            progress_callback=progress_callback,
            should_stop=should_stop,
        )
