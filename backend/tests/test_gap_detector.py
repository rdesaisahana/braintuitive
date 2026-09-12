"""Gap Detector tests.

The agent's whole claim is that it says something ``/progress/skills`` cannot:
not *that* a child is weak at a skill but *what they are doing wrong*. These
tests pin down that claim and, more importantly, the two ways it could quietly
become false:

* **Inventing a gap from thin data.** Two wrong answers is a bad afternoon.
  Reporting it as a learning gap sends a child to practise something they can
  already do.
* **Losing the diagnosis when the model is down.** The evidence is gathered in
  SQL, so an outage must degrade the report to counted misconceptions, never to
  nothing and never to a fabrication.

The model is stubbed throughout; nothing here costs an API call.

Run:
    cd backend
    pytest tests/test_gap_detector.py -v
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from agents.gap_detector import (
    MIN_EVIDENCE,
    GapDetectorAgent,
    SkillEvidence,
    cluster_rationales,
)
from db.models import (
    Base,
    CurriculumSubUnit,
    CurriculumUnit,
    DifficultyLevel,
    Quiz,
    QuizQuestion,
    QuizResponse,
    QuizStatus,
    Student,
    SubUnitProgress,
    User,
)

# The rationale text a live bank actually produces, for one real question.
WRONG_SIGN = "You may have found the difference but used the wrong sign."
ADDED_INSTEAD = "You may have subtracted 3 from -7 but incorrectly treated it as -7 + 3."


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
    unit = CurriculumUnit(unit_number=1, title="Number Fluency", subject="math", grade_level=6)
    sub_unit = CurriculumSubUnit(
        unit=unit,
        sub_unit_number="1.2",
        sequence=1,
        title="Add and subtract integers",
        is_indexed=True,
        skill_tags=["add_integers", "subtract_integers"],
    )
    user = User(email="p@example.com", hashed_password="x", full_name="Parent")
    student = Student(parent=user, first_name="Aanya", last_name="R", grade_level=6)
    db.add_all([unit, sub_unit, user, student])
    db.commit()
    return {"unit": unit, "sub_unit": sub_unit, "student": student}


class StubLLM:
    """Returns a canned payload, or raises to simulate an outage."""

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


def agent_with(llm: Any) -> GapDetectorAgent:
    """A GapDetectorAgent whose model calls hit ``llm``.

    ``get_llm`` is patched rather than the constructor, so the real
    ``__init__`` still runs and a change to it would still be exercised here.
    """
    agent = GapDetectorAgent.__new__(GapDetectorAgent)
    GapDetectorAgent.__init__(agent)
    agent.get_llm = lambda: llm  # type: ignore[method-assign]
    return agent


def add_answers(
    db: Session,
    world: dict[str, Any],
    *,
    skill_tag: str,
    results: list[bool],
    chose: str | None = None,
    rationales: dict[str, str] | None = None,
    hints: int = 0,
    is_practice: bool = False,
    difficulty: DifficultyLevel = DifficultyLevel.BEGINNER,
) -> Quiz:
    """Record one quiz's worth of answers for a skill.

    ``results`` is the correct/incorrect sequence; ``chose`` is the distractor
    picked on every wrong answer, which is what makes a misconception countable.
    """
    student, sub_unit = world["student"], world["sub_unit"]
    quiz = Quiz(
        student_id=student.id,
        sub_unit_id=sub_unit.id,
        difficulty_level=difficulty,
        status=QuizStatus.COMPLETED,
        total_questions=len(results),
        passing_threshold=difficulty.threshold,
        is_practice=is_practice,
    )
    db.add(quiz)
    db.flush()

    for index, correct in enumerate(results, start=1):
        question = QuizQuestion(
            quiz_id=quiz.id,
            question_number=index,
            question_text=f"What is -7 - 3? ({skill_tag} #{index})",
            options=[{"key": key, "text": key} for key in "ABCD"],
            correct_answer="C",
            explanation="-7 - 3 = -10.",
            distractor_rationales=rationales
            or {"A": ADDED_INSTEAD, "B": WRONG_SIGN, "D": "You used a positive sign."},
            difficulty_level=difficulty,
            skill_tag=skill_tag,
            # Evidence is attributed per question, so a question with no
            # sub-unit is invisible to the analysis.
            sub_unit_id=sub_unit.id,
        )
        db.add(question)
        db.flush()
        db.add(
            QuizResponse(
                quiz_id=quiz.id,
                question_id=question.id,
                student_id=student.id,
                selected_answer="C" if correct else (chose or "B"),
                is_correct=correct,
                hint_used=index <= hints,
            )
        )

    db.commit()
    return quiz


# --------------------------------------------------------------------------- #
# Evidence gathering
# --------------------------------------------------------------------------- #


def test_evidence_counts_the_actual_answers(db: Session, world: dict[str, Any]) -> None:
    add_answers(db, world, skill_tag="subtract_integers", results=[False] * 6, chose="B")

    evidence = GapDetectorAgent.gather_evidence(db, world["student"].id)
    assert len(evidence) == 1
    assert evidence[0].skill_tag == "subtract_integers"
    assert evidence[0].questions_answered == 6
    assert evidence[0].correct == 0
    assert evidence[0].accuracy == 0.0


def test_the_specific_misconception_is_identified(db: Session, world: dict[str, Any]) -> None:
    """The whole point: *which* error, not merely that there was one.

    Six wrong answers all landing on option B mean one thing -- the child gets
    the magnitude right and the sign wrong. That is what the report must say.
    """
    add_answers(db, world, skill_tag="subtract_integers", results=[False] * 6, chose="B")

    evidence = GapDetectorAgent.gather_evidence(db, world["student"].id)[0]
    assert evidence.misconceptions == {WRONG_SIGN: 6}
    assert evidence.top_misconception() == (WRONG_SIGN, 6)


def test_mixed_misconceptions_are_ranked(db: Session, world: dict[str, Any]) -> None:
    add_answers(db, world, skill_tag="subtract_integers", results=[False] * 4, chose="B")
    add_answers(db, world, skill_tag="subtract_integers", results=[False] * 2, chose="A")

    evidence = GapDetectorAgent.gather_evidence(db, world["student"].id)[0]
    assert list(evidence.misconceptions) == [WRONG_SIGN, ADDED_INSTEAD]
    assert evidence.misconceptions == {WRONG_SIGN: 4, ADDED_INSTEAD: 2}


def test_practice_answers_are_excluded(db: Session, world: dict[str, Any]) -> None:
    """Practice is low-stakes by design; children click through it.

    Counting those would manufacture gaps that do not exist.
    """
    add_answers(db, world, skill_tag="add_integers", results=[True] * 6)
    add_answers(db, world, skill_tag="add_integers", results=[False] * 10, is_practice=True)

    evidence = GapDetectorAgent.gather_evidence(db, world["student"].id)[0]
    assert evidence.questions_answered == 6
    assert evidence.accuracy == 1.0
    assert evidence.is_weak is False


def test_weakest_skill_comes_first(db: Session, world: dict[str, Any]) -> None:
    add_answers(db, world, skill_tag="add_integers", results=[True] * 5 + [False])
    add_answers(db, world, skill_tag="subtract_integers", results=[False] * 6)

    evidence = GapDetectorAgent.gather_evidence(db, world["student"].id)
    assert [item.skill_tag for item in evidence] == ["subtract_integers", "add_integers"]


# --------------------------------------------------------------------------- #
# The evidence threshold
# --------------------------------------------------------------------------- #


def test_thin_evidence_is_never_reported_as_a_gap(db: Session, world: dict[str, Any]) -> None:
    """Below MIN_EVIDENCE, a low score is a bad afternoon, not a gap."""
    add_answers(db, world, skill_tag="subtract_integers", results=[False] * (MIN_EVIDENCE - 1))

    llm = StubLLM('{"summary": "s", "findings": []}')
    report = agent_with(llm).analyse(world["student"].id, db=db)

    assert report.gaps == []
    assert "subtract_integers" in report.skills_with_thin_evidence
    assert llm.prompts == [], "the model should not be asked about noise"


def test_enough_evidence_produces_a_gap(db: Session, world: dict[str, Any]) -> None:
    """One more answer than the previous test, and it is now a finding."""
    add_answers(db, world, skill_tag="subtract_integers", results=[False] * MIN_EVIDENCE)

    report = agent_with(StubLLM('{"summary": "s", "findings": []}')).analyse(
        world["student"].id, db=db
    )
    assert [gap.skill_tag for gap in report.gaps] == ["subtract_integers"]


def test_a_strong_student_gets_no_gaps(db: Session, world: dict[str, Any]) -> None:
    """ "No gaps" must be a real answer, not something the agent cannot say."""
    add_answers(db, world, skill_tag="add_integers", results=[True] * 8)

    llm = StubLLM('{"summary": "unused", "findings": []}')
    report = agent_with(llm).analyse(world["student"].id, db=db)

    assert report.gaps == []
    assert "add_integers" in report.strengths
    assert llm.prompts == []


def test_a_student_with_no_answers_is_reported_as_such(db: Session, world: dict[str, Any]) -> None:
    report = agent_with(StubLLM("{}")).analyse(world["student"].id, db=db)
    assert report.gaps == []
    assert report.responses_analysed == 0
    assert "not answered enough" in report.summary


def test_heavy_hint_use_is_a_gap_despite_a_good_score(db: Session, world: dict[str, Any]) -> None:
    """A child who needs a hint every time has not got it, whatever the score."""
    add_answers(db, world, skill_tag="add_integers", results=[True] * 8, hints=6)

    report = agent_with(StubLLM('{"summary": "s", "findings": []}')).analyse(
        world["student"].id, db=db
    )
    assert [gap.skill_tag for gap in report.gaps] == ["add_integers"]
    assert report.gaps[0].hints_used == 6


def test_unknown_student_is_rejected(db: Session) -> None:
    with pytest.raises(ValueError, match="No student"):
        agent_with(StubLLM("{}")).analyse("does-not-exist", db=db)


# --------------------------------------------------------------------------- #
# Severity and remediation
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("results", "severity"),
    [
        ([False] * 8, "critical"),
        ([True] * 4 + [False] * 4, "moderate"),
    ],
)
def test_severity_follows_accuracy(
    db: Session, world: dict[str, Any], results: list[bool], severity: str
) -> None:
    add_answers(db, world, skill_tag="subtract_integers", results=results)

    report = agent_with(StubLLM('{"summary": "s", "findings": []}')).analyse(
        world["student"].id, db=db
    )
    assert report.gaps[0].severity == severity


def test_a_badly_stuck_student_is_sent_back_to_beginner(db: Session, world: dict[str, Any]) -> None:
    """Failing proficient at 0% means the basics are shaky, not that they
    should retry proficient."""
    progress = SubUnitProgress(
        student_id=world["student"].id,
        sub_unit_id=world["sub_unit"].id,
        beginner_completed=True,
        intermediate_completed=True,
    )
    db.add(progress)
    add_answers(
        db,
        world,
        skill_tag="subtract_integers",
        results=[False] * 8,
        difficulty=DifficultyLevel.PROFICIENT,
    )

    report = agent_with(StubLLM('{"summary": "s", "findings": []}')).analyse(
        world["student"].id, db=db
    )
    assert report.gaps[0].recommended_difficulty is DifficultyLevel.BEGINNER


# --------------------------------------------------------------------------- #
# Interpretation
# --------------------------------------------------------------------------- #


def test_the_model_sees_the_misconceptions(db: Session, world: dict[str, Any]) -> None:
    """It must be diagnosing from the wrong options, not from the score.

    If the prompt carried only "0/6", the model could do no better than the
    dashboard already does.
    """
    add_answers(db, world, skill_tag="subtract_integers", results=[False] * 6, chose="B")

    llm = StubLLM('{"summary": "s", "findings": []}')
    agent_with(llm).analyse(world["student"].id, db=db)

    assert len(llm.prompts) == 1
    assert WRONG_SIGN in llm.prompts[0]
    assert "6x" in llm.prompts[0]


def test_findings_are_attached_to_their_skill(db: Session, world: dict[str, Any]) -> None:
    add_answers(db, world, skill_tag="subtract_integers", results=[False] * 6, chose="B")

    payload = json.dumps(
        {
            "summary": "Aanya is confident adding; subtraction signs are the issue.",
            "findings": [
                {
                    "skill_tag": "subtract_integers",
                    "likely_misconception": "Treats a - b as |a| - |b| and keeps the sign of a.",
                    "recommendation": "Work through five subtractions on a number line tonight.",
                    "recommended_difficulty": "beginner",
                }
            ],
        }
    )
    report = agent_with(StubLLM(payload)).analyse(world["student"].id, db=db)

    gap = report.gaps[0]
    assert gap.likely_misconception.startswith("Treats a - b")
    assert "number line" in gap.recommendation
    assert report.summary.startswith("Aanya is confident")


def test_evidence_is_returned_alongside_the_diagnosis(db: Session, world: dict[str, Any]) -> None:
    """A parent must be able to check the model's claim against the answers."""
    add_answers(db, world, skill_tag="subtract_integers", results=[False] * 6, chose="B")

    report = agent_with(StubLLM('{"summary": "s", "findings": []}')).analyse(
        world["student"].id, db=db
    )
    assert any(WRONG_SIGN in line for line in report.gaps[0].evidence)


def test_a_model_outage_still_yields_the_counted_misconception(
    db: Session, world: dict[str, Any]
) -> None:
    """Degrade to evidence, never to nothing and never to a guess."""
    add_answers(db, world, skill_tag="subtract_integers", results=[False] * 6, chose="B")

    report = agent_with(StubLLM(error=RuntimeError("503"))).analyse(world["student"].id, db=db)

    gap = report.gaps[0]
    assert gap.accuracy == 0.0
    assert WRONG_SIGN in gap.likely_misconception
    assert gap.recommendation
    assert "subtract_integers" in report.summary


def test_junk_from_the_model_falls_back(db: Session, world: dict[str, Any]) -> None:
    add_answers(db, world, skill_tag="subtract_integers", results=[False] * 6, chose="B")

    report = agent_with(StubLLM("I'm afraid I can't do that.")).analyse(world["student"].id, db=db)
    assert WRONG_SIGN in report.gaps[0].likely_misconception


def test_a_skill_the_model_ignored_still_gets_a_diagnosis(
    db: Session, world: dict[str, Any]
) -> None:
    """The model returning one finding for two gaps must not blank the other."""
    add_answers(db, world, skill_tag="subtract_integers", results=[False] * 6, chose="B")
    add_answers(db, world, skill_tag="add_integers", results=[False] * 6, chose="A")

    payload = json.dumps(
        {
            "summary": "s",
            "findings": [
                {
                    "skill_tag": "subtract_integers",
                    "likely_misconception": "Sign errors.",
                    "recommendation": "Number line.",
                    "recommended_difficulty": "beginner",
                }
            ],
        }
    )
    report = agent_with(StubLLM(payload)).analyse(world["student"].id, db=db)

    by_tag = {gap.skill_tag: gap for gap in report.gaps}
    assert by_tag["subtract_integers"].likely_misconception == "Sign errors."
    assert ADDED_INSTEAD in by_tag["add_integers"].likely_misconception


def test_a_nonsense_difficulty_is_ignored(db: Session, world: dict[str, Any]) -> None:
    """The model may invent a tier; the deterministic choice must survive it."""
    add_answers(db, world, skill_tag="subtract_integers", results=[False] * 6)

    payload = json.dumps(
        {
            "summary": "s",
            "findings": [
                {
                    "skill_tag": "subtract_integers",
                    "likely_misconception": "m",
                    "recommendation": "r",
                    "recommended_difficulty": "expert",
                }
            ],
        }
    )
    report = agent_with(StubLLM(payload)).analyse(world["student"].id, db=db)
    assert report.gaps[0].recommended_difficulty is DifficultyLevel.BEGINNER


# --------------------------------------------------------------------------- #
# SkillEvidence arithmetic
# --------------------------------------------------------------------------- #


def test_empty_evidence_does_not_divide_by_zero() -> None:
    evidence = SkillEvidence(
        skill_tag="x",
        sub_unit_id="s",
        sub_unit_number="1.1",
        sub_unit_title="t",
        unit_number=1,
        questions_answered=0,
        correct=0,
        hints_used=0,
    )
    assert evidence.accuracy == 0.0
    assert evidence.hint_rate == 0.0
    assert evidence.is_weak is False


# --------------------------------------------------------------------------- #
# Grouping rationales that name the same error
# --------------------------------------------------------------------------- #

# Verbatim from a live run: one misconception, four phrasings.
LIVE_PHRASINGS = [
    "You multiplied correctly but forgot that a positive times a negative "
    "gives a negative result.",
    "You multiplied correctly but forgot that a negative times a positive "
    "must give a negative result.",
    "You multiplied correctly but forgot that a negative times a positive is negative.",
]


def test_one_error_phrased_many_ways_is_counted_once() -> None:
    """Counted verbatim these read as three unrelated slips, not one habit."""
    clustered = cluster_rationales(LIVE_PHRASINGS)
    assert len(clustered) == 1
    assert next(iter(clustered.values())) == 3


def test_the_shortest_phrasing_is_kept_as_the_label() -> None:
    """Longer phrasings carry one question's numbers, which do not generalise."""
    label = next(iter(cluster_rationales(LIVE_PHRASINGS)))
    assert label == (
        "You multiplied correctly but forgot that a negative times a positive is negative."
    )


def test_different_errors_are_never_merged() -> None:
    """Merging two real mistakes sends a child to practise the wrong thing."""
    reasons = [
        "You may have found the difference but used the wrong sign.",
        "You may have added the absolute values and used a positive sign.",
        "You multiplied correctly but forgot that a positive times a negative is negative.",
    ]
    assert len(cluster_rationales(reasons)) == 3


def test_opposite_errors_stay_apart() -> None:
    """These share every content word but mean opposite things.

    Merging them would tell a parent the wrong direction of the error.
    """
    clustered = cluster_rationales(
        ["You added instead of subtracting.", "You subtracted instead of adding."]
    )
    assert len(clustered) == 2


def test_clustering_survives_junk() -> None:
    assert cluster_rationales([]) == {}
    assert cluster_rationales(["", "   "]) == {}
    assert cluster_rationales(["Wrong sign.", "Wrong sign."]) == {"Wrong sign.": 2}


def test_clusters_are_ordered_by_frequency() -> None:
    clustered = cluster_rationales([*LIVE_PHRASINGS, "You added instead of subtracting."])
    assert list(clustered.values()) == [3, 1]
