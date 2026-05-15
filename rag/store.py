import json
import re
from pathlib import Path
from typing import Any, Callable

import numpy as np
import psycopg2
from google.genai import types
from markitdown import MarkItDown
from pgvector.psycopg2 import register_vector

from app.config import DATABASE_URL
from app.services.gemini import _client

_md = MarkItDown()

_EMBED_MODEL = "gemini-embedding-001"
_EMBED_DIM = 3072
_VALID_NAMESPACE = re.compile(r"^[a-zA-Z0-9_]+$")


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
    res = _client.models.embed_content(model=_EMBED_MODEL, contents=text)
    return res.embeddings[0].values


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
    _validate_namespace(namespace)
    with _connect() as conn, conn.cursor() as cur:
        cur.execute("CREATE EXTENSION IF NOT EXISTS vector;")
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS rag_{namespace} (
                id         SERIAL PRIMARY KEY,
                content    TEXT        NOT NULL,
                embedding  vector({_EMBED_DIM}) NOT NULL,
                metadata   JSONB,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
        """)


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
        for i, chunk in enumerate(chunks):
            embedding = np.array(_embed(chunk))
            meta = json.dumps({"source": source, "chunk_index": i})
            cur.execute(
                f"INSERT INTO rag_{namespace} (content, embedding, metadata) VALUES (%s, %s, %s)",
                (chunk, embedding, meta),
            )

    return len(chunks)


def has_rag_table(namespace: str) -> bool:
    _validate_namespace(namespace)
    table = f"rag_{namespace}"
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = %s)",
            (table,),
        )
        return cur.fetchone()[0]


def make_rag_tool(bot_id: int) -> types.Tool:
    return types.Tool(
        function_declarations=[
            types.FunctionDeclaration(
                name="retrieve_documents",
                description=(
                    "Search this bot's knowledge base for information relevant to the query. "
                    "Call this whenever the user asks something that may be answered by uploaded documents."
                ),
                parameters=types.Schema(
                    type=types.Type.OBJECT,
                    properties={
                        "query": types.Schema(
                            type=types.Type.STRING,
                            description="The search query to look up in the knowledge base.",
                        ),
                        "top_k": types.Schema(
                            type=types.Type.INTEGER,
                            description="Number of results to return (default 5).",
                        ),
                    },
                    required=["query"],
                ),
            )
        ]
    )


def make_rag_dispatcher(bot_id: int) -> Callable[[str, dict[str, Any]], Any]:
    namespace = f"bot_{bot_id}"

    def dispatcher(name: str, args: dict[str, Any]) -> Any:
        if name == "retrieve_documents":
            results = retrieve(args["query"], namespace, top_k=args.get("top_k", 5))
            return [{"content": r["content"], "similarity_score": r["similarity_score"]} for r in results]
        raise ValueError(f"Unknown tool: '{name}'")

    return dispatcher


def retrieve(query: str, namespace: str, top_k: int = 5) -> list[dict]:
    _validate_namespace(namespace)
    table = f"rag_{namespace}"

    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = %s)",
            (table,),
        )
        if not cur.fetchone()[0]:
            raise ValueError(f"Namespace '{namespace}' does not exist. Call init_rag_table first.")

        query_embedding = np.array(_embed(query))
        cur.execute(
            f"""
            SELECT content, metadata, 1 - (embedding <=> %s) AS similarity_score
            FROM {table}
            ORDER BY embedding <=> %s
            LIMIT %s
            """,
            (query_embedding, query_embedding, top_k),
        )
        return [
            {"content": row[0], "metadata": row[1], "similarity_score": float(row[2])}
            for row in cur.fetchall()
        ]
