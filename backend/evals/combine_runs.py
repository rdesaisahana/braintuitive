"""Copy a run's sheets into a workbook that already holds earlier runs.

Usage, from the backend folder:

    ../.venv/Scripts/python.exe -m evals.combine_runs \\
        --master ../evals/runs/quiz_generator_run_2026-09-11_1528.xlsx \\
        --run    ../evals/runs/quiz_generator_run_<new>.xlsx --suffix 2

Each run keeps its own file; this makes a working copy that holds them all
side by side. Excel does the copying, so formulas, dropdowns, highlighting and
the chart survive, and the copied sheets point at each other rather than at
run 1. It then builds a Comparison sheet that scores every run in the workbook
next to the others, filling in as you grade.

The master workbook must be closed in Excel while this runs.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

from evals.run_quiz_generator_eval import CASE_COLUMNS, GRADES, RESULT_COLUMNS, _letters

COPY_SCRIPT = Path(__file__).with_name("copy_sheets.ps1")
SHEETS = ("Summary", "Results", "Cases")
PERCENT, NUMBER, PLAIN = "0.0%", "0.0", None


def _cell(text: str | None = None, formula: str | None = None,
          fmt: str | None = None, bold: bool = False) -> dict[str, object]:
    return {"text": text, "formula": formula, "format": fmt, "bold": bold}


def comparison_rows(runs: list[tuple[str, str, str]]) -> list[list[dict[str, object]]]:
    """The Comparison sheet: one row per metric, one column per run.

    ``runs`` is (label, results sheet name, cases sheet name) per run, oldest
    first. Every number is a formula over that run's own sheets, so the sheet
    updates itself as more questions are graded.
    """
    R, C = _letters(RESULT_COLUMNS), _letters(CASE_COLUMNS)

    def graded(sheet: str, name: str) -> str:
        return f"'{sheet}'!${R[name]}$2:${R[name]}$2000"

    def cases(sheet: str, name: str) -> str:
        return f"'{sheet}'!${C[name]}$2:${C[name]}$500"

    def score(sheet: str, name: str) -> str:
        rng = graded(sheet, name)
        return f'IFERROR(COUNTIF({rng},"Y")/(COUNTIF({rng},"Y")+COUNTIF({rng},"N")),"")'

    metrics: list[tuple[str, str, object, str]] = []
    for name, target in GRADES:
        metrics.append((name, "grade", target, "higher"))
    metrics += [
        ("Completion", "auto", 1.0, "higher"),
        ("Format pass rate", "auto", 0.9, "higher"),
        ("Checker agreement", "auto", 0.9, "higher"),
        ("True/false share", "auto", 0.3, "lower"),
        ("Worst letter balance", "auto", 0.6, "lower"),
        ("Average attempts", "auto", 3, "lower"),
        ("Average seconds per row", "auto", None, "lower"),
    ]

    def auto_formula(sheet_cases: str, metric: str) -> tuple[str, str]:
        kind = cases(sheet_cases, "kind")
        if metric == "Completion":
            return (f'IFERROR(COUNTIFS({kind},"normal",{cases(sheet_cases, "complete?")},"Y")'
                    f'/COUNTIF({kind},"normal"),"")', PERCENT)
        if metric == "Format pass rate":
            return (f'IFERROR(SUM({cases(sheet_cases, "passed format check")})'
                    f'/SUM({cases(sheet_cases, "drafted by AI")}),"")', PERCENT)
        if metric == "Checker agreement":
            return (f'IFERROR(SUM({cases(sheet_cases, "checker agreed")})'
                    f'/SUM({cases(sheet_cases, "checked by checker")}),"")', PERCENT)
        if metric == "True/false share":
            return (f'IFERROR(SUM({cases(sheet_cases, "true/false")})'
                    f'/SUM({cases(sheet_cases, "returned")}),"")', PERCENT)
        if metric == "Worst letter balance":
            rng = cases(sheet_cases, "letter balance")
            return f'IF(COUNT({rng})=0,"",MAX({rng}))', PERCENT
        if metric == "Average attempts":
            return f'IFERROR(AVERAGEIF({kind},"normal",{cases(sheet_cases, "attempts")}),"")', NUMBER
        return f'IFERROR(AVERAGEIF({kind},"normal",{cases(sheet_cases, "seconds")}),"")', NUMBER

    header = [_cell("Metric", bold=True)]
    for label, _results, _cases in runs:
        header.append(_cell(label, bold=True))
    header += [_cell("Change", bold=True), _cell("Target", bold=True),
               _cell("Better when", bold=True)]
    rows: list[list[dict[str, object]]] = [header]

    first, last = chr(66), chr(66 + len(runs) - 1)  # column B .. the last run's column

    def section(title: str) -> None:
        rows.append([_cell(title, bold=True)])

    section("Your grades (fill in each run's Results sheet)")
    for name, kind, target, better in metrics:
        if kind != "grade":
            continue
        row = [_cell(name)]
        for _label, results_sheet, _cases_sheet in runs:
            row.append(_cell(formula=score(results_sheet, name), fmt=PERCENT))
        line = len(rows) + 1
        row += [
            _cell(formula=f'IF(OR({first}{line}="",{last}{line}=""),"",{last}{line}-{first}{line})', fmt=PERCENT),
            _cell(formula=str(target), fmt=PERCENT),
            _cell(better),
        ]
        rows.append(row)

    section("Automatic numbers")
    for name, kind, target, better in metrics:
        if kind != "auto":
            continue
        row = [_cell(name)]
        fmt = PERCENT
        for _label, _results_sheet, cases_sheet in runs:
            formula, fmt = auto_formula(cases_sheet, name)
            row.append(_cell(formula=formula, fmt=fmt))
        line = len(rows) + 1
        row += [
            _cell(formula=f'IF(OR({first}{line}="",{last}{line}=""),"",{last}{line}-{first}{line})', fmt=fmt),
            _cell(formula=str(target), fmt=fmt) if target is not None else _cell("none"),
            _cell(better),
        ]
        rows.append(row)

    section("Counts")
    for name, formula_for in (
        ("Questions", lambda sheet: f'COUNTA({graded(sheet, "case_id")})'),
        ("Questions graded", lambda sheet:
            f'COUNTIF({graded(sheet, "Correct key")},"Y")+COUNTIF({graded(sheet, "Correct key")},"N")'),
        ("Wrong keys the checker missed", lambda sheet:
            f'COUNTIFS({graded(sheet, "Correct key")},"N",{graded(sheet, "checker verdict")},"agreed")'),
    ):
        row = [_cell(name)]
        for _label, results_sheet, _cases_sheet in runs:
            row.append(_cell(formula=formula_for(results_sheet), fmt=PLAIN))
        line = len(rows) + 1
        row += [_cell(formula=f'{last}{line}-{first}{line}'), _cell(), _cell()]
        rows.append(row)

    rows.append([_cell()])
    rows.append([_cell("A change is only meaningful if the golden dataset stayed the same "
                       "between runs. Watch the checks you did not try to improve: those show "
                       "whether a change broke something else.")])
    return rows


def combine(master: Path, run: Path, suffix: str, runs: list[tuple[str, str, str]]) -> str:
    """Copy ``run``'s sheets into ``master`` and rebuild the Comparison sheet."""
    if not COPY_SCRIPT.exists():
        raise SystemExit(f"missing {COPY_SCRIPT}")
    if sys.platform != "win32":
        raise SystemExit("combining runs needs Excel, so it runs on Windows only")
    payload = {
        "sheets": list(SHEETS),
        "widths": [34, *[14] * len(runs), 12, 10, 12],
        "rows": comparison_rows(runs),
    }
    with tempfile.TemporaryDirectory(prefix="braintuitive-compare-") as tmp:
        plan = Path(tmp) / "comparison.json"
        plan.write_text(json.dumps(payload), encoding="utf-8")
        done = subprocess.run(
            ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(COPY_SCRIPT),
             "-Master", str(master.resolve()), "-RunFile", str(run.resolve()),
             "-Suffix", suffix, "-ComparisonJson", str(plan)],
            capture_output=True, text=True, timeout=300, check=False,
        )
    # Excel edits the master in place, so a half-done copy must be loud, not a
    # success message with an error buried in it.
    if done.returncode != 0 or done.stderr.strip():
        raise SystemExit(f"Combining failed; the workbook was not changed.\n{done.stdout}\n{done.stderr}".strip())
    return done.stdout.strip() or "done"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Copy a run's sheets into the combined workbook.")
    parser.add_argument("--master", required=True, type=Path, help="workbook to copy into (close it in Excel first)")
    parser.add_argument("--run", required=True, type=Path, help="the new run's workbook")
    parser.add_argument("--suffix", default="2", help="suffix for the copied sheets, e.g. 2 -> 'Results 2'")
    parser.add_argument("--labels", nargs="*", help="names for the runs on the Comparison sheet")
    args = parser.parse_args(argv)

    runs = [("Run 1", "Results", "Cases"),
            (f"Run {args.suffix}", f"Results {args.suffix}", f"Cases {args.suffix}")]
    if args.labels and len(args.labels) == len(runs):
        runs = [(label, results, cases) for label, (_old, results, cases) in zip(args.labels, runs, strict=True)]
    print(combine(args.master, args.run, args.suffix, runs))
    print(f"Combined workbook: {args.master}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
