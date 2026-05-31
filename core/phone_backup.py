import os
import shutil
import json
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from modules.phone_file_organizer import PhoneFileOrganizer


class PhoneBackup:
    """Handles backup of USB-connected phones with intelligent file organization."""

    COMMON_PHONE_PATHS = [
        "DCIM",
        "Pictures",
        "Downloads",
        "Documents",
        "Music",
        "Videos",
        "Camera",
        "Screenshots",
        "Telegram",
        "WhatsApp",
        "Messages",
    ]

    def __init__(self):
        self.organizer = None

    def detect_connected_phones(self) -> List[Dict]:
        """
        Detect Android phones connected via USB to the computer.

        Returns:
            List of detected phone drives with metadata
        """
        detected_phones = []

        try:
            import psutil

            partitions = psutil.disk_partitions(all=True)
            for partition in partitions:
                if not os.path.exists(partition.mountpoint):
                    continue

                has_phone_structure = self._check_phone_structure(
                    partition.mountpoint
                )
                if has_phone_structure:
                    phone_info = {
                        "mount_point": partition.mountpoint,
                        "device": partition.device,
                        "fstype": partition.fstype,
                        "phone_name": self._detect_phone_name(
                            partition.mountpoint
                        ),
                        "available_space": shutil.disk_usage(
                            partition.mountpoint
                        ).free,
                    }
                    detected_phones.append(phone_info)

        except ImportError:
            pass

        return detected_phones

    def _check_phone_structure(self, mount_point: str) -> bool:
        """Check if a mounted drive has typical phone folder structure."""
        for folder in self.COMMON_PHONE_PATHS:
            folder_path = os.path.join(mount_point, folder)
            if os.path.isdir(folder_path):
                return True
        return False

    def _detect_phone_name(self, mount_point: str) -> str:
        """Try to detect phone name from device properties."""
        try:
            metadata_path = os.path.join(mount_point, ".android_secure")
            if os.path.exists(metadata_path):
                return "Android_Phone"

            build_prop = os.path.join(mount_point, "system", "build.prop")
            if os.path.exists(build_prop):
                with open(build_prop, "r", encoding="utf-8", errors="ignore") as f:
                    for line in f:
                        if "ro.product.model=" in line:
                            return line.split("=")[1].strip().replace(" ", "_")

        except Exception:
            pass

        return "Phone"

    def backup_phone(
        self,
        phone_mount_point: str,
        device_name: str,
        destination_root: str,
        organize: bool = True,
        progress_callback=None,
        should_stop=None,
    ) -> Dict:
        """
        Backup files from a USB-connected phone.

        Args:
            phone_mount_point: Mount point of the phone (e.g., "E:\\")
            device_name: Human-readable device name (e.g., "iPhone_Elijah")
            destination_root: Root backup destination
            organize: Whether to organize files by type
            progress_callback: Optional callback(progress, message)
            should_stop: Optional callable to check if should stop

        Returns:
            Backup results dictionary
        """
        phone_path = Path(phone_mount_point)
        if not phone_path.exists():
            raise FileNotFoundError(
                f"Phone mount point does not exist: {phone_mount_point}"
            )

        if not self.organizer:
            self.organizer = PhoneFileOrganizer(destination_root)

        backup_root = Path(destination_root)
        device_backup_root = backup_root / device_name / "latest"
        device_backup_root.mkdir(parents=True, exist_ok=True)

        backup_results = {
            "device_name": device_name,
            "phone_path": str(phone_path),
            "destination": str(device_backup_root),
            "started_at": datetime.now().isoformat(),
            "completed_at": None,
            "files_copied": 0,
            "files_failed": 0,
            "total_bytes": 0,
            "categories": {},
            "errors": [],
        }

        try:
            files_to_backup = self._collect_phone_files(phone_path)
            total_files = len(files_to_backup)

            for index, file_path in enumerate(files_to_backup):
                if should_stop and should_stop():
                    backup_results["cancelled"] = True
                    break

                if progress_callback:
                    progress = int((index / max(total_files, 1)) * 50)
                    progress_callback(progress, f"Copying {file_path.name}")

                try:
                    copied_size = self._copy_phone_file(
                        file_path, device_backup_root, organize
                    )
                    backup_results["files_copied"] += 1
                    backup_results["total_bytes"] += copied_size

                    if organize:
                        category = self._get_file_category(file_path)
                        if category not in backup_results["categories"]:
                            backup_results["categories"][category] = 0
                        backup_results["categories"][category] += 1

                except OSError as e:
                    backup_results["files_failed"] += 1
                    backup_results["errors"].append(
                        {
                            "file": str(file_path),
                            "error": str(e),
                        }
                    )

            if should_stop and should_stop():
                backup_results["completed_at"] = datetime.now().isoformat()
                return backup_results

            if organize:
                if progress_callback:
                    progress_callback(50, "Organizing files by type...")

                organize_results = self.organizer.organize_phone_backup(
                    str(phone_path),
                    device_name,
                    progress_callback=lambda p, m: progress_callback(
                        50 + int(p * 0.5), m
                    ),
                    should_stop=should_stop,
                )

                backup_results["organization"] = organize_results

            backup_results["completed_at"] = datetime.now().isoformat()

            log_path = backup_root / "Aegis_Backups/logs"
            log_path.mkdir(parents=True, exist_ok=True)
            log_file = log_path / f"phone_backup_{device_name}_{datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}.json"

            with open(log_file, "w", encoding="utf-8") as f:
                json.dump(backup_results, f, indent=2)

            backup_results["log_file"] = str(log_file)

        except Exception as e:
            backup_results["completed_at"] = datetime.now().isoformat()
            backup_results["errors"].append(
                {
                    "phase": "backup",
                    "error": str(e),
                }
            )

        if progress_callback:
            progress_callback(100, "Phone backup complete")

        return backup_results

    def _collect_phone_files(self, phone_path: Path) -> List[Path]:
        """Collect all files from phone that should be backed up."""
        files = []

        try:
            for folder in self.COMMON_PHONE_PATHS:
                folder_path = phone_path / folder
                if not folder_path.exists():
                    continue

                for root, dirs, filenames in os.walk(str(folder_path)):
                    for filename in filenames:
                        file_path = Path(root) / filename
                        if file_path.is_file():
                            files.append(file_path)

        except PermissionError:
            pass

        return files

    def _copy_phone_file(
        self, file_path: Path, destination_root: Path, organize: bool
    ) -> int:
        """
        Copy a file from phone to backup destination.

        Args:
            file_path: Source file path
            destination_root: Destination directory
            organize: If True, organize into category folders

        Returns:
            Number of bytes copied
        """
        if organize:
            category = self._get_file_category(file_path)
            dest_dir = destination_root / category
        else:
            dest_dir = destination_root / file_path.parent.name

        dest_dir.mkdir(parents=True, exist_ok=True)
        dest_file = dest_dir / file_path.name

        if dest_file.exists():
            source_stat = file_path.stat()
            dest_stat = dest_file.stat()

            if (
                dest_stat.st_size == source_stat.st_size
                and int(dest_stat.st_mtime) >= int(source_stat.st_mtime)
            ):
                return 0

        shutil.copy2(file_path, dest_file)
        return file_path.stat().st_size

    def _get_file_category(self, file_path: Path) -> str:
        """Determine the category for a phone file."""
        from modules.phone_file_organizer import PHONE_CATEGORY_RULES

        extension = file_path.suffix.lower()
        parent_folder = file_path.parent.name.upper()

        for category, rules in PHONE_CATEGORY_RULES.items():
            if extension in rules["extensions"]:
                return category

            if parent_folder in rules["folders"]:
                return category

        return "Miscellaneous"
