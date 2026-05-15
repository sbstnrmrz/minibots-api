from typing import Any

from app.tools.calculator import CALCULATOR_TOOL, calculate
from app.tools.row_lookup import ROW_LOOKUP_TOOL, lookup_rows

ALL_TOOLS = [ROW_LOOKUP_TOOL, CALCULATOR_TOOL]


def dispatch(name: str, args: dict[str, Any]) -> Any:
    if name == "lookup_rows":
        return lookup_rows(**args)
    if name == "calculate":
        return calculate(**args)
    raise ValueError(f"Unknown tool: '{name}'")


__all__ = [
    "lookup_rows",
    "ROW_LOOKUP_TOOL",
    "calculate",
    "CALCULATOR_TOOL",
    "ALL_TOOLS",
    "dispatch",
]
