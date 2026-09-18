"""
core/disk_map.py
================
A "where did my space go" folder-size walk that covers the *whole* drive,
including Program Files / Windows / game installs that the Aegis file index
deliberately skips. stat-only (no hashing), depth-limited, cancellable.

    result = scan_folder_sizes("C:\\", should_stop=flag, progress=cb)
    result["total"]        -> bytes used under the root
    result["tree"]         -> nested {"name","path","size","own_size",
                                      "file_count","children":[...]}
    result["children"]     -> tree["children"] (biggest first)
    result["categories"]   -> {"Windows & system": n, "Programs": n,
                               "Games": n, "Users": n, "Other": n}

The tree is what the UI drills into, so it is built once here rather than
re-scanning a folder every time someone expands a row.
"""

from __future__ import annotations

import ctypes
import os
import shutil
from ctypes import wintypes
from typing import Callable, Dict, List, Optional, Tuple

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
_FILE_ATTRIBUTE_SPARSE_FILE = 0x200
_FILE_ATTRIBUTE_COMPRESSED = 0x800
# Files carrying either of these report a LOGICAL size in st_size that can be
# wildly larger than the space they actually occupy, so they need asking a
# second time (see _size_on_disk).
_NEEDS_REAL_SIZE = _FILE_ATTRIBUTE_SPARSE_FILE | _FILE_ATTRIBUTE_COMPRESSED


def _make_size_on_disk():
    """
    Actual allocated size of a file, which is not the same thing as st_size.

    st_size is the file's LOGICAL length. A sparse file reports the size it
    pretends to be: a real example from this machine is a Google Drive
    virtual-disk image, userdata.img, reporting 512.00 GB while occupying
    1.13 GB. Counting logical sizes made a 930 GB drive add up to 1,282 GB.

    GetCompressedFileSizeW returns what the file really costs, accounting for
    both sparseness and NTFS compression. It's a per-file call, so it is only
    used for files whose attributes say it could matter — a few hundred out
    of hundreds of thousands.
    """
    if os.name != "nt":
        return lambda path: None
    try:
        fn = ctypes.windll.kernel32.GetCompressedFileSizeW
        fn.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD)]
        fn.restype = wintypes.DWORD
    except (AttributeError, OSError):
        return lambda path: None

    invalid = 0xFFFFFFFF

    def size_on_disk(path: str) -> Optional[int]:
        try:
            high = wintypes.DWORD(0)
            low = fn(path, ctypes.byref(high))
            if low == invalid:
                return None
            return (high.value << 32) + low
        except (OSError, ValueError):
            return None

    return size_on_disk


_size_on_disk = _make_size_on_disk()

# How deep to keep per-folder detail. Deeper files still count toward the
# total and toward their nearest kept ancestor — this only bounds how far
# you can drill in, and how much the scan has to remember. Depth 6 reaches
# inside an individual Steam game
# (Program Files (x86) / Steam / steamapps / common / <game> / <subdir>).
DEFAULT_MAX_DEPTH = 6

# Folders smaller than this are rolled into their parent rather than listed
# individually. Without a floor, a drive produces tens of thousands of
# uninteresting 4 KB folders and the tree becomes unreadable.
DEFAULT_MIN_NODE_BYTES = 64 * 1024 * 1024  # 64 MB
DEFAULT_MAX_CHILDREN = 60

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
    max_depth: int = DEFAULT_MAX_DEPTH,
    top_n: int = DEFAULT_MAX_CHILDREN,
    min_node_bytes: int = DEFAULT_MIN_NODE_BYTES,
    should_stop: Optional[StopCB] = None,
    progress: Optional[ProgressCB] = None,
) -> Dict:
    root = root if root.endswith(os.sep) else root + os.sep

    # Steam libraries can live anywhere; knowing where they are lets the
    # category rollup call a game a game instead of "Other".
    try:
        game_prefixes = [str(p).lower() for p in steam_library_paths()]
    except Exception:  # noqa: BLE001 - never let detection break the scan
        game_prefixes = []

    total = 0
    file_count = 0

    # Bytes/files sitting DIRECTLY in each tracked folder, plus the parent
    # link, so totals can be rolled up bottom-up once at the end. The old
    # version instead re-split every file's path and credited each ancestor
    # as it went — O(files x depth) string work in the hot loop, and it threw
    # away everything below depth 1 anyway.
    own_bytes: Dict[str, int] = {root: 0}
    own_files: Dict[str, int] = {root: 0}
    parent_of: Dict[str, str] = {}

    # (directory to scan, folder its files get credited to). Past max_depth
    # the attribution folder stops advancing, so deep trees still count
    # toward the total without the scan having to remember every folder.
    stack: List[Tuple[str, str]] = [(root, root)]

    while stack:
        if should_stop and should_stop():
            break
        current, attribute_to = stack.pop()
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

                    depth = entry.path[len(root):].count(os.sep) + 1
                    if depth <= max_depth:
                        own_bytes.setdefault(entry.path, 0)
                        own_files.setdefault(entry.path, 0)
                        parent_of[entry.path] = attribute_to
                        stack.append((entry.path, entry.path))
                    else:
                        stack.append((entry.path, attribute_to))

                elif entry.is_file(follow_symlinks=False):
                    if name_low in _SKIP_FILE_NAMES:
                        continue
                    st = entry.stat(follow_symlinks=False)
                    attrs = getattr(st, "st_file_attributes", 0)
                    # cloud placeholder (OneDrive "online-only") — reports full
                    # size but takes ~no disk
                    if attrs & _FILE_ATTRIBUTE_REPARSE_POINT:
                        continue
                    size = st.st_size
                    if attrs & _NEEDS_REAL_SIZE:
                        real = _size_on_disk(entry.path)
                        if real is not None:
                            size = real
                    total += size
                    file_count += 1
                    own_bytes[attribute_to] = own_bytes.get(attribute_to, 0) + size
                    own_files[attribute_to] = own_files.get(attribute_to, 0) + 1
                    if progress and file_count % 20000 == 0:
                        progress(file_count, f"{file_count:,} files · {_gb(total)} GB")
            except OSError:
                continue

    stopped = bool(should_stop and should_stop())

    # Roll child totals into parents, deepest first so a folder's children
    # are final before it is.
    rolled: Dict[str, int] = dict(own_bytes)
    rolled_files: Dict[str, int] = dict(own_files)
    for path in sorted(own_bytes, key=lambda p: p.count(os.sep), reverse=True):
        parent = parent_of.get(path)
        if parent is not None and parent != path:
            rolled[parent] = rolled.get(parent, 0) + rolled.get(path, 0)
            rolled_files[parent] = rolled_files.get(parent, 0) + rolled_files.get(path, 0)

    children_of: Dict[str, List[str]] = {}
    for child, parent in parent_of.items():
        children_of.setdefault(parent, []).append(child)

    def build(path: str, name: str) -> Dict:
        kids = []
        for child in children_of.get(path, []):
            if rolled.get(child, 0) < min_node_bytes:
                continue
            kids.append(build(child, os.path.basename(child.rstrip(os.sep))))
        kids.sort(key=lambda c: c["size"], reverse=True)
        return {
            "name": name,
            "path": path,
            "size": rolled.get(path, 0),
            "own_size": own_bytes.get(path, 0),
            "file_count": rolled_files.get(path, 0),
            "children": kids[:top_n],
        }

    tree = build(root, root)

    categories: Dict[str, int] = {}
    for child in tree["children"]:
        key = _categorize(child["name"], child["path"], game_prefixes)
        categories[key] = categories.get(key, 0) + child["size"]

    if progress:
        progress(file_count, f"{file_count:,} files · {_gb(total)} GB")

    # Windows' own figures, so the walk can be reconciled against reality
    # rather than just asserted. The walk deliberately skips things that do
    # occupy real space (the WinSxS component store, System Volume
    # Information, files it has no permission to read), so "used" will
    # normally exceed what we can attribute — and that gap is itself worth
    # showing rather than hiding.
    capacity = used = free = 0
    try:
        capacity, used, free = shutil.disk_usage(root)
    except OSError:
        pass

    return {
        "root": root,
        "total": total,
        "file_count": file_count,
        "disk_capacity": capacity,
        "disk_used": used,
        "disk_free": free,
        "unattributed": max(0, used - total),
        "tree": tree,
        "children": tree["children"],
        "categories": dict(sorted(categories.items(), key=lambda kv: kv[1], reverse=True)),
        "stopped": stopped,
        "max_depth": max_depth,
        "min_node_bytes": min_node_bytes,
    }


def _gb(n: int) -> str:
    return f"{n / (1024 ** 3):.1f}"
