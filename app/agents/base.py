import logging
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.agents.memory import MemoryStore

logger = logging.getLogger(__name__)


class Agent(ABC):
    @property
    def name(self) -> str:
        return self.__class__.__name__

    @abstractmethod
    def run(self, input: str) -> str: ...


class Pipeline:
    def __init__(
        self,
        agents: list[Agent],
        memory_store: "MemoryStore | None" = None,
    ) -> None:
        self.agents = agents
        self._memory = memory_store

    def run(self, user_input: str, session_id: str | None = None) -> str:
        current = user_input
        for agent in self.agents:
            raw_input = current  # preserve before context injection

            if self._memory and session_id:
                history = self._memory.load(session_id, agent.name)
                if history:
                    context = "\n".join(f"{m['role']}: {m['content']}" for m in history)
                    current = f"[Prior context]\n{context}\n\n[Current input]\n{current}"
                self._memory.save(session_id, agent.name, "user", raw_input)

            logger.info("[%s] input: %s", agent.name, current)
            current = agent.run(current)
            logger.info("[%s] output: %s", agent.name, current)

            if self._memory and session_id:
                self._memory.save(session_id, agent.name, "assistant", current)

        return current
