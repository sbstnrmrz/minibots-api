"""Shared chat dispatch.

Single entry point used by the socket.io `send_message` handler. Returns
the model's reply text. Persists user + model messages and resolves the
right pipeline / fallback path based on bot configuration.
"""

import asyncio
import logging

from sqlalchemy.dialects.postgresql import insert as pg_insert

from app import models
from app.agents.base import AgentContext, Pipeline
from app.agents.factory import build_pipeline
from app.agents.intent_analyzer import IntentAnalyzerAgent
from app.agents.rag_info_agent import RAGInfoAgent
from app.database import db_context
from app.services.gemini import generate_reply, generate_with_tools
from app.services.sheets import fetch_sheet
from rag.store import (
    get_namespace,
    has_rag_table,
    make_rag_dispatcher,
    make_rag_tool,
)

logger = logging.getLogger("chat")


class _ConfigSnapshot:
    """Minimal duck type for `_augment_tools_from_links` / `_links_context`.

    The factory helpers only read `.links`, so we expose just that field
    instead of carrying a SQLAlchemy instance out of its session.
    """

    def __init__(self, links: list | None) -> None:
        self.links = links


async def handle_chat_turn(
    message: str,
    bot_id: int | None,
    chat_id: str | None,
    tenant_id: str,
) -> str:
    """Run one chat turn end-to-end and return the model reply.

    Reads bot config + history, picks the dispatch path (workflow,
    legacy RAG, generic RAG, tenant-default RAG, plain reply), persists
    both messages, and returns the reply text.
    """
    bot_type: str | None = None
    system_prompt: str | None = None
    history: list[dict] = []
    user_content = message
    rag_namespace: str | None = None
    pipeline: Pipeline | None = None

    if bot_id:
        with db_context() as db:
            bot = db.query(models.Bot).filter(models.Bot.id == bot_id).first()
            if bot:
                bot_type = bot.bot_type
                system_prompt = bot.system_prompt
                spreadsheet_id = bot.spreadsheet_id
                workflow_id = bot.workflow_id

                if chat_id:
                    db.execute(
                        pg_insert(models.Chat.__table__)
                        .values(id=chat_id, bot_id=bot_id)
                        .on_conflict_do_nothing(index_elements=["id"])
                    )
                    db.commit()

                    past = (
                        db.query(models.ChatMessage)
                        .filter(models.ChatMessage.chat_id == chat_id)
                        .order_by(models.ChatMessage.created_at)
                        .all()
                    )
                else:
                    past = (
                        db.query(models.ChatMessage)
                        .filter(models.ChatMessage.bot_id == bot_id)
                        .order_by(models.ChatMessage.created_at)
                        .all()
                    )

                history = [
                    {"role": m.role, "parts": [{"text": m.content}]}
                    for m in past
                ]

                if bot_type == "vendedor" and spreadsheet_id:
                    sheet_data = await fetch_sheet(spreadsheet_id)
                    if sheet_data:
                        user_content = f"{message}\n\nINVENTARIO ACTUAL:\n{sheet_data}"

                rag_namespace = await asyncio.to_thread(get_namespace, "bot", bot_id)

                db.add(models.ChatMessage(
                    bot_id=bot_id,
                    chat_id=chat_id,
                    role="user",
                    content=message,
                ))
                db.commit()

                if workflow_id:
                    pipeline = build_pipeline(workflow_id, db)

    contents = history + [{"role": "user", "parts": [{"text": user_content}]}]

    if pipeline is not None:
        ctx = AgentContext(input=user_content, chat_id=chat_id)
        reply = await asyncio.to_thread(pipeline.run, ctx)
    elif bot_type == "rag_info" and rag_namespace:
        legacy_pipeline = Pipeline([
            IntentAnalyzerAgent(),
            RAGInfoAgent(
                namespace=rag_namespace,
                system_prompt=system_prompt,
                session_id=chat_id or str(bot_id),
            ),
        ])
        ctx = AgentContext(input=user_content, chat_id=chat_id)
        reply = await asyncio.to_thread(legacy_pipeline.run, ctx)
    elif rag_namespace:
        reply = await generate_with_tools(
            contents=contents,
            tools=[make_rag_tool(rag_namespace)],
            dispatcher=make_rag_dispatcher(rag_namespace),
            system_prompt=system_prompt,
        )
    elif not bot_id:
        reply = await _handle_tenant_default(
            tenant_id=tenant_id,
            message=message,
            user_content=user_content,
            chat_id=chat_id,
            contents=contents,
            system_prompt=system_prompt,
        )
    else:
        reply = await generate_reply(contents, system_prompt)

    if bot_id:
        with db_context() as db:
            db.add(models.ChatMessage(
                bot_id=bot_id,
                chat_id=chat_id,
                role="model",
                content=reply,
            ))
            db.commit()

    return reply


async def _handle_tenant_default(
    tenant_id: str,
    message: str,
    user_content: str,
    chat_id: str | None,
    contents: list[dict],
    system_prompt: str | None,
) -> str:
    """No-bot fallback: route to the tenant's default AgentConfig if any."""
    agent_config_id: int | None = None
    agent_config_system_prompt: str | None = None
    agent_config_links: list | None = None
    with db_context() as db:
        tenant = (
            db.query(models.Tenant)
            .filter(models.Tenant.id == tenant_id)
            .first()
        )
        if tenant and tenant.agent_config_id:
            agent_config = (
                db.query(models.AgentConfig)
                .filter(models.AgentConfig.id == tenant.agent_config_id)
                .first()
            )
            if agent_config:
                agent_config_id = agent_config.id
                agent_config_system_prompt = agent_config.system_prompt
                agent_config_links = agent_config.links

    namespace = f"agent_{agent_config_id}" if agent_config_id is not None else None
    if not (namespace and has_rag_table(namespace)):
        return await generate_reply(contents, system_prompt)

    from app.agents.factory import _augment_tools_from_links, _links_context
    from app.agents.rag_info_agent import RAG_INFO_SYSTEM_PROMPT

    snapshot = _ConfigSnapshot(agent_config_links)
    tool_names = _augment_tools_from_links(snapshot, [])
    links_ctx = _links_context(snapshot)
    base_prompt = agent_config_system_prompt or RAG_INFO_SYSTEM_PROMPT
    rag_pipeline = Pipeline([
        IntentAnalyzerAgent(),
        RAGInfoAgent(
            namespace=namespace,
            system_prompt=base_prompt + links_ctx,
            session_id=chat_id,
            tool_names=tool_names,
        ),
    ])
    ctx = AgentContext(input=user_content, chat_id=chat_id)
    return await asyncio.to_thread(rag_pipeline.run, ctx)
