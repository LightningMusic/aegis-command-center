"""
core/media_logging.py
=====================
Silence the FFmpeg chatter that Qt's multimedia backend prints to the terminal.

Opening any video in the preview panel used to dump a stream report to stdout:

    Input #0, mov,mp4,m4a,3gp,3g2,mj2, from '…':
      Duration: 00:32:51.97, start: 0.000000, bitrate: 7655 kb/s
      Stream #0:0[0x1](und): Video: h264 (High) …
    [mp3float @ 0000…] Could not update timestamps for skipped samples.

That is FFmpeg's own av_log at info/warning level, not Qt categorised logging,
so QT_LOGGING_RULES does not fully reach it -- setting the Qt rule removes the
"Input #0" block but leaves the per-decoder warnings. The fix is to lower the
level on the avutil library Qt has bundled, which keeps genuine errors while
dropping the routine reporting. A terminal that is quiet until something breaks
is far more useful than one that is always talking.

The external ffmpeg binary that core.integrity shells out to is unaffected: it
runs in its own process with -v error and its stderr captured, so corruption
detection keeps working exactly as before.

Set AEGIS_FFMPEG_LOG to get the noise back when debugging playback:
    quiet (silent) | panic | fatal | error (default) | warning | info | verbose | debug
"""

from __future__ import annotations

import ctypes
import glob
import os
import sys

# ffmpeg's libavutil log levels.
LEVELS = {
    "quiet": -8, "panic": 0, "fatal": 8, "error": 16,
    "warning": 24, "info": 32, "verbose": 40, "debug": 48, "trace": 56,
}
DEFAULT_LEVEL = "error"

_applied = False


def _avutil_paths() -> list[str]:
    """Where Qt's bundled avutil might be, newest soname first."""
    try:
        import PyQt6

        base = os.path.dirname(PyQt6.__file__)
    except ImportError:
        return []

    patterns = []
    if sys.platform == "win32":
        patterns.append(os.path.join(base, "Qt6", "bin", "avutil-*.dll"))
    elif sys.platform == "darwin":
        patterns.append(os.path.join(base, "Qt6", "lib", "libavutil*.dylib"))
    else:
        patterns.append(os.path.join(base, "Qt6", "lib", "libavutil.so*"))

    found: list[str] = []
    for pat in patterns:
        found.extend(sorted(glob.glob(pat), reverse=True))
    return found


def quiet_ffmpeg(level: str | int | None = None) -> str:
    """Lower FFmpeg's log level in this process. Returns a one-line status.

    Safe to call more than once and never raises: failing to quieten a log is
    not a reason to stop the app starting.
    """
    global _applied

    if level is None:
        level = os.environ.get("AEGIS_FFMPEG_LOG", DEFAULT_LEVEL)
    if isinstance(level, str):
        key = level.strip().lower()
        if key not in LEVELS:
            return f"unknown level {level!r}; expected one of {', '.join(LEVELS)}"
        numeric = LEVELS[key]
        name = key
    else:
        numeric, name = int(level), str(level)

    if numeric >= LEVELS["info"]:
        _applied = True
        return f"left FFmpeg logging at {name} (as requested)"

    candidates = _avutil_paths()
    if not candidates:
        return "could not locate Qt's bundled avutil; FFmpeg logging unchanged"

    for path in candidates:
        try:
            lib = ctypes.CDLL(path)
            lib.av_log_set_level(ctypes.c_int(numeric))
            lib.av_log_get_level.restype = ctypes.c_int
            if lib.av_log_get_level() == numeric:
                _applied = True
                return f"FFmpeg logging set to {name} via {os.path.basename(path)}"
        except (OSError, AttributeError):
            continue
    return "found avutil but could not set its log level; FFmpeg logging unchanged"


def quiet_qt_multimedia_rules() -> None:
    """Add the Qt-side multimedia rule to QT_LOGGING_RULES, keeping anything
    already configured. Must run before Qt initialises its backend.

    Skipped when AEGIS_FFMPEG_LOG asks for info or louder: the Qt category
    carries the "Input #0 … Stream #0:0" dump, so leaving the rule in place
    would make the escape hatch only half work and the detail it is there to
    restore would still be missing.
    """
    wanted = os.environ.get("AEGIS_FFMPEG_LOG", DEFAULT_LEVEL).strip().lower()
    if LEVELS.get(wanted, LEVELS[DEFAULT_LEVEL]) >= LEVELS["info"]:
        return

    rule = "qt.multimedia.*=false"
    existing = os.environ.get("QT_LOGGING_RULES", "")
    parts = [p for p in existing.replace("\n", ";").split(";") if p.strip()]
    if rule not in parts:
        parts.append(rule)
    os.environ["QT_LOGGING_RULES"] = ";".join(parts)
