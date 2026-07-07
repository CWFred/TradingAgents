"""Filing-reader agent tools (design-doc build order, step 4).

LangChain @tool wrappers over the deterministic EDGAR/memo primitives, for
use by the existing agent graph. They return plain bounded text and NEVER
raise — a raised exception inside a weak local model's tool loop ends the
run, whereas an "ERROR: ..." string lets the model route around it.

The Phase B memo pipeline (ops/research/brain.py) deliberately does NOT call
these through a tool loop; it calls the underlying primitives directly.
"""

from typing import Annotated

from langchain_core.tools import tool

from tradingagents.dataflows import edgar_sections, form4
from tradingagents.memos.schema import Memo
from tradingagents.memos.store import MemoStore, default_memo_store_path

_MAX_TOOL_CHARS = 12000


def summarize_memo(memo: Memo) -> str:
    """One-paragraph summary used in tool output and thesis prompts."""
    first_sentence = memo.thesis.split(". ")[0].strip()
    line = (
        f"[{memo.memo_id}] {memo.ticker} {memo.thesis_type} "
        f"({memo.status}, tier={memo.conviction_tier}, as_of={memo.as_of_date}): "
        f"{first_sentence}."
    )
    if memo.resolution is not None:
        line += (
            f" Resolved: {memo.resolution.outcome_label}, "
            f"{memo.resolution.realized_return_pct:+.0%} vs "
            f"benchmark {memo.resolution.benchmark_return_pct:+.0%}."
        )
    return line


@tool
def read_filing_section(
    ticker: Annotated[str, "ticker symbol"],
    accession: Annotated[str, "EDGAR accession number, e.g. 0001234567-26-000123"],
    section: Annotated[str, "one of: business, risk_factors, mdna, full"],
) -> str:
    """Read one section of a specific SEC filing (bounded plain text).

    Deterministic extraction — same accession+section always returns the
    same text. Cite it as "{accession}:{section}".
    """
    try:
        result = edgar_sections.read_filing_section(
            ticker, accession, section, max_chars=_MAX_TOOL_CHARS,
        )
        return f"[{result.source_ref}] ({result.form})\n{result.text}"
    except Exception as exc:
        return f"ERROR: {type(exc).__name__}: {exc}"


@tool
def diff_filing_sections(
    ticker: Annotated[str, "ticker symbol"],
    section: Annotated[str, "one of: business, risk_factors, mdna"],
    year_a: Annotated[int, "earlier fiscal year, e.g. 2024"],
    year_b: Annotated[int, "later fiscal year, e.g. 2025"],
) -> str:
    """Unified diff of one 10-K section between two fiscal years.

    What changed in the language year-over-year: new risk factors, changed
    concentration numbers, dropped reassurances.
    """
    try:
        result = edgar_sections.diff_filing_sections(
            ticker, section, year_a, year_b, max_chars=_MAX_TOOL_CHARS,
        )
        return f"[{result.source_ref}]\n{result.text}"
    except Exception as exc:
        return f"ERROR: {type(exc).__name__}: {exc}"


@tool
def get_insider_transactions(
    ticker: Annotated[str, "ticker symbol"],
    lookback_days: Annotated[int, "how far back to look"] = 180,
) -> str:
    """Insider (Form 4) transactions: who bought/sold, open-market vs plan.

    Open-market buys (code P, non-10b5-1) are insiders spending their own
    cash; routine sales and grants carry little signal.
    """
    from datetime import date, timedelta

    try:
        txns = form4.get_insider_transactions(
            ticker, since=date.today() - timedelta(days=lookback_days),
        )
        if not txns:
            return f"No Form 4 transactions for {ticker} in the last {lookback_days} days."
        lines = [
            f"{t.transaction_date} {t.insider_name} "
            f"({t.insider_title or ('director' if t.is_director else 'insider')}) "
            f"{t.kind} {t.shares} sh @ {t.price} "
            f"{'[10b5-1 plan]' if t.ten_b5_1 else '[not a plan]'} ({t.accession})"
            for t in txns
        ]
        return "\n".join(lines)[:_MAX_TOOL_CHARS]
    except Exception as exc:
        return f"ERROR: {type(exc).__name__}: {exc}"


@tool
def get_past_memos(
    ticker: Annotated[str, "ticker symbol"],
) -> str:
    """Past research memos for this ticker from the memo corpus.

    "None found" is an explicit finding — record it as such, do not invent
    precedents.
    """
    try:
        memos = MemoStore(default_memo_store_path()).list(ticker=ticker)
        if not memos:
            return f"No past memos for {ticker}: none found."
        return "\n".join(summarize_memo(m) for m in memos)[:_MAX_TOOL_CHARS]
    except Exception as exc:
        return f"ERROR: {type(exc).__name__}: {exc}"
