from dataclasses import dataclass
from typing import Any, Callable

from app.tools.calculator import CALCULATOR_TOOL, calculate
from app.tools.row_lookup import ROW_LOOKUP_TOOL, lookup_rows
from app.tools.sheets_lookup import SHEETS_LOOKUP_TOOL, fetch_google_sheet


@dataclass
class ToolEntry:
    declaration: dict
    fn: Callable[..., Any]


TOOL_REGISTRY: dict[str, ToolEntry] = {
    "calculator": ToolEntry(declaration=CALCULATOR_TOOL, fn=calculate),
    "csv_lookup": ToolEntry(declaration=ROW_LOOKUP_TOOL, fn=lookup_rows),
    "sheets_lookup": ToolEntry(declaration=SHEETS_LOOKUP_TOOL, fn=fetch_google_sheet),
}

ALL_TOOLS = [entry.declaration for entry in TOOL_REGISTRY.values()]

# Maps the OpenAI function name (what the model calls) back to its registry key.
# Registry keys and function names differ (e.g. "calculator" -> "calculate"),
# so dispatch must resolve by function name.
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
    """Return a dispatcher scoped to the given tool names.

    `name` may be the OpenAI function name (as the model emits it) or the
    registry key; both resolve to the same tool.
    """
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
    "lookup_rows",
    "ROW_LOOKUP_TOOL",
    "calculate",
    "CALCULATOR_TOOL",
    "fetch_google_sheet",
    "SHEETS_LOOKUP_TOOL",
]
