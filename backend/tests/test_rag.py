"""Phase 2 RAG tests.

Covers parsing, chunking, filter construction, store contracts and SQLite
persistence. Network-dependent steps (Nebius embeddings, live Pinecone) are
exercised through stubs so the whole suite runs offline and deterministically.

Tests marked ``live_pdf`` use the real curriculum PDF when it is present.

Run:
    cd backend
    pytest tests/test_rag.py -v
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from db.models import Base, CurriculumSubUnit, CurriculumUnit
from rag.chunking import Chunk, CurriculumChunker, count_tokens
from rag.pdf_parser import (
    CurriculumPDFParser,
    ParsedDocument,
    ParsedSubUnit,
    ParsedUnit,
)
from rag.pinecone_client import PineconeError, PineconeStore, SearchHit, namespace_for
from rag.retrieval import CurriculumRetriever, RetrievalContext

PDF_PATH = Path(__file__).resolve().parents[2] / "06 Pre-Algebra H&A.pdf"
requires_pdf = pytest.mark.skipif(not PDF_PATH.is_file(), reason="curriculum PDF not present")


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture()
def db() -> Session:
    """In-memory database with foreign keys on."""
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )

    @event.listens_for(engine, "connect")
    def _fk_on(dbapi_connection, _record) -> None:  # noqa: ANN001
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture(scope="session")
def real_doc() -> ParsedDocument:
    """The real curriculum PDF, parsed once for the whole session.

    pdfplumber takes ~14s on this document, so re-parsing per test dominated
    the suite runtime.
    """
    return CurriculumPDFParser().parse(PDF_PATH, subject="math")


@pytest.fixture()
def synthetic_doc() -> ParsedDocument:
    """A small parsed document that does not depend on the real PDF."""
    unit = ParsedUnit(
        unit_number=1,
        title="Number Fluency",
        description="Students develop understanding of rational numbers.",
        page_start=5,
        page_end=8,
        text=(
            "Summary and Rationale\n"
            "Students will develop a comprehensive understanding of real numbers. " * 12
            + "\nObjectives\nWe are learning to/that:\n"
            "● Identify, describe, and find the absolute values of integers.\n"
            "● Fluently add, subtract, multiply and divide integers.\n"
            "Evidence of Learning\nFormative assessment details."
        ),
        pacing_days=48,
        standards=["6.NS.A", "7.NS.A"],
        sub_units=[
            ParsedSubUnit(
                sub_unit_number="1.1",
                sequence=0,
                title="Identify, describe, and find the absolute values of integers",
                description="Identify, describe, and find the absolute values of integers.",
                learning_objectives=["Identify, describe, and find the absolute values."],
                skill_tags=["absolute", "values", "integers"],
            ),
            ParsedSubUnit(
                sub_unit_number="1.2",
                sequence=1,
                title="Fluently add, subtract, multiply and divide integers",
                description="Fluently add, subtract, multiply and divide integers.",
                learning_objectives=["Fluently add, subtract, multiply and divide integers."],
                skill_tags=["fluently", "integers"],
            ),
        ],
    )
    return ParsedDocument(
        filename="synthetic.pdf",
        content_hash="deadbeef",
        title="Pre-Algebra H&A",
        subject="math",
        grade_level=6,
        page_count=8,
        pages=["cover"] * 8,
        units=[unit],
    )


# --------------------------------------------------------------------------- #
# Parser
# --------------------------------------------------------------------------- #


def test_unit_header_must_start_a_page() -> None:
    """The contents page lists 'Unit 1 Unit 2 Unit 3' inline; that is not a unit.

    Matching it would invent six phantom units on page 3.
    """
    parser = CurriculumPDFParser()
    pages = [
        "Cover Page\nGrade 6",
        "Pre-Algebra H&A\nUnit 1 Unit 2 Unit 3 Unit 4\nNumber Fluency Expressions",
        "Unit 1: Number Fluency\nSummary and Rationale\nreal content here",
        "more unit 1 content",
    ]
    boundaries = parser.find_unit_boundaries(pages)
    assert len(boundaries) == 1
    assert boundaries[0][:3] == (1, "Number Fluency", 3)
    assert boundaries[0][3] == 4, "unit should run to the last page"


def test_objectives_become_numbered_sub_units() -> None:
    parser = CurriculumPDFParser()
    text = (
        "Objectives\nWe are learning to/that:\n"
        "● Identify, describe, and find the absolute values of integers.\n"
        "● Fluently add, subtract, multiply and divide integers.\n"
        "Evidence of Learning\n"
    )
    subs = parser.parse_sub_units(1, text)
    assert [s.sub_unit_number for s in subs] == ["1.1", "1.2"]
    assert subs[0].sequence == 0
    assert "absolute" in subs[0].skill_tags


def test_duplicate_objectives_are_collapsed() -> None:
    """Unit 6 of the real PDF lists one objective twice, verbatim."""
    parser = CurriculumPDFParser()
    text = (
        "Objectives\nWe are learning to/that:\n"
        "● Write an equation in two variables.\n"
        "● Calculate the slope of a line.\n"
        "● Write an equation in two variables.\n"
        "Evidence of Learning\n"
    )
    subs = parser.parse_sub_units(6, text)
    assert len(subs) == 2, "the repeated objective must not become a second sub-unit"
    assert [s.sub_unit_number for s in subs] == ["6.1", "6.2"]


def test_missing_objectives_block_is_not_fatal() -> None:
    parser = CurriculumPDFParser()
    assert parser.parse_sub_units(3, "Summary and Rationale\nNo objectives here.") == []


def test_sub_unit_title_drops_parentheticals() -> None:
    parser = CurriculumPDFParser()
    text = (
        "Objectives\nWe are learning to/that:\n"
        "● Find GCF and LCM using a variety of strategies. "
        "(Suggested Strategies: Listing, Boot/Ladder)\n"
        "Evidence of Learning\n"
    )
    sub = parser.parse_sub_units(1, text)[0]
    assert "Suggested" not in sub.title
    assert "Suggested" in sub.description, "full text is preserved in the description"


def test_missing_pdf_raises() -> None:
    with pytest.raises(FileNotFoundError):
        CurriculumPDFParser().extract_pages("does-not-exist.pdf")


@requires_pdf
def test_real_pdf_structure(real_doc: ParsedDocument) -> None:
    """End-to-end parse of the actual curriculum document."""
    doc = real_doc

    assert doc.page_count == 23
    assert doc.grade_level == 6, "grade should be inferred from the cover page"
    assert doc.title == "Pre-Algebra H&A"
    assert [u.unit_number for u in doc.units] == [1, 2, 3, 4, 5, 6]
    assert doc.total_sub_units == 47

    unit_one = doc.units[0]
    assert unit_one.title == "Number Fluency"
    assert unit_one.pacing_days == 48
    assert unit_one.page_start == 5
    assert "6.NS.A" in unit_one.standards
    assert unit_one.sub_units[1].sub_unit_number == "1.2"
    assert "integers" in unit_one.sub_units[1].description.lower()


@requires_pdf
def test_every_unit_yields_sub_units(real_doc: ParsedDocument) -> None:
    for unit in real_doc.units:
        assert unit.sub_units, f"Unit {unit.unit_number} produced no sub-units"


@requires_pdf
def test_parse_is_deterministic(real_doc: ParsedDocument) -> None:
    """Same input, same content hash -- re-ingest must be idempotent."""
    assert CurriculumPDFParser().parse(PDF_PATH).content_hash == real_doc.content_hash


# --------------------------------------------------------------------------- #
# Chunking
# --------------------------------------------------------------------------- #


def test_chunker_rejects_overlap_larger_than_size() -> None:
    with pytest.raises(ValueError, match="overlap"):
        CurriculumChunker(chunk_size=100, overlap=100)


def test_one_objective_chunk_per_sub_unit(synthetic_doc: ParsedDocument) -> None:
    chunks = CurriculumChunker().chunk_document(synthetic_doc)
    objectives = [c for c in chunks if c.chunk_type == "objective"]
    assert len(objectives) == 2
    assert {c.sub_unit_number for c in objectives} == {"1.1", "1.2"}


def test_objective_chunk_carries_unit_context(synthetic_doc: ParsedDocument) -> None:
    """A bare verb phrase would match any unit; unit context disambiguates."""
    chunk = next(
        c for c in CurriculumChunker().chunk_document(synthetic_doc) if c.sub_unit_number == "1.2"
    )
    assert "Unit 1: Number Fluency" in chunk.text
    assert "Grade 6 math" in chunk.text


def test_chunks_respect_token_limit(synthetic_doc: ParsedDocument) -> None:
    chunker = CurriculumChunker(chunk_size=120, overlap=20)
    for chunk in chunker.chunk_document(synthetic_doc):
        assert chunk.token_count <= 120, f"{chunk.chunk_id} exceeded the limit"


def test_token_windows_overlap() -> None:
    """Consecutive windows must share text, or boundary facts get orphaned."""
    chunker = CurriculumChunker(chunk_size=60, overlap=20)
    text = " ".join(f"word{n}" for n in range(400))
    windows = chunker.split_by_tokens(text)
    assert len(windows) > 1
    first_tail = set(windows[0].split()[-10:])
    second_head = set(windows[1].split()[:20])
    assert first_tail & second_head, "no overlap between consecutive windows"


def test_chunk_ids_are_deterministic(synthetic_doc: ParsedDocument) -> None:
    """Re-ingesting must upsert in place rather than duplicate vectors."""
    first = CurriculumChunker().chunk_document(synthetic_doc)
    second = CurriculumChunker().chunk_document(synthetic_doc)
    assert [c.chunk_id for c in first] == [c.chunk_id for c in second]


def test_chunk_ids_are_unique(synthetic_doc: ParsedDocument) -> None:
    ids = [c.chunk_id for c in CurriculumChunker().chunk_document(synthetic_doc)]
    assert len(ids) == len(set(ids))


def test_sections_are_not_merged(synthetic_doc: ParsedDocument) -> None:
    """A chunk must not span 'Objectives' and 'Evidence of Learning'."""
    prose = [
        c for c in CurriculumChunker().chunk_document(synthetic_doc) if c.chunk_type == "prose"
    ]
    assert prose
    sections = {c.section for c in prose}
    assert "Summary and Rationale" in sections
    for chunk in prose:
        assert not ("Evidence of Learning" in chunk.text and "Summary and" in chunk.text)


def test_metadata_is_pinecone_safe(synthetic_doc: ParsedDocument) -> None:
    """Pinecone accepts only str, int, float, bool and list[str] -- never None."""
    for chunk in CurriculumChunker().chunk_document(synthetic_doc):
        for key, value in chunk.to_metadata().items():
            assert value is not None, f"{key} is None"
            assert isinstance(value, str | int | float | bool | list), f"{key} is {type(value)}"
            if isinstance(value, list):
                assert all(isinstance(item, str) for item in value), key


def test_count_tokens_scales_with_length() -> None:
    assert count_tokens("") <= 1
    assert count_tokens("hello world") < count_tokens("hello world " * 50)


# --------------------------------------------------------------------------- #
# Vector store contracts
# --------------------------------------------------------------------------- #


def test_namespace_isolates_subject_and_grade() -> None:
    assert namespace_for("math", 6) == "math-grade6"
    assert namespace_for("Math", 7) == "math-grade7"
    assert namespace_for("math", 6) != namespace_for("math", 7)


def test_upsert_rejects_misaligned_inputs(synthetic_doc: ParsedDocument) -> None:
    chunks = CurriculumChunker().chunk_document(synthetic_doc)
    with pytest.raises(PineconeError, match="Misaligned"):
        PineconeStore().upsert_chunks(chunks, [[0.1, 0.2]], namespace="math-grade6")


def test_upsert_of_nothing_is_a_noop() -> None:
    assert PineconeStore().upsert_chunks([], [], namespace="math-grade6") == 0


def test_search_hit_citation_is_traceable() -> None:
    hit = SearchHit(
        chunk_id="u1s1-abc",
        score=0.87,
        text="Fluently add integers.",
        metadata={
            "unit_number": 1,
            "sub_unit_number": "1.2",
            "page_start": 5,
            "page_end": 8,
        },
    )
    assert hit.citation == "Unit 1 / 1.2, pp.5-8"
    assert hit.unit_number == 1
    assert hit.sub_unit_number == "1.2"


def test_citation_coerces_pinecone_floats() -> None:
    """Pinecone returns every number as a float; citations must not show 1.0."""
    hit = SearchHit(
        chunk_id="c",
        score=0.5,
        text="",
        metadata={
            "unit_number": 1.0,
            "sub_unit_number": "1.2",
            "page_start": 5.0,
            "page_end": 8.0,
        },
    )
    assert hit.citation == "Unit 1 / 1.2, pp.5-8"
    assert hit.unit_number == 1


def test_citation_without_sub_unit_or_pages() -> None:
    assert SearchHit("c", 0.5, "", {"unit_number": 3.0}).citation == "Unit 3"
    assert SearchHit("c", 0.5, "", {}).citation == "Unit ?"


# --------------------------------------------------------------------------- #
# Retrieval
# --------------------------------------------------------------------------- #


def test_filter_construction() -> None:
    build = CurriculumRetriever.build_filter
    assert build() == {}
    assert build(unit_number=1) == {"unit_number": {"$eq": 1}}
    assert build(unit_number=2, sub_unit_number="2.3") == {
        "unit_number": {"$eq": 2},
        "sub_unit_number": {"$eq": "2.3"},
    }
    assert build(chunk_type="prose")["chunk_type"] == {"$eq": "prose"}


def test_empty_context_is_handled() -> None:
    context = RetrievalContext(query="absolute value")
    assert context.is_empty
    assert context.chunk_ids == []
    assert "No curriculum content" in context.to_prompt_block()


def test_prompt_block_cites_sources() -> None:
    context = RetrievalContext(
        query="integers",
        hits=[
            SearchHit(
                chunk_id="c1",
                score=0.91,
                text="Add and subtract integers.",
                metadata={
                    "unit_number": 1,
                    "sub_unit_number": "1.2",
                    "page_start": 5,
                    "page_end": 8,
                },
            )
        ],
    )
    block = context.to_prompt_block()
    assert "[1]" in block
    assert "Unit 1 / 1.2" in block
    assert "0.91" in block
    assert context.chunk_ids == ["c1"]


def test_prompt_block_respects_char_budget() -> None:
    hits = [
        SearchHit(chunk_id=f"c{n}", score=0.9, text="x" * 500, metadata={"unit_number": 1})
        for n in range(20)
    ]
    block = RetrievalContext(query="q", hits=hits).to_prompt_block(max_chars=1200)
    assert len(block) < 2000


def test_retrieval_failure_returns_empty_not_raise() -> None:
    """A dead vector store must not take down quiz generation."""

    class BrokenEmbedder:
        def embed_query(self, _query: str) -> list[float]:
            raise RuntimeError("nebius is down")

    retriever = CurriculumRetriever(store=PineconeStore(), embedder=BrokenEmbedder())
    context = retriever.search("integers", subject="math", grade_level=6)
    assert context.is_empty
    assert context.namespace == "math-grade6"


# --------------------------------------------------------------------------- #
# Pipeline persistence
# --------------------------------------------------------------------------- #


def _write(db: Session, document: ParsedDocument) -> tuple[int, int]:
    from rag.pipeline import CurriculumIngestionPipeline

    return CurriculumIngestionPipeline()._write_sql(db, document)


def test_sql_write_creates_units_and_sub_units(db: Session, synthetic_doc: ParsedDocument) -> None:
    units, subs = _write(db, synthetic_doc)
    db.commit()

    assert (units, subs) == (1, 2)
    unit = db.query(CurriculumUnit).one()
    assert unit.unit_number == 1
    assert unit.total_sub_units == 2
    assert unit.source_document == "synthetic.pdf"
    assert db.query(CurriculumSubUnit).count() == 2

    sub = db.query(CurriculumSubUnit).filter_by(sub_unit_number="1.2").one()
    assert sub.vector_namespace == "math-grade6"
    assert sub.learning_objectives  # JSON round-trip


def test_reingest_updates_in_place(db: Session, synthetic_doc: ParsedDocument) -> None:
    """Re-ingesting must not orphan student progress pointing at these rows."""
    _write(db, synthetic_doc)
    db.commit()
    original_unit_id = db.query(CurriculumUnit).one().id
    original_sub_id = db.query(CurriculumSubUnit).filter_by(sub_unit_number="1.1").one().id

    synthetic_doc.units[0].title = "Number Fluency (revised)"
    _write(db, synthetic_doc)
    db.commit()

    assert db.query(CurriculumUnit).count() == 1, "re-ingest duplicated the unit"
    assert db.query(CurriculumSubUnit).count() == 2
    unit = db.query(CurriculumUnit).one()
    assert unit.id == original_unit_id, "unit id changed; foreign keys would break"
    assert unit.title == "Number Fluency (revised)"
    assert db.query(CurriculumSubUnit).filter_by(sub_unit_number="1.1").one().id == original_sub_id


def test_different_grades_do_not_collide(db: Session, synthetic_doc: ParsedDocument) -> None:
    _write(db, synthetic_doc)
    synthetic_doc.grade_level = 7
    _write(db, synthetic_doc)
    db.commit()
    assert db.query(CurriculumUnit).count() == 2


def test_mark_indexed_sets_chunk_counts(db: Session, synthetic_doc: ParsedDocument) -> None:
    from rag.pipeline import CurriculumIngestionPipeline

    pipeline = CurriculumIngestionPipeline()
    pipeline._write_sql(db, synthetic_doc)
    chunks = CurriculumChunker().chunk_document(synthetic_doc)
    pipeline._mark_indexed(db, synthetic_doc, chunks)
    db.commit()

    for sub in db.query(CurriculumSubUnit).all():
        assert sub.is_indexed is True
        assert sub.chunk_count >= 1


@requires_pdf
def test_dry_run_touches_nothing(db: Session) -> None:
    from rag.pipeline import CurriculumIngestionPipeline

    report = CurriculumIngestionPipeline().ingest(PDF_PATH, dry_run=True)

    assert report.dry_run is True
    assert report.units_parsed == 6
    assert report.sub_units_parsed == 47
    assert report.chunks_created > 0
    assert report.namespace == "math-grade6"
    # Nothing was embedded, indexed or written.
    assert report.tokens_embedded == 0
    assert report.vectors_upserted == 0
    assert report.units_written == 0


def test_report_summary_renders(synthetic_doc: ParsedDocument) -> None:
    from rag.pipeline import IngestionReport

    report = IngestionReport(
        filename="x.pdf",
        content_hash="abc",
        subject="math",
        grade_level=6,
        warnings=["something to check"],
    )
    text = report.summary()
    assert "x.pdf" in text
    assert "something to check" in text


# --------------------------------------------------------------------------- #
# Stubbed end-to-end
# --------------------------------------------------------------------------- #


class StubEmbedder:
    """Deterministic fake embedder: no network, stable vectors."""

    model = "stub"
    dimensions = 8

    def _vector(self, text: str) -> list[float]:
        seed = sum(ord(ch) for ch in text)
        return [((seed >> n) % 97) / 97.0 for n in range(self.dimensions)]

    def embed_texts(self, texts: list[str]):  # noqa: ANN201
        from rag.embeddings import EmbeddingResult

        return EmbeddingResult(
            vectors=[self._vector(t) for t in texts],
            model=self.model,
            dimensions=self.dimensions,
            total_tokens=sum(len(t.split()) for t in texts),
        )

    def embed_chunks(self, chunks: list[Chunk]):  # noqa: ANN201
        return self.embed_texts([c.text for c in chunks])

    def embed_query(self, query: str) -> list[float]:
        return self._vector(query)


def test_stubbed_embed_aligns_with_chunks(synthetic_doc: ParsedDocument) -> None:
    """Vectors must stay 1:1 with chunks all the way to upsert."""
    chunks = CurriculumChunker().chunk_document(synthetic_doc)
    result = StubEmbedder().embed_chunks(chunks)

    assert len(result) == len(chunks)
    assert all(len(v) == 8 for v in result.vectors)

    payload = [
        {"id": c.chunk_id, "values": v, "metadata": c.to_metadata()}
        for c, v in zip(chunks, result.vectors, strict=True)
    ]
    assert len(payload) == len(chunks)
    assert payload[0]["metadata"]["unit_number"] == 1
