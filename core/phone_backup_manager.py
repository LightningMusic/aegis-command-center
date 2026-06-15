"""
core/phone_backup_manager.py
============================
Detects phones connected via USB (MTP / file-transfer mode or USB mass-storage)
and backs up every accessible file, organizing it into category folders via
PhoneFileOrganizer.

Folder layout produced
──────────────────────
{backup_root}/
  Phones/
    Galaxy_S24/
      _incoming/          ← persistent staging area for the MTP/drive copy stage.
                             Files land here, then get moved into category folders
                             below. Anything left here after a run means it still
                             needs organizing (or the run was interrupted) — the
                             next run picks up right where it left off.
      latest/
        Photos/
        Videos/
        Documents/
        Downloads/
        Audio/
        Messages/
        Private_Files/
        Miscellaneous/
      backup_log.json     ← small JSON history of each run (timestamps, counts, errors)
    Pixel_8_Pro/
      …

This layout is incremental and resumable: a run that's cancelled or stalls
partway through still organizes whatever made it into _incoming/, so progress
is never lost, and nothing is ever copied twice into latest/.

Detection strategy (tried in order)
────────────────────────────────────
1. MTP portable devices  – Windows Shell.Application namespace (PowerShell)
2. Removable drives with phone-like folder structure (DCIM / Android)

Usage
─────
    mgr = PhoneBackupManager("D:\\Aegis_Backups")
    phones = mgr.detect_phones()
    result = mgr.backup_phone(phones[0], progress_callback=…, should_stop=…)
"""

from __future__ import annotations

import ctypes
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import queue
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from modules.phone_file_organizer import PhoneFileOrganizer

ProgressCallback = Callable[[int, str], None]
StopCallback = Callable[[], bool]

# Per-file timeout for the MTP copy script.
MTP_FILE_TIMEOUT_SECONDS = 60

# If the whole MTP copy process produces no output at all for this long,
# assume the phone has stopped responding and give up gracefully.
MTP_STALL_TIMEOUT_SECONDS = 120


# ─────────────────────────────────────────────────────────────────────────────
# Data classes
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class PhoneDevice:
    name: str
    access_type: str          # "mtp" | "drive"
    shell_path: str = ""      # MTP shell namespace path (used for CopyHere)
    drive_root: str = ""      # e.g. "E:\\" for USB-mass-storage phones
    friendly_name: str = ""

    @property
    def display_name(self) -> str:
        return self.friendly_name or self.name

    @property
    def safe_folder_name(self) -> str:
        return _safe_folder_name(self.display_name)


# ─────────────────────────────────────────────────────────────────────────────
# PowerShell scripts (written to temp .ps1 files at runtime)
# ─────────────────────────────────────────────────────────────────────────────

# List portable/MTP devices visible in "This PC" shell namespace.
_PS_DETECT_MTP = r"""
$shell = New-Object -ComObject Shell.Application
$ns    = $shell.Namespace(17)        # 17 = CSIDL_DRIVES ("This PC")
$out   = @()
foreach ($item in $ns.Items()) {
    if (-not $item.IsFolder) { continue }
    $t = $item.Type.ToLower()
    # Skip ordinary drive types
    if ($t -like '*local disk*' -or $t -like '*removable disk*' -or
        $t -like '*cd drive*'   -or $t -like '*network drive*') { continue }
    $out += [PSCustomObject]@{
        Name = $item.Name
        Path = $item.Path
        Type = $item.Type
    }
}
if ($out.Count -eq 0) { Write-Output '[]' }
else { $out | ConvertTo-Json -Compress -Depth 2 }
"""

# Recursively copy all files from an MTP shell-namespace path to a real
# local folder.  Emits structured lines on stdout so Python can parse them.
#
# Output protocol:
#   FILE_OK:<local_dest_path>
#   FILE_SKIP:<filename>
#   FILE_TIMEOUT:<filename>
#   FOLDER_ENTER:<shell_path>
#   FOLDER_SKIP:<shell_path>
#   STATS:<copied>:<skipped>:<errors>          (heartbeat, every ~5s)
#   ERR:<shell_path>:<message>
#   DONE:<copied>:<skipped>:<errors>
_PS_COPY_MTP = r"""
param(
    [string]$SourcePath,
    [string]$DestPath,
    [int]$TimeoutSec = 60
)

$shell     = New-Object -ComObject Shell.Application
$copied    = 0
$skipped   = 0
$errors    = 0
$visited   = 0
$lastBeat  = Get-Date

# FOF_SILENT(4) + FOF_NOCONFIRMATION(16) + FOF_NOCONFIRMMKDIR(512) + FOF_NOERRORUI(1024)
# NOERRORUI is the important one — without it, a single permission error pops
# an invisible dialog and the whole script hangs forever.
$copyFlags = 1556

# Folders that are always junk / not worth backing up.
$ALWAYS_SKIP = @('.thumbnails', '.trashed', '.cache', '.thumbcache', 'lost.dir')

function ShouldSkipItem([string]$ParentLabel, [string]$Name) {
    $lower = $Name.ToLower()
    if ($ALWAYS_SKIP -contains $lower) { return $true }
    # Android 10+ blocks MTP access to Android/data and Android/obb for
    # everything except the owning app. Walking into these reliably hangs.
    if (($lower -eq 'data' -or $lower -eq 'obb') -and $ParentLabel -match '\\Android$') {
        return $true
    }
    return $false
}

function Heartbeat {
    $now = Get-Date
    if (($now - $script:lastBeat).TotalSeconds -ge 5) {
        Write-Host "STATS:$($script:copied):$($script:skipped):$($script:errors)"
        [Console]::Out.Flush()
        $script:lastBeat = $now
    }
}

function EnsureFolder([string]$Path) {
    if (-not (Test-Path $Path)) {
        New-Item -ItemType Directory -Path $Path -Force | Out-Null
    }
    return $shell.Namespace($Path)
}

function CopyMTPFolderObject($Folder, [string]$Label, [string]$Dst) {
    try {
        if ($null -eq $Folder) {
            $script:errors++
            Write-Host "ERR:$($Label):Unable to open MTP folder"
            [Console]::Out.Flush()
            return
        }

        $script:visited++
        Write-Host "FOLDER_ENTER:$($Label)"
        [Console]::Out.Flush()

        $dstNs = EnsureFolder $Dst
        if ($null -eq $dstNs) {
            $script:errors++
            Write-Host "ERR:$($Label):Unable to open destination folder $Dst"
            [Console]::Out.Flush()
            return
        }

        foreach ($item in $Folder.Items()) {
            Heartbeat
            $safeName = $item.Name -replace '[\\/:*?"<>|]', '_'

            if ($item.IsFolder) {
                if (ShouldSkipItem $Label $item.Name) {
                    Write-Host "FOLDER_SKIP:$($Label)\$($safeName)"
                    [Console]::Out.Flush()
                    continue
                }
                $childFolder = $null
                try { $childFolder = $item.GetFolder() } catch { $childFolder = $null }
                CopyMTPFolderObject $childFolder "$Label\$safeName" (Join-Path $Dst $safeName)
                continue
            }

            $destFile = Join-Path $Dst $safeName
            if (Test-Path $destFile) {
                $script:skipped++
                Write-Host "FILE_SKIP:$safeName"
                [Console]::Out.Flush()
                continue
            }

            try {
                $dstNs.CopyHere($item, $copyFlags)
            } catch {
                $script:errors++
                Write-Host "ERR:$($Label)\$($safeName):CopyHere failed: $_"
                [Console]::Out.Flush()
                continue
            }

            $ms    = 0
            $limit = $TimeoutSec * 10
            while (-not (Test-Path $destFile) -and $ms -lt $limit) {
                Start-Sleep -Milliseconds 100
                $ms++
                if ($ms % 10 -eq 0) { Heartbeat }
            }

            if (Test-Path $destFile) {
                # Give large files a brief moment to finish writing.
                try {
                    $size = (Get-Item $destFile).Length
                    if ($size -gt 5MB) {
                        $stableChecks = 0
                        $checks = 0
                        while ($stableChecks -lt 2 -and $checks -lt 15) {
                            Start-Sleep -Milliseconds 300
                            $newSize = (Get-Item $destFile).Length
                            if ($newSize -eq $size) { $stableChecks++ } else { $size = $newSize; $stableChecks = 0 }
                            $checks++
                        }
                    }
                } catch {}

                # Best-effort: preserve the phone's "date modified" so files
                # sort by when they were actually taken, not when they were
                # backed up. Column 3 is "Date modified" in most shell views;
                # if it's wrong/unavailable for this folder type, we just
                # leave the copy's own timestamp alone.
                try {
                    $dateStr = $Folder.GetDetailsOf($item, 3)
                    if ($dateStr) {
                        $dateStr = ($dateStr -replace '[^\x20-\x7E]', '').Trim()
                        $dt = [datetime]::Parse($dateStr)
                        [System.IO.File]::SetLastWriteTime($destFile, $dt)
                        [System.IO.File]::SetCreationTime($destFile, $dt)
                    }
                } catch {}

                $script:copied++
                Write-Host "FILE_OK:$destFile"
            } else {
                $script:errors++
                Write-Host "FILE_TIMEOUT:$safeName"
            }
            [Console]::Out.Flush()
        }
    } catch {
        $script:errors++
        Write-Host "ERR:$($Label):$_"
        [Console]::Out.Flush()
    }
}

$srcNs = $shell.Namespace($SourcePath)
if ($null -eq $srcNs) {
    $errors++
    Write-Host "ERR:$($SourcePath):Unable to open MTP source. Unlock the phone and keep File Transfer enabled."
} else {
    CopyMTPFolderObject $srcNs $SourcePath $DestPath
}

if ($script:visited -gt 0 -and $script:copied -eq 0 -and $script:skipped -eq 0 -and $script:errors -eq 0) {
    $script:errors++
    Write-Host "ERR:$($SourcePath):No accessible files were found. Unlock the phone and confirm the USB mode is File Transfer / MTP."
}

Write-Host "DONE:$($script:copied):$($script:skipped):$($script:errors)"
[Console]::Out.Flush()
"""


# ─────────────────────────────────────────────────────────────────────────────
# Manager
# ─────────────────────────────────────────────────────────────────────────────

class PhoneBackupManager:
    """
    Top-level manager for phone detection and backup.

    Parameters
    ----------
    backup_root : str | Path
        Root directory for all Aegis backups.
        Phone backups go into ``backup_root/Phones/``.
    """

    def __init__(self, backup_root: str | Path) -> None:
        self.backup_root = Path(backup_root)
        self._phones_dir.mkdir(parents=True, exist_ok=True)

    # ── Public properties ──────────────────────────────────────────────────

    @property
    def _phones_dir(self) -> Path:
        return self.backup_root / "Phones"

    def _device_dir(self, device: PhoneDevice) -> Path:
        return self._phones_dir / device.safe_folder_name

    # ── Detection ─────────────────────────────────────────────────────────

    def detect_phones(self) -> List[PhoneDevice]:
        """
        Return all detected phones, sorted alphabetically by display name.
        Tries MTP portable devices first, then removable drives that look like phones.
        """
        devices: List[PhoneDevice] = []
        seen: set[str] = set()

        for d in (*self._detect_mtp_devices(), *self._detect_drive_phones()):
            key = (d.shell_path or d.drive_root or d.name).lower().strip()
            if key not in seen:
                seen.add(key)
                devices.append(d)

        return sorted(devices, key=lambda d: d.display_name.lower())

    def _detect_mtp_devices(self) -> List[PhoneDevice]:
        if os.name != "nt":
            return []

        raw = self._run_ps_inline(_PS_DETECT_MTP, timeout=15)
        if not raw or raw == "[]":
            return []

        try:
            data = json.loads(raw)
            if isinstance(data, dict):
                data = [data]
        except (json.JSONDecodeError, TypeError):
            return []

        devices = []
        for item in data:
            name = item.get("Name", "Unknown Device")
            path = item.get("Path", "")
            if not path:
                continue
            if re.match(r"^[a-zA-Z]:\\", path):
                continue

            devices.append(PhoneDevice(
                name=name,
                access_type="mtp",
                shell_path=path,
                friendly_name=name,
            ))

        return devices

    def _detect_drive_phones(self) -> List[PhoneDevice]:
        """
        Check removable drives for phone-like folder signatures
        (DCIM, Android, Pictures).
        """
        if os.name != "nt":
            return []

        devices: List[PhoneDevice] = []
        try:
            bitmask = ctypes.windll.kernel32.GetLogicalDrives()
        except AttributeError:
            return []

        for i in range(26):
            if not (bitmask >> i & 1):
                continue
            drive = f"{chr(65 + i)}:\\"
            try:
                drive_type = ctypes.windll.kernel32.GetDriveTypeW(drive)
                if drive_type != 2:          # 2 = DRIVE_REMOVABLE
                    continue
                if not os.path.exists(drive):
                    continue
                indicators = ["DCIM", "Android", "Pictures", "DCIM/Camera"]
                if any(os.path.isdir(os.path.join(drive, f)) for f in indicators):
                    label = self._get_drive_label(drive) or f"Phone ({drive[0]}:)"
                    devices.append(PhoneDevice(
                        name=label,
                        access_type="drive",
                        drive_root=drive,
                        friendly_name=label,
                    ))
            except OSError:
                continue

        return devices

    @staticmethod
    def _get_drive_label(drive: str) -> Optional[str]:
        try:
            buf = ctypes.create_unicode_buffer(261)
            ctypes.windll.kernel32.GetVolumeInformationW(
                drive, buf, 261, None, None, None, None, 0
            )
            return buf.value or None
        except Exception:
            return None

    # ── Backup ────────────────────────────────────────────────────────────

    def backup_phone(
        self,
        device: PhoneDevice,
        progress_callback: Optional[ProgressCallback] = None,
        should_stop: Optional[StopCallback] = None,
    ) -> Dict[str, Any]:
        """
        Incremental backup of *device*.

        Stage 1: copy phone files into ``Phones/<Device>/_incoming/`` (skips
                 anything already sitting there from a previous interrupted run).
        Stage 2: PhoneFileOrganizer moves everything from _incoming/ into
                 ``Phones/<Device>/latest/{Photos, Videos, …}``.

        Stage 2 always runs, even if stage 1 was cancelled or the phone
        stopped responding — so whatever made it across is organized and
        nothing already-copied is lost. _incoming/ stays empty between
        clean runs and only holds "work in progress" if a run was interrupted.
        """
        device_dir   = self._device_dir(device)
        incoming_dir = device_dir / "_incoming"
        latest_dir   = device_dir / "latest"

        device_dir.mkdir(parents=True, exist_ok=True)
        incoming_dir.mkdir(parents=True, exist_ok=True)

        result: Dict[str, Any] = {
            "device":          device.display_name,
            "device_folder":   str(device_dir),
            "latest_dir":      str(latest_dir),
            "started_at":      datetime.now().isoformat(),
            "completed_at":    None,
            "cancelled":       False,
            "stalled":         False,
            "stage":           "copy",
            "copy_result":     None,
            "organize_result": None,
            "errors":          [],
        }

        if should_stop and should_stop():
            result.update(cancelled=True, completed_at=datetime.now().isoformat())
            self._record_run(device_dir, result)
            return result

        # ── Stage 1: Copy ──────────────────────────────────────────────────
        if progress_callback:
            progress_callback(0, f"Connecting to {device.display_name}…")

        if device.access_type == "mtp":
            copy_result = self._copy_mtp(
                shell_path=device.shell_path,
                dest=str(incoming_dir),
                progress_callback=progress_callback,
                should_stop=should_stop,
            )
        else:
            copy_result = self._copy_drive(
                source=device.drive_root,
                dest=str(incoming_dir),
                progress_callback=progress_callback,
                should_stop=should_stop,
            )

        result["copy_result"] = copy_result
        if copy_result.get("errors"):
            result["errors"].extend(copy_result["errors"])
        if copy_result.get("stalled"):
            result["stalled"] = True

        copy_stopped = bool(copy_result.get("cancelled")) or bool(should_stop and should_stop())

        # ── Stage 2: Organize (runs even if stage 1 was cut short) ──────────
        result["stage"] = "organize"
        if progress_callback:
            progress_callback(-1, "Organizing files into categories…")

        organizer = PhoneFileOrganizer(backup_root=str(self._phones_dir))
        org_result = organizer.organize_existing_backup(
            backup_path=str(incoming_dir),
            device_name=device.display_name,
            move_files=True,
            progress_callback=lambda p, m: progress_callback(-1, m) if progress_callback else None,
            should_stop=should_stop,
        )
        result["organize_result"] = org_result

        for err in org_result.get("errors", []):
            result["errors"].append(f"{err['file']}: {err['error']}")

        _prune_empty_dirs(incoming_dir)

        if copy_stopped or org_result.get("cancelled"):
            result.update(
                cancelled=True,
                completed_at=datetime.now().isoformat(),
            )
            self._record_run(device_dir, result)
            if progress_callback:
                if result["stalled"]:
                    progress_callback(100, "Phone stopped responding — progress saved, run backup again to continue.")
                else:
                    progress_callback(100, "Backup stopped — progress saved.")
            return result

        result.update(stage="complete", completed_at=datetime.now().isoformat())
        self._record_run(device_dir, result)
        if progress_callback:
            progress_callback(100, "Backup complete.")

        return result

    # ── Copy helpers ──────────────────────────────────────────────────────

    def _copy_mtp(
        self,
        shell_path: str,
        dest: str,
        progress_callback: Optional[ProgressCallback],
        should_stop: Optional[StopCallback],
    ) -> Dict[str, Any]:
        """
        Copy all files from an MTP shell-namespace path to a local folder
        using a PowerShell script. Files already present in *dest* (from a
        previous interrupted run) are skipped, which is what makes backups
        resumable.
        """
        result: Dict[str, Any] = {
            "copied": 0, "skipped": 0,
            "errors": [], "cancelled": False, "stalled": False,
        }

        if os.name != "nt":
            result["errors"].append("MTP copy is only supported on Windows.")
            return result

        ps_path = _write_temp_script(_PS_COPY_MTP)
        try:
            cmd = [
                "powershell", "-NoProfile", "-NonInteractive",
                "-File", ps_path,
                "-SourcePath", shell_path,
                "-DestPath",   dest,
                "-TimeoutSec", str(MTP_FILE_TIMEOUT_SECONDS),
            ]
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )

            stdout = proc.stdout
            if stdout is None:
                result["errors"].append("Failed to capture PowerShell output.")
                proc.terminate()
                return result

            line_queue: "queue.Queue[Optional[str]]" = queue.Queue()

            def _reader():
                try:
                    for raw_line in stdout:
                        line_queue.put(raw_line)
                except (ValueError, OSError):
                    pass
                finally:
                    line_queue.put(None)  # sentinel: stream closed

            threading.Thread(target=_reader, daemon=True).start()

            start_time = time.monotonic()
            last_activity = start_time
            stream_closed = False

            while True:
                if should_stop and should_stop():
                    proc.kill()
                    result["cancelled"] = True
                    break

                try:
                    line = line_queue.get(timeout=0.5)
                except queue.Empty:
                    if proc.poll() is not None and stream_closed:
                        break
                    if time.monotonic() - last_activity > MTP_STALL_TIMEOUT_SECONDS:
                        proc.kill()
                        result["stalled"] = True
                        result["cancelled"] = True
                        result["errors"].append(
                            f"Phone stopped responding after {result['copied']} file(s) copied "
                            f"this run. Files already copied have been organized — run the "
                            f"backup again to continue with the rest."
                        )
                        break
                    continue

                if line is None:
                    stream_closed = True
                    if proc.poll() is not None:
                        break
                    continue

                last_activity = time.monotonic()
                line = line.rstrip()
                if not line:
                    continue

                if line.startswith("FILE_OK:"):
                    result["copied"] += 1
                    fname = Path(line[8:]).name
                    if progress_callback:
                        progress_callback(-1, f"Copied: {fname}")

                elif line.startswith("FILE_SKIP:"):
                    result["skipped"] += 1

                elif line.startswith("FILE_TIMEOUT:"):
                    result["errors"].append(f"Timed out: {line[13:]}")

                elif line.startswith("FOLDER_ENTER:"):
                    folder = line[13:].split("\\")[-1]
                    if progress_callback:
                        progress_callback(-1, f"Scanning: {folder}")

                elif line.startswith("FOLDER_SKIP:"):
                    folder = line[12:].split("\\")[-1]
                    if progress_callback:
                        progress_callback(-1, f"Skipping restricted folder: {folder}")

                elif line.startswith("STATS:"):
                    parts = line[6:].split(":")
                    if len(parts) >= 3:
                        result["copied"] = int(parts[0])
                        result["skipped"] = int(parts[1])
                        err_count = int(parts[2])
                        elapsed = int(time.monotonic() - start_time)
                        if progress_callback:
                            progress_callback(
                                -2,
                                f"Copied {result['copied']} · Skipped {result['skipped']} · "
                                f"Errors {err_count} · {elapsed}s elapsed",
                            )

                elif line.startswith("ERR:"):
                    result["errors"].append(line[4:])

                elif line.startswith("DONE:"):
                    parts = line[5:].split(":")
                    if len(parts) >= 3:
                        result["copied"]  = int(parts[0])
                        result["skipped"] = int(parts[1])
                        err_count = int(parts[2])
                        if err_count:
                            result["errors"].append(
                                f"{err_count} file(s) failed to copy."
                            )

        finally:
            try:
                os.unlink(ps_path)
            except OSError:
                pass

        return result

    def _copy_drive(
        self,
        source: str,
        dest: str,
        progress_callback: Optional[ProgressCallback],
        should_stop: Optional[StopCallback],
    ) -> Dict[str, Any]:
        """
        Copy all files from a real drive path (USB mass-storage phones) using
        shutil. Skips Windows system directories and Android's restricted
        per-app folders. Files that already exist at the destination are only
        skipped if they're the same size and at least as new — so updated
        files on the phone get re-copied.
        """
        result: Dict[str, Any] = {
            "copied": 0, "skipped": 0,
            "errors": [], "cancelled": False, "stalled": False,
        }

        _SKIP_DIRS = {
            "windows", "system volume information", "$recycle.bin",
            "program files", "program files (x86)", "programdata",
            ".thumbnails", ".trashed", ".cache", "lost.dir",
        }

        dest_path = Path(dest)
        dest_path.mkdir(parents=True, exist_ok=True)

        source_path = Path(source)
        all_files: List[Path] = []

        for root, dirs, files in os.walk(source):
            rel_root = Path(root).relative_to(source_path)
            parent_name = rel_root.parts[-1].lower() if rel_root.parts else ""

            filtered_dirs = []
            for d in dirs:
                d_lower = d.lower()
                if d_lower in _SKIP_DIRS or d.startswith("."):
                    continue
                if d_lower in ("data", "obb") and parent_name == "android":
                    continue
                filtered_dirs.append(d)
            dirs[:] = filtered_dirs

            for fname in files:
                all_files.append(Path(root) / fname)

        total = max(len(all_files), 1)

        for idx, src_file in enumerate(all_files):
            if should_stop and should_stop():
                result["cancelled"] = True
                break

            if progress_callback:
                pct = int(idx / total * 100)
                progress_callback(pct, f"Copying: {src_file.name}")

            try:
                rel      = src_file.relative_to(source)
                dst_file = dest_path / rel
                dst_file.parent.mkdir(parents=True, exist_ok=True)

                if dst_file.exists():
                    src_stat = src_file.stat()
                    dst_stat = dst_file.stat()
                    if (
                        dst_stat.st_size == src_stat.st_size
                        and int(dst_stat.st_mtime) >= int(src_stat.st_mtime)
                    ):
                        result["skipped"] += 1
                        continue

                shutil.copy2(str(src_file), str(dst_file))
                result["copied"] += 1

            except PermissionError:
                pass   # skip locked files silently
            except Exception as exc:
                result["errors"].append(f"{src_file.name}: {exc}")

        return result

    # ── History & listing ─────────────────────────────────────────────────

    def _record_run(self, device_dir: Path, result: Dict[str, Any]) -> None:
        """Append a small entry describing this run to backup_log.json."""
        log_path = device_dir / "backup_log.json"
        history: List[Dict[str, Any]] = []

        if log_path.exists():
            try:
                loaded = json.loads(log_path.read_text(encoding="utf-8"))
                if isinstance(loaded, list):
                    history = loaded
            except (OSError, json.JSONDecodeError):
                history = []

        copy_r = result.get("copy_result") or {}
        org_r  = result.get("organize_result") or {}

        history.append({
            "started_at":     result.get("started_at"),
            "completed_at":   result.get("completed_at"),
            "cancelled":      result.get("cancelled", False),
            "stalled":        result.get("stalled", False),
            "files_copied":   copy_r.get("copied", 0),
            "files_skipped":  copy_r.get("skipped", 0),
            "files_organized": org_r.get("files_organized", 0),
            "error_count":    len(result.get("errors") or []),
        })
        history = history[-50:]

        try:
            log_path.write_text(json.dumps(history, indent=2), encoding="utf-8")
        except OSError:
            pass

    def _read_log(self, device_dir: Path) -> List[Dict[str, Any]]:
        log_path = device_dir / "backup_log.json"
        if not log_path.exists():
            return []
        try:
            loaded = json.loads(log_path.read_text(encoding="utf-8"))
            return loaded if isinstance(loaded, list) else []
        except (OSError, json.JSONDecodeError):
            return []

    def list_all_backed_up_devices(self, include_total_size: bool = False) -> List[Dict[str, Any]]:
        """
        Return every device that has at least one recorded run, sorted A→Z.
        """
        if not self._phones_dir.exists():
            return []

        out = []
        for d in sorted(self._phones_dir.iterdir(), key=lambda p: p.name.lower()):
            if not d.is_dir():
                continue

            history = self._read_log(d)
            latest_dir = d / "latest"
            incoming_dir = d / "_incoming"

            out.append({
                "name":           d.name.replace("_", " "),
                "folder":         str(d),
                "snapshot_count": len(history),
                "has_latest":     latest_dir.exists(),
                "latest_path":    str(latest_dir) if latest_dir.exists() else None,
                "total_size":     _dir_size(latest_dir) if include_total_size else None,
                "last_backup":    history[-1].get("completed_at") if history else None,
                "pending_files":  _count_files(incoming_dir),
            })
        return out

    def list_device_run_history(self, display_name: str) -> List[Dict[str, Any]]:
        """Return run history entries for *display_name*, newest first."""
        ddir = self._phones_dir / _safe_folder_name(display_name)
        return list(reversed(self._read_log(ddir)))

    # ── Legacy cleanup ───────────────────────────────────────────────────

    def find_legacy_snapshot_dirs(self) -> List[Path]:
        """
        Locate old-style per-run snapshot folders (e.g. '2026-05-30_143022')
        left over from the previous backup layout. These were full copies of
        every file and are no longer used — 'latest/' holds the organized
        files and 'backup_log.json' holds history.

        This does NOT delete anything; it just returns the list so you can
        review it and delete folders yourself once you've confirmed 'latest/'
        for that device looks complete.
        """
        pattern = re.compile(r"^\d{4}-\d{2}-\d{2}_\d{6}$")
        found: List[Path] = []

        if not self._phones_dir.exists():
            return found

        for device_dir in self._phones_dir.iterdir():
            if not device_dir.is_dir():
                continue
            for child in device_dir.iterdir():
                if child.is_dir() and pattern.match(child.name):
                    found.append(child)

        return found

    # ── PowerShell helpers ────────────────────────────────────────────────

    @staticmethod
    def _run_ps_inline(script: str, timeout: int = 30) -> str:
        """Run a short PowerShell script inline and return stdout."""
        try:
            r = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive",
                 "-Command", script],
                capture_output=True, text=True, timeout=timeout,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            return r.stdout.strip()
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return ""


# ─────────────────────────────────────────────────────────────────────────────
# Module-level helpers
# ─────────────────────────────────────────────────────────────────────────────

def _write_temp_script(body: str) -> str:
    """Write *body* to a temp .ps1 file and return its path."""
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".ps1", delete=False, encoding="utf-8"
    ) as f:
        f.write(body)
        return f.name


def _prune_empty_dirs(root: Path) -> None:
    """Remove now-empty subdirectories left behind after organizing."""
    for dirpath, _dirnames, _filenames in os.walk(root, topdown=False):
        path = Path(dirpath)
        if path == root:
            continue
        try:
            if not any(path.iterdir()):
                path.rmdir()
        except OSError:
            pass


def _safe_folder_name(value: str) -> str:
    cleaned = re.sub(r"[^\w\s\-]", "_", value.strip())
    cleaned = re.sub(r"\s+", "_", cleaned)
    return cleaned or "Unknown_Device"


def _dir_size(path: Path) -> int:
    total = 0
    try:
        for entry in path.rglob("*"):
            if entry.is_file():
                try:
                    total += entry.stat().st_size
                except OSError:
                    pass
    except OSError:
        pass
    return total


def _count_files(path: Path) -> int:
    if not path.exists():
        return 0
    count = 0
    try:
        for entry in path.rglob("*"):
            if entry.is_file():
                count += 1
    except OSError:
        pass
    return count