"""Seed a single-agent workflow that runs BusinessAnalyzerAgent.

Idempotent: skips creation if a workflow named "business-analyzer" exists.
No RAG, no tools — the agent reads a business form from the chat message
(JSON string) and returns a readiness report.

Run:  uv run python seed_business_analyzer.py
"""
from sqlalchemy.orm import Session

from app import models
from app.database import engine

WORKFLOW_NAME = "business-analyzer"


def seed(session: Session) -> tuple[int, int]:
    existing = (
        session.query(models.Workflow)
        .filter(models.Workflow.name == WORKFLOW_NAME)
        .first()
    )
    if existing:
        bot = (
            session.query(models.Bot)
            .filter(models.Bot.workflow_id == existing.id)
            .first()
        )
        print(f"Workflow '{WORKFLOW_NAME}' already exists  id={existing.id}")
        return existing.id, bot.id if bot else 0

    workflow = models.Workflow(
        name=WORKFLOW_NAME,
        description="BusinessAnalyzerAgent — scores chatbot form completeness",
    )
    session.add(workflow)
    session.flush()
    print(f"Workflow      id={workflow.id}")

    analyzer_cfg = models.AgentConfig(
        name="business-analyzer",
        agent_type="business_analyzer",
    )
    session.add(analyzer_cfg)
    session.flush()
    print(f"AgentConfig   business_analyzer={analyzer_cfg.id}")

    session.add(models.WorkflowAgent(
        workflow_id=workflow.id,
        agent_config_id=analyzer_cfg.id,
        position=0,
    ))
    session.flush()
    print("WorkflowAgent created")

    bot = models.Bot(
        name="Business Analyzer",
        bot_type="rag_info",
        workflow_id=workflow.id,
    )
    session.add(bot)
    session.flush()
    print(f"Bot           id={bot.id}")

    session.commit()
    return workflow.id, bot.id


def main() -> None:
    with Session(engine) as session:
        workflow_id, bot_id = seed(session)

    sample_form = {
        "general": {
            "description": "Empresa de tecnología especializada en software a medida.",
            "services": "Desarrollo de software, automatización e integración de sistemas.",
            "mission": "Transformar los procesos de nuestros clientes con tecnología.",
            "vision": "Ser referente de tecnología para pymes en Latinoamérica.",
            "sales_pitch": "Reduce tus tiempos operativos hasta un 60% con nosotros.",
            "faq": [
                {"question": "¿Cuánto tarda?", "answer": "Proyectos estándar de 4 a 12 semanas."},
            ],
            "additional_info": "Oficinas en Buenos Aires. Lunes a viernes de 9 a 18hs.",
            "social_media": {"website": "https://ejemplo.com"},
        },
        "contact": {"name": "Juan Pérez", "phone": "1112345678", "company_name": "TechSoluciones"},
        "links": [],
    }
    # The chat message is itself a JSON string carrying the form.
    message = json.dumps(sample_form, ensure_ascii=False)
    test_payload = json.dumps(
        {"message": message, "bot_id": bot_id, "chat_id": "analyzer-001"},
        ensure_ascii=False,
    )

    print(f"""
Seed complete.

  workflow_id = {workflow_id}
  bot_id      = {bot_id}

The chat message must be a JSON string. Accepted shapes:
  - {{"form_path": "/abs/path/form.json"}}
  - {{"form_data": {{...form...}}}}
  - the raw form: {{"general": {{...}}, "contact": {{...}}, "links": [...]}}

Test WebSocket payload:
  {test_payload}
""")


if __name__ == "__main__":
    main()
