"""Chat-completion wrappers.

Thin async adapters over `llm.call_llm`. Kept for backward compatibility:
`chat.py` and `socket.py` still call `generate_reply` / `generate_with_tools`
with Gemini-format `contents`. These functions convert that format to
OpenAI-format `messages` and delegate to the unified LLM client.
"""

import asyncio
import dataclasses
from typing import Any, Callable

from llm import DEFAULT_LLM_CONFIG, call_llm

_ROLE_MAP = {"user": "user", "model": "assistant", "assistant": "assistant"}


def _to_openai_messages(contents: list[dict]) -> list[dict]:
    """Convert Gemini-format `contents` to OpenAI-format `messages`.

    Gemini: {"role": "user"|"model", "parts": [{"text": ...}, ...]}
    OpenAI: {"role": "user"|"assistant", "content": "..."}
    """
    messages = []
    for c in contents:
        role = _ROLE_MAP.get(c.get("role", "user"), "user")
        parts = c.get("parts", [])
        text = "".join(p.get("text", "") for p in parts)
        messages.append({"role": role, "content": text})
    return messages


async def generate_reply(
    contents: list[dict],
    system_prompt: str | None = None,
) -> str:
    config = dataclasses.replace(
        DEFAULT_LLM_CONFIG,
        system_prompt=system_prompt or "",
    )
    messages = _to_openai_messages(contents)
    return await asyncio.to_thread(call_llm, config, messages)


async def generate_with_tools(
    contents: list,
    tools: list[dict],
    dispatcher: Callable[[str, dict[str, Any]], Any],
    system_prompt: str | None = None,
) -> str:
    config = dataclasses.replace(
        DEFAULT_LLM_CONFIG,
        system_prompt=system_prompt or "",
    )
    messages = _to_openai_messages(contents)
    return await asyncio.to_thread(call_llm, config, messages, tools, dispatcher)
