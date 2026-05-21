"""Google Calendar sync — best-effort, never raises to the caller.

Requires env vars:
  GCAL_SERVICE_ACCOUNT_JSON  Full contents of a service account JSON file.
  GCAL_CALENDAR_ID           Target calendar ID (default: "primary").

If GCAL_SERVICE_ACCOUNT_JSON is empty, create_event returns None immediately
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


def create_event(
    summary: str,
    start_time: datetime,
    end_time: datetime,
    description: str = "",
    calendar_id: str | None = None,
) -> str | None:
    """Create a Google Calendar event. Returns the event ID or None on failure."""
    service = _get_service()
    if service is None:
        return None

    calendar_id = calendar_id or os.getenv("GCAL_CALENDAR_ID", "primary")
    event_body = {
        "summary": summary,
        "description": description,
        "start": {"dateTime": start_time.isoformat()},
        "end": {"dateTime": end_time.isoformat()},
    }

    try:
        event = service.events().insert(calendarId=calendar_id, body=event_body).execute()
        event_id: str = event.get("id", "")
        logger.info("GCal event created: %s", event_id)
        return event_id or None
    except Exception as e:
        logger.warning("GCal event creation failed: %s", e)
        return None
