"""Run the Generate Quiz agent on the golden dataset and write a results workbook.

Usage, from the backend folder:

    ../.venv/Scripts/python.exe -m evals.run_quiz_generator_eval --owner you@example.com --cases TC004 --label trial
    ../.venv/Scripts/python.exe -m evals.run_quiz_generator_eval --owner you@example.com

What happens, step by step:

1. Read the golden dataset (evals/quiz_generator_golden_v1.xlsx) and keep the
   rows marked "first run? = Y" and "reviewed by you? = Y". ``--cases`` picks
   specific rows instead; they must still be reviewed.
2. Copy the database to a temporary file. The agent saves a skill vocabulary
   as it works, and an eval must not change real data.
3. For each row, find the topic in the owner's own curriculum and ask the real
   agent -- the real AI, with the answer checker on, exactly as the question
   bank does -- for ``--questions`` questions, timing it.
4. Write evals/runs/quiz_generator_run_<date>_<time>.xlsx:
     Summary  the scores against their targets (your proof of evals)
     Results  one row per generated question, with empty Y/N columns to grade
     Cases    the numbers measured automatically for each row
5. Ask Excel to fit the rows and add a chart (skipped if Excel is missing).
"""

from __future__ import annotations

import argparse
import logging
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, func
from sqlalchemy.orm import Session, sessionmaker

from agents.question_verifier import QuestionVerdict
from agents.quiz_generator import (
    GeneratedQuestion,
    GeneratedQuiz,
    QuizGeneratorAgent,
    validate_question,
)
from config import settings
from db.models import CurriculumSubUnit, CurriculumUnit, DifficultyLevel, User
from evals.xlsx import (
    BODY,
    DEFAULT,
    DXF_BAD,
    DXF_GOOD,
    HEADER,
    HEADING,
    INPUT,
    LABEL,
    NUMBER,
    PERCENT,
    TEXT,
    TITLE,
    Formula,
    Sheet,
    col,
    highlight_equal,
    read_sheet,
    write_workbook,
    yes_no_dropdown,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
GOLDEN = PROJECT_ROOT / "evals" / "quiz_generator_golden_v1.xlsx"
RUNS = PROJECT_ROOT / "evals" / "runs"
FINISH_SCRIPT = Path(__file__).with_name("finish_workbook.ps1")

LEVELS = {
    "easy": DifficultyLevel.BEGINNER,
    "medium": DifficultyLevel.INTERMEDIATE,
    "tricky": DifficultyLevel.PROFICIENT,
}

# The checks you grade by hand (see the Rubric sheet), and the score each should reach.
GRADES = [
    ("Correct key", 0.98),
    ("One right answer", 0.9),
    ("On topic", 0.9),
    ("Right level", 0.9),
    ("Good wrong answers", 0.9),
    ("Good explanation", 0.9),
    ("Good hint", 0.9),
]

RESULT_COLUMNS = [
    ("case_id", 9), ("topic", 7), ("level", 8), ("q#", 5), ("type", 12), ("question", 50),
    ("A", 18), ("B", 18), ("C", 18), ("D", 18), ("marked answer", 16), ("explanation", 46),
    ("hint", 34), ("checker verdict", 11), ("checker's answer", 10),
    *[(name, 11) for name, _ in GRADES],
    ("your comments", 34), ("learning objective", 36), ("a good question looks like", 34),
    ("must NOT include", 34), ("skill tag", 20),
]
CASE_COLUMNS = [
    ("case_id", 9), ("topic", 7), ("level", 8), ("kind", 14), ("status", 30), ("asked", 7),
    ("returned", 9), ("complete?", 10), ("drafted by AI", 10), ("passed format check", 11),
    ("format pass rate", 10), ("checked by checker", 10), ("checker agreed", 10),
    ("checker agreement", 11), ("rejected by checker", 11), ("attempts", 9), ("seconds", 9),
    ("letter balance", 10), ("true/false", 9), ("retrieval score", 10), ("expected behaviour", 28),
    ("behaviour met?", 10), ("warnings or error", 44),
]
EDGE = "edge: expect error"

log = logging.getLogger("evals")


# --------------------------------------------------------------------------- #
# Choosing the cases
# --------------------------------------------------------------------------- #


def is_yes(value: str) -> bool:
    return str(value).strip().upper() == "Y"


def select_cases(rows: list[dict[str, str]], only: list[str] | None = None) -> list[dict[str, str]]:
    """The rows to run: first run = Y and reviewed = Y, or the named ones.

    A named row must still be reviewed: only a row a person has confirmed is
    part of the golden dataset.
    """
    if not only:
        return [r for r in rows if is_yes(r.get("first run?", "")) and is_yes(r.get("reviewed by you?", ""))]
    wanted = [c.strip().upper() for c in only]
    by_id = {r.get("case_id", "").strip().upper(): r for r in rows}
    missing = [c for c in wanted if c not in by_id]
    if missing:
        raise SystemExit(f"Not in the golden dataset: {', '.join(missing)}")
    unreviewed = [c for c in wanted if not is_yes(by_id[c].get("reviewed by you?", ""))]
    if unreviewed:
        raise SystemExit(
            f"Not reviewed yet (set 'reviewed by you?' to Y first): {', '.join(unreviewed)}"
        )
    return [by_id[c] for c in wanted]


# --------------------------------------------------------------------------- #
# One case
# --------------------------------------------------------------------------- #


@dataclass
class CaseResult:
    """What happened when the agent was asked for one golden row."""

    case: dict[str, str]
    asked: int
    quiz: GeneratedQuiz | None = None
    drafted: int = 0
    format_passed: int = 0
    seconds: float = 0.0
    error: str = ""

    @property
    def expects_error(self) -> bool:
        return "error" in self.case.get("expected behaviour", "").lower()

    @property
    def questions(self) -> list[GeneratedQuestion]:
        return self.quiz.questions if self.quiz else []

    @property
    def complete(self) -> bool:
        return len(self.questions) >= self.asked

    @property
    def behaviour_met(self) -> bool:
        """An edge row passes by failing clearly; a normal row by completing."""
        if self.expects_error:
            return bool(self.error) and not self.questions
        return not self.error and self.complete


def letter_balance(questions: list[GeneratedQuestion]) -> float | None:
    """The largest share of multiple-choice answers on any one letter.

    True/false answers can only be A or B, so they are left out.
    """
    keys = [q.correct_answer for q in questions if not q.is_true_false]
    if not keys:
        return None
    return max(Counter(keys).values()) / len(keys)


def verdicts_by_text(quiz: GeneratedQuiz | None) -> dict[str, QuestionVerdict]:
    """The checker's verdict for each question, matched by question text.

    Matched by text rather than number: a question the checker rejects is
    replaced, and its replacement can reuse the number.
    """
    found: dict[str, QuestionVerdict] = {}
    if quiz and quiz.verification:
        for verdict in quiz.verification.verdicts:
            found[verdict.question_text.strip()] = verdict
    return found


def watch_drafts(agent: QuizGeneratorAgent) -> list[dict[str, Any]]:
    """Record every question the AI drafts, before any check throws one out.

    That record is what the format pass rate is worked out from. The agent
    itself is unchanged; only this one instance's drafting call is wrapped.
    """
    drafted: list[dict[str, Any]] = []
    original = agent._draft_batch

    def recording(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        batch = original(*args, **kwargs)
        drafted.extend(batch)
        return batch

    agent._draft_batch = recording  # type: ignore[method-assign]
    return drafted


def run_case(
    agent: QuizGeneratorAgent,
    db: Session,
    case: dict[str, str],
    topics: dict[str, CurriculumSubUnit],
    asked: int,
    drafted: list[dict[str, Any]],
) -> CaseResult:
    """Ask the agent for one golden row and time it. Never raises."""
    result = CaseResult(case=case, asked=asked)
    topic = case.get("topic", "").strip()
    level = LEVELS.get(case.get("level", "").strip().lower())
    sub_unit = topics.get(topic)
    drafted.clear()
    start = time.perf_counter()
    try:
        if level is None:
            raise ValueError(f"unknown level {case.get('level')!r} (use Easy, Medium or Tricky)")
        # A topic missing from the owner's curriculum goes to the agent as typed,
        # so the eval sees how the agent itself deals with it.
        result.quiz = agent.generate(
            sub_unit=sub_unit.id if sub_unit else topic, difficulty=level, count=asked, db=db
        )
    except Exception as exc:  # noqa: BLE001 - every failure is a result worth recording
        result.error = f"{type(exc).__name__}: {exc}"
        db.rollback()
    result.seconds = time.perf_counter() - start
    result.drafted = len(drafted)
    result.format_passed = sum(1 for item in drafted if not validate_question(item))
    return result


def progress_line(number: int, total: int, result: CaseResult) -> str:
    case = result.case
    head = f"[{number}/{total}] {case.get('case_id')} topic {case.get('topic')} {case.get('level')}: "
    if result.error:
        verdict = "expected, OK" if result.behaviour_met else "UNEXPECTED"
        return head + f"error ({verdict}) - {result.error[:120]}"
    report = result.quiz.verification if result.quiz else None
    agreed = f"{len(report.agreed)}/{report.total}" if report else "n/a"
    return (head + f"{len(result.questions)}/{result.asked} questions, checker agreed {agreed}, "
            f"{result.quiz.attempts} attempt(s), {result.seconds:.0f}s")


# --------------------------------------------------------------------------- #
# The results workbook
# --------------------------------------------------------------------------- #


def _letters(columns: list[tuple[str, float]]) -> dict[str, str]:
    return {name: col(i) for i, (name, _width) in enumerate(columns)}


def results_sheet(results: list[CaseResult]) -> Sheet:
    grade_names = {name for name, _ in GRADES}
    rows: list[list[tuple[Any, int]]] = [[(name, HEADER) for name, _ in RESULT_COLUMNS]]
    for result in results:
        case = result.case
        verdicts = verdicts_by_text(result.quiz)
        for number, question in enumerate(result.questions, start=1):
            options = {o["key"]: o["text"] for o in question.options}
            verdict = verdicts.get(question.question_text.strip())
            values = {
                "case_id": case.get("case_id", ""),
                "topic": case.get("topic", ""),
                "level": case.get("level", ""),
                "q#": number,
                "type": "True/False" if question.is_true_false else "Multiple choice",
                "question": question.question_text,
                **{key: options.get(key, "") for key in "ABCD"},
                "marked answer": f"{question.correct_answer}) {options.get(question.correct_answer, '')}",
                "explanation": question.explanation,
                "hint": question.hint,
                "checker verdict": verdict.verdict.value if verdict else "not checked",
                "checker's answer": (verdict.verifier_answer or "") if verdict else "",
                "learning objective": case.get("learning objective", ""),
                "a good question looks like": case.get("a good question looks like", ""),
                "must NOT include": case.get("must NOT include", ""),
                "skill tag": question.skill_tag,
            }
            rows.append([
                (values.get(name, ""), INPUT if name in grade_names else TEXT if name == "topic" else BODY)
                for name, _ in RESULT_COLUMNS
            ])
    letters = _letters(RESULT_COLUMNS)
    grades = f"{letters[GRADES[0][0]]}2:{letters[GRADES[-1][0]]}2000"
    return Sheet(
        "Results", rows, [w for _, w in RESULT_COLUMNS], freeze=(4, 1), autofilter=True,
        conditional=[highlight_equal(grades, "Y", DXF_GOOD, 1), highlight_equal(grades, "N", DXF_BAD, 2)],
        validations=[yes_no_dropdown(grades)],
    )


def cases_sheet(results: list[CaseResult]) -> Sheet:
    L = _letters(CASE_COLUMNS)
    rows: list[list[tuple[Any, int]]] = [[(name, HEADER) for name, _ in CASE_COLUMNS]]
    for r, result in enumerate(results, start=2):
        quiz, case = result.quiz, result.case
        report = quiz.verification if quiz else None
        balance = letter_balance(result.questions)
        warnings = "; ".join(quiz.warnings) if quiz and quiz.warnings else ""
        values: dict[str, tuple[Any, int]] = {
            "case_id": (case.get("case_id", ""), BODY),
            "topic": (case.get("topic", ""), TEXT),
            "level": (case.get("level", ""), BODY),
            "kind": (EDGE if result.expects_error else "normal", BODY),
            "status": (f"error: {result.error}" if result.error else "ok", BODY),
            "asked": (result.asked, BODY),
            "returned": (len(result.questions), BODY),
            "complete?": ("Y" if result.complete else "N", BODY),
            "drafted by AI": (result.drafted, BODY),
            "passed format check": (result.format_passed, BODY),
            "format pass rate": (Formula(f'IFERROR({L["passed format check"]}{r}/{L["drafted by AI"]}{r},"")'), PERCENT),
            "checked by checker": (report.total if report else 0, BODY),
            "checker agreed": (len(report.agreed) if report else 0, BODY),
            "checker agreement": (Formula(f'IFERROR({L["checker agreed"]}{r}/{L["checked by checker"]}{r},"")'), PERCENT),
            "rejected by checker": (len(quiz.rejected) if quiz else 0, BODY),
            "attempts": (quiz.attempts if quiz else "", BODY),
            "seconds": (round(result.seconds, 1), NUMBER),
            "letter balance": (balance if balance is not None else "", PERCENT),
            "true/false": (sum(q.is_true_false for q in result.questions), BODY),
            "retrieval score": (round(quiz.retrieval_score, 3) if quiz else "", BODY),
            "expected behaviour": (case.get("expected behaviour", ""), BODY),
            "behaviour met?": ("Y" if result.behaviour_met else "N", BODY),
            "warnings or error": (result.error or warnings, BODY),
        }
        rows.append([values[name] for name, _ in CASE_COLUMNS])
    flags = f"{L['complete?']}2:{L['complete?']}500 {L['behaviour met?']}2:{L['behaviour met?']}500"
    return Sheet(
        "Cases", rows, [w for _, w in CASE_COLUMNS], freeze=(3, 1), autofilter=True,
        conditional=[highlight_equal(flags, "Y", DXF_GOOD, 1), highlight_equal(flags, "N", DXF_BAD, 2)],
    )


def summary_sheet(results: list[CaseResult], info: dict[str, str]) -> tuple[Sheet, str, str]:
    """The Summary sheet, the range to chart, and where to put the chart."""
    n_cases = max(len(results), 1)
    n_questions = sum(len(r.questions) for r in results)
    C, R = _letters(CASE_COLUMNS), _letters(RESULT_COLUMNS)

    def cases(name: str) -> str:
        return f"Cases!${C[name]}$2:${C[name]}${n_cases + 1}"

    def graded(name: str) -> str:
        return f"Results!${R[name]}$2:${R[name]}${max(n_questions + 1, 2)}"

    rows: list[list[tuple[Any, int]]] = []

    def add(*cells: tuple[Any, int]) -> int:
        rows.append(list(cells))
        return len(rows)

    add((f"Quiz Generator eval: {info.get('Run', '')}", TITLE))
    add()
    add(("How to use this file", HEADING))
    add(("1. Open the Results sheet: one row per question the AI wrote.", DEFAULT))
    add(("2. Fill in the yellow Y/N columns for every question. The Rubric sheet in the golden dataset explains each check.", DEFAULT))
    add(("3. Come back here: 'Your grades' fills itself in as you grade.", DEFAULT))
    add(("4. Take a screenshot of this sheet as your proof of evals.", DEFAULT))
    add()
    add(("Run details", HEADING))
    for label, value in info.items():
        add((label, LABEL), (value, DEFAULT))
    add()
    add(("Automatic numbers (measured by the script)", HEADING))
    add(*[(h, HEADER) for h in ("Metric", "Score", "Target", "Result", "How it is worked out")])
    automatic = [
        ("Completion", f'IFERROR(COUNTIFS({cases("kind")},"normal",{cases("complete?")},"Y")/COUNTIF({cases("kind")},"normal"),"")',
         1, ">=", PERCENT, "Rows that returned every question they asked for (edge rows left out)."),
        ("Format pass rate", f'IFERROR(SUM({cases("passed format check")})/SUM({cases("drafted by AI")}),"")',
         0.9, ">=", PERCENT, "Of every question the AI drafted, the share that passed the format check first time."),
        ("Checker agreement", f'IFERROR(SUM({cases("checker agreed")})/SUM({cases("checked by checker")}),"")',
         0.9, ">=", PERCENT, "Of every question checked, the share the independent checker solved to the same answer."),
        ("Average attempts", f'IFERROR(AVERAGEIF({cases("kind")},"normal",{cases("attempts")}),"")',
         3, "<=", NUMBER, "Rounds of writing the agent needed per row. More rounds cost more."),
        ("Worst letter balance", f'IF(COUNT({cases("letter balance")})=0,"",MAX({cases("letter balance")}))',
         0.6, "<=", PERCENT, "The largest share of one answer letter in any row. High means guessable."),
        ("True/false share", f'IFERROR(SUM({cases("true/false")})/SUM({cases("returned")}),"")',
         0.3, "<=", PERCENT, "True/false questions among all questions (the agent allows at most 3 in 10)."),
        ("Bad input handled", f'IFERROR(COUNTIFS({cases("kind")},"{EDGE}",{cases("behaviour met?")},"Y")/COUNTIF({cases("kind")},"{EDGE}"),"")',
         1, ">=", PERCENT, "Edge rows (like a topic that doesn't exist) that failed clearly, as they should."),
        ("Average seconds per row", f'IFERROR(AVERAGEIF({cases("kind")},"normal",{cases("seconds")}),"")',
         None, "", NUMBER, "Time per row, including the checker. Recorded, no target."),
    ]
    for name, formula, target, direction, style, how in automatic:
        r = len(rows) + 1
        result = (Formula(f'IF(B{r}="","",IF(B{r}{direction}C{r},"PASS","FAIL"))') if target is not None
                  else "record only")
        add((name, LABEL), (Formula(formula), style), (target if target is not None else "", style),
            (result, BODY), (how, BODY))
    add()
    add(("Your grades (fill in the Results sheet)", HEADING))
    header = add(*[(h, HEADER) for h in ("Check", "Score", f"Graded (of {n_questions})", "Target", "Result")])
    for name, target in GRADES:
        r, rng = len(rows) + 1, graded(name)
        add((name, LABEL),
            (Formula(f'IFERROR(COUNTIF({rng},"Y")/(COUNTIF({rng},"Y")+COUNTIF({rng},"N")),"")'), PERCENT),
            (Formula(f'COUNTIF({rng},"Y")+COUNTIF({rng},"N")'), BODY),
            (target, PERCENT),
            (Formula(f'IF(C{r}=0,"not graded yet",IF(B{r}>=D{r},"PASS","FAIL"))'), BODY))
    last_grade = len(rows)
    r, key, verdict = len(rows) + 1, graded("Correct key"), graded("checker verdict")
    add(("Wrong keys the checker missed", LABEL),
        (Formula(f'COUNTIFS({key},"N",{verdict},"agreed")'), BODY),
        (Formula(f'COUNTIF({key},"Y")+COUNTIF({key},"N")'), BODY),
        (0, BODY),
        (Formula(f'IF(C{r}=0,"not graded yet",IF(B{r}<=D{r},"PASS","FAIL"))'), BODY))
    add(("A wrong key the checker agreed with is a mistake the automatic checker missed: it shows whether the checker can be trusted alone.", DEFAULT))
    chart_anchor = f"A{len(rows) + 3}"
    chart_range = f"A{header}:B{last_grade},D{header}:D{last_grade}"
    sheet = Sheet(
        "Summary", rows, [34, 12, 14, 12, 60],
        conditional=[highlight_equal("D1:E200", "PASS", DXF_GOOD, 1), highlight_equal("D1:E200", "FAIL", DXF_BAD, 2)],
    )
    return sheet, chart_range, chart_anchor


def build_workbook(results: list[CaseResult], info: dict[str, str]) -> tuple[list[Sheet], str, str]:
    summary, chart_range, chart_anchor = summary_sheet(results, info)
    return [summary, results_sheet(results), cases_sheet(results)], chart_range, chart_anchor


def finish_in_excel(path: Path, chart_range: str, chart_anchor: str) -> str:
    """Fit rows and add the chart in Excel. Optional: the workbook works without it."""
    if sys.platform != "win32" or not FINISH_SCRIPT.exists():
        return "skipped (Excel finishing runs on Windows only)"
    started = datetime.now()
    try:
        done = subprocess.run(
            ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(FINISH_SCRIPT),
             "-Path", str(path), "-ChartRange", chart_range, "-ChartAnchor", chart_anchor],
            capture_output=True, text=True, timeout=180, check=False,
        )
    except subprocess.TimeoutExpired:
        _stop_script_excel(started)
        return "skipped (Excel did not finish in 3 minutes; the workbook still works, rows are just not fitted)"
    except OSError as exc:
        return f"skipped ({exc})"
    return (done.stdout or done.stderr).strip() or f"exit code {done.returncode}"


def _stop_script_excel(since: datetime) -> None:
    """Stop an Excel this step started and left hanging -- never one you opened.

    Excel started by a script runs with '/automation' on its command line; one
    you open yourself never does.
    """
    command = (
        "Get-CimInstance Win32_Process -Filter \"Name='EXCEL.EXE'\" | Where-Object { "
        "$_.CommandLine -like '*/automation*' -and "
        f"$_.CreationDate -ge [datetime]'{since:%Y-%m-%dT%H:%M:%S}' }} | "
        "ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"
    )
    try:
        subprocess.run(["powershell.exe", "-NoProfile", "-Command", command],
                       capture_output=True, timeout=60, check=False)
    except (OSError, subprocess.TimeoutExpired):
        pass


# --------------------------------------------------------------------------- #
# Running
# --------------------------------------------------------------------------- #


def snapshot_database(folder: Path) -> Path:
    """Copy the live database into ``folder`` (safe while the app is running)."""
    copy = folder / "eval-snapshot.db"
    source = sqlite3.connect(f"{settings.SQLITE_PATH.resolve().as_uri()}?mode=ro", uri=True)
    target = sqlite3.connect(copy)
    try:
        source.backup(target)
    finally:
        source.close()
        target.close()
    return copy


def owner_topics(db: Session, owner_email: str) -> tuple[User, dict[str, CurriculumSubUnit]]:
    """The owner's account and their curriculum's topics, keyed by number."""
    user = db.query(User).filter(func.lower(User.email) == owner_email.strip().lower()).one_or_none()
    if user is None:
        raise SystemExit(f"No account with the email {owner_email!r}.")
    sub_units = (
        db.query(CurriculumSubUnit)
        .join(CurriculumUnit)
        .filter(CurriculumUnit.user_id == user.id)
        .order_by(CurriculumUnit.grade_level, CurriculumUnit.unit_number, CurriculumSubUnit.sequence)
        .all()
    )
    if not sub_units:
        raise SystemExit(f"{owner_email} has no uploaded curriculum.")
    topics: dict[str, CurriculumSubUnit] = {}
    for sub_unit in sub_units:
        topics.setdefault(sub_unit.sub_unit_number, sub_unit)
    return user, topics


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Evaluate the Generate Quiz agent on the golden dataset.")
    parser.add_argument("--owner", required=True, help="email of the account whose curriculum to use")
    parser.add_argument("--cases", nargs="*", help="only these case ids, e.g. TC004 (must be reviewed)")
    parser.add_argument("--questions", type=int, default=5, help="questions per row (default 5)")
    parser.add_argument("--golden", type=Path, default=GOLDEN, help="the golden dataset workbook")
    parser.add_argument("--label", default="", help="added to the file name, e.g. trial")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

    cases = select_cases(read_sheet(args.golden, "Test cases"), args.cases)
    if not cases:
        print("No rows are marked both 'first run? = Y' and 'reviewed by you? = Y'.")
        return 1
    started = datetime.now()
    run_id = started.strftime("%Y-%m-%d_%H%M") + (f"_{args.label}" if args.label else "")
    out = RUNS / f"quiz_generator_run_{run_id}.xlsx"
    print(f"Running {len(cases)} row(s), {args.questions} questions each, answer checker on.")

    results: list[CaseResult] = []
    with tempfile.TemporaryDirectory(prefix="braintuitive-eval-", ignore_cleanup_errors=True) as tmp:
        engine = create_engine(f"sqlite:///{snapshot_database(Path(tmp)).as_posix()}")
        make_session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
        with make_session() as db:
            _user, topics = owner_topics(db, args.owner)
            namespace = next(iter(topics.values())).vector_namespace
            agent = QuizGeneratorAgent(verify=True, verbose=False)
            drafted = watch_drafts(agent)
            for number, case in enumerate(cases, start=1):
                result = run_case(agent, db, case, topics, args.questions, drafted)
                results.append(result)
                print(progress_line(number, len(cases), result), flush=True)
        engine.dispose()

    info = {
        "Run": run_id,
        "Date": started.strftime("%d %b %Y, %H:%M"),
        "Golden dataset": args.golden.name,
        "Rows run": ", ".join(r.case.get("case_id", "") for r in results),
        "Questions to grade": str(sum(len(r.questions) for r in results)),
        "Questions asked per row": str(args.questions),
        "AI model": settings.NEBIUS_MODEL,
        "Answer checker": "on: a second AI re-solves every question without seeing the answer",
        "Curriculum": f"{args.owner} (namespace {namespace})",
        "Database": "a temporary copy; the real database was not changed",
        "Time taken": f"{(datetime.now() - started).total_seconds() / 60:.1f} minutes",
    }
    sheets, chart_range, chart_anchor = build_workbook(results, info)
    if out.exists():
        raise FileExistsError(f"{out} already exists")
    out.parent.mkdir(parents=True, exist_ok=True)
    # Built and finished outside the project, then moved in. The project sits in
    # OneDrive, which can lock a brand-new file while it uploads; a hidden Excel
    # then waits forever on a "file in use" prompt nobody can see.
    with tempfile.TemporaryDirectory(prefix="braintuitive-eval-out-", ignore_cleanup_errors=True) as tmp:
        draft = Path(tmp) / out.name
        write_workbook(draft, sheets)
        print("Excel:", finish_in_excel(draft, chart_range, chart_anchor))
        shutil.move(str(draft), out)
    print(f"Wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
