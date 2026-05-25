"""Unified LLM provider abstraction.

Agents import from here only — never a provider SDK directly.
"""

from llm.client import (
    DEFAULT_LLM_CONFIG,
    LLMConfig,
    LLMProvider,
    acall_llm,
    call_llm,
    embed,
)
from llm.tools import to_openai_tool
from llm.usage import (
    LLMCallRecord,
    get_calls,
    set_agent,
    start_tracking,
)

__all__ = [
    "DEFAULT_LLM_CONFIG",
    "LLMConfig",
    "LLMProvider",
    "LLMCallRecord",
    "acall_llm",
    "call_llm",
    "embed",
    "get_calls",
    "set_agent",
    "start_tracking",
    "to_openai_tool",
]
