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
        

    def run(self):
        total_indexed = 0
        batch_counter = 0

        for drive in self.drives:
            self.status.emit(f"Scanning {drive}")

            for root, dirs, files in os.walk(drive, topdown=True):
                # Exclude protected/system directories
                excluded_dirs = {
                    "Windows",
                    "System Volume Information",
                    "$Recycle.Bin",
                    "PerfLogs"
                }
                dirs[:] = [
                    d for d in dirs
                    if d not in excluded_dirs
                    and not d.startswith(".")
                ]
                for name in files:
                    if not self._running:
                        self.finished.emit(total_indexed)
                        return

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

                        self.file_manager._save_file_record(file_data)

                        total_indexed += 1
                        batch_counter += 1

                        if batch_counter >= 500:
                            self.progress.emit(total_indexed)
                            batch_counter = 0

                    except Exception:
                        continue

        self.finished.emit(total_indexed)

    def stop(self):
        self._running = False

    def calculate_hash(self, file_path):
        hash_sha256 = hashlib.sha256()
        try:
            with open(file_path, "rb") as f:
                for chunk in iter(lambda: f.read(8192), b""):
                    hash_sha256.update(chunk)
            return hash_sha256.hexdigest()
        except:
            return None