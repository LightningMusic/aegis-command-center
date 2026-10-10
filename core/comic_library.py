"""
core/comic_library.py
=====================
Durable local archiving for a Tachiyomi/Mihon-style comic library.

The problem this solves is not "did the download work". It is that the app
owning the files is also the thing that removes them: when a source stops
listing a chapter — a series completing and moving behind a pass, a scanlation
site dropping a title — the app no longer believes that chapter exists and
clears the download it holds for it. Anything that lives only in the
app-managed ``downloads/`` tree is therefore held at the source's discretion,
not the reader's.

So the unit of safety here is not a successful copy, it is **a verified copy on
storage the app cannot reach**, and the number worth watching is how many
chapters do not have one yet (``exposure``).

Everything in this module is additive. Nothing deletes from a source library,
nothing overwrites a good file with a worse one, and every copy is verified by
content hash before it counts. The manifest written alongside a destination
records each chapter's hash, so the same archive can be re-checked months later
and silent corruption shows up as a mismatch rather than as a chapter that
won't open.

Commands::

    python -m core.comic_library verify      <library> [--deep]
    python -m core.comic_library exposure    <downloads> <local>
    python -m core.comic_library consolidate <dest> <source>... [--apply]
    python -m core.comic_library replicate   <src> <dest> [--apply]
    python -m core.comic_library prove       <dest> <source>...
    python -m core.comic_library compare     <a> <b>
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import zipfile
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

MANIFEST_NAME = ".aegis_library.json"

# Hashes of source files, keyed by absolute path and validated against the
# file's size and mtime. Without this, every plan re-reads the whole source
# library to re-learn hashes that cannot have changed — 187 GB off a USB disk,
# ten minutes, to reach the same answer as last time. A check that costs ten
# minutes is a check that stops being run, which defeats the point of having it.
HASH_CACHE_NAME = ".aegis_hashcache.json"

# Page formats a reader will actually render. A chapter archive containing none
# of these opens to a blank series however plausible its file size looks.
PAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif", ".bmp", ".jxl")

CHAPTER_EXTS = (".cbz", ".cbr", ".zip")

# Tachiyomi writes a chapter into "<name>_tmp" and renames it to "<name>.cbz"
# only once every page has landed, so a surviving _tmp is a download that died
# partway. They hold partial or no pages and no reader will show them.
TMP_SUFFIX = "_tmp"

# Decorations different sources bolt onto the same series name. Folded only for
# MATCHING titles across libraries — never for naming anything on disk, because
# the user's own folder names are the ones they recognise.
_TITLE_NOISE = (
    "(official)", "[official]", "(complete)", "(webtoon)", "(remake)",
    "(en)", "(all)", "(uncensored)", "(fan colored)",
)

ProgressCB = Callable[[str], None]


def _say(cb: Optional[ProgressCB], msg: str) -> None:
    if cb:
        cb(msg)


def normalise_title(name: str) -> str:
    """Fold a series folder name to something comparable across sources."""
    low = name.lower()
    for junk in _TITLE_NOISE:
        low = low.replace(junk, "")
    return "".join(c for c in low if c.isalnum())


def file_hash(path: Path, chunk: int = 1 << 20) -> str:
    """
    BLAKE2b-128 of the file's bytes.

    Not cryptographic strength on purpose: this detects corruption and proves
    two files are the same chapter, and at 128 bits an accidental collision is
    not a thing that happens to a library of this size. It is several times
    faster than SHA-256, which matters when the input is 200 GB on a USB disk.
    """
    h = hashlib.blake2b(digest_size=16)
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


def _load_hash_cache(where: Path) -> Dict[str, Dict[str, object]]:
    path = where / HASH_CACHE_NAME
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _save_hash_cache(where: Path, cache: Dict[str, Dict[str, object]]) -> None:
    try:
        where.mkdir(parents=True, exist_ok=True)
        (where / HASH_CACHE_NAME).write_text(
            json.dumps(cache, indent=0), encoding="utf-8")
    except OSError:
        pass


def _cached_hash(path: Path, size: int,
                 cache: Dict[str, Dict[str, object]]) -> Optional[str]:
    """
    A previously computed hash, if the file is demonstrably unchanged.

    Validated on size AND mtime rather than path alone: a chapter replaced by a
    re-download keeps its name, so trusting the path would hand back the old
    file's hash and quietly treat new content as already archived.
    """
    rec = cache.get(str(path))
    if not isinstance(rec, dict):
        return None
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None
    if rec.get("size") != size:
        return None
    cached_mtime = rec.get("mtime")
    if not isinstance(cached_mtime, (int, float)):
        return None
    # A second of slack: some filesystems and copy paths round mtime.
    if abs(float(cached_mtime) - mtime) > 1.0:
        return None
    digest = rec.get("hash")
    return digest if isinstance(digest, str) and digest else None


# ─────────────────────────────────────────────────────────────────────────────
# Inventory
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class Chapter:
    path: Path
    series: str          # the series folder's own name
    stem: str            # chapter filename without extension
    size: int
    origin: str          # which library/source folder it came from
    digest: Optional[str] = None


@dataclass
class LibraryIndex:
    root: Path
    chapters: List[Chapter] = field(default_factory=list)
    unfinished: Dict[str, int] = field(default_factory=dict)   # series -> _tmp count
    empty_series: List[str] = field(default_factory=list)
    stray: List[Path] = field(default_factory=list)

    @property
    def total_bytes(self) -> int:
        return sum(c.size for c in self.chapters)

    def by_series(self) -> Dict[str, List[Chapter]]:
        out: Dict[str, List[Chapter]] = defaultdict(list)
        for c in self.chapters:
            out[c.series].append(c)
        return dict(out)


def _series_dirs(root: Path) -> List[Tuple[Path, str]]:
    """
    Series folders under *root*, handling both library shapes.

    A Local-source library is ``root/<Series>/<Chapter>.cbz``. The app's
    download tree inserts a source level: ``root/<Source>/<Series>/...``.
    Detected rather than configured, because pointing this at the wrong level
    silently finds nothing.
    """
    out: List[Tuple[Path, str]] = []
    if not root.is_dir():
        return out
    for first in sorted(p for p in root.iterdir() if p.is_dir()):
        children = list(first.iterdir())
        has_chapters = any(
            f.is_file() and f.suffix.lower() in CHAPTER_EXTS for f in children
        )
        # A series whose only content is unfinished downloads has no chapter
        # files to recognise it by, and descending into it would report each
        # "<Chapter>_tmp" folder as a series of its own. The _tmp markers are
        # themselves proof this level is the series level.
        has_tmp = any(f.is_dir() and f.name.endswith(TMP_SUFFIX) for f in children)
        subdirs = [f for f in children if f.is_dir() and not f.name.endswith(TMP_SUFFIX)]
        if has_chapters or has_tmp or not subdirs:
            out.append((first, root.name))
        else:
            for second in sorted(subdirs):
                out.append((second, first.name))
    return out


def scan(root: str | Path, *, progress: Optional[ProgressCB] = None) -> LibraryIndex:
    """Inventory a library without opening any archive."""
    root = Path(root)
    idx = LibraryIndex(root=root)
    for series_dir, origin in _series_dirs(root):
        n_before = len(idx.chapters)
        tmp = 0
        for child in series_dir.iterdir():
            if child.is_dir():
                if child.name.endswith(TMP_SUFFIX):
                    tmp += 1
                continue
            if child.suffix.lower() in CHAPTER_EXTS:
                try:
                    idx.chapters.append(Chapter(
                        path=child, series=series_dir.name, stem=child.stem,
                        size=child.stat().st_size, origin=origin,
                    ))
                except OSError:
                    idx.stray.append(child)
            elif child.name != MANIFEST_NAME:
                idx.stray.append(child)
        if tmp:
            idx.unfinished[f"{origin}/{series_dir.name}"] = tmp
        if len(idx.chapters) == n_before and not tmp:
            idx.empty_series.append(f"{origin}/{series_dir.name}")
    _say(progress, f"{len(idx.chapters):,} chapters, "
                   f"{idx.total_bytes / 1024 ** 3:.2f} GB under {root}")
    return idx


# ─────────────────────────────────────────────────────────────────────────────
# Integrity
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class VerifyReport:
    root: Path
    chapters: int = 0
    pages: int = 0
    total_bytes: int = 0
    crc_tested: int = 0
    unfinished: int = 0
    problems: List[Tuple[str, str, str]] = field(default_factory=list)  # kind, series, detail
    unfinished_detail: Dict[str, int] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.problems

    def summary(self) -> str:
        return (f"{self.chapters:,} chapters, {self.pages:,} pages, "
                f"{self.total_bytes / 1024 ** 3:.2f} GB, "
                f"{len(self.problems)} problem(s), "
                f"{self.unfinished} unfinished")


def verify(
    root: str | Path,
    *,
    deep: bool = False,
    sample_per_series: int = 3,
    progress: Optional[ProgressCB] = None,
) -> VerifyReport:
    """
    Check that every chapter is a readable archive holding real pages.

    A chapter can be present, plausibly sized, and still unreadable: a
    truncated archive, or one whose entries are all metadata with no images.
    Opening the zip's central directory catches the first and counting image
    entries catches the second, both without decompressing anything.

    *deep* additionally CRC-checks every entry against its stored checksum,
    which is the only way to catch bit-level corruption. That reads and
    decompresses the whole library, so it is opt-in; otherwise the first
    *sample_per_series* archives of each series are CRC-checked, which catches
    a wholesale problem without the full cost.
    """
    root = Path(root)
    idx = scan(root, progress=progress)
    rep = VerifyReport(root=root, chapters=len(idx.chapters),
                       total_bytes=idx.total_bytes,
                       unfinished=sum(idx.unfinished.values()),
                       unfinished_detail=dict(idx.unfinished))

    for series in idx.empty_series:
        rep.problems.append(("empty series", series, "no chapters at all"))

    seen_per_series: Dict[str, int] = defaultdict(int)
    for i, ch in enumerate(idx.chapters):
        if ch.size == 0:
            rep.problems.append(("zero-byte archive", ch.series, ch.path.name))
            continue
        try:
            with zipfile.ZipFile(ch.path) as z:
                infos = z.infolist()
                pages = [e for e in infos
                         if not e.is_dir() and e.filename.lower().endswith(PAGE_EXTS)]
                rep.pages += len(pages)
                if not pages:
                    rep.problems.append(
                        ("no page images", ch.series,
                         f"{ch.path.name} ({len(infos)} entries, none renderable)"))
                if any(e.file_size == 0 for e in pages):
                    rep.problems.append(("zero-byte page", ch.series, ch.path.name))
                seen_per_series[ch.series] += 1
                if deep or seen_per_series[ch.series] <= sample_per_series:
                    bad = z.testzip()
                    rep.crc_tested += 1
                    if bad is not None:
                        rep.problems.append(("CRC failure", ch.series,
                                             f"{ch.path.name} -> {bad}"))
        except zipfile.BadZipFile as exc:
            rep.problems.append(("corrupt archive", ch.series, f"{ch.path.name}: {exc}"))
        except OSError as exc:
            rep.problems.append(("unreadable", ch.series, f"{ch.path.name}: {exc}"))
        if progress and i and i % 2000 == 0:
            _say(progress, f"   verified {i:,}/{len(idx.chapters):,}")

    return rep


# ─────────────────────────────────────────────────────────────────────────────
# Exposure: what only exists where the app can clear it
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class Exposure:
    series: str
    managed_chapters: int
    safe_chapters: int
    exposed: List[str]
    exposed_bytes: int
    has_safe_copy: bool


def exposure(
    managed_root: str | Path,
    safe_root: str | Path,
    *,
    progress: Optional[ProgressCB] = None,
) -> List[Exposure]:
    """
    Chapters present in the app-managed library with no counterpart in the
    library the app treats as user content.

    This is the number that actually predicts loss. A chapter with a safe copy
    survives the source delisting it; one without does not, and no amount of
    "the download succeeded" changes that.
    """
    managed = scan(managed_root, progress=progress)
    safe = scan(safe_root, progress=progress)

    safe_by_title: Dict[str, Dict[str, Chapter]] = defaultdict(dict)
    for c in safe.chapters:
        safe_by_title[normalise_title(c.series)][c.stem] = c

    out: List[Exposure] = []
    for series, chs in sorted(managed.by_series().items()):
        key = normalise_title(series)
        mirror = safe_by_title.get(key, {})
        missing = [c for c in chs if c.stem not in mirror]
        out.append(Exposure(
            series=series,
            managed_chapters=len(chs),
            safe_chapters=len(mirror),
            exposed=sorted(c.stem for c in missing),
            exposed_bytes=sum(c.size for c in missing),
            has_safe_copy=bool(mirror),
        ))
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Consolidation / replication
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class CopyPlan:
    dest: Path
    actions: List[Tuple[Path, Path, int, Optional[str]]] = field(default_factory=list)
    already_present: int = 0
    already_bytes: int = 0
    conflicts: List[Tuple[str, str, str]] = field(default_factory=list)
    merged_series: Dict[str, List[str]] = field(default_factory=dict)

    @property
    def copy_bytes(self) -> int:
        return sum(a[2] for a in self.actions)


def plan_consolidation(
    sources: Sequence[str | Path],
    dest: str | Path,
    *,
    hash_candidates: bool = True,
    progress: Optional[ProgressCB] = None,
) -> CopyPlan:
    """
    Work out a single canonical library drawn from several source libraries.

    One folder per series, every distinct chapter once. Where the same chapter
    name appears in more than one source with different content — different
    scanlation groups number chapters alike — both are kept, the second
    suffixed with its origin, because silently picking one would throw away a
    chapter the reader has.

    Candidates are hashed only when they collide on size with something else.
    Hashing an entire library to learn that most files are unique by size alone
    is a lot of disk reading for no new information.
    """
    dest = Path(dest)
    indexes = [scan(s, progress=progress) for s in sources]
    everything = [c for idx in indexes for c in idx.chapters]

    if hash_candidates:
        by_size: Dict[int, List[Chapter]] = defaultdict(list)
        for c in everything:
            by_size[c.size].append(c)
        candidates = [c for grp in by_size.values() if len(grp) > 1 for c in grp]

        cache = _load_hash_cache(dest)
        reused = computed = 0
        pending = sum(c.size for c in candidates)
        _say(progress, f"{len(candidates):,} size-colliding chapter(s) need a hash "
                       f"({pending / 1024 ** 3:.2f} GB), consulting cache")
        for i, c in enumerate(candidates):
            hit = _cached_hash(c.path, c.size, cache)
            if hit:
                c.digest = hit
                reused += 1
            else:
                try:
                    c.digest = file_hash(c.path)
                    computed += 1
                    cache[str(c.path)] = {
                        "size": c.size,
                        "mtime": c.path.stat().st_mtime,
                        "hash": c.digest,
                    }
                except OSError:
                    pass
            if progress and computed and computed % 2000 == 0 and not hit:
                _say(progress, f"   hashed {computed:,} (reused {reused:,})")
        _say(progress, f"hashes: {reused:,} reused from cache, {computed:,} computed")
        if computed:
            _save_hash_cache(dest, cache)

    plan = CopyPlan(dest=dest)
    # group by folded title so the same series from several sources merges
    groups: Dict[str, List[Chapter]] = defaultdict(list)
    for c in everything:
        groups[normalise_title(c.series)].append(c)

    for key, chs in sorted(groups.items()):
        titles = sorted({c.series for c in chs}, key=len)
        canonical = titles[0]
        if len(titles) > 1:
            plan.merged_series[canonical] = titles
        chosen: Dict[str, Chapter] = {}
        for c in chs:
            prev = chosen.get(c.stem)
            if prev is None:
                chosen[c.stem] = c
                continue
            if c.digest and prev.digest and c.digest == prev.digest:
                # Provably the same bytes — one copy is all that is needed.
                continue
            # Not provably identical, so keep both. Choosing between them on
            # size looks reasonable and is wrong: two sources carrying
            # genuinely different scanlations of the same chapter number
            # differ in size for that reason, and discarding the smaller one
            # loses a chapter the reader had.
            #
            # Note that "no hash" already implies "different": candidates are
            # hashed precisely when they share a size with something else, so
            # two files of equal size always both carry a digest. Reaching
            # here without one means the sizes differ, which settles it.
            plan.conflicts.append((canonical, c.stem, f"{prev.origin} vs {c.origin}"))
            suffix = c.origin.replace("/", "-").replace("\\", "-")
            chosen[f"{c.stem} [{suffix}]"] = c

        for stem, c in sorted(chosen.items()):
            target = dest / canonical / f"{stem}{c.path.suffix.lower()}"
            if target.exists():
                try:
                    if target.stat().st_size == c.size:
                        plan.already_present += 1
                        plan.already_bytes += c.size
                        continue
                except OSError:
                    pass
            plan.actions.append((c.path, target, c.size, c.digest))

    return plan


def execute(
    plan: CopyPlan,
    *,
    dry_run: bool = True,
    verify_hash: bool = True,
    progress: Optional[ProgressCB] = None,
) -> Dict[str, object]:
    """
    Carry out a plan, verifying each copy before it counts as done.

    Copy-then-verify rather than trusting the copy: a file that lands short or
    corrupted is removed again, so a half-written chapter never gets recorded
    as archived. Nothing is deleted from any source.
    """
    result: Dict[str, object] = {
        "dry_run": dry_run, "copied": 0, "copied_bytes": 0,
        "skipped_present": plan.already_present,
        "failed": 0, "errors": [],
    }
    manifest: Dict[str, Dict[str, object]] = {}

    for i, (src, target, size, digest) in enumerate(plan.actions):
        if dry_run:
            result["copied"] += 1
            result["copied_bytes"] = int(result["copied_bytes"]) + size
            continue
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, target)
            landed = target.stat().st_size
            if landed != size:
                raise OSError(f"size mismatch: expected {size}, got {landed}")
            if verify_hash:
                want = digest or file_hash(src)
                got = file_hash(target)
                if want != got:
                    raise OSError("content hash mismatch after copy")
                digest = got
            result["copied"] += 1
            result["copied_bytes"] = int(result["copied_bytes"]) + size
            rel = target.relative_to(plan.dest).as_posix()
            manifest[rel] = {"bytes": size, "hash": digest,
                             "source": str(src), "copied_at": datetime.now().isoformat()}
        except (OSError, shutil.Error) as exc:
            result["failed"] = int(result["failed"]) + 1
            errs = result["errors"]
            assert isinstance(errs, list)
            if len(errs) < 20:
                errs.append(f"{target.name}: {exc}")
            try:
                if target.exists():
                    target.unlink()
            except OSError:
                pass
        if progress and i and i % 200 == 0:
            _say(progress, f"   {i:,}/{len(plan.actions):,} copied "
                           f"({int(result['copied_bytes']) / 1024 ** 3:.1f} GB)")

    if not dry_run and manifest:
        _merge_manifest(plan.dest, manifest)
    return result


def _merge_manifest(dest: Path, new_entries: Dict[str, Dict[str, object]]) -> None:
    path = dest / MANIFEST_NAME
    data: Dict[str, object] = {"updated_at": datetime.now().isoformat(), "chapters": {}}
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict) and isinstance(loaded.get("chapters"), dict):
                data["chapters"] = loaded["chapters"]
        except (OSError, json.JSONDecodeError):
            pass
    chapters = data["chapters"]
    assert isinstance(chapters, dict)
    chapters.update(new_entries)
    data["updated_at"] = datetime.now().isoformat()
    data["chapter_count"] = len(chapters)
    try:
        path.write_text(json.dumps(data, indent=1), encoding="utf-8")
    except OSError:
        pass


def replicate(
    src: str | Path,
    dest: str | Path,
    *,
    dry_run: bool = True,
    progress: Optional[ProgressCB] = None,
) -> Dict[str, object]:
    """Mirror one library to another location, verified, resumable, additive."""
    plan = plan_consolidation([src], dest, hash_candidates=False, progress=progress)
    return execute(plan, dry_run=dry_run, progress=progress)


def prove_complete(
    sources: Sequence[str | Path],
    dest: str | Path,
    *,
    progress: Optional[ProgressCB] = None,
) -> Dict[str, object]:
    """
    Prove that every source chapter has a byte-identical copy in *dest*.

    This is the guarantee worth having, and it is stronger than anything
    ``compare`` can offer. ``compare`` matches on series and chapter NAME, so
    when two sources carry the same chapter number with different content they
    fold to one key — and a destination missing one of them still looks
    complete. That is not hypothetical: it is precisely how 126 discarded
    chapters went unreported here, until this check existed.

    Matching on content hash has no such blind spot. A source file is covered
    only if its exact bytes exist somewhere in the destination, whatever it is
    called there, which also makes the answer independent of how the
    consolidation chose to name things.
    """
    dest = Path(dest)
    cache = _load_hash_cache(dest)
    computed = 0

    def digest_of(p: Path, size: int) -> Optional[str]:
        nonlocal computed
        hit = _cached_hash(p, size, cache)
        if hit:
            return hit
        try:
            h = file_hash(p)
            computed += 1
            cache[str(p)] = {"size": size, "mtime": p.stat().st_mtime, "hash": h}
            return h
        except OSError:
            return None

    dest_idx = scan(dest, progress=progress)
    _say(progress, f"hashing {len(dest_idx.chapters):,} destination chapter(s)")
    held: Dict[str, int] = defaultdict(int)
    for i, c in enumerate(dest_idx.chapters):
        h = digest_of(c.path, c.size)
        if h:
            held[h] += 1
        if progress and i and i % 2000 == 0:
            _say(progress, f"   destination {i:,}/{len(dest_idx.chapters):,}")

    missing: List[Tuple[str, str, int]] = []
    checked = 0
    for s in sources:
        idx = scan(s, progress=progress)
        _say(progress, f"checking {len(idx.chapters):,} chapter(s) from {s}")
        for i, c in enumerate(idx.chapters):
            h = digest_of(c.path, c.size)
            checked += 1
            if h is None or h not in held:
                missing.append((c.series, c.path.name, c.size))
            if progress and i and i % 2000 == 0:
                _say(progress, f"   source {i:,}/{len(idx.chapters):,}")

    if computed:
        _save_hash_cache(dest, cache)

    return {
        "dest": str(dest),
        "dest_chapters": len(dest_idx.chapters),
        "distinct_content": len(held),
        "source_chapters": checked,
        "covered": checked - len(missing),
        "missing": missing,
        "complete": not missing,
        "hashes_computed": computed,
    }


def compare(a: str | Path, b: str | Path, *,
            progress: Optional[ProgressCB] = None) -> Dict[str, object]:
    """
    Check two libraries hold the same chapters.

    Compares by series+chapter name and size. Answers the question a copy
    operation cannot: are these two archives actually equivalent right now, or
    has one of them quietly lost something since.
    """
    ia, ib = scan(a, progress=progress), scan(b, progress=progress)

    def keyed(idx: LibraryIndex) -> Dict[Tuple[str, str], Chapter]:
        return {(normalise_title(c.series), c.stem): c for c in idx.chapters}

    ka, kb = keyed(ia), keyed(ib)
    only_a = sorted(ka.keys() - kb.keys())
    only_b = sorted(kb.keys() - ka.keys())
    differing = sorted(k for k in (ka.keys() & kb.keys()) if ka[k].size != kb[k].size)
    return {
        "a": str(a), "b": str(b),
        "a_chapters": len(ka), "b_chapters": len(kb),
        "only_in_a": only_a, "only_in_b": only_b, "size_differs": differing,
        "in_agreement": not (only_a or only_b or differing),
    }


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def _cli(argv: List[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    cmd, args = argv[0], argv[1:]
    apply = "--apply" in args
    deep = "--deep" in args
    paths = [a for a in args if not a.startswith("--")]
    out = (lambda m: print(m, flush=True))

    if cmd == "verify" and paths:
        rep = verify(paths[0], deep=deep, progress=out)
        print()
        print(rep.summary())
        print(f"CRC-tested: {rep.crc_tested:,} archive(s) "
              f"({'all' if deep else 'sampled'})")
        if rep.unfinished_detail:
            print(f"\nunfinished downloads ({rep.unfinished} total):")
            for s, n in sorted(rep.unfinished_detail.items(), key=lambda kv: -kv[1]):
                print(f"   {n:>4}  {s}")
        if rep.problems:
            print(f"\nproblems ({len(rep.problems)}):")
            for kind, series, detail in rep.problems[:60]:
                print(f"   [{kind}] {series}: {detail}")
        else:
            print("\nno integrity problems found.")
        return 0 if rep.ok else 1

    if cmd == "exposure" and len(paths) >= 2:
        rows = exposure(paths[0], paths[1], progress=out)
        exposed = [r for r in rows if r.exposed]
        print()
        print(f"{'MANAGED':>8} {'SAFE':>6} {'EXPOSED':>8} {'MB':>8}  SERIES")
        for r in sorted(rows, key=lambda r: (-len(r.exposed), -r.managed_chapters)):
            flag = "!" if r.exposed else " "
            safe = "none" if not r.has_safe_copy else str(r.safe_chapters)
            print(f"{flag}{r.managed_chapters:>7} {safe:>6} {len(r.exposed):>8} "
                  f"{r.exposed_bytes / 1024 ** 2:>8.0f}  {r.series[:54]}")
        print(f"\n{sum(len(r.exposed) for r in rows):,} chapter(s) exposed across "
              f"{len(exposed)} series, "
              f"{sum(r.exposed_bytes for r in rows) / 1024 ** 3:.2f} GB")
        print(f"{sum(1 for r in rows if not r.has_safe_copy)} series have no safe copy at all")
        return 0

    if cmd == "consolidate" and len(paths) >= 2:
        dest, sources = paths[0], paths[1:]
        plan = plan_consolidation(sources, dest, progress=out)
        print()
        print(f"destination        : {dest}")
        print(f"chapters to copy   : {len(plan.actions):,} "
              f"({plan.copy_bytes / 1024 ** 3:.2f} GB)")
        print(f"already in place   : {plan.already_present:,} "
              f"({plan.already_bytes / 1024 ** 3:.2f} GB)")
        print(f"series merged      : {len(plan.merged_series)}")
        for canonical, titles in list(plan.merged_series.items())[:15]:
            print(f"   \"{canonical[:58]}\"  <-  {len(titles)} folder(s)")
        print(f"name conflicts     : {len(plan.conflicts)} (both copies kept)")
        for series, stem, detail in plan.conflicts[:10]:
            print(f"   {series[:40]} :: {stem[:36]}  ({detail})")
        res = execute(plan, dry_run=not apply, progress=out)
        print()
        print(f"{'WOULD COPY' if not apply else 'COPIED'}: {res['copied']:,} chapter(s), "
              f"{int(res['copied_bytes']) / 1024 ** 3:.2f} GB")
        if res["failed"]:
            print(f"FAILED: {res['failed']}")
            for e in res["errors"]:  # type: ignore[union-attr]
                print(f"   {e}")
        if not apply:
            print("dry run — pass --apply to perform the copy")
        return 0

    if cmd == "replicate" and len(paths) >= 2:
        res = replicate(paths[0], paths[1], dry_run=not apply, progress=out)
        print()
        print(f"{'WOULD COPY' if not apply else 'COPIED'}: {res['copied']:,}, "
              f"{int(res['copied_bytes']) / 1024 ** 3:.2f} GB; "
              f"already present {res['skipped_present']:,}")
        if res["failed"]:
            print(f"FAILED: {res['failed']}")
            for e in res["errors"]:  # type: ignore[union-attr]
                print(f"   {e}")
        if not apply:
            print("dry run — pass --apply to perform the copy")
        return 0

    if cmd == "prove" and len(paths) >= 2:
        r = prove_complete(paths[1:], paths[0], progress=out)
        print()
        print(f"destination        : {r['dest']}")
        print(f"chapters there     : {r['dest_chapters']:,} "
              f"({r['distinct_content']:,} distinct by content)")
        print(f"source chapters    : {r['source_chapters']:,}")
        print(f"covered            : {r['covered']:,}")
        missing = r["missing"]
        assert isinstance(missing, list)
        print(f"NOT covered        : {len(missing):,}")
        for series, name, size in missing[:40]:
            print(f"   {size / 1024 ** 2:>8.1f} MB  {series[:44]}  ::  {name[:44]}")
        if len(missing) > 40:
            print(f"   … and {len(missing) - 40:,} more")
        print()
        print("COMPLETE — every source chapter has an identical copy in the destination."
              if r["complete"] else
              "INCOMPLETE — the chapters listed above have no copy in the destination.")
        return 0 if r["complete"] else 1

    if cmd == "compare" and len(paths) >= 2:
        r = compare(paths[0], paths[1], progress=out)
        print()
        print(f"A: {r['a_chapters']:,} chapters    B: {r['b_chapters']:,} chapters")
        print(f"in agreement: {r['in_agreement']}")
        for label in ("only_in_a", "only_in_b", "size_differs"):
            items = r[label]
            assert isinstance(items, list)
            print(f"{label}: {len(items)}")
            for k in items[:15]:
                print(f"   {k}")
        return 0 if r["in_agreement"] else 1

    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(_cli(sys.argv[1:]))
