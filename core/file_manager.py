import os

from core.backup_manager import BackupManager
from core.database import Database
from core.storage_analyzer import StorageAnalyzer
from core.util import connected_drives
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
        self.storage_analyzer = StorageAnalyzer()
        self.duplicate_detector = DuplicateDetector()
        self.backup_manager = BackupManager(config_manager) if config_manager else None

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

    def _fetch_drive_records(self, drive, duplicates_only=False):
        if duplicates_only:
            # Only rows that could possibly be part of a duplicate group: either
            # already hashed, or sharing a size with another file on the drive.
            # For a drive full of unique-size files this cuts the set the
            # duplicate detector has to load into memory by orders of magnitude.
            query = """
                SELECT
                    absolute_path, name, extension, size_bytes,
                    modified_at, last_accessed, parent_directory, drive, hash
                FROM files
                WHERE is_directory = 0
                AND drive = ?
                AND size_bytes > 0
                AND (
                    hash IS NOT NULL
                    OR size_bytes IN (
                        SELECT size_bytes FROM files
                        WHERE is_directory = 0 AND drive = ? AND size_bytes > 0
                        GROUP BY size_bytes HAVING COUNT(*) > 1
                    )
                )
            """
            params = (drive, drive)
        else:
            query = """
                SELECT
                    absolute_path, name, extension, size_bytes,
                    modified_at, last_accessed, parent_directory, drive, hash
                FROM files
                WHERE is_directory = 0
                AND drive = ?
            """
            params = (drive,)

        rows = self.db.fetchall(query, params)

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

    def remove_missing_files(self, should_stop=None, progress_callback=None):
        """Drop rows whose file is gone from disk.

        Batched: one DELETE + commit per 500 stale rows instead of per row, so
        this can run over a large index without hammering the DB. Safe to call
        from a worker thread. Returns the number of rows removed.
        """
        records = self.db.fetchall("SELECT id, absolute_path FROM files")

        stale_ids = []
        removed = 0
        for index, (file_id, path) in enumerate(records):
            if should_stop and should_stop():
                break
            if not os.path.exists(path):
                stale_ids.append(file_id)
            if len(stale_ids) >= 500:
                self._delete_file_ids(stale_ids)
                removed += len(stale_ids)
                stale_ids = []
                if progress_callback:
                    progress_callback(f"Removed {removed} missing files")
            elif progress_callback and index % 5000 == 0:
                progress_callback(f"Checking index ({index}/{len(records)})")

        if stale_ids:
            self._delete_file_ids(stale_ids)
            removed += len(stale_ids)

        return removed

    def _delete_file_ids(self, file_ids):
        if not file_ids:
            return
        placeholders = ",".join("?" for _ in file_ids)
        self.db.execute(
            f"DELETE FROM files WHERE id IN ({placeholders})",
            tuple(file_ids),
        )

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

    def get_available_drives(self, include_network=False):
        # Network drives are scan-eligible but recursively walking one over SMB
        # can take hours and stalls on a disconnected share, so they're left out
        # of "All Connected Drives" unless explicitly requested.
        return [
            item["root"]
            for item in self.get_drive_inventory()
            if item["is_scan_eligible"]
            and (include_network or item.get("type_name") != "Network")
        ]

    def get_drive_inventory(self):
        return connected_drives()

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
        records = self._fetch_drive_records(drive, duplicates_only=True)
        return self.duplicate_detector.build_duplicate_groups(records, limit=20)

    def get_drive_dashboard(self, drive):
        """Everything the File Organizer drive tabs need, computed in one pass.

        Meant to be called from a worker thread: the UI then just formats the
        returned dict. Duplicates and cleanup suggestions are computed once here
        instead of three times across the separate render calls.
        """
        result = self.db.fetchall(
            """
            SELECT COUNT(*), COALESCE(SUM(size_bytes), 0)
            FROM files
            WHERE is_directory = 0 AND drive = ?
            """,
            (drive,),
        )
        file_count, total_size = result[0] if result else (0, 0)

        duplicates = self.get_duplicate_files(drive)
        cleanup = self.get_cleanup_suggestions(drive)

        drive_meta = next(
            (item for item in self.get_drive_inventory() if item["root"] == drive),
            None,
        )

        return {
            "drive": drive,
            "drive_meta": drive_meta,
            "file_count": file_count,
            "total_size_bytes": total_size,
            "duplicates": duplicates,
            "duplicate_reclaimable_bytes": sum(
                group["reclaimable_bytes"] for group in duplicates
            ),
            "cleanup": cleanup,
            "cleanup_candidate_bytes": sum(item["size_bytes"] for item in cleanup),
        }

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
        """(name, size_bytes) pairs, biggest first. Uses the registry + Steam
        manifests directly — the file index skips Program Files."""
        from core.app_scanner import steam_games

        return [(g["name"], g["size_bytes"]) for g in steam_games(drive)]

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

