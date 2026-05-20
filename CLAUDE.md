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

# Run the test suite
uv run pytest

# Seed a test workflow and ingest a knowledge base (one-time setup)
uv run python setup_test.py
```

## Architecture

FastAPI backend for configurable AI chatbots ("minibots"). All LLM calls go through the `llm/` provider abstraction (default: DeepSeek `deepseek-v4-flash`). Embeddings still use Gemini (`gemini-embedding-001`). The frontend is a separate React/Vite repo that talks to this backend over socket.io (chat) and plain HTTP (setup, admin, history).

**Request flow for chat:**
1. Client opens a **socket.io** connection to the FastAPI server. The `connect` handler validates an `auth: { token }` payload against `API_TOKEN`; an invalid token raises `ConnectionRefusedError`.
2. Client emits `send_message` with `{content, role, chat_id, bot_id?}`. The handler enforces a per-sid rate-limit bucket (`SocketRateLimiter`) and validates the payload against the `Message` Pydantic model.
3. The message is enqueued in a `MessageCoalescer` keyed by `(sid, chat_id)`. After `CHAT_COALESCE_WINDOW_SECONDS` (default 2s) of quiet, every buffered message in that window is concatenated with blank lines and dispatched as one chat turn. Further messages arriving inside the window reset the timer.
4. The flush callback calls `app/services/chat_handler.handle_chat_turn(message, bot_id, chat_id, tenant_id)`:
   - Loads bot config + history from PostgreSQL inside a `db_context()`.
   - History is scoped to `chat_id` when present; falls back to `bot_id` for legacy messages.
   - Chat rows are inserted via `INSERT … ON CONFLICT DO NOTHING` (no race on first message).
   - For `bot_type == "vendedor"` with `spreadsheet_id`, live inventory CSV is fetched and appended to the user content.
   - Routing (in priority order):
     - `bot.workflow_id` set → `build_pipeline(workflow_id, db)` → `pipeline.run(AgentContext)`
     - `bot_type == "rag_info"` + RAG data → legacy `IntentAnalyzerAgent → RAGInfoAgent`
     - any other bot with a RAG namespace → `generate_with_tools(...)` with `retrieve_documents`
     - no bot → resolves the tenant's default `AgentConfig` and runs an `IntentAnalyzer → RAGInfo` pipeline if a namespace exists, else `generate_reply`
     - else → `generate_reply()` with full history
   - Both user and model messages are persisted to `chat_messages` (with `chat_id` when present).
5. The handler emits `new_message` **only to the sender** via `sio.emit(..., to=sid)`. The user-role echo is not emitted — the frontend appends its own message locally.
6. On disconnect, any pending coalescer buffer and rate-limit bucket for the sid is dropped.

**Auth, limits, observability:**

- Every HTTP router carries `dependencies=[Depends(require_api_key)]`. Tokens come from `X-API-Key` or `Authorization: Bearer …`. Comparison is constant-time. With `ENVIRONMENT=development` and an empty `API_TOKEN` the gate fails open so local work isn't blocked.
- `slowapi` rate-limits HTTP routes at 60/minute by remote address. `SocketRateLimiter` (`app/rate_limit.py`) caps each socket sid to 30 events / 60s.
- `LLMConfig` carries `timeout` (default 60s) and `max_tool_rounds` (default 10). Exceeding either raises `RuntimeError` instead of looping forever.
- `RequestIDMiddleware` (`app/observability.py`) assigns or trusts `X-Request-ID` per request, binds it to a `ContextVar`, and echoes it on the response. With `LOG_JSON=true` (default outside development) every log line is JSON and carries `request_id`.
- `GET /healthz` and `GET /readyz` for liveness / readiness probes.
- FastAPI lifespan replaces import-time `create_all`: schema is built and the psycopg pool warmed on startup, both closed on shutdown.

**Package layout:**
```
app/
├── main.py            # lifespan, middleware, router includes, /healthz, /readyz
├── auth.py            # require_api_key dep + validate_api_token (used by socket connect)
├── config.py          # ALL env vars (HTTP, LLM, Garage, auth, coalescer, upload limits, logging)
├── database.py        # SQLAlchemy engine, get_db (FastAPI dep), db_context (sync ctxmgr)
├── db_pool.py         # shared psycopg ConnectionPool with pgvector codecs preregistered
├── models.py          # ORM: Tenant, TenantFile, AgentGeneralInfo, Bot, Chat, ChatMessage, Workflow, AgentConfig, WorkflowAgent, AgentTool, RagSource
├── observability.py   # JsonFormatter + RequestIDMiddleware + configure_logging
├── rate_limit.py      # slowapi `limiter` + in-memory SocketRateLimiter
├── socket.py          # socket.io AsyncServer + connect/disconnect/send_message;
│                      # all real work delegates to services/chat_handler.handle_chat_turn
├── templates.py       # TEMPLATES dict (id, name, emoji, description, system_prompt, needs_sheet)
├── schemas/
│   ├── bot.py         # BotCreate, BotResponse
│   └── chat.py        # ChatMessageResponse
├── routers/
│   ├── bots.py        # GET/POST /bots, GET /bots/{id}, GET /bots/{id}/messages
│   ├── chats.py       # GET /chats/{chat_id}/messages — history rehydration on reload
│   ├── templates.py   # GET /templates
│   ├── products.py    # GET /products?bot_id=
│   ├── documents.py   # POST /bots/{id}/documents, /workflows/{id}/documents, /agent-configs/{id}/documents
│   └── agents.py      # POST /agents/setup (202, background ingest), GET /agents/me, GET /agents/files/{id}
├── services/
│   ├── chat_handler.py # handle_chat_turn — single dispatcher used by socket.send_message
│   ├── message_queue.py# MessageCoalescer (per-(sid, chat_id) burst debounce)
│   ├── gemini.py      # generate_reply / generate_with_tools — async wrappers over llm.acall_llm
│   ├── sheets.py      # fetch_sheet — Google Sheets CSV via httpx
│   └── storage.py     # boto3 client for Garage S3 (get_client, upload_file, delete_file, get_presigned_url)
├── agents/
│   ├── base.py              # AgentContext + Agent ABC + Pipeline
│   ├── factory.py           # build_pipeline(workflow_id, db) — loads workflow from DB, assembles Pipeline
│   ├── examples.py          # SanitizerAgent, TruncateAgent — reference implementations
│   ├── intent_analyzer.py   # IntentAnalyzerAgent — sets ctx.retrieval_query (Neutral-Spanish intent JSON)
│   ├── rag_info_agent.py    # RAGInfoAgent + RAG_INFO_SYSTEM_PROMPT — grounded customer service
│   ├── generic_info_agent.py# GenericInfoAgent + GENERIC_INFO_SYSTEM_PROMPT — knowledge-based, no RAG
│   ├── business_analyzer_agent.py # FormReader + CompletenessScorer + BusinessAnalyzerAgent
│   └── memory.py            # MemoryStore — Postgres-backed session memory via the shared pool
└── tools/
    ├── __init__.py    # TOOL_REGISTRY + get_tools_for_agent + make_dispatcher_for_agent + dispatch
    ├── row_lookup.py  # lookup_rows + ROW_LOOKUP_TOOL — CSV/Excel row lookup
    ├── calculator.py  # calculate + CALCULATOR_TOOL — safe AST arithmetic with Decimal precision
    └── sheets_lookup.py # fetch_google_sheet + SHEETS_LOOKUP_TOOL — public Sheets CSV fetch
rag/
└── store.py           # init_rag_table, ingest, retrieve, has_rag_table, clear_namespace,
                      # get_namespace, make_rag_tool, make_rag_dispatcher
llm/
├── client.py          # LLMProvider, LLMConfig (timeout + max_tool_rounds), call_llm, acall_llm, embed, DEFAULT_LLM_CONFIG
├── tools.py           # to_openai_tool — OpenAI function-calling schema adapter
└── __init__.py        # public exports
tests/                 # pytest suite (calculator, scorer, pipeline, queue, auth)
```

**Adding a new feature:** create `routers/X.py` + `services/X.py` if needed, gate the router with `dependencies=[Depends(require_api_key)]`, then `app.include_router(X.router)` in `main.py`.

**Bot templates** live in `app/templates.py` as a static dict. A bot's `system_prompt` can be overridden at creation time via `POST /bots` body. Available types: `rag_info`, `vendedor`, `growth_hacker`, `zen_coach`.

**Workflow system** — DB-defined, composable agent pipelines in `app/agents/`:
- `Workflow` → ordered `WorkflowAgent` rows → `AgentConfig` rows (agent_type, system_prompt, config_json)
- `AgentTool` rows assign tool names to each agent
- `build_pipeline(workflow_id, db)` in `factory.py` loads all of the above and returns a ready `Pipeline`
- Supported `agent_type` values: `intent_analyzer`, `rag_info`, `generic_info`, `business_analyzer`, `sanitizer`, `truncate`
- Adding a new agent type: subclass `Agent`, implement `run(ctx: AgentContext) -> AgentContext`, add an entry in `factory.py:_build_agent()`

**AgentContext** — uniform data carrier through the pipeline (`app/agents/base.py`):
- `AgentContext(input: str, chat_id: str | None, retrieval_query: str | None)`
- `Agent.run(ctx) -> AgentContext` — all agents read/write context fields; never modify input in place
- `Pipeline.run(ctx) -> str` — threads context through agents sequentially, returns `ctx.input` of last agent
- Memory (if `memory_store` attached) keyed by `ctx.chat_id`; per-agent history injected before each step

**`IntentAnalyzerAgent`** — NLP intent normalizer (`app/agents/intent_analyzer.py`):
- Input: `ctx.input` (user message, optionally pre-cleaned by `SanitizerAgent`)
- Output: same `ctx` with `retrieval_query` set to the normalized Spanish `"intencion"` field
- `ctx.input` is preserved — downstream agents always see the original user message

**`GenericInfoAgent`** — general-purpose conversational agent (`app/agents/generic_info_agent.py`):
- `GenericInfoAgent(system_prompt=GENERIC_INFO_SYSTEM_PROMPT, session_id=None, tool_names=[])`
- `run(ctx)` — uses `ctx.input` as the user message; no RAG retrieval
- Loads conversation history from `MemoryStore` (keyed by `ctx.chat_id or session_id`), builds prompt, calls `llm.call_llm` with `DEFAULT_LLM_CONFIG`, saves exchange to memory

**`BusinessAnalyzerAgent`** — chatbot-form readiness scorer (`app/agents/business_analyzer_agent.py`):
- `BusinessAnalyzerAgent(llm_config=None)` — defaults to `DEFAULT_ANALYZER_CONFIG` (`DEEPSEEK` / `deepseek-v4-pro`)
- `run(ctx)` accepts an `AgentContext` (Pipeline) or a raw JSON string (direct call)
- Input JSON accepts 2 shapes: `{"form_data": {...}}` or the raw form payload itself (`{"general": {...}, "contact": {...}, "links": [...]}`). The previous `{"form_path": "..."}` shape was removed because it accepted an arbitrary filesystem path from caller-controlled JSON — call `FormReader` directly from trusted server-side code if a file must be loaded
- Flow: resolve form → `CompletenessScorer().score()` → `call_llm` with the scoring result → human-readable readiness report
- On any error returns `{"error": "..."}` as a JSON string
- `CompletenessScorer` is pure Python, weight rubric is `business_identity:18, products_and_services:25, faqs:35, policies_and_detail:12, contact_and_reach:10`

**`RAGInfoAgent`** — grounded customer service agent (`app/agents/rag_info_agent.py`):
- `RAGInfoAgent(namespace, system_prompt=RAG_INFO_SYSTEM_PROMPT, top_k=5, session_id=None, tool_names=[])`
- `run(ctx)` — uses `ctx.retrieval_query or ctx.input` for RAG retrieval; `ctx.input` as user-facing message
- Refuses out-of-scope questions; responds honestly when context has no answer — never hallucinate

**LLM provider abstraction** — `llm/`:
- All LLM calls route through `call_llm` (sync) / `acall_llm` (async). No agent imports a provider SDK directly.
- Every provider (Gemini, DeepSeek) is reached via the OpenAI Python SDK against its OpenAI-compatible endpoint. Add a provider with one `LLMProvider` enum entry + one `_PROVIDER_SETTINGS` entry.
- `LLMConfig(provider, model, max_tokens, temperature, system_prompt, timeout, max_tool_rounds)` — per-call config. `timeout` and `max_tool_rounds` are the safety dials and apply to both the chat and embed paths.
- `DEFAULT_LLM_CONFIG` — built from env `LLM_PROVIDER` / `LLM_MODEL`; falls back to `DEEPSEEK` / `deepseek-v4-flash`.
- `call_llm` and `acall_llm` run the tool-execution loop internally up to `max_tool_rounds`: model emits tool_call → `dispatcher` runs it → result fed back → repeats until plain-text reply.
- `services/gemini.py:generate_reply` / `generate_with_tools` use `acall_llm` directly so the request handler never parks a thread waiting on a provider. Pipeline-internal agents still use sync `call_llm` because `Pipeline.run` is synchronous and runs inside `asyncio.to_thread`.
- `embed(text, model="gemini-embedding-001")` — embeddings; DeepSeek has no embeddings endpoint, so this stays on Gemini.

**Tool registry** — `app/tools/__init__.py`:
- `TOOL_REGISTRY: dict[str, ToolEntry]` — maps registry key → `(declaration, fn)`
- Registry key (e.g. `calculator`, `csv_lookup`, `sheets_lookup`) ≠ OpenAI function name (e.g. `calculate`, `lookup_rows`, `fetch_google_sheet`). `_FN_NAME_TO_KEY` + `_resolve_key` resolve either form.
- `get_tools_for_agent(tool_names)` → list of OpenAI tool declarations for an agent's subset
- `make_dispatcher_for_agent(tool_names)` → scoped dispatcher that only allows the agent's tools

**RAG scoping** — `rag/store.py` + `rag_sources` table:
- All chunks stored in single `rag_chunks(namespace, content, embedding, metadata)` table
- `rag_sources(namespace, scope_type, scope_id)` registry maps a namespace to its owner
- Scope types: `"bot"` → namespace `bot_{id}`, `"workflow"` → `workflow_{id}`, `"agent"` → `agent_{id}`
- `init_rag_table(namespace)` and `ingest(...)` ensure both the table and the namespace b-tree index exist (idempotent, namespace validated as `[a-zA-Z0-9_]+`)
- `retrieve(query, namespace, top_k=5)` — cosine similarity search. The query casts both sides to `halfvec(3072)` so it hits the HNSW expression index created by `migrate.py` (pgvector caps HNSW on the plain `vector` type at 2000 dims)
- All direct-SQL paths in this module go through `app.db_pool.connection` so the pgvector codec is preregistered on every checked-out conn
- `make_rag_tool(namespace)` / `make_rag_dispatcher(namespace)` build a `retrieve_documents` OpenAI-format tool/dispatcher scoped to a namespace
- Upload endpoints: `POST /bots/{id}/documents`, `/workflows/{id}/documents`, `/agent-configs/{id}/documents`

**Namespace resolution for `rag_info` agents** (in `factory.py`):
1. Explicit `namespace` key in `agent_config.config_json`
2. `rag_sources` entry with `scope_type="agent"`, `scope_id=agent_config.id`
3. `rag_sources` entry with `scope_type="workflow"`, `scope_id=workflow_id`
4. Raises `ValueError` if none found

**Frontend contract** — frontend lives in a sibling repo (`minibots-front/`). Key endpoints:
- socket.io: connect with `auth: { token: VITE_API_TOKEN }`; emit `send_message` with `{content, role, chat_id}`; receive `new_message` ({content, role}) and `error` ({detail})
- HTTP, all gated by `X-API-Key`:
  - `POST /agents/setup` — creates/updates the tenant's agent config, returns 202 with a per-file `{file_id, status}` receipt. Heavy ingest (S3 + MarkItDown + embedding) runs as a BackgroundTask
  - `GET /agents/me` — returns the tenant's current setup (tenant, agent_config, general, links, files with ingestion status)
  - `GET /agents/files/{file_id}` — per-file status + fresh presigned URL once ingested
  - `GET /chats/{chat_id}/messages` — chronological transcript rehydration for a reloaded tab

## Environment

Copy `.env.example` to `.env` and fill in:

| Variable | Purpose |
| --- | --- |
| `GEMINI_API_KEY` | Gemini API key (embeddings; chat if `LLM_PROVIDER=GEMINI`) |
| `DEEPSEEK_API_KEY` | DeepSeek API key (default chat provider) |
| `LLM_PROVIDER` / `LLM_MODEL` | Default chat provider + model; default `DEEPSEEK` / `deepseek-v4-flash` |
| `DATABASE_URL` | PostgreSQL DSN (local Docker: `postgresql://user:1234@localhost:5432/minibots`) |
| `ENVIRONMENT` | `development` opens CORS to `*` and lets auth fall open when no `API_TOKEN` set |
| `ALLOWED_ORIGINS` | Comma-separated CORS origin list outside development |
| `API_TOKEN` | Shared API token. Required in production; missing in development = open |
| `DEFAULT_TENANT_ID` | Tenant id used by the no-bot socket fallback and `agents/setup` |
| `CHAT_COALESCE_WINDOW_SECONDS` | Burst-coalesce window (default 2.0s) |
| `MAX_UPLOAD_FILE_BYTES` / `MAX_UPLOAD_FILE_COUNT` / `ALLOWED_UPLOAD_SUFFIXES` | `/agents/setup` upload limits |
| `LOG_JSON` | Force JSON log output (defaults true outside development) |
| `GARAGE_*` | S3-compatible storage (endpoint, region, keys, bucket) |

App env vars are read once in `app/config.py`; LLM env vars are read in `llm/client.py`. Switching the chat provider globally requires only changing `LLM_PROVIDER` + `LLM_MODEL`. Database schema is built on startup via SQLAlchemy `create_all` inside the FastAPI lifespan; additional schema changes go in `migrate.py` and are run manually (no Alembic).
