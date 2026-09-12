"""Phase 3 Question Verifier tests.

The verifier is the guard against a question whose stated answer is simply
wrong -- something structural validation cannot detect. These tests stub the
LLM so the behaviour is deterministic and costs nothing to run.

The most important test in this file is
``test_verifier_never_sees_the_claimed_answer``: shown the answer key, a model
rationalises its way into agreeing, and the whole verification step becomes
theatre.

Run:
    cd backend
    pytest tests/test_question_verifier.py -v
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from agents.question_verifier import (
    QuestionVerdict,
    QuestionVerifierAgent,
    Verdict,
    VerificationReport,
)
from agents.quiz_generator import GeneratedQuestion

# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def make_question(
    number: int = 1,
    correct: str = "A",
    text: str = "What is -6 x 4?",
) -> GeneratedQuestion:
    return GeneratedQuestion(
        question_number=number,
        question_text=text,
        options=[
            {"key": "A", "text": "-24"},
            {"key": "B", "text": "24"},
            {"key": "C", "text": "-10"},
            {"key": "D", "text": "10"},
        ],
        correct_answer=correct,
        explanation="A negative times a positive is negative, so -6 x 4 = -24.",
        distractor_rationales={key: f"mistake {key}" for key in "ABCD" if key != correct},
        hint="Apply the sign rule.",
        skill_tag="multiply_integers",
    )


def solve_payload(
    answer: str = "A",
    exactly_one: bool = True,
    well_posed: bool = True,
    confidence: float = 0.95,
) -> str:
    return json.dumps(
        {
            "reasoning": "Six times four is twenty-four; the signs differ so it is negative.",
            "answer": answer,
            "exactly_one_correct": exactly_one,
            "well_posed": well_posed,
            "confidence": confidence,
        }
    )


class ScriptedLLM:
    """Returns pre-scripted responses and records every prompt it received."""

    def __init__(self, payloads: list[str]) -> None:
        self.payloads = list(payloads)
        self.prompts: list[str] = []

    def invoke(self, messages: list[dict[str, str]]) -> Any:  # noqa: ANN401
        self.prompts.append(messages[-1]["content"])
        payload = self.payloads.pop(0) if self.payloads else solve_payload()

        class _Response:
            def __init__(self, text: str) -> None:
                self.content = text

        return _Response(payload)


def build_verifier(payloads: list[str], escalation_samples: int = 2) -> QuestionVerifierAgent:
    """A verifier whose primary and escalation models are both scripted."""
    verifier = QuestionVerifierAgent(escalation_samples=escalation_samples, max_workers=2)
    scripted = ScriptedLLM(payloads)
    verifier._llm = scripted
    verifier._escalation_llm = scripted  # same script drives both passes
    return verifier


# --------------------------------------------------------------------------- #
# Blindness -- the property the whole design rests on
# --------------------------------------------------------------------------- #


def test_verifier_never_sees_the_claimed_answer() -> None:
    """Shown the key, a model agrees with it. The prompt must omit it."""
    verifier = build_verifier([solve_payload("A")])
    question = make_question(correct="B")  # deliberately odd key

    verifier.verify_question(
        question_number=1,
        question_text=question.question_text,
        options=question.options,
        claimed_answer=question.correct_answer,
    )

    prompt = verifier._llm.prompts[0]
    assert "What is -6 x 4?" in prompt
    assert "-24" in prompt, "the option texts must be present"
    # No marker of which option is intended.
    assert "correct_answer" not in prompt
    assert "claimed" not in prompt.lower()
    assert "intended" not in prompt.lower()
    for marker in ("<== correct", "(correct)", "* correct"):
        assert marker not in prompt


def test_prompt_asks_for_independent_working() -> None:
    verifier = build_verifier([solve_payload("A")])
    verifier.verify_question(1, "What is 2 + 2?", make_question().options, "A")

    prompt = verifier._llm.prompts[0]
    assert "step by step" in prompt.lower()
    assert "exactly_one_correct" in prompt
    assert "well_posed" in prompt


# --------------------------------------------------------------------------- #
# Verdicts
# --------------------------------------------------------------------------- #


def test_agreement_stops_after_one_sample() -> None:
    """The common case must not pay for escalation."""
    verifier = build_verifier([solve_payload("A")])
    verdict = verifier.verify_question(1, "What is -6 x 4?", make_question().options, "A")

    assert verdict.verdict is Verdict.AGREED
    assert verdict.samples == 1
    assert len(verifier._llm.prompts) == 1
    assert verdict.needs_review is False


def test_disagreement_escalates_and_disputes() -> None:
    """A wrong answer key must be caught."""
    # First pass says B, both escalation samples agree on B; the key says A.
    verifier = build_verifier([solve_payload("B"), solve_payload("B"), solve_payload("B")])
    verdict = verifier.verify_question(1, "What is -6 x 4?", make_question().options, "A")

    assert verdict.verdict is Verdict.DISPUTED
    assert verdict.verifier_answer == "B"
    assert verdict.samples == 3
    assert verdict.votes == {"B": 3}
    assert verdict.confidence == pytest.approx(1.0)
    assert verdict.needs_review is True


def test_split_samples_are_uncertain_not_disputed() -> None:
    """A tie is not a decision."""
    verifier = build_verifier([solve_payload("B"), solve_payload("A"), solve_payload("C")])
    verdict = verifier.verify_question(1, "q", make_question().options, "A")

    assert verdict.verdict is Verdict.UNCERTAIN
    assert any("split" in issue for issue in verdict.issues)


def test_majority_restores_agreement() -> None:
    """One slip on the first pass should not condemn a good question."""
    verifier = build_verifier([solve_payload("B"), solve_payload("A"), solve_payload("A")])
    verdict = verifier.verify_question(1, "q", make_question().options, "A")

    assert verdict.verdict is Verdict.AGREED
    assert verdict.verifier_answer == "A"
    assert verdict.votes == {"B": 1, "A": 2}


def test_no_matching_option_is_disputed() -> None:
    """'NONE' means the four options do not contain the right answer."""
    verifier = build_verifier([solve_payload("NONE")] * 3)
    verdict = verifier.verify_question(1, "q", make_question().options, "A")

    assert verdict.verdict is Verdict.DISPUTED
    assert any("no correct option" in issue for issue in verdict.issues)


def test_ambiguous_question_is_flagged_even_when_answer_matches() -> None:
    """Two defensible options make a question unscorable."""
    verifier = build_verifier(
        [solve_payload("A", exactly_one=False), solve_payload("A"), solve_payload("A")]
    )
    verdict = verifier.verify_question(1, "q", make_question().options, "A")

    assert verdict.verdict is Verdict.UNCERTAIN
    assert any("not exactly one option" in issue for issue in verdict.issues)


def test_ill_posed_question_is_flagged() -> None:
    verifier = build_verifier(
        [solve_payload("A", well_posed=False), solve_payload("A"), solve_payload("A")]
    )
    verdict = verifier.verify_question(1, "q", make_question().options, "A")

    assert verdict.verdict is Verdict.UNCERTAIN
    assert any("not well posed" in issue for issue in verdict.issues)


def test_unparseable_response_is_an_error_not_a_crash() -> None:
    verifier = build_verifier(["I cannot help with that."])
    verdict = verifier.verify_question(1, "q", make_question().options, "A")

    assert verdict.verdict is Verdict.ERROR
    assert verdict.verifier_answer is None


def test_prose_wrapped_answer_is_recovered() -> None:
    payload = json.dumps(
        {
            "reasoning": "working",
            "answer": "Option C",
            "exactly_one_correct": True,
            "well_posed": True,
            "confidence": 0.8,
        }
    )
    verifier = build_verifier([payload] * 3)
    verdict = verifier.verify_question(1, "q", make_question().options, "C")

    assert verdict.verdict is Verdict.AGREED
    assert verdict.verifier_answer == "C"


def test_nonsense_answer_is_an_error() -> None:
    payload = json.dumps({"reasoning": "x", "answer": "purple", "confidence": 0.1})
    verifier = build_verifier([payload])
    verdict = verifier.verify_question(1, "q", make_question().options, "A")

    assert verdict.verdict is Verdict.ERROR


def test_api_failure_becomes_an_error_verdict() -> None:
    class BrokenLLM:
        def invoke(self, _messages: list[dict[str, str]]) -> Any:  # noqa: ANN401
            raise RuntimeError("nebius is down")

    verifier = QuestionVerifierAgent()
    verifier._llm = BrokenLLM()
    verdict = verifier.verify_question(1, "q", make_question().options, "A")

    assert verdict.verdict is Verdict.ERROR
    assert verdict.needs_review is False, "an outage is not evidence against the question"


# --------------------------------------------------------------------------- #
# Reports
# --------------------------------------------------------------------------- #


def test_verify_reports_across_a_set() -> None:
    verifier = build_verifier([solve_payload("A")] * 6)
    questions = [make_question(number=n, correct="A") for n in range(1, 4)]

    report = verifier.verify(questions)

    assert report.total == 3
    assert len(report.agreed) == 3
    assert report.agreement_rate == 1.0
    assert report.flagged == []
    assert "3 agreed" in report.summary()


def test_verify_preserves_question_order() -> None:
    """Concurrency must not scramble the report."""
    verifier = build_verifier([solve_payload("A")] * 20)
    questions = [make_question(number=n, correct="A") for n in range(1, 8)]

    report = verifier.verify(questions)
    assert [v.question_number for v in report.verdicts] == list(range(1, 8))


def test_empty_set_is_handled() -> None:
    assert QuestionVerifierAgent().verify([]).total == 0


def test_report_serialises_flagged_only() -> None:
    report = VerificationReport(
        verdicts=[
            QuestionVerdict(1, "q1", "A", "A", Verdict.AGREED),
            QuestionVerdict(2, "q2", "A", "B", Verdict.DISPUTED),
        ]
    )
    payload = report.to_dict()

    assert payload["total"] == 2
    assert payload["agreed"] == 1
    assert payload["disputed"] == 1
    assert payload["agreement_rate"] == 0.5
    assert len(payload["flagged"]) == 1
    assert payload["flagged"][0]["question_number"] == 2


def test_verdict_summary_is_readable() -> None:
    verdict = QuestionVerdict(
        question_number=3,
        question_text="q",
        claimed_answer="A",
        verifier_answer="C",
        verdict=Verdict.DISPUTED,
        votes={"C": 3},
        samples=3,
        issues=["verifier found no correct option among the four"],
    )
    text = verdict.summary()
    assert "Q3" in text and "DISPUTED" in text and "claimed A" in text and "verifier C" in text


# --------------------------------------------------------------------------- #
# Tool surface
# --------------------------------------------------------------------------- #


def test_agent_exposes_expected_tools() -> None:
    names = {tool.name for tool in QuestionVerifierAgent().get_tools()}
    assert names == {"solve_question", "verify_answer_key"}


def test_solve_tool_returns_json() -> None:
    verifier = build_verifier([solve_payload("A")])
    payload = json.dumps({"question_text": "What is -6 x 4?", "options": make_question().options})
    result = json.loads(verifier._tool_solve_question(payload))
    assert result["answer"] == "A"


def test_verify_tool_reports_a_verdict() -> None:
    verifier = build_verifier([solve_payload("B")] * 3)
    payload = json.dumps(
        {
            "question_number": 2,
            "question_text": "What is -6 x 4?",
            "options": make_question().options,
            "correct_answer": "A",
        }
    )
    result = json.loads(verifier._tool_verify_answer_key(payload))
    assert result["verdict"] == "disputed"
    assert result["verifier_answer"] == "B"


def test_tools_reject_malformed_input() -> None:
    verifier = QuestionVerifierAgent()
    assert "ERROR" in verifier._tool_solve_question("not json")
    assert "ERROR" in verifier._tool_verify_answer_key('{"question_text": "q"}')
