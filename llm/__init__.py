"""Unified LLM provider abstraction.

Agents import from here only — never a provider SDK directly.
"""

from llm.client import (
    DEFAULT_LLM_CONFIG,
    LLMConfig,
    LLMProvider,
    call_llm,
    embed,
)
from llm.tools import to_openai_tool

__all__ = [
    "DEFAULT_LLM_CONFIG",
    "LLMConfig",
    "LLMProvider",
    "call_llm",
    "embed",
    "to_openai_tool",
]
