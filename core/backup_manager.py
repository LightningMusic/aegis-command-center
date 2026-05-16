import json
import os
import shutil
import tempfile
from datetime import datetime
from pathlib import Path

from core.windows_drives import get_drive_map


EXCLUDED_DIR_NAMES = {
    "windows",
    "program files",
    "program files (x86)",
    "programdata",
    "$recycle.bin",
    "system volume information",
    "recovery",
    "perflogs",
    "config.msi",
    "msocache",
}

USER_FOLDERS = (
    "Desktop",
    "Documents",
    "Downloads",
    "Pictures",
    "Videos",
    "Music",
    "Favorites",
    "Saved Games",
)


class BackupManager:
    def __init__(self, config_manager):
        self.config_manager = config_manager

    def get_default_destination(self):
        settings = self.config_manager.get_backup_settings()
        return settings.get("default_destination", "").strip()

    def set_default_destination(self, destination_root):
        resolved = self.resolve_destination(destination_root)
        return self.save_destination(resolved, set_default=True)

    def get_saved_destinations(self):
        settings = self.config_manager.get_backup_settings()
        saved = settings.get("saved_destinations", [])
        normalized = []
        seen = set()
        for item in saved:
            resolved = self.resolve_destination(item)
            if not resolved:
                continue
            key = resolved.lower()
            if key in seen:
                continue
            seen.add(key)
            normalized.append(resolved)
        return normalized

    def save_destination(self, destination_root, set_default=False):
        resolved = self.resolve_destination(destination_root)
        if not resolved:
            raise RuntimeError("Choose a destination path first.")

        current = self.config_manager.get_backup_settings()
        saved = current.get("saved_destinations", [])
        normalized = []
        seen = set()
        for item in [resolved, *saved]:
            normalized_item = self.resolve_destination(item)
            if not normalized_item:
                continue
            key = normalized_item.lower()
            if key in seen:
                continue
            seen.add(key)
            normalized.append(normalized_item)

        payload = {"saved_destinations": normalized}
        if set_default:
            payload["default_destination"] = resolved
        return self.config_manager.update_backup_settings(payload)

    def remove_saved_destination(self, destination_root):
        resolved = self.resolve_destination(destination_root)
        current = self.config_manager.get_backup_settings()
        saved = [
            item
            for item in current.get("saved_destinations", [])
            if self.resolve_destination(item).lower() != resolved.lower()
        ]
        payload = {"saved_destinations": saved}
        if self.resolve_destination(current.get("default_destination", "")).lower() == resolved.lower():
            payload["default_destination"] = saved[0] if saved else ""
        return self.config_manager.update_backup_settings(payload)

    def get_destination_candidates(self):
        current_default = self.get_default_destination()
        candidates = []
        for drive in get_drive_map().values():
            if not drive["is_backup_destination"]:
                continue
            candidates.append(
                {
                    **drive,
                    "is_default": drive["root"] == current_default,
                }
            )
        return candidates

    def get_destination_choices(self):
        saved = self.get_saved_destinations()
        auto = [item["root"] for item in self.get_destination_candidates()]
        choices = []
        seen = set()
        for item in [*saved, *auto]:
            resolved = self.resolve_destination(item)
            if not resolved:
                continue
            key = resolved.lower()
            if key in seen:
                continue
            seen.add(key)
            choices.append(resolved)
        return choices

    def resolve_destination(self, destination_root=None):
        candidate = (destination_root or self.get_default_destination()).strip()
        if not candidate:
            return ""

        if len(candidate) == 2 and candidate[1] == ":":
            candidate = f"{candidate}\\"

        return os.path.abspath(candidate)

    def validate_destination(self, destination_root, required_bytes=0):
        resolved = self.resolve_destination(destination_root)
        if not resolved:
            return False, "No backup destination selected.", None

        try:
            os.makedirs(resolved, exist_ok=True)
        except OSError as exc:
            return False, f"Unable to create destination: {exc}", None

        try:
            usage = shutil.disk_usage(resolved)
        except OSError as exc:
            return False, f"Unable to read destination space: {exc}", None

        try:
            with tempfile.NamedTemporaryFile(dir=resolved, prefix="aegis_backup_", delete=True):
                pass
        except OSError as exc:
            return False, f"Destination is not writable: {exc}", usage

        if required_bytes and usage.free < required_bytes:
            return (
                False,
                f"Not enough free space. Need {self._format_bytes(required_bytes)}, have {self._format_bytes(usage.free)}.",
                usage,
            )

        return True, "Destination is writable and has enough space.", usage

    def estimate_backup_size(self, mode, drives):
        total = 0
        for path in self._iter_backup_sources(mode, drives):
            total += self._estimate_path_size(path)
        return total

    def run_backup(
        self,
        mode,
        drives,
        destination_root=None,
        progress_callback=None,
        should_stop=None,
    ):
        drives = [drive if drive.endswith("\\") else f"{drive}\\" for drive in drives]
        resolved_destination = self.resolve_destination(destination_root)
        filtered_drives = self._filter_source_drives(drives, resolved_destination)

        if not filtered_drives:
            raise RuntimeError(
                "No valid source drives remain after applying self-backup safety checks."
            )

        estimated_bytes = self.estimate_backup_size(mode, filtered_drives)

        ok, message, usage = self.validate_destination(resolved_destination, estimated_bytes)
        if not ok:
            raise RuntimeError(message)

        settings = self.config_manager.get_backup_settings()
        backup_root_name = settings.get("backup_root_name", "Aegis_Backups")
        log_root_name = settings.get("log_root_name", "Aegis_Backups\\logs")

        stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        backup_root, reused_existing_set = self._resolve_backup_root(
            resolved_destination,
            backup_root_name,
            mode,
            filtered_drives,
            stamp,
        )
        log_root = os.path.join(resolved_destination, log_root_name)
        os.makedirs(backup_root, exist_ok=True)
        os.makedirs(log_root, exist_ok=True)

        manifest = {
            "mode": mode,
            "started_at": datetime.now().isoformat(),
            "destination_root": resolved_destination,
            "backup_root": backup_root,
            "drives": filtered_drives,
            "estimated_bytes": estimated_bytes,
            "copied_files": 0,
            "copied_bytes": 0,
            "skipped_files": 0,
            "errors": [],
        }

        log_file = os.path.join(log_root, f"backup_{mode}_{stamp}.log")

        sources = list(self._iter_backup_sources(mode, filtered_drives, resolved_destination))
        total_sources = max(len(sources), 1)

        for source_index, source_path in enumerate(sources, start=1):
            if should_stop and should_stop():
                return self._finish_cancelled_backup(
                    manifest,
                    resolved_destination,
                    log_root,
                    mode,
                    stamp,
                )

            source_root = Path(source_path)
            dest_root = self._build_destination_root(backup_root, mode, source_root)

            if progress_callback:
                progress_callback(
                    int(((source_index - 1) / total_sources) * 100),
                    f"Backing up {source_root}",
                )

            if source_root.is_file():
                self._copy_file(
                    source_root,
                    dest_root / source_root.name,
                    manifest,
                    should_stop=should_stop,
                )
            else:
                self._copy_tree(source_root, dest_root, manifest, should_stop=should_stop)

        manifest["completed_at"] = datetime.now().isoformat()
        manifest["free_space_after_bytes"] = shutil.disk_usage(resolved_destination).free
        manifest["backup_manifest_file"] = str(Path(backup_root) / "backup_manifest.json")

        with open(log_file, "w", encoding="utf-8") as file_handle:
            json.dump(manifest, file_handle, indent=2)
        self._write_backup_manifest(backup_root, manifest)

        self.config_manager.update_backup_settings(
            {
                "last_destination": resolved_destination,
                "last_mode": mode,
                "last_run_at": manifest["completed_at"],
                "last_status": (
                    f"Completed {manifest['copied_files']} files "
                    f"({self._format_bytes(manifest['copied_bytes'])})"
                ),
            }
        )

        if progress_callback:
            progress_callback(100, "Backup complete")

        return {
            "destination_root": resolved_destination,
            "backup_root": backup_root,
            "log_file": log_file,
            "estimated_bytes": estimated_bytes,
            "free_space_before_bytes": usage.free if usage else 0,
            "copied_files": manifest["copied_files"],
            "copied_bytes": manifest["copied_bytes"],
            "skipped_files": manifest["skipped_files"],
            "error_count": len(manifest["errors"]),
            "cancelled": False,
            "reused_existing_set": reused_existing_set,
        }

    def list_backup_folders(self, destination_root):
        resolved_destination = self.resolve_destination(destination_root)
        if not resolved_destination:
            return []

        settings = self.config_manager.get_backup_settings()
        backup_root_name = settings.get("backup_root_name", "Aegis_Backups")
        backup_root = Path(resolved_destination) / backup_root_name
        if not backup_root.exists():
            return []

        folders = []
        for child in backup_root.iterdir():
            if child.is_dir():
                folders.append(child.name)
        return sorted(folders, reverse=True)

    def merge_backup_folders(self, destination_root, source_folder, target_folder):
        if not source_folder or not target_folder:
            raise RuntimeError("Select both a source and target backup folder.")
        if source_folder == target_folder:
            raise RuntimeError("Source and target backup folders must be different.")

        resolved_destination = self.resolve_destination(destination_root)
        settings = self.config_manager.get_backup_settings()
        backup_root_name = settings.get("backup_root_name", "Aegis_Backups")
        log_root_name = settings.get("log_root_name", "Aegis_Backups\\logs")

        backup_root = Path(resolved_destination) / backup_root_name
        source_path = backup_root / source_folder
        target_path = backup_root / target_folder

        if not source_path.exists():
            raise RuntimeError("Source backup folder does not exist.")
        if not target_path.exists():
            raise RuntimeError("Target backup folder does not exist.")

        merged_files = 0
        skipped_files = 0
        copied_bytes = 0

        for current_root, _, files in os.walk(source_path):
            current_path = Path(current_root)
            relative_root = current_path.relative_to(source_path)
            destination_dir = target_path / relative_root
            os.makedirs(destination_dir, exist_ok=True)

            for file_name in files:
                source_file = current_path / file_name
                target_file = destination_dir / file_name
                try:
                    if target_file.exists():
                        source_stat = source_file.stat()
                        target_stat = target_file.stat()
                        if (
                            target_stat.st_size == source_stat.st_size
                            and int(target_stat.st_mtime) >= int(source_stat.st_mtime)
                        ):
                            skipped_files += 1
                            continue
                    shutil.copy2(source_file, target_file)
                    merged_files += 1
                    copied_bytes += source_file.stat().st_size
                except OSError:
                    skipped_files += 1

        log_root = Path(resolved_destination) / log_root_name
        os.makedirs(log_root, exist_ok=True)
        stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        log_file = log_root / f"merge_{stamp}.log"
        log_file.write_text(
            json.dumps(
                {
                    "source_folder": source_folder,
                    "target_folder": target_folder,
                    "merged_files": merged_files,
                    "skipped_files": skipped_files,
                    "copied_bytes": copied_bytes,
                    "completed_at": datetime.now().isoformat(),
                },
                indent=2,
            ),
            encoding="utf-8",
        )

        return {
            "source_folder": source_folder,
            "target_folder": target_folder,
            "merged_files": merged_files,
            "skipped_files": skipped_files,
            "copied_bytes": copied_bytes,
            "log_file": str(log_file),
        }

    def merge_folders(self, source_path, target_path):
        if not source_path or not target_path:
            raise RuntimeError("Choose both a source folder and a target folder.")

        source = Path(source_path).resolve()
        target = Path(target_path).resolve()

        if not source.exists() or not source.is_dir():
            raise RuntimeError("Source folder does not exist.")
        if not target.exists() or not target.is_dir():
            raise RuntimeError("Target folder does not exist.")
        if source == target:
            raise RuntimeError("Source and target folders must be different.")

        try:
            target.relative_to(source)
            raise RuntimeError("Target folder cannot be inside the source folder.")
        except ValueError:
            pass

        try:
            source.relative_to(target)
            raise RuntimeError("Source folder cannot be inside the target folder.")
        except ValueError:
            pass

        merged_files = 0
        skipped_files = 0
        copied_bytes = 0

        for current_root, _, files in os.walk(source):
            current_path = Path(current_root)
            relative_root = current_path.relative_to(source)
            destination_dir = target / relative_root
            os.makedirs(destination_dir, exist_ok=True)

            for file_name in files:
                source_file = current_path / file_name
                target_file = destination_dir / file_name
                try:
                    if target_file.exists():
                        source_stat = source_file.stat()
                        target_stat = target_file.stat()
                        if (
                            target_stat.st_size == source_stat.st_size
                            and int(target_stat.st_mtime) >= int(source_stat.st_mtime)
                        ):
                            skipped_files += 1
                            continue
                    shutil.copy2(source_file, target_file)
                    merged_files += 1
                    copied_bytes += source_file.stat().st_size
                except OSError:
                    skipped_files += 1

        log_root = target / "_aegis_merge_logs"
        os.makedirs(log_root, exist_ok=True)
        stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        log_file = log_root / f"merge_{stamp}.log"
        log_file.write_text(
            json.dumps(
                {
                    "source_folder": str(source),
                    "target_folder": str(target),
                    "merged_files": merged_files,
                    "skipped_files": skipped_files,
                    "copied_bytes": copied_bytes,
                    "completed_at": datetime.now().isoformat(),
                },
                indent=2,
            ),
            encoding="utf-8",
        )

        return {
            "source_folder": str(source),
            "target_folder": str(target),
            "merged_files": merged_files,
            "skipped_files": skipped_files,
            "copied_bytes": copied_bytes,
            "log_file": str(log_file),
        }

    def _iter_backup_sources(self, mode, drives, destination_root=None):
        if mode == "loose":
            yield from self._iter_loose_file_sources(drives, destination_root)
            return

        if mode == "windows":
            yield from self._iter_windows_sources(drives, destination_root)
            return

        yield from self._iter_mirror_sources(drives, destination_root)

    def _iter_mirror_sources(self, drives, destination_root=None):
        for drive in drives:
            if self._is_same_or_parent_path(drive, destination_root):
                continue
            yield drive

    def _iter_loose_file_sources(self, drives, destination_root=None):
        for drive in drives:
            drive_root = Path(drive)
            try:
                for child in drive_root.iterdir():
                    if self._is_same_or_parent_path(child, destination_root):
                        continue
                    if child.is_file():
                        yield str(child)
            except OSError:
                pass

            users_root = drive_root / "Users"
            if not users_root.exists():
                continue

            try:
                for profile in users_root.iterdir():
                    if not profile.is_dir():
                        continue
                    for folder_name in USER_FOLDERS:
                        folder = profile / folder_name
                        if folder.exists() and not self._is_same_or_parent_path(folder, destination_root):
                            yield str(folder)
            except OSError:
                continue

    def _iter_windows_sources(self, drives, destination_root=None):
        for drive in drives:
            drive_root = Path(drive)
            users_root = drive_root / "Users"
            if users_root.exists() and not self._is_same_or_parent_path(users_root, destination_root):
                yield str(users_root)

            for extra in ("ProgramData",):
                extra_path = drive_root / extra
                if extra_path.exists() and not self._is_same_or_parent_path(extra_path, destination_root):
                    yield str(extra_path)

    def _build_destination_root(self, backup_root, mode, source_root: Path):
        if source_root.drive:
            drive_name = source_root.drive.replace(":", "")
        else:
            drive_name = "misc"

        if mode == "mirror":
            return Path(backup_root) / f"{drive_name}_Drive"

        if mode == "windows":
            return Path(backup_root) / f"{drive_name}_Windows_Backup"

        return Path(backup_root) / f"{drive_name}_Loose_Files"

    def _copy_tree(self, source_root: Path, destination_root: Path, manifest, should_stop=None):
        for current_root, dirs, files in os.walk(source_root):
            if should_stop and should_stop():
                return

            current_path = Path(current_root)
            dirs[:] = [
                name
                for name in dirs
                if not self._should_skip_dir(current_path / name)
                and not self._is_same_or_parent_path(current_path / name, destination_root)
            ]

            try:
                relative_root = current_path.relative_to(source_root.anchor)
            except ValueError:
                relative_root = current_path.relative_to(source_root)

            target_root = destination_root / relative_root
            os.makedirs(target_root, exist_ok=True)

            for file_name in files:
                if should_stop and should_stop():
                    return

                source_file = current_path / file_name
                if self._should_skip_file(source_file):
                    manifest["skipped_files"] += 1
                    continue
                if self._is_same_or_parent_path(source_file, destination_root):
                    manifest["skipped_files"] += 1
                    continue
                self._copy_file(
                    source_file,
                    target_root / file_name,
                    manifest,
                    should_stop=should_stop,
                )

    def _copy_file(self, source_file: Path, destination_file: Path, manifest, should_stop=None):
        if should_stop and should_stop():
            return

        try:
            os.makedirs(destination_file.parent, exist_ok=True)
            if destination_file.exists():
                src_stat = source_file.stat()
                dst_stat = destination_file.stat()
                if (
                    src_stat.st_size == dst_stat.st_size
                    and int(src_stat.st_mtime) <= int(dst_stat.st_mtime)
                ):
                    manifest["skipped_files"] += 1
                    return

            shutil.copy2(source_file, destination_file)
            manifest["copied_files"] += 1
            manifest["copied_bytes"] += source_file.stat().st_size
        except OSError as exc:
            manifest["errors"].append({"path": str(source_file), "error": str(exc)})

    def _estimate_path_size(self, path):
        target = Path(path)
        if target.is_file():
            try:
                return target.stat().st_size
            except OSError:
                return 0

        total = 0
        for current_root, dirs, files in os.walk(target):
            current_path = Path(current_root)
            dirs[:] = [
                name
                for name in dirs
                if not self._should_skip_dir(current_path / name)
            ]
            for file_name in files:
                candidate = current_path / file_name
                if self._should_skip_file(candidate):
                    continue
                try:
                    total += candidate.stat().st_size
                except OSError:
                    continue
        return total

    def _should_skip_dir(self, path: Path):
        return path.name.lower() in EXCLUDED_DIR_NAMES

    def _should_skip_file(self, path: Path):
        return path.name.lower() in {"pagefile.sys", "hiberfil.sys", "swapfile.sys"}

    def _filter_source_drives(self, drives, destination_root):
        if not destination_root:
            return drives

        destination_anchor = Path(destination_root).anchor.lower()
        filtered = []
        for drive in drives:
            if Path(drive).anchor.lower() == destination_anchor:
                continue
            filtered.append(drive)
        return filtered

    def _is_same_or_parent_path(self, candidate, reference):
        if not candidate or not reference:
            return False

        try:
            candidate_path = Path(candidate).resolve()
            reference_path = Path(reference).resolve()
        except OSError:
            return False

        if candidate_path == reference_path:
            return True

        try:
            candidate_path.relative_to(reference_path)
            return True
        except ValueError:
            return False

    def _format_bytes(self, value):
        units = ["B", "KB", "MB", "GB", "TB"]
        size = float(value)
        for unit in units:
            if size < 1024 or unit == units[-1]:
                return f"{size:.2f} {unit}"
            size /= 1024

    def _resolve_backup_root(self, destination_root, backup_root_name, mode, drives, stamp):
        root = Path(destination_root) / backup_root_name
        os.makedirs(root, exist_ok=True)

        existing = self._find_existing_backup_root(root, mode, drives)
        if existing:
            return str(existing), True

        return str(root / f"{mode}_{stamp}"), False

    def _find_existing_backup_root(self, backup_root, mode, drives):
        requested_drives = sorted(drive.lower() for drive in drives)
        best_match = None
        best_time = None

        for child in backup_root.iterdir():
            if not child.is_dir():
                continue

            manifest_path = child / "backup_manifest.json"
            if not manifest_path.exists():
                continue

            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue

            manifest_mode = manifest.get("mode")
            manifest_drives = sorted(drive.lower() for drive in manifest.get("drives", []))
            if manifest_mode != mode or manifest_drives != requested_drives:
                continue

            completed_at = manifest.get("completed_at") or manifest.get("started_at") or ""
            if best_match is None or completed_at > (best_time or ""):
                best_match = child
                best_time = completed_at

        return best_match

    def _write_backup_manifest(self, backup_root, manifest):
        manifest_path = Path(backup_root) / "backup_manifest.json"
        manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    def _finish_cancelled_backup(self, manifest, resolved_destination, log_root, mode, stamp):
        manifest["completed_at"] = datetime.now().isoformat()
        manifest["cancelled"] = True

        log_file = os.path.join(log_root, f"backup_{mode}_{stamp}.log")
        with open(log_file, "w", encoding="utf-8") as file_handle:
            json.dump(manifest, file_handle, indent=2)
        self._write_backup_manifest(manifest["backup_root"], manifest)

        self.config_manager.update_backup_settings(
            {
                "last_destination": resolved_destination,
                "last_mode": mode,
                "last_run_at": manifest["completed_at"],
                "last_status": (
                    f"Cancelled after {manifest['copied_files']} files "
                    f"({self._format_bytes(manifest['copied_bytes'])})"
                ),
            }
        )

        return {
            "destination_root": resolved_destination,
            "backup_root": manifest["backup_root"],
            "log_file": log_file,
            "estimated_bytes": manifest["estimated_bytes"],
            "free_space_before_bytes": 0,
            "copied_files": manifest["copied_files"],
            "copied_bytes": manifest["copied_bytes"],
            "skipped_files": manifest["skipped_files"],
            "error_count": len(manifest["errors"]),
            "cancelled": True,
        }
