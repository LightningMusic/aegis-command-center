"""
core/threat_scan.py
===================
Ask Microsoft Defender what it thinks of files this app has just copied.

The point is not to duplicate Defender. Real-time protection already scans
files as they are written, which is why a modded APK pulled off a phone gets
flagged the moment it lands. The gap is that the detection goes into
Defender's history and nothing in this app ever mentions it — so a flagged
file sits in the backup indefinitely and the next device to read that backup
gets handed it.

So the primary check here reads Defender's own verdicts for files under a
given folder, which is one query and needs no rescanning. An explicit scan is
available for the cases real-time protection does not cover (it was off, the
files predate it, or you just want to be sure).

Nothing in this module deletes or quarantines anything. It reports.
"""

from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timezone
from typing import Dict, List, Optional

# Defender records each detection against one or more "resources", written
# as "file:_C:\path\to\thing". Also seen: containerfile:_ for archives.
_RESOURCE_PREFIXES = ("file:_", "containerfile:_", "process:_")

_PS = ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command"]

# Defender's SeverityID. 5 is what it uses for the serious end of the scale;
# 1 is the "potentially unwanted" end.
SEVERITY_LABELS = {
    0: "unknown",
    1: "low",
    2: "moderate",
    3: "high",
    4: "high",
    5: "severe",
}

_LIST_DETECTIONS = r"""
$ErrorActionPreference = 'Stop'
$threats = @{}
try { Get-MpThreat | ForEach-Object { $threats[[string]$_.ThreatID] = $_ } } catch {}
$out = @()
try {
    foreach ($d in Get-MpThreatDetection) {
        $t = $threats[[string]$d.ThreatID]
        foreach ($r in @($d.Resources)) {
            $out += [PSCustomObject]@{
                time     = $(if ($d.InitialDetectionTime) { $d.InitialDetectionTime.ToString('o') } else { '' })
                resource = [string]$r
                threat   = $(if ($t) { [string]$t.ThreatName } else { '' })
                severity = $(if ($t) { [int]$t.SeverityID } else { 0 })
                action   = [string]$d.CleanupActionID
            }
        }
    }
} catch {}
if ($out.Count -eq 0) { '[]' } else { $out | ConvertTo-Json -Compress -Depth 3 }
"""


def _run_ps(script: str, timeout: int = 120) -> str:
    try:
        proc = subprocess.run(
            _PS + [script],
            capture_output=True, text=True, timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return (proc.stdout or "").strip()
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return ""


def defender_available() -> bool:
    """Is Defender present and reporting status on this machine?"""
    if os.name != "nt":
        return False
    out = _run_ps(
        "try { (Get-MpComputerStatus).AMServiceEnabled } catch { 'False' }", timeout=60
    )
    return out.strip().lower().startswith("true")


def realtime_protection_on() -> Optional[bool]:
    """True/False, or None if it couldn't be determined."""
    if os.name != "nt":
        return None
    out = _run_ps(
        "try { (Get-MpComputerStatus).RealTimeProtectionEnabled } catch { '' }", timeout=60
    ).strip().lower()
    if out.startswith("true"):
        return True
    if out.startswith("false"):
        return False
    return None


def _before(moment: datetime, cutoff: datetime) -> bool:
    """
    Is *moment* earlier than *cutoff*, without tripping over tzinfo?

    Defender hands back offset-aware times; a caller passing a cutoff is
    holding whatever datetime.now() gave them, which is naive. Comparing the
    two raises TypeError, so both get normalised before the comparison.
    """
    if (moment.tzinfo is None) != (cutoff.tzinfo is None):
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        else:
            cutoff = cutoff.replace(tzinfo=timezone.utc)
    return moment < cutoff


def _clean_resource(resource: str) -> str:
    for prefix in _RESOURCE_PREFIXES:
        if resource.lower().startswith(prefix):
            return resource[len(prefix):]
    return resource


def list_detections(
    under_path: Optional[str] = None,
    *,
    since: Optional[datetime] = None,
    one_per_file: bool = True,
    timeout: int = 120,
) -> List[Dict]:
    """
    Defender's recorded detections, worst first.

    *under_path* keeps only detections whose file sits inside that folder —
    so a backup run can ask "did anything I just wrote get flagged" without
    being handed every detection on the machine.

    Defender logs an event per *encounter*, not per file, so one stubborn APK
    that gets recopied on every backup run appears once per run. With
    *one_per_file* the worst/newest entry wins and the rest are folded into
    its ``times_seen``, because the question being answered is "what is wrong
    with these files", not "how often has Defender noticed".
    """
    if os.name != "nt":
        return []

    raw = _run_ps(_LIST_DETECTIONS, timeout=timeout)
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if isinstance(data, dict):
        data = [data]

    prefix = ""
    if under_path:
        prefix = os.path.abspath(under_path).lower().rstrip("\\") + "\\"

    out: List[Dict] = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        path = _clean_resource(str(entry.get("resource") or ""))
        if not path:
            continue
        if prefix and not path.lower().startswith(prefix):
            continue

        detected_at = None
        raw_time = str(entry.get("time") or "")
        if raw_time:
            try:
                detected_at = datetime.fromisoformat(raw_time.replace("Z", "+00:00"))
            except ValueError:
                detected_at = None
        if since and detected_at and _before(detected_at, since):
            continue

        severity = int(entry.get("severity") or 0)
        out.append({
            "path": path,
            "name": os.path.basename(path),
            "threat": str(entry.get("threat") or "unknown"),
            "severity": severity,
            "severity_label": SEVERITY_LABELS.get(severity, "unknown"),
            "detected_at": detected_at.isoformat() if detected_at else "",
            "action": str(entry.get("action") or ""),
            "still_present": os.path.exists(path),
        })

    # Worst first, and within one severity the most recent first: the thing
    # you most need to look at should not be buried under a year of old
    # low-level detections.
    out.sort(key=lambda d: (d["severity"], d["detected_at"]), reverse=True)

    if one_per_file:
        best: Dict[str, Dict] = {}
        for d in out:  # already worst-first, so the first of each path wins
            key = d["path"].lower()
            if key in best:
                best[key]["times_seen"] += 1
                first = best[key]["first_detected_at"]
                if d["detected_at"] and (not first or d["detected_at"] < first):
                    best[key]["first_detected_at"] = d["detected_at"]
            else:
                d["times_seen"] = 1
                d["first_detected_at"] = d["detected_at"]
                best[key] = d
        out = [d for d in out if d.get("times_seen")]

    return out


def scan_path(path: str, *, timeout: int = 1800) -> Dict:
    """
    Run Defender over one file or folder and report the verdict.

    For when real-time protection can't be relied on — it was disabled, or
    the files were written before it was watching.
    """
    if os.name != "nt":
        return {"scanned": False, "reason": "Defender is Windows-only"}
    if not os.path.exists(path):
        return {"scanned": False, "reason": f"path does not exist: {path}"}

    escaped = path.replace("'", "''")
    out = _run_ps(
        f"try {{ Start-MpScan -ScanType CustomScan -ScanPath '{escaped}'; 'OK' }} "
        f"catch {{ 'ERR: ' + $_.Exception.Message }}",
        timeout=timeout,
    )
    if out.startswith("ERR:"):
        return {"scanned": False, "reason": out[4:].strip()}
    return {"scanned": True, "reason": "", "path": path}


def detected_since(detections: List[Dict], cutoff: datetime) -> List[Dict]:
    """
    The subset of *detections* Defender recorded at or after *cutoff*.

    Kept here rather than in the caller so the naive/aware datetime handling
    lives in one place alongside the code that parses Defender's timestamps.
    """
    out = []
    for d in detections:
        raw = d.get("detected_at") or ""
        if not raw:
            continue
        try:
            when = datetime.fromisoformat(raw)
        except ValueError:
            continue
        if not _before(when, cutoff):
            out.append(d)
    return out


def summarize(detections: List[Dict]) -> str:
    """One-line summary suitable for a log or a status bar."""
    if not detections:
        return "Defender reported nothing on the files in this backup."
    worst = detections[0]
    still = sum(1 for d in detections if d["still_present"])
    bits = [f"{len(detections)} file(s) flagged by Defender"]
    bits.append(f"worst: {worst['threat']} ({worst['severity_label']})")
    if still:
        bits.append(f"{still} still on disk")
    return " — ".join(bits)
