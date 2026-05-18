import logging

import httpx

logger = logging.getLogger("sheets")


async def fetch_sheet(spreadsheet_id: str) -> str:
    url = f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/export?format=csv"
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
            res = await client.get(url)
            logger.info("GET %s → %s", url, res.status_code)
            if res.status_code != 200:
                logger.error("error body: %s", res.text[:300])
                return ""
            logger.info("%s chars read", len(res.text))
            return res.text
    except Exception as e:
        logger.exception("exception: %s", e)
        return ""
