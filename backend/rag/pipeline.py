"""End-to-end curriculum ingestion.

    PDF -> parse -> chunk -> embed -> Pinecone
                  \\-> curriculum_units / curriculum_sub_units (SQLite)
                  \\-> curriculum_chunks / parsed_documents (MongoDB, optional)

The three stores hold different things on purpose. SQLite holds the unit and
sub-unit rows that progress tracking has foreign keys into. Pinecone holds the
vectors. MongoDB holds the full chunk documents for inspection and re-indexing
without re-parsing the PDF. Only SQLite and Pinecone are required.

Run from the command line (any working directory):
    cd backend && python -m rag.pipeline "../06 Pre-Algebra H&A.pdf" --grade 6
    cd backend && python rag/pipeline.py "../06 Pre-Algebra H&A.pdf" --dry-run
"""

from __future__ import annotations

import sys
from pathlib import Path

# Put backend/ on sys.path so this runs as a plain script too, not only via
# `python -m rag.pipeline` from backend/.
_BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import argparse  # noqa: E402
import asyncio  # noqa: E402
import logging  # noqa: E402
from dataclasses import dataclass, field  # noqa: E402
from typing import Any  # noqa: E402

from sqlalchemy.orm import Session  # noqa: E402

from config import configure_logging, settings  # noqa: E402
from db.database import (  # noqa: E402
    COLLECTION_CURRICULUM_CHUNKS,
    COLLECTION_PARSED_DOCUMENTS,
    connect_to_mongo,
    get_mongo_collection,
    session_scope,
)
from db.models import CurriculumSubUnit, CurriculumUnit  # noqa: E402
from rag.chunking import Chunk, CurriculumChunker  # noqa: E402
from rag.embeddings import NebiusEmbedder, verify_embedding_config  # noqa: E402
from rag.pdf_parser import CurriculumPDFParser, ParsedDocument  # noqa: E402
from rag.pinecone_client import PineconeStore, namespace_for  # noqa: E402

logger = logging.getLogger(__name__)


@dataclass
class IngestionReport:
    """What one ingest run actually did."""

    filename: str
    content_hash: str
    subject: str
    grade_level: int
    namespace: str = ""
    pages: int = 0
    units_parsed: int = 0
    sub_units_parsed: int = 0
    chunks_created: int = 0
    tokens_embedded: int = 0
    vectors_upserted: int = 0
    units_written: int = 0
    sub_units_written: int = 0
    mongo_documents: int = 0
    dry_run: bool = False
    warnings: list[str] = field(default_factory=list)

    def summary(self) -> str:
        mode = "DRY RUN" if self.dry_run else "INGESTED"
        return (
            f"[{mode}] {self.filename}\n"
            f"  grade/subject : {self.subject} grade {self.grade_level} "
            f"(namespace {self.namespace or 'n/a'})\n"
            f"  parsed        : {self.pages} pages, {self.units_parsed} units, "
            f"{self.sub_units_parsed} sub-units\n"
            f"  chunks        : {self.chunks_created}\n"
            f"  embedded      : {self.tokens_embedded} tokens\n"
            f"  pinecone      : {self.vectors_upserted} vectors\n"
            f"  sqlite        : {self.units_written} units, "
            f"{self.sub_units_written} sub-units\n"
            f"  mongodb       : {self.mongo_documents} documents\n"
            + ("".join(f"  warning       : {w}\n" for w in self.warnings))
        )

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


class CurriculumIngestionPipeline:
    """Parses, chunks, embeds and indexes a curriculum PDF.

    Example:
        pipeline = CurriculumIngestionPipeline()
        report = pipeline.ingest("06 Pre-Algebra H&A.pdf", grade_level=6)
        print(report.summary())
    """

    def __init__(
        self,
        parser: CurriculumPDFParser | None = None,
        chunker: CurriculumChunker | None = None,
        embedder: NebiusEmbedder | None = None,
        store: PineconeStore | None = None,
    ) -> None:
        self.parser = parser or CurriculumPDFParser()
        self.chunker = chunker or CurriculumChunker()
        self.embedder = embedder or NebiusEmbedder()
        self.store = store or PineconeStore()

    # ------------------------------------------------------------------ #
    # Persistence
    # ------------------------------------------------------------------ #

    def _write_sql(
        self, db: Session, document: ParsedDocument, user_id: str | None = None
    ) -> tuple[int, int]:
        """Upsert units and sub-units into SQLite.

        Matching is by ``(user_id, subject, grade_level, unit_number)`` and
        ``(unit_id, sub_unit_number)``, so re-ingesting an updated PDF edits
        the existing rows instead of orphaning student progress that points at
        them.

        The owner is part of the match, not merely a column written afterwards.
        Without it, a parent uploading their own grade-6 guide would silently
        overwrite the shared sample -- and every other family's dashboard with
        it.

        Args:
            db: Active session.
            document: The parsed curriculum.
            user_id: Owner of this curriculum, or None for the shared sample.

        Returns:
            ``(units_written, sub_units_written)``.
        """
        units_written = 0
        sub_units_written = 0

        for parsed_unit in document.units:
            unit = (
                db.query(CurriculumUnit)
                .filter_by(
                    user_id=user_id,
                    subject=document.subject,
                    grade_level=document.grade_level,
                    unit_number=parsed_unit.unit_number,
                )
                .one_or_none()
            )
            if unit is None:
                unit = CurriculumUnit(
                    user_id=user_id,
                    subject=document.subject,
                    grade_level=document.grade_level,
                    unit_number=parsed_unit.unit_number,
                )
                db.add(unit)

            unit.title = parsed_unit.title
            unit.description = parsed_unit.description
            unit.source_document = document.filename
            unit.source_page_start = parsed_unit.page_start
            unit.source_page_end = parsed_unit.page_end
            unit.total_sub_units = len(parsed_unit.sub_units)
            unit.is_active = True
            db.flush()  # assign unit.id before children reference it
            units_written += 1

            for parsed_sub in parsed_unit.sub_units:
                sub_unit = (
                    db.query(CurriculumSubUnit)
                    .filter_by(unit_id=unit.id, sub_unit_number=parsed_sub.sub_unit_number)
                    .one_or_none()
                )
                if sub_unit is None:
                    sub_unit = CurriculumSubUnit(
                        unit_id=unit.id, sub_unit_number=parsed_sub.sub_unit_number
                    )
                    db.add(sub_unit)

                sub_unit.sequence = parsed_sub.sequence
                sub_unit.title = parsed_sub.title
                sub_unit.description = parsed_sub.description
                sub_unit.learning_objectives = parsed_sub.learning_objectives
                # skill_tags is deliberately NOT written here. The column now
                # holds the controlled skill vocabulary, derived from the
                # objective by services.skill_taxonomy. The parser's keyword
                # extraction is far too crude for that -- it produced tags
                # like "suggested", "boot" and "ladder", leaked out of a
                # parenthetical -- and seeding them here would stop the real
                # vocabulary ever being derived.
                sub_unit.source_page_start = parsed_unit.page_start
                sub_unit.source_page_end = parsed_unit.page_end
                sub_unit.vector_namespace = namespace_for(
                    document.subject, document.grade_level, user_id
                )
                sub_unit.is_active = True
                sub_units_written += 1

            db.flush()

        return units_written, sub_units_written

    @staticmethod
    def _mark_indexed(
        db: Session,
        document: ParsedDocument,
        chunks: list[Chunk],
        user_id: str | None = None,
    ) -> None:
        """Record chunk counts on sub-units once their vectors are live.

        Scoped to the owner for the same reason the upsert is: matching on
        subject and grade alone would mark another family's sub-units as
        indexed against vectors describing a different curriculum entirely.
        """
        counts: dict[str, int] = {}
        for chunk in chunks:
            if chunk.sub_unit_number:
                counts[chunk.sub_unit_number] = counts.get(chunk.sub_unit_number, 0) + 1

        unit_ids = [
            unit.id
            for unit in db.query(CurriculumUnit).filter_by(
                user_id=user_id,
                subject=document.subject,
                grade_level=document.grade_level,
            )
        ]
        if not unit_ids:
            return

        for sub_unit in (
            db.query(CurriculumSubUnit).filter(CurriculumSubUnit.unit_id.in_(unit_ids)).all()
        ):
            count = counts.get(sub_unit.sub_unit_number, 0)
            sub_unit.chunk_count = count
            sub_unit.is_indexed = count > 0

    async def _write_mongo(self, document: ParsedDocument, chunks: list[Chunk]) -> int:
        """Store chunk documents and document metadata in MongoDB.

        Returns the number of chunk documents written; 0 when Mongo is absent.
        """
        chunk_collection = get_mongo_collection(COLLECTION_CURRICULUM_CHUNKS)
        if chunk_collection is None:
            return 0

        for chunk in chunks:
            await chunk_collection.replace_one(
                {"chunk_id": chunk.chunk_id}, chunk.to_document(), upsert=True
            )

        document_collection = get_mongo_collection(COLLECTION_PARSED_DOCUMENTS)
        if document_collection is not None:
            await document_collection.replace_one(
                {"content_hash": document.content_hash}, document.to_dict(), upsert=True
            )

        return len(chunks)

    # ------------------------------------------------------------------ #
    # Orchestration
    # ------------------------------------------------------------------ #

    def ingest(
        self,
        pdf_path: str | Path,
        subject: str = "math",
        grade_level: int | None = None,
        dry_run: bool = False,
        replace: bool = False,
        user_id: str | None = None,
    ) -> IngestionReport:
        """Run the full pipeline.

        Args:
            pdf_path: Curriculum PDF to ingest.
            subject: Subject label.
            grade_level: Grade; inferred from the PDF when omitted.
            dry_run: Parse and chunk only -- no embedding, no writes. Use this
                to check unit/sub-unit detection before spending API calls.
            replace: Clear the namespace before upserting.
            user_id: Owner of the resulting curriculum. None means the shared
                sample every family sees until they upload their own.

        Returns:
            An :class:`IngestionReport`.
        """
        # --- 1. parse -----------------------------------------------------
        document = self.parser.parse(pdf_path, subject=subject, grade_level=grade_level)
        report = IngestionReport(
            filename=document.filename,
            content_hash=document.content_hash,
            subject=document.subject,
            grade_level=document.grade_level,
            pages=document.page_count,
            units_parsed=len(document.units),
            sub_units_parsed=document.total_sub_units,
            dry_run=dry_run,
        )

        if not document.units:
            report.warnings.append("No units detected - nothing to index.")
            return report

        for unit in document.units:
            if not unit.sub_units:
                report.warnings.append(
                    f"Unit {unit.unit_number} ({unit.title}) produced no sub-units."
                )

        # --- 2. chunk -----------------------------------------------------
        chunks = self.chunker.chunk_document(document)
        report.chunks_created = len(chunks)
        report.namespace = namespace_for(document.subject, document.grade_level, user_id)

        if dry_run:
            logger.info("Dry run: stopping before embedding and writes.")
            return report

        # --- 3. embed -----------------------------------------------------
        # Verify the configured dimension against the live model before
        # touching Pinecone; a mismatch found here costs nothing to fix.
        dimensions = verify_embedding_config(self.embedder)
        embeddings = self.embedder.embed_chunks(chunks)
        report.tokens_embedded = embeddings.total_tokens

        # --- 4. index -----------------------------------------------------
        self.store.ensure_index(dimensions=dimensions)
        if replace:
            self.store.delete_namespace(report.namespace)
        report.vectors_upserted = self.store.upsert_chunks(
            chunks, embeddings.vectors, namespace=report.namespace
        )

        # --- 5. persist ---------------------------------------------------
        with session_scope() as db:
            units, sub_units = self._write_sql(db, document, user_id=user_id)
            self._mark_indexed(db, document, chunks, user_id=user_id)
            report.units_written = units
            report.sub_units_written = sub_units

        try:
            report.mongo_documents = asyncio.run(self._write_mongo(document, chunks))
        except Exception as exc:
            report.warnings.append(f"MongoDB write skipped: {exc}")

        logger.info("Ingestion complete for %s.", document.filename)
        return report


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


async def _connect_mongo_if_configured() -> None:
    if settings.mongodb_configured:
        await connect_to_mongo()


def main(argv: list[str] | None = None) -> int:
    """Command-line entry point for curriculum ingestion."""
    parser = argparse.ArgumentParser(description="Ingest a curriculum PDF into Braintuitive.")
    parser.add_argument("pdf", help="Path to the curriculum PDF")
    parser.add_argument("--subject", default="math", help="Subject label (default: math)")
    parser.add_argument("--grade", type=int, default=None, help="Grade level (inferred if omitted)")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Parse and chunk only; no embedding, no writes",
    )
    parser.add_argument(
        "--replace",
        action="store_true",
        help="Clear the namespace before upserting",
    )
    args = parser.parse_args(argv)

    configure_logging()

    if settings.mongodb_configured:
        asyncio.run(_connect_mongo_if_configured())

    pipeline = CurriculumIngestionPipeline()
    try:
        report = pipeline.ingest(
            args.pdf,
            subject=args.subject,
            grade_level=args.grade,
            dry_run=args.dry_run,
            replace=args.replace,
        )
    except Exception as exc:
        logger.error("Ingestion failed: %s", exc)
        return 1

    print()
    print(report.summary())
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI
    sys.exit(main())
