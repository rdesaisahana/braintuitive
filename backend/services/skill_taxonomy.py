"""A controlled skill vocabulary per sub-unit.

Without one, the generator invents a free-form ``skill_tag`` per question. A
live fill produced **396 distinct tags across 683 questions** -- about 1.7
questions per tag -- so every per-skill accuracy came out as ``1/1 = 100%``
and nothing could ever be flagged as shaky. The progress dashboard was correct
and useless, and the Gap Detector's only input was noise.

The fix is to decide the vocabulary once per sub-unit and make question
generation choose from it. Three to five skills over thirty questions gives
six to ten answers per skill, which is enough for an accuracy figure to mean
something.

The vocabulary is derived from the learning objective by the model, then
validated and stored on ``CurriculumSubUnit.skill_tags``. It is derived once
and reused, so a sub-unit's tags stay stable as its bank grows -- a vocabulary
that drifted between batches would fragment the data all over again.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from sqlalchemy.orm import Session

from db.models import CurriculumSubUnit

logger = logging.getLogger(__name__)

MIN_VOCABULARY = 2
MAX_VOCABULARY = 6
# Words that describe a question's shape or difficulty rather than the skill
# being tested. Left in, they fragment the vocabulary along the wrong axis:
# "add_integers_basic" and "add_integers_advanced" are one skill, not two.
BANNED_TOKENS = frozenset(
    {
        "basic",
        "advanced",
        "simple",
        "hard",
        "easy",
        "beginner",
        "intermediate",
        "proficient",
        "difficult",
        "word",
        "problem",
        "problems",
        "question",
        "questions",
        "quiz",
        "conceptual",
        "application",
        "reasoning",
        "context",
        "contextual",
        "general",
        "misc",
        "other",
    }
)

VOCABULARY_PROMPT = """You are designing the skill taxonomy for a school \
maths curriculum.

Sub-unit {sub_unit_number}: {title}
LEARNING OBJECTIVE: {objective}
Grade {grade} {subject}.

List the distinct SKILLS a quiz on this objective would test. These are used \
to track which specific skill a child is struggling with, so they must be few \
enough that each collects many questions.

RULES:
- Between 3 and 5 skills. Fewer is better than more.
- snake_case, two to four words: add_integers, compare_absolute_values.
- Each must be genuinely DISTINCT, not a rewording of another. \
"absolute_value_of_zero" and "absolute_value_of_integer" are the same skill.
- Cover the objective and nothing beyond it.
- Do NOT encode difficulty or question format. No "basic", "advanced", \
"word_problem", "conceptual".

Return ONLY a JSON array of strings, nothing else."""


def normalise_tag(raw: str) -> str:
    """Reduce a tag to comparable snake_case."""
    text = re.sub(r"[^a-z0-9]+", "_", (raw or "").strip().lower())
    return re.sub(r"_+", "_", text).strip("_")


def _tokens(tag: str) -> set[str]:
    return {token for token in normalise_tag(tag).split("_") if token}


# Two tokens count as the same word when they share this many leading
# characters. Chosen so "compare"/"comparison" and "multiply"/"multiplication"
# match, while "rational"/"irrational" -- which mean opposite things -- do not.
PREFIX_MATCH_LENGTH = 5


def _same_word(left: str, right: str) -> bool:
    """Whether two tokens are the same word in different forms.

    Exact matching is too strict for this data: the old free-form tags used
    ``comparison`` where a vocabulary says ``compare``, and treating those as
    unrelated dumped 300 of 683 questions into a fallback bucket.
    """
    if left == right:
        return True
    shorter, longer = sorted((left, right), key=len)
    if len(shorter) < PREFIX_MATCH_LENGTH:
        return False
    return (
        longer.startswith(shorter[:PREFIX_MATCH_LENGTH])
        and shorter[:PREFIX_MATCH_LENGTH] == longer[:PREFIX_MATCH_LENGTH]
    )


def _overlap(candidate_tokens: set[str], entry_tokens: set[str]) -> float:
    """Fraction of an entry's words that the candidate also uses."""
    if not entry_tokens:
        return 0.0
    matched = sum(
        1
        for entry_token in entry_tokens
        if any(_same_word(entry_token, token) for token in candidate_tokens)
    )
    return matched / len(entry_tokens)


def validate_vocabulary(candidates: list[Any]) -> list[str]:
    """Clean a proposed vocabulary, returning only usable tags.

    Drops anything empty, duplicated, or made entirely of banned words, and
    caps the size. A vocabulary that grows without limit is the problem this
    module exists to solve.
    """
    seen: set[str] = set()
    cleaned: list[str] = []

    for candidate in candidates:
        if not isinstance(candidate, str):
            continue
        tag = normalise_tag(candidate)
        if not tag or tag in seen:
            continue
        tokens = _tokens(tag)
        if not tokens or tokens <= BANNED_TOKENS:
            logger.debug("Dropping vocabulary candidate %r: no content words.", candidate)
            continue
        if len(tokens) > 5:
            continue
        seen.add(tag)
        cleaned.append(tag)
        if len(cleaned) >= MAX_VOCABULARY:
            break

    return cleaned


def snap_to_vocabulary(tag: str, vocabulary: list[str]) -> str | None:
    """Map a free-form tag onto the closest vocabulary entry.

    Snapping rather than rejecting is deliberate: a question tagged
    ``absolute_value_of_zero`` when the vocabulary says ``absolute_value`` is
    a perfectly good question with a slightly verbose label. Discarding it
    would waste a generation, and a near-miss tag in the right bucket is worth
    far more than an exact tag in a bucket of one.

    Returns None when nothing matches well enough to guess.
    """
    if not vocabulary:
        return None

    candidate = normalise_tag(tag)
    if not candidate:
        return None

    normalised = [normalise_tag(entry) for entry in vocabulary]
    if candidate in normalised:
        return candidate

    # Containment either way: "absolute_value" inside "absolute_value_of_zero".
    for entry in normalised:
        if entry and (entry in candidate or candidate in entry):
            return entry

    # Otherwise the entry sharing the most of its own words with the tag,
    # comparing word forms rather than exact strings.
    candidate_tokens = _tokens(candidate)
    best: tuple[float, str] | None = None
    for entry in normalised:
        score = _overlap(candidate_tokens, _tokens(entry))
        if score >= 0.5 and (best is None or score > best[0]):
            best = (score, entry)

    return best[1] if best else None


def fallback_vocabulary(sub_unit: CurriculumSubUnit) -> list[str]:
    """Derive a vocabulary from the objective text, without a model.

    Used when the model is unavailable or returns nothing usable. Crude but
    deterministic: better a handful of blunt tags than 69 unique ones.
    """
    text = sub_unit.description or sub_unit.title or ""
    # Drop parentheticals: "(Suggested Strategies: Listing, Boot/Ladder)" is
    # where tags like "boot" and "ladder" came from.
    text = re.sub(r"\([^)]*\)", " ", text)

    verbs = [
        "identify",
        "describe",
        "find",
        "add",
        "subtract",
        "multiply",
        "divide",
        "compare",
        "evaluate",
        "simplify",
        "solve",
        "graph",
        "write",
        "calculate",
        "classify",
        "represent",
        "apply",
    ]
    words = [w for w in re.findall(r"[a-z]+", text.lower()) if len(w) > 3]
    nouns = [
        w
        for w in words
        if w not in verbs
        and w not in BANNED_TOKENS
        and w not in {"with", "that", "from", "using", "their", "these"}
    ]
    subject = "_".join(nouns[:2]) if nouns else "skill"

    found = [verb for verb in verbs if verb in text.lower()][:3]
    tags = [normalise_tag(f"{verb}_{subject}") for verb in found] or [normalise_tag(subject)]
    return validate_vocabulary(tags)


def derive_vocabulary(sub_unit: CurriculumSubUnit, llm: Any = None) -> list[str]:
    """Ask the model for this sub-unit's skill vocabulary.

    Falls back to :func:`fallback_vocabulary` on any failure, so ingestion is
    never blocked by the model being unavailable.
    """
    if llm is None:
        return fallback_vocabulary(sub_unit)

    unit = sub_unit.unit
    prompt = VOCABULARY_PROMPT.format(
        sub_unit_number=sub_unit.sub_unit_number,
        title=sub_unit.title,
        objective=sub_unit.description or sub_unit.title,
        grade=unit.grade_level if unit else "?",
        subject=unit.subject if unit else "math",
    )

    try:
        response = llm.invoke([{"role": "user", "content": prompt}])
        content = getattr(response, "content", "") or ""
        start, end = content.find("["), content.rfind("]")
        parsed = json.loads(content[start : end + 1]) if start != -1 and end > start else []
    except Exception as exc:
        logger.warning(
            "Vocabulary generation failed for %s (%s); using the fallback.",
            sub_unit.sub_unit_number,
            exc,
        )
        return fallback_vocabulary(sub_unit)

    vocabulary = validate_vocabulary(parsed if isinstance(parsed, list) else [])
    if len(vocabulary) < MIN_VOCABULARY:
        logger.warning(
            "Model returned %d usable tag(s) for %s; using the fallback.",
            len(vocabulary),
            sub_unit.sub_unit_number,
        )
        return fallback_vocabulary(sub_unit)

    return vocabulary


def ensure_vocabulary(
    db: Session, sub_unit: CurriculumSubUnit, llm: Any = None, refresh: bool = False
) -> list[str]:
    """Return this sub-unit's vocabulary, deriving and storing it if absent.

    Stability matters more than perfection here: a vocabulary that changed
    between generation batches would fragment the data all over again, so an
    existing one is reused unless ``refresh`` is set.

    **Commits.** Unusual for a helper handed someone else's session, and
    deliberate: see the comment below.
    """
    existing = validate_vocabulary(sub_unit.skill_tags or [])
    if existing and not refresh:
        return existing

    vocabulary = derive_vocabulary(sub_unit, llm)
    sub_unit.skill_tags = vocabulary

    # Commit, not flush. A flush takes SQLite's single write lock and holds it
    # until the caller commits -- and the caller here is question generation,
    # which then spends the better part of a minute on the model. Anything else
    # trying to write in that window (a parent adding a child, say) waits past
    # `busy_timeout` and fails with "database is locked".
    #
    # Committing is also correct on its own terms: this vocabulary cost a model
    # call, and it is valid whether or not the generation that prompted it
    # succeeds. Rolling it back with a failed batch only means paying for it
    # again.
    db.commit()
    logger.info("Skill vocabulary for %s: %s", sub_unit.sub_unit_number, ", ".join(vocabulary))
    return vocabulary


__all__ = [
    "BANNED_TOKENS",
    "MAX_VOCABULARY",
    "MIN_VOCABULARY",
    "derive_vocabulary",
    "ensure_vocabulary",
    "fallback_vocabulary",
    "normalise_tag",
    "snap_to_vocabulary",
    "validate_vocabulary",
]
