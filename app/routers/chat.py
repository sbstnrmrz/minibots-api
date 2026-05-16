import asyncio
import json

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app import models
from app.agents.intent_analyzer import IntentAnalyzerAgent
from app.agents.rag_info_agent import RAGInfoAgent
from app.database import db_context
from app.services.sheets import fetch_sheet
from app.services.gemini import generate_reply, generate_with_tools
from rag.store import has_rag_table, make_rag_tool, make_rag_dispatcher

router = APIRouter(tags=["chat"])


@router.websocket("/ws/chat")
async def chat_ws(websocket: WebSocket):
    await websocket.accept()
    try:
        while True:
            data = await websocket.receive_json()
            message: str = data.get("message", "")
            bot_id: int | None = data.get("bot_id")

            bot_type: str | None = None
            system_prompt: str | None = None
            history: list[dict] = []
            user_content = message
            use_rag = False

            if bot_id:
                with db_context() as db:
                    bot = db.query(models.Bot).filter(models.Bot.id == bot_id).first()
                    if bot:
                        bot_type = bot.bot_type
                        system_prompt = bot.system_prompt
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

                        use_rag = await asyncio.to_thread(
                            has_rag_table, f"bot_{bot_id}"
                        )

                        db.add(models.ChatMessage(bot_id=bot_id, role="user", content=message))
                        db.commit()

            contents = history + [{"role": "user", "parts": [{"text": user_content}]}]

            if bot_type == "rag_info" and use_rag:
                intent_raw = await asyncio.to_thread(IntentAnalyzerAgent().run, message)
                try:
                    retrieval_query = json.loads(intent_raw).get("intencion") or message
                except (json.JSONDecodeError, AttributeError):
                    retrieval_query = message

                agent = RAGInfoAgent(
                    namespace=f"bot_{bot_id}",
                    system_prompt=system_prompt,
                    session_id=str(bot_id),
                )
                reply = await asyncio.to_thread(agent.run, message, retrieval_query)
            elif use_rag:
                reply = await generate_with_tools(
                    contents=contents,
                    tools=[make_rag_tool(bot_id)],
                    dispatcher=make_rag_dispatcher(bot_id),
                    system_prompt=system_prompt,
                )
            else:
                reply = await generate_reply(contents, system_prompt)

            if bot_id:
                with db_context() as db:
                    db.add(models.ChatMessage(bot_id=bot_id, role="model", content=reply))
                    db.commit()

            await websocket.send_json({"response": reply})
    except WebSocketDisconnect:
        pass
