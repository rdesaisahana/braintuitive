"""Token-aware chunking of parsed curriculum text.

Chunks are the unit of retrieval, so their boundaries decide what a quiz
question can be grounded in. Two kinds are produced:

``objective`` chunks
    One per sub-unit: the objective sentence plus its unit context. Small,
    highly specific, and filterable by ``sub_unit_number`` -- this is what the
    Quiz Generator retrieves when writing questions for sub-unit 1.2.

``prose`` chunks
    The unit's narrative sections (summary, enduring understandings,
    essential questions, standards) split on token count. These supply the
    surrounding context and vocabulary that make questions read like the
    curriculum rather than like a generic maths worksheet.

Splitting is section-aware: a chunk never straddles two document sections, so
a question is never grounded in a fragment that merges "Essential Questions"
with "Evidence of Learning".
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

from config import settings
from rag.pdf_parser import ParsedDocument, ParsedUnit

logger = logging.getLogger(__name__)

# Section headers used to segment unit prose before token splitting.
SECTION_HEADERS = (
    "Summary and Rationale",
    "Recommended Pacing",
    "New Jersey Student Learning Standards",
    "Standards for Mathematical Practice",
    "Instructional Focus",
    "Unit Enduring Understandings",
    "Unit Essential Questions",
    "Content Questions",
    "Objectives",
    "Evidence of Learning",
    "Formative Assessment",
    "Summative Assessment",
    "Resources",
    "Interdisciplinary Connections",
    "Career Readiness",
)

_SECTION_RE = re.compile(
    r"^\s*(" + "|".join(re.escape(h) for h in SECTION_HEADERS) + r")\s*$",
    re.MULTILINE | re.IGNORECASE,
)


# --------------------------------------------------------------------------- #
# Tokenisation
# --------------------------------------------------------------------------- #


@lru_cache(maxsize=1)
def _encoder() -> Any | None:
    """Return a cached tiktoken encoder, or None if unavailable.

    tiktoken fetches its BPE table on first use. If there is no network and
    nothing cached, chunking falls back to a word-count approximation rather
    than failing the whole ingest.
    """
    try:
        import tiktoken

        return tiktoken.get_encoding("cl100k_base")
    except Exception as exc:  # pragma: no cover - offline / download failure
        logger.warning("tiktoken unavailable (%s); using word-count approximation.", exc)
        return None


def count_tokens(text: str) -> int:
    """Count tokens in ``text``, approximating when tiktoken is unavailable."""
    encoder = _encoder()
    if encoder is not None:
        return len(encoder.encode(text))
    # English prose averages ~0.75 words per token.
    return int(len(text.split()) / 0.75) + 1


# --------------------------------------------------------------------------- #
# Data structure
# --------------------------------------------------------------------------- #


@dataclass
class Chunk:
    """One embeddable span of curriculum text plus its filter metadata."""

    chunk_id: str
    text: str
    chunk_type: str  # "objective" | "prose"
    source_document: str
    subject: str
    grade_level: int
    unit_number: int
    unit_title: str
    section: str = ""
    sub_unit_number: str | None = None
    sub_unit_title: str | None = None
    page_start: int = 0
    page_end: int = 0
    token_count: int = 0
    standards: list[str] = field(default_factory=list)
    skill_tags: list[str] = field(default_factory=list)

    def to_metadata(self) -> dict[str, Any]:
        """Flatten to Pinecone metadata.

        Pinecone accepts only str, int, float, bool and list[str], so None is
        dropped and numbers are coerced. The full text rides along so that
        retrieval does not need a second round trip to Mongo.
        """
        metadata: dict[str, Any] = {
            "text": self.text,
            "chunk_type": self.chunk_type,
            "source_document": self.source_document,
            "subject": self.subject,
            "grade_level": int(self.grade_level),
            "unit_number": int(self.unit_number),
            "unit_title": self.unit_title,
            "section": self.section,
            "page_start": int(self.page_start),
            "page_end": int(self.page_end),
            "token_count": int(self.token_count),
        }
        if self.sub_unit_number:
            metadata["sub_unit_number"] = self.sub_unit_number
        if self.sub_unit_title:
            metadata["sub_unit_title"] = self.sub_unit_title
        if self.standards:
            metadata["standards"] = self.standards
        if self.skill_tags:
            metadata["skill_tags"] = self.skill_tags
        return metadata

    def to_document(self) -> dict[str, Any]:
        """Full record for MongoDB storage."""
        return {"chunk_id": self.chunk_id, **self.to_metadata()}


# --------------------------------------------------------------------------- #
# Chunker
# --------------------------------------------------------------------------- #


class CurriculumChunker:
    """Splits a :class:`ParsedDocument` into embeddable chunks.

    Args:
        chunk_size: Target tokens per prose chunk.
        overlap: Tokens repeated between consecutive prose chunks, so a fact
            sitting on a boundary appears whole in at least one chunk.
    """

    def __init__(self, chunk_size: int | None = None, overlap: int | None = None) -> None:
        self.chunk_size = chunk_size or settings.CHUNK_SIZE_TOKENS
        self.overlap = overlap if overlap is not None else settings.CHUNK_OVERLAP_TOKENS
        if self.overlap >= self.chunk_size:
            raise ValueError(
                f"overlap ({self.overlap}) must be smaller than chunk_size ({self.chunk_size})"
            )

    # -- helpers --------------------------------------------------------- #

    @staticmethod
    def _make_id(*parts: str) -> str:
        """Deterministic chunk id, so re-ingesting a document upserts in place."""
        digest = hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:16]
        return f"{parts[0]}-{digest}"

    def _split_sections(self, text: str) -> list[tuple[str, str]]:
        """Split unit text into ``(section_name, body)`` pairs."""
        matches = list(_SECTION_RE.finditer(text))
        if not matches:
            return [("", text)]

        sections: list[tuple[str, str]] = []
        preamble = text[: matches[0].start()].strip()
        if preamble:
            sections.append(("Overview", preamble))

        for index, match in enumerate(matches):
            name = match.group(1).strip()
            start = match.end()
            end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
            body = text[start:end].strip()
            if body:
                sections.append((name, body))
        return sections

    def split_by_tokens(self, text: str) -> list[str]:
        """Split text into overlapping windows of ``chunk_size`` tokens.

        Falls back to sentence-aware word splitting when tiktoken is absent.
        """
        encoder = _encoder()
        if encoder is None:
            return self._split_by_words(text)

        tokens = encoder.encode(text)
        if len(tokens) <= self.chunk_size:
            return [text] if text.strip() else []

        step = self.chunk_size - self.overlap
        windows: list[str] = []
        for start in range(0, len(tokens), step):
            window = tokens[start : start + self.chunk_size]
            if not window:
                break
            decoded = encoder.decode(window).strip()
            if decoded:
                windows.append(decoded)
            if start + self.chunk_size >= len(tokens):
                break
        return windows

    def _split_by_words(self, text: str) -> list[str]:
        """Approximate splitter used only when tiktoken is unavailable."""
        words = text.split()
        words_per_chunk = int(self.chunk_size * 0.75)
        words_overlap = int(self.overlap * 0.75)
        if len(words) <= words_per_chunk:
            return [text] if text.strip() else []

        step = max(1, words_per_chunk - words_overlap)
        return [
            " ".join(words[start : start + words_per_chunk])
            for start in range(0, len(words), step)
            if words[start : start + words_per_chunk]
        ]

    # -- chunk builders -------------------------------------------------- #

    def chunk_unit(self, document: ParsedDocument, unit: ParsedUnit) -> list[Chunk]:
        """Produce objective and prose chunks for one unit."""
        chunks: list[Chunk] = []
        common = {
            "source_document": document.filename,
            "subject": document.subject,
            "grade_level": document.grade_level,
            "unit_number": unit.unit_number,
            "unit_title": unit.title,
            "page_start": unit.page_start,
            "page_end": unit.page_end,
            "standards": unit.standards,
        }

        # --- objective chunks: one per sub-unit --------------------------
        for sub_unit in unit.sub_units:
            # Embed the objective together with its unit context so the vector
            # captures "integers, in Unit 1 Number Fluency", not a bare verb
            # phrase that would match any unit.
            body = (
                f"Unit {unit.unit_number}: {unit.title}\n"
                f"Sub-unit {sub_unit.sub_unit_number}: {sub_unit.title}\n"
                f"Learning objective: {sub_unit.description}\n"
                f"Grade {document.grade_level} {document.subject}."
            )
            chunks.append(
                Chunk(
                    chunk_id=self._make_id(
                        f"u{unit.unit_number}s{sub_unit.sequence}", sub_unit.description
                    ),
                    text=body,
                    chunk_type="objective",
                    section="Objectives",
                    sub_unit_number=sub_unit.sub_unit_number,
                    sub_unit_title=sub_unit.title,
                    skill_tags=sub_unit.skill_tags,
                    token_count=count_tokens(body),
                    **common,
                )
            )

        # --- prose chunks: section-aware, token-windowed ------------------
        for section_name, body in self._split_sections(unit.text):
            for position, window in enumerate(self.split_by_tokens(body)):
                if count_tokens(window) < 20:  # drop scraps
                    continue
                chunks.append(
                    Chunk(
                        chunk_id=self._make_id(
                            f"u{unit.unit_number}p{position}", section_name, window[:80]
                        ),
                        text=window,
                        chunk_type="prose",
                        section=section_name,
                        token_count=count_tokens(window),
                        **common,
                    )
                )

        return chunks

    def chunk_document(self, document: ParsedDocument) -> list[Chunk]:
        """Chunk every unit in a parsed document."""
        chunks: list[Chunk] = []
        for unit in document.units:
            chunks.extend(self.chunk_unit(document, unit))

        objectives = sum(1 for c in chunks if c.chunk_type == "objective")
        tokens = sum(c.token_count for c in chunks)
        logger.info(
            "Chunked %s into %d chunk(s): %d objective, %d prose, %d tokens total.",
            document.filename,
            len(chunks),
            objectives,
            len(chunks) - objectives,
            tokens,
        )
        return chunks


__all__ = ["Chunk", "CurriculumChunker", "count_tokens"]
