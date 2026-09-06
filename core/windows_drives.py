import ctypes
import os
import shutil
import string
import threading
import time
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


_CACHE_TTL_SECONDS = 5.0
_cache_lock = threading.Lock()
_cache_value: List[Dict] = []
_cache_stamp = 0.0


def list_connected_drives(use_cache: bool = True) -> List[Dict]:
    """
    Enumerate drives. Each call probes every drive letter with syscalls
    (GetDriveTypeW / disk_usage / os.access) that can each block for seconds on
    empty card readers, optical drives, or a phone in MTP mode. Callers on the
    GUI thread hit this repeatedly per page switch, so results are cached for a
    few seconds. Pass use_cache=False to force a fresh probe.
    """
    global _cache_value, _cache_stamp

    if use_cache:
        with _cache_lock:
            if _cache_value and (time.monotonic() - _cache_stamp) < _CACHE_TTL_SECONDS:
                return [dict(item) for item in _cache_value]

    drives = _probe_connected_drives()

    with _cache_lock:
        _cache_value = [dict(item) for item in drives]
        _cache_stamp = time.monotonic()

    return drives


def _probe_connected_drives() -> List[Dict]:
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
