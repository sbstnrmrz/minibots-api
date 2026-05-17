from typing import TYPE_CHECKING

from sqlalchemy.orm import Session

from app import models
from app.agents.base import Agent, Pipeline
from app.agents.examples import SanitizerAgent, TruncateAgent
from app.agents.generic_info_agent import GenericInfoAgent
from app.agents.intent_analyzer import IntentAnalyzerAgent
from app.agents.rag_info_agent import RAGInfoAgent
from rag.store import get_namespace

if TYPE_CHECKING:
    from app.agents.memory import MemoryStore


def _build_agent(
    agent_config: models.AgentConfig,
    tool_names: list[str],
    workflow_id: int | None = None,
) -> Agent:
    config: dict = agent_config.config_json or {}
    agent_type: str = agent_config.agent_type

    if agent_type == "intent_analyzer":
        return IntentAnalyzerAgent(tool_names=tool_names)

    if agent_type == "rag_info":
        # Resolution order: explicit config → agent-scoped RAG → workflow-scoped RAG
        namespace = (
            config.get("namespace")
            or get_namespace("agent", agent_config.id)
            or (workflow_id is not None and get_namespace("workflow", workflow_id))
            or None
        )
        if not namespace:
            raise ValueError(
                f"AgentConfig {agent_config.id} (rag_info): no namespace in config_json "
                f"and no RAG source registered for agent {agent_config.id}"
                + (f" or workflow {workflow_id}" if workflow_id is not None else "")
            )
        return RAGInfoAgent(
            namespace=namespace,
            system_prompt=agent_config.system_prompt,  # None → RAGInfoAgent uses its default
            top_k=config.get("top_k", 5),
            tool_names=tool_names,
        )

    if agent_type == "generic_info":
        kwargs = {"tool_names": tool_names}
        if agent_config.system_prompt:
            kwargs["system_prompt"] = agent_config.system_prompt
        return GenericInfoAgent(**kwargs)

    if agent_type == "sanitizer":
        return SanitizerAgent(tool_names=tool_names)

    if agent_type == "truncate":
        return TruncateAgent(
            max_length=config.get("max_length", 200),
            tool_names=tool_names,
        )

    raise ValueError(f"Unknown agent_type: '{agent_type}'")


def build_pipeline(
    workflow_id: int,
    db: Session,
    memory_store: "MemoryStore | None" = None,
) -> Pipeline:
    """Load a workflow from the DB and assemble a ready-to-run Pipeline."""
    workflow = db.query(models.Workflow).filter(models.Workflow.id == workflow_id).first()
    if not workflow:
        raise ValueError(f"Workflow {workflow_id} not found.")

    workflow_agents = (
        db.query(models.WorkflowAgent)
        .filter(models.WorkflowAgent.workflow_id == workflow_id)
        .order_by(models.WorkflowAgent.position)
        .all()
    )
    if not workflow_agents:
        raise ValueError(f"Workflow {workflow_id} has no agents configured.")

    agents: list[Agent] = []
    for wa in workflow_agents:
        agent_config = (
            db.query(models.AgentConfig)
            .filter(models.AgentConfig.id == wa.agent_config_id)
            .first()
        )
        if not agent_config:
            raise ValueError(
                f"AgentConfig {wa.agent_config_id} referenced by WorkflowAgent {wa.id} not found."
            )

        tool_rows = (
            db.query(models.AgentTool)
            .filter(models.AgentTool.agent_config_id == agent_config.id)
            .all()
        )
        tool_names = [t.tool_name for t in tool_rows]

        agents.append(_build_agent(agent_config, tool_names, workflow_id=workflow_id))

    return Pipeline(agents, memory_store=memory_store)
