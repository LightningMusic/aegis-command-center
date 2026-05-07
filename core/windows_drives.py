import ctypes
import os
import shutil
import string
from typing import Dict, List


DRIVE_UNKNOWN = 0
DRIVE_NO_ROOT_DIR = 1
DRIVE_REMOVABLE = 2
DRIVE_FIXED = 3
DRIVE_REMOTE = 4
DRIVE_CDROM = 5
DRIVE_RAMDISK = 6

DRIVE_TYPE_NAMES = {
    DRIVE_UNKNOWN: "Unknown",
    DRIVE_NO_ROOT_DIR: "Missing",
    DRIVE_REMOVABLE: "Removable",
    DRIVE_FIXED: "Fixed",
    DRIVE_REMOTE: "Network",
    DRIVE_CDROM: "Optical",
    DRIVE_RAMDISK: "RAM Disk",
}

SCAN_ELIGIBLE_TYPES = {DRIVE_REMOVABLE, DRIVE_FIXED, DRIVE_REMOTE, DRIVE_RAMDISK}
BACKUP_DESTINATION_TYPES = {DRIVE_REMOVABLE, DRIVE_FIXED, DRIVE_REMOTE, DRIVE_RAMDISK}


def _get_drive_type(root_path: str) -> int:
    return ctypes.windll.kernel32.GetDriveTypeW(root_path)


def _is_writable_hint(path: str) -> bool:
    return os.access(path, os.W_OK)


def _safe_disk_usage(path: str):
    try:
        return shutil.disk_usage(path)
    except OSError:
        return None


def list_connected_drives() -> List[Dict]:
    drives = []

    for letter in string.ascii_uppercase:
        root = f"{letter}:\\"
        if not os.path.exists(root):
            continue

        drive_type = _get_drive_type(root)
        usage = _safe_disk_usage(root)
        writable = drive_type in BACKUP_DESTINATION_TYPES and _is_writable_hint(root)

        drives.append(
            {
                "root": root,
                "letter": letter,
                "type_code": drive_type,
                "type_name": DRIVE_TYPE_NAMES.get(drive_type, "Unknown"),
                "total_bytes": usage.total if usage else 0,
                "used_bytes": usage.used if usage else 0,
                "free_bytes": usage.free if usage else 0,
                "is_scan_eligible": drive_type in SCAN_ELIGIBLE_TYPES,
                "is_backup_destination": drive_type in BACKUP_DESTINATION_TYPES,
                "is_writable": writable,
            }
        )

    return drives


def get_drive_map() -> Dict[str, Dict]:
    return {item["root"]: item for item in list_connected_drives()}
