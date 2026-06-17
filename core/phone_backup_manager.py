"""
core/phone_backup_manager.py
============================
Detects phones connected via USB (MTP / file-transfer mode or USB mass-storage),
backs up every accessible file, and organizes it into category folders via
PhoneFileOrganizer — all tracked by a persistent per-device manifest so repeat
backups are fast, incremental, and resumable without supervision.

Folder layout produced
──────────────────────
{backup_root}/
  Phones/
    Galaxy_S24/
      _incoming/            ← persistent staging area for the MTP/drive copy
                               stage. Files land here, then get moved into
                               category folders below. Anything left here
                               after a run means it still needs organizing
                               (or the run was interrupted) — the next run
                               picks up right where it left off.
      latest/
        Photos/
        Videos/
        Documents/
        Downloads/
        Audio/
        Messages/
        Private_Files/
        Miscellaneous/
      backup_manifest.json  ← per-file record: size, modified date, category,
                               dest path, status, failure count. Consulted
                               BEFORE the MTP copy step so unchanged files are
                               never re-transferred, and updated AFTER the
                               organize step once a file's final resting
                               place is known.
      backup_log.json       ← small history of each run (timestamps, counts)
    Pixel_8_Pro/
      …

Self-reliance
──────────────
backup_phone_until_complete() wraps a single backup_phone() attempt in an
automatic retry loop: if the phone stops responding mid-copy, it waits,
re-detects the device (in case Windows reassigned it a new MTP shell path
after a brief disconnect), and tries again on its own — no need to come
back and click "Start Backup" a second time. This is meant to be left
running unattended (e.g. overnight).

Detection strategy (tried in order)
────────────────────────────────────
1. MTP portable devices  – Windows Shell.Application namespace (PowerShell)
2. Removable drives with phone-like folder structure (DCIM / Android)

Usage
─────
    mgr = PhoneBackupManager("D:\\Aegis_Backups")
    phones = mgr.detect_phones()
    result = mgr.backup_phone_until_complete(phones[0], progress_callback=…, should_stop=…)
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
# assume the phone has stopped responding and give up on this attempt.
MTP_STALL_TIMEOUT_SECONDS = 120

# A file that fails this many cumulative times gets marked "skip" in the
# manifest so future attempts stop wasting time retrying a dead file.
MAX_FILE_FAIL_ATTEMPTS = 3

# Defaults for the unattended retry loop.
DEFAULT_MAX_RETRY_ATTEMPTS = 12
DEFAULT_MAX_TOTAL_RUNTIME_SECONDS = 8 * 60 * 60  # 8 hours — safe for overnight


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

_PS_DETECT_MTP = r"""
$shell = New-Object -ComObject Shell.Application
$ns    = $shell.Namespace(17)        # 17 = CSIDL_DRIVES ("This PC")
$out   = @()
foreach ($item in $ns.Items()) {
    if (-not $item.IsFolder) { continue }
    $t = $item.Type.ToLower()
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

# Recursively copy files from an MTP shell-namespace path to a real local
# folder, consulting a manifest lookup so unchanged files are skipped
# without ever being copied.
#
# Output protocol:
#   FILE_OK:<local_dest_path>
#   FILE_SKIP:<filename>                  (already in _incoming this run)
#   FILE_SKIP_MANIFEST:<relkey>            (manifest says unchanged/permanently-skip)
#   FILE_TIMEOUT:<relkey>
#   FILE_ERR:<relkey>:<message>
#   FOLDER_ENTER:<shell_path>
#   FOLDER_SKIP:<shell_path>
#   STATS:<copied>:<skipped>:<errors>      (heartbeat, every ~5s)
#   ERR:<shell_path>:<message>             (folder/source-level, not file-specific)
#   DONE:<copied>:<skipped>:<errors>
_PS_COPY_MTP = r"""
param(
    [string]$SourcePath,
    [string]$DestPath,
    [int]$TimeoutSec = 60,
    [string]$ManifestPath = ""
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

$ALWAYS_SKIP = @('.thumbnails', '.trashed', '.cache', '.thumbcache', 'lost.dir')

# Load the manifest lookup (relative path -> {size, skip}) if provided.
$manifestLookup = @{}
if ($ManifestPath -and (Test-Path $ManifestPath)) {
    try {
        $raw = Get-Content $ManifestPath -Raw -ErrorAction Stop
        if ($raw) {
            $parsed = $raw | ConvertFrom-Json -ErrorAction Stop
            if ($parsed) {
                foreach ($prop in $parsed.PSObject.Properties) {
                    $manifestLookup[$prop.Name] = $prop.Value
                }
            }
        }
    } catch {
        $manifestLookup = @{}
    }
}

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

function CopyMTPFolderObject($Folder, [string]$Label, [string]$RelLabel, [string]$Dst) {
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
            $relKey = if ($RelLabel) { "$RelLabel\$safeName" } else { $safeName }

            if ($item.IsFolder) {
                if (ShouldSkipItem $Label $item.Name) {
                    Write-Host "FOLDER_SKIP:$($Label)\$($safeName)"
                    [Console]::Out.Flush()
                    continue
                }
                $childFolder = $null
                try { $childFolder = $item.GetFolder() } catch { $childFolder = $null }
                CopyMTPFolderObject $childFolder "$Label\$safeName" $relKey (Join-Path $Dst $safeName)
                continue
            }

            # Consult the manifest BEFORE touching the USB/MTP layer at all —
            # this is what makes repeat backups fast.
            if ($manifestLookup.ContainsKey($relKey)) {
                $entry = $manifestLookup[$relKey]
                $isPermSkip = $false
                try { $isPermSkip = [bool]$entry.skip } catch { $isPermSkip = $false }

                if ($isPermSkip) {
                    $script:skipped++
                    Write-Host "FILE_SKIP_MANIFEST:$relKey"
                    [Console]::Out.Flush()
                    continue
                }

                $knownSize = $null
                try { $knownSize = [int64]$entry.size } catch { $knownSize = $null }
                $liveSize = $null
                try { $liveSize = $item.Size } catch { $liveSize = $null }

                if ($null -ne $knownSize -and $null -ne $liveSize -and $liveSize -eq $knownSize) {
                    $script:skipped++
                    Write-Host "FILE_SKIP_MANIFEST:$relKey"
                    [Console]::Out.Flush()
                    continue
                }
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
                Write-Host "FILE_ERR:$($relKey):CopyHere failed: $_"
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
                # sort by when they were actually taken, not when backed up.
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
                Write-Host "FILE_TIMEOUT:$relKey"
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
    CopyMTPFolderObject $srcNs $SourcePath "" $DestPath
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

    # ── Backup (single attempt) ─────────────────────────────────────────────

    def backup_phone(
        self,
        device: PhoneDevice,
        progress_callback: Optional[ProgressCallback] = None,
        should_stop: Optional[StopCallback] = None,
    ) -> Dict[str, Any]:
        """
        One incremental backup attempt for *device*.

        Stage 1: copy phone files into ``Phones/<Device>/_incoming/``, consulting
                 the manifest so unchanged files are never re-transferred.
        Stage 2: PhoneFileOrganizer moves everything from _incoming/ into
                 ``Phones/<Device>/latest/{Photos, Videos, …}``, and the
                 manifest is updated with each file's final size, modified
                 date, and category.

        Stage 2 always runs, even if stage 1 stalled or was cancelled — so
        whatever made it across is organized and recorded, nothing already
        copied is lost, and the manifest reflects reality even after a
        partial run.

        For the full unattended/overnight experience, use
        backup_phone_until_complete() instead, which wraps this in an
        automatic retry loop.
        """
        device_dir   = self._device_dir(device)
        incoming_dir = device_dir / "_incoming"
        latest_dir   = device_dir / "latest"

        device_dir.mkdir(parents=True, exist_ok=True)
        incoming_dir.mkdir(parents=True, exist_ok=True)

        manifest = self._load_manifest(device_dir, device.display_name)
        manifest_lookup = self._build_manifest_lookup(manifest)

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

        if progress_callback:
            progress_callback(-3, f"Connecting to {device.display_name}…")

        if device.access_type == "mtp":
            copy_result = self._copy_mtp(
                shell_path=device.shell_path,
                dest=str(incoming_dir),
                manifest_lookup=manifest_lookup,
                progress_callback=progress_callback,
                should_stop=should_stop,
            )
        else:
            copy_result = self._copy_drive(
                source=device.drive_root,
                dest=str(incoming_dir),
                manifest_lookup=manifest_lookup,
                progress_callback=progress_callback,
                should_stop=should_stop,
            )

        result["copy_result"] = copy_result
        if copy_result.get("errors"):
            result["errors"].extend(copy_result["errors"])
        if copy_result.get("stalled"):
            result["stalled"] = True

        self._record_failures(manifest, copy_result.get("failed_keys", []))

        copy_stopped = bool(copy_result.get("cancelled")) or bool(should_stop and should_stop())

        # ── Organize (runs even if the copy stage was cut short) ───────────
        result["stage"] = "organize"
        if progress_callback:
            progress_callback(-3, "Organizing files into categories…")

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

        self._record_successes(manifest, org_result.get("manifest_entries", []))
        self._save_manifest(device_dir, manifest)
        result["manifest_summary"] = self._summarize_manifest(manifest)

        _prune_empty_dirs(incoming_dir)

        if copy_stopped or org_result.get("cancelled"):
            result.update(cancelled=True, completed_at=datetime.now().isoformat())
            self._record_run(device_dir, result)
            if progress_callback:
                progress_callback(
                    -3,
                    "Phone stopped responding — progress saved." if result["stalled"]
                    else "Backup stopped — progress saved.",
                )
            return result

        result.update(stage="complete", completed_at=datetime.now().isoformat())
        self._record_run(device_dir, result)
        if progress_callback:
            progress_callback(100, "Backup complete.")

        return result

    # ── Backup (self-reliant, retries automatically) ────────────────────────

    def backup_phone_until_complete(
        self,
        device: PhoneDevice,
        progress_callback: Optional[ProgressCallback] = None,
        should_stop: Optional[StopCallback] = None,
        max_attempts: int = DEFAULT_MAX_RETRY_ATTEMPTS,
        max_total_seconds: int = DEFAULT_MAX_TOTAL_RUNTIME_SECONDS,
    ) -> Dict[str, Any]:
        """
        Run backup_phone() in an automatic retry loop so a single call can be
        left unattended overnight. If a run stalls because the phone stopped
        responding, this waits, re-detects the device (Windows can reassign a
        new MTP shell path after a brief disconnect), and tries again on its
        own — no need to come back and click "Start Backup" a second time.

        Stops retrying when:
          - a run completes without stalling (success, or a genuine user cancel), or
          - max_attempts is reached, or
          - max_total_seconds of wall-clock time has elapsed.
        """
        start_time = time.monotonic()
        last_result: Dict[str, Any] = {}
        current_device = device

        for attempt in range(1, max_attempts + 1):
            if should_stop and should_stop():
                break

            if progress_callback and attempt > 1:
                progress_callback(-3, f"Retry attempt {attempt} of {max_attempts}…")

            last_result = self.backup_phone(
                current_device,
                progress_callback=progress_callback,
                should_stop=should_stop,
            )
            last_result["attempt"] = attempt

            if not last_result.get("stalled"):
                # Either succeeded outright or was a genuine user cancel —
                # either way, don't auto-retry.
                break

            if should_stop and should_stop():
                break

            if time.monotonic() - start_time > max_total_seconds:
                last_result.setdefault("errors", []).append(
                    "Stopped auto-retrying after reaching the maximum unattended run time."
                )
                break

            wait_seconds = min(15 * attempt, 120)
            if progress_callback:
                progress_callback(-3, f"Phone unresponsive — waiting {wait_seconds}s before retrying…")
            for _ in range(wait_seconds):
                if should_stop and should_stop():
                    break
                time.sleep(1)

            refreshed = self._reacquire_device(current_device)
            if refreshed:
                current_device = refreshed

        last_result["attempts_used"] = last_result.get("attempt", 1)
        return last_result

    def _reacquire_device(self, device: PhoneDevice) -> Optional[PhoneDevice]:
        """
        Try to find the same phone again after a brief disconnect, in case
        Windows assigned it a different shell path on reconnect.
        """
        try:
            candidates = self.detect_phones()
        except Exception:
            return None

        for candidate in candidates:
            if candidate.access_type != device.access_type:
                continue
            if candidate.display_name.lower() == device.display_name.lower():
                return candidate

        return None

    # ── Manifest ─────────────────────────────────────────────────────────

    def _manifest_path(self, device_dir: Path) -> Path:
        return device_dir / "backup_manifest.json"

    def _load_manifest(self, device_dir: Path, device_name: str) -> Dict[str, Any]:
        path = self._manifest_path(device_dir)
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(data, dict) and isinstance(data.get("files"), dict):
                    return data
            except (OSError, json.JSONDecodeError):
                pass
        return {"device_name": device_name, "last_updated": None, "files": {}}

    def _save_manifest(self, device_dir: Path, manifest: Dict[str, Any]) -> None:
        manifest["last_updated"] = datetime.now().isoformat()
        path = self._manifest_path(device_dir)
        try:
            path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        except OSError:
            pass

    def _build_manifest_lookup(self, manifest: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
        """{relative_path: {"size": int, "skip": bool}} for the copy stage to consult."""
        lookup: Dict[str, Dict[str, Any]] = {}
        for relkey, entry in manifest.get("files", {}).items():
            if entry.get("skip"):
                lookup[relkey] = {"size": entry.get("size", 0), "skip": True}
            elif entry.get("status") == "ok":
                lookup[relkey] = {"size": entry.get("size", 0), "skip": False}
        return lookup

    def _record_failures(self, manifest: Dict[str, Any], failed_keys: List[str]) -> None:
        files = manifest.setdefault("files", {})
        for relkey in failed_keys:
            entry = files.get(relkey, {})
            entry["status"] = "failed"
            entry["fail_count"] = entry.get("fail_count", 0) + 1
            entry["last_attempt"] = datetime.now().isoformat()
            if entry["fail_count"] >= MAX_FILE_FAIL_ATTEMPTS:
                entry["skip"] = True
            files[relkey] = entry

    def _record_successes(self, manifest: Dict[str, Any], manifest_entries: List[Dict[str, Any]]) -> None:
        files = manifest.setdefault("files", {})
        now = datetime.now().isoformat()
        for entry in manifest_entries:
            files[entry["source_relative"]] = {
                "size": entry.get("size", 0),
                "modified": entry.get("modified"),
                "category": entry.get("category"),
                "dest_path": entry.get("dest_relative"),
                "status": "ok",
                "copied_at": now,
                "fail_count": 0,
            }

    def _summarize_manifest(self, manifest: Dict[str, Any]) -> Dict[str, int]:
        ok = 0
        failed_pending = 0
        skipped_permanent = 0
        for entry in manifest.get("files", {}).values():
            if entry.get("skip"):
                skipped_permanent += 1
            elif entry.get("status") == "ok":
                ok += 1
            elif entry.get("status") == "failed":
                failed_pending += 1
        return {
            "confirmed_files": ok,
            "pending_retry_files": failed_pending,
            "permanently_skipped_files": skipped_permanent,
        }

    # ── Copy helpers ──────────────────────────────────────────────────────

    def _copy_mtp(
        self,
        shell_path: str,
        dest: str,
        manifest_lookup: Dict[str, Dict[str, Any]],
        progress_callback: Optional[ProgressCallback],
        should_stop: Optional[StopCallback],
    ) -> Dict[str, Any]:
        result: Dict[str, Any] = {
            "copied": 0, "skipped": 0, "skipped_manifest": 0,
            "errors": [], "failed_keys": [],
            "cancelled": False, "stalled": False,
        }

        if os.name != "nt":
            result["errors"].append("MTP copy is only supported on Windows.")
            return result

        manifest_path = _write_temp_manifest_lookup(manifest_lookup)
        ps_path = _write_temp_script(_PS_COPY_MTP)
        try:
            cmd = [
                "powershell", "-NoProfile", "-NonInteractive",
                "-File", ps_path,
                "-SourcePath", shell_path,
                "-DestPath",   dest,
                "-TimeoutSec", str(MTP_FILE_TIMEOUT_SECONDS),
                "-ManifestPath", manifest_path,
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
                            f"Phone stopped responding after {result['copied']} file(s) "
                            f"copied this attempt. Already-copied files have been organized."
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

                elif line.startswith("FILE_SKIP_MANIFEST:"):
                    result["skipped"] += 1
                    result["skipped_manifest"] += 1

                elif line.startswith("FILE_SKIP:"):
                    result["skipped"] += 1

                elif line.startswith("FILE_TIMEOUT:"):
                    relkey = line[13:]
                    result["errors"].append(f"Timed out: {relkey}")
                    result["failed_keys"].append(relkey)

                elif line.startswith("FILE_ERR:"):
                    remainder = line[9:]
                    relkey, _, message = remainder.partition(":")
                    result["errors"].append(f"{relkey}: {message}")
                    result["failed_keys"].append(relkey)

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
                        if err_count and not result["failed_keys"]:
                            result["errors"].append(f"{err_count} file(s) failed to copy.")

        finally:
            try:
                os.unlink(ps_path)
            except OSError:
                pass
            try:
                os.unlink(manifest_path)
            except OSError:
                pass

        return result

    def _copy_drive(
        self,
        source: str,
        dest: str,
        manifest_lookup: Dict[str, Dict[str, Any]],
        progress_callback: Optional[ProgressCallback],
        should_stop: Optional[StopCallback],
    ) -> Dict[str, Any]:
        """
        Copy all files from a real drive path (USB mass-storage phones) using
        shutil, consulting the same manifest as the MTP path so unchanged
        files are skipped without even being statted twice.
        """
        result: Dict[str, Any] = {
            "copied": 0, "skipped": 0, "skipped_manifest": 0,
            "errors": [], "failed_keys": [],
            "cancelled": False, "stalled": False,
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

            rel_key = str(src_file.relative_to(source))

            if progress_callback:
                pct = int(idx / total * 100)
                progress_callback(pct, f"Copying: {src_file.name}")

            manifest_entry = manifest_lookup.get(rel_key)
            if manifest_entry:
                if manifest_entry.get("skip"):
                    result["skipped"] += 1
                    result["skipped_manifest"] += 1
                    continue
                try:
                    if manifest_entry.get("size") == src_file.stat().st_size:
                        result["skipped"] += 1
                        result["skipped_manifest"] += 1
                        continue
                except OSError:
                    pass

            try:
                dst_file = dest_path / rel_key
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
                result["errors"].append(f"{rel_key}: {exc}")
                result["failed_keys"].append(rel_key)

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
            "started_at":      result.get("started_at"),
            "completed_at":    result.get("completed_at"),
            "cancelled":       result.get("cancelled", False),
            "stalled":         result.get("stalled", False),
            "attempt":         result.get("attempt"),
            "files_copied":    copy_r.get("copied", 0),
            "files_skipped":   copy_r.get("skipped", 0),
            "files_skipped_manifest": copy_r.get("skipped_manifest", 0),
            "files_organized": org_r.get("files_organized", 0),
            "error_count":     len(result.get("errors") or []),
            "manifest_summary": result.get("manifest_summary"),
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
        """Return every device that has at least one recorded run, sorted A→Z."""
        if not self._phones_dir.exists():
            return []

        out = []
        for d in sorted(self._phones_dir.iterdir(), key=lambda p: p.name.lower()):
            if not d.is_dir():
                continue

            history = self._read_log(d)
            latest_dir = d / "latest"
            incoming_dir = d / "_incoming"
            manifest = self._load_manifest(d, d.name.replace("_", " "))
            manifest_summary = self._summarize_manifest(manifest)

            out.append({
                "name":           d.name.replace("_", " "),
                "folder":         str(d),
                "snapshot_count": len(history),
                "has_latest":     latest_dir.exists(),
                "latest_path":    str(latest_dir) if latest_dir.exists() else None,
                "total_size":     _dir_size(latest_dir) if include_total_size else None,
                "last_backup":    history[-1].get("completed_at") if history else None,
                "pending_files":  _count_files(incoming_dir),
                "confirmed_files": manifest_summary["confirmed_files"],
                "permanently_skipped_files": manifest_summary["permanently_skipped_files"],
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
        left over from a previous backup layout. These were full copies of
        every file and are no longer used. Returns the list without deleting
        anything — review and delete manually once 'latest/' looks complete.
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


def _write_temp_manifest_lookup(lookup: Dict[str, Dict[str, Any]]) -> str:
    """Write the manifest lookup to a temp .json file for the PS script to read."""
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False, encoding="utf-8"
    ) as f:
        json.dump(lookup, f)
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