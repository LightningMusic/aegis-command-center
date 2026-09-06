"""
core/brightspace.py
===================
Brightspace (D2L) integration built around the learner's *personal calendar
feed* — the private iCal URL Brightspace generates under
Calendar -> Settings -> "Enable Calendar Feeds" / "Subscribe".

Why the feed instead of a scripted StarID login:
  * MinnState StarID sign-in is a multi-redirect SSO with Microsoft MFA;
    replaying a stored password headlessly is unreliable and against policy.
  * The feed URL carries its own bearer token, returns iCalendar over plain
    HTTPS, and contains every assignment / quiz / exam due date and event
    across all enrolled courses.

The feed URL is a secret (anyone with it can read your calendar) so it lives in
the DPAPI-encrypted SecureStore, never in config.json.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from core.ical import parse_calendar

_LE_VERSION = "1.82"          # calendar/events/myEvents route
_CALENDAR_PAST_DAYS = 14
_CALENDAR_FUTURE_DAYS = 200

_FEED_URL_RE = re.compile(
    r"^(?:https|webcal)://[^/]+/d2l/le/calendar/feed/user/feed\.ics\?\S+$",
    re.IGNORECASE,
)
_SECRET_KEY = "brightspace_feed_url"


class BrightspaceError(RuntimeError):
    pass


class BrightspaceClient:
    def __init__(self, config_manager, secure_store, oauth=None, session=None) -> None:
        self.config_manager = config_manager
        self.secure_store = secure_store
        self.oauth = oauth
        self.session = session

    # ── settings / secret ────────────────────────────────────────────────
    def get_settings(self) -> Dict[str, Any]:
        return self.config_manager.get_brightspace_settings()

    def is_enabled(self) -> bool:
        return bool(self.get_settings().get("enabled"))

    # ── which data path is live ──────────────────────────────────────────
    def has_session(self) -> bool:
        return bool(self.session is not None and self.session.is_active())

    def has_oauth(self) -> bool:
        return bool(self.oauth is not None and self.oauth.is_connected())

    def is_configured(self) -> bool:
        return self.has_session() or self.has_oauth() or self.has_feed()

    def connection_mode(self) -> str:
        if self.has_session():
            return "session"
        if self.has_oauth():
            return "oauth"
        if self.has_feed():
            return "feed"
        return "none"

    def has_feed(self) -> bool:
        try:
            return bool(self.secure_store.get(_SECRET_KEY))
        except Exception:
            return self.secure_store.has(_SECRET_KEY)

    def get_feed_url(self) -> Optional[str]:
        return self.secure_store.get(_SECRET_KEY)

    def set_feed_url(self, url: str) -> None:
        url = (url or "").strip()
        if not url:
            self.secure_store.delete(_SECRET_KEY)
            self.config_manager.update_brightspace_settings({"feed_host": ""})
            return
        if not _FEED_URL_RE.match(url):
            raise BrightspaceError(
                "That doesn't look like a Brightspace calendar feed link.\n\n"
                "In Brightspace: open Calendar, click Settings (or Subscribe), tick "
                "\"Enable Calendar Feeds\", and copy the address that ends in\n"
                "  /d2l/le/calendar/feed/user/feed.ics?token=…"
            )
        host = re.sub(r"^\w+://([^/]+)/.*$", r"\1", url)
        self.secure_store.set(_SECRET_KEY, url)
        self.config_manager.update_brightspace_settings({"feed_host": host})

    def clear_feed_url(self) -> None:
        self.set_feed_url("")

    # ── network ──────────────────────────────────────────────────────────
    def _download_feed(self, timeout: int = 20) -> str:
        url = self.get_feed_url()
        if not url:
            raise BrightspaceError(
                "No Brightspace calendar feed URL saved. Add it in Settings."
            )
        if url.lower().startswith("webcal://"):
            url = "https://" + url[len("webcal://"):]

        request = Request(url, headers={"User-Agent": "Aegis/1.0 (calendar sync)"})
        try:
            with urlopen(request, timeout=timeout) as response:
                charset = response.headers.get_content_charset() or "utf-8"
                body = response.read().decode(charset, errors="replace")
        except HTTPError as exc:
            if exc.code in (401, 403):
                raise BrightspaceError(
                    f"Brightspace rejected the feed token (HTTP {exc.code}). "
                    "Re-copy the feed URL from Brightspace and save it again."
                ) from exc
            raise BrightspaceError(f"Brightspace returned HTTP {exc.code}.") from exc
        except URLError as exc:
            raise BrightspaceError(f"Could not reach Brightspace: {exc.reason}") from exc
        except Exception as exc:  # noqa: BLE001 - surface anything odd cleanly
            raise BrightspaceError(f"Feed download failed: {exc}") from exc

        if "BEGIN:VCALENDAR" not in body:
            raise BrightspaceError(
                "The feed URL did not return calendar data. Double-check the link."
            )
        return body

    def test_connection(self) -> Tuple[bool, str]:
        mode = self.connection_mode()
        if mode in ("session", "oauth"):
            try:
                items = self._fetch_via_api_mode(mode)
            except Exception as exc:  # noqa: BLE001
                return False, str(exc)
            label = "browser sign-in" if mode == "session" else "OAuth"
            return True, f"Connected via {label} — {len(items)} upcoming item(s)."
        try:
            body = self._download_feed(timeout=15)
        except BrightspaceError as exc:
            return False, str(exc)
        return True, f"Connected — feed has {body.count('BEGIN:VEVENT')} calendar item(s)."

    # ── due items ────────────────────────────────────────────────────────
    def fetch_due_items(self) -> List[Dict[str, Any]]:
        """Every dated item, normalised for the sync layer. Prefers the browser
        session, then OAuth, then the personal iCal feed."""
        mode = self.connection_mode()
        if mode in ("session", "oauth"):
            return self._fetch_via_api_mode(mode)
        return self._fetch_via_feed()

    def _fetch_via_api_mode(self, mode: str) -> List[Dict[str, Any]]:
        if mode == "session":
            from core.brightspace_session import BrightspaceSessionError as ApiError
            transport, base = self.session.api_get, self.session.lms_url()
        else:
            from core.brightspace_oauth import BrightspaceOAuthError as ApiError
            transport, base = self.oauth.api_get, self.oauth.lms_url()

        try:
            return self._fetch_via_api(transport, base)
        except ApiError as exc:
            raise BrightspaceError(str(exc)) from exc

    def _fetch_via_api(self, api_get, base: str) -> List[Dict[str, Any]]:
        org_ids = _my_course_org_units(api_get)
        if not org_ids:
            return []

        now = datetime.now(timezone.utc)
        start = (now - timedelta(days=_CALENDAR_PAST_DAYS)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        end = (now + timedelta(days=_CALENDAR_FUTURE_DAYS)).strftime("%Y-%m-%dT%H:%M:%S.000Z")

        events: List[Dict[str, Any]] = []
        # myEvents caps the number of org units per call; page through in chunks.
        for chunk_start in range(0, len(org_ids), 20):
            chunk = org_ids[chunk_start:chunk_start + 20]
            payload = api_get(
                f"/d2l/api/le/{_LE_VERSION}/calendar/events/myEvents/",
                {
                    "orgUnitIdsCSV": ",".join(str(i) for i in chunk),
                    "startDateTime": start,
                    "endDateTime": end,
                },
            )
            if isinstance(payload, list):
                events.extend(payload)
            elif isinstance(payload, dict) and isinstance(payload.get("Items"), list):
                events.extend(payload["Items"])

        items: List[Dict[str, Any]] = []
        seen = set()
        for ev in events:
            eid = ev.get("CalendarEventId")
            if eid in seen:
                continue
            seen.add(eid)
            due = _pick_event_datetime(ev)
            if due is None:
                continue
            all_day = bool(ev.get("StartDay") or ev.get("EndDay")) and not (
                ev.get("StartDateTime") or ev.get("EndDateTime")
            )
            view = ev.get("CalendarEventViewUrl") or ""
            if view and view.startswith("/"):
                view = base + view
            course = (ev.get("OrgUnitCode") or ev.get("OrgUnitName") or "").strip()
            title = (ev.get("Title") or "").strip() or "Brightspace item"
            items.append(
                {
                    "external_id": f"brightspace:evt:{eid}",
                    "title": title,
                    "course": course,
                    "description": (ev.get("Description") or "").strip(),
                    "url": view or self._home_url(),
                    "due": due,
                    "all_day": all_day,
                    "kind": _classify_text(f"{title} {course}"),
                }
            )
        return items

    def _fetch_via_feed(self) -> List[Dict[str, Any]]:
        body = self._download_feed()
        items: List[Dict[str, Any]] = []

        for ev in parse_calendar(body):
            due = ev.end or ev.start
            if due is None:
                continue
            title, course = self._split_summary(ev)
            uid = ev.uid.strip()
            external_id = (
                f"brightspace:{uid}"
                if uid
                else f"brightspace:{title.lower()}:{due.isoformat()}"
            )
            items.append(
                {
                    "external_id": external_id,
                    "title": title,
                    "course": course,
                    "description": ev.description.strip(),
                    "url": ev.url or self._home_url(),
                    "due": due,
                    "all_day": ev.all_day,
                    "kind": self._classify(ev),
                }
            )
        return items

    @staticmethod
    def _split_summary(ev) -> Tuple[str, str]:
        summary = (ev.summary or "").strip()
        course = ev.categories[0] if ev.categories else ""
        if not course and " - " in summary:
            head, tail = summary.rsplit(" - ", 1)
            if head.strip():
                summary, course = head.strip(), tail.strip()
        if not course and ev.location:
            course = ev.location.strip()
        return summary or "Brightspace item", course

    @staticmethod
    def _classify(ev) -> str:
        return _classify_text(f"{ev.summary} {' '.join(ev.categories)}")

    def _home_url(self) -> str:
        base = (self.get_settings().get("lms_url") or "").rstrip("/")
        if base:
            return f"{base}/d2l/home"
        host = self.get_settings().get("feed_host")
        return f"https://{host}/d2l/home" if host else ""

    # ── dashboard snapshot ───────────────────────────────────────────────
    def get_dashboard_snapshot(self) -> Dict[str, Any]:
        s = self.get_settings()
        mode = self.connection_mode()
        return {
            "enabled": bool(s.get("enabled")),
            "configured": mode != "none",
            "mode": mode,
            "feed_host": s.get("feed_host", ""),
            "lms_url": s.get("lms_url", ""),
            "last_sync_at": s.get("last_sync_at", ""),
            "last_sync_status": s.get("last_sync_status", "Not connected"),
            "last_item_count": s.get("last_item_count", 0),
        }

    def record_status(self, message: str, item_count: Optional[int] = None) -> None:
        payload: Dict[str, Any] = {
            "last_sync_at": datetime.now().isoformat(timespec="seconds"),
            "last_sync_status": message,
        }
        if item_count is not None:
            payload["last_item_count"] = item_count
        self.config_manager.update_brightspace_settings(payload)


# ── module helpers ──────────────────────────────────────────────────────────
_LP_VERSION = "1.43"


def _my_course_org_units(api_get) -> List[int]:
    """Course-offering (type 3) org unit ids for the calling user, paged."""
    ids: List[int] = []
    bookmark = None
    route = f"/d2l/api/lp/{_LP_VERSION}/enrollments/myenrollments/"
    for _ in range(50):  # hard page cap
        page = api_get(route, {"orgUnitTypeId": 3, "bookmark": bookmark})
        items = page.get("Items", []) if isinstance(page, dict) else []
        for entry in items:
            org = (entry or {}).get("OrgUnit") or {}
            oid = org.get("Id")
            type_id = ((org.get("Type") or {}).get("Id"))
            if oid is not None and (type_id in (None, 3)):
                ids.append(int(oid))
        paging = page.get("PagingInfo", {}) if isinstance(page, dict) else {}
        if paging.get("HasMoreItems") and paging.get("Bookmark"):
            bookmark = paging["Bookmark"]
        else:
            break
    return ids


def _classify_text(text: str) -> str:
    blob = (text or "").lower()
    if any(k in blob for k in ("quiz", "exam", "test", "midterm", "final")):
        return "quiz"
    if any(k in blob for k in ("assignment", "dropbox", "submission", "homework", "essay", "paper")):
        return "assignment"
    if "discussion" in blob:
        return "discussion"
    if "checklist" in blob:
        return "checklist"
    return "event"


def _parse_d2l_dt(value: str) -> Optional[datetime]:
    if not value:
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        try:
            dt = datetime.strptime(value.strip()[:19], "%Y-%m-%dT%H:%M:%S")
        except ValueError:
            return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _pick_event_datetime(ev: Dict[str, Any]) -> Optional[datetime]:
    for key in ("EndDateTime", "StartDateTime", "EndDay", "StartDay"):
        parsed = _parse_d2l_dt(ev.get(key) or "")
        if parsed is not None:
            return parsed
    return None
