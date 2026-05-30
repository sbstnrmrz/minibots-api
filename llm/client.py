"""Unified LLM client — single entry point for every provider.

All providers are reached through the OpenAI Python SDK via their
OpenAI-compatible endpoints. No agent code should import a provider SDK
directly; call `call_llm()` instead.

Supported providers
-------------------
GEMINI
  base_url : https://generativelanguage.googleapis.com/v1beta/openai/
  api_key  : env GEMINI_API_KEY
  models   : gemini-2.5-flash, gemini-2.5-pro

DEEPSEEK
  base_url : https://api.deepseek.com
  api_key  : env DEEPSEEK_API_KEY
  models   : deepseek-v4-flash, deepseek-v4-pro

Required environment variables
------------------------------
GEMINI_API_KEY    API key for the Gemini OpenAI-compatible endpoint.
DEEPSEEK_API_KEY  API key for DeepSeek.
LLM_PROVIDER      Default provider for DEFAULT_LLM_CONFIG (e.g. "DEEPSEEK").
LLM_MODEL         Default model for DEFAULT_LLM_CONFIG (e.g. "deepseek-v4-flash").
                  When unset, defaults to DEEPSEEK / deepseek-v4-flash.

Adding a new provider: add one entry to `LLMProvider` and one entry to
`_PROVIDER_SETTINGS` — nothing else changes.
"""

import asyncio
import json
import logging
import os
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable

import openai
from dotenv import load_dotenv

from llm.pricing import compute_cost
from llm.usage import LLMCallRecord, get_current_agent, record as _record_usage

load_dotenv()

logger = logging.getLogger("llm")

# Max chars of message/response content written to a single log line.
_LOG_PREVIEW = 500


def _preview(text: str) -> str:
    """Truncate text for log output."""
    text = str(text).replace("\n", " ")
    return text if len(text) <= _LOG_PREVIEW else text[:_LOG_PREVIEW] + f"… (+{len(text) - _LOG_PREVIEW} chars)"


class LLMProvider(str, Enum):
    GEMINI = "GEMINI"
    DEEPSEEK = "DEEPSEEK"


# Per-provider connection settings: base_url + the env var holding the key.
_PROVIDER_SETTINGS: dict[LLMProvider, dict[str, str]] = {
    LLMProvider.GEMINI: {
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
        "api_key_env": "GEMINI_API_KEY",
    },
    LLMProvider.DEEPSEEK: {
        "base_url": "https://api.deepseek.com",
        "api_key_env": "DEEPSEEK_API_KEY",
    },
}


@dataclass
class LLMConfig:
    provider: LLMProvider
    model: str
    max_tokens: int = 1000
    temperature: float = 0.7
    system_prompt: str = ""
    # Per-call HTTP timeout (seconds). The OpenAI SDK default of 600s is
    # too long for an interactive chat backend; a hung provider would tie
    # up a worker thread for ten minutes.
    timeout: float = 60.0
    # Hard cap on tool-calling rounds. Stops a model that keeps emitting
    # tool_calls in a loop from burning unbounded credits.
    max_tool_rounds: int = 10


# Module-level client cache — one client of each variant per provider.
_clients: dict[LLMProvider, openai.OpenAI] = {}
_async_clients: dict[LLMProvider, openai.AsyncOpenAI] = {}


def _get_client(provider: LLMProvider) -> openai.OpenAI:
    """Return a cached openai.OpenAI client for the provider, creating it once."""
    if provider not in _clients:
        settings = _PROVIDER_SETTINGS[provider]
        api_key = os.getenv(settings["api_key_env"], "")
        _clients[provider] = openai.OpenAI(
            base_url=settings["base_url"],
            api_key=api_key,
        )
    return _clients[provider]


def _get_async_client(provider: LLMProvider) -> openai.AsyncOpenAI:
    """Return a cached openai.AsyncOpenAI client for the provider."""
    if provider not in _async_clients:
        settings = _PROVIDER_SETTINGS[provider]
        api_key = os.getenv(settings["api_key_env"], "")
        _async_clients[provider] = openai.AsyncOpenAI(
            base_url=settings["base_url"],
            api_key=api_key,
        )
    return _async_clients[provider]


def _build_messages(config: LLMConfig, messages: list[dict]) -> list[dict]:
    """Prepend the config system prompt unless the caller already supplied one."""
    if config.system_prompt and not (messages and messages[0].get("role") == "system"):
        return [{"role": "system", "content": config.system_prompt}, *messages]
    return list(messages)


def call_llm(
    config: LLMConfig,
    messages: list[dict],
    tools: list[dict] | None = None,
    dispatcher: Callable[[str, dict[str, Any]], Any] | None = None,
) -> str:
    """Single choke point for every LLM call.

    Args:
        config: provider, model and generation parameters.
        messages: OpenAI-format message list (system/user/assistant/tool).
        tools: optional OpenAI function-calling tool definitions.
        dispatcher: required when `tools` is given — executes a tool call
            by name and returns its result. The tool loop runs internally
            until the model produces a plain-text answer.

    Returns:
        The model's final plain-text response.

    Raises:
        ValueError: tools supplied without a dispatcher.
        RuntimeError: provider API error, tagged with the provider name.
    """
    if tools and dispatcher is None:
        raise ValueError("`tools` supplied without a `dispatcher` to execute them.")

    client = _get_client(config.provider)
    current = _build_messages(config, messages)

    tool_names = [t["function"]["name"] for t in tools] if tools else []
    logger.info(
        "   → %s/%s  msgs=%d  tools=%s",
        config.provider.value, config.model, len(current),
        ",".join(tool_names) if tool_names else "none",
    )
    for m in current:
        logger.debug("     msg[%s]: %s", m.get("role"), _preview(m.get("content") or ""))

    round_n = 0
    while True:
        round_n += 1
        if round_n > config.max_tool_rounds:
            logger.error(
                "   ✗ %s/%s exceeded max_tool_rounds=%d — aborting tool loop",
                config.provider.value, config.model, config.max_tool_rounds,
            )
            raise RuntimeError(
                f"[{config.provider.value}] LLM tool loop exceeded "
                f"max_tool_rounds={config.max_tool_rounds}"
            )
        try:
            res = client.chat.completions.create(
                model=config.model,
                messages=current,
                max_tokens=config.max_tokens,
                temperature=config.temperature,
                tools=tools or openai.NOT_GIVEN,
                timeout=config.timeout,
            )
        except Exception as e:
            logger.error("   ✗ %s/%s call failed: %s", config.provider.value, config.model, e)
            raise RuntimeError(
                f"[{config.provider.value}] LLM call failed: {e}"
            ) from e

        if res.usage:
            u = res.usage
            _record_usage(LLMCallRecord(
                provider=config.provider.value,
                model=config.model,
                prompt_tokens=u.prompt_tokens,
                completion_tokens=u.completion_tokens,
                total_tokens=u.total_tokens,
                cost_usd=compute_cost(
                    config.provider.value, config.model,
                    u.prompt_tokens, u.completion_tokens,
                ),
                agent_name=get_current_agent(),
            ))

        choice = res.choices[0]
        tool_calls = choice.message.tool_calls

        if not tool_calls:
            reply = choice.message.content or ""
            logger.info("   ← reply (rounds=%d): %s", round_n, _preview(reply))
            return reply

        # Append the assistant turn that requested the tool calls.
        current.append(choice.message.model_dump(exclude_none=True))

        for tc in tool_calls:
            try:
                args = json.loads(tc.function.arguments or "{}")
                logger.info("   → tool %s(%s)", tc.function.name, args)
                result = dispatcher(tc.function.name, args)  # type: ignore[misc]
                logger.info("   ← tool %s: %s", tc.function.name, _preview(result))
            except Exception as e:
                result = {"error": str(e)}
                logger.warning("   ✗ tool %s failed: %s", tc.function.name, e)
            current.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": json.dumps(result, default=str),
            })


async def acall_llm(
    config: LLMConfig,
    messages: list[dict],
    tools: list[dict] | None = None,
    dispatcher: Callable[[str, dict[str, Any]], Any] | None = None,
) -> str:
    """Async variant of `call_llm`.

    Uses `openai.AsyncOpenAI` so the request handler never has to park a
    thread waiting on the provider.

    A synchronous dispatcher (the common case — RAG retrieval embeds the
    query over a blocking HTTP call, sheets/CSV tools do blocking I/O) is
    run in a worker thread via `asyncio.to_thread` so it never stalls the
    event loop. An `async def` dispatcher is awaited directly.
    """
    if tools and dispatcher is None:
        raise ValueError("`tools` supplied without a `dispatcher` to execute them.")

    client = _get_async_client(config.provider)
    current = _build_messages(config, messages)

    tool_names = [t["function"]["name"] for t in tools] if tools else []
    logger.info(
        "   → %s/%s (async)  msgs=%d  tools=%s",
        config.provider.value, config.model, len(current),
        ",".join(tool_names) if tool_names else "none",
    )

    round_n = 0
    while True:
        round_n += 1
        if round_n > config.max_tool_rounds:
            logger.error(
                "   ✗ %s/%s exceeded max_tool_rounds=%d — aborting tool loop",
                config.provider.value, config.model, config.max_tool_rounds,
            )
            raise RuntimeError(
                f"[{config.provider.value}] LLM tool loop exceeded "
                f"max_tool_rounds={config.max_tool_rounds}"
            )
        try:
            res = await client.chat.completions.create(
                model=config.model,
                messages=current,
                max_tokens=config.max_tokens,
                temperature=config.temperature,
                tools=tools or openai.NOT_GIVEN,
                timeout=config.timeout,
            )
        except Exception as e:
            logger.error("   ✗ %s/%s call failed: %s", config.provider.value, config.model, e)
            raise RuntimeError(
                f"[{config.provider.value}] LLM call failed: {e}"
            ) from e

        if res.usage:
            u = res.usage
            _record_usage(LLMCallRecord(
                provider=config.provider.value,
                model=config.model,
                prompt_tokens=u.prompt_tokens,
                completion_tokens=u.completion_tokens,
                total_tokens=u.total_tokens,
                cost_usd=compute_cost(
                    config.provider.value, config.model,
                    u.prompt_tokens, u.completion_tokens,
                ),
                agent_name=get_current_agent(),
            ))

        choice = res.choices[0]
        tool_calls = choice.message.tool_calls

        if not tool_calls:
            reply = choice.message.content or ""
            logger.info("   ← reply (rounds=%d): %s", round_n, _preview(reply))
            return reply

        current.append(choice.message.model_dump(exclude_none=True))

        for tc in tool_calls:
            try:
                args = json.loads(tc.function.arguments or "{}")
                logger.info("   → tool %s(%s)", tc.function.name, args)
                result = await _run_dispatcher_async(dispatcher, tc.function.name, args)
                logger.info("   ← tool %s: %s", tc.function.name, _preview(result))
            except Exception as e:
                result = {"error": str(e)}
                logger.warning("   ✗ tool %s failed: %s", tc.function.name, e)
            current.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": json.dumps(result, default=str),
            })


async def _run_dispatcher_async(
    dispatcher: Callable[[str, dict[str, Any]], Any],
    name: str,
    args: dict[str, Any],
) -> Any:
    """Run a tool dispatcher from async code without blocking the event loop.

    - An `async def` dispatcher is awaited directly.
    - A synchronous dispatcher (the common case — RAG retrieval embeds the
      query over blocking HTTP, sheets/CSV tools do blocking I/O) is run in
      a worker thread via `asyncio.to_thread`, so a slow tool no longer
      stalls every other in-flight request sharing the loop.
    """
    if asyncio.iscoroutinefunction(dispatcher):
        return await dispatcher(name, args)  # type: ignore[misc]
    return await asyncio.to_thread(dispatcher, name, args)


def embed(
    text: str,
    model: str = "gemini-embedding-001",
    provider: LLMProvider = LLMProvider.GEMINI,
) -> list[float]:
    """Return an embedding vector for `text` via the provider's OpenAI-compatible API.

    Args:
        text: input string to embed.
        model: embedding model name. Default `gemini-embedding-001` (3072 dims).
        provider: which provider to use. DeepSeek has no embeddings endpoint —
            keep the default GEMINI for embeddings.

    Raises:
        RuntimeError: provider API error, tagged with the provider name.
    """
    client = _get_client(provider)
    logger.info("   → embed %s/%s  chars=%d", provider.value, model, len(text))
    try:
        res = client.embeddings.create(model=model, input=text, timeout=60.0)
    except Exception as e:
        logger.error("   ✗ embed %s/%s failed: %s", provider.value, model, e)
        raise RuntimeError(
            f"[{provider.value}] embedding call failed: {e}"
        ) from e
    vector = res.data[0].embedding
    logger.info("   ← embed %s  dim=%d", model, len(vector))
    if hasattr(res, "usage") and res.usage:
        u = res.usage
        prompt_toks = getattr(u, "prompt_tokens", 0) or 0
        total_toks = getattr(u, "total_tokens", prompt_toks)
        _record_usage(LLMCallRecord(
            provider=provider.value,
            model=model,
            prompt_tokens=prompt_toks,
            completion_tokens=0,
            total_tokens=total_toks,
            cost_usd=compute_cost(provider.value, model, prompt_toks, 0),
            agent_name="embedding",
        ))
    return vector


def _default_config() -> LLMConfig:
    """Build DEFAULT_LLM_CONFIG from env, falling back to the project default."""
    provider_name = os.getenv("LLM_PROVIDER", "DEEPSEEK").upper()
    try:
        provider = LLMProvider(provider_name)
    except ValueError:
        provider = LLMProvider.DEEPSEEK
    model = os.getenv("LLM_MODEL", "deepseek-v4-flash")
    return LLMConfig(provider=provider, model=model)


DEFAULT_LLM_CONFIG: LLMConfig = _default_config()
