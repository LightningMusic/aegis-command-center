import os
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional


ProgressCallback = Callable[[int, str], None]
StopCallback = Callable[[], bool]


PHONE_CATEGORY_RULES: Dict[str, Dict[str, set[str]]] = {
    "Private_Files": {
        "extensions": {
            ".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic",
            ".mp4", ".mov", ".pdf", ".doc", ".docx", ".txt",
        },
        "folders": {
            "SAFE", "VAULT", "SECURE", "SECUREFOLDER",
            "PRIVATE", "PRIVATEFILES",
        },
    },
    "Photos": {
        "extensions": {
            ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp",
            ".svg", ".tiff", ".raw", ".heic",
        },
        "folders": {
            "DCIM", "CAMERA", "PICTURES", "PHOTOS",
            "SCREENSHOT", "SCREENSHOTS",
        },
    },
    "Videos": {
        "extensions": {
            ".mp4", ".avi", ".mkv", ".mov", ".flv", ".wmv",
            ".webm", ".m4v", ".3gp", ".ts",
        },
        "folders": {
            "VIDEOS", "MOVIES", "RECORDINGS", "DCIM", "CAMERA",
        },
    },
    "Documents": {
        "extensions": {
            ".pdf", ".doc", ".docx", ".txt", ".xls", ".xlsx",
            ".ppt", ".pptx", ".odt", ".rtf",
        },
        "folders": {
            "DOCUMENTS", "DOCS",
        },
    },
    "Downloads": {
        "extensions": {
            ".zip", ".rar", ".7z", ".tar", ".gz",
            ".exe", ".apk", ".ipa", ".bin",
        },
        "folders": {
            "DOWNLOADS", "DOWNLOAD",
        },
    },
    "Audio": {
        "extensions": {
            ".mp3", ".wav", ".flac", ".aac", ".ogg",
            ".wma", ".m4a", ".aiff", ".opus",
        },
        "folders": {
            "MUSIC", "AUDIO", "SOUNDS", "PODCASTS",
            "RINGTONES", "ALARMS", "NOTIFICATIONS",
        },
    },
    "Messages": {
        "extensions": {
            ".msg", ".eml", ".vcf", ".vcard", ".json",
        },
        "folders": {
            "MESSAGES", "SMS", "MMS", "TELEGRAM",
            "WHATSAPP", "VIBER", "SKYPE",
        },
    },
}


ORGANIZED_CATEGORY_NAMES = set(PHONE_CATEGORY_RULES.keys()) | {"Miscellaneous"}


class PhoneFileOrganizer:
    """
    Organizes phone backups into category folders.

    Final layout:

        backup_root/
            Device_Name/
                latest/
                    Photos/
                    Videos/
                    Documents/
                    Downloads/
                    Audio/
                    Messages/
                    Private_Files/
                    Miscellaneous/
    """

    def __init__(self, backup_root: str | Path, max_scan_depth: int = 25):
        self.backup_root = Path(backup_root)
        self.max_scan_depth = max_scan_depth
        self.backup_root.mkdir(parents=True, exist_ok=True)

    def organize_phone_backup(
        self,
        phone_source_path: str | Path,
        device_name: str,
        progress_callback: Optional[ProgressCallback] = None,
        should_stop: Optional[StopCallback] = None,
    ) -> Dict[str, Any]:
        """
        Copy files from a phone/source path into the organized backup folder.
        Source files are not deleted.
        """
        source_root = Path(phone_source_path)
        destination_root = self._device_latest_path(device_name)

        if not source_root.exists():
            raise FileNotFoundError(f"Phone source path does not exist: {source_root}")

        return self._organize(
            source_root=source_root,
            destination_root=destination_root,
            device_name=device_name,
            move_files=False,
            progress_callback=progress_callback,
            should_stop=should_stop,
        )

    def organize_existing_backup(
        self,
        backup_path: str | Path,
        device_name: str,
        move_files: bool = False,
        progress_callback: Optional[ProgressCallback] = None,
        should_stop: Optional[StopCallback] = None,
    ) -> Dict[str, Any]:
        """
        Organize an already-created backup.

        If move_files=True, files are moved into category folders.
        If move_files=False, files are copied.
        """
        source_root = Path(backup_path)
        destination_root = self._device_latest_path(device_name)

        if not source_root.exists():
            raise FileNotFoundError(f"Backup path does not exist: {source_root}")

        return self._organize(
            source_root=source_root,
            destination_root=destination_root,
            device_name=device_name,
            move_files=move_files,
            progress_callback=progress_callback,
            should_stop=should_stop,
        )

    def get_organization_summary(self, device_name: str) -> Dict[str, Any]:
        """Return file count and size totals for each organized category."""
        destination_root = self._device_latest_path(device_name)

        if not destination_root.exists():
            return {
                "device_name": device_name,
                "exists": False,
                "categories": {},
            }

        categories: Dict[str, Dict[str, int]] = {}

        for category_dir in destination_root.iterdir():
            if not category_dir.is_dir():
                continue

            file_count = 0
            size_bytes = 0

            for path in category_dir.rglob("*"):
                if not path.is_file():
                    continue

                try:
                    file_count += 1
                    size_bytes += path.stat().st_size
                except OSError:
                    continue

            categories[category_dir.name] = {
                "file_count": file_count,
                "size_bytes": size_bytes,
            }

        return {
            "device_name": device_name,
            "exists": True,
            "backup_path": str(destination_root),
            "categories": categories,
        }

    def _organize(
        self,
        source_root: Path,
        destination_root: Path,
        device_name: str,
        move_files: bool,
        progress_callback: Optional[ProgressCallback],
        should_stop: Optional[StopCallback],
    ) -> Dict[str, Any]:
        destination_root.mkdir(parents=True, exist_ok=True)

        results: Dict[str, Any] = {
            "device_name": device_name,
            "source_path": str(source_root),
            "destination_path": str(destination_root),
            "started_at": datetime.now().isoformat(),
            "completed_at": None,
            "cancelled": False,
            "files_organized": 0,
            "files_skipped": 0,
            "files_failed": 0,
            "categories": {},
            "errors": [],
            "manifest_entries": [],
        }

        if should_stop and should_stop():
            results["cancelled"] = True
            results["completed_at"] = datetime.now().isoformat()
            return results

        all_files = self._collect_files(source_root, destination_root)
        total_files = len(all_files)

        for index, file_path in enumerate(all_files):
            if should_stop and should_stop():
                results["cancelled"] = True
                break

            if progress_callback:
                progress = int((index / max(total_files, 1)) * 100)
                progress_callback(progress, f"Organizing {file_path.name}")

            try:
                category = self._categorize_file(file_path)
                source_relative = str(file_path.relative_to(source_root))

                placed, manifest_info = self._place_file(
                    file_path=file_path,
                    destination_root=destination_root,
                    category=category,
                    move_file=move_files,
                )

                if manifest_info:
                    results["manifest_entries"].append({
                        "source_relative": source_relative,
                        **manifest_info,
                    })

                if placed:
                    results["files_organized"] += 1
                    results["categories"][category] = (
                        results["categories"].get(category, 0) + 1
                    )
                else:
                    results["files_skipped"] += 1

            except Exception as exc:
                results["files_failed"] += 1
                results["errors"].append(
                    {
                        "file": str(file_path),
                        "error": str(exc),
                    }
                )

        if progress_callback:
            progress_callback(100, "Organization complete")

        results["completed_at"] = datetime.now().isoformat()
        return results

    def _collect_files(self, source_root: Path, destination_root: Path) -> List[Path]:
        """Collect files safely, avoiding already-organized category folders."""
        files: List[Path] = []

        source_root = source_root.resolve()
        destination_root = destination_root.resolve()

        for root, dirs, filenames in os.walk(source_root):
            current_root = Path(root)

            if self._exceeds_max_depth(current_root, source_root):
                dirs.clear()
                continue

            # If scanning the same folder we are organizing into, avoid reprocessing
            # already categorized folders like Photos, Videos, Documents, etc.
            if self._is_inside_destination(current_root, destination_root):
                dirs[:] = [
                    d for d in dirs
                    if d not in ORGANIZED_CATEGORY_NAMES
                ]

            for filename in filenames:
                file_path = current_root / filename

                try:
                    if file_path.is_file():
                        files.append(file_path)
                except OSError:
                    continue

        return files

    def _categorize_file(self, file_path: Path) -> str:
        """
        Categorize by private path first, then extension, then folder names.
        """
        extension = file_path.suffix.lower()

        path_parts = {
            self._normalize_name(part)
            for part in file_path.parts
        }

        full_path_normalized = self._normalize_name(str(file_path))

        private_keywords = {
            "SAFE", "VAULT", "SECURE", "SECUREFOLDER",
            "PRIVATE", "PRIVATEFILES",
        }

        if any(keyword in full_path_normalized for keyword in private_keywords):
            return "Private_Files"

        # Extension is usually the most reliable signal.
        for category, rules in PHONE_CATEGORY_RULES.items():
            if extension in rules["extensions"]:
                return category

        # Folder names are the fallback signal.
        for category, rules in PHONE_CATEGORY_RULES.items():
            normalized_folders = {
                self._normalize_name(folder)
                for folder in rules["folders"]
            }

            if path_parts & normalized_folders:
                return category

        return "Miscellaneous"

    def _place_file(
        self,
        file_path: Path,
        destination_root: Path,
        category: str,
        move_file: bool,
    ) -> tuple[bool, Optional[dict]]:
        """
        Copy or move the file into its category folder.

        Returns (placed, manifest_info). placed is False when an equivalent
        file already existed at the destination. manifest_info describes the
        file's final resting place either way (size, modified date, category,
        dest path) — even on the "already existed" branch, since that still
        tells the caller where this logical file now lives, which is exactly
        what lets the manifest learn about files that predate it.
        """
        category_dir = destination_root / category
        category_dir.mkdir(parents=True, exist_ok=True)

        destination = category_dir / file_path.name

        if destination.exists():
            if self._same_or_newer_file(file_path, destination):
                if move_file:
                    # Already have an equivalent file in latest/ — drop the
                    # duplicate sitting in _incoming/ so it doesn't linger
                    # and get rescanned on every future run.
                    try:
                        file_path.unlink()
                    except OSError:
                        pass

                manifest_info = self._build_manifest_info(destination, destination_root, category)
                return False, manifest_info

            destination = self._unique_destination_path(destination)

        if move_file:
            shutil.move(str(file_path), str(destination))
        else:
            shutil.copy2(str(file_path), str(destination))

        manifest_info = self._build_manifest_info(destination, destination_root, category)
        return True, manifest_info

    def _build_manifest_info(self, destination: Path, destination_root: Path, category: str) -> Optional[dict]:
        try:
            stat = destination.stat()
        except OSError:
            return None

        return {
            "category": category,
            "dest_relative": str(destination.relative_to(destination_root)),
            "size": stat.st_size,
            "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(),
        }

    def _same_or_newer_file(self, source: Path, destination: Path) -> bool:
        """
        True when destination appears to already contain the same file.

        Matches on size alone. We intentionally do NOT also require the
        destination's modified time to be >= the source's: timestamps from
        MTP transfers aren't reliably trustworthy (see PhoneBackupManager's
        MTP copy stage), and requiring mtime ordering on top of a size match
        was causing re-copied files to be treated as "different" and saved
        again under a "(1)" suffix instead of being recognized as duplicates.
        """
        try:
            return source.stat().st_size == destination.stat().st_size
        except OSError:
            return False

    def _unique_destination_path(self, destination: Path) -> Path:
        """
        Avoid overwriting files with the same name but different contents.
        Example:
            IMG_001.jpg
            IMG_001 (1).jpg
            IMG_001 (2).jpg
        """
        parent = destination.parent
        stem = destination.stem
        suffix = destination.suffix

        counter = 1
        candidate = destination

        while candidate.exists():
            candidate = parent / f"{stem} ({counter}){suffix}"
            counter += 1

        return candidate

    def _device_latest_path(self, device_name: str) -> Path:
        safe_device_name = self._safe_folder_name(device_name)
        return self.backup_root / safe_device_name / "latest"

    def _exceeds_max_depth(self, current_path: Path, source_root: Path) -> bool:
        try:
            depth = len(current_path.resolve().relative_to(source_root).parts)
            return depth > self.max_scan_depth
        except ValueError:
            return False

    def _is_inside_destination(self, path: Path, destination_root: Path) -> bool:
        try:
            path.resolve().relative_to(destination_root)
            return True
        except ValueError:
            return False

    def _normalize_name(self, value: str) -> str:
        return (
            value.upper()
            .replace(" ", "")
            .replace("_", "")
            .replace("-", "")
        )

    def _safe_folder_name(self, value: str) -> str:
        cleaned = "".join(
            char if char.isalnum() or char in {" ", "_", "-"} else "_"
            for char in value.strip()
        )

        cleaned = cleaned.replace(" ", "_")
        return cleaned or "Unknown_Device"
