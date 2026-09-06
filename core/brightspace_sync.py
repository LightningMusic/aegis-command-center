"""
core/brightspace_sync.py
========================
Ties the Brightspace feed to Aegis tasks (and, if configured, Google Calendar).

`run()` is self-contained and safe to call from a worker thread: it does the
network fetch, upserts one task per dated feed item (keyed by a stable external
id so completed items keep their state and nothing duplicates), removes tasks
for items that vanished from the feed, and returns a short status string.
"""

from __future__ import annotations

from typing import Any, Dict, Tuple

from core.brightspace import BrightspaceClient, BrightspaceError


class BrightspaceSync:
    SOURCE = "brightspace"

    def __init__(self, client: BrightspaceClient, task_manager, google_calendar=None) -> None:
        self.client = client
        self.task_manager = task_manager
        self.google_calendar = google_calendar

    def run(self) -> Tuple[bool, str, Dict[str, Any]]:
        if not self.client.is_enabled():
            return False, "Brightspace sync is turned off in Settings.", {}
        if not self.client.is_configured():
            return False, "Brightspace is not connected yet (browser sign-in, OAuth, or feed URL).", {}

        try:
            items = self.client.fetch_due_items()
        except BrightspaceError as exc:
            self.client.record_status(f"Failed: {exc}")
            return False, str(exc), {}

        created = updated = 0
        keep = []
        for item in items:
            keep.append(item["external_id"])
            description = item["description"]
            if item["url"]:
                description = (description + "\n\n" + item["url"]).strip()

            outcome = self.task_manager.upsert_external_task(
                external_id=item["external_id"],
                source=self.SOURCE,
                title=item["title"],
                due_date=item["due"].date().isoformat(),
                description=description,
                category=item["course"] or "Brightspace",
            )
            if outcome == "created":
                created += 1
            elif outcome == "updated":
                updated += 1

        removed = self.task_manager.prune_external(self.SOURCE, keep)

        gcal_note = ""
        if self.google_calendar is not None and self.google_calendar.is_configured():
            try:
                pushed = self.google_calendar.push_items(items)
                gcal_note = f" · Google Calendar: {pushed} event(s)"
            except Exception as exc:  # noqa: BLE001 - report, don't abort the task sync
                gcal_note = f" · Google Calendar failed: {exc}"

        status = (
            f"{len(items)} item(s): +{created} new, {updated} updated, "
            f"{removed} removed{gcal_note}"
        )
        self.client.record_status(status, item_count=len(items))
        return True, status, {
            "total": len(items),
            "created": created,
            "updated": updated,
            "removed": removed,
        }
