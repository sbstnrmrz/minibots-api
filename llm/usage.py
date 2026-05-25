"""LLM usage tracking — zero-dependency, no app imports.

Usage is accumulated in a ContextVar holding a mutable list. The list is
created by the caller before any LLM work begins (`start_tracking()`), and
read after the work completes (`get_calls()`).

Thread-safety note
------------------
`asyncio.to_thread` snapshots the current context at the call site and runs
the thread function with that snapshot. The snapshot holds a *reference* to
the same list object, so appends inside the thread are visible to the outer
async code after the thread exits — no synchronisation needed for this
single-writer / single-reader pattern.
"""

from contextvars import ContextVar
from dataclasses import dataclass, field


@dataclass
class LLMCallRecord:
    """One row of token usage from a single provider API call."""
    provider: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    cost_usd: float | None
    agent_name: str | None = None


# Mutable list shared across the async context and any spawned threads.
_accumulator: ContextVar[list[LLMCallRecord] | None] = ContextVar(
    "_llm_usage", default=None
)

# Name of the agent that is currently executing an LLM call.
_current_agent: ContextVar[str | None] = ContextVar(
    "_llm_agent", default=None
)


def start_tracking() -> None:
    """Reset the accumulator for a new chat turn. Call before any LLM work."""
    _accumulator.set([])


def set_agent(name: str) -> None:
    """Tag subsequent LLM calls with this agent name."""
    _current_agent.set(name)


def record(rec: LLMCallRecord) -> None:
    """Append a completed LLM call. Called internally by llm/client.py."""
    acc = _accumulator.get()
    if acc is not None:
        acc.append(rec)


def get_calls() -> list[LLMCallRecord]:
    """Return all calls accumulated since the last `start_tracking()`."""
    return _accumulator.get() or []


def get_current_agent() -> str | None:
    return _current_agent.get()
