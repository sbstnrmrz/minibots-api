import asyncio
import logging

import socketio
from pydantic import BaseModel, ValidationError

from app import models
from app.agents.base import AgentContext, Pipeline
from app.agents.factory import build_pipeline
from app.agents.intent_analyzer import IntentAnalyzerAgent
from app.agents.rag_info_agent import RAGInfoAgent
from app.config import ALLOWED_ORIGINS
from app.database import db_context
from app.services.gemini import generate_reply, generate_with_tools
from app.services.sheets import fetch_sheet
from rag.store import get_namespace, has_rag_table, make_rag_tool, make_rag_dispatcher

DEFAULT_TENANT_ID = "fcbb503a-6e49-4e4c-ac58-fc232064513e"  # Crazy Imagine

logger = logging.getLogger("uvicorn")

sio = socketio.AsyncServer(async_mode="asgi", cors_allowed_origins=[])
socket_app = socketio.ASGIApp(sio)


class Message(BaseModel):
    content: str
    role: str = "user"
    bot_id: int | None = None
    chat_id: str | None = None
    tenant_id: str | None = None


@sio.event
async def connect(sid, environ, auth):
    logger.info(f"Socket client {sid} connected")


@sio.event
async def disconnect(sid, reason):
    logger.info(f"Socket client {sid} disconnected")


@sio.event
async def send_message(sid, data):
    try:
        payload = Message(**data)
    except ValidationError as e:
        await sio.emit("error", {"detail": str(e)}, to=sid)
        return

    message = payload.content
    bot_id = payload.bot_id
    chat_id = payload.chat_id
    tenant_id = payload.tenant_id or DEFAULT_TENANT_ID

    await sio.emit("new_message", {"content": message, "role": "user"})

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

                if chat_id:
                    existing_chat = db.query(models.Chat).filter(
                        models.Chat.id == chat_id
                    ).first()
                    if not existing_chat:
                        db.add(models.Chat(id=chat_id, bot_id=bot_id))
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

                if bot_type == "vendedor" and bot.spreadsheet_id:
                    sheet_data = await fetch_sheet(bot.spreadsheet_id)
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

                if bot.workflow_id:
                    pipeline = build_pipeline(bot.workflow_id, db)

    contents = history + [{"role": "user", "parts": [{"text": user_content}]}]

    if pipeline is not None:
        ctx = AgentContext(input=message, chat_id=chat_id)
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
        ctx = AgentContext(input=message, chat_id=chat_id)
        reply = await asyncio.to_thread(legacy_pipeline.run, ctx)
    elif rag_namespace:
        reply = await generate_with_tools(
            contents=contents,
            tools=[make_rag_tool(rag_namespace)],
            dispatcher=make_rag_dispatcher(rag_namespace),
            system_prompt=system_prompt,
        )
    elif not bot_id:
        with db_context() as db:
            tenant = db.query(models.Tenant).filter(models.Tenant.id == tenant_id).first()
            agent_config = (
                db.query(models.AgentConfig).filter(
                    models.AgentConfig.id == tenant.agent_config_id
                ).first()
                if tenant and tenant.agent_config_id else None
            )
        namespace = f"agent_{agent_config.id}" if agent_config else None
        if namespace and has_rag_table(namespace):
            rag_pipeline = Pipeline([
                IntentAnalyzerAgent(),
                RAGInfoAgent(
                    namespace=namespace,
                    system_prompt=agent_config.system_prompt,
                    session_id=chat_id,
                ),
            ])
            ctx = AgentContext(input=message, chat_id=chat_id)
            reply = await asyncio.to_thread(rag_pipeline.run, ctx)
        else:
            reply = await generate_reply(contents, system_prompt)
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

    await sio.emit("new_message", {"content": reply, "role": "agent"})
