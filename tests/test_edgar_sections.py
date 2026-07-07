"""Unit tests for deterministic SEC filing section extraction."""

from datetime import date

import pytest

from tradingagents.dataflows import edgar_sections
from tradingagents.dataflows.edgar import Filing
from tradingagents.dataflows.edgar_sections import (
    FilingSection,
    SectionNotFound,
    extract_section,
    read_filing_section,
)

pytestmark = pytest.mark.unit

# A miniature flattened 10-K: TOC lines first (headings back-to-back), then
# the body, where each heading is followed by real content.
TEN_K_TEXT = "\n".join([
    "TABLE OF CONTENTS",
    "Item 1. Business",
    "Item 1A. Risk Factors",
    "Item 7. Management's Discussion and Analysis",
    "Item 8. Financial Statements",
    "PART I",
    "Item 1. Business",
    "We make widgets in three segments.",
    "The widget market is cyclical.",
    "Item 1A. Risk Factors",
    "Customer concentration: one customer is 40% of revenue.",
    "Litigation: patent suit pending in Delaware.",
    "Item 7. Management's Discussion and Analysis",
    "Revenue declined 12% due to the loss of a distributor.",
    "Gross margin fell 300bps on input costs.",
    "Item 8. Financial Statements",
    "See accompanying notes.",
])


def test_extracts_body_section_not_toc():
    text = extract_section(TEN_K_TEXT, form="10-K", section="risk_factors")
    assert "Customer concentration" in text
    assert "Litigation" in text
    # Stops at the next Item heading.
    assert "Revenue declined" not in text


def test_extracts_mdna():
    text = extract_section(TEN_K_TEXT, form="10-K", section="mdna")
    assert "Revenue declined 12%" in text
    assert "See accompanying notes" not in text


def test_unknown_section_raises():
    with pytest.raises(SectionNotFound):
        extract_section(TEN_K_TEXT, form="10-K", section="compensation")


def test_missing_section_raises():
    with pytest.raises(SectionNotFound):
        extract_section("no items here at all", form="10-K", section="mdna")


def test_full_returns_whole_document_bounded():
    text = extract_section(TEN_K_TEXT, form="8-K", section="full", max_chars=40)
    assert text.startswith("TABLE OF CONTENTS")
    assert "[truncated" in text


def test_truncation_marker():
    text = extract_section(TEN_K_TEXT, form="10-K", section="risk_factors", max_chars=30)
    assert len(text) <= 30 + len("\n[truncated at 30 characters]")
    assert text.endswith("[truncated at 30 characters]")


def _filing(accession="0000000001-26-000001", form="10-K"):
    return Filing(
        ticker="WIDG", cik=1234567, accession_number=accession, form=form,
        filing_date=date(2026, 3, 1), report_date=date(2025, 12, 31),
        primary_document="widg-10k.htm",
    )


def test_read_filing_section_resolves_accession():
    filing = _filing()
    section = read_filing_section(
        "WIDG", filing.accession_number, "mdna",
        list_filings=lambda ticker, **kw: [filing],
        fetch_text=lambda f, **kw: TEN_K_TEXT,
    )
    assert isinstance(section, FilingSection)
    assert section.source_ref == f"{filing.accession_number}:mdna"
    assert "Revenue declined 12%" in section.text
    assert section.form == "10-K"


def test_read_filing_section_unknown_accession_raises():
    with pytest.raises(KeyError):
        read_filing_section(
            "WIDG", "0000000001-26-999999", "mdna",
            list_filings=lambda ticker, **kw: [_filing()],
            fetch_text=lambda f, **kw: TEN_K_TEXT,
        )


TEN_K_TEXT_PRIOR = TEN_K_TEXT.replace(
    "Customer concentration: one customer is 40% of revenue.",
    "Customer concentration: one customer is 25% of revenue.",
).replace(
    "Litigation: patent suit pending in Delaware.",
    "No material litigation.",
)


def _two_ten_ks():
    new = _filing(accession="0000000001-26-000001")
    old = Filing(
        ticker="WIDG", cik=1234567, accession_number="0000000001-25-000001",
        form="10-K", filing_date=date(2025, 3, 1), report_date=date(2024, 12, 31),
        primary_document="widg-10k.htm",
    )
    texts = {new.accession_number: TEN_K_TEXT, old.accession_number: TEN_K_TEXT_PRIOR}
    return new, old, texts


def test_diff_shows_yoy_language_change():
    new, old, texts = _two_ten_ks()
    diff = edgar_sections.diff_filing_sections(
        "WIDG", "risk_factors", 2024, 2025,
        list_filings=lambda ticker, **kw: [new, old],
        fetch_text=lambda f, **kw: texts[f.accession_number],
    )
    assert diff.source_ref == f"{new.accession_number}+{old.accession_number}:risk_factors_diff"
    assert "-Customer concentration: one customer is 25% of revenue." in diff.text
    assert "+Customer concentration: one customer is 40% of revenue." in diff.text
    assert "+Litigation: patent suit pending in Delaware." in diff.text


def test_diff_missing_year_raises():
    new, old, texts = _two_ten_ks()
    with pytest.raises(KeyError):
        edgar_sections.diff_filing_sections(
            "WIDG", "risk_factors", 2019, 2025,
            list_filings=lambda ticker, **kw: [new, old],
            fetch_text=lambda f, **kw: texts[f.accession_number],
        )
