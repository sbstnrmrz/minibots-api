from pathlib import Path

import pandas as pd

from llm.tools import to_openai_tool


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


ROW_LOOKUP_TOOL = to_openai_tool(
    name="lookup_rows",
    description=(
        "Look up rows in a CSV or Excel file where a column value matches "
        "the given search value (case-insensitive)."
    ),
    parameters={
        "type": "object",
        "properties": {
            "file_path": {
                "type": "string",
                "description": "Path to the .csv, .xlsx, or .xls file.",
            },
            "column": {
                "type": "string",
                "description": "Column name to search.",
            },
            "value": {
                "type": "string",
                "description": "Value to match (case-insensitive, whitespace-stripped).",
            },
        },
        "required": ["file_path", "column", "value"],
    },
)


