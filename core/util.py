"""
core/util.py
============
Small shared helpers that were being re-implemented across the codebase.
"""

from __future__ import annotations

import threading
import time
from typing import Dict, List

from core.windows_drives import list_connected_drives

_UNITS = ("B", "KB", "MB", "GB", "TB", "PB")


def human_bytes(n: float | int | None, decimals: int = 1) -> str:
    """1536 -> '1.5 KB'. Was copy-pasted in ~6 modules."""
    size = float(n or 0)
    for unit in _UNITS:
        if size < 1024 or unit == _UNITS[-1]:
            return f"{size:.{decimals}f} {unit}"
        size /= 1024
    return f"{size:.{decimals}f} {_UNITS[-1]}"


# ── connected-drive cache ──────────────────────────────────────────────────
# list_connected_drives() runs shutil.disk_usage per drive; a stale removable
# or disconnected network drive can block for seconds. The result barely
# changes second-to-second, so cache it briefly and let callers force a
# refresh on an explicit "Refresh Drives" action.
_drive_lock = threading.Lock()
_drive_cache: List[Dict] = []
_drive_cache_at = 0.0


def connected_drives(max_age: float = 3.0, force: bool = False) -> List[Dict]:
    global _drive_cache, _drive_cache_at
    with _drive_lock:
        if force or not _drive_cache or (time.monotonic() - _drive_cache_at) > max_age:
            _drive_cache = list_connected_drives()
            _drive_cache_at = time.monotonic()
        return list(_drive_cache)


def refresh_drives() -> List[Dict]:
    return connected_drives(force=True)


def drive_map(max_age: float = 3.0, force: bool = False) -> Dict[str, Dict]:
    return {d["root"]: d for d in connected_drives(max_age=max_age, force=force)}
