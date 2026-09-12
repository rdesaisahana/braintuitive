"""Phase 3 Quiz Generator tests.

Validation rules, the deterministic generation loop, persistence and the
sequential-unit gate. The LLM and the retriever are stubbed, so the suite runs
offline and deterministically -- no API spend to run the tests.

Run:
    cd backend
    pytest tests/test_quiz_generator.py -v
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from agents.question_verifier import QuestionVerdict, Verdict, VerificationReport
from agents.quiz_generator import (
    question_type_of,
    GeneratedQuestion,
    GeneratedQuiz,
    QuizGenerationError,
    QuizGeneratorAgent,
    find_ambiguous_stem,
    find_meta_commentary,
    rebalance_answer_keys,
    spec_for,
    validate_answer_distribution,
    validate_question,
)
from db.models import (
    QuestionType,
    Base,
    CurriculumSubUnit,
    CurriculumUnit,
    DifficultyLevel,
    Quiz,
    QuizQuestion,
    QuizStatus,
    Student,
    SubUnitProgress,
    User,
)
from rag.pinecone_client import SearchHit
from rag.retrieval import RetrievalContext
from services.quiz_service import (
    QuizServiceError,
    UnitLockedError,
    check_unit_unlocked,
    create_quiz,
    persist_quiz,
)

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
def curriculum(db: Session) -> dict[str, Any]:
    """Two units, so unit-locking can be exercised."""
    unit1 = CurriculumUnit(unit_number=1, title="Number Fluency", subject="math", grade_level=6)
    unit2 = CurriculumUnit(unit_number=2, title="Expressions", subject="math", grade_level=6)
    sub11 = CurriculumSubUnit(
        unit=unit1,
        sub_unit_number="1.1",
        sequence=0,
        title="Absolute values of integers",
        description="Identify, describe, and find the absolute values of integers.",
        is_indexed=True,
        # Stored vocabulary, as production has once derived. Without it,
        # ensure_vocabulary would call the stubbed LLM and eat a question batch.
        skill_tags=["find_absolute_value", "compare_absolute_values"],
    )
    sub12 = CurriculumSubUnit(
        unit=unit1,
        sub_unit_number="1.2",
        sequence=1,
        title="Add and subtract integers",
        description="Fluently add, subtract, multiply and divide integers.",
        is_indexed=True,
        skill_tags=["add_integers", "subtract_integers", "multiply_integers"],
    )
    sub21 = CurriculumSubUnit(
        unit=unit2,
        sub_unit_number="2.1",
        sequence=0,
        title="Equivalent expressions",
        description="Apply properties of operations to generate equivalent expressions.",
        is_indexed=True,
        skill_tags=["apply_properties", "generate_equivalent_expressions"],
    )
    db.add_all([unit1, unit2, sub11, sub12, sub21])
    db.commit()
    return {"unit1": unit1, "unit2": unit2, "sub11": sub11, "sub12": sub12, "sub21": sub21}


@pytest.fixture()
def student(db: Session) -> Student:
    user = User(email="p@example.com", hashed_password="x", full_name="Parent")
    child = Student(parent=user, first_name="Aanya", grade_level=6)
    db.add_all([user, child])
    db.commit()
    return child


def make_raw_question(number: int = 1, correct: str = "A", **overrides: Any) -> dict[str, Any]:
    """A structurally valid raw question, for mutation in tests."""
    raw: dict[str, Any] = {
        "question_text": f"What is -3 + {number + 7}?",
        "options": [
            {"key": "A", "text": "5"},
            {"key": "B", "text": "-5"},
            {"key": "C", "text": "11"},
            {"key": "D", "text": "-11"},
        ],
        "correct_answer": correct,
        "explanation": ("Start at -3 on the number line and move 8 units right, landing on 5."),
        "distractor_rationales": {
            key: f"This comes from a sign error at step {key}."
            for key in ("A", "B", "C", "D")
            if key != correct
        },
        "hint": "Think about which direction you move for a positive number.",
        "skill_tag": "add_integers",
    }
    raw.update(overrides)
    return raw


class StubLLM:
    """Returns pre-scripted JSON payloads, one per invoke() call."""

    def __init__(self, payloads: list[Any]) -> None:
        self.payloads = list(payloads)
        self.calls: list[list[dict[str, str]]] = []

    def invoke(self, messages: list[dict[str, str]]) -> Any:  # noqa: ANN401
        self.calls.append(messages)
        payload = self.payloads.pop(0) if self.payloads else []
        content = payload if isinstance(payload, str) else json.dumps(payload)

        class _Response:
            def __init__(self, text: str) -> None:
                self.content = text

        return _Response(content)


class StubRetriever:
    """Returns a fixed, non-empty retrieval context."""

    def __init__(self, empty: bool = False) -> None:
        self.empty = empty
        self.calls: list[dict[str, Any]] = []

    def for_sub_unit(self, **kwargs: Any) -> RetrievalContext:
        self.calls.append(kwargs)
        if self.empty:
            return RetrievalContext(query="q", hits=[])
        return RetrievalContext(
            query="q",
            hits=[
                SearchHit(
                    chunk_id="chunk-1",
                    score=0.81,
                    text="Fluently add, subtract, multiply and divide integers.",
                    metadata={
                        "unit_number": 1.0,
                        "sub_unit_number": "1.2",
                        "page_start": 5.0,
                        "page_end": 8.0,
                    },
                ),
                SearchHit(
                    chunk_id="chunk-2",
                    score=0.66,
                    text="Apply properties of operations to rational numbers.",
                    metadata={"unit_number": 1.0, "page_start": 5.0, "page_end": 8.0},
                ),
            ],
        )


def build_agent(payloads: list[Any], empty_retrieval: bool = False) -> QuizGeneratorAgent:
    """A QuizGeneratorAgent wired to stubs."""
    agent = QuizGeneratorAgent(
        retriever=StubRetriever(empty=empty_retrieval), batch_size=5, max_attempts=3
    )
    agent._llm = StubLLM(payloads)  # bypass the real Nebius client
    return agent


# --------------------------------------------------------------------------- #
# Difficulty specifications
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("level", "threshold"),
    [("beginner", 70.0), ("intermediate", 80.0), ("proficient", 90.0)],
)
def test_spec_thresholds_match_progression_rules(level: str, threshold: float) -> None:
    assert spec_for(level).threshold == threshold


def test_specs_describe_different_cognitive_demands() -> None:
    """Three tiers must differ in kind, not just in number size."""
    demands = {
        spec_for(level).cognitive_demand for level in ("beginner", "intermediate", "proficient")
    }
    assert len(demands) == 3


def test_spec_accepts_enum_and_string() -> None:
    assert spec_for(DifficultyLevel.PROFICIENT) is spec_for("proficient")


def test_unknown_difficulty_rejected() -> None:
    with pytest.raises(ValueError):
        spec_for("impossible")


# --------------------------------------------------------------------------- #
# Question validation
# --------------------------------------------------------------------------- #


def test_valid_question_passes() -> None:
    assert validate_question(make_raw_question()) == []


def test_rejects_wrong_option_count() -> None:
    raw = make_raw_question()
    raw["options"] = raw["options"][:3]
    assert any("exactly 4" in issue for issue in validate_question(raw))


def test_rejects_bad_option_keys() -> None:
    raw = make_raw_question()
    raw["options"][3]["key"] = "E"
    assert any("A,B,C,D" in issue for issue in validate_question(raw))


def test_rejects_correct_answer_not_in_options() -> None:
    raw = make_raw_question(correct="A")
    raw["correct_answer"] = "Z"
    assert any("not one of" in issue for issue in validate_question(raw))


def test_rejects_duplicate_options() -> None:
    raw = make_raw_question()
    raw["options"][1]["text"] = raw["options"][0]["text"]
    assert any("duplicates" in issue for issue in validate_question(raw))


def test_rejects_missing_explanation() -> None:
    """Feedback is the product; a question without it is not shippable."""
    raw = make_raw_question(explanation="Nope.")
    assert any("explanation" in issue for issue in validate_question(raw))


def test_rejects_missing_distractor_rationale() -> None:
    raw = make_raw_question(correct="A")
    del raw["distractor_rationales"]["C"]
    issues = validate_question(raw)
    assert any("missing keys" in issue and "C" in issue for issue in issues)


def test_rejects_stub_distractor_rationale() -> None:
    raw = make_raw_question(correct="A")
    raw["distractor_rationales"]["B"] = "wrong"
    assert any("too short" in issue for issue in validate_question(raw))


def test_rejects_short_question_text() -> None:
    assert any(
        "question_text" in issue
        for issue in validate_question(make_raw_question(question_text="?"))
    )


# --------------------------------------------------------------------------- #
# Answer-key distribution
# --------------------------------------------------------------------------- #


def _questions(keys: list[str]) -> list[GeneratedQuestion]:
    return [
        GeneratedQuestion(
            question_number=index + 1,
            question_text=f"q{index}",
            options=[{"key": k, "text": k} for k in ("A", "B", "C", "D")],
            correct_answer=key,
            explanation="because",
            distractor_rationales={},
        )
        for index, key in enumerate(keys)
    ]


def test_lopsided_answer_key_is_flagged() -> None:
    """Eight of ten answers on 'C' is guessable without reading anything."""
    warnings = validate_answer_distribution(_questions(list("CCCCCCCCAB")))
    assert warnings and "guessable" in warnings[0]


def test_balanced_answer_key_passes() -> None:
    assert validate_answer_distribution(_questions(list("ABCDABCDAB"))) == []


def test_distribution_ignored_for_tiny_sets() -> None:
    assert validate_answer_distribution(_questions(list("AA"))) == []


# --------------------------------------------------------------------------- #
# Generation loop
# --------------------------------------------------------------------------- #


def test_generates_full_set(db: Session, curriculum: dict[str, Any]) -> None:
    batch1 = [make_raw_question(n, correct=k) for n, k in enumerate("ABCDA", 1)]
    batch2 = [make_raw_question(n, correct=k) for n, k in enumerate("BCDAB", 6)]
    agent = build_agent([batch1, batch2])

    quiz = agent.generate(sub_unit="1.2", difficulty="beginner", count=10, db=db)

    assert quiz.is_complete
    assert len(quiz.questions) == 10
    assert [q.question_number for q in quiz.questions] == list(range(1, 11))
    assert quiz.difficulty is DifficultyLevel.BEGINNER
    assert quiz.warnings == []


def test_invalid_questions_are_discarded_and_topped_up(
    db: Session, curriculum: dict[str, Any]
) -> None:
    bad = make_raw_question(1, explanation="no")
    good_batch = [make_raw_question(n, correct=k) for n, k in enumerate("ABCD", 2)]
    agent = build_agent([[bad], good_batch])

    quiz = agent.generate(sub_unit="1.2", difficulty="beginner", count=4, db=db)

    assert len(quiz.questions) == 4
    assert all(len(q.explanation) >= 20 for q in quiz.questions)
    assert quiz.attempts == 2


def test_duplicate_questions_are_dropped(db: Session, curriculum: dict[str, Any]) -> None:
    repeated = make_raw_question(1, correct="A")
    agent = build_agent([[repeated, dict(repeated)], [make_raw_question(2, correct="B")]])

    quiz = agent.generate(sub_unit="1.2", difficulty="beginner", count=2, db=db)

    assert len(quiz.questions) == 2
    texts = {q.question_text for q in quiz.questions}
    assert len(texts) == 2


def test_partial_set_returns_with_warning(db: Session, curriculum: dict[str, Any]) -> None:
    """Nine good questions beat a hard failure."""
    agent = build_agent([[make_raw_question(1)], [], []])

    quiz = agent.generate(sub_unit="1.2", difficulty="beginner", count=5, db=db)

    assert len(quiz.questions) == 1
    assert not quiz.is_complete
    assert any("passed validation" in w for w in quiz.warnings)


def test_no_valid_questions_raises(db: Session, curriculum: dict[str, Any]) -> None:
    agent = build_agent([[], [], []])
    with pytest.raises(QuizGenerationError, match="No valid questions"):
        agent.generate(sub_unit="1.2", difficulty="beginner", count=5, db=db)


def test_unknown_sub_unit_raises(db: Session, curriculum: dict[str, Any]) -> None:
    agent = build_agent([[make_raw_question()]])
    with pytest.raises(QuizGenerationError, match="No sub-unit"):
        agent.generate(sub_unit="99.9", difficulty="beginner", db=db)


def test_unindexed_curriculum_raises(db: Session, curriculum: dict[str, Any]) -> None:
    """Generating without retrieval would mean hallucinated questions."""
    agent = build_agent([[make_raw_question()]], empty_retrieval=True)
    with pytest.raises(QuizGenerationError, match="No curriculum retrieved"):
        agent.generate(sub_unit="1.2", difficulty="beginner", db=db)


def test_questions_carry_source_chunk_ids(db: Session, curriculum: dict[str, Any]) -> None:
    """Provenance is what makes 'grounded, not hallucinated' auditable."""
    agent = build_agent([[make_raw_question(1), make_raw_question(2, correct="B")]])
    quiz = agent.generate(sub_unit="1.2", difficulty="beginner", count=2, db=db)

    assert quiz.source_chunk_ids == ["chunk-1", "chunk-2"]
    for question in quiz.questions:
        assert question.source_chunk_ids == ["chunk-1", "chunk-2"]


def test_generation_metadata_is_auditable(db: Session, curriculum: dict[str, Any]) -> None:
    agent = build_agent([[make_raw_question(1)]])
    metadata = agent.generate(
        sub_unit="1.2", difficulty="proficient", count=1, db=db
    ).generation_metadata()

    assert metadata["difficulty"] == "proficient"
    assert metadata["source_chunk_ids"] == ["chunk-1", "chunk-2"]
    assert metadata["top_retrieval_score"] == pytest.approx(0.81)
    assert "model" in metadata


def test_prompt_includes_curriculum_and_difficulty(db: Session, curriculum: dict[str, Any]) -> None:
    """The draft prompt must carry the grounding text and the tier spec."""
    agent = build_agent([[make_raw_question(1)]])
    agent.generate(sub_unit="1.2", difficulty="proficient", count=1, db=db)

    prompt = agent._llm.calls[0][1]["content"]
    assert "Fluently add, subtract, multiply and divide integers" in prompt
    assert "proficient" in prompt
    assert "error analysis" in prompt.lower()
    assert "Unit 1 / 1.2" in prompt  # citation from retrieval


def test_avoid_block_lists_prior_questions(db: Session, curriculum: dict[str, Any]) -> None:
    first = make_raw_question(1)
    agent = build_agent([[first], [make_raw_question(2, correct="B")]])
    agent.generate(sub_unit="1.2", difficulty="beginner", count=2, db=db)

    second_prompt = agent._llm.calls[1][1]["content"]
    assert first["question_text"] in second_prompt


def test_handles_object_wrapped_array(db: Session, curriculum: dict[str, Any]) -> None:
    """Models often wrap the array as {"questions": [...]}."""
    agent = build_agent([{"questions": [make_raw_question(1)]}])
    quiz = agent.generate(sub_unit="1.2", difficulty="beginner", count=1, db=db)
    assert len(quiz.questions) == 1


def test_handles_fenced_json(db: Session, curriculum: dict[str, Any]) -> None:
    fenced = "```json\n" + json.dumps([make_raw_question(1)]) + "\n```"
    agent = build_agent([fenced])
    quiz = agent.generate(sub_unit="1.2", difficulty="beginner", count=1, db=db)
    assert len(quiz.questions) == 1


def test_handles_unparseable_response(db: Session, curriculum: dict[str, Any]) -> None:
    agent = build_agent(["I'm sorry, I cannot do that.", [make_raw_question(1)]])
    quiz = agent.generate(sub_unit="1.2", difficulty="beginner", count=1, db=db)
    assert len(quiz.questions) == 1


# --------------------------------------------------------------------------- #
# Agent tool surface
# --------------------------------------------------------------------------- #


def test_agent_exposes_expected_tools() -> None:
    names = {tool.name for tool in QuizGeneratorAgent(retriever=StubRetriever()).get_tools()}
    assert names == {
        "lookup_sub_unit",
        "search_curriculum",
        "difficulty_spec",
        "draft_questions",
        "validate_questions",
    }


def test_every_tool_has_a_usable_description() -> None:
    """Descriptions are the only thing the model sees when choosing a tool."""
    for tool in QuizGeneratorAgent(retriever=StubRetriever()).get_tools():
        assert len(tool.description) > 60, tool.name


def test_validate_tool_accepts_good_set() -> None:
    agent = QuizGeneratorAgent(retriever=StubRetriever())
    payload = json.dumps([make_raw_question(n, correct=k) for n, k in enumerate("ABCD", 1)])
    assert agent._tool_validate_questions(payload) == "VALID"


def test_validate_tool_reports_problems() -> None:
    agent = QuizGeneratorAgent(retriever=StubRetriever())
    payload = json.dumps([make_raw_question(1, explanation="no")])
    assert "explanation" in agent._tool_validate_questions(payload)


def test_difficulty_spec_tool() -> None:
    agent = QuizGeneratorAgent(retriever=StubRetriever())
    assert "80%" in agent._tool_difficulty_spec("intermediate")
    assert "ERROR" in agent._tool_difficulty_spec("nonsense")


# --------------------------------------------------------------------------- #
# Persistence
# --------------------------------------------------------------------------- #


def _generated(sub_unit_id: str, count: int = 3) -> GeneratedQuiz:
    return GeneratedQuiz(
        sub_unit_id=sub_unit_id,
        sub_unit_number="1.2",
        difficulty=DifficultyLevel.INTERMEDIATE,
        source_chunk_ids=["chunk-1"],
        retrieval_score=0.8,
        questions=[
            GeneratedQuestion(
                question_number=index + 1,
                question_text=f"Question {index + 1}?",
                options=[{"key": k, "text": k} for k in ("A", "B", "C", "D")],
                correct_answer="B",
                explanation="Because the sign flips when you subtract a negative.",
                distractor_rationales={"A": "sign error", "C": "off by one", "D": "guess"},
                hint="Check the sign.",
                skill_tag="subtract_integers",
                source_chunk_ids=["chunk-1"],
            )
            for index in range(count)
        ],
    )


def test_persist_writes_quiz_and_questions(
    db: Session, curriculum: dict[str, Any], student: Student
) -> None:
    quiz = persist_quiz(db, student.id, _generated(curriculum["sub12"].id))
    db.commit()

    assert db.query(Quiz).count() == 1
    assert db.query(QuizQuestion).count() == 3
    assert quiz.total_questions == 3
    assert quiz.status is QuizStatus.PENDING
    assert quiz.passing_threshold == 80.0, "intermediate threshold must be stored"
    assert quiz.generation_metadata["source_chunk_ids"] == ["chunk-1"]


def test_persisted_questions_keep_feedback_and_provenance(
    db: Session, curriculum: dict[str, Any], student: Student
) -> None:
    persist_quiz(db, student.id, _generated(curriculum["sub12"].id, count=1))
    db.commit()

    question = db.query(QuizQuestion).one()
    assert question.explanation
    assert question.distractor_rationales["A"] == "sign error"
    assert question.source_chunk_ids == ["chunk-1"]
    assert question.difficulty_level is DifficultyLevel.INTERMEDIATE
    assert len(question.options) == 4


# --------------------------------------------------------------------------- #
# Sequential unit progression
# --------------------------------------------------------------------------- #


def test_unit_one_is_always_unlocked(
    db: Session, curriculum: dict[str, Any], student: Student
) -> None:
    assert check_unit_unlocked(db, student.id, curriculum["unit1"]).unlocked is True


def test_unit_two_locked_until_unit_one_complete(
    db: Session, curriculum: dict[str, Any], student: Student
) -> None:
    status = check_unit_unlocked(db, student.id, curriculum["unit2"])
    assert status.unlocked is False
    assert status.blocking_sub_units == ["1.1", "1.2"]
    assert "locked" in status.reason.lower()


def test_partial_completion_still_locks(
    db: Session, curriculum: dict[str, Any], student: Student
) -> None:
    """67% on one sub-unit is not completion."""
    progress = SubUnitProgress(student_id=student.id, sub_unit_id=curriculum["sub11"].id)
    progress.mark_level_complete(DifficultyLevel.BEGINNER, 90.0)
    progress.mark_level_complete(DifficultyLevel.INTERMEDIATE, 90.0)
    db.add(progress)
    db.commit()

    status = check_unit_unlocked(db, student.id, curriculum["unit2"])
    assert status.unlocked is False
    assert status.blocking_sub_units == ["1.1", "1.2"]


def test_unit_two_unlocks_when_all_sub_units_hit_100(
    db: Session, curriculum: dict[str, Any], student: Student
) -> None:
    for sub in (curriculum["sub11"], curriculum["sub12"]):
        progress = SubUnitProgress(student_id=student.id, sub_unit_id=sub.id)
        for level in DifficultyLevel:
            progress.mark_level_complete(level, 95.0)
        db.add(progress)
    db.commit()

    assert check_unit_unlocked(db, student.id, curriculum["unit2"]).unlocked is True


def test_create_quiz_blocked_by_locked_unit(
    db: Session, curriculum: dict[str, Any], student: Student
) -> None:
    agent = build_agent([[make_raw_question(1)]])
    with pytest.raises(UnitLockedError, match="locked"):
        create_quiz(db, student.id, curriculum["sub21"].id, "beginner", agent=agent)

    assert db.query(Quiz).count() == 0, "no quiz should be created for a locked unit"


def test_create_quiz_end_to_end(db: Session, curriculum: dict[str, Any], student: Student) -> None:
    batch = [make_raw_question(n, correct=k) for n, k in enumerate("ABCDA", 1)]
    agent = build_agent(
        [batch, [make_raw_question(n, correct=k) for n, k in enumerate("BCDAB", 6)]]
    )

    quiz = create_quiz(db, student.id, curriculum["sub12"].id, "beginner", agent=agent)
    db.commit()

    assert quiz.total_questions == 10
    assert quiz.student_id == student.id
    assert quiz.passing_threshold == 70.0
    assert db.query(QuizQuestion).filter_by(quiz_id=quiz.id).count() == 10
    # A progress row should now exist for this sub-unit.
    assert (
        db.query(SubUnitProgress)
        .filter_by(student_id=student.id, sub_unit_id=curriculum["sub12"].id)
        .count()
        == 1
    )


def test_create_quiz_unknown_student(db: Session, curriculum: dict[str, Any]) -> None:
    agent = build_agent([[make_raw_question(1)]])
    with pytest.raises(QuizServiceError, match="student"):
        create_quiz(db, "nope", curriculum["sub12"].id, "beginner", agent=agent)


def test_enforce_unlock_can_be_bypassed_for_seeding(
    db: Session, curriculum: dict[str, Any], student: Student
) -> None:
    agent = build_agent([[make_raw_question(1)]])
    quiz = create_quiz(
        db,
        student.id,
        curriculum["sub21"].id,
        "beginner",
        agent=agent,
        enforce_unlock=False,
    )
    assert quiz.id is not None


# --------------------------------------------------------------------------- #
# Answer-key rebalancing
# --------------------------------------------------------------------------- #


def _q(correct: str, texts: list[str] | None = None) -> GeneratedQuestion:
    texts = texts or ["w", "x", "y", "z"]
    keys = list("ABCD")
    return GeneratedQuestion(
        question_number=1,
        question_text="What is -5 + 9?",
        options=[{"key": k, "text": t} for k, t in zip(keys, texts, strict=True)],
        correct_answer=correct,
        explanation="Move nine units right from negative five.",
        distractor_rationales={k: f"mistake {k}" for k in keys if k != correct},
        hint="Use a number line.",
    )


def test_rebalance_breaks_up_a_single_key_run() -> None:
    """A live run produced AAAAAAAAAA; clicking A would score 100%."""
    questions = [_q("A") for _ in range(10)]
    rebalance_answer_keys(questions, seed=7)

    keys = [q.correct_answer for q in questions]
    assert len(set(keys)) > 1
    assert validate_answer_distribution(questions) == []


def test_rebalance_preserves_correct_option_text() -> None:
    question = _q("A", texts=["RIGHT", "w1", "w2", "w3"])
    rebalance_answer_keys([question], seed=1)

    chosen = next(o for o in question.options if o["key"] == question.correct_answer)
    assert chosen["text"] == "RIGHT", "the correct answer text must not change"
    assert {o["text"] for o in question.options} == {"RIGHT", "w1", "w2", "w3"}


def test_rebalance_keeps_rationales_with_their_options() -> None:
    """A rationale must stay attached to the distractor it explains."""
    question = _q("A", texts=["RIGHT", "w1", "w2", "w3"])
    before = {o["text"]: question.distractor_rationales.get(o["key"]) for o in question.options}
    rebalance_answer_keys([question], seed=3)
    after = {o["text"]: question.distractor_rationales.get(o["key"]) for o in question.options}

    for text in ("w1", "w2", "w3"):
        assert before[text] == after[text], f"rationale for {text} was reattached to another option"


def test_rebalance_yields_four_valid_options() -> None:
    questions = [_q("A") for _ in range(8)]
    rebalance_answer_keys(questions, seed=11)
    for question in questions:
        assert [o["key"] for o in question.options] == ["A", "B", "C", "D"]
        assert question.correct_answer in ("A", "B", "C", "D")
        expected = {k for k in "ABCD" if k != question.correct_answer}
        assert set(question.distractor_rationales) == expected


def test_rebalance_skips_positional_options() -> None:
    """Reordering 'All of the above' would change what the question means."""
    question = _q("D", texts=["3", "5", "7", "All of the above"])
    moved = rebalance_answer_keys([question], seed=5)

    assert moved == 0
    assert question.correct_answer == "D"
    assert question.options[3]["text"] == "All of the above"


def test_rebalance_is_deterministic_with_a_seed() -> None:
    a = [_q("A") for _ in range(6)]
    b = [_q("A") for _ in range(6)]
    rebalance_answer_keys(a, seed=42)
    rebalance_answer_keys(b, seed=42)
    assert [q.correct_answer for q in a] == [q.correct_answer for q in b]


def test_rebalance_handles_empty_list() -> None:
    assert rebalance_answer_keys([], seed=1) == 0


def test_generate_rebalances_by_default(db: Session, curriculum: dict[str, Any]) -> None:
    """The full pipeline must not emit a degenerate answer key."""
    batch = [make_raw_question(n, correct="A") for n in range(1, 6)]
    agent = QuizGeneratorAgent(
        retriever=StubRetriever(), batch_size=5, max_attempts=1, rebalance_seed=13
    )
    agent._llm = StubLLM([batch])

    quiz = agent.generate(sub_unit="1.2", difficulty="beginner", count=5, db=db)

    assert len({q.correct_answer for q in quiz.questions}) > 1
    assert quiz.warnings == []


def test_rebalancing_can_be_disabled(db: Session, curriculum: dict[str, Any]) -> None:
    batch = [make_raw_question(n, correct="A") for n in range(1, 6)]
    agent = QuizGeneratorAgent(
        retriever=StubRetriever(), batch_size=5, max_attempts=1, rebalance_answers=False
    )
    agent._llm = StubLLM([batch])

    quiz = agent.generate(sub_unit="1.2", difficulty="beginner", count=5, db=db)

    assert {q.correct_answer for q in quiz.questions} == {"A"}
    assert any("guessable" in w for w in quiz.warnings)


# --------------------------------------------------------------------------- #
# Model self-talk leaking into student-facing text
# --------------------------------------------------------------------------- #


def test_detects_self_talk_from_a_real_failure() -> None:
    """Verbatim from a live run; this reached a student-facing explanation."""
    leaked = (
        "You must apply order of operations. For A: (-6) x 2 = -12, then -12 + 12 = 0. "
        "For example, B: -8 / 4 = -2, then -2 + 2 = 0 - wait, that also seems correct? "
        "But recheck: actually B is also 0? Let us fix this in logic."
    )
    assert find_meta_commentary(leaked) is not None


@pytest.mark.parametrize(
    "text",
    [
        "Start at -3 on the number line and move 8 units right, landing on 5.",
        "Note that the sign changes when you multiply by a negative number.",
        "Actually, the order of operations matters here: multiply before you add.",
        "Remember to check whether the result should be positive or negative.",
    ],
)
def test_legitimate_teaching_prose_is_not_flagged(text: str) -> None:
    """False positives here would discard perfectly good questions."""
    assert find_meta_commentary(text) is None


@pytest.mark.parametrize(
    "text",
    [
        "The answer is 5. Oops, I made an error above.",
        "You divide first. Hmm, no, multiply first.",
        "Let me recheck that calculation.",
        "Scratch that, the sign is wrong.",
        "As an AI, I should note the sign rule here.",
    ],
)
def test_self_talk_variants_are_flagged(text: str) -> None:
    assert find_meta_commentary(text) is not None


def test_validation_rejects_explanation_with_self_talk() -> None:
    raw = make_raw_question(
        explanation="You add them. Wait, that seems wrong? Let me recheck this."
    )
    assert any("self-talk" in issue for issue in validate_question(raw))


def test_validation_rejects_rationale_with_self_talk() -> None:
    raw = make_raw_question(correct="A")
    raw["distractor_rationales"]["B"] = "You subtracted. Hmm, or did you add? Oops."
    assert any("self-talk" in issue for issue in validate_question(raw))


# --------------------------------------------------------------------------- #
# Generator + verifier integration
# --------------------------------------------------------------------------- #


class StubVerifier:
    """Approves everything except question texts listed in ``reject``."""

    def __init__(self, reject: set[str] | None = None, error: set[str] | None = None) -> None:
        self.reject = reject or set()
        self.error = error or set()
        self.seen: list[str] = []

    def verify(self, questions: list[GeneratedQuestion]) -> VerificationReport:
        verdicts = []
        for question in questions:
            self.seen.append(question.question_text)
            if question.question_text in self.error:
                outcome, answer = Verdict.ERROR, None
            elif question.question_text in self.reject:
                outcome, answer = Verdict.DISPUTED, "D"
            else:
                outcome, answer = Verdict.AGREED, question.correct_answer
            verdicts.append(
                QuestionVerdict(
                    question_number=question.question_number,
                    question_text=question.question_text,
                    claimed_answer=question.correct_answer,
                    verifier_answer=answer,
                    verdict=outcome,
                )
            )
        return VerificationReport(verdicts=verdicts)


def _verified_agent(payloads: list[Any], verifier: StubVerifier) -> QuizGeneratorAgent:
    agent = QuizGeneratorAgent(
        retriever=StubRetriever(),
        batch_size=5,
        max_attempts=4,
        verifier=verifier,
        rebalance_seed=5,
    )
    agent._llm = StubLLM(payloads)
    return agent


def test_verification_is_off_by_default() -> None:
    assert QuizGeneratorAgent(retriever=StubRetriever()).verifier is None


def test_disputed_questions_are_dropped_and_regenerated(
    db: Session, curriculum: dict[str, Any]
) -> None:
    """A rejected question must be replaced, not silently lost."""
    bad = make_raw_question(1)
    good = [make_raw_question(n, correct=k) for n, k in enumerate("BCD", 2)]
    verifier = StubVerifier(reject={bad["question_text"]})
    agent = _verified_agent([[bad, *good[:1]], good[1:]], verifier)

    quiz = agent.generate(sub_unit="1.2", difficulty="beginner", count=3, db=db)

    assert len(quiz.questions) == 3
    assert bad["question_text"] not in [q.question_text for q in quiz.questions]
    assert len(quiz.rejected) == 1
    assert quiz.rejected[0].verdict is Verdict.DISPUTED
    assert any("rejected by the verifier" in w for w in quiz.warnings)


def test_verification_errors_keep_the_question(db: Session, curriculum: dict[str, Any]) -> None:
    """An API outage must not silently shrink a student's quiz."""
    item = make_raw_question(1)
    verifier = StubVerifier(error={item["question_text"]})
    agent = _verified_agent([[item]], verifier)

    quiz = agent.generate(sub_unit="1.2", difficulty="beginner", count=1, db=db)

    assert len(quiz.questions) == 1
    assert quiz.rejected == []
    assert any("could not be verified" in w for w in quiz.warnings)


def test_questions_are_renumbered_contiguously_after_rejection(
    db: Session, curriculum: dict[str, Any]
) -> None:
    bad = make_raw_question(2)
    batch = [make_raw_question(1), bad, make_raw_question(3, correct="C")]
    agent = _verified_agent(
        [batch, [make_raw_question(9, correct="B")]],
        StubVerifier(reject={bad["question_text"]}),
    )

    quiz = agent.generate(sub_unit="1.2", difficulty="beginner", count=3, db=db)

    assert [q.question_number for q in quiz.questions] == [1, 2, 3]


def test_verification_report_lands_in_metadata(db: Session, curriculum: dict[str, Any]) -> None:
    bad = make_raw_question(1)
    # Two batches: the first is rejected, the second supplies the replacement.
    agent = _verified_agent(
        [[bad], [make_raw_question(2, correct="B")]],
        StubVerifier(reject={bad["question_text"]}),
    )
    quiz = agent.generate(sub_unit="1.2", difficulty="beginner", count=1, db=db)

    metadata = quiz.generation_metadata()
    assert metadata["verification"] is not None
    assert metadata["verification"]["disputed"] == 1
    assert len(metadata["rejected_by_verifier"]) == 1


def test_verdict_keys_track_rebalancing(db: Session, curriculum: dict[str, Any]) -> None:
    """Rebalancing moves the answer letter; the audit trail must follow."""
    batch = [make_raw_question(n, correct="A") for n in range(1, 6)]
    agent = _verified_agent([batch], StubVerifier())

    quiz = agent.generate(sub_unit="1.2", difficulty="beginner", count=5, db=db)

    assert quiz.verification is not None
    by_number = {v.question_number: v for v in quiz.verification.verdicts}
    for question in quiz.questions:
        verdict = by_number[question.question_number]
        assert verdict.claimed_answer == question.correct_answer
        assert verdict.verifier_answer == question.correct_answer


def test_verifier_only_sees_structurally_valid_questions(
    db: Session, curriculum: dict[str, Any]
) -> None:
    """No point paying to verify something already rejected as malformed."""
    malformed = make_raw_question(1, explanation="no")
    good = make_raw_question(2, correct="B")
    verifier = StubVerifier()
    agent = _verified_agent([[malformed, good]], verifier)

    agent.generate(sub_unit="1.2", difficulty="beginner", count=1, db=db)

    assert verifier.seen == [good["question_text"]]


# --------------------------------------------------------------------------- #
# Tier construction rules
# --------------------------------------------------------------------------- #


def test_every_tier_carries_construction_rules() -> None:
    for level in ("beginner", "intermediate", "proficient"):
        assert spec_for(level).construction_rules, f"{level} has no construction rules"


def test_harder_tiers_carry_more_rules() -> None:
    """Rejection rates measured live: 1% beginner, 10% intermediate, 15% proficient."""
    counts = [
        len(spec_for(level).construction_rules)
        for level in ("beginner", "intermediate", "proficient")
    ]
    assert counts == sorted(counts), counts
    assert counts[2] > counts[0]


def test_every_tier_requires_computing_the_answer_first() -> None:
    """27 of 38 disputed proficient questions had the answer in none of the options."""
    for level in ("beginner", "intermediate", "proficient"):
        rules = " ".join(spec_for(level).construction_rules).lower()
        assert "before writing any option" in rules, level


def test_proficient_rules_target_the_measured_failures() -> None:
    rules = " ".join(spec_for("proficient").construction_rules).lower()
    # "Taylor says the answer is -30" -- where -30 was actually correct.
    assert "error-analysis" in rules
    assert "really does differ" in rules
    # "|x| + |-x| = 0" -- disproved by every non-zero option.
    assert "counterexample" in rules
    # Prose-judgement options are rarely exactly-one-correct.
    assert "best" in rules


def test_construction_rules_reach_the_prompt() -> None:
    block = spec_for("proficient").to_prompt()
    assert "CONSTRUCTION RULES FOR THIS TIER (mandatory)" in block
    for rule in spec_for("proficient").construction_rules:
        assert rule.split(".")[0][:40] in block


def test_a_tier_without_rules_omits_the_section() -> None:
    from agents.quiz_generator import DifficultySpec

    bare = DifficultySpec(
        level=DifficultyLevel.BEGINNER,
        threshold=70.0,
        cognitive_demand="x",
        style="y",
        guidance="z",
    )
    assert "CONSTRUCTION RULES" not in bare.to_prompt()


def test_rules_appear_in_the_generated_draft_prompt(
    db: Session, curriculum: dict[str, Any]
) -> None:
    """The rules are worthless if they never reach the model."""
    agent = build_agent([[make_raw_question(1)]])
    agent.generate(sub_unit="1.2", difficulty="proficient", count=1, db=db)

    prompt = agent._llm.calls[0][1]["content"]
    assert "CONSTRUCTION RULES FOR THIS TIER (mandatory)" in prompt
    assert "appears in none of the options" in prompt


# --------------------------------------------------------------------------- #
# Ambiguous stems -- caught structurally, before paying for verification
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "stem",
    [
        # Verbatim from live fills. Each had two or more defensible options.
        "A student claims that when you add two integers, the result is always "
        "greater than either addend. Which pair of integers shows this claim is false?",
        "Which of the following is the best rational approximation to sqrt(15)?",
        "Which statement best explains the mistake Taylor made?",
        "Samuel says |x| + |-x| is always 0. Which value of x could you use to "
        "show that Samuel is incorrect?",
        "Which counterexample disproves the statement?",
    ],
)
def test_ambiguous_stems_are_caught(stem: str) -> None:
    assert find_ambiguous_stem(stem) is not None


@pytest.mark.parametrize(
    "stem",
    [
        "A submarine descends 150 m then rises 60 m. What is its final depth?",
        "At which step does the error first appear in the work shown?",
        "What is the correct value of -4(3 + 5) + 20?",
        "Which expression is equivalent to 3(x + 4) - 2x?",
        "For which pair of integers is the sum less than both numbers?",
        "A recipe needs 3/4 cup per batch. How much is needed for 5 batches?",
        "Which number line shows the solution to x > -2?",
    ],
)
def test_sound_stems_are_not_flagged(stem: str) -> None:
    """False positives here throw away perfectly good proficient questions."""
    assert find_ambiguous_stem(stem) is None


def test_regex_patterns_are_not_corrupted() -> None:
    """A shell heredoc once turned every \\b into a literal backspace byte.

    The regexes compiled fine and matched nothing, so the check silently did
    nothing at all.
    """
    from agents.quiz_generator import AMBIGUOUS_STEM_PATTERNS

    for pattern, _reason in AMBIGUOUS_STEM_PATTERNS:
        assert "\x08" not in pattern, f"backspace byte in {pattern!r}"
        assert "\\b" in pattern or "\\s" in pattern, f"no word boundary in {pattern!r}"


def test_validation_rejects_an_ambiguous_stem() -> None:
    raw = make_raw_question(question_text="Which pair of integers shows this claim is false?")
    issues = validate_question(raw)
    assert any("exactly-one-correct" in issue for issue in issues)


def test_ambiguous_stem_check_runs_before_verification(
    db: Session, curriculum: dict[str, Any]
) -> None:
    """Catching it here is free; catching it at verification costs two calls."""
    bad = make_raw_question(1, question_text="Which statement best explains the error?")
    good = make_raw_question(2, correct="B")

    class CountingVerifier:
        def __init__(self) -> None:
            self.seen: list[str] = []

        def verify(self, questions: list[GeneratedQuestion]) -> VerificationReport:
            self.seen.extend(q.question_text for q in questions)
            return VerificationReport(
                verdicts=[
                    QuestionVerdict(
                        question_number=q.question_number,
                        question_text=q.question_text,
                        claimed_answer=q.correct_answer,
                        verifier_answer=q.correct_answer,
                        verdict=Verdict.AGREED,
                    )
                    for q in questions
                ]
            )

    verifier = CountingVerifier()
    agent = QuizGeneratorAgent(
        retriever=StubRetriever(), batch_size=5, max_attempts=2, verifier=verifier
    )
    agent._llm = StubLLM([[bad, good]])

    agent.generate(sub_unit="1.2", difficulty="proficient", count=1, db=db)

    assert bad["question_text"] not in verifier.seen, "paid to verify a stem we could reject"


# --------------------------------------------------------------------------- #
# True/false questions
# --------------------------------------------------------------------------- #


def make_true_false_question(correct: str = "B", **overrides: Any) -> dict[str, Any]:
    """A valid true/false question: a false statement unless told otherwise."""
    raw: dict[str, Any] = {
        "question_text": "The sum of -4 and 9 is -13.",
        "question_type": "true_false",
        "options": [{"key": "A", "text": "True"}, {"key": "B", "text": "False"}],
        "correct_answer": correct,
        "explanation": "Start at -4 and move 9 to the right: you land on 5, not -13.",
        "distractor_rationales": {
            ("A" if correct == "B" else "B"): "Adding the sizes and keeping the minus sign gives -13."
        },
        "hint": "Which way do you move on a number line when you add 9?",
        "skill_tag": "add_integers",
    }
    raw.update(overrides)
    return raw


def _tf(correct: str) -> GeneratedQuestion:
    return QuizGeneratorAgent._to_question(make_true_false_question(correct), 1)


TRUE_FALSE = [{"key": "A", "text": "True"}, {"key": "B", "text": "False"}]


def test_true_false_question_passes() -> None:
    raw = make_true_false_question()
    assert validate_question(raw) == []
    assert question_type_of(raw) is QuestionType.TRUE_FALSE


def test_true_false_is_recognised_by_its_options() -> None:
    """A model that forgets question_type has still written a true/false question."""
    raw = make_true_false_question()
    del raw["question_type"]
    assert question_type_of(raw) is QuestionType.TRUE_FALSE
    assert validate_question(raw) == []


def test_multiple_choice_is_the_default() -> None:
    assert question_type_of(make_raw_question()) is QuestionType.MULTIPLE_CHOICE


def test_true_false_needs_true_then_false() -> None:
    """True is always on the left, so a child never has to read the buttons."""
    raw = make_true_false_question(
        options=[{"key": "A", "text": "False"}, {"key": "B", "text": "True"}]
    )
    assert any("two options" in issue for issue in validate_question(raw))


def test_true_false_cannot_have_four_options() -> None:
    raw = make_raw_question(question_type="true_false")
    assert any("two options" in issue for issue in validate_question(raw))


def test_true_false_answer_must_be_true_or_false() -> None:
    raw = make_true_false_question(correct_answer="C")
    assert any("not one of A,B" in issue for issue in validate_question(raw))


def test_true_false_needs_a_rationale_for_the_wrong_answer() -> None:
    raw = make_true_false_question(distractor_rationales={})
    assert any("missing keys: ['A']" in issue for issue in validate_question(raw))


def test_two_options_that_are_not_true_and_false_are_rejected() -> None:
    raw = make_raw_question()
    raw["options"] = raw["options"][:2]
    assert any("exactly 4" in issue for issue in validate_question(raw))


def test_true_false_parses_with_fixed_options() -> None:
    raw = make_true_false_question(options=[{"key": "a", "text": " true "}, {"key": "b", "text": "FALSE"}])
    question = QuizGeneratorAgent._to_question(raw, 1)

    assert question.is_true_false
    assert question.options == TRUE_FALSE
    kwargs = question.to_model_kwargs(DifficultyLevel.BEGINNER)
    assert kwargs["question_type"] is QuestionType.TRUE_FALSE


def test_rebalance_leaves_true_false_alone() -> None:
    questions = [_tf("A") for _ in range(4)] + [_q("A") for _ in range(8)]
    rebalance_answer_keys(questions, seed=7)

    for question in questions[:4]:
        assert question.correct_answer == "A"
        assert question.options == TRUE_FALSE
    assert len({question.correct_answer for question in questions[4:]}) > 1


def test_distribution_ignores_true_false() -> None:
    """Six true statements are not a lopsided answer key."""
    questions = [_tf("A") for _ in range(6)] + _questions(list("ABCD"))
    assert validate_answer_distribution(questions) == []


def test_prompt_asks_for_a_few_true_false(db: Session, curriculum: dict[str, Any]) -> None:
    batch = [make_raw_question(n, correct=k) for n, k in enumerate("ABCDA", 1)]
    agent = build_agent([batch])
    agent.generate(sub_unit="1.2", difficulty="beginner", count=5, db=db)

    prompts = [call[-1]["content"] for call in agent._llm.calls]
    assert any("write 1 of the 5 as true/false" in prompt for prompt in prompts)
    assert any('"question_type": "true_false"' in prompt for prompt in prompts)


def test_generate_keeps_true_false_questions(db: Session, curriculum: dict[str, Any]) -> None:
    batch = [make_raw_question(n, correct=k) for n, k in enumerate("ABCD", 1)]
    batch.append(make_true_false_question())
    agent = build_agent([batch])

    quiz = agent.generate(sub_unit="1.2", difficulty="beginner", count=5, db=db)

    assert len(quiz.questions) == 5
    true_false = [question for question in quiz.questions if question.is_true_false]
    assert len(true_false) == 1
    assert true_false[0].correct_answer == "B"
    assert true_false[0].options == TRUE_FALSE


def test_a_set_holds_only_a_few_true_false(db: Session, curriculum: dict[str, Any]) -> None:
    """A guess is right half the time on true/false: three in ten at most."""
    statements = [
        make_true_false_question(question_text=f"Adding {n} to -4 gives -{n + 4}.")
        for n in range(1, 6)
    ]
    batch2 = [make_raw_question(n, correct=k) for n, k in enumerate("ABCDA", 1)]
    batch3 = [make_raw_question(n, correct=k) for n, k in enumerate("BCDAB", 6)]
    agent = build_agent([statements, batch2, batch3])

    quiz = agent.generate(sub_unit="1.2", difficulty="beginner", count=10, db=db)

    assert len(quiz.questions) == 10
    assert sum(question.is_true_false for question in quiz.questions) == 3
    # Once the set has its three, the model is told not to write more.
    prompts = [call[-1]["content"] for call in agent._llm.calls]
    assert sum("every question as multiple choice" in prompt for prompt in prompts) == 2


def test_persisted_true_false_keeps_its_type(
    db: Session, curriculum: dict[str, Any], student: Student
) -> None:
    generated = _generated(curriculum["sub12"].id, count=1)
    generated.questions[0] = _tf("B")
    persist_quiz(db, student.id, generated)
    db.commit()

    question = db.query(QuizQuestion).one()
    assert question.question_type is QuestionType.TRUE_FALSE
    assert question.options == TRUE_FALSE


def test_banked_true_false_keeps_its_type(db: Session, curriculum: dict[str, Any]) -> None:
    """The bank hands questions out to many quizzes; the type must go with them."""
    from db.models import QuestionBankItem
    from services.question_bank import _store

    stored = _store(
        db,
        sub_unit_id=curriculum["sub12"].id,
        difficulty=DifficultyLevel.BEGINNER,
        question=_tf("B"),
        metadata={},
        status="verified",
    )
    db.commit()

    assert stored
    item = db.query(QuestionBankItem).one()
    assert item.question_type is QuestionType.TRUE_FALSE
    assert item.to_question_kwargs()["question_type"] is QuestionType.TRUE_FALSE
