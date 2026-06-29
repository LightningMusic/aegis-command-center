import hashlib
import os
import uuid
from datetime import datetime

from PyQt6.QtCore import QObject, QThread, pyqtSignal


EXCLUDED_DIRS = {
    "Windows",
    "System Volume Information",
    "$Recycle.Bin",
    "PerfLogs",
}

PROTECTED_PATH_KEYWORDS = (
    "windows\\",
    "program files",
    "programdata",
    "appdata\\local\\temp",
    "windows defender",
)

EXCLUDED_EXTENSIONS = {
    ".sys",
    ".vmdk",
    ".vdi",
    ".log",
}

EXCLUDED_FILES = {
    "pagefile.sys",
    "hiberfil.sys",
    "swapfile.sys",
}

FULL_HASH_MAX_SIZE = 1024 * 1024 * 1024
SAMPLED_HASH_CHUNK_SIZE = 64 * 1024


class ScanWorker(QObject):
    progress = pyqtSignal(int)
    status = pyqtSignal(str)
    finished = pyqtSignal(int)

    def __init__(self, file_manager, drives):
        super().__init__()
        self.file_manager = file_manager
        self.drives = drives
        self._running = True
        self._seen_directories = set()

    def _build_roots(self, drive):
        return [drive]

    def _should_skip_directory(self, entry):
        name_lower = entry.name.lower()
        if entry.name in EXCLUDED_DIRS or name_lower.startswith("."):
            return True

        try:
            if entry.is_symlink():
                return True

            attributes = entry.stat(follow_symlinks=False).st_file_attributes
            if attributes & 0x400:
                return True
        except AttributeError:
            pass
        except OSError as exc:
            print(f"Error occurred while processing directory {entry.path}: {exc}")
            return True

        return False

    def _should_skip_file(self, entry):
        path_lower = entry.path.lower()
        if any(keyword in path_lower for keyword in PROTECTED_PATH_KEYWORDS):
            return True

        name_lower = entry.name.lower()
        extension = os.path.splitext(entry.name)[1].lower()

        return name_lower in EXCLUDED_FILES or extension in EXCLUDED_EXTENSIONS

    def _save_hashes_batch(self, hash_updates):
        query = """
        UPDATE files
        SET hash = ?
        WHERE absolute_path = ?
        """
        self.file_manager.db.execute_many(query, hash_updates)

    def run(self):
        total_indexed = 0
        batch_files = []
        interrupted = False

        try:
            # Phase 1: Metadata scanning
            for drive in self.drives:
                if not drive.endswith("\\"):
                    drive = f"{drive}\\"

                self.status.emit(f"Scanning {drive}")

                if not os.path.exists(drive):
                    continue

                stack = self._build_roots(drive)

                while stack:
                    if not self._running:
                        interrupted = True
                        break

                    current_dir = stack.pop()
                    normalized_dir = os.path.normcase(os.path.normpath(current_dir))
                    if normalized_dir in self._seen_directories:
                        continue
                    self._seen_directories.add(normalized_dir)

                    try:
                        with os.scandir(current_dir) as entries:
                            for entry in entries:
                                if not self._running:
                                    interrupted = True
                                    break

                                try:
                                    if entry.is_dir(follow_symlinks=False):
                                        if self._should_skip_directory(entry):
                                            continue
                                        stack.append(entry.path)
                                        continue

                                    if not entry.is_file(follow_symlinks=False):
                                        continue

                                    if self._should_skip_file(entry):
                                        continue

                                    stat = entry.stat()
                                    size = stat.st_size

                                    file_data = {
                                        "id": str(uuid.uuid4()),
                                        "absolute_path": entry.path,
                                        "name": entry.name,
                                        "extension": os.path.splitext(entry.name)[1].lower(),
                                        "size_bytes": size,
                                        "modified_at": datetime.fromtimestamp(stat.st_mtime).isoformat(),
                                        "last_accessed": datetime.fromtimestamp(stat.st_atime).isoformat(),
                                        "parent_directory": current_dir,
                                        "depth": entry.path.count(os.sep),
                                        "drive": entry.path[:3],
                                    }

                                    batch_files.append(file_data)
                                    total_indexed += 1

                                    # Save in batches of 1000
                                    if len(batch_files) >= 1000:
                                        self.file_manager._save_file_records_batch(batch_files)
                                        batch_files = []
                                        self.progress.emit(total_indexed)

                                    # Throttle the scanner slightly to prevent freeze
                                    if total_indexed % 500 == 0:
                                        QThread.msleep(1)

                                except PermissionError:
                                    continue
                                except FileNotFoundError:
                                    continue
                                except Exception as exc:
                                    print(f"File processing error: {entry.path} - {exc}")
                                    continue

                            if interrupted:
                                break
                    except PermissionError as exc:
                        print(f"PermissionError at {current_dir}: {exc}")
                        continue
                    except FileNotFoundError as exc:
                        print(f"FileNotFoundError at {current_dir}: {exc}")
                        continue
                    except Exception as exc:
                        print(f"Unexpected error at {current_dir}: {exc}")
                        continue

                if interrupted:
                    break

            # Save any remaining indexed files
            if batch_files and self._running:
                self.file_manager._save_file_records_batch(batch_files)
                self.progress.emit(total_indexed)

            # Phase 2: Duplicate hashing (sequential, throttled)
            if self._running:
                self.status.emit("Analyzing files for duplicate size candidates...")
                query = """
                SELECT absolute_path, size_bytes
                FROM files
                WHERE size_bytes > 0 AND size_bytes IS NOT NULL AND hash IS NULL
                AND size_bytes IN (
                    SELECT size_bytes
                    FROM files
                    GROUP BY size_bytes
                    HAVING COUNT(*) > 1
                )
                """
                candidates = self.file_manager.db.fetchall(query)

                if candidates:
                    total_candidates = len(candidates)
                    self.status.emit(f"Hashing duplicates (0/{total_candidates})...")

                    hash_updates = []
                    for idx, (path, size) in enumerate(candidates):
                        if not self._running:
                            interrupted = True
                            break

                        file_hash = self.calculate_hash(path, size)
                        if file_hash:
                            hash_updates.append((file_hash, path))

                        # Save in batches of 100
                        if len(hash_updates) >= 100:
                            self._save_hashes_batch(hash_updates)
                            hash_updates = []

                        # Update status every 50 files
                        if idx % 50 == 0 or idx == total_candidates - 1:
                            self.status.emit(f"Hashing duplicate candidate {idx + 1} of {total_candidates}")

                        # Sleep between hash operations to avoid high disk utilization and freeze
                        QThread.msleep(2)

                    if hash_updates and self._running:
                        self._save_hashes_batch(hash_updates)

        finally:
            self.progress.emit(total_indexed)
            self.finished.emit(total_indexed)

    def stop(self):
        self._running = False

    def calculate_hash(self, file_path, size):
        if size <= FULL_HASH_MAX_SIZE:
            digest = hashlib.sha256()
            try:
                with open(file_path, "rb") as file_handle:
                    for chunk in iter(lambda: file_handle.read(8192), b""):
                        digest.update(chunk)
                return f"full:{digest.hexdigest()}"
            except OSError:
                return None

        digest = hashlib.sha256()
        try:
            with open(file_path, "rb") as file_handle:
                first_chunk = file_handle.read(SAMPLED_HASH_CHUNK_SIZE)
                digest.update(first_chunk)

                midpoint = max(size // 2 - SAMPLED_HASH_CHUNK_SIZE // 2, 0)
                file_handle.seek(midpoint)
                digest.update(file_handle.read(SAMPLED_HASH_CHUNK_SIZE))

                tail_start = max(size - SAMPLED_HASH_CHUNK_SIZE, 0)
                file_handle.seek(tail_start)
                digest.update(file_handle.read(SAMPLED_HASH_CHUNK_SIZE))
        except OSError:
            return None

        digest.update(str(size).encode("utf-8"))
        return f"sample:{digest.hexdigest()}"
