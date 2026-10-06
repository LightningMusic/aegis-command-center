"""
core/phone_backup_manager.py
============================
Detects phones connected via USB (MTP / file-transfer mode or USB mass-storage),
backs up every accessible file into one persistent per-device latest/ tree while
preserving the phone folder structure. A manifest tracks known files so repeat
backups are fast, incremental, and resumable without supervision.

Folder layout produced
----------------------
{backup_root}/
  Phones/
    Galaxy_S24/
      latest/
        DCIM/
        Pictures/
        Android/
        ...
      backup_manifest.json  - per-file index: size, modified date, original
                              relative path, destination path, status, and
                              failure count. Consulted before copy so unchanged
                              files are never re-transferred, then refreshed
                              from latest/ after each run.
      backup_log.json       - small history of each run (timestamps, counts)
    Pixel_8_Pro/
      ...

Self-reliance──────────────
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
import hashlib
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

from core import threat_scan
from core.copy_engine import VerifiedCopyError, verified_copy

ProgressCallback = Callable[[int, str], None]
StopCallback = Callable[[], bool]

# How long to wait for a single file to first appear in the destination after
# CopyHere is issued. Once it appears and is still growing, the script keeps
# waiting well past this (see MTP_FILE_MAX_SECONDS) — big videos are fine.
MTP_FILE_TIMEOUT_SECONDS = 90

# Absolute ceiling for one file, even while it is still growing. Guards against
# a genuinely wedged transfer without punishing large media files. Kept well
# under the whole-run stall timeout so a single slow file can never itself be
# the reason the run looks frozen for most of an hour — if it doesn't make it
# in this window, move on to the rest of the phone; the manifest means a
# later pass picks it back up rather than losing it.
MTP_FILE_MAX_SECONDS = 8 * 60

# The phone can go quiet for a while between bursts (folder enumeration, the
# provider staging a large file, the screen locking briefly). Warn softly first,
# only treat it as a real disconnect after the hard timeout.
#
# Folder *listing* (not file copying) is the main reason a perfectly healthy
# phone goes quiet for a long time: Shell.Application's Folder.Items() call
# for an MTP folder is a single call that returns nothing at all until the
# whole folder is enumerated, and on-device this is well known to take
# several minutes for a folder with thousands of files (a big camera roll or
# a WhatsApp media folder, say) — Explorer itself is just as slow browsing
# the same folder. There is no progress to report during that one call, so
# the stall ceiling has to be generous enough to outlast it rather than
# reading normal-but-slow enumeration as a real disconnect.
MTP_SOFT_STALL_SECONDS = 75
MTP_STALL_TIMEOUT_SECONDS = 480

# A file that fails this many cumulative times gets marked "skip" in the
# manifest so future attempts stop wasting time retrying a dead file.
MAX_FILE_FAIL_ATTEMPTS = 5

# Defaults for the unattended retry loop.
DEFAULT_MAX_RETRY_ATTEMPTS = 200
DEFAULT_MAX_TOTAL_RUNTIME_SECONDS = 8 * 60 * 60  # 8 hours — safe for overnight

_DUPLICATE_SUFFIX_RE = re.compile(r"^(?P<base>.*) \((?P<num>\d+)\)$")

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
#   FILE_OK_UNSIZED:<local_dest_path>      (copied, but the phone never reported a size to verify against)
#   FILE_SKIP:<filename>                  (already in latest/ this run)
#   FILE_SKIP_MANIFEST:<relkey>            (manifest says unchanged/permanently-skip)
#   FILE_SKIP_ELSEWHERE:<relkey>           (same name+size already confirmed backed up under a different path)
#   FILE_SKIP_PARTIAL:<relkey>             (half-finished download, e.g. .crdownload/.part)
#   FILE_TIMEOUT:<relkey>
#   FILE_ERR:<relkey>:<message>
#   FOLDER_ENTER:<shell_path>
#   FENTER_KEY:<relkey>                    (machine-readable companion to FOLDER_ENTER)
#   FOLDER_SKIP:<shell_path>
#   FOLDER_SIG:<relkey>:<count>:<hash>     (folder's contents fingerprint, for next run's fast-skip)
#   STATS:<copied>:<skipped>:<errors>      (heartbeat, every ~5s)
#   ERR:<shell_path>:<message>             (folder/source-level, not file-specific)
#   DONE:<copied>:<skipped>:<errors>
_PS_COPY_MTP = r"""
param(
    [string]$SourcePath,
    [string]$DestPath,
    [int]$TimeoutSec = 90,
    [int]$MaxFileSec = 3600,
    [string]$ManifestPath = "",
    [string]$SkipFoldersPath = "",
    [string]$FolderSigPath = "",
    [string]$NameSizeIndexPath = "",
    [switch]$ForceVerify
)

$ErrorActionPreference = 'Continue'

$shell     = New-Object -ComObject Shell.Application
$copied    = 0
$skipped   = 0
$errors    = 0
$visited   = 0
$lastBeat  = Get-Date

# FOF_SILENT(4) | FOF_NOCONFIRMATION(16) | FOF_NOCONFIRMMKDIR(512) |
# FOF_NOERRORUI(1024) | FOF_RENAMEONCOLLISION(8). The rename flag is belt
# and suspenders on top of the unique-per-attempt staging folders below: if
# a name collision ever happens anyway, tell the shell to silently rename
# rather than prompt, instead of relying on FOF_NOCONFIRMATION alone - which
# WPD/MTP sources are known to not fully honour for the overwrite prompt.
$copyFlags = 1564

$ALWAYS_SKIP = @('.thumbnails', '.trashed', '.cache', '.thumbcache', 'lost.dir')

# Half-finished downloads: the bytes are incomplete by definition, and the
# browser/torrent client will rename them when (if) they finish, so backing
# them up stores a broken file under a name that will never be used again.
# Deliberately NOT filtering ".trashed-" files: those are real files Android
# has only soft-deleted, and having them in the backup is the point.
$PARTIAL_DOWNLOAD_EXTS = @('.crdownload', '.part', '.partial', '.download', '.aria2', '.!ut', '.opdownload')

function IsPartialDownload([string]$Name) {
    $lower = $Name.ToLower()
    foreach ($ext in $PARTIAL_DOWNLOAD_EXTS) {
        if ($lower.EndsWith($ext)) { return $true }
    }
    return $false
}

# Shell.Application mounts archives as browsable folders. A .zip reports
# IsFolder = $true, Type = "Compressed (zipped) Folder", and GetFolder()
# succeeds -- so a walker that trusts IsFolder recurses INTO the archive,
# creates a real directory named after it, tries to pull each entry out
# one at a time, and never backs up the archive itself as a file.
#
# That is how a 463 MB SpotiDownloader zip full of MP3s ended up on the backup
# drive as a directory holding a handful of extracted tracks and several empty
# folders named after songs: zip entry names that could not be created on
# Windows (trailing spaces, which Android and zip allow and Windows strips).
#
# An archive is a file. Copy it as one and its contents travel intact.
$ARCHIVE_SHELL_FOLDER_EXTS = @('.zip', '.cab')

function IsArchiveMountedAsFolder([string]$Name) {
    if (-not $Name) { return $false }
    $ext = [System.IO.Path]::GetExtension($Name)
    if (-not $ext) { return $false }
    return $ARCHIVE_SHELL_FOLDER_EXTS -contains $ext.ToLower()
}

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

# Folders that repeatedly timed out listing on a previous attempt within
# this same backup_phone_until_complete() run - skip them so one folder
# WPD can't enumerate doesn't block the rest of the phone forever.
$skipFolderSet = @{}
if ($SkipFoldersPath -and (Test-Path $SkipFoldersPath)) {
    try {
        $raw = Get-Content $SkipFoldersPath -Raw -ErrorAction Stop
        if ($raw) {
            $parsed = $raw | ConvertFrom-Json -ErrorAction Stop
            foreach ($k in @($parsed)) {
                if ($k) { $skipFolderSet[[string]$k] = $true }
            }
        }
    } catch {
        $skipFolderSet = @{}
    }
}

# Per-folder fingerprint from the last run that saw it: total item count +
# a hash of every item's name. If a folder's current listing matches its
# cached fingerprint exactly, nothing was added, removed, or renamed in it
# since last time, so the expensive per-file Size/Date lookups below can be
# skipped entirely for files already confirmed in the manifest -- that COM
# round-trip per file is the real cost on a big unchanged folder, not the
# listing itself. Accepted tradeoff: a file replaced by a same-named,
# same-count substitute would be missed until a "Verify everything" pass.
#
# The phone's root folder has relKey "" (empty string). ConvertFrom-Json on
# this PowerShell version throws on a JSON object with an empty-string
# property name ("only supported using -AsHashTable", which this version
# doesn't have) -- so "" can never appear as an actual key in the JSON this
# reads or writes. A non-empty sentinel stands in for the root everywhere a
# folder-signature key is looked up or emitted.
$ROOT_SIG_KEY = "__ROOT__"
$folderSigLookup = @{}
if ($FolderSigPath -and (Test-Path $FolderSigPath)) {
    try {
        $raw = Get-Content $FolderSigPath -Raw -ErrorAction Stop
        if ($raw) {
            $parsed = $raw | ConvertFrom-Json -ErrorAction Stop
            if ($parsed) {
                foreach ($prop in $parsed.PSObject.Properties) {
                    $folderSigLookup[$prop.Name] = $prop.Value
                }
            }
        }
    } catch {
        $folderSigLookup = @{}
    }
}

# "<lowercase filename>|<size>" -> the backup-relative path that already
# holds those exact bytes, for every file confirmed backed up anywhere on the
# phone. When the phone presents the same file at a new path, this says where
# a copy already sits so it can be duplicated locally off the backup drive
# instead of pulled down the MTP wire again.
#
# It maps to a PATH rather than just $true because knowing only "we have this
# somewhere" is not enough to act on: the previous version skipped the file
# outright, which left the new path permanently empty and recorded nothing,
# so every later run reached the same conclusion and skipped it again.
$nameSizeIndex = @{}
if ($NameSizeIndexPath -and (Test-Path $NameSizeIndexPath)) {
    try {
        $raw = Get-Content $NameSizeIndexPath -Raw -ErrorAction Stop
        if ($raw) {
            $parsed = $raw | ConvertFrom-Json -ErrorAction Stop
            if ($parsed) {
                foreach ($prop in $parsed.PSObject.Properties) {
                    $nameSizeIndex[$prop.Name] = [string]$prop.Value
                }
            }
        }
    } catch {
        $nameSizeIndex = @{}
    }
}

function Get-FolderSignature([string[]]$Names) {
    # Deterministic across separate process launches (unlike .GetHashCode(),
    # which .NET randomizes per-process for strings) so it's comparable
    # between this run and a cached value written by a previous one.
    $sorted = ($Names | Sort-Object) -join "`n"
    $md5 = [System.Security.Cryptography.MD5]::Create()
    $bytes = [System.Text.Encoding]::UTF8.GetBytes($sorted)
    $hashBytes = $md5.ComputeHash($bytes)
    return [System.BitConverter]::ToString($hashBytes).Replace('-', '')
}

# Folders likely to receive new content (camera roll, screenshots,
# downloads, messaging media) are visited before everything else at each
# level of the tree, so newly added files get backed up early in a run
# instead of waiting behind folders that rarely change. Purely an ordering
# change -- never skips anything, never changes what gets backed up.
$PRIORITY_FOLDER_PATTERNS = @(
    'camera', 'dcim', 'screenshot', 'download', 'whatsapp images',
    'whatsapp video', 'whatsapp', 'pictures', 'movies', 'video', 'recent'
)

function Get-PriorityOrderedItems($ItemList) {
    $priority = @()
    $rest = @()
    foreach ($it in $ItemList) {
        $isPriority = $false
        if ($it.IsFolder) {
            $nameLower = $it.Name.ToLower()
            foreach ($pat in $PRIORITY_FOLDER_PATTERNS) {
                if ($nameLower -like "*$pat*") { $isPriority = $true; break }
            }
        }
        if ($isPriority) { $priority += $it } else { $rest += $it }
    }
    return @($priority) + @($rest)
}

function Get-ItemFileName($Item) {
    # $Item.Name is the DISPLAY name. With Explorer's "hide extensions for
    # known file types" enabled -- the Windows default -- it comes back
    # WITHOUT the extension ("IMG_1" for IMG_1.jpg), and that applies to the
    # WPD/MTP namespace too, not just local folders.
    #
    # Using it for filenames broke this script in four compounding ways:
    #   - relKey lost its extension, so it never matched the manifest (built
    #     from the real filenames on disk) and EVERY file looked brand new on
    #     EVERY run -- the "it isn't indexing anything" symptom;
    #   - $destFile lost its extension, so Wait-ForFile watched a path
    #     CopyHere was never going to create, and every transfer was reported
    #     as failed after burning its full timeout;
    #   - meanwhile CopyHere quietly wrote the file under its REAL name, so
    #     the backup filled up with files the script didn't believe existed;
    #   - which meant the next run hit a genuine name collision and raised
    #     the Windows overwrite dialog.
    #
    # System.FileName is the actual filename including extension. Fall back
    # through the other name properties, then to the display name with the
    # extension re-attached, so a provider that exposes none of them still
    # degrades to something sane.
    foreach ($prop in @("System.FileName", "System.ItemName", "System.ItemNameDisplay")) {
        try {
            $candidate = [string]$Item.ExtendedProperty($prop)
            if ($candidate -and $candidate.Trim()) { return $candidate }
        } catch {}
    }

    $name = [string]$Item.Name
    try {
        $ext = [string]$Item.ExtendedProperty("System.FileExtension")
        if ($ext -and -not $name.EndsWith($ext, [System.StringComparison]::OrdinalIgnoreCase)) {
            $name = "$name$ext"
        }
    } catch {}
    return $name
}

function Get-ItemSizeBestEffort($Item) {
    # WPD/MTP shell namespace extensions are notoriously inconsistent about
    # exposing .Size - for many Android phones it's always $null. Try the
    # extended-property path first (goes through IPropertyStore, which MTP
    # providers implement far more reliably than the legacy .Size getter),
    # then fall back to .Size. Returns $null if genuinely unavailable -
    # callers must treat that as "unknown", never as "changed".
    try {
        $extSize = $Item.ExtendedProperty("System.Size")
        if ($null -ne $extSize -and $extSize -gt 0) { return [int64]$extSize }
    } catch {}

    try {
        if ($Item.Size -and $Item.Size -gt 0) { return [int64]$Item.Size }
    } catch {}

    return $null
}

function Get-ItemModifiedBestEffort($Folder, $Item) {
    # Second signal for "has this file changed", used when Size can't tell
    # us (the common case for this device). $Folder.GetDetailsOf($Item, 3)
    # returns the item's modified-date column as localized display text -
    # strip the non-ASCII bits some locales inject (e.g. RTL marks) before
    # parsing. Returns $null if unavailable/unparsable.
    try {
        $dateStr = $Folder.GetDetailsOf($Item, 3)
        if ($dateStr) {
            $dateStr = ($dateStr -replace '[^\x20-\x7E]', '').Trim()
            if ($dateStr) { return [datetime]::Parse($dateStr) }
        }
    } catch {}
    return $null
}

function ShouldSkipItem([string]$ParentLabel, [string]$Name) {
    $lower = $Name.ToLower()
    if ($ALWAYS_SKIP -contains $lower) { return $true }
    if ($lower -eq 'android' -or $lower.StartsWith('.')) { return $true }
    return $false
}

function Heartbeat {
    $now = Get-Date
    if (($now - $script:lastBeat).TotalSeconds -ge 4) {
        Write-Host "STATS:$($script:copied):$($script:skipped):$($script:errors)"
        [Console]::Out.Flush()
        $script:lastBeat = $now
    }
}

function Beat([string]$Msg) {
    # Unconditional keep-alive with a note - used around calls that can block
    # (folder enumeration, waiting on a large file) so the host never mistakes
    # a slow-but-alive transfer for a disconnected phone.
    Write-Host "PING:$Msg"
    [Console]::Out.Flush()
    $script:lastBeat = Get-Date
}

function EnsureFolder([string]$Path) {
    if (-not (Test-Path $Path)) {
        New-Item -ItemType Directory -Path $Path -Force | Out-Null
    }
    return $shell.Namespace($Path)
}

function Wait-ForFile([string]$DestFile, [string]$RelKey, [int64]$ExpectedSize) {
    # Wait for CopyHere to materialise $DestFile. Once it appears, keep waiting
    # as long as it is still growing - only give up if it is genuinely stuck
    # (no size change for a while) or the absolute ceiling is hit. Emits a
    # progress line on every size change so the host's stall timer resets.
    $started   = Get-Date
    $appeared  = $false
    $lastSize  = -1
    $idleMs    = 0

    while ($true) {
        $elapsed = ((Get-Date) - $started).TotalSeconds
        $exists  = Test-Path $DestFile

        if (-not $exists) {
            if ($elapsed -ge $TimeoutSec) { return $false }
            Start-Sleep -Milliseconds 250
            if ((((Get-Date) - $script:lastBeat).TotalSeconds) -ge 3) { Beat "waiting for $RelKey" }
            continue
        }

        if (-not $appeared) { $appeared = $true }

        $size = -1
        try { $size = [int64](Get-Item -LiteralPath $DestFile -ErrorAction Stop).Length } catch { $size = -1 }

        if ($size -gt $lastSize) {
            $lastSize = $size
            $idleMs = 0
            Write-Host "COPY_PROGRESS:$($RelKey):$size"
            [Console]::Out.Flush()
            $script:lastBeat = Get-Date
        } else {
            $idleMs += 300
            if ((((Get-Date) - $script:lastBeat).TotalSeconds) -ge 3) { Beat "copying $RelKey ($lastSize bytes)" }
        }

        # Considered done: size known and reached, or size unknown but stable
        # for ~4.5s. Large files that are still flowing keep resetting $idleMs.
        if ($ExpectedSize -gt 0 -and $lastSize -ge $ExpectedSize) { return $true }
        if ($idleMs -ge 4500) { return $true }
        if ($elapsed -ge $MaxFileSec) {
            # Ran out of time. If it had actually gone quiet, that's a real
            # completion (or a stall) either way, safe to accept. But if
            # bytes were STILL actively arriving when the ceiling hit and we
            # don't know the true target size to confirm against, accepting
            # it as "done" would mean trusting an arbitrarily truncated file
            # as a good backup with no way to tell. Treat that case as a
            # timeout instead, same as "never appeared" - the caller cleans
            # up the partial and retries in a fresh attempt rather than
            # recording a partial file as verified.
            if ($idleMs -ge 1000 -or $ExpectedSize -gt 0) { return $true }
            return $false
        }

        Start-Sleep -Milliseconds 300
    }
}

function CopyMTPFolderObject($Folder, [string]$Label, [string]$RelLabel, [string]$Dst) {
    try {
        if ($null -eq $Folder) {
            $script:errors++
            Write-Host "ERR:$($Label):Unable to open MTP folder"
            [Console]::Out.Flush()
            return
        }

        if ($RelLabel -and $skipFolderSet.ContainsKey($RelLabel)) {
            Write-Host "FOLDER_SKIP:$($Label) (skipped - timed out repeatedly on a previous attempt)"
            [Console]::Out.Flush()
            return
        }

        $script:visited++
        Write-Host "FOLDER_ENTER:$($Label)"
        Write-Host "FENTER_KEY:$RelLabel"
        [Console]::Out.Flush()

        $dstNs = EnsureFolder $Dst
        if ($null -eq $dstNs) {
            $script:errors++
            Write-Host "ERR:$($Label):Unable to open destination folder $Dst"
            [Console]::Out.Flush()
            return
        }

        Beat "listing $Label"
        $itemList = $null
        try { $itemList = @($Folder.Items()) } catch {
            $script:errors++
            Write-Host "ERR:$($Label):Unable to list folder: $_"
            [Console]::Out.Flush()
            return
        }
        Beat "$Label has $($itemList.Count) entries"

        $currentCount = $itemList.Count
        $currentSig = Get-FolderSignature (@($itemList | ForEach-Object { Get-ItemFileName $_ }))
        $sigKey = if ($RelLabel) { $RelLabel } else { $ROOT_SIG_KEY }
        $folderUnchanged = $false
        if (-not $ForceVerify -and $folderSigLookup.ContainsKey($sigKey)) {
            $cached = $folderSigLookup[$sigKey]
            $cachedCount = $null
            try { $cachedCount = [int]$cached.count } catch { $cachedCount = $null }
            $cachedSig = $null
            try { $cachedSig = [string]$cached.sig } catch { $cachedSig = $null }
            if ($null -ne $cachedCount -and $cachedCount -eq $currentCount -and $cachedSig -eq $currentSig) {
                $folderUnchanged = $true
            }
        }

        foreach ($item in (Get-PriorityOrderedItems $itemList)) {
            Heartbeat
            $itemName = Get-ItemFileName $item
            $safeName = $itemName -replace '[\\/:*?"<>|]', '_'
            $relKey = if ($RelLabel) { "$RelLabel\$safeName" } else { $safeName }

            # An archive only LOOKS like a folder to the shell (see
            # IsArchiveMountedAsFolder) -- treat it as the file it is.
            if ($item.IsFolder -and -not (IsArchiveMountedAsFolder $itemName)) {
                if (ShouldSkipItem $Label $itemName) {
                    Write-Host "FOLDER_SKIP:$($Label)\$($safeName)"
                    [Console]::Out.Flush()
                    continue
                }
                $childFolder = $null
                try { $childFolder = $item.GetFolder() } catch { $childFolder = $null }
                if ($null -eq $childFolder) {
                    $script:errors++
                    Write-Host "ERR:$($Label)\$($safeName):Could not open subfolder"
                    [Console]::Out.Flush()
                    continue
                }
                CopyMTPFolderObject $childFolder "$Label\$safeName" $relKey (Join-Path $Dst $safeName)
                continue
            }

            if (IsPartialDownload $itemName) {
                $script:skipped++
                Write-Host "FILE_SKIP_PARTIAL:$relKey"
                [Console]::Out.Flush()
                continue
            }

            # Fast path: this folder's whole contents (names + count) match
            # exactly what was here last time we looked, so nothing in it
            # was added, removed, or renamed. Skip the per-file Size/Date
            # COM round-trips entirely for anything already confirmed backed
            # up - that per-file cost is what actually makes re-walking a
            # big unchanged folder slow, not the listing itself.
            if ($folderUnchanged -and $manifestLookup.ContainsKey($relKey)) {
                $script:skipped++
                Write-Host "FILE_SKIP_MANIFEST:$relKey"
                [Console]::Out.Flush()
                continue
            }

            # Consult the manifest BEFORE touching the USB/MTP layer at all.
            # Safe-by-default: only re-copy if we have POSITIVE evidence the
            # file changed. Size is the first signal, but this device's MTP
            # provider frequently can't report it at all - when that happens,
            # fall back to comparing modified dates (a real, independently
            # fetched signal) instead of immediately trusting an old record.
            # Only when NEITHER signal is available at all do we fall back to
            # trusting the manifest outright, and -ForceVerify turns even
            # that off - nothing gets skipped on trust alone, so a genuinely
            # unchanged phone still finishes fast, but nothing gets skipped
            # just because we couldn't tell.
            if ($manifestLookup.ContainsKey($relKey)) {
                $entry = $manifestLookup[$relKey]
                $isPermSkip = $false
                try { $isPermSkip = [bool]$entry.skip } catch { $isPermSkip = $false }
                $isExcluded = $false
                try { $isExcluded = [bool]$entry.excluded } catch { $isExcluded = $false }

                # An explicit exclusion outranks -ForceVerify. Verifying
                # everything means not trusting the record of what was COPIED;
                # it does not mean overriding a decision not to copy something.
                # Re-fetching here would silently undo a deletion the user asked
                # for, and would do it on every run.
                if ($isExcluded) {
                    $script:skipped++
                    Write-Host "FILE_SKIP_EXCLUDED:$relKey"
                    [Console]::Out.Flush()
                    continue
                }

                if ($isPermSkip -and -not $ForceVerify) {
                    $script:skipped++
                    Write-Host "FILE_SKIP_MANIFEST:$relKey"
                    [Console]::Out.Flush()
                    continue
                }

                $knownSize = $null
                try { $knownSize = [int64]$entry.size } catch { $knownSize = $null }
                $liveSize = Get-ItemSizeBestEffort $item

                $matches = $false
                $checkedSomething = $false
                if ($null -ne $liveSize -and $null -ne $knownSize) {
                    $checkedSomething = $true
                    $matches = ($liveSize -eq $knownSize)
                } else {
                    # Size couldn't settle it (one or both sides unknown) -
                    # try modified date as an independent second signal.
                    $knownModified = $null
                    try { $knownModified = [datetime]$entry.modified } catch { $knownModified = $null }
                    $liveModified = Get-ItemModifiedBestEffort $Folder $item

                    if ($null -ne $knownModified -and $null -ne $liveModified) {
                        $checkedSomething = $true
                        # MTP date precision can be coarse - allow slack.
                        $deltaSec = [Math]::Abs(($liveModified - $knownModified).TotalSeconds)
                        $matches = ($deltaSec -lt 120)
                    }
                }

                if ($checkedSomething) {
                    if ($matches) {
                        $script:skipped++
                        Write-Host "FILE_SKIP_MANIFEST:$relKey"
                        [Console]::Out.Flush()
                        continue
                    }
                    # else: a real signal disagreed - fall through and re-copy.
                } elseif (-not $ForceVerify) {
                    # Neither size nor date could be read from the device at
                    # all - nothing to actually check against. Trust the
                    # manifest rather than re-downloading the whole phone
                    # every run, unless the user explicitly asked to verify
                    # everything regardless of cost.
                    $script:skipped++
                    Write-Host "FILE_SKIP_MANIFEST:$relKey"
                    [Console]::Out.Flush()
                    continue
                }
                # else ($ForceVerify and nothing could be checked): fall
                # through and re-copy rather than guess.
            }

            $ownPathKnown = $manifestLookup.ContainsKey($relKey)

            $expectedSize = Get-ItemSizeBestEffort $item
            if ($null -eq $expectedSize) { $expectedSize = 0 }

            $destFile = Join-Path $Dst $safeName

            if (-not $ownPathKnown -and -not $ForceVerify -and $expectedSize -gt 0) {
                # Never seen at this exact path -- but the same name AND size is
                # already confirmed backed up elsewhere on the phone, so the
                # bytes are already on the backup drive.
                #
                # This used to just skip, which was wrong in both directions.
                # If Android had MOVED the file, the backup kept it at the old
                # path and never populated the new one, so the backup stopped
                # mirroring the phone. If the user genuinely had the same file
                # in two places -- the same song in Music and on the SD card --
                # only one ever got stored, and the other folder stayed empty.
                # Either way nothing was written to the manifest, so the next
                # run reached the identical conclusion and skipped it again,
                # for ever, while reporting the phone fully backed up.
                #
                # Copying it locally off the backup drive gets the structure
                # right for a fraction of the cost of an MTP transfer, which
                # is what made skipping attractive in the first place.
                $xrefKey = "$($itemName.ToLower())|$expectedSize"
                if ($nameSizeIndex.ContainsKey($xrefKey)) {
                    $twinRel = $nameSizeIndex[$xrefKey]
                    $twinFull = if ($twinRel) { Join-Path $DestPath $twinRel } else { $null }
                    if ($twinFull -and (Test-Path -LiteralPath $twinFull -PathType Leaf)) {
                        $localOk = $false
                        try {
                            $parent = Split-Path -Parent $destFile
                            if ($parent -and -not (Test-Path -LiteralPath $parent)) {
                                New-Item -ItemType Directory -Force -Path $parent | Out-Null
                            }
                            Copy-Item -LiteralPath $twinFull -Destination $destFile -Force -ErrorAction Stop
                            $landed = -1
                            try { $landed = [int64](Get-Item -LiteralPath $destFile -ErrorAction Stop).Length } catch { $landed = -1 }
                            $localOk = ($landed -eq $expectedSize)
                            if (-not $localOk -and (Test-Path -LiteralPath $destFile)) {
                                try { Remove-Item -LiteralPath $destFile -Force -ErrorAction Stop } catch {}
                            }
                        } catch {
                            $localOk = $false
                        }
                        if ($localOk) {
                            $script:copied++
                            Write-Host "FILE_LOCAL_COPY:$destFile"
                            [Console]::Out.Flush()
                            continue
                        }
                        # Local duplication failed -- fall through and fetch it
                        # from the phone rather than leave the path empty.
                    }
                    # No usable local twin (the manifest knew of a copy that is
                    # no longer on disk): fetch from the phone as normal.
                }
            }

            if (Test-Path $destFile) {
                # Already present. Only re-fetch if we can prove it's short.
                $haveSize = -1
                try { $haveSize = [int64](Get-Item -LiteralPath $destFile -ErrorAction Stop).Length } catch { $haveSize = -1 }
                if ($haveSize -eq 0 -and $expectedSize -ne 0) {
                    # An empty destination is not a backup of a file the phone
                    # has. This used to be kept whenever the phone couldn't
                    # report a size either, so a copy that produced nothing sat
                    # there standing in for the real file on every later run.
                    try { Remove-Item -LiteralPath $destFile -Force -ErrorAction Stop } catch {}
                } elseif ($expectedSize -gt 0 -and $haveSize -ge 0 -and $haveSize -lt $expectedSize) {
                    try { Remove-Item -LiteralPath $destFile -Force -ErrorAction Stop } catch {}
                } else {
                    $script:skipped++
                    Write-Host "FILE_SKIP:$safeName"
                    [Console]::Out.Flush()
                    continue
                }
            }

            # Copy straight into the real destination -- and only ONE
            # attempt per file per process, no in-place retry. CopyHere() is
            # fire-and-forget: it can still be finishing asynchronously in
            # the background well after we give up waiting on it. Retrying
            # into the SAME destination path risks a second CopyHere landing
            # while the first one is still writing there -- a collision of
            # our own making, which is exactly what caused the "already
            # exists" dialog before. A file that doesn't land cleanly this
            # pass is simply left for the NEXT attempt instead -- a fresh
            # PowerShell process and a fresh phone session, so there is no
            # possibility of overlapping with whatever this attempt might
            # still be doing. backup_phone_until_complete() already retries
            # the whole phone automatically, so nothing is lost by not
            # retrying in place here -- it's also just faster, since a
            # stubborn file no longer burns several seconds of sleep-and-
            # retry before moving on to everything else that's waiting.
            $copyOk = $false
            try {
                $dstNs.CopyHere($item, $copyFlags)
                if (Wait-ForFile $destFile $relKey $expectedSize) {
                    $finalSize = -1
                    try { $finalSize = [int64](Get-Item -LiteralPath $destFile -ErrorAction Stop).Length } catch { $finalSize = -1 }
                    if ($finalSize -ge 0 -and ($expectedSize -le 0 -or $finalSize -ge $expectedSize)) {
                        $copyOk = $true
                    } elseif (Test-Path $destFile) {
                        # Truncated -- drop it so it isn't recorded as good;
                        # a clean retry happens from scratch next attempt.
                        try { Remove-Item -LiteralPath $destFile -Force -ErrorAction Stop } catch {}
                    }
                } elseif (Test-Path $destFile) {
                    try { Remove-Item -LiteralPath $destFile -Force -ErrorAction Stop } catch {}
                }
            } catch {
                Beat "$relKey didn't land this pass (CopyHere error) -- will retry next attempt"
            }

            if ($copyOk) {
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
                if ($expectedSize -gt 0) {
                    Write-Host "FILE_OK:$destFile"
                } else {
                    # The phone never reported a size for this file, so
                    # reaching "expected size" was never something we could
                    # actually check -- flag it for a real local integrity
                    # check once the transfer's done.
                    Write-Host "FILE_OK_UNSIZED:$destFile"
                }
            } else {
                $script:errors++
                Write-Host "FILE_ERR:$($relKey):didn't complete this pass, will retry next attempt"
            }
            [Console]::Out.Flush()
        }

        Write-Host "FOLDER_SIG:$($sigKey):$($currentCount):$($currentSig)"
        [Console]::Out.Flush()
    } catch {
        $script:errors++
        Write-Host "ERR:$($Label):$_"
        [Console]::Out.Flush()
    }
}

    # Many WPD/MTP phones only allow ONE active session at a time. A File
    # Explorer window left open browsing the phone (very easy to do - e.g.
    # checking on it, or it never got closed from a manual look) silently
    # holds that session, so our own script can see the device fine but
    # can't actually open it for copying - which looks exactly like "the
    # computer can see it but the app can't access it". Close any such
    # windows before we try.
    try {
        foreach ($win in @($shell.Windows())) {
            try {
                $winPath = $win.Document.Folder.Self.Path
            } catch {
                $winPath = $null
            }
            if ($winPath -and ($winPath -eq $SourcePath -or $winPath.StartsWith($SourcePath))) {
                Beat "closing a File Explorer window open on the phone"
                try { $win.Quit() } catch {}
            }
        }
    } catch {}

    # Opening the source can fail transiently right after Explorer releases
    # it (or on a phone that's a little slow to hand over a session) - retry
    # a few times before giving up, instead of a single try/fail per whole
    # PowerShell process.
    $srcNs = $null
    for ($openTry = 1; $openTry -le 4 -and $null -eq $srcNs; $openTry++) {
        $srcNs = $shell.Namespace($SourcePath)
        if ($null -eq $srcNs -and $openTry -lt 4) {
            Beat "retrying connection to phone (attempt $openTry)"
            Start-Sleep -Milliseconds 1500
        }
    }

    if ($null -eq $srcNs) {
        $errors++
        Write-Host "CONNECT_FAILED:Unable to open the phone for file access. Unlock it, keep it on File Transfer/MTP mode, and close any other app or File Explorer window browsing it (only one program can access it at a time), then it will retry automatically."
    } else {
        CopyMTPFolderObject $srcNs $SourcePath "" $DestPath

        if ($script:copied -eq 0 -and $script:skipped -eq 0 -and ($script:visited -le 2 -or $script:errors -gt 0)) {
            # Opened the source but nothing came back - phone locked/half-mounted or
            # the top of the tree wouldn't enumerate. Worth an automatic retry
            # rather than a false "complete".
            Write-Host "CONNECT_FAILED:Could not read any files from the phone. Unlock it and keep the USB mode on File Transfer / MTP."
        }
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
        reindex_before: bool = True,
        skip_folder_keys: Optional[set] = None,
        force_verify: bool = True,
    ) -> Dict[str, Any]:
        """
        One incremental backup attempt for *device*.

        Files are copied directly into ``Phones/<Device>/latest/`` using the
        same relative paths they had on the phone. The manifest is consulted
        before copying so unchanged files are skipped, then refreshed from the
        ``latest/`` tree after each attempt so files from older runs are known.

        *skip_folder_keys*, if given, is a set of phone-relative folder paths
        (the same "A\\B\\C" keys used in the manifest) to skip entirely this
        attempt — used by backup_phone_until_complete() to route around a
        folder that has repeatedly timed out, so one bad folder can't block
        the rest of the phone forever.

        *force_verify* defaults to True: nothing is skipped on trust alone.
        See below for exactly what it turns off, and pass False for a fast pass
        that leans on the cached record instead.

        If True (MTP devices only), turns off the last-resort
        "trust the manifest" fallback that applies when this phone's MTP
        provider can't report a file's size OR its modified date — normally
        that ambiguous case is skipped on trust, but under force_verify it's
        re-copied instead, so nothing is ever taken on faith. Slower, and
        meant for exactly that: an explicit "verify everything, however long
        it takes" request.

        For the full unattended/overnight experience, use
        backup_phone_until_complete() instead, which wraps this in an
        automatic retry loop.
        """
        device_dir   = self._device_dir(device)
        latest_dir   = device_dir / "latest"

        device_dir.mkdir(parents=True, exist_ok=True)
        latest_dir.mkdir(parents=True, exist_ok=True)

        manifest = self._load_manifest(device_dir, device.display_name)
        # Re-walking latest/ is only needed once per session; skipping it on
        # rapid retries keeps reconnect turnaround fast on flaky phones. The
        # post-copy index below still runs every attempt so resume stays correct.
        if reindex_before:
            self._index_existing_backup_tree(manifest, latest_dir)
        manifest_lookup = self._build_manifest_lookup(manifest)

        # Kept as a datetime as well as the ISO string, so the post-copy
        # threat check can ask Defender specifically what this run brought in.
        run_started = datetime.now()

        result: Dict[str, Any] = {
            "device":            device.display_name,
            "device_folder":     str(device_dir),
            "latest_dir":        str(latest_dir),
            "started_at":        run_started.isoformat(),
            "completed_at":      None,
            "cancelled":         False,
            "stalled":           False,
            "connection_failed": False,
            "stage":             "copy",
            "copy_result":       None,
            "index_result":      None,
            "organize_result":   None,
            "threat_scan":       None,
            "errors":            [],
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
                dest=str(latest_dir),
                manifest_lookup=manifest_lookup,
                progress_callback=progress_callback,
                should_stop=should_stop,
                skip_folder_keys=skip_folder_keys,
                force_verify=force_verify,
                folder_sig_lookup=manifest.get("folder_sigs"),
                name_size_index=self._build_name_size_index(manifest),
            )
        else:
            copy_result = self._copy_drive(
                source=device.drive_root,
                dest=str(latest_dir),
                manifest_lookup=manifest_lookup,
                progress_callback=progress_callback,
                should_stop=should_stop,
            )

        result["copy_result"] = copy_result
        if copy_result.get("errors"):
            result["errors"].extend(copy_result["errors"])
        if copy_result.get("stalled"):
            result["stalled"] = True
        if copy_result.get("connection_failed"):
            result["connection_failed"] = True
        if copy_result.get("folder_sigs"):
            # Cache each folder's just-seen fingerprint (item count + name
            # hash) so the next attempt can skip re-checking every file in
            # any folder that hasn't changed at all since this pass.
            manifest.setdefault("folder_sigs", {}).update(copy_result["folder_sigs"])

        self._record_failures(manifest, copy_result.get("failed_keys", []))

        copy_stopped = bool(copy_result.get("cancelled")) or bool(should_stop and should_stop())

        result["stage"] = "index"

        # Re-walking latest/ is only worth it if this pass actually put
        # something there. On an attempt that copied nothing — the usual shape
        # of a failed reconnect — the local tree is byte-for-byte what the
        # pre-copy index already recorded, so re-statting every file in it
        # (20k+ files on a well-used backup) just makes each failed retry
        # slower for no new information.
        if int(copy_result.get("copied") or 0) > 0:
            if progress_callback:
                progress_callback(-3, "Indexing preserved phone folders...")
            result["index_result"] = self._index_existing_backup_tree(manifest, latest_dir)

        self._save_manifest(device_dir, manifest)
        result["manifest_summary"] = self._summarize_manifest(manifest)

        result["threat_scan"] = self._check_for_threats(
            latest_dir,
            since=run_started,
            progress_callback=progress_callback,
        )

        needs_retry = bool(result.get("stalled")) or bool(result.get("connection_failed"))

        if copy_stopped or needs_retry:
            result.update(cancelled=True, completed_at=datetime.now().isoformat())
            self._record_run(device_dir, result)
            if progress_callback:
                if result.get("connection_failed"):
                    progress_callback(-3, "Could not reach the phone — progress saved.")
                elif result.get("stalled"):
                    progress_callback(-3, "Phone stopped responding — progress saved.")
                else:
                    progress_callback(-3, "Backup stopped — progress saved.")
            return result

        result.update(stage="complete", completed_at=datetime.now().isoformat())
        self._record_run(device_dir, result)
        if progress_callback:
            progress_callback(100, "Backup complete.")

        return result

    # ── Threat check ─────────────────────────────────────────────────────────

    def _check_for_threats(
        self,
        latest_dir: Path,
        *,
        since: Optional[datetime] = None,
        progress_callback: Optional[ProgressCallback] = None,
    ) -> Dict[str, Any]:
        """
        Ask Defender whether anything in this device's backup is malicious.

        Phones collect sideloaded APKs, and a backup drive that gets carried
        between machines is exactly how something on one device reaches
        another. Defender's real-time protection already inspects each file as
        it is written here, so this does not rescan — it reads the verdicts
        Defender has already reached, which takes under a second.

        Two lists come back, because they answer different questions:

        ``new``      flagged during this run — what the phone just handed over.
        ``present``  flagged at any point and *still sitting in the backup* —
                     the standing problem. A file skipped by every subsequent
                     run because it is already backed up will never appear in
                     ``new`` again, but it is still there, and still infected.

        Never raises: a backup that succeeded must not be reported as failed
        because a security query went wrong.
        """
        out: Dict[str, Any] = {
            "checked": False,
            "reason": "",
            "new": [],
            "present": [],
            "summary": "",
            "realtime_protection": None,
        }

        try:
            if not threat_scan.defender_available():
                out["reason"] = "Microsoft Defender is not available on this machine."
                return out

            if progress_callback:
                progress_callback(-3, "Checking the backup against Defender…")

            out["realtime_protection"] = threat_scan.realtime_protection_on()

            found = threat_scan.list_detections(under_path=str(latest_dir))
            out["checked"] = True
            out["present"] = [d for d in found if d["still_present"]]
            if since:
                out["new"] = threat_scan.detected_since(found, since)
            out["summary"] = threat_scan.summarize(found)

            if progress_callback and out["present"]:
                worst = out["present"][0]
                progress_callback(
                    -3,
                    f"Defender flagged {len(out['present'])} file(s) still in this "
                    f"backup — worst is {worst['threat']} ({worst['severity_label']}) "
                    f"on {worst['name']}",
                )
        except Exception as exc:  # noqa: BLE001 - never fail a backup over this
            out["reason"] = f"Threat check could not complete: {exc}"

        return out

    # ── Backup (self-reliant, retries automatically) ────────────────────────

    MAX_RETRY_BACKOFF_SECONDS = 120
    DEVICE_POLL_INTERVAL_SECONDS = 5

    # How long to keep watching for a phone that has dropped off the bus
    # before spending another attempt on it. Generous on purpose: the whole
    # point of the retry loop is that a backup can be left running, and a
    # phone that needs a replug should be picked up whenever that happens
    # rather than forcing a restart from the UI.
    DEVICE_REAPPEAR_WAIT_SECONDS = 600

    def _wait_for_device(
        self,
        device: PhoneDevice,
        max_wait_seconds: int,
        should_stop: Optional[StopCallback],
        progress_callback: Optional[ProgressCallback],
    ) -> Optional[PhoneDevice]:
        """
        Watch for *device* to come back, for up to *max_wait_seconds*.

        Returns the re-detected PhoneDevice, or None if it never reappeared.
        The returned device matters: Windows routinely assigns a different
        MTP shell path after a drop, and retrying the stale one just fails
        again immediately.
        """
        started = time.monotonic()
        told_user = False

        while True:
            if should_stop and should_stop():
                return None

            refreshed = self._reacquire_device(device)
            if refreshed:
                return refreshed

            waited = time.monotonic() - started
            if waited >= max_wait_seconds:
                return None

            if progress_callback:
                if not told_user:
                    told_user = True
                    progress_callback(
                        -3,
                        "The phone has dropped off USB and Windows can't see it. "
                        "Unplug and replug the cable (and unlock the phone, keeping "
                        "it on File Transfer/MTP) — this will pick it up on its own "
                        "and carry on from where it stopped. Nothing copied so far "
                        "is lost.",
                    )
                else:
                    progress_callback(
                        -2,
                        f"Waiting for the phone to reappear… {int(waited)}s so far "
                        f"(replugging the cable is the quickest fix)",
                    )

            time.sleep(self.DEVICE_POLL_INTERVAL_SECONDS)

    def backup_phone_until_complete(
        self,
        device: PhoneDevice,
        progress_callback: Optional[ProgressCallback] = None,
        should_stop: Optional[StopCallback] = None,
        max_attempts: int = DEFAULT_MAX_RETRY_ATTEMPTS,
        max_total_seconds: int = DEFAULT_MAX_TOTAL_RUNTIME_SECONDS,
        force_verify: bool = True,
    ) -> Dict[str, Any]:
        """
        Run backup_phone() in an automatic retry loop so a single call can be
        left unattended overnight. Retries on BOTH kinds of interruption:
        a mid-transfer stall (phone goes quiet for a while) and a connection
        failure (the MTP source won't even open — USB drop, driver hiccup).
        Treating only the first kind as retry-worthy was the bug that made a
        genuinely-unreachable phone get reported as "Completed".

        *force_verify*, if True, disables the last-resort "trust it" fallback
        in the per-file check (see backup_phone()) for the whole run — every
        file the manifest can't positively confirm unchanged gets re-copied
        instead of skipped. Slower, deliberately: for when the user wants a
        genuinely thorough pass rather than a fast one.

        During the wait between attempts, polls for the device every few
        seconds rather than sleeping blindly for the whole backoff window, so
        it resumes as soon as the phone is detected again instead of always
        waiting out the full delay.
        """
        start_time = time.monotonic()
        last_result: Dict[str, Any] = {}
        current_device = device
        interruption_count = 0
        stall_streak = 0          # consecutive retries with no forward progress
        did_full_index = False

        # If the SAME folder is where the phone stalls two attempts in a row
        # with zero files copied since entering it, that folder itself is the
        # problem (an item WPD can't enumerate, most likely) — not the USB
        # connection. Retrying it forever makes zero progress on the rest of
        # the phone every single time. Skip it after that, and say so.
        skip_folder_keys: set = set()
        skip_messages: List[str] = []
        stuck_folder_key: Optional[str] = None
        stuck_folder_repeats = 0

        # Totals for the WHOLE run, not just the final attempt. Without this,
        # a run that copies 236 files and then loses the phone reports the
        # last attempt's numbers — "0 copied, 0 skipped" — which reads as
        # "it did nothing" when in fact the work was done and saved.
        run_totals: Dict[str, int] = {
            "copied": 0, "copied_locally": 0, "skipped": 0, "skipped_manifest": 0, "skipped_elsewhere": 0, "skipped_excluded": 0,
            "skipped_partial": 0,
        }

        # Threats attributed to this run, accumulated across attempts for the
        # same reason the file totals are. The final attempt's "present" list
        # is a full query of the backup folder and stands on its own, but each
        # attempt's "new" list only covers its own window — so a file flagged
        # during attempt 1 would vanish from the report if the run ended on
        # attempt 4. Keyed by path, since a retry can re-copy the same file.
        run_threats_new: Dict[str, Dict[str, Any]] = {}

        for attempt in range(1, max_attempts + 1):
            if should_stop and should_stop():
                break

            last_result = self.backup_phone(
                current_device,
                progress_callback=progress_callback,
                should_stop=should_stop,
                reindex_before=not did_full_index,
                skip_folder_keys=skip_folder_keys or None,
                force_verify=force_verify,
            )
            did_full_index = True
            last_result["attempt"] = attempt
            if skip_folder_keys:
                last_result["skipped_folders"] = sorted(skip_folder_keys)

            copy_result = last_result.get("copy_result") or {}
            made_progress = int(copy_result.get("copied") or 0) > 0

            for key in run_totals:
                run_totals[key] += int(copy_result.get(key) or 0)
            last_result["run_totals"] = dict(run_totals)

            for det in (last_result.get("threat_scan") or {}).get("new") or []:
                run_threats_new.setdefault(str(det.get("path", "")).lower(), det)

            needs_retry = bool(last_result.get("stalled") or last_result.get("connection_failed"))
            if needs_retry:
                interruption_count += 1
                # A phone that keeps handing us files between drops is "working" —
                # keep retries snappy. Only back off when nothing is getting through.
                stall_streak = 0 if made_progress else stall_streak + 1

            this_stuck_key = copy_result.get("stuck_folder_key")
            if last_result.get("stalled") and this_stuck_key:
                if this_stuck_key == stuck_folder_key:
                    stuck_folder_repeats += 1
                else:
                    stuck_folder_key = this_stuck_key
                    stuck_folder_repeats = 1

                if stuck_folder_repeats >= 2 and stuck_folder_key not in skip_folder_keys:
                    skip_folder_keys.add(stuck_folder_key)
                    msg = (
                        f"Skipping '{stuck_folder_key}' after it repeatedly timed out "
                        f"partway through listing it — continuing with the rest of the "
                        f"phone instead of retrying it forever. Open that folder in "
                        f"File Explorer to check it manually if it has files you need."
                    )
                    skip_messages.append(msg)
                    last_result.setdefault("errors", []).append(msg)
                    if progress_callback:
                        progress_callback(-3, msg)
            else:
                stuck_folder_key = None
                stuck_folder_repeats = 0

            if not needs_retry:
                break

            if should_stop and should_stop():
                break

            if time.monotonic() - start_time > max_total_seconds:
                last_result.setdefault("errors", []).append(
                    "Stopped auto-retrying after reaching the maximum unattended run time."
                )
                break

            reason = (
                "lost the USB/MTP connection" if last_result.get("connection_failed")
                else "stopped responding"
            )
            if progress_callback:
                progress_callback(
                    -3,
                    f"Phone {reason} after {int(copy_result.get('copied') or 0)} file(s) this "
                    f"pass — retrying (attempt {attempt + 1})…",
                )

            # How long to keep looking for the phone before spending another
            # attempt on it.
            #
            # This used to key off "did we copy anything this pass", which had
            # it exactly backwards for the most common real failure: copy a
            # few hundred files, then have the phone's MTP stack fall off the
            # USB bus entirely. That counted as progress, so it got the
            # SHORTEST wait (3s) — far too little for a device that needs to
            # re-enumerate, so the next attempts fired instantly against a
            # phone that wasn't there and burned themselves for nothing.
            #
            # What actually matters is whether the phone is still reachable.
            # If it isn't, be patient: a crashed MTP responder usually comes
            # back on its own within a minute or two, and a replugged cable
            # should be picked up automatically rather than making the user
            # start over.
            refreshed = self._reacquire_device(current_device)
            if refreshed is None:
                refreshed = self._wait_for_device(
                    current_device,
                    self.DEVICE_REAPPEAR_WAIT_SECONDS,
                    should_stop,
                    progress_callback,
                )
            elif not made_progress:
                # Phone is visible but gave us nothing — a short breather,
                # backing off as the empty passes pile up.
                time.sleep(min(5 * stall_streak, self.MAX_RETRY_BACKOFF_SECONDS))

            if refreshed:
                current_device = refreshed
                if progress_callback:
                    progress_callback(-3, "Phone detected again — resuming…")
            elif progress_callback:
                progress_callback(-3, "Phone still not detected — trying the existing connection anyway…")

        last_result["attempts_used"] = last_result.get("attempt", 1)
        last_result["interruption_count"] = interruption_count
        last_result["run_totals"] = dict(run_totals)

        if run_threats_new:
            scan = last_result.get("threat_scan")
            if not isinstance(scan, dict):
                # The final attempt never got as far as its threat check
                # (cancelled early, say). What earlier attempts found is still
                # true and still worth reporting.
                scan = {"checked": True, "reason": "", "new": [], "present": [],
                        "summary": "", "realtime_protection": None}
                last_result["threat_scan"] = scan
            scan["new"] = sorted(
                run_threats_new.values(),
                key=lambda d: (d.get("severity", 0), d.get("detected_at", "")),
                reverse=True,
            )
        if skip_folder_keys:
            last_result["skipped_folders"] = sorted(skip_folder_keys)
        # Folder-skip notices were recorded on whichever attempt discovered
        # them, which isn't necessarily this final one (e.g. the very next
        # attempt might succeed cleanly) — make sure they survive either way,
        # since the user still needs to know a folder was left out.
        existing_errors = last_result.setdefault("errors", [])
        for msg in skip_messages:
            if msg not in existing_errors:
                existing_errors.append(msg)
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
        """
        {relative_path: {"size", "skip", "excluded"}} for the copy stage.

        ``skip`` and ``excluded`` are deliberately separate, because
        "Verify everything" treats them differently and must. A ``skip`` set by
        repeated copy failures is a guess the run is entitled to revisit — the
        whole point of verifying everything is to stop trusting earlier
        verdicts. An ``excluded`` entry is not a verdict about the file, it is
        the user saying they do not want it backed up; re-fetching it because
        they asked for a thorough pass would quietly undo a deletion they
        requested.
        """
        lookup: Dict[str, Dict[str, Any]] = {}
        for relkey, entry in manifest.get("files", {}).items():
            excluded = entry.get("status") == "excluded"
            if entry.get("skip") or excluded:
                lookup[relkey] = {
                    "size": entry.get("size", 0),
                    "skip": True,
                    "excluded": excluded,
                }
            elif entry.get("status") == "ok":
                lookup[relkey] = {"size": entry.get("size", 0), "skip": False,
                                  "excluded": False}
        return lookup

    def _build_name_size_index(self, manifest: Dict[str, Any]) -> Dict[str, str]:
        """
        {"<lowercase filename>|<size>": "<backup-relative path>"} for every
        confirmed-ok file, regardless of which folder it is in.

        Lets the copy script recognise a file the phone now presents at a new
        path as one whose bytes are already on the backup drive, and say where
        — so it can be duplicated locally instead of pulled down the MTP wire
        again. Matched on name+size, the same trust level the rest of this
        manifest relies on, applied across the whole phone rather than one
        exact path.

        It maps to a path rather than a bare flag because the script has to be
        able to *act* on the match. Knowing only that a copy existed somewhere
        led it to skip the file and record nothing, so the new path stayed
        empty and every later run skipped it again for the same reason.
        """
        index: Dict[str, str] = {}
        for relkey, entry in manifest.get("files", {}).items():
            if entry.get("status") != "ok" or entry.get("skip"):
                continue
            size = entry.get("size") or 0
            if not size:
                continue
            name = relkey.rsplit("\\", 1)[-1].lower()
            # First writer wins; any confirmed copy serves equally as a source.
            index.setdefault(f"{name}|{size}", relkey)
        return index

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

    # Extensions whose files cannot legitimately be zero bytes: a real video,
    # audio file, image, archive, document or installer carries a header at
    # minimum. A 0-byte one is a copy that didn't happen, and recording it as a
    # success means the backup reports content it does not have.
    #
    # Android, by contrast, is full of genuinely empty files -- .nomedia
    # markers, .uid stream tags, SQLite -wal journals, Unity "config" stubs --
    # and those are faithfully backed up by being empty. Hence an allow-list of
    # types that must have bytes, rather than treating every empty file as
    # broken.
    _MUST_HAVE_BYTES_EXTS = frozenset({
        ".mp4", ".webm", ".mkv", ".avi", ".mov", ".m4v", ".3gp", ".flv", ".wmv",
        ".mp3", ".m4a", ".flac", ".ogg", ".opus", ".wav", ".aac", ".wma",
        ".jpg", ".jpeg", ".png", ".gif", ".heic", ".webp", ".bmp", ".tiff",
        ".zip", ".7z", ".rar", ".cab", ".tar", ".gz",
        ".pdf", ".docx", ".xlsx", ".pptx", ".epub", ".cbz",
        ".apk", ".exe", ".dll", ".iso", ".img",
    })

    @classmethod
    def _is_empty_but_shouldnt_be(cls, rel: str, size: int) -> bool:
        if size != 0:
            return False
        return os.path.splitext(rel)[1].lower() in cls._MUST_HAVE_BYTES_EXTS

    def _index_existing_backup_tree(self, manifest: Dict[str, Any], latest_dir: Path) -> Dict[str, int]:
        """
        Rebuild the manifest's known-good file entries from the preserved
        latest/ tree. This makes old backups and partially completed runs part
        of the incremental index without moving or reorganizing anything.
        """
        files = manifest.setdefault("files", {})
        now = datetime.now().isoformat()
        indexed = 0
        total_bytes = 0
        empty_files = 0

        if not latest_dir.exists():
            return {"indexed_files": 0, "indexed_bytes": 0, "empty_files": 0}

        for path in latest_dir.rglob("*"):
            if not path.is_file():
                continue
            if any(part.startswith("_aegis_stage_") for part in path.parts):
                continue  # orphaned staging leftovers from a killed attempt

            try:
                rel = str(path.relative_to(latest_dir))
                stat = path.stat()
            except OSError:
                continue

            prior = files.get(rel, {})
            # A content-bearing file with no content is not a backup of
            # anything. Recording it "ok" is what let four 0-byte .webm files
            # count toward "confirmed on disk" indefinitely: the phone had only
            # the .trashed- version by then, so nothing ever revisited the path
            # and the empty file stood in for the real one for good.
            empty = self._is_empty_but_shouldnt_be(rel, stat.st_size)
            files[rel] = {
                **prior,
                "size": stat.st_size,
                "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(),
                "source_relative": rel,
                "dest_path": rel,
                "status": "empty" if empty else "ok",
                "indexed_at": now,
                # Don't reset a running failure count for an empty file — it is
                # how the retry ceiling eventually stops trying a file the
                # phone genuinely cannot serve.
                "fail_count": int(prior.get("fail_count") or 0) if empty else 0,
            }
            if empty:
                empty_files += 1
                continue
            indexed += 1
            total_bytes += stat.st_size

        return {"indexed_files": indexed, "indexed_bytes": total_bytes,
                "empty_files": empty_files}

    # ── Manifest repair ──────────────────────────────────────────────────────

    def repair_manifest(
        self,
        device_dir: str | Path,
        *,
        dry_run: bool = True,
        progress_callback: Optional[ProgressCallback] = None,
    ) -> Dict[str, Any]:
        """
        Reconcile a device's manifest with what is actually in ``latest/``.

        Backups made before the switch to preserving the phone's own folder
        structure sorted files into category folders instead — ``Private_Files\\``,
        ``Messages\\``, ``Audio\\``. Those runs recorded each file under its
        *phone* path as the key while writing it to its *category* path, so the
        entry points at a location that holds nothing:

            key       Internal shared storage\\Documents\\Adobe Scan\\x.pdf
            dest_path Private_Files\\x.pdf          <- the file is actually here

        Two things follow, and both are fixed here. The tree indexer also
        records that file under its real path, so one file owns two entries and
        every total counting them is inflated. And because the stale entry says
        ``status: ok``, the copy stage skips that phone path forever, so the
        file never migrates into the structure the rest of the backup uses.

        The repair needs no re-transfer: the stale entry's own key *is* the
        phone-relative path, so the file can be moved into place locally. On
        one volume that is a rename, so 50 GB costs nothing.

        Defaults to *dry_run* — call with ``dry_run=False`` to apply.
        """
        device_dir = Path(device_dir)
        latest_dir = device_dir / "latest"
        manifest = self._load_manifest(device_dir, device_dir.name)
        files: Dict[str, Any] = manifest.setdefault("files", {})

        report: Dict[str, Any] = {
            "dry_run": dry_run,
            "device_folder": str(device_dir),
            "entries_before": len(files),
            "moved": 0, "moved_bytes": 0,
            "phantom_entries_removed": 0,
            "stale_entries_pruned": 0,
            "failure_records_kept": 0,
            "collisions": 0,
            "errors": [],
            "actions": [],
        }

        if not latest_dir.exists():
            report["errors"].append(f"no latest/ folder at {latest_dir}")
            return report

        on_disk = {
            str(p.relative_to(latest_dir)).lower()
            for p in latest_dir.rglob("*") if p.is_file()
        }

        if progress_callback:
            progress_callback(-3, f"Reconciling {len(files):,} manifest entries "
                                  f"against {len(on_disk):,} files on disk…")

        # ── plan ────────────────────────────────────────────────────────────
        # Planned first, applied second, because several stale entries can name
        # the SAME dest_path. The old scheme flattened the phone into category
        # folders, so "Pictures\custom.png" and "Pictures\custom (1).png" both
        # ended up recorded against "Private_Files\custom.png" — one physical
        # file, two claims on it. Moving as we walk would satisfy whichever
        # entry came first and then fail on the rest with a missing source.
        contenders: Dict[str, List[tuple]] = {}
        phantoms: List[str] = []

        for key, entry in files.items():
            if not isinstance(entry, dict) or key.lower() in on_disk:
                continue

            # No size means this was never a successful copy — it is a failure
            # record, and dropping it would just make the next run retry a file
            # that has already failed repeatedly.
            if "size" not in entry:
                report["failure_records_kept"] += 1
                continue

            dest = entry.get("dest_path")
            if not dest or dest.lower() == key.lower() or dest.lower() not in on_disk:
                phantoms.append(key)   # nothing behind it anywhere
                continue

            contenders.setdefault(dest.lower(), []).append((key, entry, dest))

        moves: List[tuple] = []      # (src_rel, tgt_rel, size, key, entry)

        for _, group in contenders.items():
            dest = group[0][2]
            src_path = latest_dir / dest
            try:
                actual_size = src_path.stat().st_size
            except OSError as exc:
                report["errors"].append(f"{dest}: {exc}")
                continue

            if len(group) == 1:
                winner, losers = group[0], []
            else:
                # Only one target can be satisfied honestly. Prefer the entry
                # whose recorded size matches the file actually present; the
                # others described content this scheme overwrote years ago.
                exact = [g for g in group if int(g[1].get("size") or 0) == actual_size]
                winner = exact[0] if exact else group[0]
                losers = [g for g in group if g is not winner]
                report["contended_sources"] = report.get("contended_sources", 0) + 1

            for key, _entry, _d in losers:
                phantoms.append(key)

            key, entry, _d = winner
            tgt_path = latest_dir / key
            size = int(entry.get("size") or 0)

            # A *file* cannot be sitting here: entries whose key is already on
            # disk were filtered out above, and no two entries share a key, so
            # no two moves share a target. A *directory* at the target path is
            # possible though, and os.replace would fail on it — so check, and
            # leave the file where it is rather than raise.
            if tgt_path.exists():
                report["collisions"] += 1
                report["actions"].append(("collision", dest, key, size))
                continue

            moves.append((dest, key, size, key, entry))

        # These moves cannot interfere with one another, which is worth stating
        # because it is the reason no ordering logic is needed. Every move
        # reads from a path that is on disk and writes to one that is not
        # (entries whose key already exists were filtered out at the top), so
        # no move's target is any other move's source. And keys are unique in
        # the manifest dict, so no two moves share a target either.
        report["moved"] = len(moves)
        report["moved_bytes"] = sum(m[2] for m in moves)
        report["phantom_entries_removed"] = len(phantoms)
        report["actions"].extend(("move", m[0], m[1], m[2]) for m in moves)

        # ── apply ───────────────────────────────────────────────────────────
        if not dry_run:
            for dest, key, size, _k, entry in moves:
                try:
                    tgt_path = latest_dir / key
                    tgt_path.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(latest_dir / dest, tgt_path)
                    files[key] = {
                        "size": size,
                        "modified": entry.get("modified"),
                        "source_relative": key,
                        "dest_path": key,
                        "status": "ok",
                        "indexed_at": datetime.now().isoformat(),
                        "fail_count": 0,
                    }
                except OSError as exc:
                    report["errors"].append(f"move {dest} -> {key}: {exc}")
                    report["moved"] -= 1
                    report["moved_bytes"] -= size


            for key in phantoms:
                files.pop(key, None)

            # Reconcile against the tree as it now stands. The moves above
            # emptied the old category paths, and the tree indexer that
            # recorded those paths cannot retract its own entries — it only
            # adds what it finds on a walk. So every migrated file briefly
            # owns two entries: the rewritten one at its phone path, and the
            # indexer's orphan at the category path it just left. An entry
            # claiming a copied file with no file behind it is exactly what
            # made the copy stage skip a path it had never actually written.
            still_present = {
                str(p.relative_to(latest_dir)).lower()
                for p in latest_dir.rglob("*") if p.is_file()
            }
            stale = [
                k for k, v in files.items()
                if isinstance(v, dict) and "size" in v and k.lower() not in still_present
            ]
            for key in stale:
                files.pop(key, None)
            report["stale_entries_pruned"] = len(stale)

        if not dry_run:
            _prune_empty_dirs(latest_dir)
            self._index_existing_backup_tree(manifest, latest_dir)
            self._save_manifest(device_dir, manifest)

        report["entries_after"] = len(files)
        report["summary"] = self._summarize_manifest(manifest)
        return report

    def purge_from_backup(
        self,
        device_dir: str | Path,
        relative_paths: List[str],
        *,
        mark_skip: bool = True,
        dry_run: bool = True,
    ) -> Dict[str, Any]:
        """
        Delete specific files from a device's backup and stop them coming back.

        Deleting the file alone is not enough: if it is still on the phone the
        next run simply copies it again. Setting ``skip`` on the manifest entry
        is what makes the exclusion stick — the copy stage already honours that
        flag for files that have failed too often, and it serves equally well
        for "I do not want this backed up".

        *relative_paths* are paths under ``latest/``.
        """
        device_dir = Path(device_dir)
        latest_dir = device_dir / "latest"
        manifest = self._load_manifest(device_dir, device_dir.name)
        files: Dict[str, Any] = manifest.setdefault("files", {})

        out: Dict[str, Any] = {
            "dry_run": dry_run, "deleted": 0, "deleted_bytes": 0,
            "marked_skip": 0, "not_found": 0, "errors": [], "files": [],
        }

        # A manifest key may differ in case from the path on disk.
        by_lower = {k.lower(): k for k in files}

        for rel in relative_paths:
            p = latest_dir / rel
            if not p.exists():
                out["not_found"] += 1
                continue
            try:
                size = p.stat().st_size
            except OSError as exc:
                out["errors"].append(f"{rel}: {exc}")
                continue

            out["deleted"] += 1
            out["deleted_bytes"] += size
            out["files"].append({"path": rel, "bytes": size})

            if not dry_run:
                try:
                    p.unlink()
                except OSError as exc:
                    out["errors"].append(f"unlink {rel}: {exc}")
                    out["deleted"] -= 1
                    out["deleted_bytes"] -= size
                    continue

            key = by_lower.get(rel.lower())
            if key is not None:
                out["marked_skip"] += 1
                if not dry_run:
                    if mark_skip:
                        files[key] = {
                            **files[key],
                            "status": "excluded",
                            "skip": True,
                            "excluded_at": datetime.now().isoformat(),
                        }
                    else:
                        del files[key]

        if not dry_run:
            _prune_empty_dirs(latest_dir)
            self._save_manifest(device_dir, manifest)
            out["summary"] = self._summarize_manifest(manifest)

        return out

    def _summarize_manifest(self, manifest: Dict[str, Any]) -> Dict[str, int]:
        ok = 0
        failed_pending = 0
        skipped_permanent = 0
        confirmed_bytes = 0
        empty = 0
        for entry in manifest.get("files", {}).values():
            if entry.get("skip"):
                skipped_permanent += 1
            elif entry.get("status") == "ok":
                ok += 1
                confirmed_bytes += int(entry.get("size") or 0)
            elif entry.get("status") == "empty":
                # Present on the drive but with no content, so deliberately
                # NOT counted as confirmed — that overstatement is the bug.
                empty += 1
            elif entry.get("status") == "failed":
                failed_pending += 1
        return {
            "confirmed_files": ok,
            "confirmed_bytes": confirmed_bytes,
            "pending_retry_files": failed_pending,
            "permanently_skipped_files": skipped_permanent,
            "empty_files": empty,
        }

    # ── Copy helpers ──────────────────────────────────────────────────────

    def _copy_mtp(
        self,
        shell_path: str,
        dest: str,
        manifest_lookup: Dict[str, Dict[str, Any]],
        progress_callback: Optional[ProgressCallback],
        should_stop: Optional[StopCallback],
        skip_folder_keys: Optional[set] = None,
        force_verify: bool = True,
        folder_sig_lookup: Optional[Dict[str, Dict[str, Any]]] = None,
        name_size_index: Optional[Dict[str, int]] = None,
    ) -> Dict[str, Any]:
        result: Dict[str, Any] = {
            "copied": 0, "copied_locally": 0, "skipped": 0, "skipped_manifest": 0, "skipped_elsewhere": 0, "skipped_excluded": 0,
            "skipped_partial": 0,
            "errors": [], "failed_keys": [],
            "cancelled": False, "stalled": False, "connection_failed": False,
            "folder_sigs": {},
        }

        if os.name != "nt":
            result["errors"].append("MTP copy is only supported on Windows.")
            return result

        # The copy script no longer stages through a throwaway folder (that
        # approach turned out to make real MTP transfers unreliable — see
        # the comment above the copy loop in _PS_COPY_MTP). This just mops
        # up any leftover "_aegis_stage_*" debris from a run made under an
        # earlier version of this script, so it doesn't sit there forever.
        for stray in Path(dest).glob("_aegis_stage_*"):
            shutil.rmtree(stray, ignore_errors=True)

        manifest_path = _write_temp_manifest_lookup(manifest_lookup)
        skip_folders_path = _write_temp_skip_folders(skip_folder_keys or set())
        folder_sig_path = _write_temp_manifest_lookup(folder_sig_lookup or {})
        name_size_path = _write_temp_manifest_lookup(name_size_index or {})
        ps_path = _write_temp_script(_PS_COPY_MTP)
        unverified_size_paths: List[str] = []
        # Which folder we most recently entered, and how many files had been
        # copied cumulatively at that moment — lets us tell, if this attempt
        # stalls, whether the folder we were listing made zero progress (i.e.
        # is itself the problem) rather than blaming the phone/connection.
        last_folder_key: Optional[str] = None
        folder_key_baseline_copied = 0
        try:
            cmd = [
                "powershell", "-NoProfile", "-NonInteractive",
                "-ExecutionPolicy", "Bypass",
                "-File", ps_path,
                "-SourcePath", shell_path,
                "-DestPath",   dest,
                "-TimeoutSec", str(MTP_FILE_TIMEOUT_SECONDS),
                "-MaxFileSec", str(MTP_FILE_MAX_SECONDS),
                "-ManifestPath", manifest_path,
                "-SkipFoldersPath", skip_folders_path,
                "-FolderSigPath", folder_sig_path,
                "-NameSizeIndexPath", name_size_path,
            ]
            if force_verify:
                cmd.append("-ForceVerify")
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
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
            soft_warned = False
            last_progress_emit = 0.0

            while True:
                if should_stop and should_stop():
                    _kill_process_tree(proc)
                    result["cancelled"] = True
                    break

                try:
                    line = line_queue.get(timeout=0.5)
                except queue.Empty:
                    if proc.poll() is not None and stream_closed:
                        break

                    quiet_for = time.monotonic() - last_activity

                    # The process finished but the reader hasn't drained yet —
                    # keep looping, don't treat as a stall.
                    if proc.poll() is not None:
                        continue

                    if quiet_for > MTP_SOFT_STALL_SECONDS and not soft_warned:
                        soft_warned = True
                        if progress_callback:
                            progress_callback(
                                -2,
                                f"Phone has been quiet for {int(quiet_for)}s — still "
                                f"connected, waiting (normal while it lists a large "
                                f"folder, e.g. thousands of photos — can take several "
                                f"minutes)…",
                            )

                    if quiet_for > MTP_STALL_TIMEOUT_SECONDS:
                        _kill_process_tree(proc)
                        result["stalled"] = True
                        result["cancelled"] = True
                        result["errors"].append(
                            f"Phone went unresponsive for {int(quiet_for)}s after "
                            f"{result['copied']} file(s) this attempt. Progress is saved; "
                            f"it will retry automatically."
                        )
                        if last_folder_key and result["copied"] == folder_key_baseline_copied:
                            # Nothing at all came through since we entered this
                            # folder — it's the folder itself that's stuck, not
                            # a generic connection drop.
                            result["stuck_folder_key"] = last_folder_key
                        break
                    continue

                if line is None:
                    stream_closed = True
                    if proc.poll() is not None:
                        break
                    continue

                last_activity = time.monotonic()
                soft_warned = False
                line = line.rstrip()
                if not line:
                    continue

                if line.startswith("PING:"):
                    # Keep-alive from inside a blocking call (folder listing,
                    # waiting on one file). This resets the stall timer above
                    # — the process genuinely isn't stuck — but it used to be
                    # dropped here entirely, so the UI just sat frozen with no
                    # way to tell "slow" from "dead" for as long as a single
                    # file's wait allows. Surface it (throttled) so there's
                    # always visible proof of life.
                    now = time.monotonic()
                    if progress_callback and (now - last_progress_emit) >= 1.0:
                        last_progress_emit = now
                        progress_callback(-2, line[5:])
                    continue

                if line.startswith("COPY_PROGRESS:"):
                    body = line[14:]
                    relkey, _, size_str = body.rpartition(":")
                    now = time.monotonic()
                    if progress_callback and (now - last_progress_emit) >= 1.0:
                        last_progress_emit = now
                        try:
                            mb = int(size_str) / (1024 * 1024)
                        except ValueError:
                            mb = 0.0
                        progress_callback(-2, f"Copying {Path(relkey).name} — {mb:.1f} MB")
                    continue

                if line.startswith("FILE_OK:"):
                    result["copied"] += 1
                    fname = Path(line[len("FILE_OK:"):]).name
                    if progress_callback:
                        progress_callback(-1, f"Copied: {fname}")

                elif line.startswith("FILE_OK_UNSIZED:"):
                    # Copied, but the phone never reported a size for this
                    # file, so "it reached the expected byte count" was never
                    # a check we could actually make — the only sign of
                    # success was "the bytes stopped flowing". Queue it for a
                    # real local integrity check once the transfer is done.
                    #
                    # The offset here is derived from the prefix on purpose: a
                    # hand-counted one was off by one, which silently ate the
                    # drive letter ("D:\..." -> ":\...") and made every one of
                    # these files fail its integrity check with a bogus
                    # "cannot stat" against a path that could never resolve.
                    path = line[len("FILE_OK_UNSIZED:"):]
                    result["copied"] += 1
                    unverified_size_paths.append(path)
                    if progress_callback:
                        progress_callback(-1, f"Copied: {Path(path).name}")

                elif line.startswith("FILE_SKIP_MANIFEST:"):
                    result["skipped"] += 1
                    result["skipped_manifest"] += 1

                elif line.startswith("FILE_SKIP_PARTIAL:"):
                    result["skipped"] += 1
                    result["skipped_partial"] += 1

                elif line.startswith("FILE_SKIP_EXCLUDED:"):
                    # Deliberately excluded by the user. Counted apart from
                    # ordinary skips so "nothing was copied" can be told from
                    # "you asked for these to be left alone".
                    result["skipped"] += 1
                    result["skipped_excluded"] += 1

                elif line.startswith("FILE_LOCAL_COPY:"):
                    # Same name+size was already on the backup drive under a
                    # different path, so it was duplicated locally rather than
                    # pulled over MTP again. A real copy that really landed —
                    # counted as copied, and the post-copy index records it
                    # like any other file.
                    path = line[len("FILE_LOCAL_COPY:"):]
                    result["copied"] += 1
                    result["copied_locally"] += 1
                    if progress_callback:
                        progress_callback(-1, f"Copied locally: {Path(path).name}")

                elif line.startswith("FILE_SKIP_ELSEWHERE:"):
                    # Kept for backwards compatibility with any in-flight
                    # script from an older build; current runs duplicate the
                    # file locally and emit FILE_LOCAL_COPY instead.
                    result["skipped"] += 1
                    result["skipped_elsewhere"] += 1

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

                elif line.startswith("FENTER_KEY:"):
                    last_folder_key = line[11:] or None
                    folder_key_baseline_copied = result["copied"]

                elif line.startswith("FOLDER_SKIP:"):
                    folder = line[12:].split("\\")[-1]
                    if progress_callback:
                        progress_callback(-1, f"Skipping restricted folder: {folder}")

                elif line.startswith("FOLDER_SIG:"):
                    body = line[11:]
                    try:
                        relkey, count_str, sig = body.rsplit(":", 2)
                        result["folder_sigs"][relkey] = {"count": int(count_str), "sig": sig}
                    except ValueError:
                        pass

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

                elif line.startswith("CONNECT_FAILED:"):
                    message = line[15:]
                    result["connection_failed"] = True
                    result["errors"].append(message)

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
            try:
                os.unlink(skip_folders_path)
            except OSError:
                pass
            try:
                os.unlink(folder_sig_path)
            except OSError:
                pass
            try:
                os.unlink(name_size_path)
            except OSError:
                pass

        if unverified_size_paths:
            self._verify_unsized_copies(unverified_size_paths, dest, result, progress_callback)

        return result

    def _verify_unsized_copies(
        self,
        paths: List[str],
        dest: str,
        result: Dict[str, Any],
        progress_callback: Optional[ProgressCallback],
    ) -> None:
        """
        Local, post-copy safety net for files the phone never reported a size
        for — the only evidence of a good transfer for those was "the bytes
        stopped flowing for a few seconds", which isn't real proof of an
        intact file. Runs entirely against the local copy (no MTP involved,
        so it's fast) using the same integrity checker the rest of the app
        already relies on. A file that fails gets deleted rather than left in
        place looking "confirmed" — the next attempt then sees it's simply
        missing and re-copies it for real, the same recovery path already
        used for any other copy failure.
        """
        from core.integrity import verify_deep, verify_readable

        total = len(paths)
        bad: List[tuple] = []
        for i, path in enumerate(paths):
            if progress_callback and (i % 5 == 0 or i == total - 1):
                progress_callback(
                    -2,
                    f"Verifying transferred file {i + 1}/{total} "
                    f"(phone couldn't report its size)…",
                )
            status, detail, _sha = verify_readable(path)
            if status == "ok":
                deep_status, deep_detail = verify_deep(path)
                if deep_status not in ("ok", "unverified", "skipped"):
                    bad.append((path, f"{deep_status}: {deep_detail}"))
            else:
                bad.append((path, f"{status}: {detail}"))

        if not bad:
            return

        result["copied"] = max(0, result.get("copied", 0) - len(bad))
        result["integrity_failed"] = [p for p, _ in bad]

        for path, reason in bad:
            # Only claim it was removed if it actually was. Reporting a
            # deletion that silently failed is how a bad path turned into
            # four confident "deleted so it's re-copied next run" messages
            # about files that were sitting on disk the whole time.
            removed = False
            try:
                os.unlink(path)
                removed = True
            except OSError:
                pass

            if removed:
                try:
                    result["failed_keys"].append(os.path.relpath(path, dest))
                except ValueError:
                    pass

            if len(result["errors"]) < 20:
                tail = (
                    "removed, so it gets copied again cleanly next run"
                    if removed else
                    "could NOT be removed, so it is still in the backup as-is "
                    "— check it by hand"
                )
                result["errors"].append(
                    f"Copied but failed a local integrity check ({reason}) — "
                    f"{tail}: {Path(path).name}"
                )

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


        result: Dict[str, Any] = {
            "copied": 0, "skipped": 0, "skipped_manifest": 0,
            "errors": [], "failed_keys": [],
            "cancelled": False, "stalled": False, "connection_failed": False,
        }

        if not Path(source).exists():
            result["connection_failed"] = True
            result["errors"].append(f"Source path no longer exists: {source}")
            return result

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
                if d_lower in _SKIP_DIRS or d_lower == "android" or d_lower.startswith("."):
                    continue
                filtered_dirs.append(d)
            dirs[:] = filtered_dirs

            for fname in files:
                all_files.append(Path(root) / fname)

        total = max(len(all_files), 1)

        consecutive_errors = 0

        for idx, src_file in enumerate(all_files):
            if should_stop and should_stop():
                result["cancelled"] = True
                break

            # If the phone disconnected mid-run, stop and let the retry loop
            # re-detect it rather than logging thousands of failures. The
            # exists() probe is throttled so it doesn't stat the root per file.
            if consecutive_errors >= 12 or (idx % 50 == 0 and not source_path.exists()):
                result["connection_failed"] = True
                result["errors"].append(
                    "Lost contact with the phone during the copy — progress saved, will retry."
                )
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

            dst_file = dest_path / rel_key

            try:
                dst_file.parent.mkdir(parents=True, exist_ok=True)

                if dst_file.exists():
                    src_stat = src_file.stat()
                    dst_stat = dst_file.stat()
                    if (
                        dst_stat.st_size == src_stat.st_size
                        and int(dst_stat.st_mtime) >= int(src_stat.st_mtime)
                    ):
                        result["skipped"] += 1
                        consecutive_errors = 0
                        continue
            except PermissionError:
                continue
            except OSError:
                consecutive_errors += 1
                continue

            # Up to 3 tries per file — transient USB hiccups are common.
            for attempt in range(1, 4):
                try:
                    verified_copy(str(src_file), str(dst_file),
                                  fsync=True, readback=False)
                    result["copied"] += 1
                    consecutive_errors = 0
                    break
                except PermissionError:
                    break  # locked file — skip silently
                except (OSError, VerifiedCopyError) as exc:
                    if attempt == 3:
                        result["errors"].append(f"{rel_key}: {exc}")
                        result["failed_keys"].append(rel_key)
                        consecutive_errors += 1
                        # Drop a partial so it retries cleanly next run.
                        try:
                            if dst_file.exists():
                                dst_file.unlink()
                        except OSError:
                            pass
                    else:
                        time.sleep(0.5)

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
        index_r = result.get("index_result") or {}

        # Just enough of the threat check to answer "when did this first show
        # up in my backup" months later, without copying whole detection
        # records into a file that keeps 50 of these.
        threat_r = result.get("threat_scan") or {}
        threats_present = threat_r.get("present") or []
        threat_entry = None
        if threat_r.get("checked"):
            threat_entry = {
                "flagged_still_present": len(threats_present),
                "flagged_this_run": len(threat_r.get("new") or []),
                "worst": (threats_present[0].get("threat") if threats_present else None),
                "files": [d.get("path") for d in threats_present[:10]],
            }

        history.append({
            "started_at":      result.get("started_at"),
            "completed_at":    result.get("completed_at"),
            "cancelled":       result.get("cancelled", False),
            "stalled":         result.get("stalled", False),
            "attempt":         result.get("attempt"),
            "files_copied":    copy_r.get("copied", 0),
            "files_skipped":   copy_r.get("skipped", 0),
            "files_skipped_manifest": copy_r.get("skipped_manifest", 0),
            "files_indexed":   index_r.get("indexed_files", 0),
            "error_count":     len(result.get("errors") or []),
            "manifest_summary": result.get("manifest_summary"),
            "threats":         threat_entry,
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

    def find_duplicate_groups_in_latest_dir(self, device_dir: str | Path) -> List[Dict[str, Any]]:
        latest_dir = Path(device_dir) / "latest"
        if not latest_dir.exists():
            return []

        by_folder: Dict[Path, Dict[str, List[Path]]] = {}
        for path in latest_dir.rglob("*"):
            if not path.is_file():
                continue
            match = _DUPLICATE_SUFFIX_RE.match(path.stem)
            if not match:
                continue
            base_name = f"{match.group('base')}{path.suffix}"
            by_folder.setdefault(path.parent, {}).setdefault(base_name, []).append(path)

        groups = []
        for folder, suffix_map in by_folder.items():
            for base_name, suffixed_paths in suffix_map.items():
                original = folder / base_name
                if not original.exists():
                    continue
                try:
                    original_size = original.stat().st_size
                except OSError:
                    continue

                matching = []
                for p in suffixed_paths:
                    try:
                        if p.stat().st_size == original_size:
                            matching.append(p)
                    except OSError:
                        continue

                if matching:
                    groups.append({
                        "original": str(original),
                        "duplicates": [str(p) for p in matching],
                        "size_bytes": original_size,
                        "reclaimable_bytes": original_size * len(matching),
                    })

        return groups

    def merge_duplicate_groups_in_latest_dir(self, device_dir: str | Path) -> Dict[str, Any]:
        device_dir = Path(device_dir)
        groups = self.find_duplicate_groups_in_latest_dir(device_dir)

        if not groups:
            return {"removed_files": 0, "reclaimed_bytes": 0, "groups_merged": 0}

        latest_dir = device_dir / "latest"
        manifest = self._load_manifest(device_dir, device_dir.name.replace("_", " "))
        path_translation: Dict[str, str] = {}

        removed_files = 0
        reclaimed_bytes = 0

        for group in groups:
            original_rel = str(Path(group["original"]).relative_to(latest_dir))
            for dup_path_str in group["duplicates"]:
                dup_path = Path(dup_path_str)
                dup_rel = str(dup_path.relative_to(latest_dir))
                try:
                    size = dup_path.stat().st_size
                    dup_path.unlink()
                    removed_files += 1
                    reclaimed_bytes += size
                    path_translation[dup_rel] = original_rel
                except OSError:
                    continue

        if path_translation:
            for entry in manifest.get("files", {}).values():
                dest = entry.get("dest_path")
                if dest in path_translation:
                    entry["dest_path"] = path_translation[dest]
            self._save_manifest(device_dir, manifest)

        return {
            "removed_files": removed_files,
            "reclaimed_bytes": reclaimed_bytes,
            "groups_merged": len(groups),
        }

    def find_duplicate_groups_in_latest(self, device: PhoneDevice) -> List[Dict[str, Any]]:
        return self.find_duplicate_groups_in_latest_dir(self._device_dir(device))

    def merge_duplicate_groups_in_latest(self, device: PhoneDevice) -> Dict[str, Any]:
        return self.merge_duplicate_groups_in_latest_dir(self._device_dir(device))

    def clear_manifest_dir(self, device_dir: str | Path) -> None:
        path = self._manifest_path(Path(device_dir))
        try:
            if path.exists():
                path.unlink()
        except OSError:
            pass

    def clear_manifest(self, device: PhoneDevice) -> None:
        self.clear_manifest_dir(self._device_dir(device))

    def merge_old_backup_folders_into_latest(
        self,
        device_dir: Path,
        progress_callback: Optional[ProgressCallback] = None,
    ) -> None:
        """
        Scan device_dir for any old snapshot folders (or other backup directories),
        merge their files into 'latest/' while deduplicating them using MD5 and size,
        and clean up the old folders.
        """
        if not device_dir.exists():
            return

        latest_dir = device_dir / "latest"
        latest_dir.mkdir(parents=True, exist_ok=True)

        old_folders = []
        for child in device_dir.iterdir():
            if child.is_dir() and child.name not in {"latest", "logs", "_aegis_merge_logs"}:
                old_folders.append(child)

        if not old_folders:
            return

        if progress_callback:
            progress_callback(-2, f"Merging {len(old_folders)} old backup folders into latest...")

        # Helper to calculate MD5 hash
        def _calculate_file_md5(file_path: Path) -> str:
            hasher = hashlib.md5()
            try:
                with open(file_path, "rb") as f:
                    for chunk in iter(lambda: f.read(65536), b""):
                        hasher.update(chunk)
                return hasher.hexdigest()
            except OSError:
                return ""

        # Index existing files in latest_dir
        latest_files: Dict[str, int] = {}  # rel_path_lower -> size
        for root, _, filenames in os.walk(str(latest_dir)):
            for filename in filenames:
                path = Path(root) / filename
                try:
                    rel_path = path.relative_to(latest_dir).as_posix().lower()
                    latest_files[rel_path] = path.stat().st_size
                except OSError:
                    continue

        merged_count = 0
        deleted_duplicates = 0

        for old_folder in old_folders:
            if progress_callback:
                progress_callback(-2, f"Merging folder: {old_folder.name}")
            
            for root, _, filenames in os.walk(str(old_folder)):
                for filename in filenames:
                    file_path = Path(root) / filename
                    try:
                        rel_path = file_path.relative_to(old_folder).as_posix().lower()
                        size = file_path.stat().st_size
                        target_path = latest_dir / file_path.relative_to(old_folder)

                        if rel_path in latest_files:
                            if latest_files[rel_path] == size:
                                # Compare MD5 to confirm duplicate
                                hash_old = _calculate_file_md5(file_path)
                                hash_new = _calculate_file_md5(target_path)
                                if hash_old == hash_new and hash_old != "":
                                    # It's an exact duplicate. Delete.
                                    file_path.unlink()
                                    deleted_duplicates += 1
                                    continue
                                
                            # If sizes/hashes differ, keep the newer modified one
                            try:
                                target_mtime = target_path.stat().st_mtime
                                source_mtime = file_path.stat().st_mtime
                                if source_mtime > target_mtime:
                                    target_path.parent.mkdir(parents=True, exist_ok=True)
                                    shutil.move(str(file_path), str(target_path))
                                    latest_files[rel_path] = size
                                    merged_count += 1
                                else:
                                    file_path.unlink()
                                    deleted_duplicates += 1
                            except OSError:
                                pass
                        else:
                            # Move new file
                            target_path.parent.mkdir(parents=True, exist_ok=True)
                            shutil.move(str(file_path), str(target_path))
                            latest_files[rel_path] = size
                            merged_count += 1
                    except OSError:
                        continue

            # Delete the empty legacy folder structure
            try:
                shutil.rmtree(old_folder)
            except OSError:
                pass

        if progress_callback:
            progress_callback(-2, f"Merge complete. Merged: {merged_count}, Deduplicated: {deleted_duplicates}")


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

def _kill_process_tree(proc: "subprocess.Popen") -> None:
    """
    Kill *proc* and everything it spawned. `Popen.kill()` alone only ends the
    powershell.exe wrapper — Shell.Application's CopyHere() for MTP/WPD
    sources can hand the actual file operation off to a separate COM
    surrogate process, which then keeps running orphaned (still holding the
    phone's connection) after powershell.exe is gone. `taskkill /T` kills the
    whole process tree instead.
    """
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                capture_output=True,
                creationflags=subprocess.CREATE_NO_WINDOW,
                timeout=10,
            )
        else:
            proc.kill()
    except Exception:  # noqa: BLE001
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass


def _write_temp_script(body: str) -> str:
    """
    Write *body* to a temp .ps1 file and return its path.

    Must be UTF-8 WITH a BOM ("utf-8-sig"). Windows PowerShell 5.1 (the
    classic powershell.exe these scripts run under) only recognizes a script
    file as UTF-8 if it starts with a BOM — without one, it reads the file
    under the system ANSI codepage instead, silently mangling any non-ASCII
    byte. A single such character anywhere in the script — even inside a
    plain comment — can corrupt the whole parse and make the file fail to
    run at all, with no output the caller can distinguish from "the phone
    had nothing to back up".
    """
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".ps1", delete=False, encoding="utf-8-sig"
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


def _write_temp_skip_folders(keys: set) -> str:
    """Write the set of phone-relative folder keys to skip to a temp .json file."""
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False, encoding="utf-8"
    ) as f:
        json.dump(sorted(keys), f)
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
            if entry.is_file() and not any(
                part.startswith("_aegis_stage_") for part in entry.parts
            ):
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
