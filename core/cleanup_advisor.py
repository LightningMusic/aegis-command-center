"""
core/cleanup_advisor.py
=======================
Turns a disk_map scan into a ranked "here is what to clear out" list.

Two questions drive the ranking:

  * How much space would this actually free?
  * How long since anything in it changed?

...and one that makes a recommendation safe to act on:

  * Is this already sitting on the backup drive?

Matching against the backup is by filename + exact byte size, the same
standard the phone-backup manifest uses. That is deliberately a *fast*
check: it indexes a multi-terabyte backup drive in seconds, so the answer
is available while you are looking at the list rather than minutes later.
It is not proof of identical content — two different files can share a name
and an exact size — so nothing here deletes anything. It reports, and says
plainly how sure it is.
"""

from __future__ import annotations

import os
import pickle
import re
import time
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Set

BASE_DIR = Path(__file__).resolve().parent.parent
CACHE_DIR = BASE_DIR / ".backup_index_cache"

StopCB = Callable[[], bool]
ProgressCB = Callable[[str], None]

# Folders that are never a sensible "just delete it" suggestion, however big
# or stale they look.
_NEVER_SUGGEST = (
    "\\windows",
    "\\program files\\windowsapps",
    "\\programdata\\microsoft\\windows",
    "\\$recycle.bin",
    "\\system volume information",
    "\\appdata\\local\\microsoft\\windows",
)

_DAY = 86400.0


def _cache_file(backup_root: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9]+", "_", backup_root).strip("_").lower() or "root"
    return CACHE_DIR / f"{safe}.index"


def load_cached_index(backup_root: str, max_age_hours: float = 24.0) -> Optional[Set[int]]:
    """Return a previously built index if it's still fresh, else None."""
    path = _cache_file(backup_root)
    try:
        age_hours = (time.time() - path.stat().st_mtime) / 3600.0
        if age_hours > max_age_hours:
            return None
        with open(path, "rb") as fh:
            data = pickle.load(fh)
        if isinstance(data, set):
            return data
    except (OSError, pickle.PickleError, EOFError, AttributeError, ValueError):
        pass
    return None


def cached_index_age_hours(backup_root: str) -> Optional[float]:
    """How old the stored index is, or None if there isn't one."""
    try:
        return (time.time() - _cache_file(backup_root).stat().st_mtime) / 3600.0
    except OSError:
        return None


def build_backup_index(
    backup_root: str,
    *,
    use_cache: bool = True,
    max_age_hours: float = 24.0,
    should_stop: Optional[StopCB] = None,
    progress: Optional[ProgressCB] = None,
) -> Set[int]:
    """
    Index a backup drive as a set of hashed (lowercased name, size) pairs.

    Hashes rather than the tuples themselves: a few million entries as
    strings costs hundreds of MB, as ints it is a fraction of that, and the
    only operation needed is membership.

    Walking a multi-terabyte backup drive is genuinely slow — 4.2 TB of it
    measured at ~22 minutes here — so the result is cached to disk and
    reused. Nothing about this needs to be up to the minute: it answers
    "does a copy of this already exist on the backup drive", and a copy that
    existed this morning still exists this afternoon.
    """
    if use_cache:
        cached = load_cached_index(backup_root, max_age_hours=max_age_hours)
        if cached is not None:
            if progress:
                progress(f"Using the stored index of {backup_root} ({len(cached):,} files)")
            return cached

    index: Set[int] = set()
    seen = 0
    stack = [backup_root]
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
                    index.add(hash((entry.name.lower(), entry.stat(follow_symlinks=False).st_size)))
                    seen += 1
                    if progress and seen % 50000 == 0:
                        progress(f"Indexing {backup_root} — {seen:,} files")
            except OSError:
                continue
    if progress:
        progress(f"Indexed {backup_root} — {seen:,} files")

    # Don't cache a half-built index from a cancelled run; it would look
    # authoritative and quietly under-report what's backed up.
    if not (should_stop and should_stop()):
        try:
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            tmp = _cache_file(backup_root).with_suffix(".tmp")
            with open(tmp, "wb") as fh:
                pickle.dump(index, fh, protocol=pickle.HIGHEST_PROTOCOL)
            os.replace(tmp, _cache_file(backup_root))
        except (OSError, pickle.PickleError):
            pass

    return index


def folder_backup_coverage(
    folder: str,
    index: Set[int],
    *,
    sample_limit: int = 400,
    should_stop: Optional[StopCB] = None,
) -> Dict[str, float]:
    """
    What fraction of the files in *folder* have a name+size match in *index*.

    Stops after sample_limit files: a folder with 60,000 files does not need
    all of them checked to tell whether it is backed up, and the whole point
    of this check is that it stays fast enough to run over a whole drive.
    """
    checked = matched = 0
    stack = [folder]
    while stack and checked < sample_limit:
        if should_stop and should_stop():
            break
        current = stack.pop()
        try:
            entries = list(os.scandir(current))
        except OSError:
            continue
        for entry in entries:
            if checked >= sample_limit:
                break
            try:
                if entry.is_dir(follow_symlinks=False):
                    stack.append(entry.path)
                elif entry.is_file(follow_symlinks=False):
                    checked += 1
                    if hash((entry.name.lower(), entry.stat(follow_symlinks=False).st_size)) in index:
                        matched += 1
            except OSError:
                continue

    return {
        "checked": checked,
        "matched": matched,
        "ratio": (matched / checked) if checked else 0.0,
        "sampled": checked >= sample_limit,
    }


def _iter_nodes(node: Dict, depth: int = 0) -> Iterable[Dict]:
    # No depth cap: an individual Steam game sits five levels down, and
    # capping at three meant the list could only ever offer "steamapps"
    # rather than the specific game you might actually uninstall.
    yield node
    for child in node.get("children") or []:
        yield from _iter_nodes(child, depth + 1)


def unit_boundaries(drive: str) -> Dict[str, str]:
    """
    Paths that are a *thing you would actually remove*, mapped to a label.

    Without this, "keep the most specific folder" produces rows like
    "HITMAN 3\\Runtime" and "Counter-Strike Global Offensive\\game" — real
    piles of bytes, but nothing anyone would delete on purpose. You
    uninstall the game, not one folder inside it. Steam manifests and the
    installed-program roots already know where those boundaries are, so a
    candidate that lands inside one gets reported as the game or program it
    belongs to.
    """
    units: Dict[str, str] = {}
    try:
        from core.app_scanner import installed_programs, steam_games
    except Exception:  # noqa: BLE001
        return units

    try:
        for game in steam_games(drive):
            path = (game.get("path") or "").lower().rstrip("\\")
            if path:
                units[path] = f"{game.get('name', '?')} (Steam game)"
    except Exception:  # noqa: BLE001
        pass

    try:
        for prog in installed_programs(drive, min_bytes=1024 ** 3):
            path = (prog.get("path") or "").lower().rstrip("\\")
            if path and path not in units:
                units[path] = f"{prog.get('name', '?')} ({prog.get('location', 'installed')})"
    except Exception:  # noqa: BLE001
        pass

    return units


def _snap_to_units(candidates: List[Dict], units: Dict[str, str]) -> List[Dict]:
    """Replace any candidate sitting inside a known unit with the unit itself."""
    if not units:
        return candidates

    # Longest first, so a game inside a launcher snaps to the game.
    ordered = sorted(units, key=len, reverse=True)
    best: Dict[str, Dict] = {}

    for candidate in candidates:
        low = candidate["path"].lower().rstrip("\\")
        owner = None
        for unit in ordered:
            if low == unit or low.startswith(unit + "\\"):
                owner = unit
                break

        if owner is None:
            best.setdefault(low, candidate)
            continue

        existing = best.get(owner)
        if existing is None or candidate["size_bytes"] > existing["size_bytes"]:
            promoted = dict(candidate)
            if low != owner:
                promoted["path"] = candidate["path"][:len(owner)]
                promoted["name"] = units[owner]
                # The subfolder's size understates the unit; leave the real
                # total to be filled in by whoever knows it, but never claim
                # the part is the whole.
                promoted["partial_of_unit"] = True
            else:
                promoted["name"] = units[owner]
            best[owner] = promoted

    return list(best.values())


def _drop_ancestors(candidates: List[Dict]) -> List[Dict]:
    """
    Keep only the most specific candidate on each branch.

    Without this the list is mostly the same bytes repeated: Program Files
    (x86) 532 GB, then Steam 511 GB inside it, then steamapps 499 GB inside
    that — three rows, one pile of data, none of them something you would
    actually act on. Dropping any candidate that contains another leaves the
    actionable unit: the individual game, project, or cache folder.

    A folder whose bulk is spread across many children too small to qualify
    on their own has no surviving descendant, so it is kept.
    """
    prefixes = [c["path"].lower().rstrip("\\") + "\\" for c in candidates]
    keep: List[Dict] = []
    for i, candidate in enumerate(candidates):
        mine = prefixes[i]
        if any(j != i and other.startswith(mine) for j, other in enumerate(prefixes)):
            continue
        keep.append(candidate)
    return keep


def recommend(
    scan: Dict,
    *,
    backup_index: Optional[Set[int]] = None,
    min_bytes: int = 5 * 1024 ** 3,
    stale_days: int = 180,
    limit: int = 40,
    units: Optional[Dict[str, str]] = None,
    should_stop: Optional[StopCB] = None,
    progress: Optional[ProgressCB] = None,
) -> List[Dict]:
    """
    Rank folders from a disk_map scan as cleanup candidates.

    Sorted by how much space is recoverable, weighted by how long it has sat
    untouched, with anything already on the backup drive floated to the top —
    that is the one case where deleting costs you nothing.
    """
    tree = scan.get("tree") or {}
    now = time.time()
    candidates: List[Dict] = []
    nodes_by_path: Dict[str, Dict] = {}

    for node in _iter_nodes(tree):
        path_key = (node.get("path") or "").lower().rstrip("\\")
        if path_key:
            nodes_by_path[path_key] = node
        if should_stop and should_stop():
            break
        path = node.get("path") or ""
        if not path or path == tree.get("path"):
            continue
        size = int(node.get("size") or 0)
        if size < min_bytes:
            continue
        low = path.lower()
        if any(skip in low for skip in _NEVER_SUGGEST):
            continue

        mtime = float(node.get("newest_mtime") or 0.0)
        age_days = int((now - mtime) / _DAY) if mtime > 0 else -1

        gb = size / (1024 ** 3)
        score = gb
        if age_days > stale_days:
            score *= 1.0 + min(age_days / 365.0, 3.0)

        candidates.append({
            "path": path,
            "name": node.get("name") or path,
            "size_bytes": size,
            "file_count": int(node.get("file_count") or 0),
            "age_days": age_days,
            "newest_mtime": mtime,
            "backup": None,
            "score": score,
        })

    if units is None:
        units = unit_boundaries(scan.get("root") or "")
    candidates = _snap_to_units(candidates, units)

    # Snapping a subfolder up to its game leaves the subfolder's size
    # attached to the game's name, which would under-report it. The scan
    # already measured the unit itself, so use that.
    for candidate in candidates:
        if not candidate.pop("partial_of_unit", False):
            continue
        node = nodes_by_path.get(candidate["path"].lower().rstrip("\\"))
        if not node:
            continue
        candidate["size_bytes"] = int(node.get("size") or 0)
        candidate["file_count"] = int(node.get("file_count") or 0)
        mtime = float(node.get("newest_mtime") or 0.0)
        candidate["newest_mtime"] = mtime
        candidate["age_days"] = int((now - mtime) / _DAY) if mtime > 0 else -1
        gb = candidate["size_bytes"] / (1024 ** 3)
        score = gb
        if candidate["age_days"] > stale_days:
            score *= 1.0 + min(candidate["age_days"] / 365.0, 3.0)
        candidate["score"] = score

    candidates = _drop_ancestors(candidates)
    candidates.sort(key=lambda r: r["score"], reverse=True)
    candidates = candidates[:limit]

    # The backup check reads directories, so it only runs on the shortlist
    # that survived — never on the dozens of rows that were about to be
    # discarded as duplicates or trimmed by the limit.
    if backup_index:
        for candidate in candidates:
            if should_stop and should_stop():
                break
            if progress:
                progress(f"Checking {candidate['name']} against the backup drive")
            coverage = folder_backup_coverage(
                candidate["path"], backup_index, should_stop=should_stop
            )
            candidate["backup"] = coverage
            # A confirmed copy elsewhere is the strongest signal there is:
            # it is the one case where removing this costs nothing.
            if coverage["ratio"] >= 0.95:
                candidate["score"] *= 4.0
            elif coverage["ratio"] >= 0.5:
                candidate["score"] *= 1.5

    for candidate in candidates:
        candidate["reason"] = _reason(
            candidate["age_days"], stale_days, candidate["backup"]
        )

    candidates.sort(key=lambda r: r["score"], reverse=True)
    return candidates


def _reason(age_days: int, stale_days: int, coverage: Optional[Dict]) -> str:
    bits: List[str] = []
    if coverage and coverage["checked"]:
        pct = coverage["ratio"] * 100
        where = "of a sample" if coverage["sampled"] else "of its files"
        if pct >= 95:
            bits.append(f"already on the backup drive ({pct:.0f}% {where} matched by name and size)")
        elif pct >= 50:
            bits.append(f"partly backed up ({pct:.0f}% {where} matched)")
        else:
            bits.append(f"NOT backed up ({pct:.0f}% {where} matched)")
    if age_days < 0:
        bits.append("no modification date available")
    elif age_days > stale_days:
        bits.append(f"nothing changed in {age_days} days")
    else:
        bits.append(f"active — changed {age_days} days ago")
    return "; ".join(bits)
