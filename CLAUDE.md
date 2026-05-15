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
├── config.py        # all env vars (GEMINI_API_KEY, DATABASE_URL, CORS_ORIGINS)
├── database.py      # SQLAlchemy engine, get_db (FastAPI dep), db_context (context manager for WS)
├── models.py        # ORM: Bot, ChatMessage — PostgreSQL ARRAY for documents_urls
├── templates.py     # TEMPLATES dict (id, name, emoji, description, system_prompt, needs_sheet)
├── schemas/
│   ├── bot.py       # BotCreate, BotResponse
│   └── chat.py      # ChatMessageResponse
├── routers/
│   ├── bots.py      # CRUD: GET/POST /bots, GET /bots/{id}, GET /bots/{id}/messages
│   ├── chat.py      # WebSocket /ws/chat
│   ├── templates.py # GET /templates
│   └── documents.py # POST /bots/{bot_id}/documents — file upload → RAG ingestion
├── services/
│   ├── gemini.py    # generate_reply(), generate_with_tools() — Gemini client wrappers
│   └── sheets.py    # fetch_sheet() — fetches Google Sheets CSV via httpx
├── agents/
│   ├── base.py             # Agent ABC + Pipeline — core pipeline logic
│   ├── examples.py         # SanitizerAgent, TruncateAgent — reference implementations
│   ├── intent_analyzer.py  # TextCleanerStep (fn) + IntentAnalyzerAgent — NLP intent → JSON
│   ├── rag_info_agent.py   # RAGInfoAgent + RAG_INFO_SYSTEM_PROMPT — grounded customer service agent
│   └── memory.py           # MemoryStore — Postgres-backed session memory (psycopg2 direct)
└── tools/
    ├── __init__.py    # ALL_TOOLS list + unified dispatch() covering all tools
    ├── row_lookup.py  # lookup_rows (fn) + ROW_LOOKUP_TOOL — CSV/Excel row lookup
    └── calculator.py  # calculate (fn) + CALCULATOR_TOOL — safe AST arithmetic with Decimal precision
rag/
└── store.py           # init_rag_table, ingest, retrieve, has_rag_table, make_rag_tool, make_rag_dispatcher
```

**Adding a new feature:** create `routers/X.py` + `services/X.py` if needed, then `app.include_router(X.router)` in `main.py`.

**Bot templates** live in `app/templates.py` as a static dict. A bot's `system_prompt` can be overridden at creation time via `POST /bots` body.

**RAGInfoAgent** — grounded customer service agent in `app/agents/rag_info_agent.py`:
- `RAGInfoAgent(namespace, system_prompt=RAG_INFO_SYSTEM_PROMPT, top_k=5, session_id=None)`
- On each `run(input)`: retrieves top-k chunks from `rag_{namespace}`, loads session memory, builds prompt (`<retrieved_context>` + `<conversation_history>` + user input), calls Gemini, saves exchange to memory
- `RAG_INFO_SYSTEM_PROMPT` — 4-step logic: ground in context → scope check → calculator delegation → response. Override at instantiation for custom business personas
- Refuses out-of-scope questions; responds honestly when context has no answer — never hallucinate
- Plugs into `Pipeline` with no changes; manages its own memory when `session_id` is provided

**Agent pipeline** — composable, stateless agent chain in `app/agents/`:
- `Agent` ABC: implement `run(self, input: str) -> str` only
- `Pipeline([AgentA(), AgentB()]).run("input")` — chains agents sequentially
- Optional Postgres memory: `Pipeline(agents, memory_store=MemoryStore()).run("input", session_id="abc")`
- `MemoryStore` creates `agent_memory` table automatically; stores per-session, per-agent input/output
- Memory injected as formatted context string prepended to agent input — agents are never modified directly
- Adding a new agent: subclass `Agent`, implement `run()`, pass to `Pipeline` — no other changes needed

**Gemini tools** — function calling integration in `app/tools/` + `app/services/gemini.py`:
- `lookup_rows(file_path, column, value)` — standalone CSV/Excel row lookup (case-insensitive); raises `ValueError` on bad column
- `calculate(expression)` — safe arithmetic via AST tree-walking + `decimal.Decimal`; supports `+`, `-`, `*`, `/`, parentheses; raises `ValueError` on division by zero or non-arithmetic input
- `ROW_LOOKUP_TOOL`, `CALCULATOR_TOOL` — `types.Tool` declarations; pass to `generate_with_tools()` so Gemini calls them autonomously
- `ALL_TOOLS` — list of all tool declarations; use as `tools=ALL_TOOLS` to expose everything at once
- `dispatch(name, args)` — unified entry point; executes any registered tool by name
- `generate_with_tools(contents, tools, dispatcher, system_prompt)` — async; runs tool-execution loop until Gemini returns plain text
- Adding a new tool: implement the Python function in `app/tools/`, add a `FunctionDeclaration` + entry in `dispatch()` and `ALL_TOOLS`, pass the `Tool` to `generate_with_tools()`

**Document upload** — `POST /bots/{bot_id}/documents` in `app/routers/documents.py`:
- Accepts any file via multipart upload; 404 if bot doesn't exist
- Saves to temp file, calls `init_rag_table` + `ingest`, deletes temp on success or error
- Namespace is `bot_{bot_id}` → table `rag_bot_{bot_id}` — one RAG per chatbot
- Returns `{"bot_id": N, "filename": "...", "chunks_ingested": N}`

**RAG store** — namespace-isolated vector storage in `rag/store.py`:
- `init_rag_table(namespace)` — creates `rag_{namespace}` table with `vector(3072)` column; safe to call on every startup (`CREATE TABLE IF NOT EXISTS`)
- `ingest(file_path, namespace, chunk_size=500, overlap=50, source_name=None)` — converts any file to Markdown via `markitdown` (supports .pdf, .docx, .pptx, .xlsx, .html, .txt, .md), chunks text with overlap, embeds via `gemini-embedding-001`, stores in `rag_{namespace}`; returns chunk count. Pass `source_name` to preserve original filename in metadata
- `retrieve(query, namespace, top_k=5)` — embeds query, runs cosine similarity search via pgvector `<=>` operator, returns `list[dict]` with `content`, `metadata`, `similarity_score`
- `has_rag_table(namespace)` — returns bool; used by chat handler to decide whether to activate RAG tool
- `make_rag_tool(bot_id)` + `make_rag_dispatcher(bot_id)` — build a bot-specific `retrieve_documents` Gemini tool and dispatcher (namespace baked in); used by chat WebSocket to let Gemini call retrieval autonomously
- Namespace validated as `[a-zA-Z0-9_]+` to prevent SQL injection via table name
- Same `psycopg2.connect(DATABASE_URL)` pattern as `agents/memory.py` — no new connection setup
- Requires pgvector extension enabled in Postgres (handled by `docker/init.sql` on first DB init)

## Environment

Copy `.env.example` to `.env` and fill in:
- `GEMINI_API_KEY` — Google Gemini API key
- `DATABASE_URL` — PostgreSQL connection string (default for local Docker: `postgresql://user:1234@localhost:5432/minibots`)

All env vars are read once in `app/config.py`. Database schema is auto-created on startup. Additional schema changes go in `migrate.py` and are run manually (no Alembic).

CORS is restricted to `http://localhost:5173`; change `CORS_ORIGINS` in `app/config.py`.
