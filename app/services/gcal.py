"""Google Calendar sync — best-effort, never raises to the caller.

Requires env vars:
  GCAL_SERVICE_ACCOUNT_JSON  Full contents of a service account JSON file.
  GCAL_CALENDAR_ID           Target calendar ID (default: "primary").

If GCAL_SERVICE_ACCOUNT_JSON is empty, all functions return None / empty list
so the scheduling agent works in development without credentials.
"""

import json
import logging
import os
from datetime import datetime

logger = logging.getLogger("gcal")

_SCOPES = ["https://www.googleapis.com/auth/calendar"]

_service_cache = None
_service_built = False


def _get_service():
    global _service_cache, _service_built
    if _service_built:
        return _service_cache

    _service_built = True
    creds_str = os.getenv("GCAL_SERVICE_ACCOUNT_JSON", "")
    if not creds_str:
        logger.debug("GCAL_SERVICE_ACCOUNT_JSON not set — GCal sync disabled")
        return None

    try:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build

        creds_info = json.loads(creds_str)
        creds = service_account.Credentials.from_service_account_info(
            creds_info, scopes=_SCOPES
        )
        _service_cache = build("calendar", "v3", credentials=creds, cache_discovery=False)
        logger.info("GCal service initialized for %s", creds_info.get("client_email", "?"))
    except Exception as e:
        logger.warning("GCal service build failed: %s", e)

    return _service_cache


def _default_calendar_id(calendar_id: str | None) -> str:
    return calendar_id or os.getenv("GCAL_CALENDAR_ID", "primary")


def create_event(
    summary: str,
    start_time: datetime,
    end_time: datetime,
    description: str = "",
    calendar_id: str | None = None,
) -> dict | None:
    """Create a Google Calendar event.

    Returns the full event dict (including id, hangoutLink) or None on failure.
    """
    service = _get_service()
    if service is None:
        return None

    cal_id = _default_calendar_id(calendar_id)
    event_body = {
        "summary": summary,
        "description": description,
        "start": {"dateTime": start_time.isoformat()},
        "end": {"dateTime": end_time.isoformat()},
    }

    try:
        event = (
            service.events()
            .insert(calendarId=cal_id, body=event_body)
            .execute()
        )
        logger.info("GCal event created: %s", event.get("id"))
        return event
    except Exception as e:
        logger.warning("GCal event creation failed: %s", e)
        return None


def list_events(
    calendar_id: str | None = None,
    time_min: datetime | None = None,
    time_max: datetime | None = None,
    max_results: int = 50,
) -> list[dict]:
    """Return a list of events between time_min and time_max.

    Each event dict has: id, summary, start (ISO str), end (ISO str),
    description, hangoutLink (if present).
    Returns [] on failure or when GCal is not configured.
    """
    service = _get_service()
    if service is None:
        return []

    cal_id = _default_calendar_id(calendar_id)
    kwargs: dict = {"calendarId": cal_id, "maxResults": max_results, "singleEvents": True, "orderBy": "startTime"}
    if time_min:
        kwargs["timeMin"] = time_min.isoformat()
    if time_max:
        kwargs["timeMax"] = time_max.isoformat()

    try:
        result = service.events().list(**kwargs).execute()
        raw_items = result.get("items", [])
        events = []
        for item in raw_items:
            start = item.get("start", {})
            end = item.get("end", {})
            events.append({
                "id": item.get("id", ""),
                "summary": item.get("summary", ""),
                "description": item.get("description", ""),
                "start": start.get("dateTime") or start.get("date", ""),
                "end": end.get("dateTime") or end.get("date", ""),
                "hangoutLink": item.get("hangoutLink", ""),
            })
        return events
    except Exception as e:
        logger.warning("GCal list_events failed: %s", e)
        return []


def delete_event_by_id(
    event_id: str,
    calendar_id: str | None = None,
) -> bool:
    """Delete a Google Calendar event by its ID. Returns True on success."""
    service = _get_service()
    if service is None:
        logger.debug("GCal not configured — skipping delete for event %s", event_id)
        return False

    cal_id = _default_calendar_id(calendar_id)
    try:
        service.events().delete(calendarId=cal_id, eventId=event_id).execute()
        logger.info("GCal event deleted: %s", event_id)
        return True
    except Exception as e:
        logger.warning("GCal delete failed for event %s: %s", event_id, e)
        return False
