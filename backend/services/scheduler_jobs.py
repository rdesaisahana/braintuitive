"""Background jobs run by APScheduler.

The important one is :func:`refill_question_bank`, which keeps the
pre-generated question bank stocked *ahead* of where students are working.
That is what lets ``create_quiz`` answer in milliseconds instead of spending
half a minute on the model while a child waits.

The job is deliberately budgeted. Filling one curriculum to depth is roughly
six hours of generation -- 47 sub-units x 3 tiers x 30 questions at about five
seconds each -- so a run adds at most ``max_questions`` questions and stops.
Run it often enough and the bank converges without any single run holding
resources for hours.

**Everything here is scoped to one curriculum owner.** Curriculum is per
family: a parent uploads their school's guide and their children work through
it, while families who have not uploaded share the sample. Unit numbers are
therefore not global -- one family's Unit 3 is a different Unit 3 from
another's. Filling by unit number alone would let one family reaching Unit 3
trigger Unit 3 fills for every other family at that grade, spending a shared
budget on curricula nobody is near.

Scope logic, per owner: Unit 1 is always kept stocked so a new student never
waits. Beyond that, each student keeps their current unit stocked and pulls the
next one into scope only once they are ``BANK_LOOKAHEAD_TRIGGER`` (default 70%)
through the current one. Filling the next unit the moment someone starts the
previous one would spend hours of generation on units a student who churns in
week one never sees; waiting until they near the boundary defers that cost to
where it is about to be needed, and sequential unlocking guarantees they cannot
outrun it.
"""

from __future__ import annotations

import logging

from sqlalchemy import func
from collections import defaultdict
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait

from sqlalchemy.orm import Session

from agents.quiz_generator import QuizGeneratorAgent
from config import settings
from db.database import session_scope
from db.models import CurriculumSubUnit, CurriculumUnit, DifficultyLevel, SubUnitProgress
from services.question_bank import (
    DEFAULT_BANK_DEPTH,
    FillReport,
    fill_slot,
    slot_status,
)

logger = logging.getLogger(__name__)

# A single run's generation budget, shared across every curriculum owner with
# work to do. Measured at ~5s per verified question, so 60 questions is about
# five minutes of work per run.
DEFAULT_QUESTION_BUDGET = 60

# What a newly uploaded curriculum gets filled immediately, before the
# scheduler's next tick. Three slots is roughly the first session: sub-unit 1.1
# at beginner, then whatever they reach next. The rest follows within the
# refill interval, by which time no child has got that far.
DEFAULT_PRIME_BUDGET = 90


def curriculum_owners(db: Session, subject: str = "math", grade_level: int = 6) -> list[str | None]:
    """Every distinct curriculum at this grade, identified by its owner.

    ``None`` is the shared sample curriculum, which is a real curriculum with
    real students on it and is filled like any other.
    """
    rows = (
        db.query(CurriculumUnit.user_id)
        .filter_by(subject=subject, grade_level=grade_level, is_active=True)
        .distinct()
        .all()
    )
    return [row[0] for row in rows]


def student_frontier(db: Session, subject: str = "math", grade_level: int = 6) -> int:
    """Return the highest unit number any student has reached.

    A unit counts as reached once the student has any progress row in it. With
    no students yet, the frontier is Unit 1 -- which is what a fresh install
    should be pre-filling anyway.
    """
    reached = (
        db.query(CurriculumUnit.unit_number)
        .join(CurriculumSubUnit, CurriculumSubUnit.unit_id == CurriculumUnit.id)
        .join(SubUnitProgress, SubUnitProgress.sub_unit_id == CurriculumSubUnit.id)
        .filter(
            CurriculumUnit.subject == subject,
            CurriculumUnit.grade_level == grade_level,
        )
        .order_by(CurriculumUnit.unit_number.desc())
        .first()
    )
    return int(reached[0]) if reached else 1


def unit_completion(db: Session, student_id: str, unit_id: str) -> float:
    """How far through a unit a student is, as a fraction from 0.0 to 1.0.

    Averages ``completion_percentage`` across the unit's active sub-units, so
    a student with five of seven sub-units finished sits at ~0.71 -- close
    enough to the end that the next unit is worth preparing.
    """
    sub_unit_ids = [
        row[0]
        for row in db.query(CurriculumSubUnit.id).filter_by(unit_id=unit_id, is_active=True).all()
    ]
    if not sub_unit_ids:
        return 0.0

    total = (
        db.query(func.coalesce(func.sum(SubUnitProgress.completion_percentage), 0))
        .filter(
            SubUnitProgress.student_id == student_id,
            SubUnitProgress.sub_unit_id.in_(sub_unit_ids),
        )
        .scalar()
        or 0
    )
    return float(total) / (100.0 * len(sub_unit_ids))


def units_to_fill(
    db: Session,
    subject: str = "math",
    grade_level: int = 6,
    trigger: float | None = None,
    owner: str | None = None,
) -> list[int]:
    """Decide which units of **one owner's curriculum** to keep stocked.

    Unit 1 is always kept stocked -- a new student must never wait. Beyond
    that, each student keeps their *current* unit stocked, and pulls the
    **next** one into scope only once they are ``trigger`` of the way through
    the current one.

    Pre-filling the next unit the moment a student starts the previous one
    would spend hours of generation on units a student who churns in week one
    never sees. Waiting until they are near the boundary defers that cost to
    the point where it is about to be needed, and sequential unlocking
    guarantees they cannot outrun it.

    Args:
        db: Active session.
        subject: Curriculum subject.
        grade_level: Curriculum grade.
        trigger: Fraction through a unit at which the next comes into scope.
        owner: Whose curriculum. ``None`` is the shared sample -- a real
            curriculum, not "any owner". Progress is read only from students
            actually on this curriculum, because another family's position in
            their own Unit 3 says nothing about this one.
    """
    trigger = settings.BANK_LOOKAHEAD_TRIGGER if trigger is None else trigger

    existing = {
        row[0]
        for row in db.query(CurriculumUnit.unit_number)
        .filter_by(subject=subject, grade_level=grade_level, user_id=owner)
        .all()
    }
    if not existing:
        return []

    eligible: set[int] = {1}

    # Each student's furthest unit within *this* curriculum, and how far into
    # it they are. The owner filter on the unit is what scopes it: a progress
    # row points at a sub-unit, which points at a unit, which carries the
    # owner -- so a family's progress can only ever widen the scope of the
    # curriculum they are actually working through.
    rows = (
        db.query(
            SubUnitProgress.student_id,
            CurriculumUnit.unit_number,
            CurriculumUnit.id,
        )
        .join(CurriculumSubUnit, CurriculumSubUnit.id == SubUnitProgress.sub_unit_id)
        .join(CurriculumUnit, CurriculumUnit.id == CurriculumSubUnit.unit_id)
        .filter(
            CurriculumUnit.subject == subject,
            CurriculumUnit.grade_level == grade_level,
            CurriculumUnit.user_id.is_(None) if owner is None else CurriculumUnit.user_id == owner,
        )
        .distinct()
        .all()
    )

    furthest: dict[str, tuple[int, str]] = {}
    for student_id, unit_number, unit_id in rows:
        current = furthest.get(student_id)
        if current is None or unit_number > current[0]:
            furthest[student_id] = (unit_number, unit_id)

    for student_id, (unit_number, unit_id) in furthest.items():
        eligible.add(unit_number)
        completion = unit_completion(db, student_id, unit_id)
        if completion >= trigger:
            eligible.add(unit_number + 1)
            logger.info(
                "Student %s is %.0f%% through unit %d - queuing unit %d.",
                student_id[:8],
                completion * 100,
                unit_number,
                unit_number + 1,
            )

    return sorted(eligible & existing)


def _slots_for_owner(
    db: Session,
    owner: str | None,
    subject: str,
    grade_level: int,
    target: int,
    trigger: float | None,
) -> list[tuple[CurriculumSubUnit, DifficultyLevel, object]]:
    """The unstocked slots of one owner's curriculum, most urgent first."""
    units = units_to_fill(db, subject, grade_level, trigger, owner=owner)
    if not units:
        return []

    sub_units = (
        db.query(CurriculumSubUnit)
        .join(CurriculumUnit)
        .filter(
            CurriculumUnit.subject == subject,
            CurriculumUnit.grade_level == grade_level,
            CurriculumUnit.user_id.is_(None) if owner is None else CurriculumUnit.user_id == owner,
            CurriculumUnit.unit_number.in_(units),
            CurriculumSubUnit.is_active.is_(True),
            CurriculumSubUnit.is_indexed.is_(True),
        )
        .order_by(CurriculumUnit.unit_number, CurriculumSubUnit.sequence)
        .all()
    )

    slots = [
        (sub_unit, difficulty, slot_status(db, sub_unit, difficulty, target))
        for sub_unit in sub_units
        for difficulty in DifficultyLevel
    ]
    # Earliest unit first (students are there now), then emptiest slots, so
    # nothing is starved while another is topped from 28 to 30.
    slots.sort(key=lambda entry: (entry[0].unit.unit_number, -entry[2].shortfall))
    return [entry for entry in slots if not entry[2].is_stocked]


def _priming_groups(
    slots: list[tuple[CurriculumSubUnit, DifficultyLevel, object]],
    order: str,
) -> list[list[tuple[str, DifficultyLevel]]]:
    """Split the slots into groups to be filled one group after another.

    ``topic`` puts each topic's three levels in one group, in curriculum order:
    1.1 is finished -- Easy, Medium and Tricky -- before 1.2 is started. A
    child works down a topic before moving on, so this keeps the work ahead of
    where they are.

    ``level`` puts each level in one group: Easy everywhere, then Medium, then
    Tricky. That way any topic can be opened at Easy immediately, which is what
    the scheduler wants when several children are at different places.
    """
    tier = {difficulty: rank for rank, difficulty in enumerate(DifficultyLevel)}
    if order == "topic":
        ordered = sorted(
            slots, key=lambda entry: (entry[0].unit.unit_number, entry[0].sequence, tier[entry[1]])
        )
        key = lambda entry: (entry[0].unit.unit_number, entry[0].sequence)  # noqa: E731
    else:
        ordered = sorted(
            slots, key=lambda entry: (entry[0].unit.unit_number, tier[entry[1]], entry[0].sequence)
        )
        key = lambda entry: (entry[0].unit.unit_number, entry[1])  # noqa: E731

    groups: list[list[tuple[str, DifficultyLevel]]] = []
    current_key = object()
    for entry in ordered:
        if key(entry) != current_key:
            current_key = key(entry)
            groups.append([])
        groups[-1].append((entry[0].id, entry[1]))
    return groups


def _fill_groups_in_parallel(
    groups: list[list[tuple[str, DifficultyLevel]]],
    budget: int,
    depth: int,
    workers: int,
    report: FillReport,
) -> None:
    """Fill one group at a time, its slots concurrently.

    Writing questions is almost entirely waiting on the model, so one slot at a
    time leaves a parent watching a spinner while the machine idles. Each worker
    opens its own session and its own agent -- neither is safe to share between
    threads -- exactly as utils/fill_bank.py has always done.

    A group is finished before the next is started: that is what makes the
    order mean anything. The budget counts questions actually written, checked
    before each new slot is started, so it can overshoot by at most the slots
    already in flight -- reserving it up front overshot by the whole queue.
    """
    started = report.questions_added

    def fill_one(sub_unit_id: str, difficulty: DifficultyLevel) -> FillReport:
        own = FillReport()
        with session_scope() as own_db:
            sub_unit = own_db.get(CurriculumSubUnit, sub_unit_id)
            if sub_unit is not None:
                fill_slot(
                    own_db,
                    sub_unit=sub_unit,
                    difficulty=difficulty,
                    target=depth,
                    agent=QuizGeneratorAgent(verify=True, verbose=False),
                    report=own,
                )
        return own

    def absorb(future: Future) -> None:
        try:
            part = future.result()
        except Exception as exc:  # noqa: BLE001 - one slot must not sink the rest
            logger.exception("Priming a slot failed")
            report.errors.append(str(exc)[:200])
            return
        report.slots_examined += part.slots_examined
        report.slots_filled += part.slots_filled
        report.questions_added += part.questions_added
        report.quarantined += part.quarantined
        report.duplicates_skipped += part.duplicates_skipped
        report.errors.extend(part.errors)

    for group in groups:
        queue = list(group)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            in_flight: set[Future] = set()
            while queue or in_flight:
                while queue and len(in_flight) < workers and report.questions_added - started < budget:
                    sub_unit_id, difficulty = queue.pop(0)
                    in_flight.add(pool.submit(fill_one, sub_unit_id, difficulty))
                if not in_flight:
                    break
                done, in_flight = wait(in_flight, return_when=FIRST_COMPLETED)
                for future in done:
                    absorb(future)
        if report.questions_added - started >= budget:
            return


def _fill_slots(
    db: Session,
    slots: list[tuple[CurriculumSubUnit, DifficultyLevel, object]],
    budget: int,
    target: int,
    agent: QuizGeneratorAgent,
    report: FillReport,
    workers: int = 1,
    order: str = "level",
) -> None:
    """Spend up to ``budget`` questions on these slots: breadth first, then depth.

    Pass one brings every slot to a single quiz's worth, Easy tier first across
    all the topics, then Medium, then Tricky. Pass two tops the same slots up to
    ``target`` in the order given.

    Filling one slot all the way before touching the next is how a fresh
    upload's whole priming budget ended up in Topic 1.1 -- all three tiers, 30
    deep -- while every other Unit 1 topic had nothing, and a child clicking
    Easy on 1.2 waited a minute or more for live generation. One quiz's worth
    everywhere first means any topic a child can open starts instantly; depth
    only buys retries, which are needed later.
    """
    start = report.questions_added
    first_quiz = min(target, settings.QUESTIONS_PER_QUIZ)
    groups = _priming_groups(slots, order)
    by_id = {sub_unit.id: sub_unit for sub_unit, _difficulty, _status in slots}
    breadth = [(by_id[sub_unit_id], difficulty, None) for group in groups for sub_unit_id, difficulty in group]

    if workers > 1 and groups:
        _fill_groups_in_parallel(groups, budget, first_quiz, workers, report)
        # Depth only buys retries, and nobody is watching it: one at a time.
        for sub_unit, difficulty, _status in slots:
            if report.questions_added - start >= budget:
                return
            fill_slot(
                db, sub_unit=sub_unit, difficulty=difficulty, target=target, agent=agent, report=report
            )
        return

    for ordered, depth in ((breadth, first_quiz), (slots, target)):
        for sub_unit, difficulty, _status in ordered:
            if report.questions_added - start >= budget:
                return
            fill_slot(
                db,
                sub_unit=sub_unit,
                difficulty=difficulty,
                target=depth,
                agent=agent,
                report=report,
            )


def refill_question_bank(
    subject: str = "math",
    grade_level: int = 6,
    target: int = DEFAULT_BANK_DEPTH,
    max_questions: int | None = None,
    trigger: float | None = None,
) -> FillReport:
    """Top up every curriculum's bank, ahead of where its students are working.

    The budget is a **global cost ceiling**, shared evenly between the owners
    who have work to do. Giving each owner the full budget would make one run's
    cost scale with the number of families, which is how a background job
    becomes an unbounded bill. Sharing it means each family fills more slowly
    as more join -- the honest trade -- and the lever for that is raising
    ``BANK_REFILL_BUDGET``, not letting the job decide to spend more.

    Args:
        subject: Curriculum subject to fill.
        grade_level: Curriculum grade to fill.
        target: Questions each slot should hold.
        max_questions: Generation budget for this run, across all owners.
        trigger: How far through their unit a student must be before the next
            unit is pre-filled. Defaults to ``BANK_LOOKAHEAD_TRIGGER``.

    Returns:
        A :class:`FillReport` describing what this run did.
    """
    max_questions = settings.BANK_REFILL_BUDGET if max_questions is None else max_questions
    report = FillReport()

    with session_scope() as db:
        owners = curriculum_owners(db, subject, grade_level)
        if not owners:
            logger.info("Bank refill: no curriculum found for %s grade %d.", subject, grade_level)
            return report

        work = {
            owner: _slots_for_owner(db, owner, subject, grade_level, target, trigger)
            for owner in owners
        }
        needy = {owner: slots for owner, slots in work.items() if slots}

        if not needy:
            logger.info("Bank refill: every curriculum in scope is fully stocked.")
            return report

        # Integer division can leave a remainder; the first owners get it, and
        # ordering rotates by need on the next run because a filled owner drops
        # out of `needy` entirely.
        share = max(1, max_questions // len(needy))
        logger.info(
            "Bank refill: %d curriculum(s) need work, %d slot(s) short, "
            "budget %d question(s) (%d each).",
            len(needy),
            sum(len(slots) for slots in needy.values()),
            max_questions,
            share,
        )

        agent = QuizGeneratorAgent(verify=True, verbose=False)
        for _owner, slots in needy.items():
            if report.questions_added >= max_questions:
                logger.info("Bank refill: budget reached, stopping for this run.")
                break
            remaining = max_questions - report.questions_added
            _fill_slots(db, slots, min(share, remaining), target, agent, report)

    logger.info("Bank refill complete: %s", report.summary())
    return report


def prime_new_curriculum(
    user_id: str,
    subject: str = "math",
    grade_level: int | None = None,
    budget: int | None = None,
    target: int = DEFAULT_BANK_DEPTH,
) -> FillReport:
    """Fill the first few slots of a freshly uploaded curriculum.

    A parent who has just uploaded expects their child to start tonight, and
    an empty bank means the first quiz spends ~36 seconds or more on the model.
    This gives every Unit 1 topic an Easy quiz first (see ``_fill_slots``), so
    whichever topic a child opens starts instantly; the scheduler covers the
    rest within its interval, by which time no child has got far enough to
    notice.

    Deliberately *not* the whole curriculum. That is roughly six hours of
    generation, most of it on units sequential unlocking makes unreachable for
    weeks -- and all of it wasted if the parent re-uploads a corrected PDF.

    Args:
        user_id: Owner of the newly uploaded curriculum.
        subject: Curriculum subject.
        grade_level: Grade to prime. Inferred from the owner's curriculum when
            omitted.
        budget: Questions to generate now. Defaults to ``BANK_PRIME_BUDGET``.
        target: Depth each slot should reach.

    Returns:
        A :class:`FillReport` describing what was primed.
    """
    budget = settings.BANK_PRIME_BUDGET if budget is None else budget
    report = FillReport()
    if budget <= 0:
        return report

    with session_scope() as db:
        if grade_level is None:
            row = (
                db.query(CurriculumUnit.grade_level)
                .filter_by(user_id=user_id, subject=subject, is_active=True)
                .order_by(CurriculumUnit.unit_number)
                .first()
            )
            if row is None:
                logger.info("Priming skipped: user %s owns no curriculum.", user_id[:8])
                return report
            grade_level = int(row[0])

        # Unit 1 only, and only the slots a first session can reach.
        slots = _slots_for_owner(db, user_id, subject, grade_level, target, trigger=1.1)
        slots = [entry for entry in slots if entry[0].unit.unit_number == 1]
        if not slots:
            logger.info("Priming skipped: Unit 1 is already stocked for %s.", user_id[:8])
            return report

        logger.info(
            "Priming curriculum for %s: %d slot(s) short in Unit 1, budget %d.",
            user_id[:8],
            len(slots),
            budget,
        )
        agent = QuizGeneratorAgent(verify=True, verbose=False)
        # A parent is watching this one: several slots at once, and in the
        # order a child will meet them.
        _fill_slots(
            db,
            slots,
            budget,
            target,
            agent,
            report,
            workers=settings.BANK_PRIME_WORKERS,
            order=settings.BANK_PRIME_ORDER,
        )

    logger.info("Priming complete for %s: %s", user_id[:8], report.summary())
    return report


def register_jobs(scheduler: object) -> None:
    """Register recurring jobs on an APScheduler instance.

    Called once at startup from ``main``. Kept separate from the job bodies so
    the jobs stay importable and testable without a scheduler.
    """
    scheduler.add_job(  # type: ignore[attr-defined]
        refill_question_bank,
        trigger="interval",
        minutes=30,
        id="refill_question_bank",
        replace_existing=True,
        max_instances=1,  # never let two fillers race
        coalesce=True,  # a missed run does not pile up
        misfire_grace_time=600,
    )
    logger.info("Registered job: refill_question_bank (every 30 min).")


__all__ = [
    "DEFAULT_PRIME_BUDGET",
    "DEFAULT_QUESTION_BUDGET",
    "curriculum_owners",
    "prime_new_curriculum",
    "refill_question_bank",
    "register_jobs",
    "student_frontier",
    "unit_completion",
    "units_to_fill",
]
