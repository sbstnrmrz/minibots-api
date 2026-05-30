"""Interactive CLI for testing the SchedulingAgent.

Usage:
    uv run python chat_scheduler.py                      # blank config, DB tools only
    uv run python chat_scheduler.py --bot-id 3           # load business_config from DB bot
    uv run python chat_scheduler.py --config config.json # load business_config from file

Type 'exit' or Ctrl-C to quit. Type 'reset' to clear conversation history.
"""

import argparse
import json
import sys
import uuid

from app.agents.base import AgentContext
from app.agents.scheduling_agent import SchedulingAgent, SCHEDULING_SYSTEM_PROMPT


def _load_config_from_bot(bot_id: int) -> dict | None:
    from app.database import SessionLocal
    from app import models

    db = SessionLocal()
    try:
        bot = db.query(models.Bot).filter(models.Bot.id == bot_id).first()
        if not bot:
            print(f"[error] bot_id={bot_id} not found")
            return None
        if not bot.workflow_id:
            print(f"[warn] bot {bot_id} has no workflow — no business_config to load")
            return None
        wa = (
            db.query(models.WorkflowAgent)
            .join(models.AgentConfig, models.WorkflowAgent.agent_config_id == models.AgentConfig.id)
            .filter(
                models.WorkflowAgent.workflow_id == bot.workflow_id,
                models.AgentConfig.agent_type == "scheduler",
            )
            .first()
        )
        if not wa:
            print(f"[warn] no scheduler AgentConfig found for bot {bot_id}")
            return None
        agent_config = db.query(models.AgentConfig).filter(
            models.AgentConfig.id == wa.agent_config_id
        ).first()
        cfg = (agent_config.config_json or {}).get("business_config")
        if cfg:
            print(f"[info] loaded business_config from bot {bot_id} / agent_config {agent_config.id}")
        else:
            print(f"[info] agent_config {agent_config.id} has no business_config yet")
        return cfg
    finally:
        db.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Chat with the SchedulingAgent")
    parser.add_argument("--bot-id", type=int, help="Load business_config from this bot's scheduler AgentConfig")
    parser.add_argument("--config", type=str, help="Path to a JSON file with business_config")
    parser.add_argument("--chat-id", type=str, default=None, help="Chat ID for memory (auto-generated if omitted)")
    args = parser.parse_args()

    business_config: dict | None = None

    if args.config:
        with open(args.config) as f:
            business_config = json.load(f)
        print(f"[info] loaded business_config from {args.config}")
    elif args.bot_id:
        business_config = _load_config_from_bot(args.bot_id)

    chat_id = args.chat_id or f"cli-{uuid.uuid4().hex[:8]}"

    agent = SchedulingAgent(
        system_prompt=SCHEDULING_SYSTEM_PROMPT,
        business_config=business_config,
    )

    print(f"\n{'='*60}")
    print("SchedulingAgent CLI")
    print(f"chat_id : {chat_id}")
    print(f"config  : {'loaded' if business_config else 'none (default prompt)'}")
    print("Commands: 'exit' to quit, 'reset' to clear memory")
    print(f"{'='*60}\n")

    while True:
        try:
            user_input = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n[bye]")
            break

        if not user_input:
            continue
        if user_input.lower() == "exit":
            print("[bye]")
            break
        if user_input.lower() == "reset":
            from app.agents.memory import MemoryStore
            MemoryStore().clear(chat_id, agent.name)
            print("[memory cleared]")
            continue

        ctx = AgentContext(input=user_input, chat_id=chat_id)
        try:
            result = agent.run(ctx)
            print(f"\nAgent: {result.input}\n")
        except Exception as e:
            print(f"[error] {e}\n", file=sys.stderr)


if __name__ == "__main__":
    main()
