"""
One-time setup: clean DB, create Sanitizer→IntentAnalyzer→RAGInfo workflow,
create a test bot, ingest ../example.md as the workflow knowledge base.
"""
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.database import engine
from app import models
from rag.store import init_rag_table, ingest

EXAMPLE_MD = str(Path(__file__).parent.parent / "example.md")


def clean_db(session: Session) -> None:
    print("Cleaning DB...")
    session.execute(text("UPDATE bots SET workflow_id = NULL"))
    session.execute(text("DELETE FROM agent_tools"))
    session.execute(text("DELETE FROM workflow_agents"))
    session.execute(text("DELETE FROM agent_configs"))
    session.execute(text("DELETE FROM workflows"))
    session.execute(text("DELETE FROM rag_sources"))
    session.execute(text("DELETE FROM rag_chunks"))
    session.execute(text("DELETE FROM agent_memory"))
    session.commit()
    print("  done.")


def setup(session: Session) -> tuple[int, int, str]:
    # Workflow
    workflow = models.Workflow(
        name="hotel-playa-sirena",
        description="Sanitizer → IntentAnalyzer → RAGInfo",
    )
    session.add(workflow)
    session.flush()
    print(f"Workflow      id={workflow.id}")

    # Agent configs
    sanitizer_cfg = models.AgentConfig(
        name="sanitizer",
        agent_type="sanitizer",
    )
    intent_cfg = models.AgentConfig(
        name="intent-analyzer",
        agent_type="intent_analyzer",
    )
    rag_cfg = models.AgentConfig(
        name="rag-info-hotel",
        agent_type="rag_info",
        # no namespace in config_json → resolved from rag_sources at workflow scope
    )
    session.add_all([sanitizer_cfg, intent_cfg, rag_cfg])
    session.flush()
    print(f"AgentConfigs  sanitizer={sanitizer_cfg.id}  intent={intent_cfg.id}  rag={rag_cfg.id}")

    # Ordered pipeline steps
    session.add(models.WorkflowAgent(workflow_id=workflow.id, agent_config_id=sanitizer_cfg.id, position=0))
    session.add(models.WorkflowAgent(workflow_id=workflow.id, agent_config_id=intent_cfg.id,    position=1))
    session.add(models.WorkflowAgent(workflow_id=workflow.id, agent_config_id=rag_cfg.id,       position=2))
    session.flush()
    print("WorkflowAgents created")

    # Bot wired to the workflow
    bot = models.Bot(
        name="Hotel Playa Sirena",
        bot_type="rag_info",
        workflow_id=workflow.id,
    )
    session.add(bot)
    session.flush()
    print(f"Bot           id={bot.id}")

    # Register RAG namespace at workflow scope
    namespace = f"workflow_{workflow.id}"
    session.add(models.RagSource(namespace=namespace, scope_type="workflow", scope_id=workflow.id))
    session.commit()
    print(f"RagSource     namespace={namespace}")

    return workflow.id, bot.id, namespace


def main() -> None:
    if not Path(EXAMPLE_MD).exists():
        raise FileNotFoundError(f"example.md not found at {EXAMPLE_MD}")

    with Session(engine) as session:
        clean_db(session)
        workflow_id, bot_id, namespace = setup(session)

    print(f"\nIngesting {EXAMPLE_MD} → {namespace}")
    init_rag_table(namespace)
    chunks = ingest(EXAMPLE_MD, namespace, source_name="example.md")
    print(f"Ingested {chunks} chunks")

    print(f"""
Setup complete.

  workflow_id = {workflow_id}
  bot_id      = {bot_id}
  namespace   = {namespace}

Test payload:
  {{"message": "¿cuánto cuesta una habitación?", "bot_id": {bot_id}, "chat_id": "test-001"}}
""")


if __name__ == "__main__":
    main()
