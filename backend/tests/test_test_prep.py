"""Test Prep tests.

The plan is the product, and a plan is wrong in ways that are easy to miss:
it looks perfectly reasonable while sending a child to revise the thing they
already know. So these tests are mostly about the ordering being *right*, not
merely present.

The properties that matter:

* a topic never started outranks everything -- you cannot have retained what
  you never learned;
* a topic passed two months ago outranks the identical topic passed yesterday,
  which is the signal the progress dashboard structurally cannot show;
* every topic in scope gets questions, because a test can ask about any of it;
* no single topic eats the evening;
* the riskiest work lands early, while there is still time to fix it.

The model is stubbed throughout; nothing here costs an API call.

Run:
    cd backend
    pytest tests/test_test_prep.py -v
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from agents.test_prep import (
    MAX_QUESTIONS_PER_SESSION,
    MAX_TOPIC_SHARE,
    MIN_QUESTIONS_PER_TOPIC,
    DrillBlock,
    TestPrepAgent,
    TopicRisk,
    allocate,
    decay_factor,
    schedule,
    target_difficulty,
)
from db.models import (
    Base,
    CurriculumSubUnit,
    CurriculumUnit,
    DifficultyLevel,
    Quiz,
    QuizAttempt,
    QuizQuestion,
    QuizStatus,
    Student,
    SubUnitProgress,
    User,
)

NOW = datetime(2026, 9, 9, tzinfo=UTC)


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


@pytest.fixture()
def world(db: Session) -> dict[str, Any]:
    """One unit, four sub-units, one student -- no progress yet.

    The unit is owned by the parent. Curriculum is per family with no shared
    fallback, so an unowned unit is invisible to every student and the agent
    would correctly find nothing to plan.
    """
    user = User(email="p@example.com", hashed_password="x", full_name="Parent")
    db.add(user)
    db.flush()
    unit = CurriculumUnit(
        user_id=user.id,
        unit_number=1,
        title="Number Fluency",
        subject="math",
        grade_level=6,
    )
    subs = [
        CurriculumSubUnit(
            unit=unit,
            sub_unit_number=f"1.{index + 1}",
            sequence=index,
            title=title,
            is_indexed=True,
            skill_tags=["skill_one", "skill_two"],
        )
        for index, title in enumerate(
            ["Absolute value", "Add integers", "Multiply integers", "GCF and LCM"]
        )
    ]
    student = Student(parent=user, first_name="Aanya", last_name="R", grade_level=6)
    db.add_all([unit, *subs, student])
    db.commit()
    return {"unit": unit, "subs": subs, "student": student}


class StubLLM:
    def __init__(self, content: str | None = None, error: Exception | None = None) -> None:
        self.content = content
        self.error = error
        self.prompts: list[str] = []

    def invoke(self, messages: list[dict[str, str]]) -> Any:  # noqa: ANN401
        self.prompts.append(messages[-1]["content"])
        if self.error is not None:
            raise self.error

        class _Response:
            def __init__(self, text: str) -> None:
                self.content = text

        return _Response(self.content or "")


def agent_with(llm: Any) -> TestPrepAgent:
    agent = TestPrepAgent.__new__(TestPrepAgent)
    TestPrepAgent.__init__(agent)
    agent.get_llm = lambda: llm  # type: ignore[method-assign]
    return agent


EMPTY_PLAN = json.dumps({"summary": "s", "sessions": [], "advice": []})


def give_progress(
    db: Session,
    world: dict[str, Any],
    index: int,
    *,
    completion: int = 100,
    tiers: tuple[str, ...] = ("beginner", "intermediate", "proficient"),
    days_ago: int = 1,
    attempts: int = 1,
    score: float = 95.0,
) -> SubUnitProgress:
    """Mark a sub-unit as worked on, ``days_ago`` days before ``NOW``."""
    sub_unit = world["subs"][index]
    progress = SubUnitProgress(
        student_id=world["student"].id,
        sub_unit_id=sub_unit.id,
        completion_percentage=completion,
        total_attempts=attempts,
    )
    for tier in tiers:
        setattr(progress, f"{tier}_completed", True)
        setattr(progress, f"{tier}_best_score", score)
    db.add(progress)

    when = NOW - timedelta(days=days_ago)
    quiz = Quiz(
        student_id=world["student"].id,
        sub_unit_id=sub_unit.id,
        difficulty_level=DifficultyLevel.BEGINNER,
        status=QuizStatus.COMPLETED,
        total_questions=10,
        passing_threshold=70.0,
    )
    db.add(quiz)
    db.flush()
    # "Last practised" is measured per question, so an attempt with no
    # questions refreshes nothing.
    db.add(
        QuizQuestion(
            quiz_id=quiz.id,
            question_number=1,
            question_text=f"Question on {sub_unit.sub_unit_number}",
            correct_answer="A",
            explanation="Because.",
            difficulty_level=DifficultyLevel.BEGINNER,
            sub_unit_id=sub_unit.id,
        )
    )
    db.add(
        QuizAttempt(
            student_id=world["student"].id,
            sub_unit_id=sub_unit.id,
            quiz_id=quiz.id,
            difficulty_level=DifficultyLevel.BEGINNER,
            total_questions=10,
            correct_count=int(score / 10),
            score_percentage=score,
            is_passed=score >= 70,
            completed_at=when,
        )
    )
    db.commit()
    return progress


# --------------------------------------------------------------------------- #
# Decay
# --------------------------------------------------------------------------- #


def test_decay_grows_with_time() -> None:
    assert decay_factor(0) == 0.0
    assert decay_factor(1) < decay_factor(7) < decay_factor(30)


def test_decay_is_bounded() -> None:
    """An ever-growing score would swamp every other signal."""
    assert 0.0 <= decay_factor(365) <= 1.0
    assert decay_factor(365) == decay_factor(60), "capped past the ceiling"


def test_never_practised_is_maximum_decay() -> None:
    assert decay_factor(None) == 1.0


def test_the_first_fortnight_matters_most() -> None:
    """The curve is concave: the jump from day 1 to 14 beats day 30 to 60."""
    early = decay_factor(14) - decay_factor(1)
    late = decay_factor(60) - decay_factor(30)
    assert early > late


# --------------------------------------------------------------------------- #
# Ranking
# --------------------------------------------------------------------------- #


def test_a_topic_never_started_outranks_everything(db: Session, world: dict[str, Any]) -> None:
    """You cannot have retained what you never learned."""
    for index in range(3):
        give_progress(db, world, index, days_ago=45)
    # Sub-unit 1.4 left untouched.

    risks = TestPrepAgent.assess(db, world["student"].id, [1], now=NOW)
    assert risks[0].sub_unit_number == "1.4"
    assert risks[0].never_attempted is True


def test_a_stale_pass_outranks_a_fresh_one(db: Session, world: dict[str, Any]) -> None:
    """The signal the progress dashboard cannot show.

    Both sub-units read ``100% complete``. One was passed yesterday and one
    two months ago, and they are not the same thing going into a test.
    """
    give_progress(db, world, 0, days_ago=1)
    give_progress(db, world, 1, days_ago=60)

    risks = {
        risk.sub_unit_number: risk
        for risk in TestPrepAgent.assess(db, world["student"].id, [1], now=NOW)
    }
    assert risks["1.2"].risk > risks["1.1"].risk
    assert any("not practised" in reason.lower() for reason in risks["1.2"].reasons)


def test_a_partial_topic_outranks_a_complete_one(db: Session, world: dict[str, Any]) -> None:
    give_progress(db, world, 0, completion=100, days_ago=5)
    give_progress(db, world, 1, completion=33, tiers=("beginner",), days_ago=5)

    risks = {
        risk.sub_unit_number: risk
        for risk in TestPrepAgent.assess(db, world["student"].id, [1], now=NOW)
    }
    assert risks["1.2"].risk > risks["1.1"].risk


def test_reasons_are_always_given(db: Session, world: dict[str, Any]) -> None:
    """A plan a parent cannot argue with is one they cannot trust."""
    give_progress(db, world, 0, days_ago=1)
    for risk in TestPrepAgent.assess(db, world["student"].id, [1], now=NOW):
        assert risk.reasons, f"{risk.sub_unit_number} has no stated reason"


def test_scope_defaults_to_units_actually_worked_in(db: Session, world: dict[str, Any]) -> None:
    """Revising a unit the child has never opened is not revision."""
    unit2 = CurriculumUnit(
        user_id=world["student"].user_id,
        unit_number=2,
        title="Expressions",
        subject="math",
        grade_level=6,
    )
    db.add(unit2)
    db.add(
        CurriculumSubUnit(
            unit=unit2, sub_unit_number="2.1", sequence=0, title="Expressions", is_indexed=True
        )
    )
    db.commit()
    give_progress(db, world, 0, days_ago=3)

    risks = TestPrepAgent.assess(db, world["student"].id, now=NOW)
    assert {risk.unit_number for risk in risks} == {1}


def test_an_explicit_scope_includes_untouched_units(db: Session, world: dict[str, Any]) -> None:
    """ "She has a test on Unit 2 on Friday" must work even before she starts it."""
    unit2 = CurriculumUnit(
        user_id=world["student"].user_id,
        unit_number=2,
        title="Expressions",
        subject="math",
        grade_level=6,
    )
    db.add(unit2)
    db.add(
        CurriculumSubUnit(
            unit=unit2, sub_unit_number="2.1", sequence=0, title="Expressions", is_indexed=True
        )
    )
    db.commit()

    risks = TestPrepAgent.assess(db, world["student"].id, [2], now=NOW)
    assert [risk.sub_unit_number for risk in risks] == ["2.1"]
    assert risks[0].never_attempted is True


# --------------------------------------------------------------------------- #
# Which tier to drill
# --------------------------------------------------------------------------- #


def test_untouched_topics_start_at_beginner() -> None:
    assert target_difficulty(None) is DifficultyLevel.BEGINNER


def test_drilling_targets_the_first_uncleared_tier() -> None:
    progress = SubUnitProgress(student_id="s", sub_unit_id="u", beginner_completed=True)
    assert target_difficulty(progress) is DifficultyLevel.INTERMEDIATE


def test_a_mastered_topic_is_revised_at_the_top_tier() -> None:
    """A school test will not ask beginner questions.

    Sending a child who aced everything back to beginner is busywork dressed
    up as revision.
    """
    progress = SubUnitProgress(
        student_id="s",
        sub_unit_id="u",
        beginner_completed=True,
        intermediate_completed=True,
        proficient_completed=True,
    )
    assert target_difficulty(progress) is DifficultyLevel.PROFICIENT


# --------------------------------------------------------------------------- #
# Allocation
# --------------------------------------------------------------------------- #


def make_risks(*values: float) -> list[TopicRisk]:
    return [
        TopicRisk(
            sub_unit_id=f"s{index}",
            sub_unit_number=f"1.{index + 1}",
            sub_unit_title=f"Topic {index}",
            unit_number=1,
            never_attempted=False,
            completion_percentage=100,
            days_since_practice=1,
            best_score=90.0,
            weakest_skill=None,
            weakest_accuracy=None,
            hint_rate=0.0,
            risk=value,
        )
        for index, value in enumerate(values)
    ]


def test_every_topic_in_scope_gets_questions() -> None:
    """A test can ask about anything in scope; a zero-question topic is one
    the child walks in never having looked at."""
    allocation = allocate(make_risks(0.9, 0.1, 0.05, 0.01), budget=30)
    assert len(allocation) == 4
    assert all(count >= MIN_QUESTIONS_PER_TOPIC for count in allocation.values())


def test_the_riskiest_topic_gets_the_most() -> None:
    allocation = allocate(make_risks(0.9, 0.2, 0.1), budget=30)
    assert allocation["s0"] > allocation["s1"] >= allocation["s2"]


def test_no_topic_eats_the_plan() -> None:
    """The other topics still appear on the paper."""
    allocation = allocate(make_risks(1.0, 0.01, 0.01), budget=30)
    assert allocation["s0"] <= int(30 * MAX_TOPIC_SHARE)


def test_the_budget_is_not_exceeded() -> None:
    for budget in (6, 10, 30, 47):
        allocation = allocate(make_risks(0.9, 0.5, 0.3, 0.2, 0.1), budget=budget)
        assert sum(allocation.values()) <= budget, budget


def test_a_thin_budget_drops_topics_rather_than_spreading_itself() -> None:
    """Fewer topics done properly beats every topic done uselessly."""
    allocation = allocate(make_risks(0.9, 0.8, 0.7, 0.6, 0.5), budget=4)
    assert len(allocation) == 2
    assert set(allocation) == {"s0", "s1"}, "the riskiest are the ones kept"


def test_allocation_handles_nothing() -> None:
    assert allocate([], budget=30) == {}
    assert allocate(make_risks(0.5), budget=0) == {}


def test_zero_risk_topics_still_get_the_floor() -> None:
    """Everything solid is a real state, and coverage still applies."""
    allocation = allocate(make_risks(0.0, 0.0), budget=20)
    assert set(allocation.values()) == {MIN_QUESTIONS_PER_TOPIC}


# --------------------------------------------------------------------------- #
# Scheduling
# --------------------------------------------------------------------------- #


def make_blocks(*pairs: tuple[float, int]) -> list[DrillBlock]:
    return [
        DrillBlock(
            sub_unit_id=f"s{index}",
            sub_unit_number=f"1.{index + 1}",
            sub_unit_title=f"Topic {index}",
            unit_number=1,
            difficulty=DifficultyLevel.BEGINNER,
            question_count=count,
            risk=risk,
        )
        for index, (risk, count) in enumerate(pairs)
    ]


def test_the_riskiest_work_lands_first() -> None:
    """A topic the child collapses on should surface while there is still
    time to do something about it."""
    sessions = schedule(make_blocks((0.2, 4), (0.9, 4), (0.5, 4)), days=3)
    assert sessions[0].blocks[0].risk == 0.9


def test_every_evening_gets_work() -> None:
    sessions = schedule(make_blocks((0.9, 4), (0.5, 4), (0.3, 4), (0.1, 4)), days=4)
    assert len(sessions) == 4
    assert all(session.question_count > 0 for session in sessions)


def test_one_evening_holds_everything_when_the_test_is_tomorrow() -> None:
    sessions = schedule(make_blocks((0.9, 4), (0.5, 4)), days=1)
    assert len(sessions) == 1
    assert sessions[0].question_count == 8


def test_empty_days_are_not_returned() -> None:
    """Two blocks over five days is three empty evenings, not three sessions."""
    sessions = schedule(make_blocks((0.9, 4), (0.5, 4)), days=5)
    assert len(sessions) == 2
    assert [session.day for session in sessions] == [1, 2]


def test_no_session_is_unreasonably_long() -> None:
    sessions = schedule(make_blocks(*[(0.5, 6)] * 6), days=3)
    assert all(session.question_count <= MAX_QUESTIONS_PER_SESSION for session in sessions), [
        s.question_count for s in sessions
    ]


def test_scheduling_nothing_returns_nothing() -> None:
    assert schedule([], days=3) == []


# --------------------------------------------------------------------------- #
# The plan
# --------------------------------------------------------------------------- #


def test_a_plan_is_built_end_to_end(db: Session, world: dict[str, Any]) -> None:
    give_progress(db, world, 0, days_ago=30)
    give_progress(db, world, 1, completion=33, tiers=("beginner",), days_ago=2)

    payload = json.dumps(
        {
            "summary": "Aanya is in decent shape; multiplying integers is the risk.",
            "sessions": [{"day": 1, "focus": "Sign rules, slowly."}],
            "advice": ["Do these at the table, not on a phone."],
        }
    )
    plan = agent_with(StubLLM(payload)).plan(
        world["student"].id, unit_numbers=[1], days_until_test=3, question_budget=20, db=db, now=NOW
    )

    assert plan.sessions
    assert plan.total_questions <= 20
    assert plan.summary.startswith("Aanya is in decent shape")
    assert plan.sessions[0].focus == "Sign rules, slowly."
    assert plan.advice == ["Do these at the table, not on a phone."]


def test_untouched_topics_are_named(db: Session, world: dict[str, Any]) -> None:
    give_progress(db, world, 0, days_ago=2)

    plan = agent_with(StubLLM(EMPTY_PLAN)).plan(
        world["student"].id, unit_numbers=[1], days_until_test=3, db=db, now=NOW
    )
    assert set(plan.topics_not_yet_started) == {"1.2", "1.3", "1.4"}


def test_a_model_outage_still_yields_a_usable_plan(db: Session, world: dict[str, Any]) -> None:
    """The schedule is computed here; only the prose is the model's."""
    give_progress(db, world, 0, days_ago=40)

    plan = agent_with(StubLLM(error=RuntimeError("503"))).plan(
        world["student"].id, unit_numbers=[1], days_until_test=3, db=db, now=NOW
    )
    assert plan.sessions
    assert plan.total_questions > 0
    assert plan.summary
    assert all(session.focus for session in plan.sessions)


def test_junk_from_the_model_falls_back(db: Session, world: dict[str, Any]) -> None:
    give_progress(db, world, 0, days_ago=40)

    plan = agent_with(StubLLM("I'm afraid I can't do that.")).plan(
        world["student"].id, unit_numbers=[1], days_until_test=2, db=db, now=NOW
    )
    assert plan.summary
    assert all(session.focus for session in plan.sessions)


def test_a_student_with_no_work_gets_told_so(db: Session, world: dict[str, Any]) -> None:
    """Not an empty plan dressed up as a real one."""
    llm = StubLLM(EMPTY_PLAN)
    plan = agent_with(llm).plan(world["student"].id, days_until_test=3, db=db, now=NOW)

    assert plan.sessions == []
    assert "nothing to revise" in plan.summary.lower()
    assert llm.prompts == [], "the model should not be asked to plan nothing"


def test_the_model_sees_the_reasons_not_just_the_topics(db: Session, world: dict[str, Any]) -> None:
    """Otherwise its focus lines can only restate the schedule."""
    give_progress(db, world, 0, completion=33, tiers=("beginner",), days_ago=40)

    llm = StubLLM(EMPTY_PLAN)
    agent_with(llm).plan(world["student"].id, unit_numbers=[1], days_until_test=3, db=db, now=NOW)

    prompt = llm.prompts[0]
    assert "Not practised for 40 days" in prompt
    assert "33% complete" in prompt


def test_an_unstocked_bank_is_flagged(db: Session, world: dict[str, Any]) -> None:
    """A plan that quietly promises undeliverable drills is worse than one
    that says so."""
    give_progress(db, world, 0, days_ago=40)

    plan = agent_with(StubLLM(EMPTY_PLAN)).plan(
        world["student"].id, unit_numbers=[1], days_until_test=3, db=db, now=NOW
    )
    assert any("not pre-generated" in warning for warning in plan.warnings)
    assert all(not block.bank_ready for session in plan.sessions for block in session.blocks)


def test_unknown_student_is_rejected(db: Session) -> None:
    with pytest.raises(ValueError, match="No student"):
        agent_with(StubLLM(EMPTY_PLAN)).plan("does-not-exist", db=db, now=NOW)


@pytest.mark.parametrize("days", [-5, 0, 1, 400])
def test_absurd_timeframes_are_clamped(db: Session, world: dict[str, Any], days: int) -> None:
    give_progress(db, world, 0, days_ago=10)

    plan = agent_with(StubLLM(EMPTY_PLAN)).plan(
        world["student"].id, unit_numbers=[1], days_until_test=days, db=db, now=NOW
    )
    assert 1 <= plan.days_until_test <= 30
    assert len(plan.sessions) <= plan.days_until_test


def test_the_plan_never_moves_progress(db: Session, world: dict[str, Any]) -> None:
    """Planning is analysis. Nothing about it may change a child's record."""
    give_progress(db, world, 0, completion=33, tiers=("beginner",), days_ago=10)
    before = {
        row.sub_unit_id: (row.completion_percentage, row.total_attempts)
        for row in db.query(SubUnitProgress).all()
    }

    agent_with(StubLLM(EMPTY_PLAN)).plan(
        world["student"].id, unit_numbers=[1], days_until_test=3, db=db, now=NOW
    )

    after = {
        row.sub_unit_id: (row.completion_percentage, row.total_attempts)
        for row in db.query(SubUnitProgress).all()
    }
    assert before == after


# --------------------------------------------------------------------------- #
# The dress rehearsal
# --------------------------------------------------------------------------- #


def test_a_worked_unit_is_offered_as_a_rehearsal(db: Session, world: dict[str, Any]) -> None:
    """Drills are blocked by construction. The night before a test, a child
    needs one interleaved paper instead."""
    for index in range(4):
        give_progress(db, world, index, days_ago=5)

    plan = agent_with(StubLLM(EMPTY_PLAN)).plan(
        world["student"].id, unit_numbers=[1], days_until_test=3, db=db, now=NOW
    )
    assert plan.rehearsal_unit_ids == [world["unit"].id]


def test_an_unstarted_unit_is_not_offered_as_a_rehearsal(
    db: Session, world: dict[str, Any]
) -> None:
    """Sitting a cumulative paper on material they have not met measures
    nothing, and is a discouraging way to spend the evening before a test."""
    give_progress(db, world, 0, days_ago=5)  # 1.2-1.4 never started

    plan = agent_with(StubLLM(EMPTY_PLAN)).plan(
        world["student"].id, unit_numbers=[1], days_until_test=3, db=db, now=NOW
    )
    assert plan.rehearsal_unit_ids == []
