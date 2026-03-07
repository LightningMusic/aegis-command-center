from PyQt6.QtCore import QObject, pyqtSignal
import os
from datetime import datetime
import uuid
import hashlib


class ScanWorker(QObject):

    progress = pyqtSignal(int)
    status = pyqtSignal(str)
    finished = pyqtSignal(int)

    def __init__(self, file_manager, drives):
        super().__init__()

        self.file_manager = file_manager
        self.drives = drives
        self._running = True

        # directories we NEVER scan
        self.excluded_dirs = {
            "Windows",
            "System Volume Information",
            "$Recycle.Bin",
            "PerfLogs"
        }

        # directories we avoid indexing for cleanup suggestions
        self.protected_keywords = [
            "program files",
            "steamapps",
            "vmware",
            "virtualbox"
        ]

    # -------------------------

    def run(self):

        total_indexed = 0
        batch_counter = 0

        for drive in self.drives:

            self.status.emit(f"Scanning {drive}")

            stack = [drive]

            while stack:

                if not self._running:
                    self.finished.emit(total_indexed)
                    return

                current_dir = stack.pop()

                try:
                    with os.scandir(current_dir) as entries:

                        for entry in entries:

                            if not self._running:
                                self.finished.emit(total_indexed)
                                return

                            try:

                                # -------------------------
                                # DIRECTORY
                                # -------------------------

                                if entry.is_dir(follow_symlinks=False):

                                    name_lower = entry.name.lower()

                                    if (
                                        entry.name in self.excluded_dirs
                                        or name_lower.startswith(".")
                                    ):
                                        continue

                                    stack.append(entry.path)

                                # -------------------------
                                # FILE
                                # -------------------------

                                elif entry.is_file(follow_symlinks=False):

                                    stat = entry.stat()

                                    size = stat.st_size
                                    modified = datetime.fromtimestamp(
                                        stat.st_mtime
                                    ).isoformat()

                                    last_accessed = datetime.fromtimestamp(
                                        stat.st_atime
                                    ).isoformat()

                                    file_data = {

                                        "id": str(uuid.uuid4()),
                                        "absolute_path": entry.path,
                                        "name": entry.name,
                                        "extension": os.path.splitext(entry.name)[1],
                                        "size_bytes": size,
                                        "modified_at": modified,
                                        "last_accessed": last_accessed,
                                        "parent_directory": current_dir,
                                        "depth": entry.path.count(os.sep)

                                    }

                                    self.file_manager._save_file_record(
                                        file_data,
                                        last_accessed
                                    )

                                    total_indexed += 1
                                    batch_counter += 1

                                    if batch_counter >= 500:
                                        self.progress.emit(total_indexed)
                                        batch_counter = 0

                            except PermissionError:
                                continue
                            except FileNotFoundError:
                                continue
                            except Exception:
                                continue

                except PermissionError:
                    continue
                except FileNotFoundError:
                    continue

        self.finished.emit(total_indexed)

    # -------------------------

    def stop(self):
        self._running = False

    # -------------------------

    def calculate_hash(self, file_path):

        hash_sha256 = hashlib.sha256()

        try:
            with open(file_path, "rb") as f:
                for chunk in iter(lambda: f.read(8192), b""):
                    hash_sha256.update(chunk)

            return hash_sha256.hexdigest()

        except Exception:
            return None