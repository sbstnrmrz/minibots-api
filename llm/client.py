"""Unified LLM client — single entry point for every provider.

All providers are reached through the OpenAI Python SDK via their
OpenAI-compatible endpoints. No agent code should import a provider SDK
directly; call `call_llm()` instead.

Supported providers
-------------------
GEMINI
  base_url : https://generativelanguage.googleapis.com/v1beta/openai/
  api_key  : env GEMINI_API_KEY
  models   : gemini-2.0-flash, gemini-2.5-pro, gemini-2.5-flash

DEEPSEEK
  base_url : https://api.deepseek.com
  api_key  : env DEEPSEEK_API_KEY
  models   : deepseek-v4-flash, deepseek-v4-pro

Required environment variables
------------------------------
GEMINI_API_KEY    API key for the Gemini OpenAI-compatible endpoint.
DEEPSEEK_API_KEY  API key for DeepSeek.
LLM_PROVIDER      Default provider for DEFAULT_LLM_CONFIG (e.g. "GEMINI").
LLM_MODEL         Default model for DEFAULT_LLM_CONFIG (e.g. "gemini-2.5-flash").

Adding a new provider: add one entry to `LLMProvider` and one entry to
`_PROVIDER_SETTINGS` — nothing else changes.
"""

import json
import os
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable

import openai
from dotenv import load_dotenv

load_dotenv()


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


# Module-level client cache — one openai.OpenAI instance per provider.
_clients: dict[LLMProvider, openai.OpenAI] = {}


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

    while True:
        try:
            res = client.chat.completions.create(
                model=config.model,
                messages=current,
                max_tokens=config.max_tokens,
                temperature=config.temperature,
                tools=tools or openai.NOT_GIVEN,
            )
        except Exception as e:
            raise RuntimeError(
                f"[{config.provider.value}] LLM call failed: {e}"
            ) from e

        choice = res.choices[0]
        tool_calls = choice.message.tool_calls

        if not tool_calls:
            return choice.message.content or ""

        # Append the assistant turn that requested the tool calls.
        current.append(choice.message.model_dump(exclude_none=True))

        for tc in tool_calls:
            try:
                args = json.loads(tc.function.arguments or "{}")
                result = dispatcher(tc.function.name, args)  # type: ignore[misc]
            except Exception as e:
                result = {"error": str(e)}
            current.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": json.dumps(result, default=str),
            })


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
    try:
        res = client.embeddings.create(model=model, input=text)
    except Exception as e:
        raise RuntimeError(
            f"[{provider.value}] embedding call failed: {e}"
        ) from e
    return res.data[0].embedding


def _default_config() -> LLMConfig:
    """Build DEFAULT_LLM_CONFIG from env, falling back to the project's prior default."""
    provider_name = os.getenv("LLM_PROVIDER", "GEMINI").upper()
    try:
        provider = LLMProvider(provider_name)
    except ValueError:
        provider = LLMProvider.GEMINI
    model = os.getenv("LLM_MODEL", "gemini-2.5-flash")
    return LLMConfig(provider=provider, model=model)


DEFAULT_LLM_CONFIG: LLMConfig = _default_config()
