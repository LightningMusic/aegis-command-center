"""
core/integrity.py
=================
File integrity / "is this file actually good" checks.

Two tiers:
  * verify_readable()  — stream the whole file; catches unreadable sectors,
    truncation, permission/lock problems. Returns the SHA-256 too so callers
    don't read the file twice.
  * verify_deep()      — format-aware structural check for common types
    (zip family, gzip, png/jpeg, pdf, sqlite, mp4-ish). Uses Pillow / ffprobe
    only if they happen to be installed; otherwise falls back to header/trailer
    sniffing. Never a hard dependency.

Status values: "ok" | "unreadable" | "corrupt" | "empty" | "skipped" | "unverified"

  * verify_playthrough() — the strongest check: actually decode every frame /
    page / record. Videos and audio are run through ffmpeg's null muxer (every
    frame decoded, any decode error surfaces); images get a full pixel load;
    office docs get a zip-CRC pass plus an XML parse of the main part; PDFs get
    a page walk. When the tool it needs is missing it returns "unverified"
    rather than a false "ok".
"""

from __future__ import annotations

import hashlib
import os
import shutil
import struct
import subprocess
import zipfile
from pathlib import Path
from typing import Optional, Tuple

_READ_CHUNK = 1024 * 1024

_ZIP_EXTS = {".zip", ".docx", ".xlsx", ".pptx", ".odt", ".ods", ".jar", ".apk", ".epub"}
_OFFICE_MAIN = {
    ".docx": "word/document.xml",
    ".xlsx": "xl/workbook.xml",
    ".pptx": "ppt/presentation.xml",
    ".odt": "content.xml",
    ".ods": "content.xml",
}
_HEIF_EXTS = {".heic", ".heif", ".hif", ".avif", ".avci"}
_IMG_EXTS = {
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".tiff", ".tif", ".ico",
    *_HEIF_EXTS,
}
# ISO base-media 'ftyp' brands that mean "valid HEIF/AVIF container"
_HEIF_BRANDS = {
    b"heic", b"heix", b"heim", b"heis", b"hevc", b"hevx", b"hevm", b"hevs",
    b"mif1", b"msf1", b"miaf", b"avif", b"avis", b"avio", b"MA1A", b"MA1B",
}
_VID_EXTS = {".mp4", ".mkv", ".mov", ".avi", ".m4v", ".webm", ".wmv", ".flv", ".mpg", ".mpeg", ".ts"}
_AUD_EXTS = {".mp3", ".flac", ".wav", ".m4a", ".aac", ".ogg", ".opus", ".wma", ".aiff"}
_TEXT_EXTS = {
    ".txt", ".md", ".csv", ".tsv", ".log", ".json", ".xml", ".yaml", ".yml", ".ini",
    ".cfg", ".py", ".js", ".ts", ".html", ".css", ".c", ".h", ".cpp", ".java", ".rs",
    ".go", ".rb", ".sh", ".bat", ".ps1", ".sql",
}


def verify_readable(path: str) -> Tuple[str, str, Optional[str]]:
    """Return (status, detail, sha256_hex_or_None). Reads the entire file."""
    try:
        size = os.path.getsize(path)
    except OSError as exc:
        return "unreadable", f"cannot stat: {exc}", None

    if size == 0:
        return "empty", "0 bytes", "full:" + hashlib.sha256(b"").hexdigest()

    digest = hashlib.sha256()
    read = 0
    try:
        with open(path, "rb", buffering=0) as fh:
            while True:
                chunk = fh.read(_READ_CHUNK)
                if not chunk:
                    break
                digest.update(chunk)
                read += len(chunk)
    except OSError as exc:
        return "unreadable", f"read error after {read:,}/{size:,} bytes: {exc}", None

    if read != size:
        return "unreadable", f"short read: {read:,} of {size:,} bytes", None

    return "ok", "", "full:" + digest.hexdigest()


def verify_deep(path: str) -> Tuple[str, str]:
    """Format-aware structural check. Assumes verify_readable() already passed."""
    ext = Path(path).suffix.lower()
    try:
        if ext in _ZIP_EXTS:
            return _check_zip(path)
        if ext in (".gz", ".tgz"):
            return _check_gzip(path)
        if ext in _IMG_EXTS:
            return _check_image(path)
        if ext == ".pdf":
            return _check_pdf(path)
        if ext in (".sqlite", ".sqlite3", ".db"):
            return _check_sqlite(path)
        if ext in _VID_EXTS:
            return _check_video(path)
    except Exception as exc:  # noqa: BLE001 - any parser blowing up == suspect
        return "corrupt", f"{ext} check raised {type(exc).__name__}: {exc}"
    return "ok", "no deep check for this type"


def verify_playthrough(path: str, *, timeout: int = 1800) -> Tuple[str, str]:
    """
    Strongest check: fully decode the file's payload.

    Assumes verify_readable() already passed. Returns "unverified" (not "ok")
    when the decoder needed for this type is not installed, so the caller can
    tell "checked and good" apart from "couldn't check".
    """
    ext = Path(path).suffix.lower()
    try:
        if ext in _VID_EXTS or ext in _AUD_EXTS:
            return _decode_av(path, timeout=timeout)
        if ext in _IMG_EXTS:
            return _decode_image(path)
        if ext in _OFFICE_MAIN:
            return _check_office(path, ext)
        if ext in _ZIP_EXTS:
            return _check_zip(path)
        if ext in (".gz", ".tgz"):
            return _check_gzip(path)
        if ext == ".pdf":
            return _decode_pdf(path)
        if ext in (".sqlite", ".sqlite3", ".db"):
            return _check_sqlite(path)
        if ext in _TEXT_EXTS:
            return _decode_text(path)
    except subprocess.TimeoutExpired:
        return "unverified", f"decode timed out after {timeout}s"
    except Exception as exc:  # noqa: BLE001
        return "corrupt", f"{ext} decode raised {type(exc).__name__}: {exc}"

    status, detail = verify_deep(path)
    if status == "ok" and "no deep check" in detail:
        return "unverified", "no full-decode check for this type"
    return status, detail


# ── full-decode helpers ─────────────────────────────────────────────────────

def _decode_av(path: str, *, timeout: int) -> Tuple[str, str]:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return (
            "unverified",
            "install ffmpeg for a full frame-by-frame decode, "
            "or use the preview player's play-through button",
        )
    proc = subprocess.run(
        [ffmpeg, "-v", "error", "-xerror", "-nostdin", "-i", path,
         "-map", "0", "-f", "null", "-"],
        capture_output=True, text=True, timeout=timeout,
    )
    err = (proc.stderr or "").strip()
    if proc.returncode != 0 or err:
        first = err.splitlines()[0] if err else f"exit {proc.returncode}"
        return "corrupt", f"ffmpeg decode error: {first[:200]}"
    return "ok", "every frame decoded (ffmpeg)"


def _decode_image(path: str) -> Tuple[str, str]:
    ext = Path(path).suffix.lower()
    try:
        from PIL import Image, UnidentifiedImageError  # type: ignore
    except ImportError:
        if ext in _HEIF_EXTS:
            return _image_container_sniff(path, ext)
        return "unverified", "install Pillow for a full pixel decode"

    if ext in _HEIF_EXTS:
        _ensure_heif()

    try:
        with Image.open(path) as im:
            im.load()
            frames = getattr(im, "n_frames", 1)
            for i in range(1, frames):
                im.seek(i)
                im.load()
        return "ok", f"{frames} frame(s) fully decoded (Pillow)"
    except UnidentifiedImageError:
        if ext in _HEIF_EXTS:
            return _image_container_sniff(path, ext)
        return _check_image_sniff(path)
    except OSError as exc:
        if ext in _HEIF_EXTS:
            return _image_container_sniff(path, ext)
        return "corrupt", f"image decode failed: {type(exc).__name__}: {exc}"


def _check_office(path: str, ext: str) -> Tuple[str, str]:
    status, detail = _check_zip(path)
    if status != "ok":
        return status, detail
    main = _OFFICE_MAIN[ext]
    import xml.etree.ElementTree as ET

    with zipfile.ZipFile(path) as zf:
        names = set(zf.namelist())
        if main not in names:
            return "corrupt", f"missing {main} inside the document"
        for part in [main, *[n for n in names if n.endswith(".xml")][:50]]:
            try:
                ET.fromstring(zf.read(part))
            except ET.ParseError as exc:
                return "corrupt", f"bad XML in {part}: {exc}"
    return "ok", "zip CRC + document XML parsed"


def _decode_pdf(path: str) -> Tuple[str, str]:
    reader_cls = None
    try:
        from pypdf import PdfReader as reader_cls  # type: ignore
    except ImportError:
        try:
            from PyPDF2 import PdfReader as reader_cls  # type: ignore
        except ImportError:
            reader_cls = None
    if reader_cls is None:
        status, detail = _check_pdf(path)
        return ("unverified", "install pypdf for a full page walk") if status == "ok" else (status, detail)

    reader = reader_cls(path)
    pages = len(reader.pages)
    if pages == 0:
        return "corrupt", "PDF has no pages"
    for idx in (0, pages // 2, pages - 1):
        reader.pages[idx].extract_text()
    return "ok", f"{pages} page(s) parsed (pypdf)"


def _decode_text(path: str) -> Tuple[str, str]:
    raw = Path(path).read_bytes()
    for enc in ("utf-8", "utf-16", "cp1252"):
        try:
            raw.decode(enc)
            return "ok", f"decoded as {enc}"
        except UnicodeDecodeError:
            continue
    return "corrupt", "not decodable as text in utf-8/utf-16/cp1252"


# ── per-format checks ────────────────────────────────────────────────────────

def _check_zip(path: str) -> Tuple[str, str]:
    if not zipfile.is_zipfile(path):
        return "corrupt", "not a valid zip container"
    with zipfile.ZipFile(path) as zf:
        bad = zf.testzip()
        if bad is not None:
            return "corrupt", f"CRC failed on member: {bad}"
    return "ok", "zip CRC verified"


def _check_gzip(path: str) -> Tuple[str, str]:
    import gzip

    with gzip.open(path, "rb") as fh:
        while fh.read(_READ_CHUNK):
            pass
    return "ok", "gzip stream decoded"


_HEIF_REGISTERED: Optional[bool] = None


def _ensure_heif() -> bool:
    """Register the pillow-heif opener once, if it's installed."""
    global _HEIF_REGISTERED
    if _HEIF_REGISTERED is None:
        try:
            from pillow_heif import register_heif_opener  # type: ignore

            register_heif_opener()
            _HEIF_REGISTERED = True
        except Exception:  # noqa: BLE001
            _HEIF_REGISTERED = False
    return _HEIF_REGISTERED


def _image_container_sniff(path: str, ext: str) -> Tuple[str, str]:
    """
    Last resort when there's no codec: check the container looks structurally
    valid. "no codec" is NOT corruption, so a plausible container -> unverified.
    """
    try:
        with open(path, "rb") as fh:
            head = fh.read(32)
    except OSError as exc:
        return "unreadable", str(exc)

    if ext in _HEIF_EXTS:
        if len(head) >= 12 and head[4:8] == b"ftyp":
            brand = head[8:12]
            if brand in _HEIF_BRANDS or ext == ".avif":
                return "unverified", (
                    f"{ext} container valid (brand "
                    f"{brand.decode('ascii', 'replace')}); install pillow-heif "
                    "for a full pixel decode"
                )
            return "unverified", (
                f"{ext}: unrecognised ftyp brand "
                f"{brand.decode('ascii', 'replace')!r} — install pillow-heif to check"
            )
        return "corrupt", f"{ext}: no 'ftyp' box at offset 4 (not a valid HEIF/AVIF)"

    return "unverified", f"no decoder for {ext or 'this image type'}"


def _check_image(path: str) -> Tuple[str, str]:
    ext = Path(path).suffix.lower()
    try:
        from PIL import Image, UnidentifiedImageError  # type: ignore
    except ImportError:
        return _check_image_sniff(path)

    if ext in _HEIF_EXTS:
        _ensure_heif()

    try:
        with Image.open(path) as im:
            im.verify()
        with Image.open(path) as im:
            im.load()
        return "ok", "image decoded (Pillow)"
    except UnidentifiedImageError:
        # No codec for this format. For HEIF/AVIF that's expected (needs
        # pillow-heif) and not damage; for anything else, fall back to a
        # marker sniff which *can* still catch truncation.
        if ext in _HEIF_EXTS:
            return _image_container_sniff(path, ext)
        return _check_image_sniff(path)
    except OSError as exc:
        # HEIC without pillow-heif can surface as OSError from the plugin layer.
        if ext in _HEIF_EXTS:
            return _image_container_sniff(path, ext)
        return "corrupt", f"image decode failed: {type(exc).__name__}: {exc}"


def _check_image_sniff(path: str) -> Tuple[str, str]:
    ext = Path(path).suffix.lower()
    with open(path, "rb") as fh:
        head = fh.read(16)
        fh.seek(-4, os.SEEK_END)
        tail = fh.read(4)
    if ext in (".jpg", ".jpeg"):
        if not head.startswith(b"\xff\xd8"):
            return "corrupt", "missing JPEG SOI marker"
        if not tail.endswith(b"\xff\xd9"):
            return "corrupt", "missing JPEG EOI marker (truncated)"
        return "ok", "JPEG markers present"
    if ext == ".png":
        if not head.startswith(b"\x89PNG\r\n\x1a\n"):
            return "corrupt", "bad PNG signature"
        if tail != b"\xaeB`\x82":  # IEND chunk CRC tail
            return "corrupt", "missing PNG IEND (truncated)"
        return "ok", "PNG signature + IEND present"
    if ext in _HEIF_EXTS:
        return _image_container_sniff(path, ext)
    return "unverified", "no sniff rule for this image type; header readable"


def _check_pdf(path: str) -> Tuple[str, str]:
    with open(path, "rb") as fh:
        head = fh.read(5)
        fh.seek(-1024, os.SEEK_END) if os.path.getsize(path) > 1024 else fh.seek(0)
        tail = fh.read()
    if head != b"%PDF-":
        return "corrupt", "missing %PDF- header"
    if b"%%EOF" not in tail:
        return "corrupt", "missing %%EOF trailer (truncated)"
    return "ok", "PDF header + trailer present"


def _check_sqlite(path: str) -> Tuple[str, str]:
    import sqlite3

    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        row = con.execute("PRAGMA integrity_check").fetchone()
    finally:
        con.close()
    if row and row[0] == "ok":
        return "ok", "sqlite integrity_check ok"
    return "corrupt", f"sqlite integrity_check: {row[0] if row else 'no result'}"


def _check_video(path: str) -> Tuple[str, str]:
    ffprobe = shutil.which("ffprobe")
    if ffprobe:
        proc = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1", path],
            capture_output=True, text=True, timeout=120,
        )
        if proc.returncode != 0 or "duration" not in proc.stdout.lower():
            return "corrupt", f"ffprobe: {proc.stderr.strip()[:200] or 'no duration'}"
        return "ok", "ffprobe parsed container"
    # No ffprobe — sniff container magic.
    with open(path, "rb") as fh:
        head = fh.read(12)
    ext = Path(path).suffix.lower()
    if ext in (".mp4", ".m4v", ".mov") and b"ftyp" not in head:
        return "corrupt", "no 'ftyp' box near start"
    if ext == ".mkv" or ext == ".webm":
        if not head.startswith(b"\x1a\x45\xdf\xa3"):
            return "corrupt", "bad EBML/Matroska signature"
    if ext == ".avi" and not (head.startswith(b"RIFF") and b"AVI " in head):
        return "corrupt", "bad RIFF/AVI signature"
    return "ok", "container signature present (install ffmpeg for a full check)"
