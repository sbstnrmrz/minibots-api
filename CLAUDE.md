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

# Seed a test workflow and ingest a knowledge base (one-time setup)
uv run python setup_test.py
```

## Architecture

FastAPI backend for configurable AI chatbots ("minibots"). All LLM calls go through the `llm/` provider abstraction (default: DeepSeek `deepseek-v4-flash`). Embeddings still use Gemini (`gemini-embedding-001`).

**Request flow for chat:**
1. Client connects via WebSocket at `/ws/chat`, sends JSON `{message, bot_id, chat_id}`.
   - `chat_id` is a client-supplied UUID identifying the conversation; optional for backward compat.
2. Handler loads bot config and message history from PostgreSQL via `db_context()`.
   - History is scoped to `chat_id` when present; falls back to `bot_id` for legacy messages.
   - A `Chat` row is upserted on first use of a `chat_id`.
3. For `bot_type == "vendedor"`, live inventory is fetched from Google Sheets CSV and appended to the message.
4. Routing (in priority order):
   - `bot.workflow_id` set → `build_pipeline(workflow_id, db)` → `pipeline.run(AgentContext)`
   - no workflow_id, `bot_type == "rag_info"` + RAG data exists → legacy hardcoded `IntentAnalyzerAgent → RAGInfoAgent` pipeline
   - no workflow_id, RAG data exists → `generate_with_tools()` with `retrieve_documents` tool
   - no RAG → `generate_reply()` with full history
5. Both user and model messages are persisted to `chat_messages` (with `chat_id` when present).

**Package layout:**
```
app/
├── main.py          # app init, middleware, router includes only
├── config.py        # all env vars (GEMINI_API_KEY, DATABASE_URL, ALLOWED_ORIGINS)
│                     # LLM provider env vars (DEEPSEEK_API_KEY, LLM_PROVIDER, LLM_MODEL) read in llm/client.py
├── database.py      # SQLAlchemy engine, get_db (FastAPI dep), db_context (context manager for WS)
├── models.py        # ORM: Bot, ChatMessage, Chat, Workflow, AgentConfig, WorkflowAgent, AgentTool, RagSource
├── templates.py     # TEMPLATES dict (id, name, emoji, description, system_prompt, needs_sheet)
├── schemas/
│   ├── bot.py       # BotCreate, BotResponse
│   └── chat.py      # ChatMessageResponse
├── routers/
│   ├── bots.py      # CRUD: GET/POST /bots, GET /bots/{id}, GET /bots/{id}/messages
│   ├── chat.py      # WebSocket /ws/chat — workflow routing + legacy fallback
│   ├── templates.py # GET /templates
│   └── documents.py # POST /bots/{id}/documents, /workflows/{id}/documents, /agent-configs/{id}/documents
├── services/
│   ├── gemini.py    # generate_reply(), generate_with_tools() — async wrappers over llm.call_llm;
│   │                # convert Gemini-format `contents` → OpenAI `messages` (kept for chat.py/socket.py compat)
│   └── sheets.py    # fetch_sheet() — fetches Google Sheets CSV via httpx
├── agents/
│   ├── base.py              # AgentContext dataclass + Agent ABC + Pipeline
│   ├── factory.py           # build_pipeline(workflow_id, db) — loads workflow from DB, assembles Pipeline
│   ├── examples.py          # SanitizerAgent, TruncateAgent — reference implementations
│   ├── intent_analyzer.py   # TextCleanerStep (fn) + IntentAnalyzerAgent — sets ctx.retrieval_query
│   ├── rag_info_agent.py    # RAGInfoAgent + RAG_INFO_SYSTEM_PROMPT — grounded customer service agent
│   ├── generic_info_agent.py# GenericInfoAgent + GENERIC_INFO_SYSTEM_PROMPT — general-purpose Gemini agent with memory, no RAG
│   └── memory.py            # MemoryStore — Postgres-backed session memory (psycopg2 direct)
└── tools/
    ├── __init__.py    # TOOL_REGISTRY dict + ALL_TOOLS + dispatch() + get_tools_for_agent() + make_dispatcher_for_agent()
    ├── row_lookup.py  # lookup_rows (fn) + ROW_LOOKUP_TOOL — CSV/Excel row lookup
    └── calculator.py  # calculate (fn) + CALCULATOR_TOOL — safe AST arithmetic with Decimal precision
rag/
└── store.py           # init_rag_table, ingest, retrieve, has_rag_table, get_namespace, make_rag_tool, make_rag_dispatcher
llm/
├── client.py          # LLMProvider, LLMConfig, call_llm() (single choke point), embed(), DEFAULT_LLM_CONFIG
├── tools.py           # to_openai_tool() — adapter to OpenAI function-calling schema
└── __init__.py        # public exports
```

**Adding a new feature:** create `routers/X.py` + `services/X.py` if needed, then `app.include_router(X.router)` in `main.py`.

**Bot templates** live in `app/templates.py` as a static dict. A bot's `system_prompt` can be overridden at creation time via `POST /bots` body. Available types: `rag_info`, `vendedor`, `growth_hacker`, `zen_coach`.

**Workflow system** — DB-defined, composable agent pipelines in `app/agents/`:
- `Workflow` → ordered `WorkflowAgent` rows → `AgentConfig` rows (agent_type, system_prompt, config_json)
- `AgentTool` rows assign tool names to each agent
- `build_pipeline(workflow_id, db)` in `factory.py` loads all of the above and returns a ready `Pipeline`
- Supported `agent_type` values: `intent_analyzer`, `rag_info`, `generic_info`, `sanitizer`, `truncate`
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
- On each call: loads conversation history from `MemoryStore` (keyed by `ctx.chat_id or session_id`), builds prompt, calls `llm.call_llm` with `DEFAULT_LLM_CONFIG`, saves exchange to memory
- `GENERIC_INFO_SYSTEM_PROMPT` — same 3-step structure as RAGInfoAgent but answers from the model's full knowledge; no grounding restriction
- Use when no domain-specific knowledge base is needed

**`RAGInfoAgent`** — grounded customer service agent (`app/agents/rag_info_agent.py`):
- `RAGInfoAgent(namespace, system_prompt=RAG_INFO_SYSTEM_PROMPT, top_k=5, session_id=None, tool_names=[])`
- `run(ctx)` — uses `ctx.retrieval_query or ctx.input` for RAG retrieval; `ctx.input` as user-facing message
- On each call: retrieves top-k chunks, loads conversation history from `MemoryStore` (keyed by `ctx.chat_id or session_id`), builds prompt, calls `llm.call_llm` with `DEFAULT_LLM_CONFIG`, saves exchange to memory
- `RAG_INFO_SYSTEM_PROMPT` — 4-step logic: ground in context → scope check → calculator delegation → response
- Refuses out-of-scope questions; responds honestly when context has no answer — never hallucinate

**LLM provider abstraction** — `llm/`:
- All LLM calls route through `call_llm(config, messages, tools=None, dispatcher=None)` — the single choke point. No agent imports a provider SDK directly.
- Every provider (Gemini, DeepSeek) is reached via the OpenAI Python SDK against its OpenAI-compatible endpoint. Add a provider with one `LLMProvider` enum entry + one `_PROVIDER_SETTINGS` entry.
- `LLMConfig(provider, model, max_tokens, temperature, system_prompt)` — per-call config.
- `DEFAULT_LLM_CONFIG` — built from env `LLM_PROVIDER` / `LLM_MODEL`; falls back to `DEEPSEEK` / `deepseek-v4-flash`. All agents use this.
- `call_llm` runs the tool-execution loop internally: model emits tool_call → `dispatcher` runs it → result fed back → repeats until plain-text reply.
- `embed(text, model="gemini-embedding-001")` — embeddings; DeepSeek has no embeddings endpoint, so this stays on Gemini.
- `to_openai_tool(name, description, parameters)` (`llm/tools.py`) — builds the OpenAI function-calling tool schema.
- Clients cached at module load, one per provider.

**Tool registry** — `app/tools/__init__.py`:
- `TOOL_REGISTRY: dict[str, ToolEntry]` — maps registry key → `(declaration: dict, fn: Callable)`; declaration is an OpenAI-format tool dict.
- Registry key (e.g. `calculator`, `csv_lookup`) ≠ OpenAI function name (e.g. `calculate`, `lookup_rows`). `_FN_NAME_TO_KEY` + `_resolve_key()` resolve either form.
- `get_tools_for_agent(tool_names)` → list of OpenAI tool declarations for an agent's subset
- `make_dispatcher_for_agent(tool_names)` → scoped dispatcher that only allows the agent's tools; accepts function name or registry key
- `ALL_TOOLS` and `dispatch()` preserved for backward compat
- Adding a new tool: implement in `app/tools/`, build its declaration with `to_openai_tool`, add to `TOOL_REGISTRY`, add to `AgentTool` rows in DB

**RAG scoping** — `rag/store.py` + `rag_sources` table:
- All chunks stored in single `rag_chunks(namespace, content, embedding, metadata)` table
- `rag_sources(namespace, scope_type, scope_id)` registry maps a namespace to its owner
- Scope types: `"bot"` → namespace `bot_{id}`, `"workflow"` → `workflow_{id}`, `"agent"` → `agent_{id}`
- `get_namespace(scope_type, scope_id)` — looks up the registered namespace for a scope
- `init_rag_table(namespace)` — ensures `rag_chunks` table + index exist (idempotent, namespace validated)
- `ingest(file_path, namespace, ...)` — chunks + embeds file, inserts rows into `rag_chunks`
- `retrieve(query, namespace, top_k=5)` — cosine similarity search filtered by namespace
- `make_rag_tool(namespace)` + `make_rag_dispatcher(namespace)` — build a `retrieve_documents` OpenAI-format tool and dispatcher scoped to a namespace
- Namespace validated as `[a-zA-Z0-9_]+` to prevent SQL injection
- Upload endpoints: `POST /bots/{id}/documents`, `/workflows/{id}/documents`, `/agent-configs/{id}/documents`

**Namespace resolution for `rag_info` agents** (in `factory.py`):
1. Explicit `namespace` key in `agent_config.config_json`
2. `rag_sources` entry with `scope_type="agent"`, `scope_id=agent_config.id`
3. `rag_sources` entry with `scope_type="workflow"`, `scope_id=workflow_id`
4. Raises `ValueError` if none found

## Environment

Copy `.env.example` to `.env` and fill in:
- `GEMINI_API_KEY` — Google Gemini API key (used for embeddings, and for Gemini chat if selected)
- `DEEPSEEK_API_KEY` — DeepSeek API key (default chat provider)
- `LLM_PROVIDER` — default chat provider: `DEEPSEEK` or `GEMINI` (optional; defaults to `DEEPSEEK`)
- `LLM_MODEL` — default chat model (optional; defaults to `deepseek-v4-flash`)
- `DATABASE_URL` — PostgreSQL connection string (default for local Docker: `postgresql://user:1234@localhost:5432/minibots`)
- `ENVIRONMENT` — set to `development` to allow all CORS origins (`ALLOWED_ORIGINS=["*"]`)

App env vars are read once in `app/config.py`; LLM env vars (`DEEPSEEK_API_KEY`, `LLM_PROVIDER`, `LLM_MODEL`) are read in `llm/client.py`. Switching the chat provider globally requires only changing `LLM_PROVIDER` + `LLM_MODEL`. Database schema is auto-created on startup via SQLAlchemy `create_all`. Additional schema changes go in `migrate.py` and are run manually (no Alembic).
