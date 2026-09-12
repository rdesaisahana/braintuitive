"""Test Prep Agent.

A child has a test on Friday covering Units 1 and 2, and four evenings to
revise. Everything else in this system answers "how are you doing on the thing
you are learning right now". This answers a different question: **given limited
time, what should you revise, in what order, and how much of it?**

The ranking is the whole product, so it is computed in Python rather than
asked of a model. Four signals, none of which any single existing endpoint
carries:

* **Never attempted.** A sub-unit in scope with no attempts is the largest
  risk there is -- you cannot have retained what you never learned.
* **Mastery deficit.** A sub-unit sitting at 33% is shakier than one at 100%.
* **Decay.** A tier passed five weeks ago is not a tier you still have. This
  is the signal the progress dashboard structurally cannot show: it reports
  ``beginner_completed = True`` identically whether that happened yesterday or
  in March.
* **Weakness.** Per-skill accuracy and hint reliance, taken from the same
  evidence the Gap Detector uses, so "weak" means one thing across the system.

The model's job is the part that genuinely needs judgement: turning the
resulting blueprint into a study plan a tired parent can follow on a Tuesday
evening. If it is unavailable the plan still comes back with its schedule and
question counts intact -- only the prose is lost.

What this agent deliberately does not do is award anything or move progress.
Revision drills run as practice, and practice cannot cost a child a score.
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from langchain_core.tools import Tool
from sqlalchemy.orm import Session

from agents.base_agent import BaseAgent
from agents.gap_detector import GapDetectorAgent, SkillEvidence
from db.database import session_scope
from db.models import (
    CurriculumSubUnit,
    CurriculumUnit,
    DifficultyLevel,
    QuizAttempt,
    QuizQuestion,
    Student,
    SubUnitProgress,
)
from services.curriculum_scope import curriculum_owner
from services.question_bank import slot_status

logger = logging.getLogger(__name__)

# How many questions a revision plan asks for when the caller does not say.
DEFAULT_QUESTION_BUDGET = 30
# Coverage floor: a test can ask about anything in scope, so nothing in scope
# gets zero attention however safe it looks.
MIN_QUESTIONS_PER_TOPIC = 2
# No single topic may eat the plan, however alarming its risk score.
MAX_TOPIC_SHARE = 0.35
# Realistic for one sitting; longer sessions are split across days instead.
MAX_QUESTIONS_PER_SESSION = 12

# Weeks, not days, is the wrong unit here and days is the right one: retention
# falls fastest immediately after learning. tau is a plain heuristic -- it puts
# a week-old topic at ~0.4 risk and a month-old one at ~0.9 -- not a fitted
# model of this child's memory, and nothing downstream treats it as one.
DECAY_TAU_DAYS = 14.0
# Past this, a topic is treated as fully decayed rather than ever-worsening.
DECAY_CEILING_DAYS = 60.0

RISK_WEIGHTS = {
    "mastery_deficit": 0.40,
    "weakness": 0.30,
    "decay": 0.20,
    "hint_reliance": 0.10,
}
# A topic never attempted skips the weighting entirely: there is no evidence to
# weigh, and "no evidence" is the risk.
UNATTEMPTED_RISK = 1.0


@dataclass
class TopicRisk:
    """How likely a child is to lose marks on one sub-unit, and why."""

    sub_unit_id: str
    sub_unit_number: str
    sub_unit_title: str
    unit_number: int
    never_attempted: bool
    completion_percentage: int
    days_since_practice: int | None
    best_score: float
    weakest_skill: str | None
    weakest_accuracy: float | None
    hint_rate: float
    risk: float
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "sub_unit_number": self.sub_unit_number,
            "sub_unit_title": self.sub_unit_title,
            "unit_number": self.unit_number,
            "risk": round(self.risk, 3),
            "never_attempted": self.never_attempted,
            "completion_percentage": self.completion_percentage,
            "days_since_practice": self.days_since_practice,
            "weakest_skill": self.weakest_skill,
            "reasons": self.reasons,
        }


@dataclass
class DrillBlock:
    """A slice of revision: this many questions, on this topic, at this tier."""

    sub_unit_id: str
    sub_unit_number: str
    sub_unit_title: str
    unit_number: int
    difficulty: DifficultyLevel
    question_count: int
    risk: float
    reasons: list[str] = field(default_factory=list)
    # Whether the pre-generated bank can actually supply this block. False
    # means the questions must be generated live, which takes minutes -- a plan
    # that quietly promises undeliverable drills is worse than one that says so.
    bank_ready: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "sub_unit_id": self.sub_unit_id,
            "sub_unit_number": self.sub_unit_number,
            "sub_unit_title": self.sub_unit_title,
            "unit_number": self.unit_number,
            "difficulty": self.difficulty.value,
            "question_count": self.question_count,
            "risk": round(self.risk, 3),
            "reasons": self.reasons,
            "bank_ready": self.bank_ready,
        }


@dataclass
class StudySession:
    """One evening's work."""

    day: int
    blocks: list[DrillBlock] = field(default_factory=list)
    focus: str = ""

    @property
    def question_count(self) -> int:
        return sum(block.question_count for block in self.blocks)

    def to_dict(self) -> dict[str, Any]:
        return {
            "day": self.day,
            "focus": self.focus,
            "question_count": self.question_count,
            "blocks": [block.to_dict() for block in self.blocks],
        }


@dataclass
class StudyPlan:
    """A revision plan for a specific test on a specific date."""

    student_id: str
    student_name: str
    unit_numbers: list[int] = field(default_factory=list)
    days_until_test: int = 0
    total_questions: int = 0
    sessions: list[StudySession] = field(default_factory=list)
    risks: list[TopicRisk] = field(default_factory=list)
    summary: str = ""
    advice: list[str] = field(default_factory=list)
    topics_not_yet_started: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    # Units whose cumulative test is worth sitting as a dress rehearsal, once
    # the drills are done. Empty when the scope spans no complete unit.
    rehearsal_unit_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "student_id": self.student_id,
            "student_name": self.student_name,
            "unit_numbers": self.unit_numbers,
            "days_until_test": self.days_until_test,
            "total_questions": self.total_questions,
            "summary": self.summary,
            "advice": self.advice,
            "sessions": [session.to_dict() for session in self.sessions],
            "risks": [risk.to_dict() for risk in self.risks],
            "topics_not_yet_started": self.topics_not_yet_started,
            "warnings": self.warnings,
            "rehearsal_unit_ids": self.rehearsal_unit_ids,
        }


# --------------------------------------------------------------------------- #
# Risk scoring
# --------------------------------------------------------------------------- #


def decay_factor(days: int | None) -> float:
    """Risk contributed by time since a topic was last practised, 0..1.

    ``None`` means never practised, which is handled as its own case rather
    than as infinite decay -- they are different problems with different fixes.
    """
    if days is None:
        return 1.0
    if days <= 0:
        return 0.0
    capped = min(float(days), DECAY_CEILING_DAYS)
    return round(1.0 - math.exp(-capped / DECAY_TAU_DAYS), 4)


def _weakest_skill(
    evidence: list[SkillEvidence], sub_unit_id: str
) -> tuple[str | None, float | None, float]:
    """The shakiest skill in one sub-unit, from the Gap Detector's evidence.

    Reusing that evidence rather than recomputing accuracy is deliberate: two
    definitions of "weak" would drift, and a child would then be told to revise
    a skill the gap report calls healthy.
    """
    relevant = [
        item for item in evidence if item.sub_unit_id == sub_unit_id and item.has_enough_evidence
    ]
    if not relevant:
        return None, None, 0.0

    weakest = min(relevant, key=lambda item: item.accuracy)
    worst_hints = max(item.hint_rate for item in relevant)
    return weakest.skill_tag, weakest.accuracy, worst_hints


def score_topic(
    sub_unit: CurriculumSubUnit,
    progress: SubUnitProgress | None,
    last_practised: datetime | None,
    evidence: list[SkillEvidence],
    now: datetime | None = None,
) -> TopicRisk:
    """Score one sub-unit's revision risk, recording why.

    Every finding carries its reasons because a plan a parent cannot argue
    with is a plan they cannot trust.
    """
    now = now or datetime.now(UTC)
    days = None
    if last_practised is not None:
        reference = last_practised
        if reference.tzinfo is None:
            reference = reference.replace(tzinfo=UTC)
        days = max(0, (now - reference).days)

    completion = progress.completion_percentage if progress else 0
    best = (
        max(
            progress.beginner_best_score,
            progress.intermediate_best_score,
            progress.proficient_best_score,
        )
        if progress
        else 0.0
    )
    skill, accuracy, hint_rate = _weakest_skill(evidence, sub_unit.id)
    never_attempted = progress is None or (progress.total_attempts or 0) == 0

    reasons: list[str] = []
    if never_attempted:
        risk = UNATTEMPTED_RISK
        reasons.append("Not started yet -- this is the biggest risk in the plan.")
    else:
        mastery_deficit = 1.0 - (completion / 100.0)
        weakness = 1.0 - accuracy if accuracy is not None else 0.0
        decay = decay_factor(days)

        risk = (
            RISK_WEIGHTS["mastery_deficit"] * mastery_deficit
            + RISK_WEIGHTS["weakness"] * weakness
            + RISK_WEIGHTS["decay"] * decay
            + RISK_WEIGHTS["hint_reliance"] * hint_rate
        )

        if completion < 100:
            reasons.append(f"Only {completion}% complete.")
        if accuracy is not None and accuracy < 0.7:
            reasons.append(f"Weakest skill {skill} at {accuracy:.0%}.")
        if days is not None and days >= 14:
            reasons.append(f"Not practised for {days} days.")
        if hint_rate >= 0.4:
            reasons.append(f"Needed a hint on {hint_rate:.0%} of questions.")
        if not reasons:
            reasons.append("Solid -- included for coverage.")

    return TopicRisk(
        sub_unit_id=sub_unit.id,
        sub_unit_number=sub_unit.sub_unit_number,
        sub_unit_title=sub_unit.title,
        unit_number=sub_unit.unit.unit_number,
        never_attempted=never_attempted,
        completion_percentage=completion,
        days_since_practice=days,
        best_score=best,
        weakest_skill=skill,
        weakest_accuracy=accuracy,
        hint_rate=hint_rate,
        risk=round(min(risk, 1.0), 4),
        reasons=reasons,
    )


def target_difficulty(progress: SubUnitProgress | None) -> DifficultyLevel:
    """Which tier to revise at.

    The lowest tier not yet cleared, because that is where the gap is. A child
    who has cleared everything revises at proficient: a school test will not
    ask beginner questions, and re-drilling a tier they aced is busywork
    dressed up as revision.
    """
    if progress is None:
        return DifficultyLevel.BEGINNER
    for level in (
        DifficultyLevel.BEGINNER,
        DifficultyLevel.INTERMEDIATE,
        DifficultyLevel.PROFICIENT,
    ):
        if not getattr(progress, f"{level.value}_completed", False):
            return level
    return DifficultyLevel.PROFICIENT


def allocate(risks: list[TopicRisk], budget: int) -> dict[str, int]:
    """Split a question budget across topics, in proportion to risk.

    Two guards shape the result. A floor, because a test can ask about anything
    in scope and a topic with zero questions is a topic the child walks in
    having not looked at. A cap, because one alarming topic must not consume
    the evening -- the other four still appear on the paper.
    """
    if not risks or budget <= 0:
        return {}

    # The floor may exceed the budget when scope is wide and time is short.
    # Fewer topics done properly beats every topic done uselessly, so the
    # riskiest are kept and the rest are dropped -- and the caller is told.
    affordable = max(1, budget // MIN_QUESTIONS_PER_TOPIC)
    ranked = sorted(risks, key=lambda item: -item.risk)[:affordable]

    cap = max(MIN_QUESTIONS_PER_TOPIC, int(budget * MAX_TOPIC_SHARE))
    allocation = {item.sub_unit_id: MIN_QUESTIONS_PER_TOPIC for item in ranked}
    remaining = budget - MIN_QUESTIONS_PER_TOPIC * len(ranked)

    total_risk = sum(item.risk for item in ranked)
    if remaining > 0 and total_risk > 0:
        for item in ranked:
            share = int(remaining * (item.risk / total_risk))
            allocation[item.sub_unit_id] = min(cap, allocation[item.sub_unit_id] + share)

    # Recover what integer truncation lost, riskiest first. Bounded by the
    # number of topics on purpose: the budget is a ceiling, not a quota. A
    # child who is solid everywhere should be handed a short plan, not have
    # every topic inflated to the cap to spend the budget -- that is the same
    # busywork that sending a mastered topic back to beginner would be.
    # Only when a proportional split actually happened -- with no risk to
    # distribute there is no truncation to recover, and adding anyway would
    # quietly reintroduce the quota behaviour.
    leftover = min(budget - sum(allocation.values()), len(ranked)) if total_risk > 0 else 0
    for item in ranked:
        if leftover <= 0:
            break
        if allocation[item.sub_unit_id] < cap:
            allocation[item.sub_unit_id] += 1
            leftover -= 1

    return allocation


def schedule(blocks: list[DrillBlock], days: int) -> list[StudySession]:
    """Spread drills across the evenings available, riskiest first.

    Front-loading is the point: a topic the child collapses on should surface
    on day one, while there is still time to do something about it. Leaving it
    to the night before the test converts a fixable gap into a bad morning.
    """
    if not blocks:
        return []

    days = max(1, days)
    ordered = sorted(blocks, key=lambda block: -block.risk)
    sessions = [StudySession(day=index + 1) for index in range(days)]

    for index, block in enumerate(ordered):
        # Round-robin rather than filling day one to the brim: every evening
        # gets work, and the highest-risk topics land earliest.
        session = sessions[index % days]
        if session.question_count + block.question_count > MAX_QUESTIONS_PER_SESSION:
            emptiest = min(sessions, key=lambda item: item.question_count)
            session = emptiest
        session.blocks.append(block)

    return [session for session in sessions if session.blocks]


# --------------------------------------------------------------------------- #
# Prompts
# --------------------------------------------------------------------------- #

SYSTEM_PROMPT = """You are a tutor writing a revision plan for a child's \
parent, a few days before a school test.

The plan -- which topics, how many questions, on which evening -- has already \
been decided from the child's actual results and is not yours to change. Your \
job is to explain it in a way a tired parent can follow on a weeknight.

Be concrete and be calm. Name the topic and what to watch for. Do not inflate \
a solid result into a worry, and do not soften a real gap into "keep \
practising" -- a parent who is told everything is fine and then sees a poor \
test result stops trusting the plan."""


PLAN_PROMPT = """Student: {student_name}, grade {grade}.
Test in {days} day(s), covering {scope}.

The schedule, already decided:

{schedule}

Why these topics were chosen:

{reasons}

Write:
  "summary": two or three sentences for the parent. What state the child is
      in going into this test, and the one thing that matters most.
  "sessions": one entry per day above, each
      {{"day": <number>, "focus": "one short line naming what that evening is
        for -- what to watch for, not a restatement of the topic list"}}
  "advice": two or three short, practical lines. Things to do at the kitchen
      table, not study-skills platitudes.

Return ONLY this JSON:
{{
  "summary": "string",
  "sessions": [{{"day": 1, "focus": "string"}}],
  "advice": ["string"]
}}"""


class TestPrepAgent(BaseAgent):
    """Builds a prioritised revision plan for an upcoming test.

    Example:
        agent = TestPrepAgent()
        plan = agent.plan(student_id="...", unit_numbers=[1, 2], days_until_test=4)
        for session in plan.sessions:
            print(session.day, session.focus, session.question_count)
    """

    # pytest collects any class named Test*; this is an agent, not a suite.
    __test__ = False

    def __init__(self, **kwargs: Any) -> None:
        kwargs.setdefault("temperature", 0.3)
        kwargs.setdefault("max_tokens", 2048)
        super().__init__(agent_name="TestPrep", **kwargs)

    # ------------------------------------------------------------------ #
    # BaseAgent contract
    # ------------------------------------------------------------------ #

    def get_system_prompt(self) -> str:
        return SYSTEM_PROMPT

    def get_tools(self) -> list[Tool]:
        return [
            Tool(
                name="revision_risk",
                func=self._tool_risk,
                description=(
                    "Which topics a student is most likely to lose marks on, "
                    'riskiest first. Input: JSON {"student_id": "...", '
                    '"unit_numbers": [1, 2]}. Combines mastery, decay since last '
                    "practice, and per-skill weakness."
                ),
            ),
            Tool(
                name="build_study_plan",
                func=self._tool_plan,
                description=(
                    "Build a full revision plan. Input: JSON "
                    '{"student_id": "...", "unit_numbers": [1], '
                    '"days_until_test": 4, "question_budget": 30}. Returns a '
                    "day-by-day schedule with question counts per topic."
                ),
            ),
        ]

    # ------------------------------------------------------------------ #
    # Data gathering (deterministic)
    # ------------------------------------------------------------------ #

    @staticmethod
    def _last_practised(db: Session, student_id: str) -> dict[str, datetime]:
        """Most recent time each sub-unit was touched, practice included.

        Practice counts here even though it counts nowhere else: the question
        is when the child last *touched* this material, and a practice round
        refreshes memory exactly as well as a scored one.

        Measured per question rather than per attempt, so a cumulative unit
        test refreshes every sub-unit it sampled. Keyed off the attempt means
        it would refresh none of them -- the attempt names a unit, not a
        sub-unit -- and a unit the child sat a test on last week would be
        reported as untouched for months.
        """
        rows = (
            db.query(QuizQuestion.sub_unit_id, QuizAttempt.completed_at)
            .join(QuizAttempt, QuizAttempt.quiz_id == QuizQuestion.quiz_id)
            .filter(
                QuizAttempt.student_id == student_id,
                QuizQuestion.sub_unit_id.is_not(None),
            )
            .distinct()
            .all()
        )
        latest: dict[str, datetime] = {}
        for sub_unit_id, completed_at in rows:
            if completed_at is None:
                continue
            current = latest.get(sub_unit_id)
            if current is None or completed_at > current:
                latest[sub_unit_id] = completed_at
        return latest

    @staticmethod
    def assess(
        db: Session,
        student_id: str,
        unit_numbers: list[int] | None = None,
        now: datetime | None = None,
    ) -> list[TopicRisk]:
        """Score every sub-unit in scope, riskiest first.

        Scope defaults to every unit the student has actually worked in --
        revising a unit they have never opened is not revision.
        """
        evidence = GapDetectorAgent.gather_evidence(db, student_id)
        progress_rows = {
            row.sub_unit_id: row
            for row in db.query(SubUnitProgress).filter_by(student_id=student_id).all()
        }
        last_practised = TestPrepAgent._last_practised(db, student_id)

        student = db.get(Student, student_id)
        owner = curriculum_owner(db, student) if student is not None else None
        query = (
            db.query(CurriculumSubUnit)
            .join(CurriculumUnit)
            .filter(
                CurriculumUnit.user_id.is_(None)
                if owner is None
                else CurriculumUnit.user_id == owner
            )
        )
        if student is not None:
            query = query.filter(CurriculumUnit.grade_level == student.grade_level)
        if unit_numbers:
            query = query.filter(CurriculumUnit.unit_number.in_(unit_numbers))
        sub_units = query.order_by(CurriculumUnit.unit_number, CurriculumSubUnit.sequence).all()

        if not unit_numbers:
            touched = {
                sub_unit.unit.unit_number
                for sub_unit in sub_units
                if sub_unit.id in progress_rows or sub_unit.id in last_practised
            }
            sub_units = [sub for sub in sub_units if sub.unit.unit_number in touched]

        risks = [
            score_topic(
                sub_unit,
                progress_rows.get(sub_unit.id),
                last_practised.get(sub_unit.id),
                evidence,
                now=now,
            )
            for sub_unit in sub_units
        ]
        risks.sort(key=lambda item: (-item.risk, item.unit_number, item.sub_unit_number))
        return risks

    # ------------------------------------------------------------------ #
    # Planning
    # ------------------------------------------------------------------ #

    def plan(
        self,
        student_id: str,
        unit_numbers: list[int] | None = None,
        days_until_test: int = 7,
        question_budget: int = DEFAULT_QUESTION_BUDGET,
        db: Session | None = None,
        now: datetime | None = None,
    ) -> StudyPlan:
        """Build a revision plan.

        Returns a usable plan even when the model is unavailable: the schedule
        and question counts are computed here, and only the prose is lost.
        """
        if db is not None:
            return self._plan_with_session(
                db, student_id, unit_numbers, days_until_test, question_budget, now
            )
        with session_scope() as session:
            return self._plan_with_session(
                session, student_id, unit_numbers, days_until_test, question_budget, now
            )

    def _plan_with_session(
        self,
        db: Session,
        student_id: str,
        unit_numbers: list[int] | None,
        days_until_test: int,
        question_budget: int,
        now: datetime | None,
    ) -> StudyPlan:
        student = db.get(Student, student_id)
        if student is None:
            raise ValueError(f"No student with id {student_id!r}.")

        days = max(1, min(days_until_test, 30))
        budget = max(MIN_QUESTIONS_PER_TOPIC, min(question_budget, 120))
        risks = self.assess(db, student_id, unit_numbers, now=now)

        plan = StudyPlan(
            student_id=student_id,
            student_name=student.display_name,
            unit_numbers=unit_numbers or sorted({risk.unit_number for risk in risks}),
            days_until_test=days,
            topics_not_yet_started=[risk.sub_unit_number for risk in risks if risk.never_attempted],
        )

        if not risks:
            plan.summary = (
                f"{student.display_name} has not worked in these units yet, so there "
                "is nothing to revise -- start with the first sub-unit instead."
            )
            return plan

        allocation = allocate(risks, budget)
        if len(allocation) < len(risks):
            dropped = len(risks) - len(allocation)
            plan.warnings.append(
                f"{dropped} lower-risk topic(s) left out: {budget} questions across "
                f"{len(risks)} topics would be too thin to be worth doing."
            )

        progress_rows = {
            row.sub_unit_id: row
            for row in db.query(SubUnitProgress).filter_by(student_id=student_id).all()
        }
        blocks = [
            self._build_block(
                db, risk, allocation[risk.sub_unit_id], progress_rows.get(risk.sub_unit_id)
            )
            for risk in risks
            if allocation.get(risk.sub_unit_id)
        ]
        plan.sessions = schedule(blocks, days)
        plan.total_questions = sum(block.question_count for block in blocks)
        plan.risks = risks

        # A rehearsal is only worth offering for a unit the child has actually
        # worked through. Sitting a cumulative paper on material they have not
        # met yet measures nothing and is a discouraging way to spend an
        # evening before a test.
        owner = curriculum_owner(db, student)
        plan.rehearsal_unit_ids = [
            unit.id
            for unit in db.query(CurriculumUnit)
            .filter(
                CurriculumUnit.unit_number.in_(plan.unit_numbers or [0]),
                CurriculumUnit.grade_level == student.grade_level,
                (
                    CurriculumUnit.user_id.is_(None)
                    if owner is None
                    else CurriculumUnit.user_id == owner
                ),
            )
            .all()
            if not any(
                risk.never_attempted for risk in risks if risk.unit_number == unit.unit_number
            )
        ]

        not_banked = [block.sub_unit_number for block in blocks if not block.bank_ready]
        if not_banked:
            plan.warnings.append(
                "Questions for "
                + ", ".join(sorted(set(not_banked)))
                + " are not pre-generated, so those drills will take a couple of "
                "minutes to prepare."
            )

        self._write_plan(plan, student)
        return plan

    @staticmethod
    def _build_block(
        db: Session,
        risk: TopicRisk,
        count: int,
        progress: SubUnitProgress | None,
    ) -> DrillBlock:
        """Turn a scored topic into a concrete drill.

        ``progress`` is passed in rather than looked up here: it is already
        loaded, and a lookup by sub-unit alone would silently read whichever
        student's row came first.
        """
        difficulty = target_difficulty(progress)
        sub_unit = db.get(CurriculumSubUnit, risk.sub_unit_id)
        bank_ready = (
            slot_status(db, sub_unit, difficulty).servable >= count
            if sub_unit is not None
            else False
        )

        return DrillBlock(
            sub_unit_id=risk.sub_unit_id,
            sub_unit_number=risk.sub_unit_number,
            sub_unit_title=risk.sub_unit_title,
            unit_number=risk.unit_number,
            difficulty=difficulty,
            question_count=count,
            risk=risk.risk,
            reasons=risk.reasons,
            bank_ready=bank_ready,
        )

    # ------------------------------------------------------------------ #
    # Prose
    # ------------------------------------------------------------------ #

    @staticmethod
    def _format_schedule(plan: StudyPlan) -> str:
        lines: list[str] = []
        for session in plan.sessions:
            lines.append(f"Day {session.day} ({session.question_count} questions):")
            lines.extend(
                f"  {block.sub_unit_number} {block.sub_unit_title} -- "
                f"{block.question_count} at {block.difficulty.value}"
                for block in session.blocks
            )
        return "\n".join(lines)

    @staticmethod
    def _format_reasons(plan: StudyPlan) -> str:
        scheduled = {block.sub_unit_id for session in plan.sessions for block in session.blocks}
        return "\n".join(
            f"  {risk.sub_unit_number} {risk.sub_unit_title}: " + " ".join(risk.reasons)
            for risk in plan.risks
            if risk.sub_unit_id in scheduled
        )

    def _write_plan(self, plan: StudyPlan, student: Student) -> None:
        prompt = PLAN_PROMPT.format(
            student_name=student.display_name,
            grade=student.grade_level,
            days=plan.days_until_test,
            scope=", ".join(f"Unit {number}" for number in plan.unit_numbers) or "recent work",
            schedule=self._format_schedule(plan),
            reasons=self._format_reasons(plan),
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
            logger.warning("Study-plan prose failed (%s); returning the schedule only.", exc)
            parsed = None

        if not isinstance(parsed, dict):
            plan.summary = self._fallback_summary(plan)
            for session in plan.sessions:
                session.focus = self._fallback_focus(session)
            return

        plan.summary = str(parsed.get("summary", "")).strip() or self._fallback_summary(plan)
        plan.advice = [
            str(line).strip()
            for line in parsed.get("advice", [])
            if isinstance(line, str) and line.strip()
        ][:4]

        focuses = {
            item.get("day"): str(item.get("focus", "")).strip()
            for item in parsed.get("sessions", [])
            if isinstance(item, dict)
        }
        for session in plan.sessions:
            session.focus = focuses.get(session.day) or self._fallback_focus(session)

    @staticmethod
    def _fallback_focus(session: StudySession) -> str:
        titles = ", ".join(block.sub_unit_title for block in session.blocks)
        return f"{session.question_count} questions: {titles}"

    @staticmethod
    def _fallback_summary(plan: StudyPlan) -> str:
        if not plan.sessions:
            return f"Nothing to revise for {plan.student_name} in this scope."
        riskiest = max(
            (block for session in plan.sessions for block in session.blocks),
            key=lambda block: block.risk,
        )
        return (
            f"{plan.total_questions} questions over {len(plan.sessions)} session(s) "
            f"before the test. Start with {riskiest.sub_unit_number} "
            f"{riskiest.sub_unit_title} -- {riskiest.reasons[0]}"
        )

    # ------------------------------------------------------------------ #
    # Tools
    # ------------------------------------------------------------------ #

    def _tool_risk(self, raw: str) -> str:
        parsed = self.parse_json_output(raw)
        if not isinstance(parsed, dict):
            return 'ERROR: expected {"student_id": "...", "unit_numbers": [1]}.'
        try:
            with session_scope() as db:
                risks = self.assess(
                    db, str(parsed.get("student_id", "")), parsed.get("unit_numbers")
                )
                return json.dumps([risk.to_dict() for risk in risks], indent=2)
        except Exception as exc:
            return f"ERROR: {exc}"

    def _tool_plan(self, raw: str) -> str:
        parsed = self.parse_json_output(raw)
        if not isinstance(parsed, dict):
            return 'ERROR: expected {"student_id": "...", "days_until_test": 4}.'
        try:
            plan = self.plan(
                str(parsed.get("student_id", "")),
                unit_numbers=parsed.get("unit_numbers"),
                days_until_test=int(parsed.get("days_until_test", 7)),
                question_budget=int(parsed.get("question_budget", DEFAULT_QUESTION_BUDGET)),
            )
            return json.dumps(plan.to_dict(), indent=2)
        except Exception as exc:
            return f"ERROR: {exc}"


__all__ = [
    "DECAY_TAU_DAYS",
    "DEFAULT_QUESTION_BUDGET",
    "MAX_QUESTIONS_PER_SESSION",
    "MAX_TOPIC_SHARE",
    "MIN_QUESTIONS_PER_TOPIC",
    "DrillBlock",
    "StudyPlan",
    "StudySession",
    "TestPrepAgent",
    "TopicRisk",
    "allocate",
    "decay_factor",
    "schedule",
    "score_topic",
    "target_difficulty",
]
