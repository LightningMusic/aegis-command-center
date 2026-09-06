"""
core/copy_engine.py
===================
One verified single-file copy primitive, shared by drive consolidation, the
Backup Manager, and USB-phone drive backups. Everything that copies a file in
Aegis now goes through here, so every copy is hash-checked.

verified_copy(src, dst):
  1. hash the source (SHA-256) unless the caller already has it
  2. stream-copy to "<dst>.aegis-part", flushing to the device (fsync)
  3. verify #1  — hash the freshly written temp file; SHA-256 + byte size must
     equal the source
  4. os.replace() the temp onto its final name (atomic on one volume)
  5. verify #2  — independently re-read the final file from disk; must match
     again  (skippable with readback=False for callers that don't then delete
     the source — a backup keeps the original, so verify #1 + fsync is enough)

Raises VerifiedCopyError if the result can't be proven identical to the source.
The temp file is always cleaned up on failure; on a verify-#2 failure the
destination is left in place for inspection. This function never touches the
source.
"""

from __future__ import annotations

import os
import shutil
from typing import Optional, Tuple

from core.integrity import verify_readable

_CHUNK = 4 * 1024 * 1024


class VerifiedCopyError(RuntimeError):
    pass


def safe_unlink(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


def file_size(path: str) -> int:
    try:
        return os.path.getsize(path)
    except OSError:
        return -1


def copy_with_fsync(src: str, dst: str, chunk: int = _CHUNK) -> None:
    """Byte copy that flushes to the storage device before returning, so a
    read-back isn't just re-reading our own write cache."""
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


def verified_copy(
    src: str,
    dst: str,
    *,
    source_sha: Optional[str] = None,
    fsync: bool = True,
    readback: bool = True,
) -> Tuple[str, str]:
    """Copy src -> dst and prove it. Returns (sha, short_detail).

    Pass source_sha if you have already hashed the source this run to avoid a
    second read. Pass readback=False to skip the independent re-read (verify #2)
    when the caller will not delete the source afterwards.
    """
    if source_sha is None:
        s_status, s_detail, source_sha = verify_readable(src)
        if s_status != "ok" or not source_sha:
            raise VerifiedCopyError(f"source unreadable ({s_detail or 'no hash'})")

    src_size = file_size(src)
    if src_size < 0:
        raise VerifiedCopyError("cannot stat source")

    os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
    tmp = dst + ".aegis-part"
    safe_unlink(tmp)

    try:
        if fsync:
            copy_with_fsync(src, tmp)
        else:
            shutil.copyfile(src, tmp)
        try:
            shutil.copystat(src, tmp)
        except OSError:
            pass
    except OSError as exc:
        safe_unlink(tmp)
        raise VerifiedCopyError(f"copy failed: {exc}") from exc

    c_status, c_detail, copy_sha = verify_readable(tmp)
    if c_status != "ok" or copy_sha != source_sha or file_size(tmp) != src_size:
        safe_unlink(tmp)
        raise VerifiedCopyError(f"verify #1 failed ({c_detail or 'hash/size mismatch'})")

    try:
        os.replace(tmp, dst)
    except OSError as exc:
        safe_unlink(tmp)
        raise VerifiedCopyError(f"could not finalize copy: {exc}") from exc

    if readback:
        f_status, f_detail, final_sha = verify_readable(dst)
        if f_status != "ok" or final_sha != source_sha or file_size(dst) != src_size:
            raise VerifiedCopyError(
                f"verify #2 (read-back) failed ({f_detail or 'hash/size mismatch'}); "
                f"target copy left at {dst}"
            )

    return source_sha, "copied + verified"
