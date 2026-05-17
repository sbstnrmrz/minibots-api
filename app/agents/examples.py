import dataclasses

from app.agents.base import Agent, AgentContext


class SanitizerAgent(Agent):
    """Strips leading/trailing whitespace and collapses internal spaces."""

    def run(self, ctx: AgentContext) -> AgentContext:
        return dataclasses.replace(ctx, input=" ".join(ctx.input.split()))


class TruncateAgent(Agent):
    """Truncates input to max_length characters."""

    def __init__(self, max_length: int = 200, tool_names: list[str] | None = None) -> None:
        super().__init__(tool_names)
        self._max_length = max_length

    def run(self, ctx: AgentContext) -> AgentContext:
        text = ctx.input
        if len(text) > self._max_length:
            text = text[: self._max_length].rstrip() + "…"
        return dataclasses.replace(ctx, input=text)
