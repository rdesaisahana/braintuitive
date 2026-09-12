"""Quiz Generator Agent.

Writes curriculum-grounded multiple-choice questions for one sub-unit at one
difficulty level, with the immediate Khan-Academy-style feedback baked into
each question rather than bolted on afterwards.

Every question carries:
    * ``explanation``           -- why the right answer is right, addressed to
                                   the student, shown the instant they answer.
    * ``distractor_rationales`` -- for each wrong option, the specific mistake
                                   that leads there. This is what turns a wrong
                                   click into a teaching moment.
    * ``hint``                  -- a nudge that does not give the answer away.
    * ``source_chunk_ids``      -- the retrieved curriculum chunks the question
                                   was written from, so any question can be
                                   traced back to a page of the district guide.

Two entry points, sharing one set of tools:

``generate()``
    The deterministic pipeline used by API routes: retrieve, draft, validate,
    top up. Predictable latency and cost.

``execute()``
    The inherited ReAct loop, for open-ended requests ("the student keeps
    missing negative-number questions, build a remediation set"). The agent
    reasons about which tools to call; the tools do the work.

Structured generation deliberately happens inside a focused tool call rather
than in the ReAct scratchpad -- a ten-question JSON payload squeezed through
Thought/Action/Observation parsing is a reliability problem, not a design.
"""

from __future__ import annotations

import json
import logging
import random
import re
from dataclasses import dataclass, field
from typing import Any

from langchain_core.tools import Tool
from sqlalchemy.orm import Session

from agents.base_agent import BaseAgent
from agents.question_verifier import (
    QuestionVerdict,
    QuestionVerifierAgent,
    Verdict,
    VerificationReport,
)
from config import settings
from db.database import session_scope
from db.models import CurriculumSubUnit, CurriculumUnit, DifficultyLevel, QuestionType
from rag.retrieval import CurriculumRetriever, RetrievalContext
from services.skill_taxonomy import ensure_vocabulary, snap_to_vocabulary

logger = logging.getLogger(__name__)

OPTION_KEYS = ("A", "B", "C", "D")

# A true/false question is a two-option question: A is always "True" and B is
# always "False". Keeping the multiple-choice shape means answering, feedback,
# hints, the bank and picking a quiz up again all work unchanged.
TRUE_FALSE_KEYS = ("A", "B")
TRUE_FALSE_OPTIONS = ({"key": "A", "text": "True"}, {"key": "B", "text": "False"})

# A guess is right half the time on true/false against a quarter of the time on
# four options, so a set has a few of them for variety and no more: about one
# in five is asked for, and three in ten is the most a set will hold.
TRUE_FALSE_SHARE = 0.2
TRUE_FALSE_MAX_SHARE = 0.3

_DEFAULT_MAX_ATTEMPTS = 3
_VERIFIED_MAX_ATTEMPTS = 6


# --------------------------------------------------------------------------- #
# Difficulty specifications
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class DifficultySpec:
    """What a difficulty level means for question writing.

    These drive the prompt, so they are the difference between three tiers of
    genuinely different questions and three tiers of the same question with
    bigger numbers.
    """

    level: DifficultyLevel
    threshold: float
    cognitive_demand: str
    style: str
    guidance: str
    # Extra, tier-specific constraints on how a question may be built. These
    # exist because harder tiers fail in specific, measurable ways: a live fill
    # of Unit 1 saw the verifier reject 1% of beginner questions but 15% of
    # proficient ones, almost all because the stated answer was not among the
    # options or more than one option was defensible.
    construction_rules: tuple[str, ...] = ()

    def to_prompt(self) -> str:
        block = (
            f"DIFFICULTY: {self.level.value} (pass mark {self.threshold:.0f}%)\n"
            f"COGNITIVE DEMAND: {self.cognitive_demand}\n"
            f"QUESTION STYLE: {self.style}\n"
            f"GUIDANCE: {self.guidance}"
        )
        if self.construction_rules:
            listed = "\n".join(f"  - {rule}" for rule in self.construction_rules)
            block += f"\nCONSTRUCTION RULES FOR THIS TIER (mandatory):\n{listed}"
        return block


DIFFICULTY_SPECS: dict[DifficultyLevel, DifficultySpec] = {
    DifficultyLevel.BEGINNER: DifficultySpec(
        level=DifficultyLevel.BEGINNER,
        threshold=70.0,
        cognitive_demand="Recall and single-step procedure.",
        style=(
            "Direct computation or definition. One step to the answer. "
            "Small, friendly numbers. No multi-clause word problems."
        ),
        guidance=(
            "The student is meeting this skill for the first time. Test whether "
            "they can execute the procedure at all. Keep the wording short and "
            "literal."
        ),
        construction_rules=(
            "Work the answer out yourself BEFORE writing any option, then make "
            "the correct option exactly that value.",
        ),
    ),
    DifficultyLevel.INTERMEDIATE: DifficultySpec(
        level=DifficultyLevel.INTERMEDIATE,
        threshold=80.0,
        cognitive_demand="Application across two or three steps.",
        style=(
            "Multi-step problems, short real-world contexts, and moving between "
            "representations (a table to an equation, a fraction to a decimal)."
        ),
        guidance=(
            "The student can already do the basic procedure. Test whether they "
            "can choose it and combine it with another step."
        ),
        construction_rules=(
            "Work the answer out yourself BEFORE writing any option, then make "
            "the correct option exactly that value.",
            "Check each of the other three options is definitively WRONG, not "
            "merely less good. If two could be argued for, rewrite the question.",
            "Show every step of a multi-step calculation in your head before "
            "committing to the answer; sign and order-of-operations slips are "
            "the usual cause of an answer that matches no option.",
        ),
    ),
    DifficultyLevel.PROFICIENT: DifficultySpec(
        level=DifficultyLevel.PROFICIENT,
        threshold=90.0,
        cognitive_demand="Reasoning, transfer and error analysis.",
        style=(
            "Richer word problems, questions asking which reasoning is valid, "
            "spot-the-mistake items, and problems needing a strategy choice."
        ),
        guidance=(
            "The student has the procedure fluent. Test depth: justify a result, "
            "find the flaw in someone else's work, or apply the idea somewhere "
            "unfamiliar. Do not simply use larger numbers."
        ),
        construction_rules=(
            "Work the answer out yourself BEFORE writing any option, then make "
            "the correct option exactly that value. The most common defect at "
            "this tier is a correct answer that appears in none of the options.",
            "Check each of the other three options is definitively WRONG. If two "
            "could be defended, the question is unscorable -- rewrite it.",
            "ERROR-ANALYSIS ITEMS: if you quote a student's answer as wrong, "
            "first compute the correct result and confirm the quoted answer "
            "really does differ from it. Asking a child to explain a mistake in "
            "work that is actually correct is worse than asking nothing.",
            "ERROR-ANALYSIS ITEMS: ask for something computable -- 'What is the "
            "correct answer?' or 'At which step does the error first appear?' -- "
            "not 'Which statement explains the mistake?'. Prose-judgement "
            "options are almost never exactly-one-correct.",
            "DO NOT write counterexample items -- anything of the form 'which "
            "option shows this claim is false'. A universal claim ('always', "
            "'never') is usually disproved by more than one of four options, and "
            "reliably checking all four is where these questions break. Test the "
            "same idea with a computable question instead: not 'which pair shows "
            "the sum is not always greater' but 'for which pair is the sum LESS "
            "than both numbers?'.",
            "Never use the word 'best' in the question stem -- not 'which "
            "statement best explains', not 'the best approximation'. 'Best' "
            "concedes that more than one option is partly right.",
            "Prefer a stem with one numeric or one uniquely-identifiable answer. "
            "If you cannot state in one sentence why the other three options are "
            "wrong, the question is not ready.",
        ),
    ),
}


def spec_for(difficulty: DifficultyLevel | str) -> DifficultySpec:
    """Return the specification for a difficulty level."""
    if isinstance(difficulty, str):
        difficulty = DifficultyLevel(difficulty.lower().strip())
    return DIFFICULTY_SPECS[difficulty]


# --------------------------------------------------------------------------- #
# Generated data structures
# --------------------------------------------------------------------------- #


@dataclass
class GeneratedQuestion:
    """One validated question, ready to become a ``QuizQuestion`` row."""

    question_number: int
    question_text: str
    options: list[dict[str, str]]
    correct_answer: str
    explanation: str
    distractor_rationales: dict[str, str]
    hint: str = ""
    skill_tag: str = ""
    source_chunk_ids: list[str] = field(default_factory=list)
    points: int = 10
    question_type: QuestionType = QuestionType.MULTIPLE_CHOICE

    @property
    def is_true_false(self) -> bool:
        return self.question_type is QuestionType.TRUE_FALSE

    def to_model_kwargs(self, difficulty: DifficultyLevel) -> dict[str, Any]:
        """Keyword arguments for constructing a ``QuizQuestion``."""
        return {
            "question_number": self.question_number,
            "question_text": self.question_text,
            "question_type": self.question_type,
            "options": self.options,
            "correct_answer": self.correct_answer,
            "explanation": self.explanation,
            "distractor_rationales": self.distractor_rationales,
            "hint": self.hint or None,
            "difficulty_level": difficulty,
            "skill_tag": self.skill_tag or None,
            "source_chunk_ids": self.source_chunk_ids,
            "points": self.points,
        }


@dataclass
class GeneratedQuiz:
    """A generated question set plus the provenance of how it was made."""

    sub_unit_id: str
    sub_unit_number: str
    difficulty: DifficultyLevel
    questions: list[GeneratedQuestion] = field(default_factory=list)
    source_chunk_ids: list[str] = field(default_factory=list)
    retrieval_score: float = 0.0
    attempts: int = 0
    warnings: list[str] = field(default_factory=list)
    verification: VerificationReport | None = None
    rejected: list[QuestionVerdict] = field(default_factory=list)

    @property
    def is_complete(self) -> bool:
        return len(self.questions) == settings.QUESTIONS_PER_QUIZ

    def generation_metadata(self) -> dict[str, Any]:
        """Audit trail stored on ``Quiz.generation_metadata``."""
        return {
            "model": settings.NEBIUS_MODEL,
            "embedding_model": settings.NEBIUS_EMBEDDING_MODEL,
            "difficulty": self.difficulty.value,
            "source_chunk_ids": self.source_chunk_ids,
            "top_retrieval_score": round(self.retrieval_score, 4),
            "draft_attempts": self.attempts,
            "question_count": len(self.questions),
            "warnings": self.warnings,
            "verification": self.verification.to_dict() if self.verification else None,
            "rejected_by_verifier": [v.to_dict() for v in self.rejected],
        }


class QuizGenerationError(RuntimeError):
    """Raised when a usable question set could not be produced."""


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #


def _normalise(text: str) -> str:
    """Lowercase and collapse whitespace, for duplicate detection."""
    return re.sub(r"\s+", " ", text or "").strip().lower()


# Model self-talk that leaked into an explanation. A live run produced:
#   "...then -2 + 2 = 0 - wait, that also seems correct? But recheck: ...
#    both A and B are 0? Let's fix this in logic."
# which would have been shown verbatim to a child. These markers are
# deliberately high-precision: "actually" and "note that" are legitimate in
# teaching prose and are not matched.
META_COMMENTARY_RE = re.compile(
    r"(?:\bwait\b\s*[,!?.—-]"
    r"|\blet'?s fix\b"
    r"|\blet me (?:re)?check\b"
    r"|\brecheck\b"
    r"|\bhmm+\b"
    r"|\bas an AI\b"
    r"|\bi (?:made|apologi[sz]e)\b"
    r"|\bmy mistake\b"
    r"|\bon second thought\b"
    r"|\bscratch that\b"
    r"|\boops\b"
    r"|\bignore (?:the|my) (?:previous|above)\b"
    r"|\bthat (?:also )?seems correct\?"
    r"|\bfix this in logic\b)",
    re.IGNORECASE,
)


# Stem shapes that are reliably NOT exactly-one-correct. Measured on a live
# fill: counterexample items ("which pair shows this claim is false") had three
# of four options valid, and "best" stems invite several defensible answers.
# Catching them here is free; catching them at verification costs two model
# calls and a regeneration.
AMBIGUOUS_STEM_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"\bwhich\b[^.?]{0,60}\bbest\b", "asks which option is 'best'"),
    (r"\bthe best\b", "asks for the 'best' option"),
    (r"\bcounter-?examples?\b", "asks for a counterexample"),
    (r"\bdisprove[sd]?\b", "asks the student to disprove a claim"),
    (
        r"\b(shows?|demonstrates?|proves?)\b[^.?]{0,60}"
        r"\b(is|are)\s+(false|incorrect|wrong|not\s+always\s+true)\b",
        "asks which option shows a claim is false",
    ),
    (
        r"\b(always|never)\b[^.?]{0,80}\bwhich\b",
        "tests a universal claim, which several options usually disprove",
    ),
)

_AMBIGUOUS_STEM_RE = tuple(
    (re.compile(pattern, re.IGNORECASE), reason) for pattern, reason in AMBIGUOUS_STEM_PATTERNS
)


def find_ambiguous_stem(question_text: str) -> str | None:
    """Return why a stem is likely to have more than one correct option."""
    for pattern, reason in _AMBIGUOUS_STEM_RE:
        if pattern.search(question_text or ""):
            return reason
    return None


def find_meta_commentary(text: str) -> str | None:
    """Return the self-talk marker found in ``text``, if any."""
    match = META_COMMENTARY_RE.search(text or "")
    return match.group(0).strip() if match else None


def question_type_of(raw: dict[str, Any]) -> QuestionType:
    """The format of a drafted question: true/false or multiple choice.

    Taken from ``question_type`` when the model sets it. Otherwise two options
    reading True and False make it a true/false question, whatever the model
    forgot to say.
    """
    named = "".join(ch for ch in str(raw.get("question_type") or "").lower() if ch.isalpha())
    if named in {"truefalse", "trueorfalse", "tf"}:
        return QuestionType.TRUE_FALSE
    options = raw.get("options")
    if isinstance(options, list) and len(options) == 2:
        texts = {_normalise(str(option.get("text", ""))) for option in options if isinstance(option, dict)}
        if texts == {"true", "false"}:
            return QuestionType.TRUE_FALSE
    return QuestionType.MULTIPLE_CHOICE


def _is_true_false_pair(options: Any) -> bool:  # noqa: ANN401 - raw model output
    """True if the options are exactly A "True" and B "False", in that order."""
    if not isinstance(options, list) or len(options) != 2:
        return False
    if not all(isinstance(option, dict) for option in options):
        return False
    pairs = {
        str(option.get("key", "")).strip().upper(): _normalise(str(option.get("text", "")))
        for option in options
    }
    return pairs == {"A": "true", "B": "false"}


def validate_question(raw: dict[str, Any]) -> list[str]:
    """Check one drafted question. Returns a list of problems (empty = good).

    Structural validation only. Whether the maths is *correct* cannot be
    checked here -- see the module notes in the review docs and
    ``QuizQuestion.source_chunk_ids`` for the provenance trail a human reviewer
    would use.
    """
    issues: list[str] = []

    text = (raw.get("question_text") or "").strip()
    if len(text) < 10:
        issues.append("question_text is missing or too short")
    ambiguous = find_ambiguous_stem(text)
    if ambiguous:
        issues.append(f"stem is unlikely to be exactly-one-correct: {ambiguous}")

    kind = question_type_of(raw)
    allowed = TRUE_FALSE_KEYS if kind is QuestionType.TRUE_FALSE else OPTION_KEYS
    options = raw.get("options")
    if kind is QuestionType.TRUE_FALSE:
        # Fixed options, so True is always on the left and a child never has
        # to read the buttons to know which is which.
        if not _is_true_false_pair(options):
            issues.append('a true/false question needs exactly two options: A "True" and B "False"')
            return issues
    else:
        if not isinstance(options, list) or len(options) != 4:
            issues.append("options must be a list of exactly 4 entries")
            return issues  # everything below depends on well-formed options

        keys = [str(option.get("key", "")).strip().upper() for option in options]
        if sorted(keys) != sorted(OPTION_KEYS):
            issues.append(f"option keys must be exactly A,B,C,D (got {keys})")
            return issues

        texts = [str(option.get("text", "")).strip() for option in options]
        if any(not value for value in texts):
            issues.append("every option needs non-empty text")
        if len({_normalise(value) for value in texts}) != 4:
            issues.append("options contain duplicates")

    correct = str(raw.get("correct_answer", "")).strip().upper()
    if correct not in allowed:
        issues.append(f"correct_answer {correct!r} is not one of {','.join(allowed)}")
        return issues

    explanation = (raw.get("explanation") or "").strip()
    if len(explanation) < 20:
        issues.append("explanation is missing or too short to teach anything")
    leaked = find_meta_commentary(explanation)
    if leaked:
        issues.append(f"explanation contains model self-talk ({leaked!r})")

    rationales = raw.get("distractor_rationales")
    if not isinstance(rationales, dict):
        issues.append("distractor_rationales must be an object")
    else:
        expected = {key for key in allowed if key != correct}
        provided = {str(key).strip().upper() for key in rationales}
        missing = expected - provided
        if missing:
            issues.append(f"distractor_rationales missing keys: {sorted(missing)}")
        if any(len(str(value).strip()) < 10 for value in rationales.values()):
            issues.append("a distractor rationale is too short to be useful")
        for key, value in rationales.items():
            leaked_rationale = find_meta_commentary(str(value))
            if leaked_rationale:
                issues.append(f"rationale {key} contains model self-talk ({leaked_rationale!r})")
                break

    return issues


# Options whose meaning depends on their position; permuting these breaks them.
POSITIONAL_OPTION_RE = re.compile(
    r"\b(all|none|both|either|neither)\s+of\s+(the\s+)?(above|these|below)\b"
    r"|\bboth\s+[A-D]\s+and\s+[A-D]\b"
    r"|\b(answers?|options?|choices?)\s+[A-D]\b",
    re.IGNORECASE,
)


def _has_positional_options(question: GeneratedQuestion) -> bool:
    """True if any option refers to another option by position."""
    return any(POSITIONAL_OPTION_RE.search(option.get("text", "")) for option in question.options)


def rebalance_answer_keys(questions: list[GeneratedQuestion], seed: int | None = None) -> int:
    """Redistribute correct answers evenly across A-D, in place.

    Language models park the correct answer in the same slot with striking
    consistency -- a live run produced ``AAAAAAAAAA``. A student could then
    score 100% by clicking A ten times and be credited with mastery, which
    corrupts the progression the whole product rests on.

    Prompting against this is unreliable, so the fix is mechanical: permute
    each question's options so the correct answer lands on a target key drawn
    from a shuffled round-robin over A-D. Option *text* and its matching
    distractor rationale move together, so the pedagogy is untouched.

    Questions containing position-dependent options ("all of the above") are
    skipped -- reordering those would change what they mean. So are true/false
    questions: True is always A and False always B, and which of the two is
    right is decided by the statement, not by a slot the model favours.

    Args:
        questions: Questions to rebalance, modified in place.
        seed: Optional seed, for reproducible tests.

    Returns:
        How many questions were changed.
    """
    questions = [question for question in questions if not question.is_true_false]
    if not questions:
        return 0

    rng = random.Random(seed)
    targets: list[str] = []
    while len(targets) < len(questions):
        block = list(OPTION_KEYS)
        rng.shuffle(block)
        targets.extend(block)

    changed = 0
    for question, target in zip(questions, targets, strict=False):
        if question.correct_answer == target:
            continue
        if _has_positional_options(question):
            logger.debug("Skipping rebalance for a question with position-dependent options.")
            continue

        texts = {option["key"]: option["text"] for option in question.options}
        if target not in texts or question.correct_answer not in texts:
            continue

        correct_text = texts[question.correct_answer]
        distractors = [
            (texts[key], question.distractor_rationales.get(key, ""))
            for key in OPTION_KEYS
            if key != question.correct_answer
        ]

        remaining = [key for key in OPTION_KEYS if key != target]
        options = [{"key": target, "text": correct_text}]
        rationales: dict[str, str] = {}
        for new_key, (text, rationale) in zip(remaining, distractors, strict=False):
            options.append({"key": new_key, "text": text})
            rationales[new_key] = rationale

        options.sort(key=lambda option: option["key"])
        question.options = options
        question.correct_answer = target
        question.distractor_rationales = rationales
        changed += 1

    return changed


def validate_answer_distribution(
    questions: list[GeneratedQuestion], max_share: float = 0.6
) -> list[str]:
    """Warn when the answer key is lopsided.

    Language models reliably favour one option position. A set where eight of
    ten answers are "C" is guessable without reading the questions, which
    silently breaks the mastery signal the whole progression rests on.

    Only multiple-choice questions count: a true/false answer can only ever be
    A or B, which says nothing about where a model parks its answers.
    """
    questions = [question for question in questions if not question.is_true_false]
    if len(questions) < 4:
        return []

    counts: dict[str, int] = {}
    for question in questions:
        counts[question.correct_answer] = counts.get(question.correct_answer, 0) + 1

    warnings: list[str] = []
    for key, count in sorted(counts.items()):
        share = count / len(questions)
        if share > max_share:
            warnings.append(
                f"answer key {key} is used in {count}/{len(questions)} questions "
                f"({share:.0%}) - the set is guessable"
            )
    return warnings


# --------------------------------------------------------------------------- #
# Prompts
# --------------------------------------------------------------------------- #

SYSTEM_PROMPT = """You are an expert mathematics item writer for a district \
curriculum team. You write multiple-choice and true/false questions that are \
precisely aligned to a stated learning objective and grounded in the district's own curriculum \
text.

Your questions are used by students working alone, so the feedback attached to \
each question has to teach on its own: explain the correct reasoning, and name \
the specific misconception behind every wrong option.

Think step by step. Use the tools to look up the sub-unit, retrieve the \
curriculum it is based on, and check the difficulty specification before you \
write anything. Never invent curriculum content that the retrieved text does \
not support. If the retrieved context is too thin to write good questions, say \
so plainly instead of guessing."""


DRAFT_PROMPT = """Write {count} questions for a Grade {grade} {subject} student.

=== CURRICULUM CONTEXT (your only source of truth) ===
{context}

=== TARGET ===
Unit {unit_number}: {unit_title}
Sub-unit {sub_unit_number}: {sub_unit_title}
LEARNING OBJECTIVE: {objective}

=== {difficulty_block} ===

=== RULES ===
1. Every question must test the learning objective above, nothing else.
2. Stay inside the curriculum context. Do not introduce notation or topics it
   does not support.
3. Question formats:
   - Multiple choice: exactly 4 options with keys "A", "B", "C", "D", and
     "question_type": "multiple_choice".
{true_false_rule}
4. Multiple choice: vary which key is correct. Do not put the answer in the
   same position twice in a row, and do not favour any one key across the set.
5. Every distractor must be the result of a specific, plausible student error,
   not a random number.
6. "explanation": 2-3 sentences addressed to the student as "you", teaching the
   correct reasoning. This is shown the instant they answer.
7. "distractor_rationales": one sentence for EACH incorrect key, naming the
   exact mistake that leads to it.
8. "hint": the first real step, in a child's own words -- not the name of a
   strategy. Say what to actually do, and turn any word the question uses into
   the maths it means: descends/falls/loses means subtract, rises/climbs/gains
   means add, "of" in a percent problem means multiply. Never give the answer
   away.
   GOOD: "Start at -150. Rising 80 means add 80, then descending 120 means
   subtract 120."
   GOOD: "Move the 3 to the other side, where it becomes -3. Then divide by 2."
   BAD: "Undo subtraction first, then undo multiplication." (names a strategy
   instead of the step)
   BAD: "Model each movement as an integer and apply them in order." (words a
   child does not use)
9. Write mathematics in plain text: -3 + 8, 3/4, x^2, sqrt(16), 25%. No LaTeX,
   no markdown.
10. "skill_tag": choose EXACTLY ONE from this list, copied verbatim:
{skill_vocabulary}
    Do not invent a new label, do not combine two, do not add qualifiers.
    Pick the closest one. Tracking which skill a child is stuck on only
    works if the same skill always gets the same name.
{avoid_block}
=== OUTPUT ===
Return ONLY a JSON array of {count} objects. No prose, no markdown fence.
Each object must have exactly these fields:
{{
  "question_text": "string",
  "options": [{{"key": "A", "text": "string"}}, {{"key": "B", "text": "string"}},
              {{"key": "C", "text": "string"}}, {{"key": "D", "text": "string"}}],
  "correct_answer": "A",
  "explanation": "string",
  "distractor_rationales": {{"B": "string", "C": "string", "D": "string"}},
  "hint": "string",
  "skill_tag": "string",
  "question_type": "multiple_choice"
}}
A true/false object has the same fields with "question_type": "true_false",
the options [{{"key": "A", "text": "True"}}, {{"key": "B", "text": "False"}}],
"correct_answer" "A" if the statement is true or "B" if it is false, and one
rationale, for the wrong key."""


def true_false_count(count: int) -> int:
    """How many of a batch of ``count`` questions to ask for as true/false."""
    return int(count * TRUE_FALSE_SHARE + 0.5)


def true_false_rule(true_false: int, count: int) -> str:
    """Rule 3's true/false line: how many to write, and how to write them."""
    if true_false <= 0:
        return "   - Write every question as multiple choice this time."
    return (
        f"   - True/false: write {true_false} of the {count} as true/false, the rest as\n"
        "     multiple choice. \"question_text\" is one statement, not a question, and\n"
        "     it must be definitely true or definitely false as written -- never true\n"
        "     in some cases and false in others. Make some true and some false; a false\n"
        "     statement is false because of one specific, common mistake, which its\n"
        "     rationale names. Do not start it with \"True or false\"."
    )


# --------------------------------------------------------------------------- #
# Agent
# --------------------------------------------------------------------------- #


class QuizGeneratorAgent(BaseAgent):
    """Generates curriculum-grounded quizzes for a sub-unit and difficulty.

    Example:
        agent = QuizGeneratorAgent()
        quiz = agent.generate(sub_unit_id="...", difficulty=DifficultyLevel.BEGINNER)
        print(quiz.questions[0].explanation)
    """

    def __init__(
        self,
        retriever: CurriculumRetriever | None = None,
        temperature: float = 0.4,
        batch_size: int = 5,
        max_attempts: int = _DEFAULT_MAX_ATTEMPTS,
        rebalance_answers: bool = True,
        rebalance_seed: int | None = None,
        verifier: QuestionVerifierAgent | None = None,
        verify: bool = False,
        **kwargs: Any,
    ) -> None:
        # Ten questions with explanations and three rationales each will not fit
        # in the 2048-token conversational default.
        kwargs.setdefault("max_tokens", 4096)
        super().__init__(agent_name="QuizGenerator", temperature=temperature, **kwargs)
        self.retriever = retriever or CurriculumRetriever()
        # Smaller batches keep each response inside the token budget and
        # measurably improve question quality over asking for all ten at once.
        self.batch_size = batch_size
        self.max_attempts = max_attempts
        self.rebalance_answers = rebalance_answers
        self.rebalance_seed = rebalance_seed
        # Verification roughly doubles cost and latency, so it is opt-in.
        # Anything student-facing should turn it on.
        self.verifier = verifier or (QuestionVerifierAgent() if verify else None)
        if self.verifier is not None and max_attempts == _DEFAULT_MAX_ATTEMPTS:
            # Verification rejects questions, so the top-up loop needs more
            # room. A live proficient run exhausted 3 attempts at 8/10.
            self.max_attempts = _VERIFIED_MAX_ATTEMPTS

    # ------------------------------------------------------------------ #
    # BaseAgent contract
    # ------------------------------------------------------------------ #

    def get_system_prompt(self) -> str:
        return SYSTEM_PROMPT

    def get_tools(self) -> list[Tool]:
        return [
            Tool(
                name="lookup_sub_unit",
                func=self._tool_lookup_sub_unit,
                description=(
                    "Look up a sub-unit's title, learning objective, unit and grade. "
                    "Input: the sub-unit id, or 'unit.subunit' such as '1.2'. "
                    "Always call this first so you know what you are writing about."
                ),
            ),
            Tool(
                name="search_curriculum",
                func=self._tool_search_curriculum,
                description=(
                    "Retrieve the district curriculum text for a sub-unit. "
                    'Input: JSON such as {"sub_unit_number": "1.2", "unit_number": 1, '
                    '"grade_level": 6}. Returns cited curriculum extracts. Call this '
                    "before writing questions; it is the only approved source."
                ),
            ),
            Tool(
                name="difficulty_spec",
                func=self._tool_difficulty_spec,
                description=(
                    "Get the writing specification for a difficulty level. "
                    "Input: 'beginner', 'intermediate' or 'proficient'. Returns the "
                    "cognitive demand, question style and pass mark."
                ),
            ),
            Tool(
                name="draft_questions",
                func=self._tool_draft_questions,
                description=(
                    "Write a batch of questions from retrieved curriculum. "
                    'Input: JSON such as {"sub_unit_id": "...", "difficulty": '
                    '"beginner", "count": 5}. Returns the drafted questions as JSON. '
                    "Use only after search_curriculum."
                ),
            ),
            Tool(
                name="validate_questions",
                func=self._tool_validate_questions,
                description=(
                    "Check drafted questions for structural problems: wrong option "
                    "keys, missing explanations, missing distractor rationales, a "
                    "lopsided answer key. Input: the JSON array of questions. "
                    "Returns 'VALID' or a numbered list of problems to fix."
                ),
            ),
        ]

    # ------------------------------------------------------------------ #
    # Tools
    # ------------------------------------------------------------------ #

    @staticmethod
    def _parse_tool_input(raw: str) -> dict[str, Any]:
        """Parse a tool argument that may be JSON or a bare string."""
        text = (raw or "").strip().strip("`")
        if text.startswith("{"):
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                pass
        return {"value": text}

    def _tool_lookup_sub_unit(self, raw: str) -> str:
        """Resolve a sub-unit reference to its curriculum details."""
        argument = self._parse_tool_input(raw)
        reference = str(argument.get("sub_unit_id") or argument.get("value") or "").strip()
        if not reference:
            return "ERROR: provide a sub-unit id or a number such as '1.2'."

        try:
            with session_scope() as db:
                sub_unit = self._resolve_sub_unit(db, reference)
                if sub_unit is None:
                    return f"ERROR: no sub-unit matching {reference!r}."
                unit = sub_unit.unit
                return json.dumps(
                    {
                        "sub_unit_id": sub_unit.id,
                        "sub_unit_number": sub_unit.sub_unit_number,
                        "title": sub_unit.title,
                        "objective": sub_unit.description,
                        "skill_tags": sub_unit.skill_tags or [],
                        "unit_number": unit.unit_number,
                        "unit_title": unit.title,
                        "subject": unit.subject,
                        "grade_level": unit.grade_level,
                        "is_indexed": sub_unit.is_indexed,
                    },
                    indent=2,
                )
        except Exception as exc:
            logger.exception("lookup_sub_unit failed")
            return f"ERROR: {exc}"

    def _tool_search_curriculum(self, raw: str) -> str:
        """Retrieve grounding curriculum for a sub-unit."""
        argument = self._parse_tool_input(raw)
        try:
            with session_scope() as db:
                reference = str(
                    argument.get("sub_unit_id")
                    or argument.get("sub_unit_number")
                    or argument.get("value")
                    or ""
                ).strip()
                sub_unit = self._resolve_sub_unit(db, reference)
                if sub_unit is None:
                    return f"ERROR: no sub-unit matching {reference!r}."
                context = self._retrieve(sub_unit)

            if context.is_empty:
                return (
                    "No curriculum was retrieved. The sub-unit may not be indexed yet; "
                    "run the ingestion pipeline before generating questions."
                )
            return context.to_prompt_block()
        except Exception as exc:
            logger.exception("search_curriculum failed")
            return f"ERROR: {exc}"

    def _tool_difficulty_spec(self, raw: str) -> str:
        """Return the writing specification for a difficulty level."""
        argument = self._parse_tool_input(raw)
        value = str(argument.get("difficulty") or argument.get("value") or "").strip()
        try:
            return spec_for(value).to_prompt()
        except (KeyError, ValueError):
            return "ERROR: difficulty must be 'beginner', 'intermediate' or 'proficient'."

    def _tool_draft_questions(self, raw: str) -> str:
        """Draft a batch of questions and return them as JSON."""
        argument = self._parse_tool_input(raw)
        reference = str(
            argument.get("sub_unit_id")
            or argument.get("sub_unit_number")
            or argument.get("value")
            or ""
        ).strip()
        difficulty = str(argument.get("difficulty") or "beginner")
        count = int(argument.get("count") or self.batch_size)

        try:
            quiz = self.generate(sub_unit=reference, difficulty=difficulty, count=count)
        except Exception as exc:
            logger.exception("draft_questions failed")
            return f"ERROR: {exc}"

        return json.dumps(
            [
                {
                    "question_text": question.question_text,
                    "options": question.options,
                    "correct_answer": question.correct_answer,
                    "explanation": question.explanation,
                    "distractor_rationales": question.distractor_rationales,
                    "hint": question.hint,
                    "skill_tag": question.skill_tag,
                    "question_type": question.question_type.value,
                }
                for question in quiz.questions
            ],
            indent=2,
        )

    def _tool_validate_questions(self, raw: str) -> str:
        """Validate a JSON array of questions."""
        parsed = self.parse_json_output(raw)
        if not isinstance(parsed, list):
            return "ERROR: expected a JSON array of question objects."

        problems: list[str] = []
        accepted: list[GeneratedQuestion] = []
        for index, item in enumerate(parsed, start=1):
            if not isinstance(item, dict):
                problems.append(f"{index}. not an object")
                continue
            issues = validate_question(item)
            if issues:
                problems.extend(f"{index}. {issue}" for issue in issues)
            else:
                accepted.append(self._to_question(item, index))

        problems.extend(validate_answer_distribution(accepted))
        return "VALID" if not problems else "\n".join(problems)

    # ------------------------------------------------------------------ #
    # Deterministic pipeline
    # ------------------------------------------------------------------ #

    @staticmethod
    def _resolve_sub_unit(db: Session, reference: str) -> CurriculumSubUnit | None:
        """Find a sub-unit by id or by number such as '1.2'."""
        if not reference:
            return None

        sub_unit = db.get(CurriculumSubUnit, reference)
        if sub_unit is not None:
            return sub_unit

        return (
            db.query(CurriculumSubUnit)
            .join(CurriculumUnit)
            .filter(CurriculumSubUnit.sub_unit_number == reference)
            .order_by(CurriculumUnit.grade_level)
            .first()
        )

    def _retrieve(self, sub_unit: CurriculumSubUnit) -> RetrievalContext:
        """Fetch grounding curriculum for a sub-unit.

        The namespace comes off the sub-unit rather than being recomputed from
        subject and grade. An uploaded curriculum lives in its owner's own
        namespace, and recomputing would send the search to the shared one --
        returning material from a different curriculum, confidently and
        without erroring.
        """
        unit = sub_unit.unit
        return self.retriever.for_sub_unit(
            subject=unit.subject,
            grade_level=unit.grade_level,
            unit_number=unit.unit_number,
            sub_unit_number=sub_unit.sub_unit_number,
            objective=sub_unit.description or sub_unit.title,
            namespace=sub_unit.vector_namespace,
        )

    @staticmethod
    def _to_question(
        raw: dict[str, Any], number: int, chunk_ids: list[str] | None = None
    ) -> GeneratedQuestion:
        """Convert a validated raw dict into a :class:`GeneratedQuestion`."""
        kind = question_type_of(raw)
        if kind is QuestionType.TRUE_FALSE:
            options = [dict(option) for option in TRUE_FALSE_OPTIONS]
        else:
            options = [
                {"key": str(option["key"]).strip().upper(), "text": str(option["text"]).strip()}
                for option in raw["options"]
            ]
            options.sort(key=lambda option: option["key"])
        rationales = {
            str(key).strip().upper(): str(value).strip()
            for key, value in (raw.get("distractor_rationales") or {}).items()
        }
        return GeneratedQuestion(
            question_number=number,
            question_text=str(raw["question_text"]).strip(),
            options=options,
            correct_answer=str(raw["correct_answer"]).strip().upper(),
            explanation=str(raw["explanation"]).strip(),
            distractor_rationales=rationales,
            hint=str(raw.get("hint") or "").strip(),
            skill_tag=str(raw.get("skill_tag") or "").strip()[:120],
            source_chunk_ids=list(chunk_ids or []),
            question_type=kind,
        )

    def _draft_batch(
        self,
        context: RetrievalContext,
        sub_unit: CurriculumSubUnit,
        spec: DifficultySpec,
        count: int,
        avoid: list[str],
        vocabulary: list[str] | None = None,
        true_false: int | None = None,
    ) -> list[dict[str, Any]]:
        """Ask the model for ``count`` questions and parse the JSON array.

        ``true_false`` is how many of them to write as true/false; by default
        about one in five.
        """
        unit = sub_unit.unit
        if true_false is None:
            true_false = true_false_count(count)
        avoid_block = ""
        if avoid:
            listed = "\n".join(f"   - {text}" for text in avoid[:12])
            avoid_block = (
                "11. Do NOT repeat or lightly reword any of these already-written "
                f"questions:\n{listed}\n"
            )

        listed_skills = "\n".join(f"      - {tag}" for tag in (vocabulary or []))
        prompt = DRAFT_PROMPT.format(
            skill_vocabulary=listed_skills or "      (no vocabulary; use a short snake_case label)",
            count=count,
            grade=unit.grade_level,
            subject=unit.subject,
            context=context.to_prompt_block(max_chars=5000),
            unit_number=unit.unit_number,
            unit_title=unit.title,
            sub_unit_number=sub_unit.sub_unit_number,
            sub_unit_title=sub_unit.title,
            objective=sub_unit.description or sub_unit.title,
            difficulty_block=spec.to_prompt(),
            avoid_block=avoid_block,
            true_false_rule=true_false_rule(max(0, min(true_false, count)), count),
        )

        response = self.get_llm().invoke(
            [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ]
        )
        parsed = self.parse_json_output(getattr(response, "content", "") or "")
        if isinstance(parsed, dict):
            # Models sometimes wrap the array in {"questions": [...]}.
            for value in parsed.values():
                if isinstance(value, list):
                    parsed = value
                    break
        if not isinstance(parsed, list):
            logger.warning("Draft response was not a JSON array; discarding batch.")
            return []
        return [item for item in parsed if isinstance(item, dict)]

    def _verify_candidates(
        self, candidates: list[GeneratedQuestion], quiz: GeneratedQuiz
    ) -> list[GeneratedQuestion]:
        """Independently check candidates, returning only those fit to ship.

        Runs before the questions are accepted into the set, so anything the
        verifier disputes is replaced by the existing top-up loop rather than
        simply lost.

        A question whose verification *errored* is kept: a transient API
        failure should not silently shrink a student's quiz. It is recorded in
        the report so the gap is visible.
        """
        if self.verifier is None or not candidates:
            return candidates

        report = self.verifier.verify(candidates)
        if quiz.verification is None:
            quiz.verification = report
        else:
            quiz.verification.verdicts.extend(report.verdicts)

        by_number = {verdict.question_number: verdict for verdict in report.verdicts}
        accepted: list[GeneratedQuestion] = []

        for question in candidates:
            verdict = by_number.get(question.question_number)
            if verdict is None or verdict.verdict in (Verdict.AGREED, Verdict.ERROR):
                if verdict is not None and verdict.verdict is Verdict.ERROR:
                    logger.warning(
                        "Keeping Q%d unverified: %s",
                        question.question_number,
                        "; ".join(verdict.issues) or "verification errored",
                    )
                accepted.append(question)
                continue

            quiz.rejected.append(verdict)
            logger.warning("Rejected by verifier -> %s", verdict.summary())

        # Renumber the survivors so the set stays contiguous.
        for position, question in enumerate(accepted, start=len(quiz.questions) + 1):
            question.question_number = position

        return accepted

    @staticmethod
    def _refresh_verdict_keys(quiz: GeneratedQuiz) -> None:
        """Re-point surviving verdicts at the post-rebalance answer keys.

        Rebalancing moves the correct answer to a different letter while
        keeping its text, so a verdict recorded before the shuffle would cite a
        stale key in the audit trail. Only questions the verifier agreed with
        survive to this point, so the verifier's answer is the correct one by
        definition.
        """
        if quiz.verification is None:
            return

        surviving = {question.question_number: question for question in quiz.questions}
        for verdict in quiz.verification.verdicts:
            question = surviving.get(verdict.question_number)
            if question is None or verdict.verdict is not Verdict.AGREED:
                continue
            verdict.claimed_answer = question.correct_answer
            verdict.verifier_answer = question.correct_answer

    def get_llm_or_none(self) -> Any:
        """The LLM if one can be built, else None.

        Vocabulary derivation falls back to a deterministic rule when the
        model is unavailable, so a missing key must not raise here.
        """
        try:
            return self.get_llm()
        except Exception:
            return None

    @staticmethod
    def _resolve_skill_tag(tag: str, vocabulary: list[str]) -> str:
        """Force a generated tag onto the controlled vocabulary.

        Snapping rather than rejecting: a question labelled
        ``absolute_value_of_zero`` when the vocabulary says ``absolute_value``
        is a good question with a verbose label, and discarding it would waste
        a generation. Anything unrecognisable falls to the first vocabulary
        entry, which keeps the bucket count bounded -- the entire point.
        """
        if not vocabulary:
            return tag
        snapped = snap_to_vocabulary(tag, vocabulary)
        if snapped is None:
            logger.info("Skill tag %r did not match the vocabulary; using %r.", tag, vocabulary[0])
            return vocabulary[0]
        if snapped != tag:
            logger.debug("Snapped skill tag %r -> %r", tag, snapped)
        return snapped

    def generate(
        self,
        sub_unit: str,
        difficulty: DifficultyLevel | str,
        count: int | None = None,
        db: Session | None = None,
    ) -> GeneratedQuiz:
        """Generate a validated question set for one sub-unit and difficulty.

        Drafts in batches, validates each question, discards the bad ones and
        tops the set up over repeated attempts. Partial sets are returned with
        a warning rather than raising, so a route can still serve nine good
        questions instead of failing outright.

        Args:
            sub_unit: Sub-unit id, or a number such as ``"1.2"``.
            difficulty: The tier to write for.
            count: Questions wanted. Defaults to ``QUESTIONS_PER_QUIZ``.
            db: Optional session; one is opened if omitted.

        Returns:
            A :class:`GeneratedQuiz`.

        Raises:
            QuizGenerationError: If the sub-unit is unknown, has no indexed
                curriculum, or no valid question could be produced at all.
        """
        count = count or settings.QUESTIONS_PER_QUIZ
        spec = spec_for(difficulty)

        if db is not None:
            return self._generate_with_session(db, sub_unit, spec, count)
        with session_scope() as session:
            return self._generate_with_session(session, sub_unit, spec, count)

    def _generate_with_session(
        self, db: Session, reference: str, spec: DifficultySpec, count: int
    ) -> GeneratedQuiz:
        resolved = self._resolve_sub_unit(db, reference)
        if resolved is None:
            raise QuizGenerationError(f"No sub-unit matching {reference!r}.")

        context = self._retrieve(resolved)
        if context.is_empty:
            raise QuizGenerationError(
                f"No curriculum retrieved for sub-unit {resolved.sub_unit_number}. "
                "Run the ingestion pipeline before generating questions."
            )

        # One vocabulary for the whole run: deriving it per batch would let it
        # drift and fragment the data all over again.
        vocabulary = ensure_vocabulary(db, resolved, llm=self.get_llm_or_none())

        quiz = GeneratedQuiz(
            sub_unit_id=resolved.id,
            sub_unit_number=resolved.sub_unit_number,
            difficulty=spec.level,
            source_chunk_ids=context.chunk_ids,
            retrieval_score=context.hits[0].score if context.hits else 0.0,
        )

        seen: set[str] = set()
        true_false_cap = max(1, int(count * TRUE_FALSE_MAX_SHARE))
        while len(quiz.questions) < count and quiz.attempts < self.max_attempts:
            quiz.attempts += 1
            wanted = min(self.batch_size, count - len(quiz.questions))
            true_false_held = sum(question.is_true_false for question in quiz.questions)
            batch = self._draft_batch(
                context=context,
                sub_unit=resolved,
                spec=spec,
                count=wanted,
                avoid=[question.question_text for question in quiz.questions],
                vocabulary=vocabulary,
                true_false=min(true_false_count(wanted), true_false_cap - true_false_held),
            )

            # Structural validation and de-duplication first: no point paying
            # for verification on a question that is already malformed.
            candidates: list[GeneratedQuestion] = []
            for item in batch:
                if len(quiz.questions) + len(candidates) >= count:
                    break
                issues = validate_question(item)
                if issues:
                    logger.info("Discarded a question: %s", "; ".join(issues))
                    continue
                key = _normalise(str(item.get("question_text", "")))
                if key in seen:
                    logger.info("Discarded a duplicate question.")
                    continue
                if question_type_of(item) is QuestionType.TRUE_FALSE:
                    if true_false_held >= true_false_cap:
                        logger.info("Discarded a true/false question: the set has enough.")
                        continue
                    true_false_held += 1
                seen.add(key)
                candidate = self._to_question(
                    item,
                    len(quiz.questions) + len(candidates) + 1,
                    chunk_ids=context.chunk_ids,
                )
                candidate.skill_tag = self._resolve_skill_tag(candidate.skill_tag, vocabulary)
                candidates.append(candidate)

            quiz.questions.extend(self._verify_candidates(candidates, quiz))

            logger.info(
                "%s: attempt %d -> %d/%d questions.",
                self.agent_name,
                quiz.attempts,
                len(quiz.questions),
                count,
            )

        if not quiz.questions:
            raise QuizGenerationError(
                f"No valid questions could be generated for sub-unit "
                f"{resolved.sub_unit_number} at {spec.level.value} after "
                f"{quiz.attempts} attempt(s)."
            )

        if len(quiz.questions) < count:
            quiz.warnings.append(
                f"only {len(quiz.questions)} of {count} questions passed validation"
            )

        if self.rebalance_answers:
            moved = rebalance_answer_keys(quiz.questions, seed=self.rebalance_seed)
            if moved:
                logger.info(
                    "%s: rebalanced the answer key on %d/%d question(s).",
                    self.agent_name,
                    moved,
                    len(quiz.questions),
                )

        self._refresh_verdict_keys(quiz)

        # Re-check after rebalancing: any remaining warning is real.
        quiz.warnings.extend(validate_answer_distribution(quiz.questions))

        if quiz.rejected:
            quiz.warnings.append(
                f"{len(quiz.rejected)} question(s) rejected by the verifier " f"and regenerated"
            )
        if quiz.verification is not None:
            unverified = [v for v in quiz.verification.verdicts if v.verdict is Verdict.ERROR]
            if unverified:
                quiz.warnings.append(
                    f"{len(unverified)} question(s) could not be verified and were kept"
                )

        for warning in quiz.warnings:
            logger.warning("%s: %s", self.agent_name, warning)

        return quiz


__all__ = [
    "DIFFICULTY_SPECS",
    "DifficultySpec",
    "GeneratedQuestion",
    "GeneratedQuiz",
    "QuizGenerationError",
    "QuizGeneratorAgent",
    "rebalance_answer_keys",
    "find_ambiguous_stem",
    "find_meta_commentary",
    "spec_for",
    "validate_answer_distribution",
    "validate_question",
]
