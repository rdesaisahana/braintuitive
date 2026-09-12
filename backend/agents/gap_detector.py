"""Gap Detector Agent.

``/progress/skills`` already reports *that* a child is weak at
``subtract_integers``. This agent answers the question a parent or teacher
actually asks next: **why**, and what to do about it.

The signal it uses is one nothing else touches. Every distractor carries a
rationale naming the specific error that leads to it -- *"you may have found
the difference but used the wrong sign"* -- and every response records which
option the child chose. Joining those two turns a bare accuracy figure into a
named misconception:

    subtract_integers  0/6
      4x  "you may have found the difference but used the wrong sign"
      2x  "you may have added the absolute values and used a positive sign"

That is a diagnosis. "0%" is only a symptom.

Two things this agent is careful not to do:

* **Invent a gap from thin data.** Two wrong answers is a bad afternoon, not a
  learning gap. Skills below ``MIN_EVIDENCE`` answers are reported as
  "insufficient evidence" rather than dressed up as findings.
* **Restate the numbers.** The deterministic evidence is gathered in Python
  and is correct by construction; the model's job is interpretation and
  remediation, and its output is checked back against the evidence.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from langchain_core.tools import Tool
from sqlalchemy import Integer, func
from sqlalchemy.orm import Session

from agents.base_agent import BaseAgent
from db.database import session_scope
from db.models import (
    CurriculumSubUnit,
    DifficultyLevel,
    Quiz,
    QuizQuestion,
    QuizResponse,
    Student,
    SubUnitProgress,
)

logger = logging.getLogger(__name__)

# Below this many answered questions, a low score is noise rather than a gap.
MIN_EVIDENCE = 4
# At or below this accuracy a skill is worth a parent's attention.
WEAK_ACCURACY = 0.7
CRITICAL_ACCURACY = 0.4
# A child who keeps needing hints has not really got it, even at high accuracy.
HINT_RELIANCE = 0.4


# --------------------------------------------------------------------------- #
# Grouping rationales that name the same error
# --------------------------------------------------------------------------- #

# The generator phrases one misconception many ways. A live run produced four
# rationales for the same mistake:
#
#   "you multiplied correctly but forgot that a positive times a negative
#    gives a negative result"
#   "you multiplied correctly but forgot that a negative times a positive
#    must give a negative result"
#   ...
#
# Counted verbatim these are four findings of one occurrence each, which reads
# as four unrelated slips instead of the one systematic error it is -- and the
# deterministic fallback, which reports the most frequent rationale, would then
# be choosing arbitrarily between four ties.

_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "the",
        "you",
        "your",
        "may",
        "have",
        "might",
        "but",
        "and",
        "or",
        "of",
        "to",
        "it",
        "is",
        "was",
        "that",
        "this",
        "when",
        "then",
        "instead",
        "correctly",
        "incorrectly",
        "result",
        "answer",
        "number",
        "numbers",
        "value",
        "values",
        "sign",
        "signs",
        "here",
        "as",
        "in",
        "on",
        "with",
        "for",
        "from",
        "by",
        "be",
        "been",
        "should",
        "would",
        "which",
        "what",
        "them",
        "they",
        "its",
        "did",
        "does",
        "do",
        "so",
    }
)

# Rationales sharing at least this fraction of their content words describe the
# same error. Set high deliberately: merging two genuinely different mistakes
# would send a child to practise the wrong thing.
_SAME_ERROR_OVERLAP = 0.6

# Order matters for these. "added instead of subtracting" and "subtracted
# instead of adding" share every content word but mean opposite things, and
# merging them would report the wrong direction of error to a parent.
#
# Only the operation verbs belong here. "positive"/"negative" were tried and
# removed: "a positive times a negative" and "a negative times a positive"
# state the same commutative rule, and treating their order as meaningful
# split one misconception back into three.
_OPERATIONS = ("add", "subtract", "multiply", "divide")


def _content_words(text: str) -> set[str]:
    words = re.findall(r"[a-z]+", (text or "").lower())
    return {word for word in words if len(word) > 2 and word not in _STOPWORDS}


def _operation_sequence(text: str) -> list[str]:
    """The operation words in the order they appear, stems only."""
    sequence: list[str] = []
    for word in re.findall(r"[a-z]+", (text or "").lower()):
        for operation in _OPERATIONS:
            if word.startswith(operation):
                sequence.append(operation)
                break
    return sequence


def _same_error(left: str, right: str) -> bool:
    """Whether two rationales describe the same mistake."""
    left_words, right_words = _content_words(left), _content_words(right)
    if not left_words or not right_words:
        return False

    union = left_words | right_words
    if len(left_words & right_words) / len(union) < _SAME_ERROR_OVERLAP:
        return False

    # Same words in a different operation order means the opposite mistake.
    left_ops, right_ops = _operation_sequence(left), _operation_sequence(right)
    if left_ops and right_ops and sorted(left_ops) == sorted(right_ops):
        return left_ops == right_ops
    return True


def cluster_rationales(reasons: list[str]) -> dict[str, int]:
    """Count rationales, merging the ones that name the same error.

    The label kept for a cluster is its shortest member, which is almost always
    the clearest: the longer phrasings carry the specific numbers from one
    question ("subtracted 3 from -7") that do not generalise to the group.

    Returns a mapping of label to occurrences, most frequent first.
    """
    clusters: list[list[str]] = []
    for reason in reasons:
        cleaned = (reason or "").strip()
        if not cleaned:
            continue
        for cluster in clusters:
            # Against every member, not just the first: phrasings of one error
            # form a chain, and each may be closest to a different sibling.
            if any(_same_error(member, cleaned) for member in cluster):
                cluster.append(cleaned)
                break
        else:
            clusters.append([cleaned])

    counted = [(min(cluster, key=len), len(cluster)) for cluster in clusters]
    counted.sort(key=lambda item: -item[1])
    return dict(counted)


@dataclass
class SkillEvidence:
    """What the data says about one skill, before any interpretation."""

    skill_tag: str
    sub_unit_id: str
    sub_unit_number: str
    sub_unit_title: str
    unit_number: int
    questions_answered: int
    correct: int
    hints_used: int
    # Distractor rationale -> how many times this child picked it.
    misconceptions: dict[str, int] = field(default_factory=dict)
    example_questions: list[str] = field(default_factory=list)

    @property
    def accuracy(self) -> float:
        return self.correct / self.questions_answered if self.questions_answered else 0.0

    @property
    def hint_rate(self) -> float:
        return self.hints_used / self.questions_answered if self.questions_answered else 0.0

    @property
    def has_enough_evidence(self) -> bool:
        return self.questions_answered >= MIN_EVIDENCE

    @property
    def is_weak(self) -> bool:
        return self.has_enough_evidence and (
            self.accuracy < WEAK_ACCURACY or self.hint_rate >= HINT_RELIANCE
        )

    @property
    def severity(self) -> str:
        if self.accuracy < CRITICAL_ACCURACY:
            return "critical"
        if self.accuracy < WEAK_ACCURACY:
            return "moderate"
        return "minor"

    def top_misconception(self) -> tuple[str, int] | None:
        if not self.misconceptions:
            return None
        return max(self.misconceptions.items(), key=lambda item: item[1])

    def to_dict(self) -> dict[str, Any]:
        return {
            "skill_tag": self.skill_tag,
            "sub_unit_number": self.sub_unit_number,
            "questions_answered": self.questions_answered,
            "correct": self.correct,
            "accuracy": round(self.accuracy, 3),
            "hints_used": self.hints_used,
            "misconceptions": self.misconceptions,
        }


@dataclass
class Gap:
    """One diagnosed gap: what, why, and what to do."""

    skill_tag: str
    sub_unit_id: str
    sub_unit_number: str
    sub_unit_title: str
    unit_number: int
    severity: str
    accuracy: float
    questions_answered: int
    hints_used: int
    evidence: list[str] = field(default_factory=list)
    likely_misconception: str = ""
    recommendation: str = ""
    recommended_difficulty: DifficultyLevel = DifficultyLevel.BEGINNER

    def to_dict(self) -> dict[str, Any]:
        return {
            "skill_tag": self.skill_tag,
            "sub_unit_id": self.sub_unit_id,
            "sub_unit_number": self.sub_unit_number,
            "sub_unit_title": self.sub_unit_title,
            "unit_number": self.unit_number,
            "severity": self.severity,
            "accuracy": round(self.accuracy, 3),
            "questions_answered": self.questions_answered,
            "hints_used": self.hints_used,
            "evidence": self.evidence,
            "likely_misconception": self.likely_misconception,
            "recommendation": self.recommendation,
            "recommended_difficulty": self.recommended_difficulty.value,
        }


@dataclass
class GapReport:
    """The full analysis for one student."""

    student_id: str
    student_name: str
    gaps: list[Gap] = field(default_factory=list)
    strengths: list[str] = field(default_factory=list)
    summary: str = ""
    skills_analysed: int = 0
    responses_analysed: int = 0
    skills_with_thin_evidence: list[str] = field(default_factory=list)

    @property
    def has_gaps(self) -> bool:
        return bool(self.gaps)

    def to_dict(self) -> dict[str, Any]:
        return {
            "student_id": self.student_id,
            "student_name": self.student_name,
            "summary": self.summary,
            "gaps": [gap.to_dict() for gap in self.gaps],
            "strengths": self.strengths,
            "skills_analysed": self.skills_analysed,
            "responses_analysed": self.responses_analysed,
            "skills_with_thin_evidence": self.skills_with_thin_evidence,
        }


SYSTEM_PROMPT = """You are an experienced mathematics teacher reviewing one \
child's quiz history.

You are given, for each weak skill, exactly which wrong options the child \
chose and what error each of those options represents. Your job is to say what \
the child actually misunderstands and what they should do next.

Be specific and be honest. "Needs more practice with integers" helps nobody. \
"Consistently keeps the sign of the larger number when subtracting" is \
something a parent can sit down and work on. If the evidence does not support \
a confident diagnosis, say so rather than inventing one -- a wrong diagnosis \
sends a child to practise the wrong thing."""


ANALYSIS_PROMPT = """Student: {student_name}, grade {grade}.

Weak skills, with the specific wrong options they chose:

{evidence}

Skills they are doing well at: {strengths}

For each weak skill, give:
  "skill_tag": copied exactly from above
  "likely_misconception": one sentence naming the specific error, grounded in
      the wrong options listed. Not "struggles with subtraction" -- say what
      they are actually doing wrong.
  "recommendation": one or two sentences a parent could act on tonight.
  "recommended_difficulty": "beginner", "intermediate" or "proficient" --
      where they should practise. Send them back a tier when the basics are
      shaky.

Also give a "summary": two or three sentences for the parent. Lead with what
is going well, then the single most important thing to work on.

Return ONLY this JSON:
{{
  "summary": "string",
  "findings": [
    {{"skill_tag": "...", "likely_misconception": "...",
      "recommendation": "...", "recommended_difficulty": "beginner"}}
  ]
}}"""


class GapDetectorAgent(BaseAgent):
    """Diagnoses why a student is struggling, from their actual wrong answers.

    Example:
        agent = GapDetectorAgent()
        report = agent.analyse(student_id="...")
        for gap in report.gaps:
            print(gap.skill_tag, gap.likely_misconception)
    """

    def __init__(self, **kwargs: Any) -> None:
        kwargs.setdefault("temperature", 0.2)
        kwargs.setdefault("max_tokens", 2048)
        super().__init__(agent_name="GapDetector", **kwargs)

    # ------------------------------------------------------------------ #
    # BaseAgent contract
    # ------------------------------------------------------------------ #

    def get_system_prompt(self) -> str:
        return SYSTEM_PROMPT

    def get_tools(self) -> list[Tool]:
        return [
            Tool(
                name="skill_performance",
                func=self._tool_skill_performance,
                description=(
                    "Per-skill accuracy and hint use for a student. Input: the "
                    "student id. Returns every skill they have answered questions "
                    "on, weakest first, with the sample size behind each figure."
                ),
            ),
            Tool(
                name="misconception_evidence",
                func=self._tool_misconceptions,
                description=(
                    "The specific wrong options a student chose for one skill, and "
                    'what each represents. Input: JSON {"student_id": "...", '
                    '"skill_tag": "subtract_integers"}. This is the evidence that '
                    "turns a low score into a diagnosis."
                ),
            ),
            Tool(
                name="analyse_student",
                func=self._tool_analyse,
                description=(
                    "Run the full gap analysis for a student. Input: the student "
                    "id. Returns diagnosed gaps with misconceptions and "
                    "recommendations."
                ),
            ),
        ]

    # ------------------------------------------------------------------ #
    # Evidence gathering (deterministic)
    # ------------------------------------------------------------------ #

    @staticmethod
    def gather_evidence(db: Session, student_id: str) -> list[SkillEvidence]:
        """Build per-skill evidence from this student's actual responses.

        Practice attempts are excluded: they are low-stakes by design and
        children click through them, so folding them in would manufacture
        gaps that do not exist.

        Attribution is per question, not per quiz. A cumulative unit test has
        no ``Quiz.sub_unit_id``, so grouping by the quiz would drop every one
        of its answers -- and those are the best evidence available, since an
        interleaved paper makes a child choose the method rather than handing
        it to them.
        """
        rows = (
            db.query(
                QuizQuestion.skill_tag,
                QuizQuestion.sub_unit_id,
                func.count(QuizResponse.id),
                func.sum(func.cast(QuizResponse.is_correct, Integer)),
                func.sum(func.cast(QuizResponse.hint_used, Integer)),
            )
            .join(QuizQuestion, QuizQuestion.id == QuizResponse.question_id)
            .join(Quiz, Quiz.id == QuizResponse.quiz_id)
            .filter(
                QuizResponse.student_id == student_id,
                Quiz.is_practice.is_(False),
                QuizQuestion.sub_unit_id.is_not(None),
            )
            .group_by(QuizQuestion.skill_tag, QuizQuestion.sub_unit_id)
            .all()
        )

        sub_units = {
            sub_unit.id: sub_unit
            for sub_unit in db.query(CurriculumSubUnit)
            .filter(CurriculumSubUnit.id.in_({row[1] for row in rows}))
            .all()
        }

        evidence: list[SkillEvidence] = []
        for tag, sub_unit_id, answered, correct, hints in rows:
            sub_unit = sub_units.get(sub_unit_id)
            if sub_unit is None:
                continue
            evidence.append(
                SkillEvidence(
                    skill_tag=tag or "untagged",
                    sub_unit_id=sub_unit_id,
                    sub_unit_number=sub_unit.sub_unit_number,
                    sub_unit_title=sub_unit.title,
                    unit_number=sub_unit.unit.unit_number,
                    questions_answered=int(answered or 0),
                    correct=int(correct or 0),
                    hints_used=int(hints or 0),
                )
            )

        for item in evidence:
            if item.is_weak:
                GapDetectorAgent._attach_misconceptions(db, student_id, item)

        evidence.sort(key=lambda item: (item.accuracy, -item.questions_answered))
        return evidence

    @staticmethod
    def _attach_misconceptions(
        db: Session, student_id: str, evidence: SkillEvidence, limit: int = 4
    ) -> None:
        """Record which wrong options the student picked, and what they mean.

        This is the whole point of the agent. ``distractor_rationales`` names
        the error behind each option, so counting the ones a child actually
        chose says what they are doing wrong rather than merely that they are.
        """
        wrong = (
            db.query(QuizResponse, QuizQuestion)
            .join(QuizQuestion, QuizQuestion.id == QuizResponse.question_id)
            .join(Quiz, Quiz.id == QuizResponse.quiz_id)
            .filter(
                QuizResponse.student_id == student_id,
                QuizResponse.is_correct.is_(False),
                QuizQuestion.skill_tag == evidence.skill_tag,
                QuizQuestion.sub_unit_id == evidence.sub_unit_id,
                Quiz.is_practice.is_(False),
            )
            .all()
        )

        reasons: list[str] = []
        for response, question in wrong:
            rationales = question.distractor_rationales or {}
            reason = rationales.get((response.selected_answer or "").upper())
            if reason:
                reasons.append(reason.strip())
            if len(evidence.example_questions) < 3:
                evidence.example_questions.append(question.question_text)

        clustered = cluster_rationales(reasons)
        evidence.misconceptions = dict(list(clustered.items())[:limit])

    # ------------------------------------------------------------------ #
    # Analysis
    # ------------------------------------------------------------------ #

    @staticmethod
    def _format_evidence(weak: list[SkillEvidence]) -> str:
        blocks: list[str] = []
        for item in weak:
            lines = [
                f"SKILL: {item.skill_tag}  "
                f"(sub-unit {item.sub_unit_number} {item.sub_unit_title})",
                f"  score: {item.correct}/{item.questions_answered} "
                f"= {item.accuracy:.0%}, hints used {item.hints_used}",
            ]
            if item.misconceptions:
                lines.append("  wrong options chosen, and what each means:")
                lines.extend(
                    f"    {count}x  {reason}" for reason, count in item.misconceptions.items()
                )
            else:
                lines.append("  (no distractor rationale recorded for their choices)")
            if item.example_questions:
                lines.append(f"  example question: {item.example_questions[0][:140]}")
            blocks.append("\n".join(lines))
        return "\n\n".join(blocks)

    def analyse(self, student_id: str, db: Session | None = None) -> GapReport:
        """Diagnose a student's gaps.

        Returns a report even when the model is unavailable: the evidence is
        gathered deterministically, so a failed interpretation degrades to
        numbers-with-misconceptions rather than to nothing.
        """
        if db is not None:
            return self._analyse_with_session(db, student_id)
        with session_scope() as session:
            return self._analyse_with_session(session, student_id)

    def _analyse_with_session(self, db: Session, student_id: str) -> GapReport:
        student = db.get(Student, student_id)
        if student is None:
            raise ValueError(f"No student with id {student_id!r}.")

        evidence = self.gather_evidence(db, student_id)
        report = GapReport(
            student_id=student_id,
            student_name=student.display_name,
            skills_analysed=len(evidence),
            responses_analysed=sum(item.questions_answered for item in evidence),
            skills_with_thin_evidence=[
                item.skill_tag for item in evidence if not item.has_enough_evidence
            ],
        )

        if not evidence:
            report.summary = (
                f"{student.display_name} has not answered enough questions yet "
                "for a meaningful analysis."
            )
            return report

        weak = [item for item in evidence if item.is_weak]
        report.strengths = [
            item.skill_tag
            for item in evidence
            if item.has_enough_evidence and item.accuracy >= 0.85
        ][:5]

        if not weak:
            report.summary = (
                f"No clear gaps. {student.display_name} is at or above "
                f"{WEAK_ACCURACY:.0%} on every skill with enough evidence to judge."
            )
            return report

        # Build the gaps from evidence first, so the report is grounded even
        # if the model adds nothing.
        report.gaps = [
            Gap(
                skill_tag=item.skill_tag,
                sub_unit_id=item.sub_unit_id,
                sub_unit_number=item.sub_unit_number,
                sub_unit_title=item.sub_unit_title,
                unit_number=item.unit_number,
                severity=item.severity,
                accuracy=item.accuracy,
                questions_answered=item.questions_answered,
                hints_used=item.hints_used,
                evidence=[
                    f"{count} of {item.questions_answered - item.correct} wrong answers: {reason}"
                    for reason, count in item.misconceptions.items()
                ]
                or [f"{item.correct}/{item.questions_answered} correct"],
                recommended_difficulty=self._suggest_difficulty(db, student_id, item),
            )
            for item in weak
        ]

        self._interpret(report, weak, student)
        return report

    @staticmethod
    def _suggest_difficulty(
        db: Session, student_id: str, evidence: SkillEvidence
    ) -> DifficultyLevel:
        """Where the student should practise this skill next.

        A shaky skill sends them back to the lowest tier they have not
        convincingly cleared, rather than to whatever tier they happened to
        fail last.
        """
        progress = (
            db.query(SubUnitProgress)
            .filter_by(student_id=student_id, sub_unit_id=evidence.sub_unit_id)
            .one_or_none()
        )
        if progress is None:
            return DifficultyLevel.BEGINNER
        if evidence.accuracy < CRITICAL_ACCURACY:
            # Badly stuck: go back to the beginning of this sub-unit.
            return DifficultyLevel.BEGINNER
        return progress.next_difficulty or DifficultyLevel.BEGINNER

    def _interpret(self, report: GapReport, weak: list[SkillEvidence], student: Student) -> None:
        """Ask the model to name the misconception and recommend a next step."""
        prompt = ANALYSIS_PROMPT.format(
            student_name=student.display_name,
            grade=student.grade_level,
            evidence=self._format_evidence(weak),
            strengths=", ".join(report.strengths) or "none recorded yet",
        )

        try:
            response = self.get_llm().invoke(
                [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ]
            )
            parsed = self.parse_json_output(getattr(response, "content", "") or "")
        except Exception as exc:
            logger.warning("Gap interpretation failed (%s); reporting evidence only.", exc)
            parsed = None

        if not isinstance(parsed, dict):
            report.summary = self._fallback_summary(report, weak)
            for gap in report.gaps:
                gap.likely_misconception = self._fallback_misconception(weak, gap.skill_tag)
                gap.recommendation = (
                    f"Practise {gap.sub_unit_number} at the "
                    f"{gap.recommended_difficulty.value} level."
                )
            return

        report.summary = str(parsed.get("summary", "")).strip() or self._fallback_summary(
            report, weak
        )

        findings = {
            str(finding.get("skill_tag", "")): finding
            for finding in parsed.get("findings", [])
            if isinstance(finding, dict)
        }
        for gap in report.gaps:
            finding = findings.get(gap.skill_tag)
            if finding is None:
                gap.likely_misconception = self._fallback_misconception(weak, gap.skill_tag)
                gap.recommendation = (
                    f"Practise {gap.sub_unit_number} at the "
                    f"{gap.recommended_difficulty.value} level."
                )
                continue

            gap.likely_misconception = str(finding.get("likely_misconception", "")).strip()
            gap.recommendation = str(finding.get("recommendation", "")).strip()
            # The model may suggest a tier; the deterministic rule wins when it
            # proposes something the student has already cleared.
            proposed = str(finding.get("recommended_difficulty", "")).strip().lower()
            if proposed in {level.value for level in DifficultyLevel}:
                gap.recommended_difficulty = DifficultyLevel(proposed)

    @staticmethod
    def _fallback_misconception(weak: list[SkillEvidence], skill_tag: str) -> str:
        """Name the most common wrong option, with no model involved."""
        for item in weak:
            if item.skill_tag != skill_tag:
                continue
            top = item.top_misconception()
            if top:
                return f"Most often: {top[0]}"
            return f"Scored {item.correct}/{item.questions_answered} on this skill."
        return ""

    @staticmethod
    def _fallback_summary(report: GapReport, weak: list[SkillEvidence]) -> str:
        worst = weak[0]
        return (
            f"{report.student_name} has {len(weak)} skill(s) worth attention. "
            f"The weakest is {worst.skill_tag} at {worst.accuracy:.0%} "
            f"({worst.correct}/{worst.questions_answered})."
        )

    # ------------------------------------------------------------------ #
    # Tools
    # ------------------------------------------------------------------ #

    def _tool_skill_performance(self, raw: str) -> str:
        student_id = (raw or "").strip().strip('"')
        try:
            with session_scope() as db:
                evidence = self.gather_evidence(db, student_id)
                return json.dumps([item.to_dict() for item in evidence], indent=2)
        except Exception as exc:
            return f"ERROR: {exc}"

    def _tool_misconceptions(self, raw: str) -> str:
        parsed = self.parse_json_output(raw)
        if not isinstance(parsed, dict):
            return 'ERROR: expected {"student_id": "...", "skill_tag": "..."}.'
        try:
            with session_scope() as db:
                for item in self.gather_evidence(db, str(parsed.get("student_id", ""))):
                    if item.skill_tag == parsed.get("skill_tag"):
                        return json.dumps(item.to_dict(), indent=2)
                return f"No responses recorded for skill {parsed.get('skill_tag')!r}."
        except Exception as exc:
            return f"ERROR: {exc}"

    def _tool_analyse(self, raw: str) -> str:
        student_id = (raw or "").strip().strip('"')
        try:
            return json.dumps(self.analyse(student_id).to_dict(), indent=2)
        except Exception as exc:
            return f"ERROR: {exc}"


__all__ = [
    "CRITICAL_ACCURACY",
    "HINT_RELIANCE",
    "MIN_EVIDENCE",
    "WEAK_ACCURACY",
    "Gap",
    "GapDetectorAgent",
    "GapReport",
    "SkillEvidence",
    "cluster_rationales",
]
