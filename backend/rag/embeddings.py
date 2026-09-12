"""Embedding generation via Nebius AI Studio.

Nebius speaks the OpenAI wire protocol, so the official ``openai`` client
pointed at ``NEBIUS_BASE_URL`` is the supported path.

Two things this module insists on:

* **Dimension truth.** The configured ``EMBEDDING_DIMENSIONS`` is a claim, not
  a fact. :meth:`NebiusEmbedder.probe_dimensions` asks the API what size it
  actually returns, and :func:`verify_embedding_config` fails loudly on a
  mismatch -- a wrong dimension does not error at embed time, it errors much
  later at Pinecone upsert, or worse, silently degrades retrieval.
* **Batching with retry.** Embedding calls are the slowest, most rate-limited
  step of ingest, so requests are batched and retried with exponential
  backoff.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from config import settings

if TYPE_CHECKING:
    from rag.chunking import Chunk

logger = logging.getLogger(__name__)

# Embedding endpoints accept many inputs per call; too many risks a payload
# limit, too few wastes round trips.
DEFAULT_BATCH_SIZE = 32


class EmbeddingError(RuntimeError):
    """Raised when embeddings cannot be produced."""


@dataclass
class EmbeddingResult:
    """Vectors plus the accounting needed to reason about cost and cost limits."""

    vectors: list[list[float]]
    model: str
    dimensions: int
    total_tokens: int = 0

    def __len__(self) -> int:
        return len(self.vectors)


class NebiusEmbedder:
    """Batched embedding client for Nebius AI Studio.

    Example:
        embedder = NebiusEmbedder()
        result = embedder.embed_texts(["What is -3 + 8?"])
        print(result.dimensions)
    """

    def __init__(
        self,
        model: str | None = None,
        batch_size: int = DEFAULT_BATCH_SIZE,
    ) -> None:
        self.model = model or settings.NEBIUS_EMBEDDING_MODEL
        self.batch_size = batch_size
        self._client = None
        self._async_client = None
        self._dimensions: int | None = None

    # -- clients --------------------------------------------------------- #

    def _require_key(self) -> None:
        if not settings.nebius_configured:
            raise EmbeddingError(
                "NEBIUS_API_KEY is not set - cannot generate embeddings. "
                "Add a valid Nebius AI Studio key to .env."
            )

    @property
    def client(self):  # noqa: ANN201 - openai.OpenAI, imported lazily
        """Lazily built synchronous OpenAI-compatible client."""
        if self._client is None:
            self._require_key()
            from openai import OpenAI

            self._client = OpenAI(
                api_key=settings.NEBIUS_API_KEY,
                base_url=settings.NEBIUS_BASE_URL,
                timeout=settings.LLM_REQUEST_TIMEOUT,
                max_retries=0,  # tenacity owns retry policy
            )
        return self._client

    @property
    def async_client(self):  # noqa: ANN201 - openai.AsyncOpenAI, imported lazily
        """Lazily built asynchronous client."""
        if self._async_client is None:
            self._require_key()
            from openai import AsyncOpenAI

            self._async_client = AsyncOpenAI(
                api_key=settings.NEBIUS_API_KEY,
                base_url=settings.NEBIUS_BASE_URL,
                timeout=settings.LLM_REQUEST_TIMEOUT,
                max_retries=0,
            )
        return self._async_client

    # -- core calls ------------------------------------------------------ #

    @retry(
        stop=stop_after_attempt(4),
        wait=wait_exponential(multiplier=1.5, min=2, max=30),
        retry=retry_if_exception_type(Exception),
        reraise=True,
    )
    def _embed_batch(self, batch: list[str]) -> tuple[list[list[float]], int]:
        """Embed one batch, retrying transient failures with backoff."""
        response = self.client.embeddings.create(model=self.model, input=batch)
        vectors = [item.embedding for item in response.data]
        used = getattr(response, "usage", None)
        return vectors, int(getattr(used, "total_tokens", 0) or 0)

    def embed_texts(self, texts: list[str]) -> EmbeddingResult:
        """Embed a list of texts, batching automatically.

        Args:
            texts: Non-empty strings to embed.

        Returns:
            An :class:`EmbeddingResult` whose ``vectors`` align 1:1 with
            ``texts``.

        Raises:
            EmbeddingError: If the API key is missing or the call fails.
        """
        if not texts:
            return EmbeddingResult(vectors=[], model=self.model, dimensions=0)

        cleaned = [text.strip() or " " for text in texts]
        vectors: list[list[float]] = []
        total_tokens = 0

        for start in range(0, len(cleaned), self.batch_size):
            batch = cleaned[start : start + self.batch_size]
            try:
                batch_vectors, tokens = self._embed_batch(batch)
            except Exception as exc:
                raise EmbeddingError(
                    f"Embedding failed for batch {start // self.batch_size + 1} "
                    f"({len(batch)} texts) using model {self.model!r}: {exc}"
                ) from exc

            vectors.extend(batch_vectors)
            total_tokens += tokens
            logger.debug("Embedded %d/%d texts.", len(vectors), len(cleaned))

        dimensions = len(vectors[0]) if vectors else 0
        self._dimensions = dimensions
        logger.info(
            "Embedded %d text(s) -> %d dims, %d tokens (model=%s).",
            len(vectors),
            dimensions,
            total_tokens,
            self.model,
        )
        return EmbeddingResult(
            vectors=vectors,
            model=self.model,
            dimensions=dimensions,
            total_tokens=total_tokens,
        )

    def embed_query(self, query: str) -> list[float]:
        """Embed a single search query and return its vector."""
        return self.embed_texts([query]).vectors[0]

    def embed_chunks(self, chunks: list[Chunk]) -> EmbeddingResult:
        """Embed the text of parsed :class:`~rag.chunking.Chunk` objects."""
        return self.embed_texts([chunk.text for chunk in chunks])

    async def aembed_texts(self, texts: list[str]) -> EmbeddingResult:
        """Async counterpart to :meth:`embed_texts`."""
        if not texts:
            return EmbeddingResult(vectors=[], model=self.model, dimensions=0)

        cleaned = [text.strip() or " " for text in texts]
        vectors: list[list[float]] = []
        total_tokens = 0

        for start in range(0, len(cleaned), self.batch_size):
            batch = cleaned[start : start + self.batch_size]
            try:
                response = await self.async_client.embeddings.create(model=self.model, input=batch)
            except Exception as exc:
                raise EmbeddingError(f"Async embedding failed: {exc}") from exc
            vectors.extend(item.embedding for item in response.data)
            used = getattr(response, "usage", None)
            total_tokens += int(getattr(used, "total_tokens", 0) or 0)

        dimensions = len(vectors[0]) if vectors else 0
        self._dimensions = dimensions
        return EmbeddingResult(
            vectors=vectors,
            model=self.model,
            dimensions=dimensions,
            total_tokens=total_tokens,
        )

    # -- introspection --------------------------------------------------- #

    def probe_dimensions(self) -> int:
        """Ask the API what dimension this model actually returns.

        Cached for the lifetime of the embedder.
        """
        if self._dimensions is None:
            self._dimensions = len(self.embed_query("dimension probe"))
        return self._dimensions


def verify_embedding_config(embedder: NebiusEmbedder | None = None) -> int:
    """Check ``EMBEDDING_DIMENSIONS`` against the live model.

    Call before creating a Pinecone index. A mismatch here is cheap to fix; the
    same mismatch discovered after indexing means re-embedding everything.

    Returns:
        The true dimension reported by the API.

    Raises:
        EmbeddingError: If configuration and reality disagree.
    """
    embedder = embedder or NebiusEmbedder()
    actual = embedder.probe_dimensions()
    configured = settings.EMBEDDING_DIMENSIONS

    if actual != configured:
        raise EmbeddingError(
            f"EMBEDDING_DIMENSIONS is {configured} but model "
            f"{embedder.model!r} returns {actual}. Set EMBEDDING_DIMENSIONS={actual} "
            "in .env before creating the Pinecone index."
        )

    logger.info("Embedding config verified: %s -> %d dims.", embedder.model, actual)
    return actual


__all__ = [
    "EmbeddingError",
    "EmbeddingResult",
    "NebiusEmbedder",
    "verify_embedding_config",
]
