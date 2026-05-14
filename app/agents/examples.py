from app.agents.base import Agent


class SanitizerAgent(Agent):
    """Strips leading/trailing whitespace and collapses internal spaces."""

    def run(self, input: str) -> str:
        return " ".join(input.split())


class TruncateAgent(Agent):
    """Truncates input to max_length characters."""

    def __init__(self, max_length: int = 200) -> None:
        self._max_length = max_length

    def run(self, input: str) -> str:
        if len(input) <= self._max_length:
            return input
        return input[: self._max_length].rstrip() + "…"
