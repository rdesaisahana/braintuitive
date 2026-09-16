"""Accepting and ingesting a parent's curriculum PDF.

Ingestion is slow -- parse, chunk, embed every chunk, upsert to Pinecone -- so
the upload request cannot wait for it. The request validates and stores the
file, records a row, and returns; the work happens afterwards and the parent
polls the row.

Validation happens before anything is stored. A PDF is read by a parser that
takes arbitrary bytes, so "is this actually a PDF, and is it a sane size" is
answered up front rather than discovered halfway through an ingest.

**Nothing is built until the parent confirms.** Ingestion runs in two phases:

1. :func:`run_preview` parses the PDF -- seconds, no API calls -- and stops at
   ``review`` with what it found: the title, the grade, the units.
2. The parent looks and says yes (:func:`confirm`) or no (:func:`cancel`).
   Only a yes runs :func:`run_ingestion`: embedding every chunk, indexing it,
   and writing the first questions -- minutes of work and real model spend.

School sites publish a dozen similar-looking guides, and the wrong one is easy
to pick. Asking after the parse means the question has a concrete answer --
"Grade 6, six units, Unit 1: Number Fluency" -- and a wrong file costs a few
seconds instead of an ingest.
"""

from __future__ import annotations

import hashlib
import logging
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from config import UPLOAD_DIR, settings
from db.database import session_scope
from db.models import CurriculumUpload, UploadStatus

logger = logging.getLogger(__name__)

# Every PDF begins with this. Checking the bytes rather than the filename or
# the client-supplied content type, both of which the client chooses.
PDF_MAGIC = b"%PDF-"
MAX_FILENAME = 200


# Shown when a PDF parses but yields nothing -- the common failure, and the
# parent's input to fix, so it names the input rather than an exception.
NO_UNITS_MESSAGE = (
    "We could not find any units in that PDF. It should be a "
    "curriculum guide with numbered units and their objectives."
)


class UploadRejectedError(Exception):
    """The file cannot be accepted. The message is shown to the parent."""


class UploadStateError(Exception):
    """The upload is not in a state that allows this. Shown to the parent."""


def _safe_name(filename: str) -> str:
    """A filename safe to join onto a directory.

    Strips any directory component and anything that is not obviously part of a
    name, so a crafted ``../../`` cannot escape the upload directory.
    """
    base = Path(filename or "curriculum.pdf").name
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", base).strip("._") or "curriculum.pdf"
    if not cleaned.lower().endswith(".pdf"):
        cleaned += ".pdf"
    return cleaned[:MAX_FILENAME]


def validate(content: bytes, filename: str) -> None:
    """Reject anything that is not a plausibly-sized PDF.

    Raises:
        UploadRejectedError: with a message written for the parent, not the log.
    """
    if not content:
        raise UploadRejectedError("That file is empty.")

    limit = settings.MAX_UPLOAD_MB * 1024 * 1024
    if len(content) > limit:
        actual = len(content) / (1024 * 1024)
        raise UploadRejectedError(
            f"That file is {actual:.1f} MB; the limit is {settings.MAX_UPLOAD_MB} MB."
        )

    if not content.startswith(PDF_MAGIC):
        raise UploadRejectedError("That does not look like a PDF. Please upload the PDF itself.")

    if not filename.lower().endswith(".pdf"):
        raise UploadRejectedError("Please upload a file ending in .pdf.")


def store(content: bytes, filename: str, user_id: str) -> tuple[Path, str]:
    """Write the upload to disk under the owner's own directory.

    Returns:
        ``(path, content_hash)``.
    """
    digest = hashlib.sha256(content).hexdigest()
    directory = UPLOAD_DIR / user_id
    directory.mkdir(parents=True, exist_ok=True)

    # Named by hash: re-uploading the same PDF overwrites rather than
    # accumulating copies, and the name cannot collide across parents because
    # the directory is already theirs.
    path = directory / f"{digest[:16]}_{_safe_name(filename)}"
    path.write_bytes(content)
    return path, digest


def record(
    db: Session,
    user_id: str,
    filename: str,
    path: Path,
    digest: str,
    size: int,
    subject: str,
    grade_level: int | None,
) -> CurriculumUpload:
    """Create the pending upload row the parent will poll."""
    upload = CurriculumUpload(
        user_id=user_id,
        filename=_safe_name(filename),
        stored_path=str(path),
        content_hash=digest,
        size_bytes=size,
        subject=subject,
        grade_level=grade_level,
        status=UploadStatus.PENDING,
    )
    db.add(upload)
    db.flush()
    return upload


def build_preview(document: Any) -> dict[str, Any]:  # noqa: ANN401 - ParsedDocument
    """What the parent is shown before confirming.

    Enough to recognise the right guide at a glance -- its title, the grade,
    and the unit names -- and nothing that needs the model to produce.
    """
    return {
        "title": document.title,
        "grade_level": document.grade_level,
        "page_count": document.page_count,
        "total_units": len(document.units),
        "total_topics": document.total_sub_units,
        "units": [
            {
                "unit_number": unit.unit_number,
                "title": unit.title,
                "topics": len(unit.sub_units),
            }
            for unit in document.units
        ],
    }


# What the preview parsed, waiting for the parent to say yes. Reading the PDF
# is the slowest step that is not a model call, and the confirm step would
# otherwise do it all over again on the same file. Keyed by upload, with the
# inputs that produced it, so a cached copy is only ever used for its own file.
# A restart between the two steps simply empties this and the parse runs again.
_parsed_cache: dict[str, tuple[str, str, int | None, Any]] = {}


def cache_parsed(upload_id: str, path: Path, subject: str, grade_level: int | None, document: Any) -> None:
    """Keep the preview's parse for the confirm step. Holds the last few only."""
    for stale in list(_parsed_cache)[:-2]:
        _parsed_cache.pop(stale, None)
    _parsed_cache[upload_id] = (str(path), subject, grade_level, document)


def take_parsed(upload_id: str, path: Path, subject: str, grade_level: int | None) -> Any:
    """The cached parse for this exact file, or None to parse it again."""
    cached = _parsed_cache.pop(upload_id, None)
    if cached is None:
        return None
    cached_path, cached_subject, cached_grade, document = cached
    if (cached_path, cached_subject) != (str(path), subject):
        return None
    # The preview infers the grade when the form left it blank; reuse only when
    # the confirm step asks for the same grade, or for whatever was inferred.
    if grade_level is not None and grade_level != cached_grade and grade_level != getattr(document, "grade_level", None):
        return None
    return document


def run_preview(upload_id: str) -> None:
    """Read a stored upload and stop for the parent to confirm it.

    Parse only: no embedding, no indexing, no questions. This is the cheap half
    of ingestion, and it is enough to show the parent what the file actually
    is. Like :func:`run_ingestion`, every failure lands on the row, because
    nothing else is listening.
    """
    error: str | None = None
    preview: dict[str, Any] | None = None
    grade: int | None = None

    try:
        # Imported here for the same reason as in run_ingestion: an import
        # failure must still be recorded on the row, not escape the worker.
        from rag.pdf_parser import CurriculumPDFParser

        with session_scope() as db:
            upload = db.get(CurriculumUpload, upload_id)
            if upload is None:
                logger.error("Upload %s vanished before it could be read.", upload_id)
                return
            upload.status = UploadStatus.PROCESSING
            upload.started_at = datetime.now(UTC)
            path = upload.stored_path
            subject = upload.subject
            grade_level = upload.grade_level
            db.commit()

        document = CurriculumPDFParser().parse(path, subject=subject, grade_level=grade_level)
        cache_parsed(upload_id, path, subject, grade_level, document)
        if not document.units:
            error = NO_UNITS_MESSAGE
        else:
            preview = build_preview(document)
            # 0 means the cover page did not say. Left unset so ingestion infers
            # it again rather than pinning a grade nobody chose.
            grade = document.grade_level or None
    except Exception as exc:
        logger.exception("Reading upload %s failed", upload_id)
        error = f"We could not read that PDF: {exc}"

    with session_scope() as db:
        upload = db.get(CurriculumUpload, upload_id)
        if upload is None:
            return
        if error:
            upload.status = UploadStatus.FAILED
            upload.error = error
            upload.completed_at = datetime.now(UTC)
        else:
            upload.preview = preview
            if grade:
                upload.grade_level = grade
            upload.status = UploadStatus.REVIEW
        db.commit()

    logger.info("Upload %s read: %s.", upload_id, "failed" if error else "waiting for confirmation")


def confirm(db: Session, upload: CurriculumUpload) -> None:
    """The parent says it is the right curriculum. Queue it to be built.

    The caller commits and schedules :func:`run_ingestion`.

    Raises:
        UploadStateError: it is not waiting for confirmation.
    """
    if upload.status is not UploadStatus.REVIEW:
        raise UploadStateError("This upload is not waiting for confirmation.")
    upload.status = UploadStatus.PENDING
    upload.error = None
    db.flush()


def cancel(db: Session, upload: CurriculumUpload) -> None:
    """The parent says it is the wrong file. Nothing was built, so little to undo.

    The stored PDF is removed unless another upload row points at the same
    file -- files are named by content hash, so re-uploading an identical PDF
    reuses the path, and deleting it out from under that row would be wrong.

    Raises:
        UploadStateError: it is not waiting for confirmation.
    """
    if upload.status is not UploadStatus.REVIEW:
        raise UploadStateError("Only an upload waiting for your confirmation can be cancelled.")

    upload.status = UploadStatus.CANCELLED
    upload.completed_at = datetime.now(UTC)

    stored = upload.stored_path
    if stored:
        shared = (
            db.query(CurriculumUpload)
            .filter(
                CurriculumUpload.stored_path == stored,
                CurriculumUpload.id != upload.id,
            )
            .count()
        )
        if not shared:
            Path(stored).unlink(missing_ok=True)
            upload.stored_path = None
    db.flush()


def run_ingestion(upload_id: str) -> None:
    """Build a confirmed upload. Runs in the background, after the response.

    Only ever queued once the parent has confirmed the preview -- this is the
    expensive half: embedding, indexing, and writing the first questions.

    Opens its own session: the request's session is closed by the time this
    runs. Every failure is recorded on the row rather than raised, because
    nothing is listening -- an exception here would vanish into the worker
    thread and leave the parent polling a row stuck on "processing" forever.
    """
    error: str | None = None
    report = None

    try:
        # Imported here rather than at module scope: the pipeline pulls in the
        # embedding and Pinecone clients, and the API should still boot when
        # those are unconfigured. Inside the try because an ImportError here
        # would otherwise escape before the row is marked, leaving the parent
        # polling "pending" forever with nothing to explain it -- which is
        # exactly what happened the first time this ran.
        from rag.pipeline import CurriculumIngestionPipeline

        with session_scope() as db:
            upload = db.get(CurriculumUpload, upload_id)
            if upload is None:
                logger.error("Upload %s vanished before ingestion started.", upload_id)
                return
            upload.status = UploadStatus.PROCESSING
            upload.started_at = datetime.now(UTC)
            path = upload.stored_path
            subject = upload.subject
            grade_level = upload.grade_level
            user_id = upload.user_id
            db.commit()

        report = CurriculumIngestionPipeline().ingest(
            path,
            subject=subject,
            grade_level=grade_level,
            user_id=user_id,
            # The preview read this same PDF a moment ago; reading it again is
            # the longest wait in this step that is not a model call.
            document=take_parsed(upload_id, Path(path), subject, grade_level) if path else None,
        )
        if not report.units_written:
            # A PDF that parses but yields nothing is the common failure, and
            # it is the parent's problem to fix, not a crash.
            error = NO_UNITS_MESSAGE
    except Exception as exc:
        logger.exception("Ingestion failed for upload %s", upload_id)
        error = f"Ingestion failed: {exc}"

    with session_scope() as db:
        upload = db.get(CurriculumUpload, upload_id)
        if upload is None:
            return
        if report is not None:
            upload.grade_level = report.grade_level
            upload.units_written = report.units_written
            upload.sub_units_written = report.sub_units_written
            upload.chunks_created = report.chunks_created
            upload.vectors_upserted = report.vectors_upserted
            upload.warnings = list(report.warnings)
        upload.error = error
        upload.status = UploadStatus.FAILED if error else UploadStatus.COMPLETED
        upload.completed_at = datetime.now(UTC)
        db.commit()

    logger.info(
        "Upload %s finished: %s (%s unit(s)).",
        upload_id,
        "failed" if error else "completed",
        report.units_written if report else 0,
    )

    if error is None:
        _prime_bank(user_id)


def _prime_bank(user_id: str) -> None:
    """Fill the first slots of a newly uploaded curriculum.

    Runs after the upload is already marked complete, so the parent sees
    success immediately and their child can start on the live-generation path
    while this fills behind them. A failure here costs nothing but speed --
    the scheduler will reach the same slots within its interval -- so it is
    logged rather than allowed to turn a successful upload into a failed one.
    """
    try:
        from services.scheduler_jobs import prime_new_curriculum

        prime_new_curriculum(user_id)
    except Exception:
        logger.exception(
            "Priming the bank failed for %s; the scheduler will pick it up.", user_id[:8]
        )


# Reading and building happen in a background thread. If the server restarts
# mid-way -- ordinary on small hosts, which restart on every deploy -- that
# thread is gone and its row stays "processing" with nobody working on it. Past
# this age it is treated as dead, so it can never wedge a parent out of
# uploading again or deleting what they have.
ABANDONED_AFTER = timedelta(minutes=10)


def _is_abandoned(upload: CurriculumUpload, now: datetime) -> bool:
    """True if nothing has touched this half-finished upload for a long time."""
    if upload.status not in (UploadStatus.PENDING, UploadStatus.PROCESSING):
        return False
    touched = upload.started_at or upload.updated_at or upload.created_at
    if touched is None:
        return False
    if touched.tzinfo is None:
        touched = touched.replace(tzinfo=UTC)
    return now - touched > ABANDONED_AFTER


def active_upload(db: Session, user_id: str) -> CurriculumUpload | None:
    """An upload this parent has in flight, if any.

    One at a time: two concurrent ingests of different PDFs would race on the
    same ``(user, subject, grade, unit)`` rows and interleave two curricula.

    An upload waiting for confirmation counts. It has not built anything yet,
    but it is still the parent's open decision -- and counting it is what
    brings the question back if they reload the page or come back tomorrow.

    One that died half-way does not count: it is marked failed here, with a
    reason the parent can read, rather than blocking them forever.
    """
    upload = (
        db.query(CurriculumUpload)
        .filter(
            CurriculumUpload.user_id == user_id,
            CurriculumUpload.status.in_(
                [UploadStatus.PENDING, UploadStatus.PROCESSING, UploadStatus.REVIEW]
            ),
        )
        .order_by(CurriculumUpload.created_at.desc())
        .first()
    )
    if upload is not None and _is_abandoned(upload, datetime.now(UTC)):
        logger.warning("Upload %s looks abandoned; marking it failed.", upload.id)
        upload.status = UploadStatus.FAILED
        upload.error = (
            "This upload stopped part-way through, most likely because the server "
            "restarted. Nothing was kept. Please upload the file again."
        )
        upload.completed_at = datetime.now(UTC)
        db.commit()
        return None
    return upload


__all__ = [
    "NO_UNITS_MESSAGE",
    "UploadRejectedError",
    "UploadStateError",
    "active_upload",
    "build_preview",
    "cancel",
    "confirm",
    "record",
    "run_ingestion",
    "run_preview",
    "store",
    "validate",
]
