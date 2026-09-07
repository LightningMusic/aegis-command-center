"""
core/disk_map.py
================
A "where did my space go" folder-size walk that covers the *whole* drive,
including Program Files / Windows / game installs that the Aegis file index
deliberately skips. stat-only (no hashing), depth-limited, cancellable.

    result = scan_folder_sizes("C:\\", max_depth=2, should_stop=flag, progress=cb)
    result["total"]        -> bytes used under the root
    result["children"]     -> [{"name","path","size","is_dir"}]  biggest first
    result["categories"]   -> {"Windows": n, "Programs": n, "Games": n,
                               "Users\\<name>": n, "Other": n}
"""

from __future__ import annotations

import os
from typing import Callable, Dict, List, Optional

from core.app_scanner import steam_library_paths

_SKIP_DIR_NAMES = {
    "$recycle.bin",
    "system volume information",
    "$windows.~bt",
    "$windows.~ws",
    "config.msi",
    "recovery",
}
# WinSxS is one giant pile of hardlinks to the component store; summing file
# sizes there over-counts by hundreds of GB. Real tools special-case it.
_SKIP_DIR_PATHS = ("\\windows\\winsxs", "\\windows\\installer")
_SKIP_FILE_NAMES = {"pagefile.sys", "hiberfil.sys", "swapfile.sys", "dumpstack.log.tmp"}
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400

ProgressCB = Callable[[int, str], None]
StopCB = Callable[[], bool]


def _categorize(name: str, path: str, game_prefixes: List[str]) -> str:
    low = name.lower()
    plow = path.lower()
    if low.startswith("windows") or low in ("perflogs", "programdata"):
        return "Windows & system"
    if low.startswith("program files"):
        return "Programs"
    if low == "users":
        return "Users"
    if low in ("steamlibrary", "games", "xboxgames", "epic games") or any(
        plow.startswith(g) for g in game_prefixes
    ):
        return "Games"
    return "Other"


def scan_folder_sizes(
    root: str,
    *,
    max_depth: int = 2,
    top_n: int = 40,
    should_stop: Optional[StopCB] = None,
    progress: Optional[ProgressCB] = None,
) -> Dict:
    root = root if root.endswith(os.sep) else root + os.sep
    game_prefixes = [str(p).lower() for p in steam_library_paths()]

    total = 0
    file_count = 0
    # size accumulated per folder path we still care about (depth <= max_depth)
    sizes: Dict[str, int] = {}
    root_depth = root.rstrip(os.sep).count(os.sep)

    def bump(path: str, nbytes: int) -> None:
        # attribute nbytes to every ancestor folder down to max_depth
        parts = path[len(root):].split(os.sep)
        for i in range(1, min(len(parts), max_depth) + 1):
            key = root + os.sep.join(parts[:i])
            sizes[key] = sizes.get(key, 0) + nbytes

    stack = [root]
    while stack:
        if should_stop and should_stop():
            break
        current = stack.pop()
        try:
            entries = list(os.scandir(current))
        except OSError:
            continue
        for entry in entries:
            try:
                name_low = entry.name.lower()
                if entry.is_dir(follow_symlinks=False):
                    if name_low in _SKIP_DIR_NAMES:
                        continue
                    epath_low = entry.path.lower()
                    if any(s in epath_low for s in _SKIP_DIR_PATHS):
                        continue
                    try:
                        if entry.stat(follow_symlinks=False).st_file_attributes & _FILE_ATTRIBUTE_REPARSE_POINT:
                            continue  # junction / symlink
                    except (AttributeError, OSError):
                        pass
                    stack.append(entry.path)
                elif entry.is_file(follow_symlinks=False):
                    if name_low in _SKIP_FILE_NAMES:
                        continue
                    st = entry.stat(follow_symlinks=False)
                    # cloud placeholder (OneDrive "online-only") — reports full
                    # size but takes ~no disk
                    if getattr(st, "st_file_attributes", 0) & _FILE_ATTRIBUTE_REPARSE_POINT:
                        continue
                    size = st.st_size
                    total += size
                    file_count += 1
                    bump(entry.path, size)
                    if progress and file_count % 20000 == 0:
                        progress(file_count, f"{file_count:,} files · {_gb(total)} GB")
            except OSError:
                continue

    # top-level children (depth 1) sorted, plus the category rollup
    children: List[Dict] = []
    categories: Dict[str, int] = {}
    for path, size in sizes.items():
        rel = path[len(root):]
        if os.sep in rel:  # only depth-1 entries for the children list
            continue
        children.append({"name": rel, "path": path, "size": size, "is_dir": True})
        categories[_categorize(rel, path, game_prefixes)] = (
            categories.get(_categorize(rel, path, game_prefixes), 0) + size
        )

    children.sort(key=lambda c: c["size"], reverse=True)
    if progress:
        progress(file_count, f"{file_count:,} files · {_gb(total)} GB")

    return {
        "root": root,
        "total": total,
        "file_count": file_count,
        "children": children[:top_n],
        "categories": dict(sorted(categories.items(), key=lambda kv: kv[1], reverse=True)),
        "stopped": bool(should_stop and should_stop()),
    }


def _gb(n: int) -> str:
    return f"{n / (1024 ** 3):.1f}"
