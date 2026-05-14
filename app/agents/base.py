import logging
from abc import ABC, abstractmethod

logger = logging.getLogger(__name__)


class Agent(ABC):
    @property
    def name(self) -> str:
        return self.__class__.__name__

    @abstractmethod
    def run(self, input: str) -> str: ...


class Pipeline:
    def __init__(self, agents: list[Agent]) -> None:
        self.agents = agents

    def run(self, user_input: str) -> str:
        current = user_input
        for agent in self.agents:
            logger.info("[%s] input: %s", agent.name, current)
            current = agent.run(current)
            logger.info("[%s] output: %s", agent.name, current)
        return current
