"""Curriculum routes: the map a student sees when they log in.

Everything else in the API is transactional -- start a quiz, answer, complete.
This router answers the question none of those can: *what does this child see
right now?* It composes three things that otherwise only exist separately:

* the static structure parsed from the district PDF (units and sub-units),
* this student's progress against it,
* the sequential-unit gate, and the reason a locked unit is locked.

**Everything is batch-loaded.** ``check_unit_unlocked()`` runs three queries
per unit, so calling it in a loop -- plus per-unit sub-unit and progress
lookups -- turns one dashboard render into twenty-odd round trips, growing
with the curriculum. This module issues a fixed four queries regardless of
size and applies the identical rule in memory via
``compute_unlock_status()``. A test pins the query count so it cannot quietly
regress.
"""

from __future__ import annotations

import logging
from collections import defaultdict

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    Form,
    HTTPException,
    Response,
    UploadFile,
    status,
)
from sqlalchemy import func
from sqlalchemy.orm import Session

from api.deps import get_current_user, get_owned_student
from api.schemas import (
    CurriculumDeletionOut,
    CurriculumPreviewOut,
    CurriculumStatusOut,
    CurriculumUploadOut,
    NextActionOut,
    SubUnitOut,
    UnitOut,
)
from config import settings
from db.database import get_db
from db.models import (  # noqa: F401  (resume lookup)
    CurriculumSubUnit,
    CurriculumUnit,
    CurriculumUpload,
    DifficultyLevel,
    ProgressStatus,
    QuestionBankItem,
    Quiz,
    QuizResponse,
    Student,
    SubUnitProgress,
    UploadStatus,
    User,
)
from services.curriculum_delete import delete_curriculum
from services.curriculum_delete import preview as preview_deletion
from services.curriculum_scope import has_own_curriculum, visible_units
from services.curriculum_upload import (
    UploadRejectedError,
    UploadStateError,
    active_upload,
    record,
    run_ingestion,
    run_preview,
    store,
    validate,
)
from services.curriculum_upload import cancel as cancel_upload
from services.curriculum_upload import confirm as confirm_upload
from services.quiz_service import compute_unlock_status, find_resumable_quiz

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/curriculum", tags=["curriculum"])


# --------------------------------------------------------------------------- #
# Batch loading
# --------------------------------------------------------------------------- #


def _ready_difficulties(db: Session, sub_unit_ids: list[str]) -> dict[str, list[DifficultyLevel]]:
    """Which (sub-unit, difficulty) slots can fill a quiz from the bank.

    One grouped query for the whole curriculum. A tier that is absent still
    works -- it falls back to live generation -- but takes about two minutes,
    which the UI must be able to warn about.
    """
    if not sub_unit_ids:
        return {}

    rows = (
        db.query(
            QuestionBankItem.sub_unit_id,
            QuestionBankItem.difficulty_level,
            func.count(QuestionBankItem.id),
        )
        .filter(
            QuestionBankItem.sub_unit_id.in_(sub_unit_ids),
            QuestionBankItem.is_active.is_(True),
            QuestionBankItem.verification_status != "disputed",
        )
        .group_by(QuestionBankItem.sub_unit_id, QuestionBankItem.difficulty_level)
        .all()
    )

    ready: dict[str, list[DifficultyLevel]] = defaultdict(list)
    for sub_unit_id, difficulty, count in rows:
        if count >= settings.QUESTIONS_PER_QUIZ:
            ready[sub_unit_id].append(difficulty)
    for values in ready.values():
        values.sort(key=lambda level: level.order)
    return ready


def _sub_unit_view(
    sub_unit: CurriculumSubUnit,
    progress: SubUnitProgress | None,
    ready: list[DifficultyLevel],
) -> SubUnitOut:
    """Fold one student's progress into a sub-unit."""
    return SubUnitOut(
        id=sub_unit.id,
        sub_unit_number=sub_unit.sub_unit_number,
        title=sub_unit.title,
        description=sub_unit.description,
        sequence=sub_unit.sequence,
        completion_percentage=progress.completion_percentage if progress else 0,
        status=progress.status if progress else ProgressStatus.NOT_STARTED,
        beginner_completed=bool(progress and progress.beginner_completed),
        intermediate_completed=bool(progress and progress.intermediate_completed),
        proficient_completed=bool(progress and progress.proficient_completed),
        next_difficulty=(progress.next_difficulty if progress else DifficultyLevel.BEGINNER),
        # Reporting only. Spending it here would let a background refresh
        # swallow a celebration the child never saw.
        should_celebrate=bool(progress and progress.should_celebrate),
        ready_difficulties=ready,
    )


def build_curriculum_map(db: Session, student: Student) -> list[UnitOut]:
    """Return every unit for this student's grade, with progress and lock state.

    Four queries total: units, sub-units, progress, bank counts.
    """
    units: list[CurriculumUnit] = visible_units(db, student)
    if not units:
        return []

    unit_ids = [unit.id for unit in units]
    sub_units: list[CurriculumSubUnit] = (
        db.query(CurriculumSubUnit)
        .filter(CurriculumSubUnit.unit_id.in_(unit_ids), CurriculumSubUnit.is_active.is_(True))
        .order_by(CurriculumSubUnit.sequence)
        .all()
    )

    by_unit: dict[str, list[CurriculumSubUnit]] = defaultdict(list)
    for sub_unit in sub_units:
        by_unit[sub_unit.unit_id].append(sub_unit)

    sub_unit_ids = [sub_unit.id for sub_unit in sub_units]
    progress_rows: list[SubUnitProgress] = (
        db.query(SubUnitProgress)
        .filter(
            SubUnitProgress.student_id == student.id,
            SubUnitProgress.sub_unit_id.in_(sub_unit_ids),
        )
        .all()
        if sub_unit_ids
        else []
    )
    progress_by_sub_unit = {row.sub_unit_id: row for row in progress_rows}
    completed_ids = {row.sub_unit_id for row in progress_rows if row.completion_percentage == 100}
    ready_map = _ready_difficulties(db, sub_unit_ids)

    result: list[UnitOut] = []
    previous_unit: CurriculumUnit | None = None

    for unit in units:
        unit_sub_units = by_unit.get(unit.id, [])
        unlock = compute_unlock_status(
            unit=unit,
            previous_unit=previous_unit,
            previous_sub_units=by_unit.get(previous_unit.id, []) if previous_unit else [],
            completed_sub_unit_ids=completed_ids,
        )

        views = [
            _sub_unit_view(
                sub_unit,
                progress_by_sub_unit.get(sub_unit.id),
                ready_map.get(sub_unit.id, []),
            )
            for sub_unit in unit_sub_units
        ]
        # A unit's completion is the mean of its sub-units, so a unit with five
        # of seven finished reads 71% rather than "in progress".
        #
        # Rounded half UP, not with round(): Python rounds halves to even, so
        # round(16.5) is 16. Showing a child 16% when they are at 16.5%
        # understates their work, and 16.5 -> 16 next to 17.5 -> 18 looks
        # simply broken.
        completion = (
            int(sum(view.completion_percentage for view in views) / len(views) + 0.5)
            if views
            else 0
        )

        result.append(
            UnitOut(
                id=unit.id,
                unit_number=unit.unit_number,
                title=unit.title,
                description=unit.description,
                total_sub_units=len(unit_sub_units),
                unlocked=unlock.unlocked,
                lock_reason=unlock.reason or None,
                blocking_sub_units=unlock.blocking_sub_units or [],
                completion_percentage=completion,
                sub_units=views,
            )
        )
        previous_unit = unit

    return result


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #


@router.get("/students/{student_id}/units", response_model=list[UnitOut])
def list_units(
    student: Student = Depends(get_owned_student),
    db: Session = Depends(get_db),
) -> list[UnitOut]:
    """The dashboard: every unit for this student, with progress and lock state.

    Locked units still list their sub-units. Seeing what is coming is
    motivating, and hiding it makes the course look smaller than it is.
    """
    return build_curriculum_map(db, student)


@router.get("/students/{student_id}/units/{unit_number}", response_model=UnitOut)
def read_unit(
    unit_number: int,
    student: Student = Depends(get_owned_student),
    db: Session = Depends(get_db),
) -> UnitOut:
    """One unit in detail, for the unit screen."""
    for unit in build_curriculum_map(db, student):
        if unit.unit_number == unit_number:
            return unit
    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail=f"No unit {unit_number} in the grade {student.grade_level} curriculum.",
    )


@router.get("/students/{student_id}/next", response_model=NextActionOut)
def next_action(
    student: Student = Depends(get_owned_student),
    db: Session = Depends(get_db),
) -> NextActionOut:
    """What this student should do next.

    An unfinished quiz always wins. A child who stopped halfway through
    yesterday should land back on that quiz, in that unit, at the question
    they reached -- not be pointed at something new while their half-finished
    work sits invisible.

    Failing that, this walks units in order and, within the first unlocked
    incomplete unit, picks the earliest unfinished sub-unit and the tier they
    have not yet passed. Sub-units are flexible in principle, so it is a
    recommendation rather than a restriction.
    """
    units = build_curriculum_map(db, student)
    if not units:
        return NextActionOut(
            has_next=False,
            action="none",
            message=f"No curriculum has been loaded for grade {student.grade_level} yet.",
        )

    # --- an unfinished quiz takes priority over anything new --------------
    resumable = find_resumable_quiz(db, student_id=student.id)
    if resumable is not None:
        answered = db.query(QuizResponse).filter_by(quiz_id=resumable.id).count()
        answered_ids = {
            row[0]
            for row in db.query(QuizResponse.question_id).filter_by(quiz_id=resumable.id).all()
        }
        next_number = next(
            (
                question.question_number
                for question in sorted(resumable.questions, key=lambda q: q.question_number)
                if question.id not in answered_ids
            ),
            None,
        )
        sub_unit = db.get(CurriculumSubUnit, resumable.sub_unit_id)
        unit = sub_unit.unit if sub_unit else None
        return NextActionOut(
            has_next=True,
            action="resume",
            resume_quiz_id=resumable.id,
            answered_count=answered,
            next_question_number=next_number,
            unit_id=unit.id if unit else None,
            unit_number=unit.unit_number if unit else None,
            unit_title=unit.title if unit else None,
            sub_unit_id=resumable.sub_unit_id,
            sub_unit_number=sub_unit.sub_unit_number if sub_unit else None,
            sub_unit_title=sub_unit.title if sub_unit else None,
            difficulty=resumable.difficulty_level,
            quiz_ready=True,
            message=(
                f"Pick up where you left off: "
                f"{sub_unit.sub_unit_number if sub_unit else '?'} "
                f"({resumable.difficulty_level.value}), "
                f"{answered} of {resumable.total_questions} done"
            ),
        )

    # --- otherwise, the next thing to start -------------------------------
    for unit in units:
        if not unit.unlocked:
            continue
        for sub_unit in sorted(unit.sub_units, key=lambda s: s.sequence):
            if sub_unit.completion_percentage >= 100:
                continue
            difficulty = sub_unit.next_difficulty or DifficultyLevel.BEGINNER
            return NextActionOut(
                has_next=True,
                action="start",
                unit_id=unit.id,
                unit_number=unit.unit_number,
                unit_title=unit.title,
                sub_unit_id=sub_unit.id,
                sub_unit_number=sub_unit.sub_unit_number,
                sub_unit_title=sub_unit.title,
                difficulty=difficulty,
                quiz_ready=difficulty in sub_unit.ready_difficulties,
                message=(
                    f"Next: {sub_unit.sub_unit_number} {sub_unit.title} " f"({difficulty.value})"
                ),
            )

    return NextActionOut(
        has_next=False,
        action="none",
        message="Everything available has been completed. Nice work!",
    )


@router.post(
    "/students/{student_id}/celebrations/{sub_unit_id}/ack",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    # Required, not decorative: this module uses
    # `from __future__ import annotations`, so `-> None` reaches FastAPI as
    # the string "None", resolves to truthy NoneType, and a 204 with a
    # response model refuses to mount.
    response_model=None,
)
def acknowledge_celebration(
    sub_unit_id: str,
    student: Student = Depends(get_owned_student),
    db: Session = Depends(get_db),
) -> None:
    """Mark a 100% celebration as shown.

    Reporting ``should_celebrate`` never spends it -- the client calls this
    once the animation has actually played. That way a dropped response or a
    tab closed mid-animation does not cost a child the one moment the whole
    progression is building towards; they will simply see it next time.

    Idempotent: acknowledging twice is not an error.
    """
    progress = (
        db.query(SubUnitProgress)
        .filter_by(student_id=student.id, sub_unit_id=sub_unit_id)
        .one_or_none()
    )
    if progress is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No progress recorded for that sub-unit.",
        )

    if not progress.celebration_shown:
        progress.celebration_shown = True
        db.commit()
        logger.info(
            "Celebration acknowledged: student %s, sub-unit %s",
            student.id[:8],
            sub_unit_id[:8],
        )


# --------------------------------------------------------------------------- #
# Curriculum upload
# --------------------------------------------------------------------------- #


def _upload_view(upload: CurriculumUpload) -> CurriculumUploadOut:
    return CurriculumUploadOut(
        id=upload.id,
        filename=upload.filename,
        subject=upload.subject,
        grade_level=upload.grade_level,
        status=upload.status.value,
        error=upload.error,
        units_written=upload.units_written,
        sub_units_written=upload.sub_units_written,
        chunks_created=upload.chunks_created,
        vectors_upserted=upload.vectors_upserted,
        warnings=list(upload.warnings or []),
        preview=CurriculumPreviewOut(**upload.preview) if upload.preview else None,
        created_at=upload.created_at,
        started_at=upload.started_at,
        completed_at=upload.completed_at,
    )


@router.post(
    "/upload",
    response_model=CurriculumUploadOut,
    status_code=status.HTTP_202_ACCEPTED,
)
async def upload_curriculum(
    background: BackgroundTasks,
    file: UploadFile = File(..., description="The curriculum guide, as a PDF"),
    subject: str = Form("math"),
    grade_level: int | None = Form(None),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> CurriculumUploadOut:
    """Ingest a curriculum PDF for this family.

    Returns **202** rather than 201: the file is accepted and stored, but the
    curriculum does not exist yet. Parsing, chunking, embedding and indexing
    take minutes, so they run after this response and the returned record is
    polled at ``GET /curriculum/uploads/{id}``.

    The uploaded curriculum belongs to this account. It replaces the shared
    sample for their children rather than merging with it -- two interleaved
    Unit 3s would break sequential progression, which assumes one course.
    """
    existing = active_upload(db, user.id)
    if existing is not None:
        # Two concurrent ingests would race on the same (user, subject, grade,
        # unit) rows and interleave two curricula into one.
        detail = (
            f"{existing.filename} is waiting for you to confirm it. "
            "Confirm or cancel it before uploading another."
            if existing.status is UploadStatus.REVIEW
            else f"{existing.filename} is still being processed. "
            "Please wait for it to finish before uploading another."
        )
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=detail)

    content = await file.read()
    try:
        validate(content, file.filename or "")
    except UploadRejectedError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc

    path, digest = store(content, file.filename or "curriculum.pdf", user.id)
    upload = record(
        db,
        user_id=user.id,
        filename=file.filename or "curriculum.pdf",
        path=path,
        digest=digest,
        size=len(content),
        subject=subject,
        grade_level=grade_level,
    )
    db.commit()

    # Read, not built: this phase parses the PDF and stops at "review" for the
    # parent to confirm. Nothing expensive happens until they say yes.
    background.add_task(run_preview, upload.id)

    logger.info(
        "Accepted curriculum %r (%.1f MB) from user %s.",
        upload.filename,
        len(content) / (1024 * 1024),
        user.id[:8],
    )
    return _upload_view(upload)


@router.get("/uploads", response_model=list[CurriculumUploadOut])
def list_uploads(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[CurriculumUploadOut]:
    """Every curriculum this account has uploaded, newest first."""
    uploads = (
        db.query(CurriculumUpload)
        .filter_by(user_id=user.id)
        .order_by(CurriculumUpload.created_at.desc())
        .limit(50)
        .all()
    )
    return [_upload_view(upload) for upload in uploads]


@router.get("/uploads/{upload_id}", response_model=CurriculumUploadOut)
def read_upload(
    upload_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> CurriculumUploadOut:
    """Poll one ingestion.

    Another account's upload returns 404 rather than 403, matching every other
    ownership check here: distinguishing them would confirm the id is real.
    """
    upload = db.get(CurriculumUpload, upload_id)
    if upload is None or upload.user_id != user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Upload not found.")
    return _upload_view(upload)


def _owned_upload(db: Session, user: User, upload_id: str) -> CurriculumUpload:
    upload = db.get(CurriculumUpload, upload_id)
    if upload is None or upload.user_id != user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Upload not found.")
    return upload


@router.post(
    "/uploads/{upload_id}/confirm",
    response_model=CurriculumUploadOut,
    status_code=status.HTTP_202_ACCEPTED,
)
def confirm_curriculum_upload(
    upload_id: str,
    background: BackgroundTasks,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> CurriculumUploadOut:
    """Yes, this is the right curriculum -- build it.

    Queues the expensive half of ingestion: embedding, indexing, and writing
    the first questions. 202 for the same reason as the upload itself: it takes
    minutes, and the returned record is polled.
    """
    upload = _owned_upload(db, user, upload_id)
    try:
        confirm_upload(db, upload)
    except UploadStateError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    db.commit()

    background.add_task(run_ingestion, upload.id)
    logger.info("Upload %s confirmed by user %s; building.", upload.id[:8], user.id[:8])
    return _upload_view(upload)


@router.post("/uploads/{upload_id}/cancel", response_model=CurriculumUploadOut)
def cancel_curriculum_upload(
    upload_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> CurriculumUploadOut:
    """No, that is the wrong file. Nothing was built, so nothing is lost."""
    upload = _owned_upload(db, user, upload_id)
    try:
        cancel_upload(db, upload)
    except UploadStateError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    db.commit()
    return _upload_view(upload)


@router.get("/status", response_model=CurriculumStatusOut)
def curriculum_status(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> CurriculumStatusOut:
    """Whether this family is on their own curriculum or the shared sample.

    What the app asks before deciding whether to show the upload screen.
    """
    owned = has_own_curriculum(db, user.id)
    units = (
        db.query(CurriculumUnit)
        .filter_by(user_id=user.id if owned else None, is_active=True)
        .count()
    )
    running = active_upload(db, user.id)

    # The PDF the curriculum in use came from, so the picker can show it
    # attached rather than a separate "your curriculum" panel beside it.
    latest = (
        db.query(CurriculumUpload)
        .filter_by(user_id=user.id, status=UploadStatus.COMPLETED)
        .order_by(CurriculumUpload.created_at.desc())
        .first()
        if owned
        else None
    )

    return CurriculumStatusOut(
        has_own_curriculum=owned,
        units=units,
        using_sample=not owned,
        active_upload=_upload_view(running) if running else None,
        filename=latest.filename if latest else None,
    )


@router.get("/deletion-preview", response_model=CurriculumDeletionOut)
def preview_curriculum_deletion(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> CurriculumDeletionOut:
    """What deleting this family's curriculum would remove.

    Reads only. Curriculum is the root of everything -- units own sub-units,
    which own quizzes, progress and the question bank -- so a parent about to
    click the cross deserves the actual cost before agreeing to it.
    """
    return CurriculumDeletionOut(**vars(preview_deletion(db, user.id)))


@router.delete("/", response_model=CurriculumDeletionOut)
def remove_curriculum(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> CurriculumDeletionOut:
    """Delete this family's curriculum and everything built on it.

    Refused while an upload is running: that job writes units and questions as
    it goes, and deleting underneath it would leave half a curriculum behind.
    """
    if active_upload(db, user.id) is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="An upload is in progress. Finish or cancel it first.",
        )

    report = delete_curriculum(db, user.id)
    db.commit()
    return CurriculumDeletionOut(**vars(report))
