"""Scheduling tools: check_availability, recommend_slots, book_reservation.

All functions are synchronous — they run inside call_llm's tool loop, which
itself runs inside asyncio.to_thread when called from the async socket handler.
DB access goes through app.db_pool.connection (psycopg, same pool as memory.py).
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


def _count_overlaps(cur, start: datetime, end: datetime) -> int:
    cur.execute(
        "SELECT COUNT(*) FROM reservations WHERE start_time < %s AND end_time > %s",
        (end, start),
    )
    return cur.fetchone()[0]


# ---------------------------------------------------------------------------
# Public tool functions
# ---------------------------------------------------------------------------

def check_availability(
    start_time: str,
    duration_minutes: int,
    buffer_minutes: int = 0,
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

    # Expand the checked window by the buffer on both sides
    buf = timedelta(minutes=buffer_minutes)
    with connection() as conn, conn.cursor() as cur:
        count = _count_overlaps(cur, start - buf, end + buf)

    return {"available": count == 0, "conflicts": count}


def recommend_slots(
    date: str,
    duration_minutes: int,
    buffer_minutes: int = 0,
    excluded_times: list[str] | None = None,
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
            if _count_overlaps(cur, cursor_dt - buf, end + buf) == 0:
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
    """Persist a confirmed reservation. Returns {status, reservation_id, gcal_event_id}."""
    _ensure_table_once()
    if duration_minutes <= 0:
        raise ValueError("duration_minutes must be positive.")
    start = _parse_iso(start_time)
    if start <= datetime.now(tz=start.tzinfo):
        raise ValueError("Cannot book a slot in the past.")
    end = start + timedelta(minutes=duration_minutes)

    with connection() as conn, conn.cursor() as cur:
        # Atomic overlap re-check before insert
        conflicts = _count_overlaps(cur, start, end)
        if conflicts > 0:
            return {
                "status": "conflict",
                "message": "That slot is no longer available. Please choose another time.",
            }

        cur.execute(
            """
            INSERT INTO reservations
                (tenant_id, chat_id, booker_name, booker_contact, service,
                 start_time, end_time, duration_minutes)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (tenant_id, chat_id, booker_name, booker_contact or "",
             service or "", start, end, duration_minutes),
        )
        reservation_id: int = cur.fetchone()[0]

    # Best-effort GCal sync — DB row is already committed; never roll back on failure
    gcal_event_id: str | None = None
    gcal_error: str | None = None
    try:
        from app.services.gcal import create_event
        gcal_event_id = create_event(
            summary=f"{service} — {booker_name}",
            start_time=start,
            end_time=end,
            description=f"Contact: {booker_contact}" if booker_contact else "",
            calendar_id=calendar_id,
        )
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
    }


# ---------------------------------------------------------------------------
# OpenAI tool declarations
# ---------------------------------------------------------------------------

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
        "Returns {status, reservation_id, gcal_event_id}."
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
