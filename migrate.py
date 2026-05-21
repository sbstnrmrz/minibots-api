from app.database import engine
from sqlalchemy import text

with engine.connect() as conn:
    # --- existing columns (idempotent) ---
    conn.execute(text("""
        ALTER TABLE bots
            ADD COLUMN IF NOT EXISTS bot_type      VARCHAR NOT NULL DEFAULT 'zen_coach',
            ADD COLUMN IF NOT EXISTS spreadsheet_id VARCHAR;
    """))

    # --- new schema tables ---
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS workflows (
            id          SERIAL PRIMARY KEY,
            name        VARCHAR NOT NULL,
            description VARCHAR,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
    """))

    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS agent_configs (
            id            SERIAL PRIMARY KEY,
            name          VARCHAR NOT NULL,
            agent_type    VARCHAR NOT NULL,
            system_prompt TEXT,
            config_json   JSONB
        );
    """))

    conn.execute(text("""
        ALTER TABLE agent_configs
            ADD COLUMN IF NOT EXISTS links JSONB;
    """))

    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS workflow_agents (
            id              SERIAL PRIMARY KEY,
            workflow_id     INTEGER NOT NULL REFERENCES workflows(id),
            agent_config_id INTEGER NOT NULL REFERENCES agent_configs(id),
            position        INTEGER NOT NULL
        );
    """))

    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS agent_tools (
            id              SERIAL PRIMARY KEY,
            agent_config_id INTEGER NOT NULL REFERENCES agent_configs(id),
            tool_name       VARCHAR NOT NULL
        );
    """))

    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS rag_sources (
            id         SERIAL PRIMARY KEY,
            namespace  VARCHAR NOT NULL UNIQUE,
            scope_type VARCHAR NOT NULL,
            scope_id   INTEGER NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
    """))

    conn.execute(text("""
        ALTER TABLE bots
            ADD COLUMN IF NOT EXISTS workflow_id INTEGER REFERENCES workflows(id);
    """))

    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS chats (
            id         VARCHAR PRIMARY KEY,
            bot_id     INTEGER NOT NULL REFERENCES bots(id),
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
    """))

    conn.execute(text("""
        ALTER TABLE chat_messages
            ADD COLUMN IF NOT EXISTS chat_id VARCHAR REFERENCES chats(id);
    """))

    # --- consolidated RAG table (replaces per-namespace rag_* tables) ---
    conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector;"))
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS rag_chunks (
            id         SERIAL PRIMARY KEY,
            namespace  TEXT         NOT NULL,
            content    TEXT         NOT NULL,
            embedding  vector(3072) NOT NULL,
            metadata   JSONB,
            created_at TIMESTAMPTZ  NOT NULL DEFAULT NOW()
        );
    """))
    conn.execute(text("""
        CREATE INDEX IF NOT EXISTS idx_rag_chunks_namespace ON rag_chunks (namespace);
    """))

    # HNSW ANN index on the embedding column. Without it every retrieve()
    # does a full namespace scan + 3072-d cosine per chunk. pgvector's
    # `vector` type caps HNSW at 2000 dimensions, so we index a halfvec
    # cast — the query in rag/store.py casts both sides the same way to
    # hit this index. CREATE INDEX IF NOT EXISTS is idempotent.
    conn.execute(text("""
        CREATE INDEX IF NOT EXISTS idx_rag_chunks_embedding_hnsw
        ON rag_chunks
        USING hnsw ((embedding::halfvec(3072)) halfvec_cosine_ops);
    """))

    # --- scheduling / reservations ---
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS reservations (
            id               SERIAL PRIMARY KEY,
            tenant_id        UUID        REFERENCES tenants(id),
            chat_id          VARCHAR,
            booker_name      VARCHAR     NOT NULL,
            booker_contact   VARCHAR,
            service          VARCHAR,
            start_time       TIMESTAMPTZ NOT NULL,
            end_time         TIMESTAMPTZ NOT NULL,
            duration_minutes INTEGER     NOT NULL,
            gcal_event_id    VARCHAR,
            gcal_sync_error  VARCHAR,
            created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
    """))
    conn.execute(text("""
        CREATE INDEX IF NOT EXISTS idx_reservations_time_range
            ON reservations (start_time, end_time);
    """))

    conn.commit()
    print("Migration complete.")
