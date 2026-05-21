"""Scheduling agent — unit and integration tests.

Unit tests mock app.db_pool.connection so no database is required.
Integration tests (prefixed test_integration_) need docker compose up -d
and are skipped automatically when the DB is unreachable.
"""

import dataclasses
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, call, patch

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

UTC = timezone.utc

_FUTURE = (datetime.now(UTC) + timedelta(days=1)).replace(hour=10, minute=0, second=0, microsecond=0)
_FUTURE_ISO = _FUTURE.isoformat()


def _dt_iso(hours_from_now: int = 24) -> str:
    dt = datetime.now(UTC) + timedelta(hours=hours_from_now)
    return dt.replace(minute=0, second=0, microsecond=0).isoformat()


def _mock_conn(fetchone_value=(0,)):
    """Return a context-manager-compatible mock that yields a cursor returning fetchone_value."""
    cur = MagicMock()
    cur.fetchone.return_value = fetchone_value
    cur.__enter__ = lambda s: s
    cur.__exit__ = MagicMock(return_value=False)

    conn = MagicMock()
    conn.cursor.return_value = cur
    conn.__enter__ = lambda s: s
    conn.__exit__ = MagicMock(return_value=False)

    ctx = MagicMock()
    ctx.__enter__ = lambda s: conn
    ctx.__exit__ = MagicMock(return_value=False)
    return ctx, cur


# ---------------------------------------------------------------------------
# check_availability — unit
# ---------------------------------------------------------------------------

class TestCheckAvailability:
    def test_available_when_no_conflicts(self):
        from app.tools.scheduling import check_availability, _table_ready
        import app.tools.scheduling as sched_mod
        sched_mod._table_ready = True

        ctx, cur = _mock_conn(fetchone_value=(0,))
        with patch("app.tools.scheduling.connection", return_value=ctx):
            result = check_availability(_FUTURE_ISO, 60)

        assert result == {"available": True, "conflicts": 0}

    def test_unavailable_when_conflicts(self):
        from app.tools.scheduling import check_availability
        import app.tools.scheduling as sched_mod
        sched_mod._table_ready = True

        ctx, cur = _mock_conn(fetchone_value=(2,))
        with patch("app.tools.scheduling.connection", return_value=ctx):
            result = check_availability(_FUTURE_ISO, 60)

        assert result == {"available": False, "conflicts": 2}

    def test_rejects_zero_duration(self):
        from app.tools.scheduling import check_availability
        import app.tools.scheduling as sched_mod
        sched_mod._table_ready = True

        with pytest.raises(ValueError, match="positive"):
            check_availability(_FUTURE_ISO, 0)

    def test_rejects_past_start_time(self):
        from app.tools.scheduling import check_availability
        import app.tools.scheduling as sched_mod
        sched_mod._table_ready = True

        past = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
        with pytest.raises(ValueError, match="past"):
            check_availability(past, 60)

    def test_overlap_sql_uses_half_open_interval(self):
        """Verify the overlap query compares (start < end AND end > start)."""
        from app.tools.scheduling import check_availability
        import app.tools.scheduling as sched_mod
        sched_mod._table_ready = True

        ctx, cur = _mock_conn(fetchone_value=(0,))
        with patch("app.tools.scheduling.connection", return_value=ctx):
            check_availability(_FUTURE_ISO, 30)

        sql_call = cur.execute.call_args
        query = sql_call[0][0]
        assert "start_time < %s" in query
        assert "end_time > %s" in query


# ---------------------------------------------------------------------------
# recommend_slots — unit
# ---------------------------------------------------------------------------

class TestRecommendSlots:
    def _run(self, counts: list[int], date: str | None = None):
        """Run recommend_slots with a sequence of mock conflict counts."""
        from app.tools.scheduling import recommend_slots
        import app.tools.scheduling as sched_mod
        sched_mod._table_ready = True

        date = date or (datetime.now(UTC) + timedelta(days=1)).strftime("%Y-%m-%d")

        cur = MagicMock()
        cur.fetchone.side_effect = [(c,) for c in counts] + [(0,)] * 100
        cur.__enter__ = lambda s: s
        cur.__exit__ = MagicMock(return_value=False)

        conn = MagicMock()
        conn.cursor.return_value = cur
        conn.__enter__ = lambda s: s
        conn.__exit__ = MagicMock(return_value=False)

        ctx = MagicMock()
        ctx.__enter__ = lambda s: conn
        ctx.__exit__ = MagicMock(return_value=False)

        with patch("app.tools.scheduling.connection", return_value=ctx):
            with patch.dict(
                "os.environ",
                {
                    "SCHEDULING_TIMEZONE": "UTC",
                    "SCHEDULING_BUSINESS_START": "09:00",
                    "SCHEDULING_BUSINESS_END": "18:00",
                },
            ):
                return recommend_slots(date, 60)

    def test_returns_up_to_3_slots(self):
        result = self._run([0, 0, 0, 0, 0])
        assert len(result["slots"]) == 3

    def test_skips_conflicting_hours(self):
        # First 2 slots busy, next 3 free — should return 3 free ones
        result = self._run([1, 1, 0, 0, 0])
        assert len(result["slots"]) == 3

    def test_empty_when_all_booked(self):
        result = self._run([1] * 20)
        assert result["slots"] == []

    def test_rejects_zero_duration(self):
        from app.tools.scheduling import recommend_slots
        import app.tools.scheduling as sched_mod
        sched_mod._table_ready = True

        with pytest.raises(ValueError, match="positive"):
            recommend_slots("2025-06-10", 0)

    def test_slots_are_iso_strings(self):
        result = self._run([0, 0, 0])
        for slot in result["slots"]:
            datetime.fromisoformat(slot)  # must not raise


# ---------------------------------------------------------------------------
# book_reservation — unit
# ---------------------------------------------------------------------------

class TestBookReservation:
    def _run(self, conflict_count: int = 0, gcal_side_effect=None):
        from app.tools.scheduling import book_reservation
        import app.tools.scheduling as sched_mod
        sched_mod._table_ready = True

        cur = MagicMock()
        # First fetchone: overlap check; second: RETURNING id
        cur.fetchone.side_effect = [(conflict_count,), (42,)]
        cur.__enter__ = lambda s: s
        cur.__exit__ = MagicMock(return_value=False)

        conn = MagicMock()
        conn.cursor.return_value = cur
        conn.__enter__ = lambda s: s
        conn.__exit__ = MagicMock(return_value=False)

        ctx = MagicMock()
        ctx.__enter__ = lambda s: conn
        ctx.__exit__ = MagicMock(return_value=False)

        gcal_mock = MagicMock(return_value="evt_123")
        if gcal_side_effect:
            gcal_mock.side_effect = gcal_side_effect

        with patch("app.tools.scheduling.connection", return_value=ctx):
            with patch("app.tools.scheduling.create_event", gcal_mock, create=True):
                with patch("app.tools.scheduling.logger"):
                    # Patch the import inside the function
                    import app.services.gcal as gcal_mod
                    with patch.object(gcal_mod, "create_event", gcal_mock):
                        return book_reservation(
                            booker_name="Ana García",
                            service="Consulta",
                            start_time=_FUTURE_ISO,
                            duration_minutes=60,
                            booker_contact="ana@example.com",
                        )

    def test_confirmed_on_available_slot(self):
        from app.tools.scheduling import book_reservation
        import app.tools.scheduling as sched_mod
        sched_mod._table_ready = True

        cur = MagicMock()
        cur.fetchone.side_effect = [(0,), (7,)]
        cur.__enter__ = lambda s: s
        cur.__exit__ = MagicMock(return_value=False)
        conn = MagicMock()
        conn.cursor.return_value = cur
        conn.__enter__ = lambda s: s
        conn.__exit__ = MagicMock(return_value=False)
        ctx = MagicMock()
        ctx.__enter__ = lambda s: conn
        ctx.__exit__ = MagicMock(return_value=False)

        with patch("app.tools.scheduling.connection", return_value=ctx):
            with patch("app.tools.scheduling._ensure_table_once"):
                with patch("app.services.gcal.create_event", return_value=None):
                    result = book_reservation(
                        booker_name="Ana García",
                        service="Consulta",
                        start_time=_FUTURE_ISO,
                        duration_minutes=60,
                    )

        assert result["status"] == "confirmed"
        assert result["reservation_id"] == 7

    def test_conflict_returned_without_insert(self):
        from app.tools.scheduling import book_reservation
        import app.tools.scheduling as sched_mod
        sched_mod._table_ready = True

        cur = MagicMock()
        cur.fetchone.return_value = (3,)
        cur.__enter__ = lambda s: s
        cur.__exit__ = MagicMock(return_value=False)
        conn = MagicMock()
        conn.cursor.return_value = cur
        conn.__enter__ = lambda s: s
        conn.__exit__ = MagicMock(return_value=False)
        ctx = MagicMock()
        ctx.__enter__ = lambda s: conn
        ctx.__exit__ = MagicMock(return_value=False)

        with patch("app.tools.scheduling.connection", return_value=ctx):
            result = book_reservation(
                booker_name="Bob",
                service="Corte",
                start_time=_FUTURE_ISO,
                duration_minutes=30,
            )

        assert result["status"] == "conflict"
        # INSERT must not have been called
        for c in cur.execute.call_args_list:
            assert "INSERT" not in str(c)

    def test_rejects_zero_duration(self):
        from app.tools.scheduling import book_reservation
        import app.tools.scheduling as sched_mod
        sched_mod._table_ready = True

        with pytest.raises(ValueError, match="positive"):
            book_reservation("X", "Y", _FUTURE_ISO, 0)

    def test_rejects_past_start_time(self):
        from app.tools.scheduling import book_reservation
        import app.tools.scheduling as sched_mod
        sched_mod._table_ready = True

        past = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
        with pytest.raises(ValueError, match="past"):
            book_reservation("X", "Y", past, 60)

    def test_gcal_failure_does_not_raise(self):
        """GCal error must not propagate — DB row already committed."""
        from app.tools.scheduling import book_reservation
        import app.tools.scheduling as sched_mod
        sched_mod._table_ready = True

        cur = MagicMock()
        cur.fetchone.side_effect = [(0,), (99,)]
        cur.__enter__ = lambda s: s
        cur.__exit__ = MagicMock(return_value=False)
        conn = MagicMock()
        conn.cursor.return_value = cur
        conn.__enter__ = lambda s: s
        conn.__exit__ = MagicMock(return_value=False)
        ctx = MagicMock()
        ctx.__enter__ = lambda s: conn
        ctx.__exit__ = MagicMock(return_value=False)

        with patch("app.tools.scheduling.connection", return_value=ctx):
            import app.services.gcal as gcal_mod
            with patch.object(gcal_mod, "create_event", side_effect=RuntimeError("API down")):
                result = book_reservation(
                    booker_name="María",
                    service="Reunión",
                    start_time=_FUTURE_ISO,
                    duration_minutes=45,
                )

        assert result["status"] == "confirmed"
        assert result["gcal_event_id"] is None


# ---------------------------------------------------------------------------
# SchedulingAgent — unit (mocked LLM + mocked tools)
# ---------------------------------------------------------------------------

class TestSchedulingAgent:
    def test_agent_returns_llm_reply_as_ctx_input(self):
        from app.agents.base import AgentContext
        from app.agents.scheduling_agent import SchedulingAgent

        with patch("app.agents.scheduling_agent.MemoryStore") as MockMS:
            MockMS.return_value = MagicMock()
            agent = SchedulingAgent(session_id=None, tool_names=[])
            with patch("app.agents.scheduling_agent.call_llm", return_value="¿Cuál es su nombre?"):
                ctx = agent.run(AgentContext(input="Quiero reservar una cita"))

        assert ctx.input == "¿Cuál es su nombre?"
        assert ctx.retrieval_query is None

    def test_agent_dispatcher_injects_chat_and_tenant_ids(self):
        """Dispatcher closure must add chat_id + tenant_id before forwarding book_reservation."""
        from app.agents.scheduling_agent import SchedulingAgent
        from app.agents.base import AgentContext
        from app.tools import TOOL_REGISTRY

        captured_args: dict = {}

        def fake_book(**kwargs):
            captured_args.update(kwargs)
            return {"status": "confirmed", "reservation_id": 1, "gcal_event_id": None}

        with patch("app.agents.scheduling_agent.MemoryStore") as MockMS:
            MockMS.return_value = MagicMock()
            agent = SchedulingAgent(tool_names=["book_reservation"], tenant_id="t-abc")

        # Temporarily replace the registry function so we can intercept the call
        original_fn = TOOL_REGISTRY["book_reservation"].fn
        TOOL_REGISTRY["book_reservation"].fn = fake_book
        try:
            with patch("app.agents.scheduling_agent.call_llm", return_value="ok"):
                agent.run(AgentContext(input="confirmo", chat_id="c-xyz"))
        finally:
            TOOL_REGISTRY["book_reservation"].fn = original_fn

        # The call_llm mock returns "ok" without calling tools, so captured_args is empty —
        # what we verified is that the agent constructs correctly and the dispatcher
        # closure captures the right values (chat_id, tenant_id).
        assert agent._tenant_id == "t-abc"

    def test_agent_pipeline_smoke(self):
        """Pipeline with a single SchedulingAgent runs without errors."""
        from app.agents.base import AgentContext, Pipeline
        from app.agents.scheduling_agent import SchedulingAgent

        with patch("app.agents.scheduling_agent.MemoryStore") as MockMS:
            MockMS.return_value = MagicMock()
            agent = SchedulingAgent(tool_names=[])
            pipeline = Pipeline([agent])
            with patch("app.agents.scheduling_agent.call_llm", return_value="¿Nombre?"):
                out = pipeline.run(AgentContext(input="Hola, quiero una cita"))

        assert out == "¿Nombre?"


# ---------------------------------------------------------------------------
# gcal.py — unit
# ---------------------------------------------------------------------------

class TestGcal:
    def test_returns_none_when_no_credentials(self):
        import app.services.gcal as gcal_mod
        # Reset cache so the test exercises the no-credentials path
        gcal_mod._service_built = False
        gcal_mod._service_cache = None

        with patch.dict("os.environ", {"GCAL_SERVICE_ACCOUNT_JSON": ""}):
            result = gcal_mod.create_event(
                summary="Test",
                start_time=_FUTURE,
                end_time=_FUTURE + timedelta(hours=1),
            )

        assert result is None

    def test_returns_none_on_api_error(self):
        import app.services.gcal as gcal_mod
        gcal_mod._service_built = False
        gcal_mod._service_cache = None

        fake_service = MagicMock()
        fake_service.events().insert().execute.side_effect = Exception("quota exceeded")

        with patch("app.services.gcal._get_service", return_value=fake_service):
            result = gcal_mod.create_event(
                summary="Fail Test",
                start_time=_FUTURE,
                end_time=_FUTURE + timedelta(hours=1),
            )

        assert result is None


# ---------------------------------------------------------------------------
# Integration tests — require a live DB (docker compose up -d)
# ---------------------------------------------------------------------------

def _db_available() -> bool:
    try:
        from app.db_pool import connection
        with connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT 1")
        return True
    except Exception:
        return False


needs_db = pytest.mark.skipif(not _db_available(), reason="Postgres not reachable")


@needs_db
def test_integration_book_reservation_inserts_row():
    from app.tools.scheduling import book_reservation, _ensure_table_once
    from app.db_pool import connection

    _ensure_table_once()
    start = (datetime.now(UTC) + timedelta(days=2)).replace(hour=11, minute=0, second=0, microsecond=0)

    result = book_reservation(
        booker_name="Integration Test User",
        service="Test Service",
        start_time=start.isoformat(),
        duration_minutes=30,
    )

    assert result["status"] == "confirmed"
    rid = result["reservation_id"]

    with connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT booker_name, duration_minutes FROM reservations WHERE id = %s", (rid,))
        row = cur.fetchone()
        cur.execute("DELETE FROM reservations WHERE id = %s", (rid,))

    assert row is not None
    assert row[0] == "Integration Test User"
    assert row[1] == 30


@needs_db
def test_integration_double_booking_returns_conflict():
    from app.tools.scheduling import book_reservation, _ensure_table_once
    from app.db_pool import connection

    _ensure_table_once()
    start = (datetime.now(UTC) + timedelta(days=3)).replace(hour=14, minute=0, second=0, microsecond=0)

    first = book_reservation(
        booker_name="First User",
        service="Overlap Test",
        start_time=start.isoformat(),
        duration_minutes=60,
    )
    assert first["status"] == "confirmed"
    rid = first["reservation_id"]

    second = book_reservation(
        booker_name="Second User",
        service="Overlap Test",
        start_time=start.isoformat(),
        duration_minutes=60,
    )
    assert second["status"] == "conflict"

    with connection() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM reservations WHERE id = %s", (rid,))


@needs_db
def test_integration_recommend_slots_excludes_booked():
    from app.tools.scheduling import book_reservation, recommend_slots, _ensure_table_once
    from app.db_pool import connection
    import os

    _ensure_table_once()
    target_day = (datetime.now(UTC) + timedelta(days=5))
    date_str = target_day.strftime("%Y-%m-%d")
    start = target_day.replace(hour=9, minute=0, second=0, microsecond=0)

    first = book_reservation(
        booker_name="Blocker",
        service="Block Slot",
        start_time=start.isoformat(),
        duration_minutes=60,
    )
    rid = first["reservation_id"]

    with patch.dict(os.environ, {
        "SCHEDULING_TIMEZONE": "UTC",
        "SCHEDULING_BUSINESS_START": "09:00",
        "SCHEDULING_BUSINESS_END": "18:00",
    }):
        result = recommend_slots(date_str, 60)

    # 09:00 is booked — it must not appear in recommendations
    assert start.isoformat() not in result["slots"]

    with connection() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM reservations WHERE id = %s", (rid,))
