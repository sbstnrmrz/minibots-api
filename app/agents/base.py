import dataclasses
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.agents.memory import MemoryStore

logger = logging.getLogger("pipeline")


@dataclass
class AgentContext:
    input: str
    chat_id: str | None = None
    retrieval_query: str | None = None


class Agent(ABC):
    # Set True in agents that manage their own conversation memory internally
    # (e.g. RAGInfoAgent, GenericInfoAgent). Pipeline.run will skip its own
    # memory injection for those agents to avoid injecting history twice.
    manages_own_memory: bool = False

    def __init__(self, tool_names: list[str] | None = None) -> None:
        self._tool_names: list[str] = tool_names or []

    @property
    def name(self) -> str:
        return self.__class__.__name__

    @abstractmethod
    def run(self, ctx: AgentContext) -> AgentContext: ...


class Pipeline:
    def __init__(
        self,
        agents: list[Agent],
        memory_store: "MemoryStore | None" = None,
    ) -> None:
        self.agents = agents
        self._memory = memory_store

    def run(self, ctx: AgentContext) -> str:
        current = ctx
        session_id = ctx.chat_id

        chain = " → ".join(a.name.removesuffix("Agent") for a in self.agents)
        logger.info("┌─ Pipeline start  chat_id=%s  agents=[%s]", session_id, chain)
        logger.info("│  input: %s", current.input)

        for agent in self.agents:
            raw_input = current.input

            # Skip Pipeline-level memory injection for agents that manage their
            # own memory internally — injecting twice creates duplicate history.
            if self._memory and session_id and not agent.manages_own_memory:
                history = self._memory.load(session_id, agent.name)
                if history:
                    context_str = "\n".join(
                        f"{m['role']}: {m['content']}" for m in history
                    )
                    current = dataclasses.replace(
                        current,
                        input=f"[Prior context]\n{context_str}\n\n[Current input]\n{current.input}",
                    )
                self._memory.save(session_id, agent.name, "user", raw_input)

            short = agent.name.removesuffix("Agent")
            logger.info("│  %s ▸ in:  %s", short, current.input)
            current = agent.run(current)
            logger.info("│  %s ▸ out: %s", short, current.input)

            if self._memory and session_id and not agent.manages_own_memory:
                self._memory.save(session_id, agent.name, "assistant", current.input)

        logger.info("└─ Pipeline done   chat_id=%s", session_id)
        logger.info("   output: %s", current.input)
        return current.input
