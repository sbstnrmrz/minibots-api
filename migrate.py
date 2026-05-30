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

    conn.execute(text("""
        ALTER TABLE tenants
            ADD COLUMN IF NOT EXISTS gcal_calendar_id VARCHAR,
            ADD COLUMN IF NOT EXISTS api_token VARCHAR UNIQUE;
    """))

    conn.execute(text("""
        ALTER TABLE workflows
            ADD COLUMN IF NOT EXISTS tenant_id UUID REFERENCES tenants(id);
    """))

    conn.execute(text("""
        ALTER TABLE bots
            ADD COLUMN IF NOT EXISTS tenant_id UUID REFERENCES tenants(id),
            DROP COLUMN IF EXISTS system_prompt,
            DROP COLUMN IF EXISTS documents_urls;
    """))

    # --- no-bot (tenant-default) chat persistence ---
    # Make bot_id nullable on chats and chat_messages so tenant-default
    # agent flows (no explicit bot) can persist their conversations.
    conn.execute(text("""
        ALTER TABLE chats
            ADD COLUMN IF NOT EXISTS tenant_id UUID REFERENCES tenants(id);
    """))
    conn.execute(text("""
        ALTER TABLE chats
            ALTER COLUMN bot_id DROP NOT NULL;
    """))
    conn.execute(text("""
        ALTER TABLE chat_messages
            ADD COLUMN IF NOT EXISTS tenant_id UUID REFERENCES tenants(id);
    """))
    conn.execute(text("""
        ALTER TABLE chat_messages
            ALTER COLUMN bot_id DROP NOT NULL;
    """))

    # --- token usage tracking ---
    conn.execute(text("""
        ALTER TABLE chat_messages
            ADD COLUMN IF NOT EXISTS prompt_tokens     INTEGER,
            ADD COLUMN IF NOT EXISTS completion_tokens INTEGER,
            ADD COLUMN IF NOT EXISTS total_tokens      INTEGER,
            ADD COLUMN IF NOT EXISTS cost_usd          NUMERIC(12, 8);
    """))

    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS llm_calls (
            id               BIGSERIAL PRIMARY KEY,
            tenant_id        UUID        NOT NULL REFERENCES tenants(id),
            bot_id           INTEGER     REFERENCES bots(id),
            chat_id          VARCHAR     REFERENCES chats(id),
            chat_message_id  INTEGER     REFERENCES chat_messages(id),
            agent_name       VARCHAR,
            provider         VARCHAR     NOT NULL,
            model            VARCHAR     NOT NULL,
            prompt_tokens    INTEGER     NOT NULL,
            completion_tokens INTEGER    NOT NULL,
            total_tokens     INTEGER     NOT NULL,
            cost_usd         NUMERIC(12, 8),
            created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
    """))
    conn.execute(text("""
        CREATE INDEX IF NOT EXISTS ix_llm_calls_tenant
            ON llm_calls (tenant_id);
    """))
    conn.execute(text("""
        CREATE INDEX IF NOT EXISTS ix_llm_calls_chat
            ON llm_calls (chat_id);
    """))
    conn.execute(text("""
        CREATE INDEX IF NOT EXISTS ix_llm_calls_ts
            ON llm_calls (created_at);
    """))

    # --- agent_configs: config_scope discriminator ---
    conn.execute(text("""
        ALTER TABLE agent_configs
            ADD COLUMN IF NOT EXISTS config_scope VARCHAR;
    """))
    # Backfill: agent_configs referenced by tenants.agent_config_id → "tenant_default"
    conn.execute(text("""
        UPDATE agent_configs
        SET config_scope = 'tenant_default'
        WHERE id IN (SELECT agent_config_id FROM tenants WHERE agent_config_id IS NOT NULL)
          AND config_scope IS NULL;
    """))
    # Backfill: agent_configs referenced only by workflow_agents → "workflow_step"
    conn.execute(text("""
        UPDATE agent_configs
        SET config_scope = 'workflow_step'
        WHERE id IN (SELECT agent_config_id FROM workflow_agents)
          AND id NOT IN (SELECT agent_config_id FROM tenants WHERE agent_config_id IS NOT NULL)
          AND config_scope IS NULL;
    """))

    # --- rag_sources: backfill rows for existing tenant default agents ---
    # Tenants that ran /agents/setup before this fix have chunks in rag_chunks
    # but no rag_sources row. Insert the missing rows so get_namespace() works.
    conn.execute(text("""
        INSERT INTO rag_sources (namespace, scope_type, scope_id)
        SELECT
            'agent_' || ac.id,
            'agent',
            ac.id
        FROM agent_configs ac
        JOIN tenants t ON t.agent_config_id = ac.id
        WHERE NOT EXISTS (
            SELECT 1 FROM rag_sources rs
            WHERE rs.namespace = 'agent_' || ac.id
        );
    """))

    # --- tenant_files: enforce tenant_id NOT NULL ---
    conn.execute(text("""
        DO $$
        BEGIN
            -- Remove orphan rows (should not exist, but be safe)
            DELETE FROM tenant_files WHERE tenant_id IS NULL;
            IF EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'tenant_files'
                  AND column_name = 'tenant_id'
                  AND is_nullable = 'YES'
            ) THEN
                ALTER TABLE tenant_files ALTER COLUMN tenant_id SET NOT NULL;
            END IF;
        END $$;
    """))

    # --- chat_messages: enforce tenant_id NOT NULL ---
    conn.execute(text("""
        -- Backfill from the parent Chat row where possible
        UPDATE chat_messages cm
        SET tenant_id = c.tenant_id
        FROM chats c
        WHERE cm.chat_id = c.id
          AND cm.tenant_id IS NULL
          AND c.tenant_id IS NOT NULL;
    """))
    conn.execute(text("""
        DO $$
        BEGIN
            -- Remove rows that still have no tenant_id (no recoverable parent)
            DELETE FROM chat_messages WHERE tenant_id IS NULL;
            IF EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'chat_messages'
                  AND column_name = 'tenant_id'
                  AND is_nullable = 'YES'
            ) THEN
                ALTER TABLE chat_messages ALTER COLUMN tenant_id SET NOT NULL;
            END IF;
        END $$;
    """))

    # --- reservations: cancelled flag + reservation_code ---
    conn.execute(text("""
        ALTER TABLE reservations
            ADD COLUMN IF NOT EXISTS cancelled         BOOLEAN NOT NULL DEFAULT FALSE,
            ADD COLUMN IF NOT EXISTS reservation_code VARCHAR;
    """))
    conn.execute(text("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_reservations_code
            ON reservations (reservation_code)
            WHERE reservation_code IS NOT NULL;
    """))

    # --- rag_sources: tenant ownership for cross-tenant RAG isolation ---
    # Alembic-style (when Alembic is wired up):
    #   op.add_column("rag_sources", sa.Column("tenant_id", postgresql.UUID(as_uuid=True),
    #                 sa.ForeignKey("tenants.id"), nullable=True))
    conn.execute(text("""
        ALTER TABLE rag_sources
            ADD COLUMN IF NOT EXISTS tenant_id UUID REFERENCES tenants(id);
    """))
    # Backfill owner from each scope's parent table. After backfill,
    # retrieve(tenant_id=...) can reject a namespace that isn't the tenant's.
    #   - "agent"    scope_id → agent_configs.id → tenants.agent_config_id
    conn.execute(text("""
        UPDATE rag_sources rs
        SET tenant_id = t.id
        FROM tenants t
        WHERE rs.scope_type = 'agent'
          AND t.agent_config_id = rs.scope_id
          AND rs.tenant_id IS NULL;
    """))
    #   - "bot"      scope_id → bots.id → bots.tenant_id
    conn.execute(text("""
        UPDATE rag_sources rs
        SET tenant_id = b.tenant_id
        FROM bots b
        WHERE rs.scope_type = 'bot'
          AND b.id = rs.scope_id
          AND rs.tenant_id IS NULL;
    """))
    #   - "workflow" scope_id → workflows.id → workflows.tenant_id
    conn.execute(text("""
        UPDATE rag_sources rs
        SET tenant_id = w.tenant_id
        FROM workflows w
        WHERE rs.scope_type = 'workflow'
          AND w.id = rs.scope_id
          AND rs.tenant_id IS NULL;
    """))
    conn.execute(text("""
        CREATE INDEX IF NOT EXISTS ix_rag_sources_tenant
            ON rag_sources (tenant_id);
    """))

    # --- reservations: index tenant_id for tenant-scoped overlap/cancel ---
    # Overlap and cancellation queries now filter on tenant_id so one
    # tenant's bookings can't block or be cancelled by another.
    #   op.create_index("ix_reservations_tenant", "reservations", ["tenant_id"])
    conn.execute(text("""
        CREATE INDEX IF NOT EXISTS ix_reservations_tenant
            ON reservations (tenant_id);
    """))

    # --- tenants.id: UUID → TEXT (use crazyagents org_id as PK directly) ---
    # Drop all FK constraints that reference tenants.id, change the column
    # types, then re-add the constraints.
    conn.execute(text("""
        DO $$
        DECLARE
            r RECORD;
        BEGIN
            FOR r IN (
                SELECT kcu.table_name, tc.constraint_name
                FROM information_schema.table_constraints tc
                JOIN information_schema.referential_constraints rc
                    ON tc.constraint_name = rc.constraint_name
                JOIN information_schema.key_column_usage kcu
                    ON tc.constraint_name = kcu.constraint_name
                JOIN information_schema.table_constraints ccu
                    ON rc.unique_constraint_name = ccu.constraint_name
                WHERE tc.constraint_type = 'FOREIGN KEY'
                  AND ccu.table_name = 'tenants'
            ) LOOP
                EXECUTE format('ALTER TABLE %I DROP CONSTRAINT %I', r.table_name, r.constraint_name);
            END LOOP;
        END $$;
    """))

    conn.execute(text("ALTER TABLE tenants ALTER COLUMN id TYPE TEXT USING id::TEXT;"))
    conn.execute(text("ALTER TABLE tenants ALTER COLUMN id DROP DEFAULT;"))
    conn.execute(text("ALTER TABLE tenants ALTER COLUMN agent_tier DROP NOT NULL;"))

    for tbl in (
        "tenant_files", "workflows", "bots", "chats",
        "chat_messages", "llm_calls", "rag_sources", "reservations",
    ):
        conn.execute(text(
            f"ALTER TABLE {tbl} ALTER COLUMN tenant_id TYPE TEXT USING tenant_id::TEXT;"
        ))

    conn.execute(text("ALTER TABLE tenant_files  ADD CONSTRAINT tenant_files_tenant_id_fkey  FOREIGN KEY (tenant_id) REFERENCES tenants(id);"))
    conn.execute(text("ALTER TABLE workflows     ADD CONSTRAINT workflows_tenant_id_fkey     FOREIGN KEY (tenant_id) REFERENCES tenants(id);"))
    conn.execute(text("ALTER TABLE bots          ADD CONSTRAINT bots_tenant_id_fkey          FOREIGN KEY (tenant_id) REFERENCES tenants(id);"))
    conn.execute(text("ALTER TABLE chats         ADD CONSTRAINT chats_tenant_id_fkey         FOREIGN KEY (tenant_id) REFERENCES tenants(id);"))
    conn.execute(text("ALTER TABLE chat_messages ADD CONSTRAINT chat_messages_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES tenants(id);"))
    conn.execute(text("ALTER TABLE llm_calls     ADD CONSTRAINT llm_calls_tenant_id_fkey     FOREIGN KEY (tenant_id) REFERENCES tenants(id);"))
    conn.execute(text("ALTER TABLE rag_sources   ADD CONSTRAINT rag_sources_tenant_id_fkey   FOREIGN KEY (tenant_id) REFERENCES tenants(id);"))
    conn.execute(text("ALTER TABLE reservations  ADD CONSTRAINT reservations_tenant_id_fkey  FOREIGN KEY (tenant_id) REFERENCES tenants(id);"))

    conn.commit()
    print("Migration complete.")
