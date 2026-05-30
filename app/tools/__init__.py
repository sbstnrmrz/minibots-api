from dataclasses import dataclass
from typing import Any, Callable

from app.tools.calculator import CALCULATOR_TOOL, calculate
from app.tools.row_lookup import ROW_LOOKUP_TOOL, lookup_rows
from app.tools.scheduling import (
    # GCal-native tools (new scheduler prompt)
    GET_EVENTS_TOOL,
    CREATE_EVENT_TOOL,
    DELETE_EVENT_TOOL,
    INBOX_RESERVE_TOOL,
    get_events,
    create_event,
    delete_event,
    inbox_reserve,
    # DB-native tools (backwards compatibility)
    CHECK_AVAILABILITY_TOOL,
    RECOMMEND_SLOTS_TOOL,
    BOOK_RESERVATION_TOOL,
    check_availability,
    recommend_slots,
    book_reservation,
)
from app.tools.sheets_lookup import SHEETS_LOOKUP_TOOL, fetch_google_sheet


@dataclass
class ToolEntry:
    declaration: dict
    fn: Callable[..., Any]


TOOL_REGISTRY: dict[str, ToolEntry] = {
    "calculator": ToolEntry(declaration=CALCULATOR_TOOL, fn=calculate),
    "csv_lookup": ToolEntry(declaration=ROW_LOOKUP_TOOL, fn=lookup_rows),
    "sheets_lookup": ToolEntry(declaration=SHEETS_LOOKUP_TOOL, fn=fetch_google_sheet),
    # GCal-native
    "get_events": ToolEntry(declaration=GET_EVENTS_TOOL, fn=get_events),
    "create_event": ToolEntry(declaration=CREATE_EVENT_TOOL, fn=create_event),
    "delete_event": ToolEntry(declaration=DELETE_EVENT_TOOL, fn=delete_event),
    "inbox_reserve": ToolEntry(declaration=INBOX_RESERVE_TOOL, fn=inbox_reserve),
    # DB-native (kept for existing workflows)
    "check_availability": ToolEntry(declaration=CHECK_AVAILABILITY_TOOL, fn=check_availability),
    "recommend_slots": ToolEntry(declaration=RECOMMEND_SLOTS_TOOL, fn=recommend_slots),
    "book_reservation": ToolEntry(declaration=BOOK_RESERVATION_TOOL, fn=book_reservation),
}

ALL_TOOLS = [entry.declaration for entry in TOOL_REGISTRY.values()]

# Maps the OpenAI function name (what the model calls) back to its registry key.
_FN_NAME_TO_KEY: dict[str, str] = {
    entry.declaration["function"]["name"]: key
    for key, entry in TOOL_REGISTRY.items()
}


def _resolve_key(name: str) -> str | None:
    """Resolve a name (function name or registry key) to its registry key."""
    if name in TOOL_REGISTRY:
        return name
    return _FN_NAME_TO_KEY.get(name)


def dispatch(name: str, args: dict[str, Any]) -> Any:
    key = _resolve_key(name)
    if key is None:
        raise ValueError(f"Unknown tool: '{name}'")
    return TOOL_REGISTRY[key].fn(**args)


def get_tools_for_agent(tool_names: list[str]) -> list[dict]:
    """Return OpenAI-format tool declarations for the given tool names."""
    return [TOOL_REGISTRY[n].declaration for n in tool_names if n in TOOL_REGISTRY]


def make_dispatcher_for_agent(tool_names: list[str]) -> Callable[[str, dict[str, Any]], Any]:
    """Return a dispatcher scoped to the given tool names."""
    allowed = set(tool_names)

    def dispatcher(name: str, args: dict[str, Any]) -> Any:
        key = _resolve_key(name)
        if key is None or key not in allowed:
            raise ValueError(f"Tool '{name}' not available to this agent.")
        return TOOL_REGISTRY[key].fn(**args)

    return dispatcher


__all__ = [
    "ToolEntry",
    "TOOL_REGISTRY",
    "ALL_TOOLS",
    "dispatch",
    "get_tools_for_agent",
    "make_dispatcher_for_agent",
    # GCal-native
    "get_events", "GET_EVENTS_TOOL",
    "create_event", "CREATE_EVENT_TOOL",
    "delete_event", "DELETE_EVENT_TOOL",
    "inbox_reserve", "INBOX_RESERVE_TOOL",
    # DB-native
    "check_availability", "CHECK_AVAILABILITY_TOOL",
    "recommend_slots", "RECOMMEND_SLOTS_TOOL",
    "book_reservation", "BOOK_RESERVATION_TOOL",
    # Other
    "lookup_rows", "ROW_LOOKUP_TOOL",
    "calculate", "CALCULATOR_TOOL",
    "fetch_google_sheet", "SHEETS_LOOKUP_TOOL",
]
