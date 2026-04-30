import getpass
import hashlib
import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from PyQt6.QtCore import QObject, pyqtSignal


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
        self.hash_pool = ThreadPoolExecutor(max_workers=4)
        self._seen_directories = set()

    def _hash_and_store(self, path, size):
        file_hash = self.calculate_hash(path, size)
        if file_hash:
            self.file_manager.db.execute(
                """
                UPDATE files
                SET hash = ?
                WHERE absolute_path = ?
                """,
                (file_hash, path),
            )

    def _build_roots(self, drive):
        roots = []
        user_path = os.path.join(drive, "Users", getpass.getuser())

        if os.path.exists(user_path):
            roots.append(user_path)

        roots.append(drive)
        return roots

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

    def run(self):
        total_indexed = 0
        batch_counter = 0
        interrupted = False

        try:
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

                                    self.file_manager._save_file_record(file_data)
                                    self.hash_pool.submit(self._hash_and_store, entry.path, size)

                                    total_indexed += 1
                                    batch_counter += 1

                                    if total_indexed % 1000 == 0:
                                        print(f"Indexed {total_indexed} files...")

                                    if batch_counter >= 100:
                                        self.progress.emit(total_indexed)
                                        batch_counter = 0
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
        finally:
            self.hash_pool.shutdown(wait=True)
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
