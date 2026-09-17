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

# "<lowercase filename>|<size>" -> 1, for every file already confirmed
# backed up anywhere on the phone (any folder). Lets a file the phone has
# moved to a different folder since the last run be recognized as already
# covered instead of re-transferred.
$nameSizeIndex = @{}
if ($NameSizeIndexPath -and (Test-Path $NameSizeIndexPath)) {
    try {
        $raw = Get-Content $NameSizeIndexPath -Raw -ErrorAction Stop
        if ($raw) {
            $parsed = $raw | ConvertFrom-Json -ErrorAction Stop
            if ($parsed) {
                foreach ($prop in $parsed.PSObject.Properties) {
                    $nameSizeIndex[$prop.Name] = $true
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
        $currentSig = Get-FolderSignature (@($itemList | ForEach-Object { $_.Name }))
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
                if ($null -eq $childFolder) {
                    $script:errors++
                    Write-Host "ERR:$($Label)\$($safeName):Could not open subfolder"
                    [Console]::Out.Flush()
                    continue
                }
                CopyMTPFolderObject $childFolder "$Label\$safeName" $relKey (Join-Path $Dst $safeName)
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

            if (-not $ownPathKnown -and -not $ForceVerify -and $expectedSize -gt 0) {
                # Never seen at this exact path before -- but if a file with
                # the same name AND size is already confirmed backed up
                # somewhere else on the phone, this is almost certainly that
                # same file after Android moved/reorganized it (WhatsApp
                # media folders do this often). Trust it at the same level
                # the rest of this manifest already does, instead of paying
                # to re-transfer content that's already safely backed up.
                $xrefKey = "$($item.Name.ToLower())|$expectedSize"
                if ($nameSizeIndex.ContainsKey($xrefKey)) {
                    $script:skipped++
                    Write-Host "FILE_SKIP_ELSEWHERE:$relKey"
                    [Console]::Out.Flush()
                    continue
                }
            }

            $destFile = Join-Path $Dst $safeName
            if (Test-Path $destFile) {
                # Already present. Only re-fetch if we can prove it's short.
                $haveSize = -1
                try { $haveSize = [int64](Get-Item -LiteralPath $destFile -ErrorAction Stop).Length } catch { $haveSize = -1 }
                if ($expectedSize -gt 0 -and $haveSize -ge 0 -and $haveSize -lt $expectedSize) {
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
        force_verify: bool = False,
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

        *force_verify*, if True (MTP devices only), turns off the last-resort
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

        result: Dict[str, Any] = {
            "device":            device.display_name,
            "device_folder":     str(device_dir),
            "latest_dir":        str(latest_dir),
            "started_at":        datetime.now().isoformat(),
            "completed_at":      None,
            "cancelled":         False,
            "stalled":           False,
            "connection_failed": False,
            "stage":             "copy",
            "copy_result":       None,
            "index_result":      None,
            "organize_result":   None,
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
        if progress_callback:
            progress_callback(-3, "Indexing preserved phone folders...")

        index_result = self._index_existing_backup_tree(manifest, latest_dir)
        result["index_result"] = index_result
        self._save_manifest(device_dir, manifest)
        result["manifest_summary"] = self._summarize_manifest(manifest)

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

    # ── Backup (self-reliant, retries automatically) ────────────────────────

    MAX_RETRY_BACKOFF_SECONDS = 120
    DEVICE_POLL_INTERVAL_SECONDS = 5

    def backup_phone_until_complete(
        self,
        device: PhoneDevice,
        progress_callback: Optional[ProgressCallback] = None,
        should_stop: Optional[StopCallback] = None,
        max_attempts: int = DEFAULT_MAX_RETRY_ATTEMPTS,
        max_total_seconds: int = DEFAULT_MAX_TOTAL_RUNTIME_SECONDS,
        force_verify: bool = False,
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
            if made_progress:
                wait_seconds = 3
            else:
                wait_seconds = min(10 * stall_streak, self.MAX_RETRY_BACKOFF_SECONDS)
            if progress_callback:
                progress_callback(
                    -3,
                    f"Phone {reason} after {int(copy_result.get('copied') or 0)} file(s) this "
                    f"pass — retrying (attempt {attempt + 1})…",
                )

            deadline = time.monotonic() + wait_seconds
            refreshed = None
            while time.monotonic() < deadline:
                if should_stop and should_stop():
                    break
                refreshed = self._reacquire_device(current_device)
                if refreshed:
                    break
                time.sleep(min(self.DEVICE_POLL_INTERVAL_SECONDS, 2))

            # Always re-detect once more before retrying — Windows often hands the
            # phone a fresh MTP shell path after even a momentary drop, and the
            # stale one will just fail again.
            if not refreshed:
                refreshed = self._reacquire_device(current_device)

            if refreshed:
                current_device = refreshed
                if progress_callback:
                    progress_callback(-3, "Phone detected again — resuming…")
            elif progress_callback:
                progress_callback(-3, "Phone still not detected — trying the existing connection anyway…")

        last_result["attempts_used"] = last_result.get("attempt", 1)
        last_result["interruption_count"] = interruption_count
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
        """{relative_path: {"size": int, "skip": bool}} for the copy stage to consult."""
        lookup: Dict[str, Dict[str, Any]] = {}
        for relkey, entry in manifest.get("files", {}).items():
            if entry.get("skip"):
                lookup[relkey] = {"size": entry.get("size", 0), "skip": True}
            elif entry.get("status") == "ok":
                lookup[relkey] = {"size": entry.get("size", 0), "skip": False}
        return lookup

    def _build_name_size_index(self, manifest: Dict[str, Any]) -> Dict[str, int]:
        """
        {"<lowercase filename>|<size>": 1} for every confirmed-ok file,
        regardless of which folder it's in. Lets the copy script recognize a
        file the phone has moved to a different folder (e.g. Android
        reorganizing WhatsApp media) as already backed up, instead of
        re-transferring content that's already been confirmed good — matched
        by name+size only, the same trust level the rest of this manifest
        already relies on, just applied across the whole phone instead of
        one exact path.
        """
        index: Dict[str, int] = {}
        for relkey, entry in manifest.get("files", {}).items():
            if entry.get("status") != "ok":
                continue
            size = entry.get("size") or 0
            if not size:
                continue
            name = relkey.rsplit("\\", 1)[-1].lower()
            index[f"{name}|{size}"] = 1
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

        if not latest_dir.exists():
            return {"indexed_files": 0, "indexed_bytes": 0}

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

            files[rel] = {
                **files.get(rel, {}),
                "size": stat.st_size,
                "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(),
                "source_relative": rel,
                "dest_path": rel,
                "status": "ok",
                "indexed_at": now,
                "fail_count": 0,
            }
            indexed += 1
            total_bytes += stat.st_size

        return {"indexed_files": indexed, "indexed_bytes": total_bytes}

    def _summarize_manifest(self, manifest: Dict[str, Any]) -> Dict[str, int]:
        ok = 0
        failed_pending = 0
        skipped_permanent = 0
        confirmed_bytes = 0
        for entry in manifest.get("files", {}).values():
            if entry.get("skip"):
                skipped_permanent += 1
            elif entry.get("status") == "ok":
                ok += 1
                confirmed_bytes += int(entry.get("size") or 0)
            elif entry.get("status") == "failed":
                failed_pending += 1
        return {
            "confirmed_files": ok,
            "confirmed_bytes": confirmed_bytes,
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
        skip_folder_keys: Optional[set] = None,
        force_verify: bool = False,
        folder_sig_lookup: Optional[Dict[str, Dict[str, Any]]] = None,
        name_size_index: Optional[Dict[str, int]] = None,
    ) -> Dict[str, Any]:
        result: Dict[str, Any] = {
            "copied": 0, "skipped": 0, "skipped_manifest": 0, "skipped_elsewhere": 0,
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
                    fname = Path(line[8:]).name
                    if progress_callback:
                        progress_callback(-1, f"Copied: {fname}")

                elif line.startswith("FILE_OK_UNSIZED:"):
                    # Copied, but the phone never reported a size for this
                    # file, so "it reached the expected byte count" was never
                    # a check we could actually make — the only sign of
                    # success was "the bytes stopped flowing". Queue it for a
                    # real local integrity check once the transfer is done.
                    path = line[17:]
                    result["copied"] += 1
                    unverified_size_paths.append(path)
                    if progress_callback:
                        progress_callback(-1, f"Copied: {Path(path).name}")

                elif line.startswith("FILE_SKIP_MANIFEST:"):
                    result["skipped"] += 1
                    result["skipped_manifest"] += 1

                elif line.startswith("FILE_SKIP_ELSEWHERE:"):
                    # Never seen at this exact path before, but a file with
                    # the same name+size is already confirmed backed up
                    # somewhere else on the phone (Android reorganized it).
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
        for path, reason in bad[:20]:
            result["errors"].append(
                f"Copied but failed a local integrity check ({reason}) — "
                f"deleted so it's re-copied properly next run: {Path(path).name}"
            )
        for path, _reason in bad:
            try:
                relkey = os.path.relpath(path, dest)
                result["failed_keys"].append(relkey)
            except ValueError:
                pass
            try:
                os.unlink(path)
            except OSError:
                pass

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
