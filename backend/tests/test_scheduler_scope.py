"""Bank-filling scope tests.

When curriculum became per-family, the filler did not follow. It selected work
by unit *number* across every owner at a grade, so one family reaching their
Unit 3 queued Unit 3 fills for the shared sample and for every other family --
none of whom were anywhere near it -- and they all competed for the same
budget. These tests pin the scoping that fixes it.

The properties that matter:

* one family's progress never widens another family's fill scope;
* the shared sample is a real curriculum, filled like any other, not a
  fallback that only fills when nobody owns anything;
* a new upload is primed far enough for a first session, and no further;
* the run budget is a global ceiling shared between owners, not a per-owner
  allowance that scales the bill with the number of families.

Generation is stubbed throughout; nothing here costs an API call.

Run:
    cd backend
    pytest tests/test_scheduler_scope.py -v
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
    Student,
    SubUnitProgress,
    User,
)
from services.scheduler_jobs import curriculum_owners, units_to_fill

SUB_UNITS_PER_UNIT = 2


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
    db: Session, owner: User | None, label: str, units: int = 4, grade: int = 6
) -> list[CurriculumUnit]:
    created: list[CurriculumUnit] = []
    for number in range(1, units + 1):
        unit = CurriculumUnit(
            user_id=owner.id if owner else None,
            unit_number=number,
            title=f"{label} Unit {number}",
            subject="math",
            grade_level=grade,
        )
        db.add(unit)
        db.flush()
        for index in range(SUB_UNITS_PER_UNIT):
            db.add(
                CurriculumSubUnit(
                    unit=unit,
                    sub_unit_number=f"{number}.{index + 1}",
                    sequence=index,
                    title=f"{label} {number}.{index + 1}",
                    is_indexed=True,
                )
            )
        created.append(unit)
    db.commit()
    return created


@pytest.fixture()
def world(db: Session) -> dict[str, Any]:
    """Sample curriculum plus two families, each with their own."""
    sample = make_curriculum(db, None, "Sample")

    alice = User(email="alice@example.com", hashed_password="x", full_name="Alice")
    bob = User(email="bob@example.com", hashed_password="x", full_name="Bob")
    db.add_all([alice, bob])
    db.flush()
    ana = Student(parent=alice, first_name="Ana", grade_level=6)
    ben = Student(parent=bob, first_name="Ben", grade_level=6)
    db.add_all([ana, ben])
    db.commit()

    return {
        "sample": sample,
        "alice": alice,
        "bob": bob,
        "ana": ana,
        "ben": ben,
        "alice_units": make_curriculum(db, alice, "Alice"),
        "bob_units": make_curriculum(db, bob, "Bob"),
    }


def put_student_in(db: Session, student: Student, unit: CurriculumUnit, completion: int) -> None:
    """Give a student progress across every sub-unit of one unit."""
    for sub_unit in unit.sub_units:
        db.add(
            SubUnitProgress(
                student_id=student.id,
                sub_unit_id=sub_unit.id,
                completion_percentage=completion,
                total_attempts=1,
            )
        )
    db.commit()


# --------------------------------------------------------------------------- #
# Owners
# --------------------------------------------------------------------------- #


def test_every_curriculum_is_found_including_the_sample(db: Session, world: dict[str, Any]) -> None:
    owners = curriculum_owners(db)
    assert set(owners) == {None, world["alice"].id, world["bob"].id}


def test_an_empty_grade_has_no_owners(db: Session, world: dict[str, Any]) -> None:
    assert curriculum_owners(db, grade_level=9) == []


# --------------------------------------------------------------------------- #
# Scope isolation -- the bug this file exists for
# --------------------------------------------------------------------------- #


def test_unit_one_is_always_in_scope(db: Session, world: dict[str, Any]) -> None:
    """A new student must never wait, whichever curriculum they are on."""
    for owner in (None, world["alice"].id, world["bob"].id):
        assert units_to_fill(db, owner=owner) == [1]


def test_one_familys_progress_does_not_widen_anothers_scope(
    db: Session, world: dict[str, Any]
) -> None:
    """The defect this replaces: Ana reaching Unit 3 queued Unit 3 fills for
    the sample and for Bob, neither of whom is near it."""
    put_student_in(db, world["ana"], world["alice_units"][2], completion=100)

    assert units_to_fill(db, owner=world["alice"].id) == [1, 3, 4]
    assert units_to_fill(db, owner=world["bob"].id) == [1]
    assert units_to_fill(db, owner=None) == [1]


def test_the_sample_is_filled_from_its_own_students(db: Session, world: dict[str, Any]) -> None:
    """It is a real curriculum with real students, not a fallback."""
    carol = User(email="carol@example.com", hashed_password="x", full_name="Carol")
    db.add(carol)
    db.flush()
    chris = Student(parent=carol, first_name="Chris", grade_level=6)
    db.add(chris)
    db.commit()
    put_student_in(db, chris, world["sample"][1], completion=100)

    assert units_to_fill(db, owner=None) == [1, 2, 3]
    assert units_to_fill(db, owner=world["alice"].id) == [1]


def test_the_next_unit_arrives_only_near_the_boundary(db: Session, world: dict[str, Any]) -> None:
    """Pre-filling the moment they start would spend hours on units a student
    who churns in week one never sees."""
    put_student_in(db, world["ana"], world["alice_units"][1], completion=33)
    assert units_to_fill(db, owner=world["alice"].id) == [1, 2], "too early for Unit 3"

    for row in db.query(SubUnitProgress).filter_by(student_id=world["ana"].id):
        row.completion_percentage = 100
    db.commit()
    assert units_to_fill(db, owner=world["alice"].id) == [1, 2, 3]


def test_scope_never_runs_past_the_end_of_a_curriculum(db: Session, world: dict[str, Any]) -> None:
    """Unit 5 does not exist, so finishing Unit 4 must not queue it."""
    put_student_in(db, world["ana"], world["alice_units"][3], completion=100)
    assert units_to_fill(db, owner=world["alice"].id) == [1, 4]


def test_an_owner_with_no_curriculum_gets_nothing(db: Session, world: dict[str, Any]) -> None:
    """Not Unit 1 by default -- there is no Unit 1 to fill."""
    assert units_to_fill(db, owner="nobody") == []


def test_grade_still_separates_curricula(db: Session, world: dict[str, Any]) -> None:
    make_curriculum(db, world["alice"], "Alice G7", grade=7)
    assert units_to_fill(db, grade_level=7, owner=world["alice"].id) == [1]
    assert units_to_fill(db, grade_level=9, owner=world["alice"].id) == []


# --------------------------------------------------------------------------- #
# Budget sharing and priming
# --------------------------------------------------------------------------- #


def stub_filling(monkeypatch: pytest.MonkeyPatch, per_slot: int = 30) -> list[tuple[str, str]]:
    """Replace generation with a counter. Returns the slots that were filled."""
    import services.scheduler_jobs as jobs

    filled: list[tuple[str, str]] = []

    def fake_fill_slot(  # noqa: ANN001 - mirrors fill_slot's own signature
        db,
        sub_unit,
        difficulty,
        target: int = 30,
        agent: Any = None,
        report: Any = None,
        **kwargs: Any,
    ) -> Any:
        report.slots_examined += 1
        report.slots_filled += 1
        report.questions_added += per_slot
        filled.append((sub_unit.unit.user_id or "sample", sub_unit.sub_unit_number))
        return report

    monkeypatch.setattr(jobs, "fill_slot", fake_fill_slot)
    monkeypatch.setattr(jobs, "QuizGeneratorAgent", lambda **kwargs: object())
    return filled


def use_test_session(monkeypatch: pytest.MonkeyPatch, db: Session) -> None:
    """Hand the job the test's session instead of opening a real one."""
    import services.scheduler_jobs as jobs

    class _Scope:
        def __enter__(self) -> Session:
            return db

        def __exit__(self, *exc: object) -> None:
            return None

    monkeypatch.setattr(jobs, "session_scope", lambda: _Scope())


def test_the_budget_is_shared_between_owners_not_multiplied(
    db: Session, world: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Giving each owner the full budget is how a background job becomes an
    unbounded bill: one run's cost would scale with the number of families."""
    from services.scheduler_jobs import refill_question_bank

    use_test_session(monkeypatch, db)
    stub_filling(monkeypatch, per_slot=30)

    report = refill_question_bank(max_questions=90)

    # Three curricula need work; the ceiling holds regardless.
    assert report.questions_added <= 90, report.summary()


def test_every_owner_gets_a_share_of_the_run(
    db: Session, world: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """No family may be starved while another is topped up."""
    from services.scheduler_jobs import refill_question_bank

    use_test_session(monkeypatch, db)
    filled = stub_filling(monkeypatch, per_slot=30)

    refill_question_bank(max_questions=90)

    assert {owner for owner, _ in filled} == {"sample", world["alice"].id, world["bob"].id}


def test_filling_stays_inside_each_owners_scope(
    db: Session, world: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ana's progress must not pull Bob's Unit 3 into the run."""
    from services.scheduler_jobs import refill_question_bank

    put_student_in(db, world["ana"], world["alice_units"][2], completion=100)
    use_test_session(monkeypatch, db)
    filled = stub_filling(monkeypatch, per_slot=10)

    refill_question_bank(max_questions=600)

    for owner, sub_unit_number in filled:
        unit = int(sub_unit_number.split(".")[0])
        if owner == world["alice"].id:
            assert unit in (1, 3, 4), f"Alice got unit {unit}"
        else:
            assert unit == 1, f"{owner} got unit {unit} without being near it"


def test_priming_fills_unit_one_only(
    db: Session, world: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Priming the whole curriculum would be ~6 hours of generation, most of
    it on units sequential unlocking makes unreachable for weeks.

    Other units are deliberately put *in scope* first -- Ana finishing Unit 2
    makes 1, 2 and 3 all eligible for the scheduler. Without that setup the
    assertion is vacuous: Unit 1 would be the only candidate anyway, and the
    test would pass with the Unit-1 filter deleted. It did, until this.
    """
    from services.scheduler_jobs import prime_new_curriculum, units_to_fill

    put_student_in(db, world["ana"], world["alice_units"][1], completion=100)
    assert units_to_fill(db, owner=world["alice"].id) == [1, 2, 3], "setup is not exercising it"

    use_test_session(monkeypatch, db)
    filled = stub_filling(monkeypatch, per_slot=30)

    prime_new_curriculum(world["alice"].id, budget=300)

    assert filled, "nothing was primed"
    assert {owner for owner, _ in filled} == {world["alice"].id}
    assert all(number.startswith("1.") for _, number in filled), filled


def test_priming_respects_its_budget(
    db: Session, world: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from services.scheduler_jobs import prime_new_curriculum

    use_test_session(monkeypatch, db)
    stub_filling(monkeypatch, per_slot=30)

    report = prime_new_curriculum(world["alice"].id, budget=60)
    assert report.questions_added <= 60 + 30, "overshot by more than one slot"


def test_priming_an_owner_with_no_curriculum_is_a_no_op(
    db: Session, world: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from services.scheduler_jobs import prime_new_curriculum

    use_test_session(monkeypatch, db)
    filled = stub_filling(monkeypatch)

    report = prime_new_curriculum("nobody")
    assert report.questions_added == 0
    assert filled == []


def test_a_zero_budget_primes_nothing(
    db: Session, world: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The lever for turning priming off entirely."""
    from services.scheduler_jobs import prime_new_curriculum

    use_test_session(monkeypatch, db)
    filled = stub_filling(monkeypatch)

    assert prime_new_curriculum(world["alice"].id, budget=0).questions_added == 0
    assert filled == []


def stub_filling_to_depth(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str, int]]:
    """Replace generation with a stock-keeper that fills exactly as far as asked.

    Unlike ``stub_filling`` it records the tier and the depth of every call, and
    adds only what a real fill would -- ``target`` minus what the slot holds --
    so a second pass over a slot tops it up rather than refilling it.
    """
    import services.scheduler_jobs as jobs

    calls: list[tuple[str, str, int]] = []
    stock: dict[tuple[str, str], int] = {}

    def fake_fill_slot(  # noqa: ANN001 - mirrors fill_slot's own signature
        db,
        sub_unit,
        difficulty,
        target: int = 30,
        agent: Any = None,
        report: Any = None,
        **kwargs: Any,
    ) -> Any:
        key = (sub_unit.sub_unit_number, difficulty.value)
        added = max(0, target - stock.get(key, 0))
        stock[key] = stock.get(key, 0) + added
        report.slots_examined += 1
        report.questions_added += added
        calls.append((sub_unit.sub_unit_number, difficulty.value, target))
        return report

    monkeypatch.setattr(jobs, "fill_slot", fake_fill_slot)
    monkeypatch.setattr(jobs, "QuizGeneratorAgent", lambda **kwargs: object())
    return calls


def test_priming_gives_every_unit_one_topic_an_easy_quiz_before_going_deeper(
    db: Session, world: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bug this guards: priming filled Topic 1.1 thirty deep in all three
    tiers and spent its whole budget there, so a child who opened 1.2 waited
    a minute or more for live generation.

    The budget here is exactly one quiz per Unit 1 topic. Filled depth-first,
    it all goes into 1.1's Easy slot and 1.2 gets nothing.
    """
    from config import settings
    from services.scheduler_jobs import prime_new_curriculum

    use_test_session(monkeypatch, db)
    calls = stub_filling_to_depth(monkeypatch)
    one_quiz = settings.QUESTIONS_PER_QUIZ

    prime_new_curriculum(world["alice"].id, budget=SUB_UNITS_PER_UNIT * one_quiz)

    assert calls == [
        ("1.1", "beginner", one_quiz),
        ("1.2", "beginner", one_quiz),
    ], calls


def test_breadth_comes_first_then_every_slot_is_topped_up(
    db: Session, world: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Easy everywhere, then Medium, then Tricky -- one quiz each -- and only
    then the full depth that buys retries."""
    from config import settings
    from services.scheduler_jobs import prime_new_curriculum

    use_test_session(monkeypatch, db)
    calls = stub_filling_to_depth(monkeypatch)
    one_quiz = settings.QUESTIONS_PER_QUIZ

    prime_new_curriculum(world["alice"].id, budget=10_000, target=30)

    breadth = calls[: 3 * SUB_UNITS_PER_UNIT]
    assert breadth == [
        ("1.1", "beginner", one_quiz),
        ("1.2", "beginner", one_quiz),
        ("1.1", "intermediate", one_quiz),
        ("1.2", "intermediate", one_quiz),
        ("1.1", "proficient", one_quiz),
        ("1.2", "proficient", one_quiz),
    ], breadth
    depth = calls[3 * SUB_UNITS_PER_UNIT :]
    assert depth and all(target == 30 for _, _, target in depth), depth
    assert {(number, tier) for number, tier, _ in depth} == {
        (number, tier) for number, tier, _ in breadth
    }, "every slot should be topped up"
