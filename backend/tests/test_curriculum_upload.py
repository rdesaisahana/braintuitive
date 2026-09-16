"""Curriculum upload and ownership tests.

Curriculum used to be global: one operator-loaded set that every family shared.
Making it uploadable per parent introduces a class of bug worse than any it
fixes -- one family seeing, or overwriting, another's material. So most of what
is tested here is isolation, not the happy path.

The properties that matter:

* two families can each own a grade-6 Unit 1, and neither can see the other's;
* a parent with no upload still sees the shared sample, rather than nothing;
* an upload replaces the sample for that family rather than merging with it;
* deleting an account takes its curriculum with it;
* the file is validated before it is stored anywhere.

Ingestion itself is stubbed -- it costs embedding calls and minutes.

Run:
    cd backend
    pytest tests/test_curriculum_upload.py -v
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from db.models import (
    Base,
    CurriculumSubUnit,
    CurriculumUnit,
    CurriculumUpload,
    Student,
    UploadStatus,
    User,
)
from rag.pinecone_client import namespace_for
from services.curriculum_scope import curriculum_owner, has_own_curriculum, visible_units
from services.curriculum_upload import UploadRejectedError, _safe_name, active_upload, validate

PDF_BYTES = b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n1 0 obj\n<<>>\nendobj\ntrailer\n%%EOF\n"


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture()
def db() -> Session:
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


def make_curriculum(
    db: Session, owner: User | None, title: str, units: int = 2, grade: int = 6
) -> list[CurriculumUnit]:
    created: list[CurriculumUnit] = []
    for number in range(1, units + 1):
        unit = CurriculumUnit(
            user_id=owner.id if owner else None,
            unit_number=number,
            title=f"{title} Unit {number}",
            subject="math",
            grade_level=grade,
        )
        db.add(unit)
        db.flush()
        db.add(
            CurriculumSubUnit(
                unit=unit,
                sub_unit_number=f"{number}.1",
                sequence=0,
                title=f"{title} {number}.1",
                is_indexed=True,
            )
        )
        created.append(unit)
    db.commit()
    return created


@pytest.fixture()
def world(db: Session) -> dict[str, Any]:
    """A shared sample, plus two families -- one with their own curriculum."""
    make_curriculum(db, None, "Sample")

    alice = User(email="alice@example.com", hashed_password="x", full_name="Alice")
    bob = User(email="bob@example.com", hashed_password="x", full_name="Bob")
    db.add_all([alice, bob])
    db.flush()

    alice_child = Student(parent=alice, first_name="Ana", grade_level=6)
    bob_child = Student(parent=bob, first_name="Ben", grade_level=6)
    db.add_all([alice_child, bob_child])
    db.commit()

    make_curriculum(db, alice, "Alice's District")
    return {"alice": alice, "bob": bob, "ana": alice_child, "ben": bob_child}


# --------------------------------------------------------------------------- #
# Isolation
# --------------------------------------------------------------------------- #


def test_two_families_can_each_own_a_grade_six_unit_one(db: Session, world: dict[str, Any]) -> None:
    """The reason the identity constraint had to widen.

    Their Unit 1s are different curricula that happen to share a number.
    """
    make_curriculum(db, world["bob"], "Bob's District")

    ones = db.query(CurriculumUnit).filter_by(unit_number=1, grade_level=6).all()
    owners = {unit.user_id for unit in ones}
    assert len(ones) == 3, "sample + two families"
    assert owners == {None, world["alice"].id, world["bob"].id}


def test_a_child_sees_only_their_own_familys_curriculum(db: Session, world: dict[str, Any]) -> None:
    make_curriculum(db, world["bob"], "Bob's District")

    for child, expected in ((world["ana"], "Alice's District"), (world["ben"], "Bob's District")):
        titles = [unit.title for unit in visible_units(db, child)]
        assert all(expected in title for title in titles), (child.first_name, titles)


def test_a_family_without_an_upload_sees_nothing(db: Session, world: dict[str, Any]) -> None:
    """No fallback, on purpose.

    An earlier version showed the shared sample until a family uploaded, so a
    new account was never an empty screen. But those questions came from one
    district's guide, and serving them to a child whose parent never chose it
    is a quiet lie about what the child is being taught. The empty list is the
    honest answer, and the app renders it as "upload to get started".
    """
    assert visible_units(db, world["ben"]) == []
    assert curriculum_owner(db, world["ben"]) == world["bob"].id


def test_a_sample_curriculum_is_no_longer_special(db: Session, world: dict[str, Any]) -> None:
    """An unowned curriculum is invisible to everyone, not a default."""
    unowned = db.query(CurriculumUnit).filter_by(user_id=None).all()
    assert unowned, "the fixture still creates one"
    for child in (world["ana"], world["ben"]):
        assert not any(unit.user_id is None for unit in visible_units(db, child))


def test_an_upload_replaces_the_sample_rather_than_adding_to_it(
    db: Session, world: dict[str, Any]
) -> None:
    """Two interleaved Unit 3s would break sequential progression, which
    assumes one ordered course."""
    titles = [unit.title for unit in visible_units(db, world["ana"])]
    assert not any("Sample" in title for title in titles)
    assert curriculum_owner(db, world["ana"]) == world["alice"].id


def test_curriculum_follows_the_grade_not_just_the_owner(
    db: Session, world: dict[str, Any]
) -> None:
    """A grade-7 upload must not appear on a grade-6 child's dashboard."""
    make_curriculum(db, world["bob"], "Bob's Grade 7", grade=7)

    titles = [unit.title for unit in visible_units(db, world["ben"])]
    assert all("Sample" in title for title in titles), titles


def test_deleting_a_parent_takes_their_curriculum(db: Session, world: dict[str, Any]) -> None:
    before = db.query(CurriculumUnit).count()
    db.delete(world["alice"])
    db.commit()

    remaining = db.query(CurriculumUnit).all()
    assert len(remaining) < before
    assert all(unit.user_id is None for unit in remaining), "the sample survives"


def test_has_own_curriculum_is_grade_agnostic(db: Session, world: dict[str, Any]) -> None:
    """It answers "should we still be asking them to upload?" -- and a parent
    who uploaded a grade-7 guide has clearly found the screen."""
    assert has_own_curriculum(db, world["alice"].id) is True
    assert has_own_curriculum(db, world["bob"].id) is False

    make_curriculum(db, world["bob"], "Bob's Grade 8", grade=8)
    assert has_own_curriculum(db, world["bob"].id) is True


# --------------------------------------------------------------------------- #
# Vector namespaces
# --------------------------------------------------------------------------- #


def test_the_shared_namespace_is_unchanged() -> None:
    """Vectors indexed before ownership existed must stay reachable."""
    assert namespace_for("math", 6) == "math-grade6"
    assert namespace_for("MATH", 6, None) == "math-grade6"


def test_each_owner_gets_their_own_namespace() -> None:
    """Isolating by namespace rather than a metadata filter means a forgotten
    filter cannot surface another family's material."""
    alice = namespace_for("math", 6, "11111111-2222-3333-4444-555555555555")
    bob = namespace_for("math", 6, "99999999-8888-7777-6666-555555555555")

    assert alice != bob
    assert alice.startswith("math-grade6-u")
    assert bob.startswith("math-grade6-u")
    assert namespace_for("math", 6) not in (alice, bob)


# --------------------------------------------------------------------------- #
# Validation, before anything is written
# --------------------------------------------------------------------------- #


def test_a_real_pdf_is_accepted() -> None:
    validate(PDF_BYTES, "curriculum.pdf")


def test_a_renamed_non_pdf_is_rejected() -> None:
    """Checked by magic bytes, not by the name or content type -- both of
    which the client chooses."""
    with pytest.raises(UploadRejectedError, match="does not look like a PDF"):
        validate(b"PK\x03\x04 this is a zip", "curriculum.pdf")


def test_an_empty_file_is_rejected() -> None:
    with pytest.raises(UploadRejectedError, match="empty"):
        validate(b"", "curriculum.pdf")


def test_an_oversized_file_is_rejected() -> None:
    from config import settings

    too_big = PDF_BYTES + b"\x00" * (settings.MAX_UPLOAD_MB * 1024 * 1024)
    with pytest.raises(UploadRejectedError, match="limit is"):
        validate(too_big, "curriculum.pdf")


def test_a_non_pdf_extension_is_rejected() -> None:
    with pytest.raises(UploadRejectedError, match=r"\.pdf"):
        validate(PDF_BYTES, "curriculum.docx")


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("../../etc/passwd.pdf", "etcpasswd.pdf"),
        ("..\\..\\windows\\system.pdf", "windowssystem.pdf"),
        ("my curriculum (2026).pdf", "my_curriculum_2026_.pdf"),
        ("", "curriculum.pdf"),
    ],
)
def test_filenames_cannot_escape_the_upload_directory(given: str, expected: str) -> None:
    """A crafted name must not be able to write outside the parent's own
    directory."""
    safe = _safe_name(given)
    assert "/" not in safe and "\\" not in safe
    assert not safe.startswith("..")
    assert safe.endswith(".pdf")


def test_a_safe_name_keeps_something_readable() -> None:
    assert _safe_name("Grade 6 Pre-Algebra.pdf") == "Grade_6_Pre-Algebra.pdf"


def test_a_very_long_filename_is_truncated() -> None:
    assert len(_safe_name("x" * 500 + ".pdf")) <= 200


# --------------------------------------------------------------------------- #
# One ingestion at a time
# --------------------------------------------------------------------------- #


def make_upload(db: Session, user: User, status: UploadStatus) -> CurriculumUpload:
    upload = CurriculumUpload(user_id=user.id, filename="guide.pdf", subject="math", status=status)
    db.add(upload)
    db.commit()
    return upload


def test_an_upload_abandoned_mid_way_stops_blocking(
    db: Session, world: dict[str, Any]
) -> None:
    """A server restart kills the thread doing the work and leaves the row on
    "processing" with nobody working on it. Before this, that wedged the parent
    out of uploading again *and* out of deleting what they had, forever."""
    from datetime import UTC, datetime, timedelta

    upload = make_upload(db, world["alice"], UploadStatus.PROCESSING)
    upload.started_at = datetime.now(UTC) - timedelta(minutes=30)
    db.commit()

    assert active_upload(db, world["alice"].id) is None
    db.refresh(upload)
    assert upload.status is UploadStatus.FAILED
    assert "restarted" in (upload.error or ""), "the parent is told why, and what to do"


def test_an_upload_still_being_worked_on_keeps_blocking(
    db: Session, world: dict[str, Any]
) -> None:
    """The point of the rule above is the dead ones only; a live ingest must
    still stop a second one starting underneath it."""
    from datetime import UTC, datetime, timedelta

    upload = make_upload(db, world["alice"], UploadStatus.PROCESSING)
    upload.started_at = datetime.now(UTC) - timedelta(minutes=1)
    db.commit()

    assert active_upload(db, world["alice"].id) is not None


@pytest.mark.parametrize("status", [UploadStatus.PENDING, UploadStatus.PROCESSING])
def test_an_unfinished_upload_is_reported_as_active(
    db: Session, world: dict[str, Any], status: UploadStatus
) -> None:
    """Two concurrent ingests would race on the same rows and interleave two
    curricula into one."""
    make_upload(db, world["alice"], status)
    assert active_upload(db, world["alice"].id) is not None


@pytest.mark.parametrize("status", [UploadStatus.COMPLETED, UploadStatus.FAILED])
def test_a_finished_upload_does_not_block_the_next(
    db: Session, world: dict[str, Any], status: UploadStatus
) -> None:
    make_upload(db, world["alice"], status)
    assert active_upload(db, world["alice"].id) is None


def test_one_familys_upload_does_not_block_another(db: Session, world: dict[str, Any]) -> None:
    make_upload(db, world["alice"], UploadStatus.PROCESSING)
    assert active_upload(db, world["bob"].id) is None


def test_deleting_a_parent_takes_their_upload_records(db: Session, world: dict[str, Any]) -> None:
    make_upload(db, world["alice"], UploadStatus.COMPLETED)
    db.delete(world["alice"])
    db.commit()
    assert db.query(CurriculumUpload).count() == 0


# --------------------------------------------------------------------------- #
# A failed ingestion must always say so
# --------------------------------------------------------------------------- #


def test_a_broken_ingestion_records_the_failure(
    db: Session, world: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The row must never be left on "pending" with nothing to explain it.

    This is not hypothetical: the first live run raised an ImportError before
    the row was ever marked, and the parent polled a spinner forever with no
    error anywhere. Anything that can go wrong in the background has to land on
    the row, because nothing else is listening.
    """
    import services.curriculum_upload as module

    upload = make_upload(db, world["alice"], UploadStatus.PENDING)
    monkeypatch.setattr(module, "session_scope", lambda: _FixedScope(db))
    monkeypatch.setitem(
        __import__("sys").modules,
        "rag.pipeline",
        _BrokenPipelineModule(),
    )

    module.run_ingestion(upload.id)

    db.refresh(upload)
    assert upload.status is UploadStatus.FAILED
    assert upload.error and "boom" in upload.error
    assert upload.completed_at is not None


class _BrokenPipelineModule:
    """Stands in for rag.pipeline and fails the way a real outage would."""

    class CurriculumIngestionPipeline:  # noqa: N801 - mirrors the real name
        def ingest(self, *args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
            raise RuntimeError("boom")


class _FixedScope:
    """Hand the module the test's session instead of opening a real one."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def __enter__(self) -> Session:
        return self.session

    def __exit__(self, *exc: object) -> None:
        return None


def test_a_pdf_with_no_units_is_a_parent_facing_error(
    db: Session, world: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A PDF that parses but yields nothing is the common failure, and it is
    the parent's input to fix -- so the message names the input, not a class."""
    import services.curriculum_upload as module

    class _EmptyReport:
        grade_level = 6
        units_written = 0
        sub_units_written = 0
        chunks_created = 4
        vectors_upserted = 4
        warnings: list[str] = []

    class _EmptyPipelineModule:
        class CurriculumIngestionPipeline:  # noqa: N801
            def ingest(self, *args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
                return _EmptyReport()

    upload = make_upload(db, world["alice"], UploadStatus.PENDING)
    monkeypatch.setattr(module, "session_scope", lambda: _FixedScope(db))
    monkeypatch.setitem(__import__("sys").modules, "rag.pipeline", _EmptyPipelineModule())

    module.run_ingestion(upload.id)

    db.refresh(upload)
    assert upload.status is UploadStatus.FAILED
    assert "could not find any units" in (upload.error or "")
    assert "Exception" not in (upload.error or ""), "the message is for a parent"


# --------------------------------------------------------------------------- #
# Reading before building
# --------------------------------------------------------------------------- #


class _FakeUnit:
    def __init__(self, number: int, title: str, topics: int) -> None:
        self.unit_number = number
        self.title = title
        self.sub_units = [object()] * topics


class _FakeDocument:
    def __init__(self, units: list[_FakeUnit], grade: int = 6) -> None:
        self.units = units
        self.title = "Grade 6 Pre-Algebra"
        self.grade_level = grade
        self.page_count = 40

    @property
    def total_sub_units(self) -> int:
        return sum(len(unit.sub_units) for unit in self.units)


def _parser_module(document: Any = None, error: Exception | None = None) -> Any:  # noqa: ANN401
    """Stands in for rag.pdf_parser."""

    class _Parser:
        def parse(self, *args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
            if error is not None:
                raise error
            return document

    class _Module:
        CurriculumPDFParser = _Parser

    return _Module()


class _PipelineMustNotRun:
    """If reading a PDF ever reaches for the build pipeline, fail loudly."""

    class CurriculumIngestionPipeline:  # noqa: N801 - mirrors the real name
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            raise AssertionError("reading a PDF must not start the expensive build")


def _read(
    db: Session,
    world: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    document: Any = None,  # noqa: ANN401
    error: Exception | None = None,
) -> CurriculumUpload:
    import services.curriculum_upload as module

    upload = make_upload(db, world["alice"], UploadStatus.PENDING)
    monkeypatch.setattr(module, "session_scope", lambda: _FixedScope(db))
    monkeypatch.setitem(
        __import__("sys").modules, "rag.pdf_parser", _parser_module(document, error)
    )
    monkeypatch.setitem(__import__("sys").modules, "rag.pipeline", _PipelineMustNotRun())

    module.run_preview(upload.id)
    db.refresh(upload)
    return upload


def test_reading_stops_for_review_and_builds_nothing(
    db: Session, world: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cheap half only. No units are written until the parent says yes."""
    from db.models import CurriculumUnit as Unit

    # The fixture already gives alice a curriculum of her own, so the claim is
    # that reading *adds* nothing -- not that she owns nothing.
    owned_before = db.query(Unit).filter_by(user_id=world["alice"].id).count()

    document = _FakeDocument([_FakeUnit(1, "Number Fluency", 3), _FakeUnit(2, "Expressions", 2)])
    upload = _read(db, world, monkeypatch, document)

    assert upload.status is UploadStatus.REVIEW
    assert upload.preview["total_units"] == 2
    assert upload.preview["total_topics"] == 5
    assert [unit["title"] for unit in upload.preview["units"]] == ["Number Fluency", "Expressions"]
    assert upload.grade_level == 6
    assert db.query(Unit).filter_by(user_id=world["alice"].id).count() == owned_before


def test_a_pdf_with_no_units_fails_at_the_read(
    db: Session, world: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Caught before the parent is asked anything -- there is nothing to confirm."""
    upload = _read(db, world, monkeypatch, _FakeDocument([]))

    assert upload.status is UploadStatus.FAILED
    assert "could not find any units" in (upload.error or "")


def test_an_unreadable_pdf_fails_at_the_read(
    db: Session, world: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    upload = _read(db, world, monkeypatch, error=ValueError("broken xref table"))

    assert upload.status is UploadStatus.FAILED
    assert "could not read" in (upload.error or "")
    assert upload.completed_at is not None


def test_an_undetected_grade_is_not_pinned(
    db: Session, world: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cover page with no grade gives 0. That is shown, not stored -- pinning
    it would put every unit at "grade 0", where no child would ever see them."""
    upload = _read(db, world, monkeypatch, _FakeDocument([_FakeUnit(1, "Unit", 2)], grade=0))

    assert upload.status is UploadStatus.REVIEW
    assert upload.preview["grade_level"] == 0
    assert upload.grade_level is None


def test_waiting_for_review_counts_as_active(db: Session, world: dict[str, Any]) -> None:
    """It is still the parent's open decision -- and counting it is what brings
    the question back when they reload."""
    waiting = make_upload(db, world["alice"], UploadStatus.REVIEW)
    assert active_upload(db, world["alice"].id).id == waiting.id


def test_a_cancelled_upload_is_not_active(db: Session, world: dict[str, Any]) -> None:
    make_upload(db, world["alice"], UploadStatus.CANCELLED)
    assert active_upload(db, world["alice"].id) is None


def test_confirming_queues_the_build(db: Session, world: dict[str, Any]) -> None:
    from services.curriculum_upload import confirm

    upload = make_upload(db, world["alice"], UploadStatus.REVIEW)
    confirm(db, upload)
    assert upload.status is UploadStatus.PENDING


@pytest.mark.parametrize(
    "status",
    [UploadStatus.PENDING, UploadStatus.COMPLETED, UploadStatus.FAILED, UploadStatus.CANCELLED],
)
def test_only_a_waiting_upload_can_be_decided(
    db: Session, world: dict[str, Any], status: UploadStatus
) -> None:
    """Confirming a finished upload would rebuild it; cancelling a running one
    would pull the file out from under the job."""
    from services.curriculum_upload import UploadStateError, cancel, confirm

    upload = make_upload(db, world["alice"], status)
    with pytest.raises(UploadStateError):
        confirm(db, upload)
    with pytest.raises(UploadStateError):
        cancel(db, upload)


def test_saying_no_removes_the_stored_file(
    db: Session, world: dict[str, Any], tmp_path: Any  # noqa: ANN401
) -> None:
    from services.curriculum_upload import cancel

    stored = tmp_path / "guide.pdf"
    stored.write_bytes(b"%PDF-1.7")
    upload = make_upload(db, world["alice"], UploadStatus.REVIEW)
    upload.stored_path = str(stored)
    db.commit()

    cancel(db, upload)

    assert upload.status is UploadStatus.CANCELLED
    assert not stored.exists()
    assert upload.stored_path is None


def test_saying_no_keeps_a_file_another_upload_still_uses(
    db: Session, world: dict[str, Any], tmp_path: Any  # noqa: ANN401
) -> None:
    """Files are named by content hash, so re-uploading the same PDF shares the
    path. Deleting it would pull it out from under the other row."""
    from services.curriculum_upload import cancel

    stored = tmp_path / "guide.pdf"
    stored.write_bytes(b"%PDF-1.7")
    earlier = make_upload(db, world["alice"], UploadStatus.COMPLETED)
    earlier.stored_path = str(stored)
    waiting = make_upload(db, world["alice"], UploadStatus.REVIEW)
    waiting.stored_path = str(stored)
    db.commit()

    cancel(db, waiting)

    assert stored.exists()
    assert waiting.status is UploadStatus.CANCELLED
