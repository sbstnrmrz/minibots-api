from app.db_pool import connection

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS agent_memory (
    id         SERIAL PRIMARY KEY,
    session_id TEXT        NOT NULL,
    agent_name TEXT        NOT NULL,
    role       TEXT        NOT NULL,
    content    TEXT        NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""

# How many recent turns to load per (session, agent). Memory grows
# unbounded otherwise, both in DB and in the prompt context that
# Pipeline injects before each agent step.
_MEMORY_LOAD_LIMIT = 40

_table_ready = False


def _ensure_table_once() -> None:
    global _table_ready
    if _table_ready:
        return
    with connection() as conn, conn.cursor() as cur:
        cur.execute(_CREATE_TABLE)
    _table_ready = True


class MemoryStore:
    def __init__(self) -> None:
        _ensure_table_once()

    def load(self, session_id: str, agent_name: str) -> list[dict]:
        with connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT role, content FROM (
                    SELECT role, content, created_at
                    FROM agent_memory
                    WHERE session_id = %s AND agent_name = %s
                    ORDER BY created_at DESC
                    LIMIT %s
                ) recent
                ORDER BY created_at
                """,
                (session_id, agent_name, _MEMORY_LOAD_LIMIT),
            )
            return [{"role": row[0], "content": row[1]} for row in cur.fetchall()]

    def save(self, session_id: str, agent_name: str, role: str, content: str) -> None:
        with connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO agent_memory (session_id, agent_name, role, content)
                VALUES (%s, %s, %s, %s)
                """,
                (session_id, agent_name, role, content),
            )
