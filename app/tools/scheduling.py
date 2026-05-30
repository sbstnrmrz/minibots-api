"""Scheduling tools for the SchedulingAgent.

All functions are synchronous — they run inside call_llm's tool loop,
which itself runs inside asyncio.to_thread when called from the async
socket handler. DB access goes through app.db_pool.connection.

Tool inventory
--------------
get_events       — query Google Calendar for events in a time window
create_event     — create GCal event + persist reservation in DB (fusion of
                   the old book_reservation)
delete_event     — delete GCal event by ID + mark reservation cancelled in DB
inbox_reserve    — placeholder notification hook (stub, always succeeds)
check_availability — DB-based slot availability check (kept for compatibility)
recommend_slots    — DB-based slot recommendation (kept for compatibility)
book_reservation   — alias of create_event kept for existing workflows
"""

import logging
from datetime import date as date_type
from datetime import datetime, timedelta, time
from zoneinfo import ZoneInfo

from app.db_pool import connection
from llm.tools import to_openai_tool

logger = logging.getLogger("scheduling")

_table_ready = False

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS reservations (
    id               SERIAL PRIMARY KEY,
    tenant_id        UUID        REFERENCES tenants(id),
    chat_id          VARCHAR,
    booker_name      VARCHAR     NOT NULL,
    booker_contact   VARCHAR,
    service          VARCHAR,
    start_time       TIMESTAMPTZ NOT NULL,
    end_time         TIMESTAMPTZ NOT NULL,
    duration_minutes INTEGER     NOT NULL,
    gcal_event_id    VARCHAR,
    gcal_sync_error  VARCHAR,
    cancelled        BOOLEAN     NOT NULL DEFAULT FALSE,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_reservations_time_range
    ON reservations (start_time, end_time);
"""


def _ensure_table_once() -> None:
    global _table_ready
    if _table_ready:
        return
    with connection() as conn, conn.cursor() as cur:
        cur.execute(_CREATE_TABLE)
    _table_ready = True


def _get_tz_and_hours() -> tuple[ZoneInfo, time, time]:
    from app.config import SCHEDULING_TIMEZONE, SCHEDULING_BUSINESS_START, SCHEDULING_BUSINESS_END
    tz = ZoneInfo(SCHEDULING_TIMEZONE)
    sh, sm = map(int, SCHEDULING_BUSINESS_START.split(":"))
    eh, em = map(int, SCHEDULING_BUSINESS_END.split(":"))
    return tz, time(sh, sm), time(eh, em)


def _parse_iso(ts: str) -> datetime:
    dt = datetime.fromisoformat(ts)
    if dt.tzinfo is None:
        tz, _, _ = _get_tz_and_hours()
        dt = dt.replace(tzinfo=tz)
    return dt


def _count_overlaps(
    cur, start: datetime, end: datetime, tenant_id: str | None = None
) -> int:
    """Count active reservations overlapping [start, end).

    When tenant_id is given, only that tenant's reservations count — one
    tenant's bookings must never block another tenant's calendar.
    """
    if tenant_id is not None:
        cur.execute(
            "SELECT COUNT(*) FROM reservations "
            "WHERE cancelled = FALSE AND tenant_id = %s "
            "AND start_time < %s AND end_time > %s",
            (tenant_id, end, start),
        )
    else:
        cur.execute(
            "SELECT COUNT(*) FROM reservations "
            "WHERE cancelled = FALSE AND start_time < %s AND end_time > %s",
            (end, start),
        )
    return cur.fetchone()[0]


# ---------------------------------------------------------------------------
# GCal-native tools (new prompt interface)
# ---------------------------------------------------------------------------

def get_events(
    calendar_id: str,
    time_min: str,
    time_max: str,
) -> dict:
    """Return events from a Google Calendar between two ISO datetimes.

    Returns {events: [{id, summary, description, start, end, hangoutLink}]}.
    Returns empty list when GCal is not configured (development mode).
    """
    from app.services.gcal import list_events

    t_min = _parse_iso(time_min)
    t_max = _parse_iso(time_max)
    events = list_events(calendar_id=calendar_id, time_min=t_min, time_max=t_max)
    return {"events": events}


def create_event(
    summary: str,
    start_time: str,
    end_time: str,
    description: str = "",
    calendar_id: str | None = None,
    booker_name: str = "",
    booker_contact: str = "",
    service: str = "",
    reservation_code: str = "",
    chat_id: str | None = None,
    tenant_id: str | None = None,
) -> dict:
    """Create a Google Calendar event and persist the reservation in the DB.

    This is the fused version of the old book_reservation + gcal.create_event.
    Returns {status, reservation_id, gcal_event_id, hangout_link}.
    """
    _ensure_table_once()

    start = _parse_iso(start_time)
    end = _parse_iso(end_time)
    if start >= end:
        raise ValueError("end_time must be after start_time.")
    if start <= datetime.now(tz=start.tzinfo):
        raise ValueError("Cannot book a slot in the past.")

    duration_minutes = int((end - start).total_seconds() / 60)

    with connection() as conn, conn.cursor() as cur:
        conflicts = _count_overlaps(cur, start, end, tenant_id=tenant_id)
        if conflicts > 0:
            return {
                "status": "conflict",
                "message": "That slot is no longer available. Please choose another time.",
            }

        cur.execute(
            """
            INSERT INTO reservations
                (tenant_id, chat_id, booker_name, booker_contact, service,
                 start_time, end_time, duration_minutes, reservation_code)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (tenant_id, chat_id, booker_name or "", booker_contact or "",
             service or summary, start, end, duration_minutes,
             reservation_code or None),
        )
        reservation_id: int = cur.fetchone()[0]

    gcal_event_id: str | None = None
    hangout_link: str | None = None
    gcal_error: str | None = None
    try:
        from app.services.gcal import create_event as _gcal_create
        event = _gcal_create(
            summary=summary,
            start_time=start,
            end_time=end,
            description=description,
            calendar_id=calendar_id,
        )
        if event:
            gcal_event_id = event.get("id") or None
            hangout_link = event.get("hangoutLink") or None
    except Exception as e:
        gcal_error = str(e)
        logger.warning("GCal sync failed for reservation %d: %s", reservation_id, e)

    if gcal_event_id is not None or gcal_error is not None:
        with connection() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE reservations SET gcal_event_id = %s, gcal_sync_error = %s WHERE id = %s",
                (gcal_event_id, gcal_error, reservation_id),
            )

    return {
        "status": "confirmed",
        "reservation_id": reservation_id,
        "gcal_event_id": gcal_event_id,
        "hangout_link": hangout_link,
    }


def delete_event(
    event_id: str,
    calendar_id: str | None = None,
    tenant_id: str | None = None,
) -> dict:
    """Delete a Google Calendar event and mark the matching DB reservation as cancelled.

    When tenant_id is provided, the cancel only touches that tenant's row —
    a tenant must never be able to cancel another tenant's reservation by
    supplying a foreign gcal_event_id.

    Returns {status: "deleted"|"gcal_error"|"not_found", gcal_deleted: bool}.
    """
    from app.services.gcal import delete_event_by_id

    gcal_deleted = delete_event_by_id(event_id=event_id, calendar_id=calendar_id)

    cancelled_rows = 0
    with connection() as conn, conn.cursor() as cur:
        # Only mark as cancelled if the reservations table exists
        try:
            if tenant_id is not None:
                cur.execute(
                    "UPDATE reservations SET cancelled = TRUE "
                    "WHERE gcal_event_id = %s AND tenant_id = %s AND cancelled = FALSE",
                    (event_id, tenant_id),
                )
            else:
                cur.execute(
                    "UPDATE reservations SET cancelled = TRUE "
                    "WHERE gcal_event_id = %s AND cancelled = FALSE",
                    (event_id,),
                )
            cancelled_rows = cur.rowcount
        except Exception:
            pass

    return {
        "status": "deleted" if gcal_deleted else "gcal_error",
        "gcal_deleted": gcal_deleted,
        "db_rows_cancelled": cancelled_rows,
    }


def inbox_reserve(
    booker_name: str = "",
    service: str = "",
    start_time: str = "",
    reservation_code: str = "",
    extras: str = "",
    notes: str = "",
) -> dict:
    """Notify the business inbox about a new or modified reservation.

    Stub — always returns success. Replace the body with real notification
    logic (email, WhatsApp, Slack, etc.) when ready.
    """
    logger.info(
        "inbox_reserve stub called: booker=%s service=%s start=%s code=%s",
        booker_name, service, start_time, reservation_code,
    )
    return {"status": "ok", "notified": False}


# ---------------------------------------------------------------------------
# DB-native tools (kept for backwards compatibility)
# ---------------------------------------------------------------------------

def check_availability(
    start_time: str,
    duration_minutes: int,
    buffer_minutes: int = 0,
    tenant_id: str | None = None,
) -> dict:
    """Return {available: bool, conflicts: int} for the requested slot.

    buffer_minutes adds a required gap before and after the slot so that
    back-to-back reservations are separated by at least that many minutes.
    """
    _ensure_table_once()
    if duration_minutes <= 0:
        raise ValueError("duration_minutes must be positive.")
    if buffer_minutes < 0:
        raise ValueError("buffer_minutes must be non-negative.")
    start = _parse_iso(start_time)
    if start <= datetime.now(tz=start.tzinfo):
        raise ValueError("Cannot book a slot in the past.")
    end = start + timedelta(minutes=duration_minutes)

    buf = timedelta(minutes=buffer_minutes)
    with connection() as conn, conn.cursor() as cur:
        count = _count_overlaps(cur, start - buf, end + buf, tenant_id=tenant_id)

    return {"available": count == 0, "conflicts": count}


def recommend_slots(
    date: str,
    duration_minutes: int,
    buffer_minutes: int = 0,
    excluded_times: list[str] | None = None,
    tenant_id: str | None = None,
) -> dict:
    """Return {slots: [ISO datetime strings]} — up to 3 available starting times.

    buffer_minutes enforces the same gap logic as check_availability.
    excluded_times is a list of ISO datetimes the user already knows are
    occupied; when provided, candidates are sorted by proximity to those
    times so the nearest alternatives are returned first.
    """
    _ensure_table_once()
    if duration_minutes <= 0:
        raise ValueError("duration_minutes must be positive.")
    if buffer_minutes < 0:
        raise ValueError("buffer_minutes must be non-negative.")

    tz, biz_start, biz_end = _get_tz_and_hours()
    day = date_type.fromisoformat(date)
    buf = timedelta(minutes=buffer_minutes)

    biz_end_dt = datetime.combine(day, biz_end, tzinfo=tz)
    latest_start = biz_end_dt - timedelta(minutes=duration_minutes)

    cursor_dt = datetime.combine(day, biz_start, tzinfo=tz)
    candidates: list[datetime] = []

    with connection() as conn, conn.cursor() as cur:
        while cursor_dt <= latest_start:
            end = cursor_dt + timedelta(minutes=duration_minutes)
            if _count_overlaps(cur, cursor_dt - buf, end + buf, tenant_id=tenant_id) == 0:
                candidates.append(cursor_dt)
            cursor_dt += timedelta(hours=1)

    if excluded_times:
        parsed_excluded = [_parse_iso(t) for t in excluded_times]

        def _proximity(dt: datetime) -> timedelta:
            return min(abs(dt - ex) for ex in parsed_excluded)

        candidates.sort(key=_proximity)

    return {"slots": [c.isoformat() for c in candidates[:3]]}


def book_reservation(
    booker_name: str,
    service: str,
    start_time: str,
    duration_minutes: int,
    booker_contact: str = "",
    chat_id: str | None = None,
    tenant_id: str | None = None,
    calendar_id: str | None = None,
) -> dict:
    """Persist a confirmed reservation. Alias of create_event for existing workflows.

    Returns {status, reservation_id, gcal_event_id}.
    """
    if duration_minutes <= 0:
        raise ValueError("duration_minutes must be positive.")
    start = _parse_iso(start_time)
    end = start + timedelta(minutes=duration_minutes)
    result = create_event(
        summary=f"{service} — {booker_name}",
        start_time=start.isoformat(),
        end_time=end.isoformat(),
        description=f"Contact: {booker_contact}" if booker_contact else "",
        calendar_id=calendar_id,
        booker_name=booker_name,
        booker_contact=booker_contact,
        service=service,
        chat_id=chat_id,
        tenant_id=tenant_id,
    )
    return result


# ---------------------------------------------------------------------------
# OpenAI tool declarations
# ---------------------------------------------------------------------------

GET_EVENTS_TOOL = to_openai_tool(
    name="get_events",
    description=(
        "Query a Google Calendar for events in a given time window. "
        "Use this to check availability, find existing reservations by code, "
        "or inspect the schedule before creating or modifying a booking. "
        "Returns {events: [{id, summary, description, start, end, hangoutLink}]}."
    ),
    parameters={
        "type": "object",
        "properties": {
            "calendar_id": {
                "type": "string",
                "description": "Google Calendar ID to query (e.g. 'abc@group.calendar.google.com').",
            },
            "time_min": {
                "type": "string",
                "description": "ISO 8601 datetime — start of the search window.",
            },
            "time_max": {
                "type": "string",
                "description": "ISO 8601 datetime — end of the search window.",
            },
        },
        "required": ["calendar_id", "time_min", "time_max"],
    },
)

CREATE_EVENT_TOOL = to_openai_tool(
    name="create_event",
    description=(
        "Create a Google Calendar event and persist the reservation in the database. "
        "Call ONLY after the user has explicitly confirmed all details. "
        "Returns {status, reservation_id, gcal_event_id, hangout_link}."
    ),
    parameters={
        "type": "object",
        "properties": {
            "summary": {
                "type": "string",
                "description": "Event title, e.g. 'Reserva Sala Ideas - Juan Pérez - #A3K9P2'.",
            },
            "start_time": {
                "type": "string",
                "description": "ISO 8601 datetime with timezone for the event start.",
            },
            "end_time": {
                "type": "string",
                "description": "ISO 8601 datetime with timezone for the event end.",
            },
            "description": {
                "type": "string",
                "description": "Event description with reservation details (code, name, ID, phone, email, extras).",
            },
            "calendar_id": {
                "type": "string",
                "description": "Google Calendar ID of the resource to book.",
            },
            "booker_name": {
                "type": "string",
                "description": "Full name of the person making the booking.",
            },
            "booker_contact": {
                "type": "string",
                "description": "Phone or email of the booker.",
            },
            "service": {
                "type": "string",
                "description": "Name of the resource or service being reserved.",
            },
            "reservation_code": {
                "type": "string",
                "description": "6-character alphanumeric code generated for this reservation (e.g. 'A3K9P2').",
            },
        },
        "required": ["summary", "start_time", "end_time"],
    },
)

DELETE_EVENT_TOOL = to_openai_tool(
    name="delete_event",
    description=(
        "Delete a Google Calendar event by its ID and mark the matching reservation "
        "as cancelled in the database. Use for cancellations and when replacing an event "
        "during a modification (delete old → create new). "
        "Returns {status, gcal_deleted, db_rows_cancelled}."
    ),
    parameters={
        "type": "object",
        "properties": {
            "event_id": {
                "type": "string",
                "description": "Google Calendar event ID to delete.",
            },
            "calendar_id": {
                "type": "string",
                "description": "Google Calendar ID where the event lives.",
            },
        },
        "required": ["event_id"],
    },
)

INBOX_RESERVE_TOOL = to_openai_tool(
    name="inbox_reserve",
    description=(
        "Notify the business inbox about a new or modified reservation. "
        "Call after create_event succeeds. Always returns {status: 'ok'}."
    ),
    parameters={
        "type": "object",
        "properties": {
            "booker_name": {"type": "string", "description": "Full name of the booker."},
            "service": {"type": "string", "description": "Resource or service reserved."},
            "start_time": {"type": "string", "description": "ISO 8601 start datetime."},
            "reservation_code": {"type": "string", "description": "6-character alphanumeric code."},
            "extras": {"type": "string", "description": "Comma-separated extras selected."},
            "notes": {"type": "string", "description": "Any additional notes."},
        },
        "required": [],
    },
)

CHECK_AVAILABILITY_TOOL = to_openai_tool(
    name="check_availability",
    description=(
        "Check whether a time slot is free for booking. "
        "Returns {available: bool, conflicts: int}. "
        "Always call this before confirming any slot with the user."
    ),
    parameters={
        "type": "object",
        "properties": {
            "start_time": {
                "type": "string",
                "description": "ISO 8601 datetime with timezone, e.g. '2025-06-10T14:00:00-05:00'.",
            },
            "duration_minutes": {
                "type": "integer",
                "description": "Duration of the reservation in minutes.",
            },
            "buffer_minutes": {
                "type": "integer",
                "description": (
                    "Minimum gap in minutes required before and after this slot "
                    "so it does not crowd adjacent reservations. Defaults to 0."
                ),
            },
        },
        "required": ["start_time", "duration_minutes"],
    },
)

RECOMMEND_SLOTS_TOOL = to_openai_tool(
    name="recommend_slots",
    description=(
        "Return up to 3 available starting times on a given day for a given duration. "
        "Call when the user's requested slot is unavailable. "
        "Pass excluded_times with any times the user already mentioned as occupied — "
        "the returned slots will be sorted nearest to those times first."
    ),
    parameters={
        "type": "object",
        "properties": {
            "date": {
                "type": "string",
                "description": "Date in YYYY-MM-DD format.",
            },
            "duration_minutes": {
                "type": "integer",
                "description": "Duration of the reservation in minutes.",
            },
            "buffer_minutes": {
                "type": "integer",
                "description": (
                    "Minimum gap in minutes required before and after each candidate slot. "
                    "Use the same value as check_availability. Defaults to 0."
                ),
            },
            "excluded_times": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "ISO 8601 datetimes the user has already tried or stated as occupied. "
                    "Candidates are sorted by proximity to these times so the nearest "
                    "alternatives appear first."
                ),
            },
        },
        "required": ["date", "duration_minutes"],
    },
)

BOOK_RESERVATION_TOOL = to_openai_tool(
    name="book_reservation",
    description=(
        "Persist a confirmed reservation to the database and sync to Google Calendar. "
        "Call ONLY after the user has explicitly confirmed the slot, their name, and the service. "
        "Returns {status, reservation_id, gcal_event_id}. "
        "Prefer create_event for new workflows."
    ),
    parameters={
        "type": "object",
        "properties": {
            "booker_name": {
                "type": "string",
                "description": "Full name of the person making the booking.",
            },
            "service": {
                "type": "string",
                "description": "Name of the service or meeting type being booked.",
            },
            "start_time": {
                "type": "string",
                "description": "ISO 8601 datetime with timezone for the reservation start.",
            },
            "duration_minutes": {
                "type": "integer",
                "description": "Duration in minutes.",
            },
            "booker_contact": {
                "type": "string",
                "description": "Optional phone number or email for the booker.",
            },
        },
        "required": ["booker_name", "service", "start_time", "duration_minutes"],
    },
)
