"""Semantic retrieval over indexed curriculum.

This is the interface the Phase 3 agents call. The Quiz Generator asks for
context on sub-unit 1.2 and gets back curriculum text plus the chunk ids that
text came from -- those ids are written to ``QuizQuestion.source_chunk_ids``,
which is what makes "grounded, not hallucinated" an auditable claim rather
than a marketing line.

Retrieval is deliberately scoped:

* ``namespace`` isolates subject + grade.
* ``unit_number`` narrows to the unit being studied.
* ``sub_unit_number`` pins to the specific objective when one is given.

Scoping matters pedagogically. A grade 6 student working on "absolute value"
must not be handed Unit 6 slope material because it happened to embed nearby,
so filters are applied at the vector store, not after the fact.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from config import settings
from rag.embeddings import NebiusEmbedder
from rag.pinecone_client import PineconeStore, SearchHit, namespace_for

logger = logging.getLogger(__name__)


@dataclass
class RetrievalContext:
    """Retrieved curriculum, ready to be dropped into an agent prompt."""

    query: str
    hits: list[SearchHit] = field(default_factory=list)
    namespace: str = ""
    filters: dict[str, Any] = field(default_factory=dict)

    @property
    def is_empty(self) -> bool:
        return not self.hits

    @property
    def chunk_ids(self) -> list[str]:
        """Ids to persist on generated questions for provenance."""
        return [hit.chunk_id for hit in self.hits]

    def to_prompt_block(self, max_chars: int = 6000) -> str:
        """Render the hits as a numbered, cited block for an LLM prompt.

        Citations are included so the model can attribute claims, and so a
        human reviewing a generated question can find the source page.
        """
        if self.is_empty:
            return "No curriculum content was retrieved for this query."

        parts: list[str] = []
        used = 0
        for position, hit in enumerate(self.hits, start=1):
            entry = f"[{position}] ({hit.citation}, relevance {hit.score:.2f})\n{hit.text}"
            if used + len(entry) > max_chars:
                break
            parts.append(entry)
            used += len(entry)
        return "\n\n".join(parts)

    def to_dict(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "namespace": self.namespace,
            "filters": self.filters,
            "hit_count": len(self.hits),
            "chunk_ids": self.chunk_ids,
            "top_score": self.hits[0].score if self.hits else 0.0,
        }


class CurriculumRetriever:
    """Semantic search over indexed curriculum, with metadata scoping.

    Example:
        retriever = CurriculumRetriever()
        context = retriever.for_sub_unit(
            subject="math", grade_level=6, unit_number=1, sub_unit_number="1.2"
        )
        prompt_text = context.to_prompt_block()
    """

    def __init__(
        self,
        store: PineconeStore | None = None,
        embedder: NebiusEmbedder | None = None,
    ) -> None:
        self.store = store or PineconeStore()
        self.embedder = embedder or NebiusEmbedder()

    # -- filter construction --------------------------------------------- #

    @staticmethod
    def build_filter(
        unit_number: int | None = None,
        sub_unit_number: str | None = None,
        chunk_type: str | None = None,
        sections: list[str] | None = None,
    ) -> dict[str, Any]:
        """Build a Pinecone metadata filter from optional constraints."""
        conditions: dict[str, Any] = {}
        if unit_number is not None:
            conditions["unit_number"] = {"$eq": int(unit_number)}
        if sub_unit_number is not None:
            conditions["sub_unit_number"] = {"$eq": sub_unit_number}
        if chunk_type is not None:
            conditions["chunk_type"] = {"$eq": chunk_type}
        if sections:
            conditions["section"] = {"$in": sections}
        return conditions

    # -- core search ------------------------------------------------------ #

    def search(
        self,
        query: str,
        subject: str,
        grade_level: int,
        unit_number: int | None = None,
        sub_unit_number: str | None = None,
        chunk_type: str | None = None,
        top_k: int | None = None,
        min_score: float | None = None,
        namespace: str | None = None,
    ) -> RetrievalContext:
        """Search the curriculum for text relevant to ``query``.

        Returns:
            A :class:`RetrievalContext`. An empty context is a valid result --
            callers must handle "nothing relevant indexed" rather than assume
            hits exist.
        """
        # An explicit namespace wins: a sub-unit records the namespace it was
        # indexed into (``CurriculumSubUnit.vector_namespace``), and recomputing
        # it from subject and grade alone would send an uploaded curriculum's
        # search to the shared namespace and quietly return the wrong material.
        namespace = namespace or namespace_for(subject, grade_level)
        metadata_filter = self.build_filter(unit_number, sub_unit_number, chunk_type)

        try:
            vector = self.embedder.embed_query(query)
            hits = self.store.query(
                vector=vector,
                namespace=namespace,
                top_k=top_k,
                metadata_filter=metadata_filter,
                min_score=min_score,
            )
        except Exception as exc:
            # Retrieval failure must not take down quiz generation; the caller
            # sees an empty context and can degrade or surface an error.
            logger.error("Retrieval failed for %r in %s: %s", query[:80], namespace, exc)
            return RetrievalContext(query=query, namespace=namespace, filters=metadata_filter)

        logger.info(
            "Retrieved %d chunk(s) for %r (ns=%s, filters=%s).",
            len(hits),
            query[:60],
            namespace,
            metadata_filter or "none",
        )
        return RetrievalContext(
            query=query, hits=hits, namespace=namespace, filters=metadata_filter
        )

    # -- task-shaped helpers ---------------------------------------------- #

    def for_sub_unit(
        self,
        subject: str,
        grade_level: int,
        unit_number: int,
        sub_unit_number: str,
        objective: str | None = None,
        top_k: int | None = None,
        namespace: str | None = None,
    ) -> RetrievalContext:
        """Gather grounding context for one sub-unit.

        Runs two scoped searches and merges them: the objective chunk itself
        (pinned to the sub-unit) plus supporting prose from the same unit.
        Without the second search a quiz would be written from a single
        one-sentence objective, which is too thin to build ten questions on.
        """
        query = objective or f"Unit {unit_number} sub-unit {sub_unit_number} learning objective"
        top_k = top_k or settings.RETRIEVAL_TOP_K

        pinned = self.search(
            query=query,
            subject=subject,
            grade_level=grade_level,
            unit_number=unit_number,
            sub_unit_number=sub_unit_number,
            top_k=2,
            namespace=namespace,
        )
        supporting = self.search(
            query=query,
            subject=subject,
            grade_level=grade_level,
            unit_number=unit_number,
            chunk_type="prose",
            top_k=top_k,
            namespace=namespace,
        )

        merged: list[SearchHit] = []
        seen: set[str] = set()
        for hit in [*pinned.hits, *supporting.hits]:
            if hit.chunk_id in seen:
                continue
            seen.add(hit.chunk_id)
            merged.append(hit)

        merged.sort(key=lambda h: h.score, reverse=True)
        return RetrievalContext(
            query=query,
            hits=merged[: top_k + 2],
            namespace=pinned.namespace,
            filters={"unit_number": unit_number, "sub_unit_number": sub_unit_number},
        )

    def for_unit(
        self,
        subject: str,
        grade_level: int,
        unit_number: int,
        query: str | None = None,
        top_k: int | None = None,
        namespace: str | None = None,
    ) -> RetrievalContext:
        """Gather context for a whole unit (test prep, unit-level summaries)."""
        return self.search(
            query=query or f"Unit {unit_number} overview and objectives",
            subject=subject,
            grade_level=grade_level,
            unit_number=unit_number,
            top_k=top_k,
            namespace=namespace,
        )

    def for_skill(
        self,
        subject: str,
        grade_level: int,
        skill: str,
        top_k: int | None = None,
    ) -> RetrievalContext:
        """Find curriculum covering a skill, across units.

        Used by the Gap Detector: when a student keeps missing "absolute
        value" questions, this locates every place the curriculum teaches it,
        including units they have already passed.
        """
        return self.search(query=skill, subject=subject, grade_level=grade_level, top_k=top_k)


__all__ = ["CurriculumRetriever", "RetrievalContext"]
