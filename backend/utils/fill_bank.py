"""Fill the question bank for a range of units.

The APScheduler job tops the bank up gradually in budgeted runs. This is the
manual equivalent for when you want a unit stocked *now* -- before a pilot,
after ingesting a new curriculum, or to reach steady state on a fresh install.

Run:
    cd backend
    python utils/fill_bank.py --unit 1
    python utils/fill_bank.py --unit 1 --depth 30 --workers 3
    python utils/fill_bank.py --units 1-3 --dry-run

Slots are filled concurrently. Each worker opens its own SQLAlchemy session
and its own agent, because neither is safe to share across threads; SQLite is
in WAL mode with a 30s busy timeout, which handles the concurrent writes.
"""

from __future__ import annotations

import argparse
import logging
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

# Put backend/ on sys.path so this runs from any working directory.
_BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from config import configure_logging  # noqa: E402
from db.database import session_scope  # noqa: E402
from db.models import CurriculumSubUnit, CurriculumUnit, DifficultyLevel  # noqa: E402
from services.question_bank import (  # noqa: E402
    DEFAULT_BANK_DEPTH,
    FillReport,
    fill_slot,
    slot_status,
)

logger = logging.getLogger("fill_bank")

_print_lock = threading.Lock()


def _say(message: str) -> None:
    """Print without threads interleaving mid-line."""
    with _print_lock:
        print(message, flush=True)


def _parse_units(raw: str) -> list[int]:
    """Parse ``"1"`` or ``"1-3"`` or ``"1,3,5"`` into unit numbers."""
    units: set[int] = set()
    for part in raw.split(","):
        part = part.strip()
        if "-" in part:
            start, end = part.split("-", 1)
            units.update(range(int(start), int(end) + 1))
        elif part:
            units.add(int(part))
    return sorted(units)


def _fill_one(
    sub_unit_id: str,
    label: str,
    difficulty: DifficultyLevel,
    target: int,
    index: int,
    total: int,
) -> FillReport:
    """Fill one slot in its own session, with its own agent."""
    from agents.quiz_generator import QuizGeneratorAgent

    started = time.perf_counter()
    report = FillReport()
    try:
        with session_scope() as db:
            sub_unit = db.get(CurriculumSubUnit, sub_unit_id)
            if sub_unit is None:
                report.errors.append(f"{label}: sub-unit vanished")
                return report

            before = slot_status(db, sub_unit, difficulty, target).servable
            if before >= target:
                _say(f"[{index}/{total}] {label}/{difficulty.value:12} already stocked ({before})")
                return report

            # Verification is always on for the bank: a bad question here
            # reaches every student who draws it.
            fill_slot(
                db,
                sub_unit=sub_unit,
                difficulty=difficulty,
                target=target,
                agent=QuizGeneratorAgent(verify=True, verbose=False),
                max_batches=8,
                report=report,
            )
            after = slot_status(db, sub_unit, difficulty, target).servable

        elapsed = time.perf_counter() - started
        _say(
            f"[{index}/{total}] {label}/{difficulty.value:12} "
            f"{before:2} -> {after:2}/{target}  (+{report.questions_added} in {elapsed:.0f}s"
            + (f", {report.quarantined} quarantined" if report.quarantined else "")
            + ")"
        )
    except Exception as exc:
        report.errors.append(f"{label}/{difficulty.value}: {exc}")
        _say(f"[{index}/{total}] {label}/{difficulty.value:12} FAILED: {str(exc)[:90]}")

    return report


def main(argv: list[str] | None = None) -> int:
    """Command-line entry point."""
    parser = argparse.ArgumentParser(description="Fill the question bank for whole units.")
    parser.add_argument("--unit", type=int, help="A single unit number to fill")
    parser.add_argument("--units", type=str, help='A range or list, e.g. "1-3" or "1,4"')
    parser.add_argument("--subject", default="math")
    parser.add_argument("--grade", type=int, default=6)
    parser.add_argument("--depth", type=int, default=DEFAULT_BANK_DEPTH, help="Questions per slot")
    parser.add_argument("--workers", type=int, default=3, help="Slots filled concurrently")
    parser.add_argument(
        "--dry-run", action="store_true", help="Show what is short without generating"
    )
    args = parser.parse_args(argv)

    configure_logging()
    for noisy in (
        "agents.quiz_generator",
        "agents.question_verifier",
        "pinecone_plugin_interface.logging",
        "rag",
        "services",
    ):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    if args.unit is not None:
        units = [args.unit]
    elif args.units:
        units = _parse_units(args.units)
    else:
        parser.error("give --unit or --units")

    # Collect the work list up front so no session is held during generation.
    with session_scope() as db:
        rows = (
            db.query(CurriculumSubUnit)
            .join(CurriculumUnit)
            .filter(
                CurriculumUnit.subject == args.subject,
                CurriculumUnit.grade_level == args.grade,
                CurriculumUnit.unit_number.in_(units),
                CurriculumSubUnit.is_active.is_(True),
                CurriculumSubUnit.is_indexed.is_(True),
            )
            .order_by(CurriculumUnit.unit_number, CurriculumSubUnit.sequence)
            .all()
        )
        work = [
            (
                sub_unit.id,
                sub_unit.sub_unit_number,
                difficulty,
                slot_status(db, sub_unit, difficulty, args.depth),
            )
            for sub_unit in rows
            for difficulty in DifficultyLevel
        ]

    if not work:
        print(f"No indexed sub-units found in unit(s) {units}. Run the ingest pipeline first.")
        return 1

    short = [entry for entry in work if not entry[3].is_stocked]
    outstanding = sum(entry[3].shortfall for entry in short)

    print()
    print(f"Unit(s) {units}: {len(work)} slot(s), {len(short)} short, target depth {args.depth}")
    print(f"Questions to generate: ~{outstanding}")
    if args.dry_run:
        print("-" * 74)
        print(f"  {'':4}{'slot':24}{'servable':>10}{'quarantined':>14}")
        for _, label, difficulty, status in work:
            flag = "OK " if status.is_stocked else "GAP"
            slot = f"{label}/{difficulty.value}"
            print(
                f"  {flag} {slot:24}{status.servable:>4}/{args.depth:<5}"
                f"{status.quarantined:>14}"
            )
        print("-" * 74)
        servable = sum(entry[3].servable for entry in work)
        quarantined = sum(entry[3].quarantined for entry in work)
        stocked = sum(1 for entry in work if entry[3].is_stocked)
        print(
            f"  {stocked}/{len(work)} slots stocked | "
            f"{servable} servable | {quarantined} quarantined for review"
        )
        return 0

    print(f"Workers: {args.workers}")
    print("-" * 74)

    started = time.perf_counter()
    total = FillReport()

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [
            pool.submit(_fill_one, sub_unit_id, label, difficulty, args.depth, index, len(short))
            for index, (sub_unit_id, label, difficulty, _status) in enumerate(short, start=1)
        ]
        for future in as_completed(futures):
            part = future.result()
            total.slots_examined += part.slots_examined
            total.slots_filled += part.slots_filled
            total.questions_added += part.questions_added
            total.quarantined += part.quarantined
            total.duplicates_skipped += part.duplicates_skipped
            total.errors.extend(part.errors)

    elapsed = time.perf_counter() - started
    print("-" * 74)
    print(total.summary())
    print(f"elapsed: {elapsed / 60:.1f} min")

    with session_scope() as db:
        rows = (
            db.query(CurriculumSubUnit)
            .join(CurriculumUnit)
            .filter(
                CurriculumUnit.subject == args.subject,
                CurriculumUnit.grade_level == args.grade,
                CurriculumUnit.unit_number.in_(units),
            )
            .all()
        )
        remaining = sum(
            slot_status(db, sub_unit, difficulty, args.depth).shortfall
            for sub_unit in rows
            for difficulty in DifficultyLevel
        )
    print(f"remaining shortfall: {remaining}")

    for error in total.errors:
        print(f"  error: {error}")
    return 0 if not total.errors else 1


if __name__ == "__main__":  # pragma: no cover - CLI
    sys.exit(main())
