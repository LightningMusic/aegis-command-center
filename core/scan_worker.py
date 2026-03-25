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
            "programdata",
            "vmware",
            "virtualbox"
        ]
        

    # -------------------------

    def run(self):

        total_indexed = 0
        batch_counter = 0

        for drive in self.drives:

            # Ensure proper Windows path format
            if not drive.endswith("\\"):
                drive = drive + "\\"

            self.status.emit(f"Scanning {drive}")

            # Skip invalid drives
            if not os.path.exists(drive):
                continue

            # Start from safer, accessible directories
            current_user = os.getlogin()
            user_path = os.path.join(drive, "Users", current_user)

            stack = []

            # Start with user directory FIRST
            if os.path.exists(user_path):
                stack.append(user_path)

            # Then scan rest of drive AFTER
            stack.append(drive)
            

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

                                    try:
                                        # Skip Windows junctions / reparse points
                                        if entry.is_symlink():
                                            continue

                                        if hasattr(entry, "stat"):
                                            if entry.stat(follow_symlinks=False).st_file_attributes & 0x400:
                                                continue
                                    except Exception as e:
                                        print(f"Error occurred while processing directory {entry.path}: {e}")
                                        continue

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
                                path_lower = entry.path.lower()
                                drive_letter = entry.path[:3] 

                                # Skip protected system areas completely
                                if any(keyword in path_lower for keyword in [
                                    "windows\\",
                                    "program files",
                                    "programdata",
                                    "appdata\\local\\temp",
                                    "windows defender"
                                ]):
                                    continue
                                elif entry.is_file(follow_symlinks=False):

                                    name_lower = entry.name.lower()
                                    ext = os.path.splitext(entry.name)[1].lower()

                                    # Skip unwanted system / VM files
                                    excluded_extensions = {
                                        ".sys",
                                        ".vmdk",
                                        ".vdi",
                                        ".log"
                                    }

                                    excluded_files = {
                                        "pagefile.sys",
                                        "hiberfil.sys",
                                        "swapfile.sys"
                                    }

                                    if name_lower in excluded_files:
                                        continue

                                    if ext in excluded_extensions:
                                        continue

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
                                        "extension": ext,
                                        "size_bytes": size,
                                        "modified_at": modified,
                                        "last_accessed": last_accessed,
                                        "parent_directory": current_dir,
                                        "depth": entry.path.count(os.sep),
                                        "drive": drive_letter
                                    }

                                    self.file_manager._save_file_record(file_data)

                                    total_indexed += 1
                                    if total_indexed % 1000 == 0:
                                        print(f"Indexed {total_indexed} files...")
                                    batch_counter += 1

                                    if batch_counter >= 100:
                                        self.progress.emit(total_indexed)
                                        batch_counter = 0

                            except PermissionError:
                                continue
                            except FileNotFoundError:
                                continue
                            except Exception as e:
                                print(f"File processing error: {entry.path} — {e}")
                                continue

                except PermissionError as e:
                    print(f"PermissionError at {current_dir}: {e}")
                    continue

                except FileNotFoundError as e:
                    print(f"FileNotFoundError at {current_dir}: {e}")
                    continue

                except Exception as e:
                    print(f"Unexpected error at {current_dir}: {e}")
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