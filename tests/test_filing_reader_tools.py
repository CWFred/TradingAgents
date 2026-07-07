"""Unit tests for the filing-reader agent tools (all I/O injected/mocked)."""

from datetime import date

import pytest

from tradingagents.agents.utils import filing_reader_tools as frt

pytestmark = pytest.mark.unit


def test_tools_are_langchain_tools():
    for t in (frt.read_filing_section, frt.diff_filing_sections,
              frt.get_insider_transactions, frt.get_past_memos):
        assert hasattr(t, "invoke") and hasattr(t, "name")  # BaseTool interface


def test_read_filing_section_returns_error_string_not_raise(monkeypatch):
    def boom(*a, **kw):
        raise KeyError("no filing with accession 'x'")

    monkeypatch.setattr(frt.edgar_sections, "read_filing_section", boom)
    out = frt.read_filing_section.invoke(
        {"ticker": "WIDG", "accession": "x", "section": "mdna"}
    )
    assert out.startswith("ERROR:")


def test_get_past_memos_reports_none_found(tmp_path, monkeypatch):
    monkeypatch.setenv("OPS_MEMO_STORE_PATH", str(tmp_path / "memos.sqlite"))
    out = frt.get_past_memos.invoke({"ticker": "WIDG"})
    assert "none found" in out.lower()


def test_get_past_memos_lists_summaries(tmp_path, monkeypatch):
    from tradingagents.memos.schema import EvidenceItem, Falsifier, Memo, ValueThesis
    from tradingagents.memos.store import MemoStore

    monkeypatch.setenv("OPS_MEMO_STORE_PATH", str(tmp_path / "memos.sqlite"))
    memo = Memo(
        ticker="WIDG", as_of_date=date(2026, 1, 1), thesis_type="value",
        thesis="Cheap because of a temporary distributor loss.",
        evidence=[EvidenceItem(claim="c", source_type="filing", source_ref="a:mdna")],
        value_block=ValueThesis(
            why_cheap="distributor loss", change_trigger="selloff",
            normalized_earnings_view="normal", quality_assessment="fine",
        ),
        conviction_tier="starter", entry_price_ref=4.0,
        price_target_low=5.0, price_target_high=8.0, expected_holding_months=12,
        must_be_true=["distributor replaced"],
        falsifiers=[Falsifier(description="revenue keeps falling", check_type="fundamental")],
    )
    MemoStore(tmp_path / "memos.sqlite").save(memo)
    out = frt.get_past_memos.invoke({"ticker": "WIDG"})
    assert memo.memo_id in out
    assert "value" in out
