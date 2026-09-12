"""Derive skill vocabularies and re-tag existing bank questions.

Questions generated before the controlled vocabulary existed carry free-form
tags -- 396 distinct labels across 683 questions, so per-skill accuracy was
always ``1/1``. This maps each one onto its sub-unit's vocabulary so the
existing bank becomes as useful as newly generated questions, without
regenerating anything.

Run:
    cd backend
    python utils/retag_bank.py --dry-run
    python utils/retag_bank.py
    python utils/retag_bank.py --unit 1 --refresh
"""

from __future__ import annotations

import argparse
import collections
import logging
import sys
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from config import configure_logging  # noqa: E402
from db.database import session_scope  # noqa: E402
from db.models import CurriculumSubUnit, CurriculumUnit, QuestionBankItem  # noqa: E402
from services.skill_taxonomy import (  # noqa: E402
    derive_vocabulary,
    snap_to_vocabulary,
    validate_vocabulary,
)

logger = logging.getLogger("retag_bank")


CLASSIFY_PROMPT = """Sub-unit {sub_unit_number}: {title}
LEARNING OBJECTIVE: {objective}

SKILLS -- choose exactly one per question, copied verbatim:
{vocabulary}

QUESTIONS:
{questions}

For each question, decide which single skill it primarily tests. Judge by what
the student actually has to DO, not by the surface wording.

Return ONLY a JSON object mapping the question number to the skill, e.g.
{{"1": "find_gcf", "2": "find_lcm"}}"""


def reclassify_sub_unit(
    db,  # noqa: ANN001 - Session
    sub_unit,  # noqa: ANN001 - CurriculumSubUnit
    vocabulary: list[str],
    llm,  # noqa: ANN001 - ChatOpenAI
    batch_size: int = 20,
) -> tuple[int, int]:
    """Re-derive each question's skill tag from its text.

    Snapping an old free-form tag onto a vocabulary only works when the old tag
    was descriptive. Classifying the question itself is both more accurate and
    independent of whatever the tag happened to say -- and it is the only way
    back once the original tags have been overwritten.

    Returns ``(changed, total)``.
    """
    import json as _json

    items = db.query(QuestionBankItem).filter_by(sub_unit_id=sub_unit.id).all()
    if not items or not vocabulary:
        return 0, len(items)

    listed = "\n".join(f"  - {tag}" for tag in vocabulary)
    changed = 0

    for start in range(0, len(items), batch_size):
        batch = items[start : start + batch_size]
        numbered = "\n".join(
            f"{index + 1}. {item.question_text[:220]}" for index, item in enumerate(batch)
        )
        prompt = CLASSIFY_PROMPT.format(
            sub_unit_number=sub_unit.sub_unit_number,
            title=sub_unit.title,
            objective=sub_unit.description or sub_unit.title,
            vocabulary=listed,
            questions=numbered,
        )
        try:
            response = llm.invoke([{"role": "user", "content": prompt}])
            content = getattr(response, "content", "") or ""
            first, last = content.find("{"), content.rfind("}")
            mapping = _json.loads(content[first : last + 1]) if first != -1 else {}
        except Exception as exc:
            logger.warning(
                "Classification failed for %s batch at %d (%s); leaving those tags.",
                sub_unit.sub_unit_number,
                start,
                exc,
            )
            continue

        for index, item in enumerate(batch, start=1):
            proposed = mapping.get(str(index)) or mapping.get(index)
            if not isinstance(proposed, str):
                continue
            # The model may still drift; force the answer into the vocabulary.
            snapped = snap_to_vocabulary(proposed, vocabulary)
            if snapped is None:
                continue
            if snapped != item.skill_tag:
                item.skill_tag = snapped
                changed += 1

        db.flush()

    return changed, len(items)


def _llm():  # noqa: ANN202 - ChatOpenAI, built lazily
    """The chat model, or None if it cannot be built."""
    try:
        from agents.quiz_generator import QuizGeneratorAgent

        return QuizGeneratorAgent(verbose=False).get_llm()
    except Exception as exc:
        logger.warning("No LLM available (%s); vocabularies will use the fallback.", exc)
        return None


def main(argv: list[str] | None = None) -> int:
    """Command-line entry point."""
    parser = argparse.ArgumentParser(description="Re-tag the question bank onto a vocabulary.")
    parser.add_argument("--unit", type=int, default=None, help="Only this unit")
    parser.add_argument("--subject", default="math")
    parser.add_argument("--grade", type=int, default=6)
    parser.add_argument(
        "--refresh", action="store_true", help="Re-derive vocabularies that already exist"
    )
    parser.add_argument("--dry-run", action="store_true", help="Report without writing")
    parser.add_argument(
        "--reclassify",
        action="store_true",
        help="Re-derive every tag from the question text (slower, far more accurate)",
    )
    args = parser.parse_args(argv)

    configure_logging()
    for noisy in ("httpx", "openai", "agents", "services.skill_taxonomy"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    llm = None if args.dry_run else _llm()

    with session_scope() as db:
        query = (
            db.query(CurriculumSubUnit)
            .join(CurriculumUnit)
            .filter(
                CurriculumUnit.subject == args.subject,
                CurriculumUnit.grade_level == args.grade,
                CurriculumSubUnit.is_active.is_(True),
            )
        )
        if args.unit is not None:
            query = query.filter(CurriculumUnit.unit_number == args.unit)
        sub_units = query.order_by(CurriculumUnit.unit_number, CurriculumSubUnit.sequence).all()

        if not sub_units:
            print("No sub-units found. Run the ingest pipeline first.")
            return 1

        print()
        print(f"{'sub-unit':10}{'vocabulary':56}{'questions':>10}{'retagged':>10}{'unmatched':>11}")
        print("-" * 97)

        total_questions = total_retagged = total_unmatched = 0
        before_tags: set[str] = set()
        after_tags: set[str] = set()

        for sub_unit in sub_units:
            existing = validate_vocabulary(sub_unit.skill_tags or [])
            if existing and not args.refresh:
                vocabulary = existing
            elif args.dry_run:
                vocabulary = existing or ["(would derive)"]
            else:
                vocabulary = derive_vocabulary(sub_unit, llm)
                sub_unit.skill_tags = vocabulary
                db.flush()

            items = db.query(QuestionBankItem).filter_by(sub_unit_id=sub_unit.id).all()
            retagged = unmatched = 0

            if args.reclassify and items and llm is not None and not args.dry_run:
                retagged, _ = reclassify_sub_unit(db, sub_unit, vocabulary, llm)
                for item in items:
                    after_tags.add(item.skill_tag or "")
                    before_tags.add(item.skill_tag or "")
                total_questions += len(items)
                total_retagged += retagged
                shown = ", ".join(vocabulary)[:54]
                print(
                    f"{sub_unit.sub_unit_number:10}{shown:56}{len(items):>10}"
                    f"{retagged:>10}{unmatched:>11}"
                )
                continue

            for item in items:
                before_tags.add(item.skill_tag or "")
                snapped = snap_to_vocabulary(item.skill_tag or "", vocabulary)
                if snapped is None:
                    # Nothing close enough to guess: park it on the first entry
                    # rather than leave a bucket of one behind.
                    snapped = vocabulary[0] if vocabulary else item.skill_tag
                    unmatched += 1
                if snapped != item.skill_tag:
                    if not args.dry_run:
                        item.skill_tag = snapped
                    retagged += 1
                after_tags.add(snapped or "")

            total_questions += len(items)
            total_retagged += retagged
            total_unmatched += unmatched

            shown = ", ".join(vocabulary)[:54]
            print(
                f"{sub_unit.sub_unit_number:10}{shown:56}{len(items):>10}"
                f"{retagged:>10}{unmatched:>11}"
            )

        print("-" * 97)
        print(f"{'TOTAL':10}{'':56}{total_questions:>10}{total_retagged:>10}{total_unmatched:>11}")
        print()
        print(f"distinct tags before : {len(before_tags)}")
        print(f"distinct tags after  : {len(after_tags)}")
        if after_tags:
            print(f"questions per tag    : {total_questions / len(after_tags):.1f}")

        if args.dry_run:
            print("\n(dry run - nothing written)")
            db.rollback()
            return 0

        # Distribution check: the point is buckets big enough to mean something.
        counts = collections.Counter(
            row[0]
            for row in db.query(QuestionBankItem.skill_tag).filter(
                QuestionBankItem.sub_unit_id.in_([s.id for s in sub_units])
            )
        )
        thin = [tag for tag, n in counts.items() if n < 4]
        if thin:
            print(f"\nwarning: {len(thin)} tag(s) still hold fewer than 4 questions")

    return 0


if __name__ == "__main__":  # pragma: no cover - CLI
    sys.exit(main())
