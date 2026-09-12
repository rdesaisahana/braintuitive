"""The eval tooling: reading the golden dataset, scoring, and the results workbook.

These use canned questions, like every other test; the eval itself is what runs
the real AI.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agents.question_verifier import QuestionVerdict, Verdict, VerificationReport
from agents.quiz_generator import GeneratedQuestion, GeneratedQuiz
from db.models import DifficultyLevel, QuestionType
from evals.combine_runs import comparison_rows
from evals.run_quiz_generator_eval import CaseResult, build_workbook, letter_balance, select_cases
from evals.xlsx import BODY, HEADER, Formula, Sheet, read_sheet, write_workbook


def _row(case_id: str, first: str = "Y", reviewed: str = "Y", expected: str = "5 questions") -> dict[str, str]:
    return {
        "case_id": case_id, "topic": "1.2", "level": "Easy", "first run?": first,
        "reviewed by you?": reviewed, "expected behaviour": expected,
        "learning objective": "Add integers.", "a good question looks like": "One step.",
        "must NOT include": "fractions",
    }


def _question(number: int, key: str = "A", true_false: bool = False) -> GeneratedQuestion:
    options = (
        [{"key": "A", "text": "True"}, {"key": "B", "text": "False"}]
        if true_false
        else [{"key": k, "text": f"{k}{number}"} for k in "ABCD"]
    )
    return GeneratedQuestion(
        question_number=number, question_text=f"Question {number}?", options=options,
        correct_answer=key, explanation="Because.", distractor_rationales={}, hint="Think.",
        question_type=QuestionType.TRUE_FALSE if true_false else QuestionType.MULTIPLE_CHOICE,
    )


def _quiz(questions: list[GeneratedQuestion]) -> GeneratedQuiz:
    report = VerificationReport(verdicts=[
        QuestionVerdict(question_number=q.question_number, question_text=q.question_text,
                        claimed_answer=q.correct_answer, verifier_answer=q.correct_answer,
                        verdict=Verdict.AGREED)
        for q in questions
    ])
    return GeneratedQuiz(sub_unit_id="s", sub_unit_number="1.2", difficulty=DifficultyLevel.BEGINNER,
                         questions=questions, attempts=1, verification=report)


def test_workbook_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "book.xlsx"
    rows = [[("name", HEADER), ("score", HEADER), ("double", HEADER)],
            [("3.10", BODY), (4, BODY), (Formula("B2*2"), BODY)]]
    write_workbook(path, [Sheet("Data", rows, widths=[10, 10, 10])])

    # Text stays text (3.10 is not turned into 3.1); a formula has no value until Excel opens it.
    assert read_sheet(path, "Data") == [{"name": "3.10", "score": "4", "double": ""}]


def test_workbook_is_never_overwritten(tmp_path: Path) -> None:
    path = tmp_path / "book.xlsx"
    write_workbook(path, [Sheet("Data", [[("a", BODY)]], widths=[5])])
    with pytest.raises(FileExistsError):
        write_workbook(path, [Sheet("Data", [[("b", BODY)]], widths=[5])])


def test_only_reviewed_first_run_rows_are_selected() -> None:
    rows = [_row("TC001"), _row("TC002", reviewed="N"), _row("TC003", first="N")]
    assert [r["case_id"] for r in select_cases(rows)] == ["TC001"]


def test_named_rows_must_be_reviewed() -> None:
    rows = [_row("TC001"), _row("TC002", reviewed="N")]
    assert [r["case_id"] for r in select_cases(rows, ["tc001"])] == ["TC001"]
    with pytest.raises(SystemExit, match="Not reviewed"):
        select_cases(rows, ["TC002"])
    with pytest.raises(SystemExit, match="Not in the golden"):
        select_cases(rows, ["TC999"])


def test_letter_balance_ignores_true_false() -> None:
    questions = [_question(1, "A", True), _question(2, "A", True), _question(3, "A"), _question(4, "B")]
    assert letter_balance(questions) == 0.5
    assert letter_balance([_question(1, "A", True)]) is None


def test_an_edge_row_passes_only_by_failing_clearly() -> None:
    edge = CaseResult(case=_row("TC142", expected="A clear error"), asked=5,
                      error="QuizGenerationError: No sub-unit matching '9.9'.")
    assert edge.behaviour_met
    invented = CaseResult(case=_row("TC142", expected="A clear error"), asked=5, quiz=_quiz([_question(1)]))
    assert not invented.behaviour_met
    normal = CaseResult(case=_row("TC004"), asked=2, quiz=_quiz([_question(1), _question(2, "B")]))
    assert normal.behaviour_met


def test_comparison_scores_each_run_from_its_own_sheets() -> None:
    """Run 2's numbers must come from run 2's sheets, not run 1's."""
    rows = comparison_rows([("Run 1", "Results", "Cases"), ("Run 2", "Results 2", "Cases 2")])

    header = [cell.get("text") for cell in rows[0]]
    assert header == ["Metric", "Run 1", "Run 2", "Change", "Target", "Better when"]

    hint = next(r for r in rows if r[0].get("text") == "Good hint")
    assert "'Results'!" in hint[1]["formula"] and "'Results 2'!" not in hint[1]["formula"]
    assert "'Results 2'!" in hint[2]["formula"]
    assert hint[3]["formula"].endswith("C4-B4") or "C" in hint[3]["formula"]

    agreement = next(r for r in rows if r[0].get("text") == "Checker agreement")
    assert "'Cases'!" in agreement[1]["formula"] and "'Cases 2'!" in agreement[2]["formula"]


def test_results_workbook_has_one_row_per_question(tmp_path: Path) -> None:
    results = [
        CaseResult(case=_row("TC004"), asked=2, quiz=_quiz([_question(1), _question(2, "B", True)]),
                   drafted=3, format_passed=2, seconds=12.5),
        CaseResult(case=_row("TC142", expected="A clear error"), asked=2,
                   error="QuizGenerationError: No sub-unit matching '9.9'."),
    ]
    sheets, chart_range, chart_anchor = build_workbook(results, {"Run": "test"})
    path = tmp_path / "run.xlsx"
    write_workbook(path, sheets)

    graded = read_sheet(path, "Results")
    assert [(r["case_id"], r["type"]) for r in graded] == [("TC004", "Multiple choice"), ("TC004", "True/False")]
    assert graded[1]["marked answer"] == "B) False"
    assert graded[0]["checker verdict"] == "agreed"
    assert all(r["Correct key"] == "" for r in graded), "grades are left for a person"

    cases = {r["case_id"]: r for r in read_sheet(path, "Cases")}
    assert cases["TC004"]["complete?"] == "Y"
    assert cases["TC004"]["drafted by AI"] == "3"
    assert cases["TC142"]["behaviour met?"] == "Y"
    assert chart_range and chart_anchor
