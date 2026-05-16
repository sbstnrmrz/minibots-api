# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Start dev server (hot reload)
uv run fastapi dev app/main.py

# Run ad-hoc migrations
uv run python migrate.py

# Start the local PostgreSQL database
docker compose up -d

# Install/sync dependencies
uv sync
```

## Architecture

FastAPI backend for configurable AI chatbots ("minibots") powered by Google Gemini (`gemini-2.5-flash`).

**Request flow for chat:**
1. Client connects via WebSocket at `/ws/chat`, sends JSON `{message, bot_id}`.
2. Handler loads bot config and full message history from PostgreSQL via `db_context()`.
3. For `bot_type == "vendedor"`, live inventory is fetched from a public Google Sheets CSV (`services/sheets.py`) and appended to the user message.
4. Gemini is called in a thread (`asyncio.to_thread`) with full history as multi-turn `contents` (`services/gemini.py`).
5. Both user and model messages are persisted to `chat_messages` after each turn.

**Package layout:**
```
app/
├── main.py          # app init, middleware, router includes only
├── config.py        # all env vars (GEMINI_API_KEY, DATABASE_URL, ALLOWED_ORIGINS)
├── database.py      # SQLAlchemy engine, get_db (FastAPI dep), db_context (context manager for WS)
├── models.py        # ORM: Bot, ChatMessage — PostgreSQL ARRAY for documents_urls
├── templates.py     # TEMPLATES dict (id, name, emoji, description, system_prompt, needs_sheet)
├── schemas/
│   ├── bot.py       # BotCreate, BotResponse
│   └── chat.py      # ChatMessageResponse
├── routers/
│   ├── bots.py      # CRUD: GET/POST /bots, GET /bots/{id}, GET /bots/{id}/messages
│   ├── chat.py      # WebSocket /ws/chat
│   └── templates.py # GET /templates
└── services/
    ├── gemini.py    # generate_reply() — wraps Gemini client
    └── sheets.py    # fetch_sheet() — fetches Google Sheets CSV via httpx
```

**Adding a new feature:** create `routers/X.py` + `services/X.py` if needed, then `app.include_router(X.router)` in `main.py`.

**Bot templates** live in `app/templates.py` as a static dict. A bot's `system_prompt` can be overridden at creation time via `POST /bots` body.

## Environment

Copy `.env.example` to `.env` and fill in:
- `GEMINI_API_KEY` — Google Gemini API key
- `DATABASE_URL` — PostgreSQL connection string (default for local Docker: `postgresql://user:1234@localhost:5432/minibots`)

All env vars are read once in `app/config.py`. Database schema is auto-created on startup. Additional schema changes go in `migrate.py` and are run manually (no Alembic).

CORS is restricted to `http://localhost:5173`; change `ALLOWED_ORIGINS` in `app/config.py`.
