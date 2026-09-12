"""Curriculum PDF parsing.

Turns a curriculum PDF into the structure the rest of Braintuitive expects:
units, sub-units and the prose sections that quiz questions get grounded in.

The shape this targets is the New Jersey district curriculum-guide format
(verified against *06 Pre-Algebra H&A.pdf*, WW-P Regional School District):

    Unit 1: Number Fluency          <- unit header, starts a page
      Content Area / Course & Grade Level
      Summary and Rationale         <- prose
      Recommended Pacing            <- "48 days"
      New Jersey Student Learning Standards
        CPI # ...                   <- standards tables
      Instructional Focus
        Unit Enduring Understandings
        Unit Essential Questions
        Content Questions
      Objectives                    <- the sub-unit source
        We are learning to/that:
          * Identify, describe, and find the absolute values of integers.
          * Fluently add, subtract, multiply and divide integers.
      Evidence of Learning
      Resources

Sub-units are derived from the **Objectives** bullets. This document (like
most district guides) numbers units but not sub-units, so ``1.1`` .. ``1.7``
are assigned from the objective order. Each objective is one teachable skill,
which is exactly the granularity a quiz is written against.
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Patterns
# --------------------------------------------------------------------------- #

# "Unit 3: Proportional Relationships" at the head of a line.
UNIT_HEADER_RE = re.compile(r"^\s*Unit\s+(\d+)\s*:\s*(.+?)\s*$", re.MULTILINE)

# The objectives block, ending at the next major section or unit.
OBJECTIVES_RE = re.compile(
    r"Objectives\s*\n\s*We are learning to/that:\s*\n(.*?)"
    r"(?=\nEvidence of Learning|\nUnit\s+\d+\s*:|\Z)",
    re.DOTALL | re.IGNORECASE,
)

# Bullets in this document are U+25CF; tolerate the usual substitutes.
BULLET_SPLIT_RE = re.compile(r"\n?\s*[●•▪·\-\*]\s+")

SUMMARY_RE = re.compile(
    r"Summary and Rationale\s*\n(.*?)(?=\nRecommended Pacing|\nNew Jersey|\Z)",
    re.DOTALL | re.IGNORECASE,
)
PACING_RE = re.compile(r"Recommended Pacing\s*\n\s*(\d+)\s*days", re.IGNORECASE)
ESSENTIAL_Q_RE = re.compile(
    r"(?:Unit Essential Questions|Content Questions)\s*\n(.*?)"
    r"(?=\nObjectives|\nUnit Enduring|\nEvidence of Learning|\Z)",
    re.DOTALL | re.IGNORECASE,
)
ENDURING_RE = re.compile(
    r"Unit Enduring Understandings\s*\n(.*?)"
    r"(?=\nUnit Essential Questions|\nContent Questions|\nObjectives|\Z)",
    re.DOTALL | re.IGNORECASE,
)
# NJSLS codes such as 6.RP.A.1, 7.NS.A.2.b, 8.EE.B.5
STANDARD_CODE_RE = re.compile(r"\b(\d\.[A-Z]{2,3}\.[A-Z](?:\.\d+)?(?:\.[a-z])?)\b")

GRADE_RE = re.compile(r"Grade\s+(\d+)", re.IGNORECASE)


# --------------------------------------------------------------------------- #
# Data structures
# --------------------------------------------------------------------------- #


@dataclass
class ParsedSubUnit:
    """One objective, promoted to a quizzable sub-unit."""

    sub_unit_number: str  # "1.2"
    sequence: int  # 0-based order within the unit
    title: str  # short label derived from the objective
    description: str  # the full objective text
    learning_objectives: list[str] = field(default_factory=list)
    skill_tags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "sub_unit_number": self.sub_unit_number,
            "sequence": self.sequence,
            "title": self.title,
            "description": self.description,
            "learning_objectives": self.learning_objectives,
            "skill_tags": self.skill_tags,
        }


@dataclass
class ParsedUnit:
    """A curriculum unit with its prose, standards and sub-units."""

    unit_number: int
    title: str
    description: str
    page_start: int
    page_end: int
    text: str  # full text of the unit's pages
    pacing_days: int | None = None
    essential_questions: list[str] = field(default_factory=list)
    enduring_understandings: list[str] = field(default_factory=list)
    standards: list[str] = field(default_factory=list)
    sub_units: list[ParsedSubUnit] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "unit_number": self.unit_number,
            "title": self.title,
            "description": self.description,
            "page_start": self.page_start,
            "page_end": self.page_end,
            "pacing_days": self.pacing_days,
            "essential_questions": self.essential_questions,
            "enduring_understandings": self.enduring_understandings,
            "standards": self.standards,
            "sub_units": [s.to_dict() for s in self.sub_units],
        }


@dataclass
class ParsedDocument:
    """The whole parsed curriculum document."""

    filename: str
    content_hash: str
    title: str
    subject: str
    grade_level: int
    page_count: int
    pages: list[str]
    units: list[ParsedUnit] = field(default_factory=list)

    @property
    def total_sub_units(self) -> int:
        return sum(len(u.sub_units) for u in self.units)

    def to_dict(self) -> dict[str, Any]:
        """Metadata only -- page text is excluded to keep documents small."""
        return {
            "filename": self.filename,
            "content_hash": self.content_hash,
            "title": self.title,
            "subject": self.subject,
            "grade_level": self.grade_level,
            "page_count": self.page_count,
            "total_units": len(self.units),
            "total_sub_units": self.total_sub_units,
            "units": [u.to_dict() for u in self.units],
        }


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _clean(text: str) -> str:
    """Collapse whitespace and strip PDF artefacts from a fragment."""
    text = text.replace("­", "")  # soft hyphen
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _split_bullets(block: str) -> list[str]:
    """Split a bullet block into cleaned, de-duplicated items."""
    items: list[str] = []
    for raw in BULLET_SPLIT_RE.split(block):
        item = _clean(raw)
        # Drop stubs and stray headers that survive the split.
        if len(item) < 8:
            continue
        if item.lower().startswith(("we are learning", "content questions")):
            continue
        if item not in items:
            items.append(item)
    return items


def _title_from_objective(objective: str) -> str:
    """Derive a short sub-unit label from a full objective sentence.

    "Fluently add, subtract, multiply and divide integers." -> "Fluently add,
    subtract, multiply and divide integers"

    Parenthetical asides and trailing examples are dropped so the label stays
    usable in a progress UI.
    """
    title = re.sub(r"\s*\([^)]*\)", "", objective)  # drop "(Suggested ...)"
    title = title.split(" e.g.")[0].split(" including")[0]
    title = title.strip(" .;:")
    if len(title) > 90:
        title = title[:87].rsplit(" ", 1)[0] + "..."
    return title


_STOPWORDS = {
    "the",
    "and",
    "with",
    "that",
    "from",
    "for",
    "using",
    "use",
    "a",
    "an",
    "of",
    "to",
    "in",
    "on",
    "or",
    "their",
    "these",
    "including",
    "include",
    "given",
    "various",
    "variety",
    "strategies",
    "we",
    "are",
    "learning",
    "identify",
    "describe",
    "find",
    "apply",
    "solve",
    "write",
    "graph",
    "calculate",
    "determine",
    "represent",
    "compare",
    "select",
    "recognize",
}


def _skill_tags(objective: str, limit: int = 5) -> list[str]:
    """Extract content keywords from an objective, for gap detection later."""
    words = re.findall(r"[a-z][a-z\-]{3,}", objective.lower())
    tags: list[str] = []
    for word in words:
        if word in _STOPWORDS or word in tags:
            continue
        tags.append(word)
        if len(tags) >= limit:
            break
    return tags


# --------------------------------------------------------------------------- #
# Parser
# --------------------------------------------------------------------------- #


class CurriculumPDFParser:
    """Extracts units and sub-units from a curriculum PDF.

    Uses ``pdfplumber`` for layout-aware extraction, falling back to ``pypdf``
    if pdfplumber is unavailable or errors on a page.

    Example:
        parser = CurriculumPDFParser()
        doc = parser.parse("06 Pre-Algebra H&A.pdf", subject="math", grade_level=6)
        print(doc.total_sub_units)
    """

    def __init__(self, min_unit_text_length: int = 200) -> None:
        self.min_unit_text_length = min_unit_text_length

    # -- extraction ------------------------------------------------------ #

    def extract_pages(self, pdf_path: str | Path) -> list[str]:
        """Return the text of every page, one string per page.

        Raises:
            FileNotFoundError: If the PDF does not exist.
            RuntimeError: If no text could be extracted at all (likely a scan
                needing OCR).
        """
        path = Path(pdf_path)
        if not path.is_file():
            raise FileNotFoundError(f"PDF not found: {path}")

        pages = self._extract_with_pdfplumber(path)
        if not any(p.strip() for p in pages):
            logger.warning("pdfplumber found no text in %s; trying pypdf.", path.name)
            pages = self._extract_with_pypdf(path)

        if not any(p.strip() for p in pages):
            raise RuntimeError(
                f"No extractable text in {path.name}. It is probably a scanned "
                "document and needs OCR before it can be indexed."
            )
        return pages

    @staticmethod
    def _extract_with_pdfplumber(path: Path) -> list[str]:
        try:
            import pdfplumber
        except ImportError:  # pragma: no cover - dependency is pinned
            return []

        pages: list[str] = []
        with pdfplumber.open(str(path)) as pdf:
            for number, page in enumerate(pdf.pages, start=1):
                try:
                    pages.append(page.extract_text() or "")
                except Exception as exc:  # pragma: no cover - malformed page
                    logger.warning("pdfplumber failed on page %d: %s", number, exc)
                    pages.append("")
        return pages

    @staticmethod
    def _extract_with_pypdf(path: Path) -> list[str]:
        try:
            from pypdf import PdfReader
        except ImportError:  # pragma: no cover - dependency is pinned
            return []

        reader = PdfReader(str(path))
        pages: list[str] = []
        for number, page in enumerate(reader.pages, start=1):
            try:
                pages.append(page.extract_text() or "")
            except Exception as exc:  # pragma: no cover - malformed page
                logger.warning("pypdf failed on page %d: %s", number, exc)
                pages.append("")
        return pages

    # -- structure ------------------------------------------------------- #

    def find_unit_boundaries(self, pages: list[str]) -> list[tuple[int, int, str, int]]:
        """Locate unit headers and the page span each unit covers.

        A header only counts when it is the first non-blank line of a page --
        the overview table on the contents page lists "Unit 1 Unit 2 Unit 3..."
        inline, and matching that would produce phantom units.

        Returns:
            ``(unit_number, title, page_start, page_end)`` tuples, 1-indexed.
        """
        starts: list[tuple[int, str, int]] = []
        for index, text in enumerate(pages):
            lines = [line for line in text.split("\n") if line.strip()]
            if not lines:
                continue
            match = UNIT_HEADER_RE.match(lines[0])
            if match:
                starts.append((int(match.group(1)), _clean(match.group(2)), index + 1))

        boundaries: list[tuple[int, int, str, int]] = []
        for position, (number, title, page_start) in enumerate(starts):
            page_end = starts[position + 1][2] - 1 if position + 1 < len(starts) else len(pages)
            boundaries.append((number, title, page_start, page_end))

        logger.info("Detected %d unit(s): %s", len(boundaries), [b[0] for b in boundaries])
        return boundaries

    def parse_sub_units(self, unit_number: int, unit_text: str) -> list[ParsedSubUnit]:
        """Derive sub-units from the unit's Objectives bullets."""
        match = OBJECTIVES_RE.search(unit_text)
        if not match:
            logger.warning("Unit %d: no Objectives block found.", unit_number)
            return []

        objectives = _split_bullets(match.group(1))
        sub_units: list[ParsedSubUnit] = []
        for index, objective in enumerate(objectives):
            sub_units.append(
                ParsedSubUnit(
                    sub_unit_number=f"{unit_number}.{index + 1}",
                    sequence=index,
                    title=_title_from_objective(objective),
                    description=objective,
                    learning_objectives=[objective],
                    skill_tags=_skill_tags(objective),
                )
            )

        logger.info("Unit %d: %d sub-unit(s) from objectives.", unit_number, len(sub_units))
        return sub_units

    def parse(
        self,
        pdf_path: str | Path,
        subject: str = "math",
        grade_level: int | None = None,
    ) -> ParsedDocument:
        """Parse a curriculum PDF end to end.

        Args:
            pdf_path: Path to the PDF.
            subject: Subject label stored on every unit.
            grade_level: Grade. Inferred from the cover page when omitted.

        Returns:
            A fully populated :class:`ParsedDocument`.
        """
        path = Path(pdf_path)
        pages = self.extract_pages(path)
        raw = "\n".join(pages)

        content_hash = hashlib.sha256(raw.encode("utf-8", errors="ignore")).hexdigest()

        # Cover page carries the document title and grade.
        cover_lines = [line for line in pages[0].split("\n") if line.strip()] if pages else []
        title = _clean(cover_lines[1]) if len(cover_lines) > 1 else path.stem
        if grade_level is None:
            grade_match = GRADE_RE.search(pages[0] if pages else "")
            grade_level = int(grade_match.group(1)) if grade_match else 0
            if not grade_level:
                logger.warning("Could not infer grade level from %s; defaulting to 0.", path.name)

        document = ParsedDocument(
            filename=path.name,
            content_hash=content_hash,
            title=title,
            subject=subject,
            grade_level=grade_level,
            page_count=len(pages),
            pages=pages,
        )

        for number, unit_title, page_start, page_end in self.find_unit_boundaries(pages):
            unit_text = "\n".join(pages[page_start - 1 : page_end])
            if len(unit_text) < self.min_unit_text_length:
                logger.warning("Unit %d is suspiciously short; skipping.", number)
                continue

            summary_match = SUMMARY_RE.search(unit_text)
            pacing_match = PACING_RE.search(unit_text)
            essential_match = ESSENTIAL_Q_RE.search(unit_text)
            enduring_match = ENDURING_RE.search(unit_text)

            unit = ParsedUnit(
                unit_number=number,
                title=unit_title,
                description=_clean(summary_match.group(1)) if summary_match else "",
                page_start=page_start,
                page_end=page_end,
                text=unit_text,
                pacing_days=int(pacing_match.group(1)) if pacing_match else None,
                essential_questions=(
                    _split_bullets(essential_match.group(1)) if essential_match else []
                ),
                enduring_understandings=(
                    _split_bullets(enduring_match.group(1)) if enduring_match else []
                ),
                standards=sorted(set(STANDARD_CODE_RE.findall(unit_text))),
                sub_units=self.parse_sub_units(number, unit_text),
            )
            document.units.append(unit)

        logger.info(
            "Parsed %s: %d page(s), %d unit(s), %d sub-unit(s).",
            path.name,
            document.page_count,
            len(document.units),
            document.total_sub_units,
        )
        return document


__all__ = [
    "CurriculumPDFParser",
    "ParsedDocument",
    "ParsedSubUnit",
    "ParsedUnit",
]
