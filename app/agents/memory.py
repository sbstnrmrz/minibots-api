import psycopg2
from app.config import DATABASE_URL

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


class MemoryStore:
    def __init__(self) -> None:
        self._dsn = DATABASE_URL
        self._ensure_table()

    def _connect(self):
        return psycopg2.connect(self._dsn)

    def _ensure_table(self) -> None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(_CREATE_TABLE)

    def load(self, session_id: str, agent_name: str) -> list[dict]:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT role, content FROM agent_memory
                WHERE session_id = %s AND agent_name = %s
                ORDER BY created_at
                """,
                (session_id, agent_name),
            )
            return [{"role": row[0], "content": row[1]} for row in cur.fetchall()]

    def save(self, session_id: str, agent_name: str, role: str, content: str) -> None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO agent_memory (session_id, agent_name, role, content)
                VALUES (%s, %s, %s, %s)
                """,
                (session_id, agent_name, role, content),
            )
