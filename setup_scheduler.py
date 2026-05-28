"""
One-time setup: create a Scheduler workflow and a test bot wired to it.
Safe to re-run — updates tenant assignment if run again with a different tenant.
Run migrate.py first to ensure the reservations table exists.

Usage:
    uv run python setup_scheduler.py                       # uses DEFAULT_TENANT_ID env var
    uv run python setup_scheduler.py --tenant <id-or-slug>
    uv run python setup_scheduler.py --list-tenants
"""

import argparse
import os
import uuid

from dotenv import load_dotenv
from sqlalchemy.orm import Session

from app.database import engine
from app import models

load_dotenv()


def _resolve_tenant(session: Session, identifier: str | None) -> models.Tenant:
    """Resolve a tenant by UUID, slug, or DEFAULT_TENANT_ID env var (matches auth stub)."""
    if not identifier:
        identifier = os.getenv("DEFAULT_TENANT_ID", "")
    if not identifier:
        tenants = session.query(models.Tenant).all()
        if len(tenants) == 1:
            return tenants[0]
        names = "\n".join(f"  {t.id}  {t.slug}  {t.name!r}" for t in tenants)
        raise SystemExit(
            f"Multiple tenants found — pass --tenant <id-or-slug>:\n{names}"
        )

    # Try UUID first, then slug
    try:
        tid = uuid.UUID(identifier)
        tenant = session.query(models.Tenant).filter(models.Tenant.id == tid).first()
    except ValueError:
        tenant = session.query(models.Tenant).filter(models.Tenant.slug == identifier).first()

    if not tenant:
        raise SystemExit(f"Tenant not found: {identifier!r}")
    return tenant


def setup(session: Session, tenant: models.Tenant) -> tuple[int, int]:
    existing = session.query(models.Workflow).filter_by(name="scheduler-test").first()

    if existing:
        bot = session.query(models.Bot).filter_by(workflow_id=existing.id).first()
        # Patch tenant_id if missing or wrong
        if bot and bot.tenant_id != tenant.id:
            bot.tenant_id = tenant.id
            session.commit()
            print(f"Updated bot {bot.id} tenant_id → {tenant.id}")
        if existing.tenant_id != tenant.id:
            existing.tenant_id = tenant.id
            session.commit()
        print(f"Already exists — workflow_id={existing.id}  bot_id={bot.id if bot else '?'}")
        return existing.id, bot.id if bot else -1

    # Workflow
    workflow = models.Workflow(
        name="scheduler-test",
        description="SchedulingAgent pipeline for manual testing",
        tenant_id=tenant.id,
    )
    session.add(workflow)
    session.flush()
    print(f"Workflow      id={workflow.id}")

    # AgentConfig — scheduler type, default system prompt
    scheduler_cfg = models.AgentConfig(
        name="scheduler-agent",
        agent_type="scheduler",
        system_prompt=None,  # uses SCHEDULING_SYSTEM_PROMPT default
        config_json={},
        config_scope="workflow_step",
    )
    session.add(scheduler_cfg)
    session.flush()
    print(f"AgentConfig   id={scheduler_cfg.id}  type=scheduler")

    # Wire agent into workflow at position 0
    session.add(models.WorkflowAgent(
        workflow_id=workflow.id,
        agent_config_id=scheduler_cfg.id,
        position=0,
    ))
    session.flush()
    print("WorkflowAgent created")

    # Bot wired to the workflow + tenant
    bot = models.Bot(
        name="Scheduling Test Bot",
        bot_type="rag_info",  # bot_type not used — workflow takes over routing
        workflow_id=workflow.id,
        tenant_id=tenant.id,
    )
    session.add(bot)
    session.flush()
    print(f"Bot           id={bot.id}  tenant_id={tenant.id}")

    session.commit()
    return workflow.id, bot.id


def main() -> None:
    parser = argparse.ArgumentParser(description="Create/update the scheduler test bot")
    parser.add_argument("--tenant", metavar="ID_OR_SLUG", help="Tenant UUID or slug")
    parser.add_argument("--list-tenants", action="store_true", help="Print available tenants and exit")
    args = parser.parse_args()

    with Session(engine) as session:
        if args.list_tenants:
            tenants = session.query(models.Tenant).all()
            print("Available tenants:")
            for t in tenants:
                print(f"  {t.id}  slug={t.slug!r}  name={t.name!r}  token={t.api_token!r}")
            return

        tenant = _resolve_tenant(session, args.tenant)
        tenant_name = tenant.name
        tenant_id_str = str(tenant.id)
        print(f"Tenant: {tenant_name!r}  ({tenant_id_str})")
        workflow_id, bot_id = setup(session, tenant)

    print(f"""
Setup complete.

  workflow_id = {workflow_id}
  bot_id      = {bot_id}
  tenant      = {tenant_name!r}

Run the CLI:
  uv run python -m cli.main

Or test via socket.io:
  emit("send_message", {{
    "content": "Hola, quiero reservar una consulta",
    "role": "user",
    "chat_id": "sched-test-001",
    "bot_id": {bot_id}
  }})
""")


if __name__ == "__main__":
    main()
