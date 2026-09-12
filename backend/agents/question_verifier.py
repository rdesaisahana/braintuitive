"""Question Verifier Agent.

Independently solves generated questions and flags any whose stated answer key
looks wrong. This closes the gap the Quiz Generator cannot close on its own:
structural validation checks that a question is *well-formed*, not that it is
*correct*. A question reading "What is -6 x 4?" with the key set to "24" passes
every structural check and teaches a child the wrong thing.

The single most important design decision here is that verification is
**blind**. The verifier never sees the claimed answer. Shown the key, a model
reliably rationalises its way to agreeing with it, which produces a
verification step that validates nothing while looking rigorous.

Escalation keeps the cost sane:

1. Solve once at temperature 0. Agreement is the common case and stops here.
2. On disagreement, solve again several times at a higher temperature and take
   a majority vote across every sample.

A single dissent is usually the verifier slipping; a consistent dissent across
independent samples is worth a human's attention.

The verifier also reports two things beyond the answer key:

``exactly_one_correct``
    Whether exactly one option is defensible. Two correct options make a
    question unscorable, however tidy it looks.

``well_posed``
    Whether the question is answerable as written.
"""

from __future__ import annotations

import enum
import json
import logging
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from langchain_core.tools import Tool

from agents.base_agent import BaseAgent
from config import settings

if TYPE_CHECKING:
    from agents.quiz_generator import GeneratedQuestion

logger = logging.getLogger(__name__)

OPTION_KEYS = ("A", "B", "C", "D")


class Verdict(str, enum.Enum):
    """Outcome of verifying one question."""

    AGREED = "agreed"
    """The verifier independently reached the claimed answer."""

    DISPUTED = "disputed"
    """The verifier consistently reached a different answer."""

    UNCERTAIN = "uncertain"
    """Samples split, or the verifier flagged the question as flawed."""

    ERROR = "error"
    """Verification could not run (API failure, unparseable response)."""


@dataclass
class QuestionVerdict:
    """What the verifier concluded about one question."""

    question_number: int
    question_text: str
    claimed_answer: str
    verifier_answer: str | None = None
    verdict: Verdict = Verdict.ERROR
    votes: dict[str, int] = field(default_factory=dict)
    confidence: float = 0.0
    reasoning: str = ""
    issues: list[str] = field(default_factory=list)
    samples: int = 0

    @property
    def needs_review(self) -> bool:
        """True when this question should not go to a student unchecked."""
        return self.verdict in (Verdict.DISPUTED, Verdict.UNCERTAIN)

    def summary(self) -> str:
        line = (
            f"Q{self.question_number}: {self.verdict.value.upper()} "
            f"(claimed {self.claimed_answer}, verifier {self.verifier_answer or '?'}"
        )
        if self.samples > 1:
            line += f", votes {self.votes}"
        line += ")"
        if self.issues:
            line += f" issues: {'; '.join(self.issues)}"
        return line

    def to_dict(self) -> dict[str, Any]:
        return {
            "question_number": self.question_number,
            "claimed_answer": self.claimed_answer,
            "verifier_answer": self.verifier_answer,
            "verdict": self.verdict.value,
            "votes": self.votes,
            "confidence": round(self.confidence, 3),
            "issues": self.issues,
            "samples": self.samples,
        }


@dataclass
class VerificationReport:
    """Aggregate result of verifying a question set."""

    verdicts: list[QuestionVerdict] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.verdicts)

    @property
    def agreed(self) -> list[QuestionVerdict]:
        return [v for v in self.verdicts if v.verdict is Verdict.AGREED]

    @property
    def disputed(self) -> list[QuestionVerdict]:
        return [v for v in self.verdicts if v.verdict is Verdict.DISPUTED]

    @property
    def uncertain(self) -> list[QuestionVerdict]:
        return [v for v in self.verdicts if v.verdict is Verdict.UNCERTAIN]

    @property
    def errors(self) -> list[QuestionVerdict]:
        return [v for v in self.verdicts if v.verdict is Verdict.ERROR]

    @property
    def flagged(self) -> list[QuestionVerdict]:
        """Everything a human should look at before a student sees it."""
        return [v for v in self.verdicts if v.needs_review]

    @property
    def agreement_rate(self) -> float:
        """Share of questions the verifier independently confirmed."""
        return len(self.agreed) / self.total if self.total else 0.0

    def summary(self) -> str:
        return (
            f"verified {self.total}: {len(self.agreed)} agreed, "
            f"{len(self.disputed)} disputed, {len(self.uncertain)} uncertain, "
            f"{len(self.errors)} errored "
            f"({self.agreement_rate:.0%} agreement)"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "agreed": len(self.agreed),
            "disputed": len(self.disputed),
            "uncertain": len(self.uncertain),
            "errors": len(self.errors),
            "agreement_rate": round(self.agreement_rate, 3),
            "flagged": [v.to_dict() for v in self.flagged],
        }


# --------------------------------------------------------------------------- #
# Prompts
# --------------------------------------------------------------------------- #

SYSTEM_PROMPT = """You are a meticulous mathematics checker. You are given \
multiple-choice and true/false questions written by someone else and you work each one out \
from scratch.

You are never told which answer is supposed to be correct, and you must not \
guess at what the author intended. Solve the problem yourself and report what \
you actually get. If your result is not among the options, say so. If more \
than one option is defensible, say so. Being the second opinion is the whole \
job -- agreeing for the sake of agreeing is worse than useless."""


SOLVE_PROMPT = """Solve this question yourself.

QUESTION:
{question_text}

OPTIONS:
{options_block}

Work it out step by step before choosing. Then judge two further things:

- "exactly_one_correct": true if exactly one option is correct. False if none
  of them are, or if two or more are defensible. For a true/false statement,
  false if the statement is true in some cases and false in others.
- "well_posed": true if the question is clear, self-contained and answerable
  as written. False if it is ambiguous, missing information, or contradictory.

Return ONLY this JSON object, with no prose and no markdown fence:
{{
  "reasoning": "your working, in one or two sentences",
  "answer": "A",
  "exactly_one_correct": true,
  "well_posed": true,
  "confidence": 0.95
}}

If no option matches your result, set "answer" to "NONE"."""


class QuestionVerifierAgent(BaseAgent):
    """Independently solves questions to check their answer keys.

    Example:
        verifier = QuestionVerifierAgent()
        report = verifier.verify(quiz.questions)
        print(report.summary())
        for verdict in report.flagged:
            print(verdict.summary())
    """

    def __init__(
        self,
        escalation_samples: int = 2,
        escalation_temperature: float = 0.7,
        max_workers: int = 5,
        **kwargs: Any,
    ) -> None:
        # Temperature 0: the first pass should be the verifier's best single
        # answer, not a creative one.
        kwargs.setdefault("temperature", 0.0)
        kwargs.setdefault("max_tokens", 800)
        super().__init__(agent_name="QuestionVerifier", **kwargs)
        self.escalation_samples = escalation_samples
        self.escalation_temperature = escalation_temperature
        self.max_workers = max_workers
        self._escalation_llm: Any | None = None

    # ------------------------------------------------------------------ #
    # BaseAgent contract
    # ------------------------------------------------------------------ #

    def get_system_prompt(self) -> str:
        return SYSTEM_PROMPT

    def get_tools(self) -> list[Tool]:
        return [
            Tool(
                name="solve_question",
                func=self._tool_solve_question,
                description=(
                    "Independently solve one multiple-choice question without being "
                    'told the intended answer. Input: JSON such as {"question_text": '
                    '"What is -6 x 4?", "options": [{"key": "A", "text": "-24"}, ...]}. '
                    "Returns the solved answer, whether exactly one option is correct, "
                    "and whether the question is well posed."
                ),
            ),
            Tool(
                name="verify_answer_key",
                func=self._tool_verify_answer_key,
                description=(
                    "Check a question's claimed answer key by solving it blind and "
                    'comparing. Input: JSON such as {"question_text": "...", "options": '
                    '[...], "correct_answer": "A"}. Returns AGREED, DISPUTED, '
                    "UNCERTAIN or ERROR with the verifier's own answer."
                ),
            ),
        ]

    # ------------------------------------------------------------------ #
    # Solving
    # ------------------------------------------------------------------ #

    @staticmethod
    def _options_block(options: list[dict[str, str]]) -> str:
        return "\n".join(
            f"{option.get('key', '?')}) {option.get('text', '')}" for option in options
        )

    def _escalation_llm_instance(self) -> Any:  # noqa: ANN401 - ChatOpenAI
        """A second LLM handle at a higher temperature, for extra samples."""
        if self._escalation_llm is None:
            from langchain_openai import ChatOpenAI

            self._escalation_llm = ChatOpenAI(
                model=self.model,
                api_key=settings.NEBIUS_API_KEY,
                base_url=settings.NEBIUS_BASE_URL,
                temperature=self.escalation_temperature,
                max_tokens=self.max_tokens,
                timeout=self.request_timeout,
                max_retries=2,
            )
        return self._escalation_llm

    def solve(
        self,
        question_text: str,
        options: list[dict[str, str]],
        escalated: bool = False,
    ) -> dict[str, Any] | None:
        """Solve one question blind.

        Args:
            question_text: The question stem.
            options: The options -- four, or True and False -- as
                ``{"key": ..., "text": ...}``.
            escalated: Use the higher-temperature model for an extra sample.

        Returns:
            The parsed verifier response, or None if it could not be obtained.
            Note that ``options`` is passed **without** any correct-answer
            marker; that omission is what makes this an independent check.
        """
        prompt = SOLVE_PROMPT.format(
            question_text=question_text,
            options_block=self._options_block(options),
        )
        llm = self._escalation_llm_instance() if escalated else self.get_llm()

        try:
            response = llm.invoke(
                [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ]
            )
        except Exception as exc:
            logger.warning("Verifier solve failed: %s", exc)
            return None

        parsed = self.parse_json_output(getattr(response, "content", "") or "")
        if not isinstance(parsed, dict):
            logger.warning("Verifier returned unparseable output.")
            return None

        answer = str(parsed.get("answer", "")).strip().upper()
        if answer not in (*OPTION_KEYS, "NONE"):
            # Tolerate "Option B" / "B)" style answers.
            for key in OPTION_KEYS:
                if answer.startswith(key) or f"OPTION {key}" in answer:
                    answer = key
                    break
            else:
                logger.warning("Verifier gave an unusable answer: %r", answer)
                return None

        return {
            "answer": answer,
            "reasoning": str(parsed.get("reasoning", "")).strip()[:600],
            "exactly_one_correct": bool(parsed.get("exactly_one_correct", True)),
            "well_posed": bool(parsed.get("well_posed", True)),
            "confidence": float(parsed.get("confidence", 0.0) or 0.0),
        }

    # ------------------------------------------------------------------ #
    # Verification
    # ------------------------------------------------------------------ #

    def verify_question(
        self,
        question_number: int,
        question_text: str,
        options: list[dict[str, str]],
        claimed_answer: str,
    ) -> QuestionVerdict:
        """Verify one question's answer key.

        Solves once at temperature 0. If that disagrees with the claimed key,
        escalates to additional higher-temperature samples and decides by
        majority vote across all of them.
        """
        claimed = str(claimed_answer).strip().upper()
        verdict = QuestionVerdict(
            question_number=question_number,
            question_text=question_text,
            claimed_answer=claimed,
        )

        first = self.solve(question_text, options)
        if first is None:
            verdict.verdict = Verdict.ERROR
            verdict.issues.append("verifier could not solve the question")
            return verdict

        verdict.samples = 1
        verdict.reasoning = first["reasoning"]
        verdict.confidence = first["confidence"]
        verdict.verifier_answer = first["answer"]

        if not first["exactly_one_correct"]:
            verdict.issues.append("verifier says not exactly one option is correct")
        if not first["well_posed"]:
            verdict.issues.append("verifier says the question is not well posed")

        answers = [first["answer"]]

        # Agreement on the first pass is the common case; stop there unless the
        # verifier itself flagged a structural problem with the question.
        if first["answer"] == claimed and not verdict.issues:
            verdict.votes = {claimed: 1}
            verdict.verdict = Verdict.AGREED
            return verdict

        for _ in range(self.escalation_samples):
            extra = self.solve(question_text, options, escalated=True)
            if extra is None:
                continue
            answers.append(extra["answer"])
            verdict.samples += 1
            if not extra["exactly_one_correct"]:
                verdict.issues.append("a sample says not exactly one option is correct")
            if not extra["well_posed"]:
                verdict.issues.append("a sample says the question is not well posed")

        verdict.issues = list(dict.fromkeys(verdict.issues))  # de-duplicate
        counts = Counter(answers)
        verdict.votes = dict(counts)
        winner, winning_votes = counts.most_common(1)[0]
        verdict.verifier_answer = winner
        verdict.confidence = winning_votes / len(answers)

        # A tie between two answers is not a decision.
        tied = [key for key, count in counts.items() if count == winning_votes]

        if len(tied) > 1:
            verdict.verdict = Verdict.UNCERTAIN
            verdict.issues.append(f"samples split between {sorted(tied)}")
        elif winner == claimed:
            # It agreed on balance; any flags still warrant a human look.
            verdict.verdict = Verdict.UNCERTAIN if verdict.issues else Verdict.AGREED
        elif winner == "NONE":
            verdict.verdict = Verdict.DISPUTED
            verdict.issues.append("verifier found no correct option among the options")
        else:
            verdict.verdict = Verdict.DISPUTED

        return verdict

    def verify(self, questions: list[GeneratedQuestion]) -> VerificationReport:
        """Verify a whole question set, checking questions concurrently.

        Args:
            questions: The generated questions to check.

        Returns:
            A :class:`VerificationReport`. Verification never raises: a failed
            check becomes an ``ERROR`` verdict so one bad call cannot discard
            an otherwise good quiz.
        """
        if not questions:
            return VerificationReport()

        def _run(question: GeneratedQuestion) -> QuestionVerdict:
            try:
                return self.verify_question(
                    question_number=question.question_number,
                    question_text=question.question_text,
                    options=question.options,
                    claimed_answer=question.correct_answer,
                )
            except Exception as exc:  # pragma: no cover - defensive
                logger.exception("Verification crashed on Q%d", question.question_number)
                return QuestionVerdict(
                    question_number=question.question_number,
                    question_text=question.question_text,
                    claimed_answer=question.correct_answer,
                    verdict=Verdict.ERROR,
                    issues=[str(exc)[:200]],
                )

        workers = max(1, min(self.max_workers, len(questions)))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            verdicts = list(pool.map(_run, questions))

        verdicts.sort(key=lambda v: v.question_number)
        report = VerificationReport(verdicts=verdicts)

        logger.info("%s: %s", self.agent_name, report.summary())
        for flagged in report.flagged:
            logger.warning("%s: %s", self.agent_name, flagged.summary())

        return report

    # ------------------------------------------------------------------ #
    # Tools
    # ------------------------------------------------------------------ #

    def _tool_solve_question(self, raw: str) -> str:
        """Solve one question supplied as JSON."""
        parsed = self.parse_json_output(raw)
        if not isinstance(parsed, dict):
            return 'ERROR: expected JSON such as {"question_text": "...", "options": [...]}.'

        options = parsed.get("options")
        if not isinstance(options, list) or not options:
            return "ERROR: 'options' must be a non-empty list."

        result = self.solve(str(parsed.get("question_text", "")), options)
        if result is None:
            return "ERROR: could not solve the question."
        return json.dumps(result, indent=2)

    def _tool_verify_answer_key(self, raw: str) -> str:
        """Verify one question's claimed answer key."""
        parsed = self.parse_json_output(raw)
        if not isinstance(parsed, dict):
            return "ERROR: expected a JSON object with question_text, options, correct_answer."

        options = parsed.get("options")
        if not isinstance(options, list) or not options:
            return "ERROR: 'options' must be a non-empty list."

        verdict = self.verify_question(
            question_number=int(parsed.get("question_number", 1) or 1),
            question_text=str(parsed.get("question_text", "")),
            options=options,
            claimed_answer=str(parsed.get("correct_answer", "")),
        )
        return json.dumps(verdict.to_dict(), indent=2)


__all__ = [
    "QuestionVerdict",
    "QuestionVerifierAgent",
    "VerificationReport",
    "Verdict",
]
