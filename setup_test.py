"""
One-time dev setup: wipe all data, create a test tenant + workflow + bot,
ingest ../example.md as the workflow knowledge base.

Run once after `docker compose up -d` and `uv run python migrate.py`.
"""
import secrets
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.database import engine
from app import models
from rag.store import init_rag_table, ingest

EXAMPLE_MD = str(Path(__file__).parent.parent / "example.md")

TEST_TENANT_ID = "fcbb503a-6e49-4e4c-ac58-fc232064513e"
TEST_TENANT_SLUG = "test-tenant"


def clean_db(session: Session) -> None:
    print("Cleaning DB...")
    # Null FK cycles before deleting referenced rows
    session.execute(text("UPDATE tenants SET agent_config_id = NULL"))
    session.execute(text("UPDATE bots SET workflow_id = NULL"))
    session.commit()

    tables = [
        "reservations",
        "agent_memory",
        "chat_messages",
        "chats",
        "rag_chunks",
        "rag_sources",
        "agent_tools",
        "workflow_agents",
        "agents_general_info",
        "tenant_files",
        "agent_configs",
        "workflows",
        "bots",
        "tenants",
    ]
    for t in tables:
        session.execute(text(f"DELETE FROM {t}"))
    session.commit()
    print("  done.")


def setup(session: Session) -> tuple[str, int, int, str]:
    api_token = secrets.token_urlsafe(32)

    tenant = models.Tenant(
        id=TEST_TENANT_ID,
        name="Hotel Playa Sirena",
        slug=TEST_TENANT_SLUG,
        agent_tier=models.AgentTier.support,
        api_token=api_token,
    )
    session.add(tenant)
    session.flush()
    print(f"Tenant        id={tenant.id}  slug={tenant.slug}")

    workflow = models.Workflow(
        tenant_id=tenant.id,
        name="hotel-playa-sirena",
        description="Sanitizer → IntentAnalyzer → RAGInfo",
    )
    session.add(workflow)
    session.flush()
    print(f"Workflow      id={workflow.id}")

    sanitizer_cfg = models.AgentConfig(name="sanitizer", agent_type="sanitizer")
    intent_cfg = models.AgentConfig(name="intent-analyzer", agent_type="intent_analyzer")
    rag_cfg = models.AgentConfig(name="rag-info-hotel", agent_type="rag_info")
    session.add_all([sanitizer_cfg, intent_cfg, rag_cfg])
    session.flush()
    print(f"AgentConfigs  sanitizer={sanitizer_cfg.id}  intent={intent_cfg.id}  rag={rag_cfg.id}")

    session.add(models.WorkflowAgent(workflow_id=workflow.id, agent_config_id=sanitizer_cfg.id, position=0))
    session.add(models.WorkflowAgent(workflow_id=workflow.id, agent_config_id=intent_cfg.id,    position=1))
    session.add(models.WorkflowAgent(workflow_id=workflow.id, agent_config_id=rag_cfg.id,       position=2))
    session.flush()
    print("WorkflowAgents created")

    bot = models.Bot(
        tenant_id=tenant.id,
        name="Hotel Playa Sirena",
        bot_type="rag_info",
        workflow_id=workflow.id,
    )
    session.add(bot)
    session.flush()
    print(f"Bot           id={bot.id}")

    namespace = f"workflow_{workflow.id}"
    session.add(models.RagSource(namespace=namespace, scope_type="workflow", scope_id=workflow.id))
    session.commit()
    print(f"RagSource     namespace={namespace}")

    return api_token, workflow.id, bot.id, namespace


def main() -> None:
    if not Path(EXAMPLE_MD).exists():
        raise FileNotFoundError(f"example.md not found at {EXAMPLE_MD}")

    with Session(engine) as session:
        clean_db(session)
        api_token, workflow_id, bot_id, namespace = setup(session)

    print(f"\nIngesting {EXAMPLE_MD} → {namespace}")
    init_rag_table(namespace)
    chunks = ingest(EXAMPLE_MD, namespace, source_name="example.md")
    print(f"Ingested {chunks} chunks")

    print(f"""
Setup complete.

  tenant_id   = {TEST_TENANT_ID}
  api_token   = {api_token}
  workflow_id = {workflow_id}
  bot_id      = {bot_id}
  namespace   = {namespace}

Test payload (socket):
  auth: {{ token: "{api_token}" }}
  send_message: {{ content: "¿cuánto cuesta una habitación?", bot_id: {bot_id}, chat_id: "test-001" }}

HTTP header:
  X-Api-Key: {api_token}
""")


if __name__ == "__main__":
    main()
