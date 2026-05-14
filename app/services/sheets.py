import httpx


async def fetch_sheet(spreadsheet_id: str) -> str:
    url = f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/export?format=csv"
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
            res = await client.get(url)
            print(f"[sheet] GET {url} → {res.status_code}")
            if res.status_code != 200:
                print(f"[sheet] error body: {res.text[:300]}")
                return ""
            print(f"[sheet] {len(res.text)} chars leídos")
            return res.text
    except Exception as e:
        print(f"[sheet] excepción: {e}")
        return ""
