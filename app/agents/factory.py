from typing import TYPE_CHECKING

from sqlalchemy.orm import Session as _Session

from app import models
from app.agents.base import Agent, Pipeline
from app.agents.business_analyzer_agent import BusinessAnalyzerAgent
from app.agents.examples import SanitizerAgent, TruncateAgent
from app.agents.generic_info_agent import GenericInfoAgent
from app.agents.intent_analyzer import IntentAnalyzerAgent
from app.agents.rag_info_agent import RAGInfoAgent, RAG_INFO_SYSTEM_PROMPT
from rag.store import get_namespace

if TYPE_CHECKING:
    from app.agents.memory import MemoryStore


def _links_context(agent_config: models.AgentConfig) -> str:
    """Build an 'Available resources' block from the agent's links, or empty string."""
    links: list[dict] = agent_config.links or []
    if not links:
        return ""
    lines = "\n".join(f"- {l['label']}: {l['url']}" for l in links if l.get("url"))
    return f"\n\nAvailable resources (call fetch_google_sheet to read a spreadsheet):\n{lines}"


def _augment_tools_from_links(agent_config: models.AgentConfig, tool_names: list[str]) -> list[str]:
    """Auto-inject sheets_lookup when the agent has Google Sheets links."""
    links: list[dict] = agent_config.links or []
    has_sheets = any(
        "docs.google.com/spreadsheets" in (l.get("url") or "")
        for l in links
    )
    if has_sheets and "sheets_lookup" not in tool_names:
        return [*tool_names, "sheets_lookup"]
    return tool_names


def _build_agent(
    agent_config: models.AgentConfig,
    tool_names: list[str],
    workflow_id: int | None = None,
    db: "_Session | None" = None,
) -> Agent:
    config: dict = agent_config.config_json or {}
    agent_type: str = agent_config.agent_type
    tool_names = _augment_tools_from_links(agent_config, tool_names)
    links_ctx = _links_context(agent_config)

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
        base_prompt = agent_config.system_prompt or RAG_INFO_SYSTEM_PROMPT
        return RAGInfoAgent(
            namespace=namespace,
            system_prompt=base_prompt + links_ctx,
            top_k=config.get("top_k", 5),
            tool_names=tool_names,
        )

    if agent_type == "generic_info":
        base_prompt = agent_config.system_prompt or ""
        kwargs: dict = {"tool_names": tool_names}
        combined = base_prompt + links_ctx
        if combined:
            kwargs["system_prompt"] = combined
        return GenericInfoAgent(**kwargs)

    if agent_type == "business_analyzer":
        return BusinessAnalyzerAgent()

    if agent_type == "sanitizer":
        return SanitizerAgent(tool_names=tool_names)

    if agent_type == "truncate":
        return TruncateAgent(
            max_length=config.get("max_length", 200),
            tool_names=tool_names,
        )

    if agent_type == "scheduler":
        from app.agents.scheduling_agent import SchedulingAgent, SCHEDULING_SYSTEM_PROMPT
        base_prompt = agent_config.system_prompt or SCHEDULING_SYSTEM_PROMPT
        tenant_id = config.get("tenant_id")
        calendar_id: str | None = None
        if tenant_id and db is not None:
            tenant = db.query(models.Tenant).filter(models.Tenant.id == tenant_id).first()
            if tenant:
                calendar_id = tenant.gcal_calendar_id
        elif tenant_id and db is None:
            import logging as _logging
            _logging.getLogger("factory").warning(
                "scheduler agent config %s has tenant_id but no db session was provided; "
                "calendar_id will be None",
                agent_config.id,
            )
        return SchedulingAgent(
            system_prompt=base_prompt,
            tool_names=tool_names or None,
            tenant_id=tenant_id,
            calendar_id=calendar_id,
        )

    raise ValueError(f"Unknown agent_type: '{agent_type}'")


def build_pipeline(
    workflow_id: int,
    db: "_Session",
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

        agents.append(_build_agent(agent_config, tool_names, workflow_id=workflow_id, db=db))

    return Pipeline(agents, memory_store=memory_store)
