import os
import sys
from typing import List, Dict

from core.windows_drives import get_drive_map


class DriveIndexer:
    def __init__(self):
        self.index: List[Dict] = []

        self.excluded_paths = [
            "C:\\Windows",
            "C:\\Program Files\\WindowsApps",
            "C:\\ProgramData\\Microsoft"
        ]

    def is_excluded(self, path: str) -> bool:
        for excluded in self.excluded_paths:
            if path.startswith(excluded):
                return True
        return False

    def scan_drive(self, root="C:\\"):
        self.index.clear()

        print("Scanning drive...\n")

        drive_details = get_drive_map().get(root if root.endswith("\\") else f"{root}\\")
        if drive_details and not drive_details["is_scan_eligible"]:
            print(f"Skipping {root} because it is not scan eligible.")
            return self.index

        total_dirs = sum(len(dirs) for _, dirs, _ in os.walk(root))
        processed_dirs = 0
        file_count = 0

        for root_dir, dirs, files in os.walk(root):
            if self.is_excluded(root_dir):
                continue

            processed_dirs += 1
            percent = int((processed_dirs / max(total_dirs, 1)) * 100)

            for file in files:
                full_path = os.path.join(root_dir, file)

                try:
                    size = os.path.getsize(full_path)
                except Exception:
                    continue

                self.index.append({
                    "name": file,
                    "path": full_path,
                    "size": size,
                    "extension": os.path.splitext(file)[1].lower()
                })

                file_count += 1

            # Update progress on ONE line
            bar_length = 30
            filled = int(bar_length * percent / 100)
            bar = "#" * filled + "-" * (bar_length - filled)

            sys.stdout.write(
                f"\r[{bar}] {percent}% | Files: {file_count}"
            )
            sys.stdout.flush()

        print("\n\nScan complete.")
        print(f"Total files indexed: {file_count}\n")

        return self.index
