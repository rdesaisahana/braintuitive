"""Preflight check for the services the RAG pipeline depends on.

Verifies each integration independently and reports what is actually true --
including the real embedding dimension, which must match both
``EMBEDDING_DIMENSIONS`` and any existing Pinecone index.

Run it any of these ways -- all equivalent:
    cd backend && python -m utils.check_services
    cd backend && python utils/check_services.py
    python backend/utils/check_services.py
    cd backend/utils && python check_services.py
"""

from __future__ import annotations

import sys
from pathlib import Path

# Put backend/ on sys.path so `config`, `db` and `rag` import regardless of the
# working directory. Running this file as a plain script (rather than with
# `-m` from backend/) otherwise fails with ModuleNotFoundError: No module
# named 'config'.
_BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from typing import Any  # noqa: E402

from config import settings  # noqa: E402

OK = "  OK   "
FAIL = " FAIL  "
SKIP = " SKIP  "


def _line(status: str, name: str, detail: str) -> None:
    print(f"[{status}] {name:<22} {detail}")


def _squash(exc: Exception, limit: int = 240) -> str:
    """Collapse an exception to one readable line.

    Provider errors carry the actionable part (an unknown model id, a bad
    region) late in the message, so truncating too early hides the cause.
    """
    text = " ".join(str(exc).split())
    return text[:limit] + ("..." if len(text) > limit else "")


def check_sqlite() -> bool:
    """Confirm the relational database answers a query."""
    try:
        from db.database import check_sql_connection

        if check_sql_connection():
            _line(OK, "SQLite", str(settings.SQLITE_PATH))
            return True
        _line(FAIL, "SQLite", "connection failed")
    except Exception as exc:
        _line(FAIL, "SQLite", str(exc)[:90])
    return False


def check_nebius_embeddings() -> int | None:
    """Return the real embedding dimension, or None if unreachable."""
    if not settings.nebius_configured:
        _line(SKIP, "Nebius embeddings", "NEBIUS_API_KEY not set")
        return None

    try:
        from rag.embeddings import NebiusEmbedder

        dimensions = NebiusEmbedder().probe_dimensions()
    except Exception as exc:
        _line(FAIL, "Nebius embeddings", f"{type(exc).__name__}: {_squash(exc)}")
        return None

    configured = settings.EMBEDDING_DIMENSIONS
    if dimensions == configured:
        _line(OK, "Nebius embeddings", f"{settings.NEBIUS_EMBEDDING_MODEL} -> {dimensions} dims")
    else:
        _line(
            FAIL,
            "Nebius embeddings",
            f"model returns {dimensions} dims but EMBEDDING_DIMENSIONS={configured}. "
            f"Set EMBEDDING_DIMENSIONS={dimensions} in .env",
        )
    return dimensions


def check_nebius_chat() -> bool:
    """Confirm the chat model used by the Phase 3 agents responds."""
    if not settings.nebius_configured:
        _line(SKIP, "Nebius chat", "NEBIUS_API_KEY not set")
        return False

    try:
        from openai import OpenAI

        client = OpenAI(
            api_key=settings.NEBIUS_API_KEY,
            base_url=settings.NEBIUS_BASE_URL,
            timeout=45,
        )
        response = client.chat.completions.create(
            model=settings.NEBIUS_MODEL,
            messages=[{"role": "user", "content": "Reply with the single word: ready"}],
            max_tokens=8,
            temperature=0,
        )
        reply = (response.choices[0].message.content or "").strip()
        _line(OK, "Nebius chat", f"{settings.NEBIUS_MODEL} -> {reply[:40]!r}")
        return True
    except Exception as exc:
        _line(FAIL, "Nebius chat", f"{type(exc).__name__}: {_squash(exc)}")
    return False


def check_pinecone(expected_dimensions: int | None) -> bool:
    """Confirm Pinecone authenticates and report the target index state."""
    if not settings.pinecone_configured:
        _line(SKIP, "Pinecone", "PINECONE_API_KEY not set")
        return False

    try:
        from rag.pinecone_client import PineconeStore

        store = PineconeStore()
        indexes = store.list_indexes()
    except Exception as exc:
        _line(FAIL, "Pinecone", f"{type(exc).__name__}: {_squash(exc)}")
        return False

    _line(OK, "Pinecone auth", f"{len(indexes)} existing index(es): {indexes}")

    if store.index_name not in indexes:
        _line(
            SKIP,
            "Pinecone index",
            f"{store.index_name!r} does not exist yet - ingest will create it",
        )
        return True

    try:
        stats: dict[str, Any] = store.stats()
    except Exception as exc:
        _line(FAIL, "Pinecone index", str(exc)[:90])
        return False

    dimension = stats.get("dimension")
    detail = f"{store.index_name!r} dim={dimension} vectors={stats.get('total_vector_count')}"
    if expected_dimensions is not None and dimension != expected_dimensions:
        _line(
            FAIL,
            "Pinecone index",
            f"{detail} but embeddings are {expected_dimensions} dims - delete and recreate",
        )
        return False
    _line(OK, "Pinecone index", detail)
    return True


def check_mongo() -> bool:
    """MongoDB is optional; report but never fail the run on it."""
    if not settings.mongodb_configured:
        _line(SKIP, "MongoDB", "MONGODB_URI not set (optional - chunks stay in Pinecone)")
        return True

    import asyncio

    from db.database import check_mongo_connection, connect_to_mongo

    async def _probe() -> bool:
        await connect_to_mongo()
        return await check_mongo_connection()

    try:
        if asyncio.run(_probe()):
            _line(OK, "MongoDB", settings.MONGODB_DB_NAME)
            return True
        _line(FAIL, "MongoDB", "ping failed")
    except Exception as exc:
        _line(FAIL, "MongoDB", str(exc)[:90])
    return False


def main() -> int:
    """Run every check. Returns 0 when the pipeline can run end to end."""
    print(f"\nBraintuitive service check ({settings.ENVIRONMENT})")
    print("-" * 72)

    sqlite_ok = check_sqlite()
    dimensions = check_nebius_embeddings()
    chat_ok = check_nebius_chat()
    pinecone_ok = check_pinecone(dimensions)
    check_mongo()

    print("-" * 72)
    ready = sqlite_ok and dimensions is not None and pinecone_ok
    if ready:
        print("RAG ingestion is ready to run:")
        print('  python -m rag.pipeline "../06 Pre-Algebra H&A.pdf" --grade 6')
        if not chat_ok:
            print("Note: the chat model failed; Phase 3 agents will not work yet.")
    else:
        print("RAG ingestion is NOT ready - resolve the FAIL lines above.")
    print()
    return 0 if ready else 1


if __name__ == "__main__":  # pragma: no cover - CLI
    sys.exit(main())
