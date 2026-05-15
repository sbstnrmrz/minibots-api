from pathlib import Path
from typing import Any

import pandas as pd
from google.genai import types


def lookup_rows(file_path: str, column: str, value: str) -> list[dict]:
    ext = Path(file_path).suffix.lower()
    if ext == ".csv":
        df = pd.read_csv(file_path, dtype=str)
    elif ext in (".xlsx", ".xls"):
        df = pd.read_excel(file_path, dtype=str)
    else:
        raise ValueError(f"Unsupported file type: '{ext}'. Use .csv, .xlsx, or .xls.")

    if column not in df.columns:
        raise ValueError(f"Column '{column}' not found. Available: {list(df.columns)}")

    needle = value.strip().lower()
    mask = df[column].str.strip().str.lower() == needle
    return df[mask].to_dict(orient="records")


ROW_LOOKUP_TOOL = types.Tool(
    function_declarations=[
        types.FunctionDeclaration(
            name="lookup_rows",
            description=(
                "Look up rows in a CSV or Excel file where a column value matches "
                "the given search value (case-insensitive)."
            ),
            parameters=types.Schema(
                type=types.Type.OBJECT,
                properties={
                    "file_path": types.Schema(
                        type=types.Type.STRING,
                        description="Path to the .csv, .xlsx, or .xls file.",
                    ),
                    "column": types.Schema(
                        type=types.Type.STRING,
                        description="Column name to search.",
                    ),
                    "value": types.Schema(
                        type=types.Type.STRING,
                        description="Value to match (case-insensitive, whitespace-stripped).",
                    ),
                },
                required=["file_path", "column", "value"],
            ),
        )
    ]
)


def dispatch(name: str, args: dict[str, Any]) -> Any:
    if name == "lookup_rows":
        return lookup_rows(**args)
    raise ValueError(f"Unknown tool: '{name}'")
