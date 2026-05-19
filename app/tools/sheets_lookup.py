import logging
import re

import httpx

from llm.tools import to_openai_tool

logger = logging.getLogger("sheets_lookup")

_SHEETS_ID_RE = re.compile(r"/spreadsheets/d/([a-zA-Z0-9_-]+)")


def _extract_spreadsheet_id(url: str) -> str:
    m = _SHEETS_ID_RE.search(url)
    if not m:
        raise ValueError(f"Cannot extract spreadsheet ID from URL: {url!r}")
    return m.group(1)


def fetch_google_sheet(url: str) -> str:
    spreadsheet_id = _extract_spreadsheet_id(url)
    export_url = f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/export?format=csv"
    try:
        with httpx.Client(timeout=10, follow_redirects=True) as client:
            res = client.get(export_url)
            logger.info("GET %s → %s", export_url, res.status_code)
            if res.status_code != 200:
                logger.error("error body: %s", res.text[:300])
                return f"Error: could not fetch sheet (HTTP {res.status_code})"
            logger.info("%s chars read", len(res.text))
            return res.text
    except Exception as e:
        logger.exception("exception: %s", e)
        return f"Error fetching sheet: {e}"


SHEETS_LOOKUP_TOOL = to_openai_tool(
    name="fetch_google_sheet",
    description=(
        "Fetch the contents of a public Google Sheets spreadsheet as CSV text. "
        "Use when the user asks about data in a linked spreadsheet. "
        "Pass the full Google Sheets URL from the available resources list."
    ),
    parameters={
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "Full Google Sheets URL (https://docs.google.com/spreadsheets/d/...).",
            }
        },
        "required": ["url"],
    },
)
