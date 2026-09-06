"""
core/ical.py
============
Just enough of RFC 5545 to read a Brightspace personal calendar feed:
line-unfolding, property parameters, DATE / DATE-TIME / UTC values, and text
un-escaping. No third-party dependency.

    for ev in parse_calendar(ics_text):
        print(ev.summary, ev.start, ev.all_day)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List, Optional, Tuple


@dataclass
class VEvent:
    uid: str = ""
    summary: str = ""
    description: str = ""
    location: str = ""
    url: str = ""
    categories: List[str] = field(default_factory=list)
    start: Optional[datetime] = None
    end: Optional[datetime] = None
    all_day: bool = False


def _unfold(text: str) -> List[str]:
    raw = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    lines: List[str] = []
    for line in raw:
        if line[:1] in (" ", "\t") and lines:
            lines[-1] += line[1:]
        else:
            lines.append(line)
    return lines


def _unescape(value: str) -> str:
    out: List[str] = []
    i = 0
    mapping = {"n": "\n", "N": "\n", ",": ",", ";": ";", "\\": "\\"}
    while i < len(value):
        ch = value[i]
        if ch == "\\" and i + 1 < len(value):
            out.append(mapping.get(value[i + 1], value[i + 1]))
            i += 2
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def _parse_dt(value: str, params: dict) -> Tuple[Optional[datetime], bool]:
    value = value.strip()
    is_date = params.get("VALUE", "").upper() == "DATE" or (
        len(value) == 8 and value.isdigit()
    )
    try:
        if is_date:
            return datetime.strptime(value, "%Y%m%d"), True
        if value.endswith("Z"):
            return (
                datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc),
                False,
            )
        # Local or TZID-qualified datetime. We don't ship a timezone database,
        # so keep it naive — callers treat naive feed times as UTC, which is
        # what Brightspace emits in practice.
        return datetime.strptime(value, "%Y%m%dT%H%M%S"), False
    except ValueError:
        return None, is_date


def parse_calendar(text: str) -> List[VEvent]:
    events: List[VEvent] = []
    current: Optional[VEvent] = None

    for line in _unfold(text):
        if not line:
            continue
        if line == "BEGIN:VEVENT":
            current = VEvent()
            continue
        if line == "END:VEVENT":
            if current is not None:
                events.append(current)
            current = None
            continue
        if current is None or ":" not in line:
            continue

        name_part, value = line.split(":", 1)
        pieces = name_part.split(";")
        name = pieces[0].upper()
        params: dict = {}
        for p in pieces[1:]:
            if "=" in p:
                k, v = p.split("=", 1)
                params[k.upper()] = v.strip('"')

        if name == "UID":
            current.uid = value.strip()
        elif name == "SUMMARY":
            current.summary = _unescape(value)
        elif name == "DESCRIPTION":
            current.description = _unescape(value)
        elif name == "LOCATION":
            current.location = _unescape(value)
        elif name == "URL":
            current.url = value.strip()
        elif name == "CATEGORIES":
            current.categories = [
                c.strip() for c in _unescape(value).split(",") if c.strip()
            ]
        elif name == "DTSTART":
            current.start, current.all_day = _parse_dt(value, params)
        elif name == "DTEND":
            current.end, _ = _parse_dt(value, params)

    return events
