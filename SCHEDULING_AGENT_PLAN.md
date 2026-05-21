# Scheduling Agent — Implementation Plan

## 1. Objective

Add a `"scheduler"` agent type to the workflow system that:
- Extracts user identity + reservation intent via LLM tool-calling
- Checks availability and recommends slots against the local PostgreSQL `reservations` table
- Persists confirmed bookings to PostgreSQL
- Syncs each confirmed booking to a Google Calendar as a secondary, best-effort operation

---

## 2. Alignment With Existing Patterns

| Concern | Existing Pattern | Scheduling Agent Follows |
|---|---|---|
| Agent class | Subclass `Agent`, `run(ctx) -> AgentContext` | Yes — `SchedulingAgent` in `app/agents/scheduling_agent.py` |
| Tool declaration | `to_openai_tool()` + `TOOL_REGISTRY` entry | Yes — three tools in `app/tools/scheduling.py` |
| DB access (sync) | `app.db_pool.connection()` psycopg context manager | Yes — tools use `connection()` directly |
| Config / env vars | `app/config.py` + `os.getenv` | Yes — two new vars added to `config.py` |
| Migrations | Raw SQL in `migrate.py` (idempotent `IF NOT EXISTS`) | Yes — `reservations` table added there |
| Agent type registration | `factory.py:_build_agent()` dispatch dict | Yes — `"scheduler"` case added |

---

## 3. New Files

```
app/
├── agents/
│   └── scheduling_agent.py   # SchedulingAgent class + SCHEDULING_SYSTEM_PROMPT
├── tools/
│   └── scheduling.py         # check_availability, recommend_slots, book_reservation
└── services/
    └── gcal.py               # Google Calendar sync (create_event, best-effort)
tests/
└── test_scheduling.py        # unit + integration tests
```

---

## 4. Database Schema

### New table: `reservations`

```sql
CREATE TABLE IF NOT EXISTS reservations (
    id               SERIAL PRIMARY KEY,
    tenant_id        UUID        REFERENCES tenants(id),
    chat_id          VARCHAR,
    booker_name      VARCHAR     NOT NULL,
    booker_contact   VARCHAR,
    service          VARCHAR,
    start_time       TIMESTAMPTZ NOT NULL,
    end_time         TIMESTAMPTZ NOT NULL,    -- computed: start_time + duration
    duration_minutes INTEGER     NOT NULL,
    gcal_event_id    VARCHAR,                -- NULL until GCal sync succeeds
    gcal_sync_error  VARCHAR,                -- last GCal error if sync failed
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_reservations_time_range
    ON reservations (start_time, end_time);
```

**Overlap condition used in `check_availability`:**

```
existing.start_time < :requested_end
AND existing.end_time   > :requested_start
```

This is the standard half-open interval overlap check — covers all partial and full overlap cases.

### Migration

The SQL block above is added to `migrate.py` alongside the existing idempotent blocks. No Alembic; no new migration runner.

---

## 5. Tools (`app/tools/scheduling.py`)

All three functions are synchronous — they run inside `call_llm`'s tool loop, which itself runs inside `asyncio.to_thread` when called from the async socket handler.

### 5.1 `check_availability`

```python
def check_availability(start_time: str, duration_minutes: int) -> dict:
    """
    Returns {"available": bool, "conflicts": int}.
    start_time: ISO 8601 string with timezone (e.g. "2025-06-10T14:00:00-05:00").
    """
```

Implementation:
- Parse `start_time` to `datetime` (timezone-aware)
- Compute `end_time = start_time + timedelta(minutes=duration_minutes)`
- Query `reservations` with the overlap condition above
- Return `{"available": True}` or `{"available": False, "conflicts": N}`

### 5.2 `recommend_slots`

```python
def recommend_slots(date: str, duration_minutes: int) -> dict:
    """
    Returns {"slots": ["2025-06-10T09:00:00-05:00", ...]} — up to 3 ISO strings.
    date: YYYY-MM-DD string.
    """
```

Implementation:
- Generate candidate start times: every hour from `SCHEDULING_BUSINESS_START` (default 09:00) to `SCHEDULING_BUSINESS_END` (default 18:00) minus `duration_minutes`
- For each candidate, call the overlap query (reuse the same SQL)
- Collect first 3 available slots
- Return `{"slots": [...]}` — empty list if none found that day

Business hours configurable via `SCHEDULING_BUSINESS_START` / `SCHEDULING_BUSINESS_END` env vars (HH:MM format). Timezone set by `SCHEDULING_TIMEZONE` (default `"UTC"`).

### 5.3 `book_reservation`

```python
def book_reservation(
    booker_name: str,
    booker_contact: str,
    service: str,
    start_time: str,
    duration_minutes: int,
    chat_id: str | None = None,
    tenant_id: str | None = None,
) -> dict:
    """
    Inserts a reservation row, then attempts GCal sync.
    Returns {"status": "confirmed", "reservation_id": int, "gcal_event_id": str | None}.
    """
```

Implementation:
1. Re-run overlap check (atomic guard — double-check before insert)
2. If conflict found: return `{"status": "conflict", "message": "..."}` without inserting
3. `INSERT INTO reservations (...)` — get returned `id`
4. Call `gcal.create_event(...)` wrapped in `try/except`
   - On success: `UPDATE reservations SET gcal_event_id = ...`
   - On failure: `UPDATE reservations SET gcal_sync_error = ...` + log warning — **do not raise**
5. Return confirmation dict with `reservation_id` and `gcal_event_id` (may be `None`)

### Tool Declarations

All three registered in `TOOL_REGISTRY` under keys `"check_availability"`, `"recommend_slots"`, `"book_reservation"` respectively.

The OpenAI function names match the registry keys (unlike `calculate` vs `calculator`) to keep things simple.

---

## 6. Agent (`app/agents/scheduling_agent.py`)

```python
class SchedulingAgent(Agent):
    def __init__(
        self,
        system_prompt: str = SCHEDULING_SYSTEM_PROMPT,
        session_id: str | None = None,
        tool_names: list[str] | None = None,
        tenant_id: str | None = None,
    ) -> None: ...

    def run(self, ctx: AgentContext) -> AgentContext: ...
```

**`run()` flow:**
1. Load conversation history from `MemoryStore` (keyed by `ctx.chat_id or session_id`)
2. Build messages list: system prompt + history + user input
3. Call `call_llm(config, messages, tools=scheduling_tools, dispatcher=scheduling_dispatcher)`
   - `config` replaces `system_prompt` on `DEFAULT_LLM_CONFIG`
   - `tools` = declarations for the three scheduling tools
   - The LLM iteratively calls tools until it can produce a final text reply
4. Save exchange to `MemoryStore`
5. Return `dataclasses.replace(ctx, input=reply)`

**`tenant_id` threading:** passed down to `book_reservation` via a closure on the dispatcher, so the DB row gets the correct tenant scope without the LLM needing to know about it.

### System Prompt (`SCHEDULING_SYSTEM_PROMPT`)

Key instructions:
- Collect required fields before calling `book_reservation`: `booker_name`, `service`, `date`, `start_time`, `duration_minutes`. `booker_contact` is optional (phone/email).
- If any required field is missing, ask the user — do not guess or hallucinate defaults.
- Call `check_availability(start_time, duration_minutes)` before confirming any slot.
- If unavailable, call `recommend_slots(date, duration_minutes)` and present the options to the user.
- Only call `book_reservation` after the user explicitly confirms the slot.
- Respond in the user's language. No markdown. No filler phrases.

---

## 7. Factory Registration (`app/agents/factory.py`)

One new `if` block in `_build_agent()`:

```python
if agent_type == "scheduler":
    from app.agents.scheduling_agent import SchedulingAgent
    return SchedulingAgent(
        system_prompt=agent_config.system_prompt or SCHEDULING_SYSTEM_PROMPT,
        tool_names=tool_names,
        tenant_id=config.get("tenant_id"),
    )
```

No other changes to `factory.py` or `build_pipeline`.

---

## 8. Google Calendar Integration (`app/services/gcal.py`)

### Authentication

Service account credentials stored as a JSON string in `GCAL_SERVICE_ACCOUNT_JSON` env var (the full contents of the downloaded `.json` file). Target calendar: `GCAL_CALENDAR_ID`.

```python
def _get_service():
    """Build and cache a Google Calendar API service object."""
    creds_json = os.getenv("GCAL_SERVICE_ACCOUNT_JSON", "")
    calendar_id = os.getenv("GCAL_CALENDAR_ID", "primary")
    ...
```

If `GCAL_SERVICE_ACCOUNT_JSON` is empty, `create_event` returns `None` immediately with a warning log — useful in development without creds.

### `create_event`

```python
def create_event(
    summary: str,
    start_time: datetime,
    end_time: datetime,
    description: str = "",
) -> str | None:
    """
    Creates a Google Calendar event. Returns the event ID on success, None on failure.
    Never raises — caller decides what to do with None.
    """
```

Uses `googleapiclient.discovery.build("calendar", "v3", credentials=creds)`. Event body follows the Calendar API v3 schema with RFC3339 datetimes.

### Required packages (add to `pyproject.toml`):

```
google-api-python-client
google-auth
```

---

## 9. Config (`app/config.py`)

New env vars (all optional — agent degrades gracefully if absent):

| Variable | Default | Purpose |
|---|---|---|
| `GCAL_SERVICE_ACCOUNT_JSON` | `""` | Full service account JSON string; GCal sync disabled if empty |
| `GCAL_CALENDAR_ID` | `"primary"` | Target Google Calendar ID |
| `SCHEDULING_TIMEZONE` | `"UTC"` | IANA timezone for slot generation and display |
| `SCHEDULING_BUSINESS_START` | `"09:00"` | Earliest bookable hour (HH:MM) |
| `SCHEDULING_BUSINESS_END` | `"18:00"` | Latest bookable start hour (HH:MM) |

---

## 10. Tests (`tests/test_scheduling.py`)

### Unit tests (no DB, no GCal)

| Test | What it verifies |
|---|---|
| `test_overlap_detection_partial` | Overlapping by 1 min → conflict detected |
| `test_overlap_detection_adjacent` | Back-to-back slots → no conflict |
| `test_overlap_detection_contained` | New slot fully inside existing → conflict |
| `test_recommend_slots_returns_max_3` | At most 3 slots returned |
| `test_recommend_slots_skips_conflicts` | Booked hours excluded from recommendations |
| `test_business_hours_respected` | No slot starts before `BUSINESS_START` or after `BUSINESS_END - duration` |

These use a real test DB (same pattern as existing tests in `tests/`) — spin up with `docker compose up -d` before running.

### Integration tests

| Test | What it verifies |
|---|---|
| `test_book_reservation_inserts_row` | `book_reservation` → row exists in DB with correct fields |
| `test_book_reservation_conflict_guard` | Double-booking the same slot → second call returns conflict |
| `test_gcal_error_does_not_rollback_reservation` | GCal failure → DB row still committed, `gcal_sync_error` set |
| `test_gcal_disabled_when_no_creds` | Empty `GCAL_SERVICE_ACCOUNT_JSON` → no exception, `gcal_event_id=None` |

### Pipeline smoke test

| Test | What it verifies |
|---|---|
| `test_scheduling_agent_via_pipeline` | Build a one-agent `Pipeline([SchedulingAgent(...)])`, run with a mock `call_llm` that simulates tool calls, confirm `book_reservation` was called with correct args |

---

## 11. Edge Cases and Guardrails

| Scenario | Handling |
|---|---|
| User provides ambiguous time ("tomorrow at 3") | LLM asked to resolve to ISO datetime; if it cannot determine date/timezone it asks the user |
| Slot unavailable, `recommend_slots` returns empty list | Agent tells user no availability that day, asks for a different date |
| `book_reservation` called twice (double-tap) | Atomic re-check before insert — second call returns conflict without inserting |
| GCal credentials missing | `create_event` returns `None`; reservation persists; log warning; user confirmation message omits calendar link |
| GCal API rate-limit or 5xx | Same as missing creds — `try/except`, log, continue |
| `duration_minutes` ≤ 0 | Tool raises `ValueError`; `call_llm` catches and feeds error back to model |
| `start_time` in the past | Tool raises `ValueError("Cannot book a slot in the past")` |
| Invalid ISO string for `start_time` | `datetime.fromisoformat()` raises; caught and returned as `{"error": "..."}` to model |

---

## 12. Rollout Sequence (post-approval)

1. Add `reservations` table and indexes to `migrate.py` → run `uv run python migrate.py`
2. Add GCal/scheduling env vars to `.env.example`
3. Add `google-api-python-client` + `google-auth` to `pyproject.toml` → `uv sync`
4. Create `app/services/gcal.py`
5. Create `app/tools/scheduling.py` + register in `app/tools/__init__.py`
6. Create `app/agents/scheduling_agent.py`
7. Add `"scheduler"` branch to `factory.py:_build_agent()`
8. Add scheduling config vars to `app/config.py`
9. Write and run `tests/test_scheduling.py`
10. Smoke-test via `setup_test.py` or manual socket.io call with a scheduler workflow

Each step is independently reviewable and reversible before the next.

---

## 13. What Is NOT Changed

- `app/main.py` — no new routers needed; agent runs inside the existing pipeline/socket path
- `app/socket.py` — unchanged
- `llm/client.py` — unchanged
- Existing tool registry entries — unchanged
- Any existing agent — unchanged
- Core `Pipeline.run` execution loop — unchanged

---

## Stop Condition — Awaiting Sign-Off

This plan is ready for architectural review. Implementation begins only after explicit approval.

Key decisions to confirm:
1. **Three-tool design** (`check_availability` / `recommend_slots` / `book_reservation`) vs. fewer, richer tools
2. **GCal auth method** — service account JSON in env var (proposed) vs. OAuth2 flow
3. **Business hours config** — env vars (proposed) vs. stored per-tenant in `agent_configs.config_json`
4. **Timezone handling** — single global `SCHEDULING_TIMEZONE` (proposed) vs. per-booking timezone passed by client
5. **Reservation table scope** — global (proposed, any tenant) vs. add a required `tenant_id` FK that blocks cross-tenant reads
