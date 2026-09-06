"""
core/google_calendar.py
=======================
Push Brightspace due items into Google Calendar using nothing but stdlib HTTP.

Setup the user does once (Settings -> Google):
  * Create an OAuth client (Desktop) in Google Cloud, enable the Calendar API.
  * Obtain a refresh token for the Calendar scope and paste client id / secret /
    refresh token into Settings.
  * Optionally set a target calendar id (defaults to "primary").

Each Brightspace item maps to one event with a deterministic id derived from the
item's external id, so re-syncing updates in place instead of duplicating.
"""

from __future__ import annotations

import hashlib
import json
import time
from datetime import timedelta, timezone
from typing import Any, Dict, List, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

_TOKEN_URL = "https://oauth2.googleapis.com/token"
_API = "https://www.googleapis.com/calendar/v3"


class GoogleCalendarError(RuntimeError):
    pass


class GoogleCalendarSync:
    def __init__(self, config_manager, secure_store=None, google_oauth=None) -> None:
        self.config_manager = config_manager
        self.secure_store = secure_store
        self.google_oauth = google_oauth
        self._access_token: Optional[str] = None
        self._expires_at = 0.0

    # ── config ───────────────────────────────────────────────────────────
    def _cfg(self) -> Dict[str, Any]:
        return self.config_manager.get_google_workspace_settings()

    def is_configured(self) -> bool:
        c = self._cfg()
        if not c.get("enabled"):
            return False
        if self.google_oauth is not None and self.google_oauth.is_connected():
            return True
        return bool(c.get("client_id") and c.get("client_secret") and c.get("refresh_token"))

    def calendar_id(self) -> str:
        return (self._cfg().get("calendar_id") or "primary").strip() or "primary"

    # ── auth ─────────────────────────────────────────────────────────────
    def _token(self) -> str:
        # Preferred: the "Sign in with Google" flow, which keeps its own
        # encrypted refresh token and access-token cache.
        if self.google_oauth is not None and self.google_oauth.is_connected():
            try:
                return self.google_oauth.access_token()
            except Exception as exc:  # noqa: BLE001
                raise GoogleCalendarError(str(exc)) from exc

        if self._access_token and time.time() < self._expires_at - 60:
            return self._access_token

        c = self._cfg()
        if not (c.get("client_id") and c.get("client_secret") and c.get("refresh_token")):
            raise GoogleCalendarError("Sign in with Google in Settings first.")
        data = urlencode(
            {
                "client_id": c["client_id"],
                "client_secret": c["client_secret"],
                "refresh_token": c["refresh_token"],
                "grant_type": "refresh_token",
            }
        ).encode("utf-8")
        req = Request(
            _TOKEN_URL,
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        try:
            with urlopen(req, timeout=20) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except HTTPError as exc:
            raise GoogleCalendarError(
                f"Google token refresh failed (HTTP {exc.code}). Re-check the "
                "client id, secret and refresh token in Settings."
            ) from exc
        except URLError as exc:
            raise GoogleCalendarError(f"Could not reach Google: {exc.reason}") from exc

        self._access_token = payload["access_token"]
        self._expires_at = time.time() + int(payload.get("expires_in", 3600))
        return self._access_token

    # ── push ─────────────────────────────────────────────────────────────
    @staticmethod
    def _event_id(external_id: str) -> str:
        # Google event ids allow base32hex chars (0-9a-v); a hex digest fits.
        return "aegis" + hashlib.sha1(external_id.encode("utf-8")).hexdigest()

    def push_items(self, items: List[Dict[str, Any]]) -> int:
        if not self.is_configured():
            return 0
        token = self._token()
        cal = self.calendar_id()
        pushed = 0
        for item in items:
            if self._upsert(cal, self._event_id(item["external_id"]),
                            self._to_event(item), token):
                pushed += 1
        return pushed

    @staticmethod
    def _to_event(item: Dict[str, Any]) -> Dict[str, Any]:
        due = item["due"]
        summary = item["title"]
        if item.get("course"):
            summary = f"{summary} ({item['course']})"

        description = item.get("description", "") or ""
        if item.get("url"):
            description = (description + "\n" + item["url"]).strip()

        event: Dict[str, Any] = {"summary": summary, "description": description}

        if item.get("all_day"):
            day = due.date().isoformat()
            event["start"] = {"date": day}
            event["end"] = {"date": day}
        else:
            when = due if due.tzinfo else due.replace(tzinfo=timezone.utc)
            start = when - timedelta(minutes=30)
            event["start"] = {"dateTime": start.isoformat(), "timeZone": "UTC"}
            event["end"] = {"dateTime": when.isoformat(), "timeZone": "UTC"}

        return event

    def _upsert(self, cal: str, event_id: str, body: Dict[str, Any], token: str) -> bool:
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        payload = json.dumps(body).encode("utf-8")
        patch_url = f"{_API}/calendars/{cal}/events/{event_id}"

        try:
            with urlopen(Request(patch_url, data=payload, headers=headers, method="PATCH"),
                         timeout=20):
                return True
        except HTTPError as exc:
            if exc.code not in (404, 410):
                raise GoogleCalendarError(
                    f"Google Calendar update failed (HTTP {exc.code})."
                ) from exc
        except URLError as exc:
            raise GoogleCalendarError(f"Could not reach Google: {exc.reason}") from exc

        insert_url = f"{_API}/calendars/{cal}/events"
        with_id = dict(body, id=event_id)
        try:
            with urlopen(
                Request(insert_url, data=json.dumps(with_id).encode("utf-8"),
                        headers=headers, method="POST"),
                timeout=20,
            ):
                return True
        except HTTPError as exc:
            if exc.code == 409:  # already there (race) — fine
                return True
            raise GoogleCalendarError(
                f"Google Calendar insert failed (HTTP {exc.code})."
            ) from exc
        except URLError as exc:
            raise GoogleCalendarError(f"Could not reach Google: {exc.reason}") from exc
