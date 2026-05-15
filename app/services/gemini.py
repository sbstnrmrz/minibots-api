import asyncio
from typing import Any, Callable

from google import genai
from google.genai import types

from app.config import GEMINI_API_KEY

_client = genai.Client(api_key=GEMINI_API_KEY)

_MODEL = "gemini-2.5-flash"


async def generate_reply(
    contents: list[dict],
    system_prompt: str | None = None,
) -> str:
    config = types.GenerateContentConfig(
        system_instruction=system_prompt,
    ) if system_prompt else None
    res = await asyncio.to_thread(
        lambda: _client.models.generate_content(
            model=_MODEL,
            contents=contents,
            config=config,
        )
    )
    return res.text


async def generate_with_tools(
    contents: list,
    tools: list[types.Tool],
    dispatcher: Callable[[str, dict[str, Any]], Any],
    system_prompt: str | None = None,
) -> str:
    config = types.GenerateContentConfig(
        tools=tools,
        system_instruction=system_prompt,
    )
    current = list(contents)

    while True:
        res = await asyncio.to_thread(
            lambda c=current: _client.models.generate_content(
                model=_MODEL,
                contents=c,
                config=config,
            )
        )

        candidate = res.candidates[0]
        function_calls = [
            part.function_call
            for part in candidate.content.parts
            if part.function_call
        ]

        if not function_calls:
            return res.text

        current.append(candidate.content)

        responses = []
        for fc in function_calls:
            try:
                result = dispatcher(fc.name, dict(fc.args))
            except Exception as e:
                result = {"error": str(e)}
            responses.append(
                types.Part(
                    function_response=types.FunctionResponse(
                        name=fc.name,
                        response={"result": result},
                    )
                )
            )

        current.append(types.Content(role="user", parts=responses))
