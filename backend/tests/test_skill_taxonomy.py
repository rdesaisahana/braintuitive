"""Skill vocabulary tests.

The problem this module solves, measured on a live bank: the generator invented
a free-form ``skill_tag`` per question, producing 396 distinct tags across 683
questions. Every per-skill accuracy came out as ``1/1 = 100%`` and nothing
could ever be flagged as needing attention.

Run:
    cd backend
    pytest tests/test_skill_taxonomy.py -v
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from db.models import Base, CurriculumSubUnit, CurriculumUnit
from services.skill_taxonomy import (
    BANNED_TOKENS,
    MAX_VOCABULARY,
    _same_word,
    derive_vocabulary,
    ensure_vocabulary,
    fallback_vocabulary,
    normalise_tag,
    snap_to_vocabulary,
    validate_vocabulary,
)


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
def sub_unit(db: Session) -> CurriculumSubUnit:
    unit = CurriculumUnit(unit_number=1, title="Number Fluency", subject="math", grade_level=6)
    sub = CurriculumSubUnit(
        unit=unit,
        sub_unit_number="1.4",
        sequence=3,
        title="Find GCF and LCM",
        description=(
            "Find GCF and LCM using a variety of strategies. "
            "(Suggested Strategies: Listing, Boot/Ladder, Prime Factorization)"
        ),
    )
    db.add_all([unit, sub])
    db.commit()
    return sub


class StubLLM:
    def __init__(self, content: str) -> None:
        self.content = content
        self.prompts: list[str] = []

    def invoke(self, messages: list[dict[str, str]]) -> Any:  # noqa: ANN401
        self.prompts.append(messages[-1]["content"])

        class _Response:
            def __init__(self, text: str) -> None:
                self.content = text

        return _Response(self.content)


# --------------------------------------------------------------------------- #
# Normalisation and validation
# --------------------------------------------------------------------------- #


def test_normalise_tag() -> None:
    assert normalise_tag("Add Integers") == "add_integers"
    assert normalise_tag("  add--integers  ") == "add_integers"
    assert normalise_tag("Add, Subtract & Divide") == "add_subtract_divide"
    assert normalise_tag("") == ""


def test_validation_deduplicates() -> None:
    assert validate_vocabulary(["add_integers", "Add Integers", "ADD_INTEGERS"]) == ["add_integers"]


def test_validation_drops_format_and_difficulty_words() -> None:
    """ "add_integers_basic" and "add_integers_advanced" are one skill."""
    assert validate_vocabulary(["basic", "advanced", "word_problem", "conceptual"]) == []


def test_validation_caps_the_size() -> None:
    """An unbounded vocabulary is the problem, not the fix."""
    candidates = [f"skill_number_{n}" for n in range(20)]
    assert len(validate_vocabulary(candidates)) == MAX_VOCABULARY


def test_validation_ignores_non_strings() -> None:
    assert validate_vocabulary(["add_integers", None, 42, {"a": 1}]) == ["add_integers"]


def test_banned_tokens_do_not_block_real_skills() -> None:
    """A banned word inside a real skill name is fine; only pure junk is dropped."""
    assert validate_vocabulary(["solve_word_equations"]) == ["solve_word_equations"]
    assert "reasoning" in BANNED_TOKENS


# --------------------------------------------------------------------------- #
# Word-form matching
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("compare", "comparison"),
        ("multiply", "multiplication"),
        ("approximate", "approximations"),
        ("integer", "integers"),
        ("evaluate", "evaluating"),
    ],
)
def test_word_forms_match(left: str, right: str) -> None:
    assert _same_word(left, right) is True


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("rational", "irrational"),  # opposites must never merge
        ("add", "subtract"),
        ("gcf", "lcm"),
    ],
)
def test_different_words_do_not_match(left: str, right: str) -> None:
    assert _same_word(left, right) is False


# --------------------------------------------------------------------------- #
# Snapping
# --------------------------------------------------------------------------- #


VOCAB = ["absolute_value", "compare_absolute_values", "absolute_value_in_context"]


def test_exact_match_snaps_to_itself() -> None:
    assert snap_to_vocabulary("absolute_value", VOCAB) == "absolute_value"


@pytest.mark.parametrize(
    "tag",
    [
        "absolute_value_of_zero",
        "absolute_value_of_integer",
        "absolute_value_symmetry",
        "identify_number_from_absolute_value",
    ],
)
def test_verbose_variants_snap_to_the_vocabulary(tag: str) -> None:
    """These are the real tags from a live bank; each is one skill, not four."""
    assert snap_to_vocabulary(tag, VOCAB) == "absolute_value"


def test_unrelated_tags_do_not_snap() -> None:
    """Guessing wildly would be worse than admitting no match."""
    assert snap_to_vocabulary("multiply_integers", VOCAB) is None
    assert snap_to_vocabulary("totally_unrelated", VOCAB) is None


def test_snapping_handles_word_forms() -> None:
    vocabulary = ["compare_rational_approximations", "order_irrational_numbers"]
    assert (
        snap_to_vocabulary("rational_approximation_comparison", vocabulary)
        == "compare_rational_approximations"
    )


def test_snapping_with_no_vocabulary_returns_none() -> None:
    assert snap_to_vocabulary("anything", []) is None
    assert snap_to_vocabulary("", VOCAB) is None


# --------------------------------------------------------------------------- #
# Deriving
# --------------------------------------------------------------------------- #


def test_derive_uses_the_model(sub_unit: CurriculumSubUnit) -> None:
    llm = StubLLM('["find_gcf", "find_lcm", "use_prime_factorization"]')
    assert derive_vocabulary(sub_unit, llm) == [
        "find_gcf",
        "find_lcm",
        "use_prime_factorization",
    ]
    assert "Find GCF and LCM" in llm.prompts[0]


def test_derive_falls_back_when_the_model_returns_junk(
    sub_unit: CurriculumSubUnit,
) -> None:
    """A model outage must not block ingestion."""
    assert derive_vocabulary(sub_unit, StubLLM("I cannot help with that.")) == (
        fallback_vocabulary(sub_unit)
    )


def test_derive_falls_back_when_too_few_usable_tags(
    sub_unit: CurriculumSubUnit,
) -> None:
    assert derive_vocabulary(sub_unit, StubLLM('["basic", "advanced"]')) == (
        fallback_vocabulary(sub_unit)
    )


def test_fallback_ignores_parentheticals(sub_unit: CurriculumSubUnit) -> None:
    """ "boot" and "ladder" leaked out of "(Suggested Strategies: Boot/Ladder)"."""
    tags = fallback_vocabulary(sub_unit)
    joined = " ".join(tags)
    for leak in ("suggested", "boot", "ladder", "listing"):
        assert leak not in joined, f"{leak!r} leaked from the parenthetical"
    assert tags


def test_fallback_is_deterministic(sub_unit: CurriculumSubUnit) -> None:
    assert fallback_vocabulary(sub_unit) == fallback_vocabulary(sub_unit)


# --------------------------------------------------------------------------- #
# Persistence and stability
# --------------------------------------------------------------------------- #


def test_ensure_stores_the_vocabulary(db: Session, sub_unit: CurriculumSubUnit) -> None:
    llm = StubLLM('["find_gcf", "find_lcm", "use_prime_factorization"]')
    vocabulary = ensure_vocabulary(db, sub_unit, llm)
    db.commit()

    assert sub_unit.skill_tags == vocabulary
    assert vocabulary == ["find_gcf", "find_lcm", "use_prime_factorization"]


def test_ensure_reuses_an_existing_vocabulary(db: Session, sub_unit: CurriculumSubUnit) -> None:
    """Stability matters: a drifting vocabulary re-fragments the data."""
    sub_unit.skill_tags = ["find_gcf", "find_lcm"]
    db.commit()

    llm = StubLLM('["something", "completely", "different"]')
    assert ensure_vocabulary(db, sub_unit, llm) == ["find_gcf", "find_lcm"]
    assert llm.prompts == [], "the model should not have been called"


def test_refresh_forces_rederivation(db: Session, sub_unit: CurriculumSubUnit) -> None:
    sub_unit.skill_tags = ["old_tag_one", "old_tag_two"]
    db.commit()

    llm = StubLLM('["find_gcf", "find_lcm", "use_prime_factorization"]')
    assert ensure_vocabulary(db, sub_unit, llm, refresh=True) == [
        "find_gcf",
        "find_lcm",
        "use_prime_factorization",
    ]


def test_ensure_survives_a_junk_stored_vocabulary(db: Session, sub_unit: CurriculumSubUnit) -> None:
    """The parser once stored ["suggested","boot","ladder"] here."""
    sub_unit.skill_tags = ["basic", "advanced"]
    db.commit()

    llm = StubLLM('["find_gcf", "find_lcm", "use_prime_factorization"]')
    vocabulary = ensure_vocabulary(db, sub_unit, llm)
    assert "basic" not in vocabulary
    assert vocabulary == ["find_gcf", "find_lcm", "use_prime_factorization"]
