"""
core/phone_backup_manager.py
============================
Detects phones connected via USB (MTP / file-transfer mode or USB mass-storage)
and backs up every accessible file, then hands the raw dump to
PhoneFileOrganizer so files land in labelled category folders.

Folder layout produced
──────────────────────
{backup_root}/
  Phones/                           ← all phone backups live here
    Galaxy_S24/                     ← one folder per device, A→Z sorted
      2026-05-30_143022/            ← timestamped full snapshot
        Photos/
        Videos/
        Documents/
        Downloads/
        Audio/
        Messages/
        Private_Files/
        Miscellaneous/
      latest/                       ← refreshed after every successful run
        Photos/
        …
    Pixel_8_Pro/
      …

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
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from modules.phone_file_organizer import PhoneFileOrganizer

ProgressCallback = Callable[[int, str], None]
StopCallback = Callable[[], bool]


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

# List the immediate child folders of an MTP shell-namespace path.
# Used to find "Internal storage" / "Phone" inside the device root.
_PS_LIST_SUBFOLDERS = r"""
param([string]$ParentPath)
$shell = New-Object -ComObject Shell.Application
$ns = $shell.Namespace($ParentPath)
$out = @()
if ($ns -ne $null) {
    foreach ($item in $ns.Items()) {
        if ($item.IsFolder) {
            $out += [PSCustomObject]@{ Name = $item.Name; Path = $item.Path }
        }
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
#   ERR:<shell_path>:<message>
#   DONE:<copied>:<skipped>:<errors>
_PS_COPY_MTP = r"""
param(
    [string]$SourcePath,
    [string]$DestPath,
    [int]$TimeoutSec = 180
)

$shell   = New-Object -ComObject Shell.Application
$copied  = 0
$skipped = 0
$errors  = 0

function CopyMTPFolder([string]$Src, [string]$Dst) {
    try {
        $srcNs = $shell.Namespace($Src)
        if ($null -eq $srcNs) { return }
        Write-Host "FOLDER_ENTER:$Src"
        [Console]::Out.Flush()

        if (-not (Test-Path $Dst)) {
            New-Item -ItemType Directory -Path $Dst -Force | Out-Null
        }
        $dstNs = $shell.Namespace($Dst)

        foreach ($item in $srcNs.Items()) {
            if ($item.IsFolder) {
                CopyMTPFolder $item.Path (Join-Path $Dst $item.Name)
            } else {
                $destFile = Join-Path $Dst $item.Name
                if (Test-Path $destFile) {
                    $script:skipped++
                    Write-Host "FILE_SKIP:$($item.Name)"
                    [Console]::Out.Flush()
                    continue
                }

                # Trigger the shell copy (async)
                $dstNs.CopyHere($item, 20)   # 4 (no progress UI) + 16 (yes to all)

                # Poll until the file appears or we time out
                $ms    = 0
                $limit = $TimeoutSec * 10
                while (-not (Test-Path $destFile) -and $ms -lt $limit) {
                    Start-Sleep -Milliseconds 100
                    $ms++
                }

                # For large files, wait an extra moment for size to stabilise
                if (Test-Path $destFile) {
                    $prevSize = -1
                    $stable   = 0
                    while ($stable -lt 3 -and $ms -lt $limit) {
                        $curSize = (Get-Item $destFile).Length
                        if ($curSize -eq $prevSize) { $stable++ } else { $stable = 0 }
                        $prevSize = $curSize
                        Start-Sleep -Milliseconds 200
                        $ms += 2
                    }
                }

                if (Test-Path $destFile) {
                    $script:copied++
                    Write-Host "FILE_OK:$destFile"
                } else {
                    $script:errors++
                    Write-Host "FILE_TIMEOUT:$($item.Name)"
                }
                [Console]::Out.Flush()
            }
        }
    } catch {
        $script:errors++
        Write-Host "ERR:$Src`:$_"
        [Console]::Out.Flush()
    }
}

CopyMTPFolder $SourcePath $DestPath
Write-Host "DONE:$script:copied`:$script:skipped`:$script:errors"
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

    # ── Detection ─────────────────────────────────────────────────────────

    def detect_phones(self) -> List[PhoneDevice]:
        """
        Return all detected phones, sorted alphabetically by display name.

        Tries MTP (Windows Shell namespace) first, then removable drives
        that look like phones.
        """
        devices: List[PhoneDevice] = []
        seen: set[str] = set()

        for d in (*self._detect_mtp_devices(), *self._detect_drive_phones()):
            key = d.name.lower().strip()
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

            # Find the actual storage root (e.g. "Internal storage") inside
            # the device namespace so we copy files, not just the device node.
            storage_path = self._find_storage_root(path) or path

            devices.append(PhoneDevice(
                name=name,
                access_type="mtp",
                shell_path=storage_path,
                friendly_name=name,
            ))

        return devices

    def _find_storage_root(self, device_shell_path: str) -> Optional[str]:
        """
        Look for 'Internal storage', 'Phone', 'Tablet' etc. one level
        inside the device node.  Returns the shell path of the first match,
        or None if none found (caller falls back to the device root itself).
        """
        raw = self._run_ps_file(
            _PS_LIST_SUBFOLDERS,
            {"ParentPath": device_shell_path},
            timeout=10,
        )
        if not raw or raw == "[]":
            return None

        try:
            folders = json.loads(raw)
            if isinstance(folders, dict):
                folders = [folders]
        except (json.JSONDecodeError, TypeError):
            return None

        preferred = {"internal storage", "phone", "tablet", "sdcard", "sd card"}
        for folder in folders:
            if folder.get("Name", "").lower() in preferred:
                return folder.get("Path", "")

        # If nothing named as expected, return the first folder (likely storage)
        if folders:
            return folders[0].get("Path", "")

        return None

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
        Full backup of *device*.

        Stages
        ------
        1. Copy raw files from phone → ``snapshot/_raw/``
        2. PhoneFileOrganizer sorts them into category folders
        3. ``latest/`` is refreshed with the new organised files

        Returns a result dict with counts, paths, and any errors.
        """
        stamp      = datetime.now().strftime("%Y-%m-%d_%H%M%S")
        safe_name  = device.safe_folder_name
        device_dir = self._phones_dir / safe_name          # e.g. Phones/Galaxy_S24/
        snapshot   = device_dir / stamp                    # e.g. Phones/Galaxy_S24/2026-05-30_143022/
        latest_dir = device_dir / "latest"
        raw_dir    = snapshot / "_raw"

        snapshot.mkdir(parents=True, exist_ok=True)
        raw_dir.mkdir(parents=True, exist_ok=True)

        result: Dict[str, Any] = {
            "device":        device.display_name,
            "device_folder": str(device_dir),
            "snapshot_dir":  str(snapshot),
            "latest_dir":    str(latest_dir),
            "started_at":    datetime.now().isoformat(),
            "completed_at":  None,
            "cancelled":     False,
            "stage":         "copy",
            "copy_result":   None,
            "organize_result": None,
            "errors":        [],
        }

        if should_stop and should_stop():
            result.update(cancelled=True, completed_at=datetime.now().isoformat())
            return result

        # ── Stage 1: Copy ──────────────────────────────────────────────────
        def _half_progress(p: int, m: str) -> None:
            if progress_callback:
                progress_callback(min(int(p * 0.50), 49), m)

        if progress_callback:
            progress_callback(0, f"Connecting to {device.display_name}…")

        if device.access_type == "mtp":
            copy_result = self._copy_mtp(
                shell_path=device.shell_path,
                dest=str(raw_dir),
                progress_callback=_half_progress,
                should_stop=should_stop,
            )
        else:
            copy_result = self._copy_drive(
                source=device.drive_root,
                dest=str(raw_dir),
                progress_callback=_half_progress,
                should_stop=should_stop,
            )

        result["copy_result"] = copy_result
        if copy_result.get("errors"):
            result["errors"].extend(copy_result["errors"])

        if copy_result.get("cancelled") or (should_stop and should_stop()):
            result.update(cancelled=True, stage="copy",
                          completed_at=datetime.now().isoformat())
            return result

        # ── Stage 2: Organise ──────────────────────────────────────────────
        result["stage"] = "organize"
        if progress_callback:
            progress_callback(50, "Organising files into categories…")

        def _second_half(p: int, m: str) -> None:
            if progress_callback:
                progress_callback(50 + min(int(p * 0.40), 39), m)

        # PhoneFileOrganizer places output at:
        #   backup_root / device_name / "latest" / {Photos, Videos, …}
        # We point it at device_dir so output lands in:
        #   device_dir / stamp / "latest" / {Photos, …}
        organizer = PhoneFileOrganizer(backup_root=str(snapshot))
        org_result = organizer.organize_existing_backup(
            backup_path=str(raw_dir),
            device_name="sorted",      # → snapshot/sorted/latest/{Photos,…}
            move_files=True,
            progress_callback=_second_half,
            should_stop=should_stop,
        )
        result["organize_result"] = org_result

        # Flatten: move snapshot/sorted/latest/* up to snapshot/
        _collapse_sorted_dir(snapshot)

        # Remove empty _raw dir
        try:
            if raw_dir.exists() and not any(raw_dir.iterdir()):
                raw_dir.rmdir()
        except OSError:
            pass

        if org_result.get("cancelled") or (should_stop and should_stop()):
            result.update(cancelled=True, stage="organize",
                          completed_at=datetime.now().isoformat())
            return result

        # ── Stage 3: Refresh latest/ ───────────────────────────────────────
        result["stage"] = "finalize"
        if progress_callback:
            progress_callback(92, "Updating latest/ folder…")

        _refresh_latest(snapshot, latest_dir)

        result.update(stage="complete", completed_at=datetime.now().isoformat())
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
        using a PowerShell script that polls each file until it's stable.
        """
        result: Dict[str, Any] = {
            "copied": 0, "skipped": 0,
            "errors": [], "cancelled": False,
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
                "-TimeoutSec", "180",
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

            while True:
                if should_stop and should_stop():
                    proc.terminate()
                    result["cancelled"] = True
                    break

                line = stdout.readline()
                if not line:
                    if proc.poll() is not None:
                        break
                    continue


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
        shutil.  Skips Windows system directories.
        """
        result: Dict[str, Any] = {
            "copied": 0, "skipped": 0,
            "errors": [], "cancelled": False,
        }

        _SKIP_DIRS = {
            "windows", "system volume information", "$recycle.bin",
            "program files", "program files (x86)", "programdata",
        }

        dest_path = Path(dest)
        dest_path.mkdir(parents=True, exist_ok=True)

        # Collect file list first so we can report progress
        all_files: List[Path] = []
        for root, dirs, files in os.walk(source):
            dirs[:] = [
                d for d in dirs
                if d.lower() not in _SKIP_DIRS and not d.startswith(".")
            ]
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
                    result["skipped"] += 1
                else:
                    shutil.copy2(str(src_file), str(dst_file))
                    result["copied"] += 1

            except PermissionError:
                pass   # skip locked files silently
            except Exception as exc:
                result["errors"].append(f"{src_file.name}: {exc}")

        return result

    # ── History & listing ─────────────────────────────────────────────────

    def list_all_backed_up_devices(self) -> List[Dict[str, Any]]:
        """
        Return every device that has at least one snapshot, sorted A→Z.
        """
        if not self._phones_dir.exists():
            return []

        out = []
        for d in sorted(self._phones_dir.iterdir(), key=lambda p: p.name.lower()):
            if not d.is_dir():
                continue
            snapshots = self._list_snapshots(d)
            out.append({
                "name":           d.name.replace("_", " "),
                "folder":         str(d),
                "snapshot_count": len(snapshots),
                "has_latest":     (d / "latest").exists(),
                "latest_path":    str(d / "latest") if (d / "latest").exists() else None,
                "total_size":     _dir_size(d),
                "last_backup":    snapshots[0] if snapshots else None,
            })
        return out

    def list_device_snapshots(self, display_name: str) -> List[str]:
        """Return snapshot folder names for *display_name*, newest first."""
        safe  = _safe_folder_name(display_name)
        ddir  = self._phones_dir / safe
        return self._list_snapshots(ddir)

    @staticmethod
    def _list_snapshots(device_dir: Path) -> List[str]:
        if not device_dir.exists():
            return []
        entries = [
            p.name for p in device_dir.iterdir()
            if p.is_dir() and p.name != "latest"
        ]
        return sorted(entries, reverse=True)   # newest first

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

    @staticmethod
    def _run_ps_file(
        script: str,
        params: Dict[str, str],
        timeout: int = 30,
    ) -> str:
        """Write *script* to a temp .ps1 file, run it, return stdout."""
        ps_path = _write_temp_script(script)
        try:
            cmd = ["powershell", "-NoProfile", "-NonInteractive",
                   "-File", ps_path]
            for k, v in params.items():
                cmd += [f"-{k}", v]
            r = subprocess.run(
                cmd, capture_output=True, text=True, timeout=timeout,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            return r.stdout.strip()
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return ""
        finally:
            try:
                os.unlink(ps_path)
            except OSError:
                pass


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


def _collapse_sorted_dir(snapshot_dir: Path) -> None:
    """
    PhoneFileOrganizer places output at snapshot/sorted/latest/{Photos,…}.
    Move those category folders up to snapshot/{Photos,…} so the snapshot
    directory is clean.
    """
    sorted_latest = snapshot_dir / "sorted" / "latest"
    if not sorted_latest.exists():
        return

    for child in sorted_latest.iterdir():
        dst = snapshot_dir / child.name
        if dst.exists():
            shutil.rmtree(str(dst), ignore_errors=True)
        shutil.move(str(child), str(dst))

    # Clean up the now-empty scaffolding
    sorted_dir = snapshot_dir / "sorted"
    shutil.rmtree(str(sorted_dir), ignore_errors=True)


def _refresh_latest(snapshot_dir: Path, latest_dir: Path) -> None:
    """Replace latest/ with a fresh copy of the current snapshot."""
    if latest_dir.exists():
        shutil.rmtree(str(latest_dir), ignore_errors=True)
    try:
        shutil.copytree(str(snapshot_dir), str(latest_dir))
    except Exception:
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