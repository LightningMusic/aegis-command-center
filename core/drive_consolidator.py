"""
core/drive_consolidator.py
==========================
Compare two locations (whole drives or folders), decide what to do with every
file on the SOURCE so it can be consolidated onto the TARGET, then carry it out
with verification.

Core guarantee (double-verified)
--------------------------------
A source file is NEVER deleted until a byte-for-byte identical copy is proven to
exist on the target. For every MOVE:

  1. the SOURCE is hashed again at apply time (the plan's hash is not trusted)
  2. the copy is written and fsync()'d to the device
  3. verify #1  — the freshly written temp file is hashed; SHA-256 + size must
     equal the source
  4. the temp file is atomically promoted to its final name
  5. verify #2  — the final file is read back from disk independently; SHA-256 +
     size must equal the source again
  6. only now is the source deleted, and afterwards we confirm the source is
     gone and the target copy still exists

For files reported as already present on the target, both files are fully read
and their hashes + sizes compared at apply time before the source is removed;
if that check fails the entry is turned into a real verified move instead.

Every step is written to a JSONL journal in __Aegis_Consolidation__ on the
target. Deletions go to the Recycle Bin by default (quarantine folder if that
fails) — never an unrecoverable wipe.

Pipeline
--------
    planner = ConsolidationPlanner(source, target, hash_cache=DbHashCache(db))
    plan    = planner.build_plan(progress=cb, should_stop=flag)   # read-only
    # user reviews / toggles plan.entries in the UI
    result  = planner.execute(plan, progress=cb, should_stop=flag)

Actions
-------
    MOVE                 not on target -> copy across, verify, delete source
    ALREADY_THERE        identical file already at same relative path
    DUPLICATE_ELSEWHERE  identical content already on target, different path
    CONFLICT             same relative path, different content (needs a choice)
    JUNK                 temp/cache/regenerable -> delete, don't move
    CORRUPT              unreadable or failed a structural check -> leave alone
    SKIP                 protected / system / outside scope
"""

from __future__ import annotations

import os
import shutil
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional

from core.integrity import verify_deep, verify_playthrough, verify_readable
from core.util import human_bytes

ProgressCB = Callable[[str, int, int, str], None]
StopCB = Callable[[], bool]

# ── what we never touch ─────────────────────────────────────────────────────
_PROTECTED_DIRS = {
    "windows", "program files", "program files (x86)", "programdata",
    "$recycle.bin", "system volume information", "recovery", "perflogs",
    "config.msi", "msocache", "$windows.~bt", "$windows.~ws",
    "__aegis_consolidation__", "__aegis_quarantine__",
}
_PROTECTED_NAMES = {"pagefile.sys", "hiberfil.sys", "swapfile.sys", "bootmgr", "ntldr"}

# ── junk heuristics ────────────────────────────────────────────────────────
_JUNK_DIRS = {
    "temp", "tmp", "cache", ".cache", "caches", "temporary internet files",
    "crashdumps", "crash reports", "gpucache", "code cache", "blob_storage",
    "$recycle.bin",
}
_JUNK_AGGRESSIVE_DIRS = {"node_modules", "windows.old", ".gradle", ".nuget", "__pycache__"}
_JUNK_NAMES = {"thumbs.db", "desktop.ini", ".ds_store", "iconcache.db", "ehthumbs.db"}
_JUNK_EXTS = {".tmp", ".temp", ".crdownload", ".part", ".partial", ".download",
              ".dmp", ".chk", ".~tmp"}
_LOGISH_EXTS = {".log", ".etl", ".old", ".bak"}
_OLD_DAYS = 120


@dataclass
class PlanEntry:
    source_path: str
    rel_path: str
    size: int
    mtime: float
    action: str
    target_path: str = ""
    reason: str = ""
    integrity: str = ""
    integrity_detail: str = ""
    sha: str = ""
    selected: bool = True
    resolution: str = ""            # for CONFLICT: keep_source|keep_target|keep_both

    def as_row(self) -> dict:
        return asdict(self)


@dataclass
class Plan:
    source_root: str
    target_root: str
    entries: List[PlanEntry] = field(default_factory=list)
    source_label: str = "SOURCE"
    scanned_files: int = 0
    scanned_bytes: int = 0
    errors: List[str] = field(default_factory=list)

    # ── summary the UI shows ──────────────────────────────────────────────
    def summary(self) -> Dict[str, Dict[str, int]]:
        out: Dict[str, Dict[str, int]] = {}
        for e in self.entries:
            b = out.setdefault(e.action, {"files": 0, "bytes": 0, "selected": 0, "sel_bytes": 0})
            b["files"] += 1
            b["bytes"] += e.size
            if e.selected:
                b["selected"] += 1
                b["sel_bytes"] += e.size
        return out

    def reclaimable_bytes(self) -> int:
        """Bytes freed on the source if the selected plan is executed."""
        freed = 0
        for e in self.entries:
            if not e.selected:
                continue
            if e.action in ("MOVE", "ALREADY_THERE", "DUPLICATE_ELSEWHERE", "JUNK"):
                freed += e.size
            elif e.action == "CONFLICT" and e.resolution in ("keep_target", "keep_source", "keep_both"):
                if e.resolution != "keep_both":
                    freed += e.size
        return freed

    def target_needed_bytes(self) -> int:
        need = 0
        for e in self.entries:
            if not e.selected:
                continue
            if e.action == "MOVE":
                need += e.size
            elif e.action == "CONFLICT" and e.resolution in ("keep_source", "keep_both"):
                need += e.size
        return need


# ── hash cache — persists across runs so unchanged files are read once ─────
class DbHashCache:
    """
    Remembers the SHA-256 (and deep/play-through result) of every file it has
    seen, keyed by absolute path + size + mtime. On the next run, a file whose
    size and mtime are unchanged is trusted from the cache and never re-read —
    that's what makes the 2nd/3rd consolidation of a big drive fast.

    Backed by table ``aegis_consolidation_cache`` in aegis.db. Also falls back
    to hashes the Scanner already computed in the ``files`` table (matched on
    size + mtime, so it can't return a stale hash for a changed file).
    """

    _TABLE = "aegis_consolidation_cache"

    def __init__(self, db=None, *, ignore: bool = False):
        self.db = db
        self.ignore = ignore
        self._mem: Dict[str, tuple] = {}       # path -> (sig, sha)
        self._mem_deep: Dict[str, tuple] = {}  # path -> (sig, kind, status, detail)
        self.hits = 0
        self.misses = 0
        if self.db is not None:
            try:
                self.db.execute(
                    f"""CREATE TABLE IF NOT EXISTS {self._TABLE} (
                        path TEXT PRIMARY KEY,
                        size INTEGER, mtime INTEGER,
                        sha TEXT, sha_at TEXT,
                        deep_kind TEXT, deep_status TEXT, deep_detail TEXT, deep_at TEXT
                    )"""
                )
            except Exception:
                self.db = None

    @staticmethod
    def _sig(size: int, mtime: float) -> str:
        return f"{int(size)}|{int(mtime)}"

    # ── content hash ─────────────────────────────────────────────────────
    def get(self, path: str, size: int, mtime: float) -> Optional[str]:
        if self.ignore:
            return None
        sig = self._sig(size, mtime)
        m = self._mem.get(path)
        if m and m[0] == sig:
            self.hits += 1
            return m[1]
        if self.db is not None:
            try:
                rows = self.db.fetchall(
                    f"SELECT size, mtime, sha FROM {self._TABLE} WHERE path = ?", (path,)
                )
            except Exception:
                rows = []
            for s, mt, sha in rows or []:
                if sha and sha.startswith("full:") and self._sig(s, mt) == sig:
                    self._mem[path] = (sig, sha)
                    self.hits += 1
                    return sha
            # reuse a hash the Scanner already computed (size + mtime must match)
            try:
                rows = self.db.fetchall(
                    "SELECT size_bytes, modified_at, hash FROM files "
                    "WHERE absolute_path = ?",
                    (path,),
                )
            except Exception:
                rows = []
            for s, mod, h in rows or []:
                if not (h and h.startswith("full:") and int(s or -1) == int(size)):
                    continue
                try:
                    mod_epoch = int(datetime.fromisoformat(mod).timestamp())
                except Exception:
                    continue
                if abs(mod_epoch - int(mtime)) <= 2:
                    self.put(path, size, mtime, h)  # promote for a strict hit next time
                    self.hits += 1
                    return h
        self.misses += 1
        return None

    def put(self, path: str, size: int, mtime: float, sha: str) -> None:
        if not sha or not sha.startswith("full:"):
            return
        self._mem[path] = (self._sig(size, mtime), sha)
        if self.db is None:
            return
        try:
            self.db.execute(
                f"""INSERT INTO {self._TABLE}(path, size, mtime, sha, sha_at)
                    VALUES(?, ?, ?, ?, ?)
                    ON CONFLICT(path) DO UPDATE SET
                        size=excluded.size, mtime=excluded.mtime,
                        sha=excluded.sha, sha_at=excluded.sha_at""",
                (path, int(size), int(mtime), sha,
                 datetime.now().isoformat(timespec="seconds")),
            )
        except Exception:
            pass

    # ── deep / play-through result ──────────────────────────────────────
    def get_deep(self, path: str, size: int, mtime: float, kind: str):
        if self.ignore:
            return None
        sig = self._sig(size, mtime)
        m = self._mem_deep.get(path)
        if m and m[0] == sig and m[1] == kind:
            return (m[2], m[3])
        if self.db is None:
            return None
        try:
            rows = self.db.fetchall(
                f"SELECT size, mtime, deep_kind, deep_status, deep_detail "
                f"FROM {self._TABLE} WHERE path = ?",
                (path,),
            )
        except Exception:
            return None
        for s, mt, dk, ds, dd in rows or []:
            if ds and dk == kind and self._sig(s, mt) == sig:
                self._mem_deep[path] = (sig, kind, ds, dd or "")
                return (ds, dd or "")
        return None

    def put_deep(self, path: str, size: int, mtime: float, kind: str,
                 status: str, detail: str) -> None:
        self._mem_deep[path] = (self._sig(size, mtime), kind, status, detail or "")
        if self.db is None:
            return
        try:
            self.db.execute(
                f"""INSERT INTO {self._TABLE}
                    (path, size, mtime, deep_kind, deep_status, deep_detail, deep_at)
                    VALUES(?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(path) DO UPDATE SET
                        size=excluded.size, mtime=excluded.mtime,
                        deep_kind=excluded.deep_kind, deep_status=excluded.deep_status,
                        deep_detail=excluded.deep_detail, deep_at=excluded.deep_at""",
                (path, int(size), int(mtime), kind, status, detail or "",
                 datetime.now().isoformat(timespec="seconds")),
            )
        except Exception:
            pass


# ── planner ───────────────────────────────────────────────────────────────
class ConsolidationPlanner:
    def __init__(
        self,
        source_root: str,
        target_root: str,
        *,
        hash_cache: Optional[DbHashCache] = None,
        deep_verify: bool = False,
        playthrough: bool = False,
        aggressive_junk: bool = False,
        use_recycle_bin: bool = True,
        deletion_mode: Optional[str] = None,
        hash_workers: int = 4,
    ) -> None:
        self.source_root = str(Path(source_root))
        self.target_root = str(Path(target_root))
        self.hash_cache = hash_cache or DbHashCache(None)
        # playthrough implies deep_verify (it's the strongest tier).
        self.deep_verify = deep_verify or playthrough
        self.playthrough = playthrough
        self.aggressive_junk = aggressive_junk
        # How a verified-redundant source file is removed:
        #   "recycle"    -> Recycle Bin (recoverable, but still uses the drive
        #                   until you empty it)
        #   "quarantine" -> moved to __Aegis_Quarantine__ on the source drive
        #   "permanent"  -> os.remove(); frees space immediately, no undo
        # Only ever reached AFTER the copy is double-verified on the target.
        self.deletion_mode = deletion_mode or ("recycle" if use_recycle_bin else "quarantine")
        self.use_recycle_bin = self.deletion_mode == "recycle"
        self.hash_workers = max(1, int(hash_workers))
        self._readfail: Dict[str, tuple] = {}
        self._prehashed: Dict[str, str] = {}

        self._validate_roots()

    # ── validation ──────────────────────────────────────────────────────
    def _validate_roots(self) -> None:
        s = Path(self.source_root).resolve()
        t = Path(self.target_root).resolve()
        if not s.exists() or not s.is_dir():
            raise ValueError(f"Source folder does not exist: {self.source_root}")
        if not t.exists() or not t.is_dir():
            raise ValueError(f"Target folder does not exist: {self.target_root}")
        if s == t:
            raise ValueError("Source and target are the same location.")
        sd = os.path.splitdrive(str(s))[0].lower()
        td = os.path.splitdrive(str(t))[0].lower()
        if sd and td and sd == td:
            raise ValueError(
                f"Source and target are on the same drive ({sd.upper()}). "
                "Consolidation moves data from one drive to another to free space - "
                "pick two different drives."
            )
        try:
            s.relative_to(t)
            raise ValueError("Source is inside the target.")
        except ValueError as exc:
            if "inside the target" in str(exc):
                raise
        try:
            t.relative_to(s)
            raise ValueError("Target is inside the source.")
        except ValueError as exc:
            if "inside the source" in str(exc):
                raise

    # ── helpers ─────────────────────────────────────────────────────────
    @staticmethod
    def _is_protected(rel_parts: tuple, name: str) -> bool:
        if name.lower() in _PROTECTED_NAMES:
            return True
        return any(p.lower() in _PROTECTED_DIRS for p in rel_parts)

    def _classify_junk(self, rel_parts: tuple, name: str, size: int, mtime: float) -> Optional[str]:
        lower = name.lower()
        ext = os.path.splitext(lower)[1]
        parts_lower = {p.lower() for p in rel_parts}

        if lower in _JUNK_NAMES:
            return f"junk file name ({name})"
        if lower.startswith("~$"):
            return "office lock file"
        if ext in _JUNK_EXTS:
            return f"temporary file ({ext})"
        if parts_lower & _JUNK_DIRS:
            return f"inside a cache/temp folder"
        if self.aggressive_junk and (parts_lower & _JUNK_AGGRESSIVE_DIRS):
            return "regenerable folder (node_modules / windows.old / build cache)"
        if ext in _LOGISH_EXTS:
            age = (time.time() - mtime) / 86400
            if age > _OLD_DAYS:
                return f"{ext} file, not modified in {int(age)} days"
        return None

    def _walk(self, root: str, phase: str, progress: Optional[ProgressCB],
              should_stop: Optional[StopCB]) -> Dict[str, os.stat_result]:
        """rel_path (lowercased, / separated) -> stat. Skips protected trees."""
        out: Dict[str, os.stat_result] = {}
        root_path = Path(root)
        count = 0
        for dirpath, dirnames, filenames in os.walk(root, topdown=True):
            if should_stop and should_stop():
                break
            rel_dir = Path(dirpath).relative_to(root_path)
            rel_parts = tuple() if str(rel_dir) == "." else rel_dir.parts
            # prune protected / junk-container dirs from descent
            dirnames[:] = [
                d for d in dirnames
                if d.lower() not in _PROTECTED_DIRS
                and not self._is_protected(rel_parts + (d,), d)
            ]
            for fn in filenames:
                full = os.path.join(dirpath, fn)
                try:
                    st = os.stat(full)
                except OSError:
                    continue
                rel = (str(rel_dir / fn) if rel_parts else fn).replace("\\", "/")
                out[rel.lower()] = st
                count += 1
                if progress and count % 2000 == 0:
                    progress(phase, count, 0, f"{count:,} files  ({rel})")
        if progress:
            progress(phase, count, count, f"{count:,} files indexed")
        return out

    def _integrity_check(self, path: str) -> tuple:
        """Deep structural check, or a full frame/page decode when playthrough is on."""
        if self.playthrough:
            return verify_playthrough(path)
        return verify_deep(path)

    def _hash(self, path: str, size: int, mtime: float) -> tuple:
        fail = self._readfail.get(path)
        if fail:
            return fail[0], fail[1], ""
        pre = self._prehashed.get(path)
        if pre:
            return "ok", "", pre
        cached = self.hash_cache.get(path, size, mtime)
        if cached:
            return "ok", "", cached
        status, detail, sha = verify_readable(path)
        if sha:
            self.hash_cache.put(path, size, mtime, sha)
        return status, detail, sha

    def _prehash_source(self, src: Dict[str, os.stat_result],
                        progress: Optional[ProgressCB],
                        should_stop: Optional[StopCB]) -> None:
        """
        Hash every source file that isn't already cached, in parallel. Unchanged
        files (same size + mtime as a previous run) are skipped entirely, so a
        repeat consolidation reads almost nothing. Results feed the cache; the
        main planning loop then gets instant cache hits.
        """
        todo: List[tuple] = []
        for rel, st in src.items():
            name = os.path.basename(rel)
            rel_parts = tuple(rel.split("/")[:-1])
            if self._is_protected(rel_parts + (name,), name):
                continue
            if self._classify_junk(rel_parts, name, st.st_size, st.st_mtime):
                continue
            full = os.path.join(self.source_root, rel.replace("/", os.sep))
            if self.hash_cache.get(full, st.st_size, st.st_mtime) is not None:
                continue
            todo.append((full, st.st_size, st.st_mtime))

        cached_n = len(src) - len(todo)
        if progress:
            progress("hash-source", 0, max(len(todo), 1),
                     f"{cached_n:,} of {len(src):,} unchanged since last run — "
                     f"hashing {len(todo):,}")
        if not todo:
            return

        from concurrent.futures import ThreadPoolExecutor, as_completed

        done = 0
        total = len(todo)
        with ThreadPoolExecutor(max_workers=self.hash_workers) as ex:
            futures = [ex.submit(_hash_job, job) for job in todo]
            try:
                for fut in as_completed(futures):
                    path, size, mtime, status, detail, sha = fut.result()
                    done += 1
                    if sha:
                        self._prehashed[path] = sha
                        self.hash_cache.put(path, size, mtime, sha)
                    elif status != "ok":
                        self._readfail[path] = (status, detail)
                    if progress and done % 40 == 0:
                        progress("hash-source", done, total, f"{done:,}/{total:,} hashed")
                    if should_stop and should_stop():
                        break
            finally:
                ex.shutdown(wait=False, cancel_futures=True)
        if progress:
            progress("hash-source", total, total, "source hashing complete")

    # ── build the plan (read-only) ─────────────────────────────────────
    def build_plan(self, progress: Optional[ProgressCB] = None,
                   should_stop: Optional[StopCB] = None) -> Plan:
        plan = Plan(
            source_root=self.source_root,
            target_root=self.target_root,
            source_label=_drive_label(self.source_root),
        )

        self._readfail.clear()
        self._prehashed.clear()

        src = self._walk(self.source_root, "index-source", progress, should_stop)
        if should_stop and should_stop():
            return plan
        tgt = self._walk(self.target_root, "index-target", progress, should_stop)
        if should_stop and should_stop():
            return plan

        # Parallel, cache-aware hash pass over the source. On a repeat run this
        # is nearly instant because unchanged files are already cached.
        self._prehash_source(src, progress, should_stop)
        if should_stop and should_stop():
            return plan

        # target content index by size -> {sha: relpath}, filled lazily
        tgt_by_size: Dict[int, List[str]] = {}
        for rel, st in tgt.items():
            tgt_by_size.setdefault(st.st_size, []).append(rel)
        tgt_sha_cache: Dict[str, str] = {}

        def target_sha(rel: str) -> str:
            if rel in tgt_sha_cache:
                return tgt_sha_cache[rel]
            st = tgt[rel]
            full = os.path.join(self.target_root, rel.replace("/", os.sep))
            _s, _d, sha = self._hash(full, st.st_size, st.st_mtime)
            tgt_sha_cache[rel] = sha or ""
            return tgt_sha_cache[rel]

        total = len(src)
        done = 0
        for rel, st in src.items():
            if should_stop and should_stop():
                break
            done += 1
            if progress and done % 200 == 0:
                progress("plan", done, total, f"{done:,}/{total:,}  {rel}")

            full = os.path.join(self.source_root, rel.replace("/", os.sep))
            name = os.path.basename(rel)
            rel_parts = tuple(rel.split("/")[:-1])
            plan.scanned_files += 1
            plan.scanned_bytes += st.st_size

            entry = PlanEntry(
                source_path=full, rel_path=rel, size=st.st_size, mtime=st.st_mtime,
                action="MOVE", target_path=os.path.join(
                    self.target_root, rel.replace("/", os.sep)),
            )

            # 1. protected / system
            if self._is_protected(rel_parts + (name,), name):
                entry.action, entry.selected = "SKIP", False
                entry.reason = "protected system location"
                plan.entries.append(entry)
                continue

            # 2. junk
            junk = self._classify_junk(rel_parts, name, st.st_size, st.st_mtime)
            if junk:
                entry.action = "JUNK"
                entry.reason = junk
                entry.selected = True
                plan.entries.append(entry)
                continue

            # 3. read + hash source (this is also the readability check)
            status, detail, sha = self._hash(full, st.st_size, st.st_mtime)
            entry.sha = sha or ""
            if status in ("unreadable", "corrupt"):
                entry.action, entry.selected = "CORRUPT", False
                entry.integrity, entry.integrity_detail = status, detail
                entry.reason = f"cannot read source: {detail}"
                plan.entries.append(entry)
                continue
            entry.integrity = "ok"

            # 3b. optional deep structural check / full-decode play-through
            if self.deep_verify and status == "ok" and st.st_size > 0:
                kind = "playthrough" if self.playthrough else "structural"
                cached_deep = self.hash_cache.get_deep(full, st.st_size, st.st_mtime, kind)
                if cached_deep is not None:
                    d_status, d_detail = cached_deep
                else:
                    d_status, d_detail = self._integrity_check(full)
                    self.hash_cache.put_deep(
                        full, st.st_size, st.st_mtime, kind, d_status, d_detail
                    )
                if d_status == "corrupt":
                    entry.action, entry.selected = "CORRUPT", False
                    entry.integrity, entry.integrity_detail = "corrupt", d_detail
                    entry.reason = f"failed {os.path.splitext(name)[1]} integrity check: {d_detail}"
                    plan.entries.append(entry)
                    continue
                # "unverified" == couldn't fully decode (missing ffmpeg/Pillow/…);
                # not a failure, just flagged so the UI can show it.
                entry.integrity = d_status if d_status in ("ok", "unverified") else "ok"
                entry.integrity_detail = d_detail

            # 4. same relative path on target?
            if rel in tgt:
                if tgt[rel].st_size == st.st_size and sha and target_sha(rel) == sha:
                    entry.action = "ALREADY_THERE"
                    entry.reason = "identical copy already on target"
                    entry.selected = True
                else:
                    entry.action = "CONFLICT"
                    entry.selected = False
                    newer = "source" if st.st_mtime > tgt[rel].st_mtime + 2 else (
                        "target" if tgt[rel].st_mtime > st.st_mtime + 2 else "same-time")
                    entry.reason = (
                        f"different content at same path; "
                        f"{'source is newer' if newer == 'source' else 'target is newer' if newer == 'target' else 'timestamps match, bytes differ'}"
                        f" (src {_h(st.st_size)} / tgt {_h(tgt[rel].st_size)})"
                    )
                    entry.resolution = {"source": "keep_source", "target": "keep_target"}.get(newer, "")
                plan.entries.append(entry)
                continue

            # 5. identical content elsewhere on target?
            matched_rel = ""
            if sha:
                for cand in tgt_by_size.get(st.st_size, []):
                    if target_sha(cand) == sha:
                        matched_rel = cand
                        break
            if matched_rel:
                entry.action = "DUPLICATE_ELSEWHERE"
                entry.target_path = os.path.join(
                    self.target_root, matched_rel.replace("/", os.sep))
                entry.reason = f"identical content already on target at: {matched_rel}"
                entry.selected = True
                plan.entries.append(entry)
                continue

            # 6. genuinely new -> move it
            entry.action = "MOVE"
            entry.reason = "not present on target"
            entry.selected = True
            plan.entries.append(entry)

        plan.entries.sort(key=lambda e: (_ACTION_ORDER.get(e.action, 9), -e.size))
        return plan

    # ── execute (mutating) ────────────────────────────────────────────
    def execute(self, plan: Plan, progress: Optional[ProgressCB] = None,
                should_stop: Optional[StopCB] = None) -> Dict[str, object]:
        journal_dir = Path(self.target_root) / "__Aegis_Consolidation__"
        journal_dir.mkdir(parents=True, exist_ok=True)
        journal = journal_dir / f"journal_{datetime.now():%Y%m%d_%H%M%S}.jsonl"

        todo = [e for e in plan.entries if e.selected and e.action != "SKIP"]
        result = {
            "moved": 0, "moved_bytes": 0, "deleted": 0, "deleted_bytes": 0,
            "conflicts_resolved": 0, "errors": [], "skipped": 0,
            "journal": str(journal),
            "deletion_mode": self.deletion_mode,
            "source_free_before": _free(self.source_root),
        }
        total = len(todo)

        with open(journal, "w", encoding="utf-8") as jf:
            for i, e in enumerate(todo, 1):
                if should_stop and should_stop():
                    break
                if progress:
                    progress("execute", i, total, f"{e.action}  {e.rel_path}")
                try:
                    self._apply_entry(e, result, jf)
                except Exception as exc:  # noqa: BLE001
                    result["errors"].append(f"{e.rel_path}: {exc}")
                    _log(jf, e, "error", str(exc))

        result["source_free_after"] = _free(self.source_root)
        gained = result["source_free_after"] - result["source_free_before"]
        result["source_freed"] = max(gained, 0)
        removed = result["deleted_bytes"] + result["moved_bytes"]
        if self.deletion_mode != "permanent" and removed - result["source_freed"] > 64 * 1024 * 1024:
            where = "Recycle Bin" if self.deletion_mode == "recycle" else "__Aegis_Quarantine__ folder"
            result["space_note"] = (
                f"~{_h(removed - result['source_freed'])} of removed originals is still on "
                f"the source in the {where}. Empty it to reclaim that space "
                f"(or re-run with 'Permanent delete')."
            )
        return result

    def _apply_entry(self, e: PlanEntry, result: dict, jf) -> None:
        if e.action == "JUNK":
            # Junk is deleted by design; it still goes to the Recycle Bin /
            # quarantine, so it's recoverable. No copy is involved.
            if self._delete_source(e):
                result["deleted"] += 1
                result["deleted_bytes"] += e.size
                _log(jf, e, "deleted-junk", "")
            else:
                result["errors"].append(f"{e.rel_path}: could not delete")
            return

        if e.action in ("ALREADY_THERE", "DUPLICATE_ELSEWHERE"):
            # An identical copy is claimed to already be on the target. Prove it
            # *now* — full read of both files, hash + size must match — before
            # removing the source. If it doesn't hold, fall back to a real
            # verified move to this file's natural place on the target.
            keeper = e.target_path
            if not self._target_copy_matches_source(e, keeper, jf):
                _log(jf, e, "dup-recheck-failed-falling-back-to-move", keeper)
                natural = os.path.join(
                    self.target_root, e.rel_path.replace("/", os.sep)
                )
                if self._copy_verify_delete(e, natural, result, jf, tag="moved"):
                    result["moved"] += 1
                    result["moved_bytes"] += e.size
                return
            if self._delete_source_confirmed(e, keeper, result, jf, "deleted-duplicate"):
                result["deleted"] += 1
                result["deleted_bytes"] += e.size
            return

        if e.action == "MOVE":
            self._do_move(e, result, jf)
            return

        if e.action == "CONFLICT":
            if e.resolution == "keep_target":
                # User chose to discard the source content — but still confirm
                # the target file we're keeping is fully readable first.
                t_status, t_detail, _ = verify_readable(e.target_path)
                if t_status != "ok":
                    result["errors"].append(
                        f"{e.rel_path}: keep-target refused - target copy is {t_status} "
                        f"({t_detail}); source kept"
                    )
                    _log(jf, e, "keep-target-refused", t_detail)
                    return
                if self._delete_source_confirmed(e, e.target_path, result, jf,
                                                 "conflict-kept-target"):
                    result["deleted"] += 1
                    result["deleted_bytes"] += e.size
                    result["conflicts_resolved"] += 1
            elif e.resolution == "keep_source":
                self._backup_target_then_overwrite(e, result, jf)
                result["conflicts_resolved"] += 1
            elif e.resolution == "keep_both":
                aside = _dedupe_name(e.target_path, self.source_label)
                if self._copy_verify_delete(e, aside, result, jf, tag="conflict-kept-both"):
                    result["moved"] += 1
                    result["moved_bytes"] += e.size
                result["conflicts_resolved"] += 1
            else:
                result["skipped"] += 1
            return

        result["skipped"] += 1

    # ── move primitives ───────────────────────────────────────────────
    def _do_move(self, e: PlanEntry, result: dict, jf) -> None:
        if self._copy_verify_delete(e, e.target_path, result, jf, tag="moved"):
            result["moved"] += 1
            result["moved_bytes"] += e.size

    def _copy_verify_delete(self, e: PlanEntry, dest: str, result: dict, jf, tag: str) -> bool:
        """
        Copy source -> dest and only delete the source once the copy is proven
        good. Returns True iff the source was removed after a verified copy.

        Guarantees, in order:
          1. hash the SOURCE right now (never trust the plan's hash)
          2. write the copy and fsync it to the device
          3. verify #1  — hash the freshly written temp file, must equal (1) + size
          4. atomically promote temp -> dest
          5. verify #2  — RE-READ dest from disk, must equal (1) + size
          6. only now delete the source
          7. confirm the source is gone and dest still exists
        Any failure leaves the source untouched.
        """
        # 1. fresh source hash — mandatory
        s_status, s_detail, src_sha = verify_readable(e.source_path)
        try:
            src_size = os.path.getsize(e.source_path)
        except OSError as exc:
            src_size = -1
            s_status, s_detail = "unreadable", str(exc)
        if s_status != "ok" or not src_sha:
            result["errors"].append(
                f"{e.rel_path}: source re-check failed ({s_detail or 'no hash'}); "
                "nothing copied or deleted"
            )
            _log(jf, e, "source-recheck-failed", s_detail or "no hash")
            return False

        os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
        tmp = dest + ".aegis-part"
        _safe_unlink(tmp)

        # 2. copy + fsync
        try:
            _copy_with_fsync(e.source_path, tmp)
            try:
                shutil.copystat(e.source_path, tmp)
            except OSError:
                pass
        except OSError as exc:
            _safe_unlink(tmp)
            result["errors"].append(f"{e.rel_path}: copy failed ({exc}); source kept")
            _log(jf, e, "copy-failed", str(exc))
            return False

        # 3. verify #1 — the just-written temp file
        c_status, c_detail, copy_sha = verify_readable(tmp)
        if c_status != "ok" or copy_sha != src_sha or _size(tmp) != src_size:
            _safe_unlink(tmp)
            result["errors"].append(
                f"{e.rel_path}: copy verify #1 failed "
                f"({c_detail or 'hash/size mismatch'}); source kept"
            )
            _log(jf, e, "verify1-failed", c_detail or "hash/size mismatch")
            return False

        # 4. promote temp -> dest (atomic on one volume)
        try:
            os.replace(tmp, dest)
        except OSError as exc:
            _safe_unlink(tmp)
            result["errors"].append(
                f"{e.rel_path}: could not finalize copy ({exc}); source kept"
            )
            _log(jf, e, "finalize-failed", str(exc))
            return False

        # 5. verify #2 — independent re-read of the final file
        f_status, f_detail, final_sha = verify_readable(dest)
        if f_status != "ok" or final_sha != src_sha or _size(dest) != src_size:
            result["errors"].append(
                f"{e.rel_path}: copy verify #2 (read-back) failed "
                f"({f_detail or 'hash/size mismatch'}); source kept, "
                f"target copy left at {dest} for inspection"
            )
            _log(jf, e, "verify2-failed", f_detail or "hash/size mismatch")
            return False

        _log(jf, e, tag + "-verified", dest, sha=src_sha, size=src_size)

        # remember the new target file so a later Analyze doesn't re-hash it
        try:
            dst_st = os.stat(dest)
            self.hash_cache.put(dest, dst_st.st_size, dst_st.st_mtime, final_sha)
        except OSError:
            pass

        # 6-7. now it is safe to delete the source
        return self._delete_source_confirmed(e, dest, result, jf, tag)

    def _delete_source_confirmed(self, e: PlanEntry, keeper: str, result: dict,
                                 jf, tag: str) -> bool:
        """Delete the source, then confirm it is gone and the keeper survived."""
        if keeper and not os.path.exists(keeper):
            result["errors"].append(
                f"{e.rel_path}: refusing to delete source - keeper missing at {keeper}"
            )
            _log(jf, e, "keeper-missing-preflight", keeper)
            return False

        if not self._delete_source(e):
            result["errors"].append(
                f"{e.rel_path}: verified copy exists but the source could not be removed"
            )
            _log(jf, e, "delete-failed", keeper)
            return False

        if os.path.exists(e.source_path):
            result["errors"].append(
                f"{e.rel_path}: source still present after delete — treat as NOT deleted"
            )
            _log(jf, e, "post-delete-source-present", "")
            return False

        if keeper and not os.path.exists(keeper):
            result["errors"].append(
                f"{e.rel_path}: CRITICAL — keeper vanished right after source delete ({keeper})"
            )
            _log(jf, e, "post-delete-keeper-missing", keeper)
            return False

        _log(jf, e, tag, keeper)
        return True

    def _backup_target_then_overwrite(self, e: PlanEntry, result: dict, jf) -> None:
        backup = _dedupe_name(e.target_path, "replaced")
        try:
            os.replace(e.target_path, backup)
            _log(jf, e, "conflict-target-backed-up", backup)
        except OSError as exc:
            result["errors"].append(f"{e.rel_path}: could not back up target copy: {exc}")
            return
        if self._copy_verify_delete(e, e.target_path, result, jf, tag="conflict-kept-source"):
            result["moved"] += 1
            result["moved_bytes"] += e.size

    # ── deletion ──────────────────────────────────────────────────────
    def _delete_source(self, e: PlanEntry) -> bool:
        path = e.source_path
        if not os.path.exists(path):
            return True

        if self.deletion_mode == "permanent":
            # Reached only after the target copy is double-verified.
            try:
                os.remove(path)
                return True
            except OSError:
                return False

        if self.deletion_mode == "recycle" and _recycle(path):
            return True

        # quarantine mode, or a Recycle-Bin failure: move it aside on the
        # source drive (still occupies space — visible for review, then purge).
        try:
            qroot = Path(self.source_root) / "__Aegis_Quarantine__"
            dest = qroot / e.rel_path.replace("/", os.sep)
            dest.parent.mkdir(parents=True, exist_ok=True)
            os.replace(path, dest)
            return True
        except OSError:
            return False

    def _target_copy_matches_source(self, e: PlanEntry, keeper: str, jf) -> bool:
        """Full read of source and keeper *right now*; hash + size must match."""
        if not os.path.exists(keeper):
            return False
        s_status, _sd, src_sha = verify_readable(e.source_path)
        k_status, _kd, keeper_sha = verify_readable(keeper)
        if s_status != "ok" or k_status != "ok" or not src_sha:
            return False
        if src_sha != keeper_sha:
            return False
        try:
            return os.path.getsize(e.source_path) == os.path.getsize(keeper)
        except OSError:
            return False


# ── module helpers ────────────────────────────────────────────────────────
_ACTION_ORDER = {
    "CONFLICT": 0, "CORRUPT": 1, "MOVE": 2, "DUPLICATE_ELSEWHERE": 3,
    "ALREADY_THERE": 4, "JUNK": 5, "SKIP": 6,
}


def _h(n: int) -> str:
    return human_bytes(n)


def _drive_label(path: str) -> str:
    drive = os.path.splitdrive(os.path.abspath(path))[0]
    return drive.replace(":", "") or "SOURCE"


def _dedupe_name(path: str, tag: str) -> str:
    p = Path(path)
    stem, suffix = p.stem, p.suffix
    i = 0
    while True:
        cand = p.with_name(f"{stem} ({tag}{'' if i == 0 else f' {i}'}){suffix}")
        if not cand.exists():
            return str(cand)
        i += 1


def _safe_unlink(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


def _size(path: str) -> int:
    try:
        return os.path.getsize(path)
    except OSError:
        return -1


def _free(path: str) -> int:
    try:
        return shutil.disk_usage(path).free
    except OSError:
        return 0


def _hash_job(job: tuple) -> tuple:
    """Worker for the parallel pre-hash pass. Pure I/O, no shared state."""
    path, size, mtime = job
    status, detail, sha = verify_readable(path)
    return path, size, mtime, status, detail, sha


def _copy_with_fsync(src: str, dst: str, chunk: int = 4 * 1024 * 1024) -> None:
    """
    Byte copy that flushes to the storage device before returning, so the
    read-back verification isn't just re-reading our own write cache.
    """
    with open(src, "rb", buffering=0) as fsrc, open(dst, "wb", buffering=0) as fdst:
        while True:
            block = fsrc.read(chunk)
            if not block:
                break
            fdst.write(block)
        fdst.flush()
        try:
            os.fsync(fdst.fileno())
        except OSError:
            pass


def _recycle(path: str) -> bool:
    try:
        from win32com.shell import shell, shellcon  # type: ignore
    except ImportError:
        return False
    try:
        flags = shellcon.FOF_ALLOWUNDO | shellcon.FOF_NOCONFIRMATION | shellcon.FOF_SILENT
        rc, aborted = shell.SHFileOperation((0, shellcon.FO_DELETE, path, None, flags, None, None))
        return rc == 0 and not aborted
    except Exception:  # noqa: BLE001
        return False


def _log(jf, e: PlanEntry, event: str, detail: str, **extra) -> None:
    import json

    record = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "event": event, "action": e.action, "rel": e.rel_path,
        "size": e.size, "source": e.source_path, "target": e.target_path,
        "detail": detail,
    }
    record.update(extra)
    jf.write(json.dumps(record) + "\n")
    jf.flush()
