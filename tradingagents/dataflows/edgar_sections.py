"""Deterministic section extraction from SEC filing text.

Local-model research reads filings section-by-section ("tool-based bounded
reading" — spec decision 3): the evidence stage gets one bounded section per
LLM call instead of a stuffed context. Extraction must therefore be
deterministic and cheap — a regex over the Item-heading taxonomy, never an
LLM. The TOC-vs-body ambiguity is resolved by span length: candidate spans
run from a start-heading match to the next any-Item heading, and the body
occurrence is the longest span (TOC entries collide with their neighbors).
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass

# form family -> section key -> Item number.
# 10-Q MD&A is Part I Item 2; the longest-span rule keeps Part II items from
# winning because they carry little text.
SECTION_ITEMS: dict[str, dict[str, str]] = {
    "10-K": {"business": "1", "risk_factors": "1A", "mdna": "7"},
    "10-Q": {"mdna": "2", "risk_factors": "1A"},
}

_ANY_ITEM = re.compile(r"^\s*item\s+\d+[a-z]?\.?\b", re.IGNORECASE | re.MULTILINE)


class SectionNotFound(ValueError):
    """The requested section key is unknown or absent from the document."""


@dataclass(frozen=True)
class FilingSection:
    ticker: str
    accession: str
    section: str
    form: str
    text: str

    @property
    def source_ref(self) -> str:
        return f"{self.accession}:{self.section}"


@dataclass(frozen=True)
class SectionDiff:
    ticker: str
    section: str
    accession_new: str
    accession_old: str
    text: str

    @property
    def source_ref(self) -> str:
        return f"{self.accession_new}+{self.accession_old}:{self.section}_diff"


def _truncate(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + f"\n[truncated at {max_chars} characters]"


def _form_family(form: str) -> str | None:
    for family in SECTION_ITEMS:
        if form.upper().startswith(family):
            return family
    return None


def extract_section(
    text: str, *, form: str, section: str, max_chars: int = 12000,
) -> str:
    """Extract one canonical section from flattened filing text, bounded."""
    if section == "full":
        return _truncate(text, max_chars)
    family = _form_family(form)
    items = SECTION_ITEMS.get(family or "", {})
    item = items.get(section)
    if item is None:
        raise SectionNotFound(
            f"section {section!r} not defined for form {form!r} "
            f"(known: {sorted(items) + ['full']})"
        )
    start_re = re.compile(
        rf"^\s*item\s+{re.escape(item)}\.?\b", re.IGNORECASE | re.MULTILINE,
    )
    best: str | None = None
    for m in start_re.finditer(text):
        nxt = _ANY_ITEM.search(text, m.end())
        span = text[m.end(): nxt.start()] if nxt else text[m.end():]
        if best is None or len(span) > len(best):
            best = span
    if best is None or not best.strip():
        raise SectionNotFound(f"Item {item} ({section}) not found in this {form}")
    return _truncate(best.strip(), max_chars)


def read_filing_section(
    ticker: str,
    accession: str,
    section: str,
    *,
    max_chars: int = 12000,
    list_filings=None,
    fetch_text=None,
) -> FilingSection:
    """Resolve an accession for ``ticker`` and extract one section from it."""
    from tradingagents.dataflows import edgar

    list_filings = list_filings or edgar.list_filings
    fetch_text = fetch_text or edgar.fetch_filing_text
    filings = list_filings(ticker, limit=200)
    filing = next((f for f in filings if f.accession_number == accession), None)
    if filing is None:
        raise KeyError(f"no filing with accession {accession!r} for {ticker!r}")
    text = extract_section(
        fetch_text(filing), form=filing.form, section=section, max_chars=max_chars,
    )
    return FilingSection(
        ticker=ticker.upper(), accession=accession, section=section,
        form=filing.form, text=text,
    )


def _fiscal_year(filing) -> int:
    when = filing.report_date or filing.filing_date
    return when.year


def diff_filing_sections(
    ticker: str,
    section: str,
    year_a: int,
    year_b: int,
    *,
    max_chars: int = 12000,
    list_filings=None,
    fetch_text=None,
) -> SectionDiff:
    """Unified diff of one section between two fiscal years' 10-Ks (old→new).

    The YoY language delta is the point: new risk factors, changed customer
    concentration numbers, dropped reassurances. Line-level diff works because
    fetch_filing_text flattens HTML to one text chunk per line.
    """
    from tradingagents.dataflows import edgar

    list_filings = list_filings or edgar.list_filings
    fetch_text = fetch_text or edgar.fetch_filing_text
    filings = list_filings(ticker, forms={"10-K", "10-K/A"}, limit=200)
    by_year: dict[int, object] = {}
    for f in filings:  # newest-first; keep the newest filing per fiscal year
        by_year.setdefault(_fiscal_year(f), f)
    old_year, new_year = sorted((year_a, year_b))
    missing = [y for y in (old_year, new_year) if y not in by_year]
    if missing:
        raise KeyError(
            f"no 10-K for fiscal year(s) {missing} for {ticker!r} "
            f"(have: {sorted(by_year)})"
        )
    old_f, new_f = by_year[old_year], by_year[new_year]
    old_text = extract_section(
        fetch_text(old_f), form=old_f.form, section=section, max_chars=max_chars,
    )
    new_text = extract_section(
        fetch_text(new_f), form=new_f.form, section=section, max_chars=max_chars,
    )
    diff_lines = difflib.unified_diff(
        old_text.splitlines(), new_text.splitlines(),
        fromfile=f"{old_f.accession_number} (FY{old_year})",
        tofile=f"{new_f.accession_number} (FY{new_year})",
        lineterm="", n=1,
    )
    return SectionDiff(
        ticker=ticker.upper(), section=section,
        accession_new=new_f.accession_number, accession_old=old_f.accession_number,
        text=_truncate("\n".join(diff_lines), max_chars),
    )
