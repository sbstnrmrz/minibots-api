import json
import re
from pathlib import Path
from typing import Any, Callable

import numpy as np
import psycopg2
from markitdown import MarkItDown
from pgvector.psycopg2 import register_vector

from app.config import DATABASE_URL
from llm import embed as _embed_llm
from llm.tools import to_openai_tool

_md = MarkItDown()

_EMBED_MODEL = "gemini-embedding-001"
_EMBED_DIM = 3072
_VALID_NAMESPACE = re.compile(r"^[a-zA-Z0-9_]+$")

_INIT_TABLE_SQL = f"""
CREATE TABLE IF NOT EXISTS rag_chunks (
    id         SERIAL PRIMARY KEY,
    namespace  TEXT         NOT NULL,
    content    TEXT         NOT NULL,
    embedding  vector({_EMBED_DIM}) NOT NULL,
    metadata   JSONB,
    created_at TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_rag_chunks_namespace ON rag_chunks (namespace);
"""


def _validate_namespace(namespace: str) -> None:
    if not _VALID_NAMESPACE.match(namespace):
        raise ValueError(
            f"Invalid namespace '{namespace}'. Use only letters, digits, and underscores."
        )


def _connect():
    conn = psycopg2.connect(DATABASE_URL)
    register_vector(conn)
    return conn


def _embed(text: str) -> list[float]:
    return _embed_llm(text, model=_EMBED_MODEL)


def _read_file(file_path: str) -> str:
    path = Path(file_path)
    if path.suffix.lower() == ".md":
        return path.read_text(encoding="utf-8")
    return _md.convert(file_path).text_content


def _chunk_text(text: str, chunk_size: int, overlap: int) -> list[str]:
    chunks = []
    start = 0
    while start < len(text):
        chunk = text[start : start + chunk_size]
        if chunk.strip():
            chunks.append(chunk)
        start += chunk_size - overlap
    return chunks


def init_rag_table(namespace: str) -> None:
    """Ensure the shared rag_chunks table and its namespace index exist."""
    _validate_namespace(namespace)
    with _connect() as conn, conn.cursor() as cur:
        cur.execute("CREATE EXTENSION IF NOT EXISTS vector;")
        cur.execute(_INIT_TABLE_SQL)


def ingest(
    file_path: str,
    namespace: str,
    chunk_size: int = 500,
    overlap: int = 50,
    source_name: str | None = None,
) -> int:
    _validate_namespace(namespace)
    text = _read_file(file_path)
    chunks = _chunk_text(text, chunk_size, overlap)
    source = source_name or Path(file_path).name

    with _connect() as conn, conn.cursor() as cur:
        cur.execute("CREATE EXTENSION IF NOT EXISTS vector;")
        cur.execute(_INIT_TABLE_SQL)
        for i, chunk in enumerate(chunks):
            embedding = np.array(_embed(chunk))
            meta = json.dumps({"source": source, "chunk_index": i})
            cur.execute(
                "INSERT INTO rag_chunks (namespace, content, embedding, metadata) VALUES (%s, %s, %s, %s)",
                (namespace, chunk, embedding, meta),
            )

    return len(chunks)


def clear_namespace(namespace: str) -> None:
    """Delete all chunks for a namespace."""
    _validate_namespace(namespace)
    with _connect() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM rag_chunks WHERE namespace = %s", (namespace,))


def has_rag_table(namespace: str) -> bool:
    """Return True if any chunks exist for this namespace."""
    _validate_namespace(namespace)
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT EXISTS (SELECT 1 FROM rag_chunks WHERE namespace = %s LIMIT 1)",
            (namespace,),
        )
        return cur.fetchone()[0]


def get_namespace(scope_type: str, scope_id: int) -> str | None:
    """Return the registered RAG namespace for a given scope, or None if not found."""
    with psycopg2.connect(DATABASE_URL) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT namespace FROM rag_sources WHERE scope_type = %s AND scope_id = %s LIMIT 1",
            (scope_type, scope_id),
        )
        row = cur.fetchone()
        return row[0] if row else None


_RAG_TOOL_DECLARATION = to_openai_tool(
    name="retrieve_documents",
    description=(
        "Search the knowledge base for information relevant to the query. "
        "Call this whenever the user asks something that may be answered by uploaded documents."
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "The search query to look up in the knowledge base.",
            },
            "top_k": {
                "type": "integer",
                "description": "Number of results to return (default 5).",
            },
        },
        "required": ["query"],
    },
)


def make_rag_tool(namespace: str) -> dict:
    _validate_namespace(namespace)
    return _RAG_TOOL_DECLARATION


def make_rag_dispatcher(namespace: str) -> Callable[[str, dict[str, Any]], Any]:
    _validate_namespace(namespace)

    def dispatcher(name: str, args: dict[str, Any]) -> Any:
        if name == "retrieve_documents":
            results = retrieve(args["query"], namespace, top_k=args.get("top_k", 5))
            return [{"content": r["content"], "similarity_score": r["similarity_score"]} for r in results]
        raise ValueError(f"Unknown tool: '{name}'")

    return dispatcher


def retrieve(query: str, namespace: str, top_k: int = 5) -> list[dict]:
    _validate_namespace(namespace)

    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT EXISTS (SELECT 1 FROM rag_chunks WHERE namespace = %s LIMIT 1)",
            (namespace,),
        )
        if not cur.fetchone()[0]:
            raise ValueError(f"Namespace '{namespace}' has no ingested data.")

        query_embedding = np.array(_embed(query))
        cur.execute(
            """
            SELECT content, metadata, 1 - (embedding <=> %s) AS similarity_score
            FROM rag_chunks
            WHERE namespace = %s
            ORDER BY embedding <=> %s
            LIMIT %s
            """,
            (query_embedding, namespace, query_embedding, top_k),
        )
        return [
            {"content": row[0], "metadata": row[1], "similarity_score": float(row[2])}
            for row in cur.fetchall()
        ]
