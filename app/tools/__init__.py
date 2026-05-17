from dataclasses import dataclass
from typing import Any, Callable

from google.genai import types

from app.tools.calculator import CALCULATOR_TOOL, calculate
from app.tools.row_lookup import ROW_LOOKUP_TOOL, lookup_rows


@dataclass
class ToolEntry:
    declaration: types.Tool
    fn: Callable[..., Any]


TOOL_REGISTRY: dict[str, ToolEntry] = {
    "calculator": ToolEntry(declaration=CALCULATOR_TOOL, fn=calculate),
    "csv_lookup": ToolEntry(declaration=ROW_LOOKUP_TOOL, fn=lookup_rows),
}

ALL_TOOLS = [entry.declaration for entry in TOOL_REGISTRY.values()]


def dispatch(name: str, args: dict[str, Any]) -> Any:
    if name not in TOOL_REGISTRY:
        raise ValueError(f"Unknown tool: '{name}'")
    return TOOL_REGISTRY[name].fn(**args)


def get_tools_for_agent(tool_names: list[str]) -> list[types.Tool]:
    """Return Gemini Tool declarations for the given tool names."""
    return [TOOL_REGISTRY[n].declaration for n in tool_names if n in TOOL_REGISTRY]


def make_dispatcher_for_agent(tool_names: list[str]) -> Callable[[str, dict[str, Any]], Any]:
    """Return a dispatcher scoped to the given tool names."""
    allowed = set(tool_names)

    def dispatcher(name: str, args: dict[str, Any]) -> Any:
        if name not in allowed or name not in TOOL_REGISTRY:
            raise ValueError(f"Tool '{name}' not available to this agent.")
        return TOOL_REGISTRY[name].fn(**args)

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
]
