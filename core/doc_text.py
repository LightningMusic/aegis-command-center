"""
core/doc_text.py
================
Pull readable text out of document formats using only the standard library.

PreviewPanel can already render images, video, PDF and plain text, but a .docx
is a zip of XML and a .rtf is markup, so both previewed as "no inline preview".
When you are deciding whether a document is what its name suggests, being told
to open it in Word is not an answer.

No third-party dependency is used on purpose: python-docx is not installed and
this has to work on a plain PyQt6 environment.

Every extractor returns (text, note). The note says what was approximated, so
the caller can show it rather than implying the text is exact.
"""

from __future__ import annotations

import html
import re
import zipfile
from pathlib import Path
from typing import Tuple

# Zip-based office formats: which member holds the body text.
_ZIP_BODY = {
    ".docx": ("word/document.xml", "w:p"),
    ".odt": ("content.xml", "text:p"),
    ".ods": ("content.xml", "text:p"),
    ".odp": ("content.xml", "text:p"),
    ".pptx": (None, "a:p"),          # many slideN.xml members
}

DOC_TEXT_EXTS = {".docx", ".odt", ".ods", ".odp", ".pptx", ".rtf", ".doc"}

_LIMIT = 400_000


def _tidy(text: str) -> str:
    text = html.unescape(text)
    text = text.replace("\xa0", " ").replace("\u200b", "")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)      # no stray indent after a break
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _from_zip_xml(path: Path, ext: str, limit: int) -> Tuple[str, str]:
    member, para_tag = _ZIP_BODY[ext]
    try:
        with zipfile.ZipFile(path) as z:
            if member is None:                      # pptx: every slide
                names = sorted(n for n in z.namelist()
                               if re.match(r"ppt/slides/slide\d+\.xml$", n))
            else:
                names = [member] if member in z.namelist() else []
                if not names:
                    return "", "no document body found inside the archive"
            chunks = []
            for n in names[:60]:
                chunks.append(z.read(n).decode("utf-8", "replace"))
                if sum(len(c) for c in chunks) > limit * 4:
                    break
            xml = "\n".join(chunks)
    except zipfile.BadZipFile:
        return "", "not a readable archive — the file may be corrupt"
    except (OSError, KeyError) as exc:
        return "", f"could not read: {exc}"

    # Paragraph and break markers become newlines before tags are stripped,
    # otherwise the whole document collapses onto one line.
    xml = re.sub(rf"</{re.escape(para_tag)}>", "\n", xml)
    xml = re.sub(r"<(w:br|w:cr|text:line-break)\b[^>]*/?>", "\n", xml)
    xml = re.sub(r"<(w:tab|text:tab)\b[^>]*/?>", "\t", xml)
    xml = re.sub(r"<[^>]+>", "", xml)
    return _tidy(xml)[:limit], ""


# Matches immediately after the '{' of a group, e.g. "{\fonttbl…" or
# "{\*\themedata…". The earlier version required two backslashes and so never
# matched a plain "{\fonttbl", leaving font names in the extracted text.
_RTF_SKIP_GROUPS = re.compile(
    r"\\(?:\*\\)?(fonttbl|colortbl|stylesheet|info|pict|object|themedata|"
    r"colorschememapping|latentstyles|datastore|generator|filetbl|listtable|"
    r"listoverridetable|rsidtbl|mmathPr|xmlnstbl)\b")


def _from_rtf(path: Path, limit: int) -> Tuple[str, str]:
    try:
        raw = path.read_bytes()[:limit * 6].decode("latin-1", "replace")
    except OSError as exc:
        return "", f"could not read: {exc}"

    # Drop whole groups that hold no body text (font tables, embedded images).
    out, depth, skip_at = [], 0, None
    i = 0
    while i < len(raw):
        c = raw[i]
        if c == "{":
            depth += 1
            if skip_at is None and _RTF_SKIP_GROUPS.match(raw, i + 1):
                skip_at = depth
        elif c == "}":
            if skip_at is not None and depth == skip_at:
                skip_at = None
            depth -= 1
        elif skip_at is None:
            out.append(c)
        i += 1
    text = "".join(out)

    # A single space after a control word is RTF's delimiter and is not
    # content, so consume it -- otherwise every new line starts indented.
    text = re.sub(r"\\pard?\b ?", "\n", text)
    text = re.sub(r"\\line\b ?", "\n", text)
    text = re.sub(r"\\tab\b ?", "\t", text)
    text = re.sub(r"\\'([0-9a-fA-F]{2})",
                  lambda m: bytes([int(m.group(1), 16)]).decode("latin-1", "replace"),
                  text)
    text = re.sub(r"\\u(-?\d+)\s?\??",
                  lambda m: chr(int(m.group(1)) % 65536), text)
    text = re.sub(r"\\[a-zA-Z]+-?\d*\s?", "", text)   # remaining control words
    text = text.replace("\\*", "").replace("{", "").replace("}", "")
    return _tidy(text)[:limit], ""


def _from_legacy_doc(path: Path, limit: int) -> Tuple[str, str]:
    """Old binary .doc: scrape printable runs. Approximate by nature."""
    try:
        raw = path.read_bytes()[:limit * 4]
    except OSError as exc:
        return "", f"could not read: {exc}"

    # Word 97 stores body text as runs of cp1252 or UTF-16LE.
    runs = re.findall(rb"(?:[\x20-\x7e\r\n\t]{6,})", raw)
    text = "\n".join(r.decode("cp1252", "replace") for r in runs)
    wide = re.findall(rb"(?:(?:[\x20-\x7e]\x00){6,})", raw)
    if len(b"".join(wide)) > len(b"".join(runs)):
        text = "\n".join(w.decode("utf-16-le", "replace") for w in wide)
    return (_tidy(text)[:limit],
            "legacy .doc — text scraped from the binary, so formatting and "
            "ordering may be wrong")


def extract_text(path: str | Path, limit: int = _LIMIT) -> Tuple[str, str]:
    """Return (text, note) for a document. Never raises."""
    p = Path(path)
    ext = p.suffix.lower()
    try:
        if ext in _ZIP_BODY:
            text, note = _from_zip_xml(p, ext, limit)
        elif ext == ".rtf":
            text, note = _from_rtf(p, limit)
        elif ext == ".doc":
            text, note = _from_legacy_doc(p, limit)
        else:
            text, note = p.read_bytes()[:limit].decode("utf-8", "replace"), ""
    except Exception as exc:  # noqa: BLE001 - a preview must not kill the UI
        return "", f"{type(exc).__name__}: {exc}"

    if not text.strip():
        return "", note or "no extractable text (it may be images only)"
    try:
        if p.stat().st_size > limit:
            text += "\n\n… (truncated preview) …"
    except OSError:
        pass
    return text, note
