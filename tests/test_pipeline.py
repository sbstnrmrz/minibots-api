"""Pipeline integration test with mocked LLM.

Confirms Pipeline threads AgentContext through agents and that the
final ctx.input is returned. We use a stub Agent so the test never
opens a network connection or a DB session.
"""

import dataclasses

from app.agents.base import Agent, AgentContext, Pipeline
from app.agents.examples import SanitizerAgent, TruncateAgent


class _UppercaseAgent(Agent):
    def run(self, ctx: AgentContext) -> AgentContext:
        return dataclasses.replace(ctx, input=ctx.input.upper())


class _AppendAgent(Agent):
    def __init__(self, suffix: str) -> None:
        super().__init__()
        self._suffix = suffix

    def run(self, ctx: AgentContext) -> AgentContext:
        return dataclasses.replace(ctx, input=ctx.input + self._suffix)


def test_pipeline_threads_context_through_agents():
    pipeline = Pipeline([_UppercaseAgent(), _AppendAgent("!")])
    out = pipeline.run(AgentContext(input="hola"))
    assert out == "HOLA!"


def test_sanitizer_collapses_internal_whitespace():
    pipeline = Pipeline([SanitizerAgent()])
    out = pipeline.run(AgentContext(input="  hola   mundo \t!"))
    assert out == "hola mundo !"


def test_truncate_appends_ellipsis_when_over_limit():
    pipeline = Pipeline([TruncateAgent(max_length=5)])
    out = pipeline.run(AgentContext(input="hello world"))
    assert out.endswith("…")
    assert len(out) <= 6


def test_truncate_leaves_short_input_alone():
    pipeline = Pipeline([TruncateAgent(max_length=50)])
    out = pipeline.run(AgentContext(input="short"))
    assert out == "short"


def test_pipeline_propagates_chat_id():
    """chat_id stays on AgentContext across the chain so memory keys hit
    the same session."""
    seen: list[str | None] = []

    class _SpyAgent(Agent):
        def run(self, ctx):
            seen.append(ctx.chat_id)
            return ctx

    Pipeline([_SpyAgent(), _SpyAgent()]).run(
        AgentContext(input="x", chat_id="chat-123")
    )
    assert seen == ["chat-123", "chat-123"]
