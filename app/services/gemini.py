import asyncio
from google import genai
from app.config import GEMINI_API_KEY

_client = genai.Client(api_key=GEMINI_API_KEY)


async def generate_reply(
    contents: list[dict],
    system_prompt: str | None = None,
) -> str:
    config = {"system_instruction": system_prompt} if system_prompt else None
    res = await asyncio.to_thread(
        lambda: _client.models.generate_content(
            model="gemini-2.5-flash",
            contents=contents,
            config=config,
        )
    )
    return res.text
