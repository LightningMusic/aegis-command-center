import os
import shutil
from pathlib import Path
from typing import Dict, List, Optional, Set

PHONE_CATEGORY_RULES = {
    "Photos": {
        "extensions": {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".heic", ".raw"},
        "folders": {"DCIM", "Pictures", "Camera", "Screenshots", "PHOTOS"},
    },
    "Videos": {
        "extensions": {".mp4", ".mkv", ".avi", ".mov", ".flv", ".wmv", ".webm", ".m4v"},
        "folders": {"DCIM", "Movies", "Videos", "Recordings"},
    },
    "Documents": {
        "extensions": {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".txt", ".odt", ".rtf"},
        "folders": {"Documents", "Docs"},
    },
    "Downloads": {
        "extensions": {".zip", ".rar", ".7z", ".tar", ".gz", ".exe", ".apk", ".ipa"},
        "folders": {"Downloads", "Download"},
    },
    "Audio": {
        "extensions": {".mp3", ".wav", ".flac", ".aac", ".ogg", ".m4a", ".wma", ".opus"},
        "folders": {"Music", "Audio", "Sounds", "Podcasts", "Music"},
    },
    "Messages": {
        "extensions": {".txt", ".msg", ".eml"},
        "folders": {"Messages", "SMS", "MMS", "Telegram", "WhatsApp"},
    },
}


class PhoneFileOrganizer:
    """Intelligently organizes phone backup files into categories."""

    def __init__(self, backup_destination: str):
        """
        Initialize the organizer.

        Args:
            backup_destination: Root path where phone files are backed up
        """
        self.backup_destination = Path(backup_destination)
        self.backup_destination.mkdir(parents=True, exist_ok=True)

    def organize_phone_backup(
        self,
        phone_source_path: str,
        device_name: str,
        progress_callback=None,
        should_stop=None,
    ) -> Dict:
        """
        Organize a phone backup into categorized folders.

        Args:
            phone_source_path: Path to the phone storage
            device_name: Name of the device (e.g., "iPhone_Elijah")
            progress_callback: Optional callback for progress updates
            should_stop: Optional callable to check if operation should stop

        Returns:
            Dictionary with organization results
        """
        device_backup_root = self.backup_destination / device_name / "latest"
        device_backup_root.mkdir(parents=True, exist_ok=True)

        phone_root = Path(phone_source_path)
        if not phone_root.exists():
            raise FileNotFoundError(f"Phone source path does not exist: {phone_source_path}")

        results = {
            "device_name": device_name,
            "source_path": str(phone_root),
            "destination_path": str(device_backup_root),
            "files_organized": 0,
            "files_failed": 0,
            "categories": {},
            "unorganized_files": [],
        }

        if should_stop and should_stop():
            return results

        all_files = self._collect_all_files(phone_root)
        total_files = len(all_files)

        for index, file_path in enumerate(all_files):
            if should_stop and should_stop():
                break

            if progress_callback:
                progress_callback(
                    int((index / max(total_files, 1)) * 100),
                    f"Organizing {file_path.name}",
                )

            try:
                category = self._categorize_file(file_path)
                if category:
                    self._copy_file_to_category(
                        file_path, device_backup_root, category
                    )
                    if category not in results["categories"]:
                        results["categories"][category] = 0
                    results["categories"][category] += 1
                    results["files_organized"] += 1
                else:
                    self._copy_file_to_category(
                        file_path, device_backup_root, "Miscellaneous"
                    )
                    results["unorganized_files"].append(str(file_path))
                    if "Miscellaneous" not in results["categories"]:
                        results["categories"]["Miscellaneous"] = 0
                    results["categories"]["Miscellaneous"] += 1
                    results["files_organized"] += 1

            except OSError:
                results["files_failed"] += 1

        if progress_callback:
            progress_callback(100, "Organization complete")

        return results

    def _collect_all_files(self, source_path: Path, max_depth: int = 10) -> List[Path]:
        """Recursively collect all files from source directory."""
        files = []
        try:
            for root, dirs, filenames in os.walk(str(source_path)):
                if should_stop_recursion(root, source_path, max_depth):
                    dirs.clear()
                    continue

                for filename in filenames:
                    file_path = Path(root) / filename
                    files.append(file_path)

        except PermissionError:
            pass

        return files

    def _categorize_file(self, file_path: Path) -> Optional[str]:
        """
        Determine the category for a file based on extension and location.

        Returns:
            Category name or None if uncategorized
        """
        extension = file_path.suffix.lower()
        parent_folder = file_path.parent.name.upper()

        for category, rules in PHONE_CATEGORY_RULES.items():
            if extension in rules["extensions"]:
                return category

            if parent_folder in rules["folders"]:
                return category

        return None

    def _copy_file_to_category(
        self, file_path: Path, backup_root: Path, category: str
    ) -> None:
        """Copy file to the appropriate category folder."""
        category_folder = backup_root / category
        category_folder.mkdir(parents=True, exist_ok=True)

        destination = category_folder / file_path.name

        if destination.exists():
            source_stat = file_path.stat()
            dest_stat = destination.stat()

            if (
                dest_stat.st_size == source_stat.st_size
                and int(dest_stat.st_mtime) >= int(source_stat.st_mtime)
            ):
                return

        try:
            shutil.copy2(file_path, destination)
        except OSError:
            raise

    def organize_existing_backup(
        self,
        backup_path: str,
        device_name: str,
        progress_callback=None,
        should_stop=None,
    ) -> Dict:
        """
        Reorganize an existing phone backup that wasn't organized.

        Args:
            backup_path: Path to the existing backup
            device_name: Name of the device

        Returns:
            Organization results
        """
        backup_root = Path(backup_path)
        if not backup_root.exists():
            raise FileNotFoundError(f"Backup path does not exist: {backup_path}")

        device_backup_root = self.backup_destination / device_name / "latest"

        results = {
            "device_name": device_name,
            "source_path": backup_path,
            "destination_path": str(device_backup_root),
            "files_reorganized": 0,
            "files_failed": 0,
            "categories": {},
        }

        if should_stop and should_stop():
            return results

        all_files = self._collect_all_files(backup_root)
        total_files = len(all_files)

        for index, file_path in enumerate(all_files):
            if should_stop and should_stop():
                break

            if progress_callback:
                progress_callback(
                    int((index / max(total_files, 1)) * 100),
                    f"Reorganizing {file_path.name}",
                )

            try:
                category = self._categorize_file(file_path)
                if not category:
                    category = "Miscellaneous"

                self._copy_file_to_category(
                    file_path, device_backup_root, category
                )

                if category not in results["categories"]:
                    results["categories"][category] = 0
                results["categories"][category] += 1
                results["files_reorganized"] += 1

            except OSError:
                results["files_failed"] += 1

        if progress_callback:
            progress_callback(100, "Reorganization complete")

        return results

    def get_organization_summary(self, device_name: str) -> Dict:
        """Get summary of how a device's backup is organized."""
        device_backup_root = self.backup_destination / device_name / "latest"

        if not device_backup_root.exists():
            return {"device_name": device_name, "exists": False, "categories": {}}

        categories = {}
        for category_folder in device_backup_root.iterdir():
            if category_folder.is_dir():
                file_count = len(list(category_folder.rglob("*")))
                total_size = sum(
                    f.stat().st_size
                    for f in category_folder.rglob("*")
                    if f.is_file()
                )
                categories[category_folder.name] = {
                    "file_count": file_count,
                    "size_bytes": total_size,
                }

        return {
            "device_name": device_name,
            "exists": True,
            "backup_path": str(device_backup_root),
            "categories": categories,
        }


def should_stop_recursion(current_path: str, source_path: Path, max_depth: int) -> bool:
    """Check if we've exceeded max recursion depth."""
    try:
        depth = len(Path(current_path).relative_to(source_path).parts)
        return depth > max_depth
    except ValueError:
        return False
