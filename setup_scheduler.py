"""
One-time setup: create a Scheduler workflow and a test bot wired to it.
Safe to re-run — skips creation if a workflow named 'scheduler-test' already exists.
Run migrate.py first to ensure the reservations table exists.
"""

from sqlalchemy.orm import Session

from app.database import engine
from app import models


def setup(session: Session) -> tuple[int, int]:
    existing = session.query(models.Workflow).filter_by(name="scheduler-test").first()
    if existing:
        bot = session.query(models.Bot).filter_by(workflow_id=existing.id).first()
        print(f"Already exists — workflow_id={existing.id}  bot_id={bot.id if bot else '?'}")
        return existing.id, bot.id if bot else -1

    # Workflow
    workflow = models.Workflow(
        name="scheduler-test",
        description="SchedulingAgent pipeline for manual testing",
    )
    session.add(workflow)
    session.flush()
    print(f"Workflow      id={workflow.id}")

    # Single agent config — scheduler type, default system prompt
    scheduler_cfg = models.AgentConfig(
        name="scheduler-agent",
        agent_type="scheduler",
        system_prompt=None,   # uses SCHEDULING_SYSTEM_PROMPT default
        config_json={},
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

    # Bot wired to the workflow
    bot = models.Bot(
        name="Scheduling Test Bot",
        bot_type="rag_info",   # bot_type not used — workflow takes over routing
        workflow_id=workflow.id,
    )
    session.add(bot)
    session.flush()
    print(f"Bot           id={bot.id}")

    session.commit()
    return workflow.id, bot.id


def main() -> None:
    with Session(engine) as session:
        workflow_id, bot_id = setup(session)

    print(f"""
Setup complete.

  workflow_id = {workflow_id}
  bot_id      = {bot_id}

Test via socket.io (example payload):
  emit("send_message", {{
    "content": "Hola, quiero reservar una consulta",
    "role": "user",
    "chat_id": "sched-test-001",
    "bot_id": {bot_id}
  }})

Or curl the health check first to confirm the server is up:
  curl http://localhost:8000/healthz
""")


if __name__ == "__main__":
    main()
