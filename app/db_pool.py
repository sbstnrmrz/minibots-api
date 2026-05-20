"""Shared psycopg connection pool for direct-SQL code paths.

SQLAlchemy already pools the FastAPI request-scoped sessions, but
`rag/store.py` and `app/agents/memory.py` reach for raw psycopg, and
each chat turn opens 4-8 short-lived connections that way. Routing
those calls through a single pgvector-registered pool removes the
per-call TCP+TLS+auth handshake.

`connection()` yields a checked-out connection with the pgvector type
codec already registered, so callers don't need to touch
`register_vector` themselves.
"""

from contextlib import contextmanager

from pgvector.psycopg import register_vector
from psycopg_pool import ConnectionPool

from app.config import DATABASE_URL


def _configure(conn):
    register_vector(conn)


_pool: ConnectionPool | None = None


def get_pool() -> ConnectionPool:
    """Lazy singleton — initialised on first use to keep import cheap."""
    global _pool
    if _pool is None:
        _pool = ConnectionPool(
            conninfo=DATABASE_URL,
            min_size=1,
            max_size=10,
            kwargs={"autocommit": False},
            configure=_configure,
            open=True,
        )
    return _pool


@contextmanager
def connection():
    """Check a connection out of the pool. Returns it on exit."""
    with get_pool().connection() as conn:
        yield conn
