"""Pinecone vector store wrapper.

Owns index lifecycle (create, describe, delete), batched upserts and filtered
queries. Everything above this module works with :class:`~rag.chunking.Chunk`
objects and never touches the Pinecone SDK directly.

Namespacing: one namespace per ``{subject}-grade{n}``, e.g. ``math-grade6``.
That keeps a district's grade 6 and grade 7 curricula in one free-tier index
without their vectors ever matching each other, and makes re-ingesting a
single grade a namespace delete rather than a full rebuild.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

from config import settings
from rag.chunking import Chunk

logger = logging.getLogger(__name__)

# Pinecone recommends <=100 vectors or ~2MB per upsert request.
UPSERT_BATCH_SIZE = 64


class PineconeError(RuntimeError):
    """Raised when a Pinecone operation cannot be completed."""


@dataclass
class SearchHit:
    """One retrieval result."""

    chunk_id: str
    score: float
    text: str
    metadata: dict[str, Any]

    def _int_field(self, key: str) -> int | None:
        """Read a whole-number metadata field.

        Pinecone stores every number as a float and returns it that way, so an
        integer written as 1 comes back as 1.0. Without this coercion citations
        render as "Unit 1.0, pp.5.0-8.0" -- which is what the agents would then
        quote back to students.
        """
        value = self.metadata.get(key)
        if isinstance(value, bool) or value is None:
            return None
        if isinstance(value, int | float):
            return int(value)
        try:
            return int(str(value))
        except ValueError:
            return None

    @property
    def unit_number(self) -> int | None:
        return self._int_field("unit_number")

    @property
    def sub_unit_number(self) -> str | None:
        return self.metadata.get("sub_unit_number")

    @property
    def citation(self) -> str:
        """Human-readable provenance, e.g. 'Unit 1 / 1.2, pp.5-8'."""
        unit = self._int_field("unit_number")
        start = self._int_field("page_start")
        end = self._int_field("page_end")
        sub = self.metadata.get("sub_unit_number")

        head = f"Unit {unit if unit is not None else '?'}"
        if sub:
            head += f" / {sub}"
        if start is None and end is None:
            return head
        return f"{head}, pp.{start}-{end}"


def namespace_for(subject: str, grade_level: int, user_id: str | None = None) -> str:
    """Return the namespace a curriculum lives in.

    Shared (operator-loaded) curriculum keeps the bare ``math-grade6`` form, so
    vectors indexed before curriculum ownership existed stay reachable without
    a re-ingest.

    An uploaded curriculum gets its own namespace per owner. Isolating by
    namespace rather than by a metadata filter means one family's search can
    never surface another family's material even if a filter is forgotten, and
    deleting an account is one namespace drop rather than a filtered scan.
    """
    base = f"{subject.lower().strip()}-grade{grade_level}"
    if user_id is None:
        return base
    # A uuid prefix is enough to separate accounts and keeps the namespace
    # readable in the Pinecone console.
    return f"{base}-u{user_id.replace('-', '')[:12]}"


class PineconeStore:
    """Thin, typed wrapper around a single Pinecone index.

    Example:
        store = PineconeStore()
        store.ensure_index(dimensions=4096)
        store.upsert_chunks(chunks, vectors, namespace="math-grade6")
    """

    def __init__(self, index_name: str | None = None) -> None:
        self.index_name = index_name or settings.PINECONE_INDEX_NAME
        self._client = None
        self._index = None

    # -- connection ------------------------------------------------------ #

    @property
    def client(self):  # noqa: ANN201 - pinecone.Pinecone, imported lazily
        """Lazily built Pinecone client."""
        if self._client is None:
            if not settings.pinecone_configured:
                raise PineconeError("PINECONE_API_KEY is not set - vector storage is unavailable.")
            from pinecone import Pinecone

            self._client = Pinecone(api_key=settings.PINECONE_API_KEY)
        return self._client

    def list_indexes(self) -> list[str]:
        """Return the names of every index on the account."""
        return [index["name"] for index in self.client.list_indexes()]

    def index_exists(self) -> bool:
        """True if this store's index already exists."""
        return self.index_name in self.list_indexes()

    # -- lifecycle ------------------------------------------------------- #

    def ensure_index(
        self,
        dimensions: int | None = None,
        metric: str | None = None,
        wait_timeout: int = 120,
    ) -> None:
        """Create the index if absent, and validate it if present.

        Args:
            dimensions: Vector size. Defaults to ``EMBEDDING_DIMENSIONS``.
            metric: Distance metric. Defaults to ``PINECONE_METRIC``.
            wait_timeout: Seconds to wait for a new index to become ready.

        Raises:
            PineconeError: If an existing index has a different dimension --
                silently writing 1024-d vectors into a 4096-d index is not
                something to paper over.
        """
        dimensions = dimensions or settings.EMBEDDING_DIMENSIONS
        metric = metric or settings.PINECONE_METRIC

        if self.index_exists():
            stats = self.client.Index(self.index_name).describe_index_stats()
            existing = int(stats.get("dimension", dimensions))
            if existing != dimensions:
                raise PineconeError(
                    f"Index {self.index_name!r} has dimension {existing}, but "
                    f"{dimensions} was requested. Either set EMBEDDING_DIMENSIONS="
                    f"{existing} or delete the index and let it be recreated."
                )
            logger.info("Pinecone index %r ready (dim=%d).", self.index_name, existing)
            return

        from pinecone import ServerlessSpec

        logger.info(
            "Creating Pinecone index %r (dim=%d, metric=%s, %s/%s)...",
            self.index_name,
            dimensions,
            metric,
            settings.PINECONE_CLOUD,
            settings.PINECONE_REGION,
        )
        self.client.create_index(
            name=self.index_name,
            dimension=dimensions,
            metric=metric,
            spec=ServerlessSpec(cloud=settings.PINECONE_CLOUD, region=settings.PINECONE_REGION),
        )

        deadline = time.time() + wait_timeout
        while time.time() < deadline:
            try:
                if self.client.describe_index(self.index_name)["status"]["ready"]:
                    logger.info("Index %r is ready.", self.index_name)
                    return
            except Exception:  # pragma: no cover - index still materialising
                pass
            time.sleep(2)

        raise PineconeError(f"Index {self.index_name!r} was not ready within {wait_timeout}s.")

    @property
    def index(self):  # noqa: ANN201 - pinecone.Index, resolved lazily
        """The Pinecone index handle."""
        if self._index is None:
            if not self.index_exists():
                raise PineconeError(
                    f"Index {self.index_name!r} does not exist. Call ensure_index() first."
                )
            self._index = self.client.Index(self.index_name)
        return self._index

    def delete_index(self) -> None:
        """Delete the whole index. Destructive; used by tooling, not routes."""
        if self.index_exists():
            self.client.delete_index(self.index_name)
            self._index = None
            logger.warning("Deleted Pinecone index %r.", self.index_name)

    def delete_namespace(self, namespace: str) -> None:
        """Delete every vector in one namespace, e.g. before a re-ingest."""
        try:
            self.index.delete(delete_all=True, namespace=namespace)
            logger.info("Cleared namespace %r.", namespace)
        except Exception as exc:
            # A namespace that was never written to is not an error.
            logger.debug("Namespace %r not cleared: %s", namespace, exc)

    # -- writes ---------------------------------------------------------- #

    def upsert_chunks(
        self,
        chunks: list[Chunk],
        vectors: list[list[float]],
        namespace: str,
        batch_size: int = UPSERT_BATCH_SIZE,
    ) -> int:
        """Upsert chunks and their vectors into a namespace.

        Chunk ids are deterministic, so re-ingesting the same document updates
        vectors in place rather than duplicating them.

        Args:
            chunks: The chunks being stored.
            vectors: Embeddings, aligned 1:1 with ``chunks``.
            namespace: Target namespace.
            batch_size: Vectors per upsert request.

        Returns:
            The number of vectors upserted.

        Raises:
            PineconeError: If the inputs are misaligned or a write fails.
        """
        if len(chunks) != len(vectors):
            raise PineconeError(
                f"Misaligned inputs: {len(chunks)} chunks but {len(vectors)} vectors."
            )
        if not chunks:
            return 0

        payload = [
            {"id": chunk.chunk_id, "values": vector, "metadata": chunk.to_metadata()}
            for chunk, vector in zip(chunks, vectors, strict=True)
        ]

        written = 0
        for start in range(0, len(payload), batch_size):
            batch = payload[start : start + batch_size]
            try:
                self.index.upsert(vectors=batch, namespace=namespace)
            except Exception as exc:
                raise PineconeError(f"Upsert failed at batch starting {start}: {exc}") from exc
            written += len(batch)
            logger.debug("Upserted %d/%d vectors.", written, len(payload))

        logger.info("Upserted %d vector(s) into %r/%r.", written, self.index_name, namespace)
        return written

    # -- reads ----------------------------------------------------------- #

    def query(
        self,
        vector: list[float],
        namespace: str,
        top_k: int | None = None,
        metadata_filter: dict[str, Any] | None = None,
        min_score: float | None = None,
    ) -> list[SearchHit]:
        """Run a similarity search.

        Args:
            vector: The query embedding.
            namespace: Namespace to search.
            top_k: Maximum hits. Defaults to ``RETRIEVAL_TOP_K``.
            metadata_filter: Pinecone filter, e.g. ``{"unit_number": {"$eq": 1}}``.
            min_score: Drop hits scoring below this. Defaults to
                ``RETRIEVAL_MIN_SCORE``.

        Returns:
            Hits ordered by descending score.
        """
        top_k = top_k or settings.RETRIEVAL_TOP_K
        min_score = settings.RETRIEVAL_MIN_SCORE if min_score is None else min_score

        try:
            response = self.index.query(
                vector=vector,
                top_k=top_k,
                namespace=namespace,
                filter=metadata_filter or None,
                include_metadata=True,
            )
        except Exception as exc:
            raise PineconeError(f"Query failed: {exc}") from exc

        hits: list[SearchHit] = []
        for match in response.get("matches", []):
            score = float(match.get("score", 0.0))
            if score < min_score:
                continue
            metadata = dict(match.get("metadata") or {})
            hits.append(
                SearchHit(
                    chunk_id=match.get("id", ""),
                    score=score,
                    text=metadata.get("text", ""),
                    metadata=metadata,
                )
            )

        logger.debug(
            "Query returned %d hit(s) above score %.2f in %r.", len(hits), min_score, namespace
        )
        return hits

    def stats(self, namespace: str | None = None) -> dict[str, Any]:
        """Return index statistics, optionally narrowed to one namespace."""
        raw = self.index.describe_index_stats()
        namespaces = raw.get("namespaces", {}) or {}

        if namespace is not None:
            return {
                "namespace": namespace,
                "vector_count": (namespaces.get(namespace) or {}).get("vector_count", 0),
                "dimension": raw.get("dimension"),
            }

        return {
            "index": self.index_name,
            "dimension": raw.get("dimension"),
            "total_vector_count": raw.get("total_vector_count", 0),
            "namespaces": {
                name: value.get("vector_count", 0) for name, value in namespaces.items()
            },
        }


__all__ = [
    "PineconeError",
    "PineconeStore",
    "SearchHit",
    "namespace_for",
]
