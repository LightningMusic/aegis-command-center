"""
core/app_scanner.py
===================
Detect installed applications and games directly from the filesystem / registry,
not from the Aegis file index (which deliberately skips Program Files and game
installs). This is what the "Games & Apps" view uses.

- Steam: find every library folder (registry + libraryfolders.vdf), read each
  appmanifest_*.acf for the game name and SizeOnDisk (no folder walk needed).
- Installed programs: size the top-level folders under Program Files /
  ProgramData / known launcher roots on a given drive.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Dict, List, Optional

# ── VDF / KeyValues (Valve's config format used by .acf + libraryfolders) ────

_VDF_TOKEN = re.compile(r'"((?:[^"\\]|\\.)*)"|([{}])')


def parse_vdf(text: str) -> dict:
    """Minimal nested KeyValues parser. Good enough for .acf and .vdf files."""
    stack: List[dict] = [{}]
    key: Optional[str] = None
    for quoted, brace in _VDF_TOKEN.findall(text):
        if brace == "{":
            node: dict = {}
            if key is not None:
                stack[-1][key] = node
            stack.append(node)
            key = None
        elif brace == "}":
            if len(stack) > 1:
                stack.pop()
            key = None
        else:
            value = quoted.replace('\\\\', '\\').replace('\\"', '"')
            if key is None:
                key = value
            else:
                stack[-1][key] = value
                key = None
    return stack[0]


# ── Steam ──────────────────────────────────────────────────────────────────

def _reg_value(hive, subkey: str, name: str) -> Optional[str]:
    try:
        import winreg  # type: ignore

        with winreg.OpenKey(hive, subkey) as key:
            return str(winreg.QueryValueEx(key, name)[0])
    except (OSError, ImportError):
        return None


def steam_root() -> Optional[Path]:
    try:
        import winreg  # type: ignore

        candidates = [
            _reg_value(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam", "SteamPath"),
            _reg_value(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam", "InstallPath"),
            _reg_value(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Valve\Steam", "InstallPath"),
        ]
    except ImportError:
        candidates = []

    candidates += [
        os.path.expandvars(r"%ProgramFiles(x86)%\Steam"),
        os.path.expandvars(r"%ProgramFiles%\Steam"),
        r"C:\Steam",
    ]
    for c in candidates:
        if not c:
            continue
        p = Path(c)
        if (p / "steamapps").is_dir() or (p / "steam.exe").exists():
            return p
    return None


def steam_library_paths() -> List[Path]:
    """Every Steam library folder on the system (may span drives)."""
    root = steam_root()
    paths: List[Path] = []
    seen: set[str] = set()

    def add(p) -> None:
        try:
            rp = Path(p).resolve()
        except OSError:
            return
        key = str(rp).lower()
        if key not in seen and (rp / "steamapps").is_dir():
            seen.add(key)
            paths.append(rp)

    if root:
        add(root)
        vdf = root / "steamapps" / "libraryfolders.vdf"
        if not vdf.exists():
            vdf = root / "config" / "libraryfolders.vdf"
        if vdf.exists():
            try:
                data = parse_vdf(vdf.read_text(encoding="utf-8", errors="ignore"))
            except OSError:
                data = {}
            folders = data.get("libraryfolders") or data.get("LibraryFolders") or {}
            for _k, entry in folders.items() if isinstance(folders, dict) else []:
                if isinstance(entry, dict) and entry.get("path"):
                    add(entry["path"])
                elif isinstance(entry, str) and len(entry) > 2 and entry[1] == ":":
                    add(entry)
    return paths


def steam_games(drive: Optional[str] = None) -> List[Dict]:
    """
    All installed Steam games. `drive` like 'C:\\' filters to that drive.
    Size comes from the manifest's SizeOnDisk; falls back to a folder walk.
    """
    drive_prefix = drive.rstrip("\\").lower() if drive else None
    out: List[Dict] = []

    for lib in steam_library_paths():
        steamapps = lib / "steamapps"
        try:
            manifests = list(steamapps.glob("appmanifest_*.acf"))
        except OSError:
            continue
        for acf in manifests:
            try:
                data = parse_vdf(acf.read_text(encoding="utf-8", errors="ignore"))
            except OSError:
                continue
            state = data.get("AppState") or {}
            name = state.get("name") or acf.stem
            installdir = state.get("installdir") or ""
            game_path = steamapps / "common" / installdir
            try:
                size = int(state.get("SizeOnDisk") or 0)
            except ValueError:
                size = 0
            if size <= 0 and game_path.exists():
                size = _dir_size(game_path)

            if drive_prefix and not str(game_path).lower().startswith(drive_prefix):
                continue

            out.append({
                "name": name,
                "appid": state.get("appid", ""),
                "size_bytes": size,
                "path": str(game_path),
                "library": str(lib),
            })

    out.sort(key=lambda g: g["size_bytes"], reverse=True)
    return out


# ── Installed programs (top folders under app roots) ────────────────────────

_APP_ROOT_NAMES = (
    "Program Files",
    "Program Files (x86)",
    "ProgramData",
    "Games",
    "XboxGames",
)
_LAUNCHER_SUBDIRS = (
    ("Program Files (x86)", "Steam", "steamapps", "common"),
    ("Program Files", "Epic Games"),
    ("Program Files (x86)", "Epic Games"),
    ("Program Files (x86)", "Ubisoft", "Ubisoft Game Launcher", "games"),
    ("Program Files", "EA Games"),
    ("Program Files (x86)", "EA Games"),
    ("Program Files (x86)", "Origin Games"),
    ("Program Files (x86)", "GOG Galaxy", "Games"),
    ("Program Files (x86)", "Battle.net"),
    ("Program Files (x86)", "Riot Games"),
)


def installed_programs(drive: str, min_bytes: int = 50 * 1024 * 1024,
                       should_stop=None) -> List[Dict]:
    """Top-level folders under the app roots on *drive*, sized by walking."""
    base = Path(drive if drive.endswith("\\") else drive + "\\")
    seen: set[str] = set()
    out: List[Dict] = []

    roots = [base / name for name in _APP_ROOT_NAMES]
    roots += [base.joinpath(*parts) for parts in _LAUNCHER_SUBDIRS]

    for root in roots:
        if not root.is_dir():
            continue
        try:
            children = list(os.scandir(root))
        except OSError:
            continue
        for child in children:
            if should_stop and should_stop():
                return out
            if not child.is_dir(follow_symlinks=False):
                continue
            key = child.path.lower()
            if key in seen:
                continue
            seen.add(key)
            size = _dir_size(Path(child.path), should_stop=should_stop)
            if size >= min_bytes:
                out.append({
                    "name": child.name,
                    "path": child.path,
                    "size_bytes": size,
                    "location": root.name,
                })

    _add_exclusive_sizes(out)
    out.sort(key=lambda p: p["size_bytes"], reverse=True)
    return out


def _add_exclusive_sizes(entries: List[Dict]) -> None:
    """
    Annotate entries that physically contain other entries.

    The app roots overlap on purpose: scanning "Program Files (x86)" finds
    Steam, and scanning "Steam\\steamapps\\common" finds each game. Both are
    worth showing, but Steam's size already includes every one of those
    games, so a flat list double-counts — on a real machine that reads as
    ~1.1 TB of programs on a 930 GB drive, and any size-ranked "what should
    I delete" recommendation built on it is nonsense.

    Adds, without changing size_bytes:
      exclusive_bytes - size not accounted for by other listed entries
      contains        - how many listed entries live inside this one
    """
    prepared = [(e, e["path"].lower().rstrip("\\") + "\\") for e in entries]

    for entry, prefix in prepared:
        nested_total = 0
        nested_count = 0
        for other, other_prefix in prepared:
            if other is entry:
                continue
            if other_prefix.startswith(prefix):
                nested_total += int(other.get("size_bytes") or 0)
                nested_count += 1
        entry["contains"] = nested_count
        entry["exclusive_bytes"] = max(0, int(entry.get("size_bytes") or 0) - nested_total)


# ── helper ─────────────────────────────────────────────────────────────────

def _dir_size(path: Path, should_stop=None) -> int:
    total = 0
    stack = [str(path)]
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
                if entry.is_dir(follow_symlinks=False):
                    stack.append(entry.path)
                elif entry.is_file(follow_symlinks=False):
                    total += entry.stat(follow_symlinks=False).st_size
            except OSError:
                continue
    return total
