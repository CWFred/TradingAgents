"""Mechanical accept/reject gate for locally-generated memos.

Local models produce plausible-looking garbage; the defense is structural,
not prompt hope (spec decision 3). Every check here is machine-decidable:
a memo that fails is rejected (the hit is marked failed after one retry),
never stored. The monitoring loop (Phase C) depends on check 2: without a
machine-checkable falsifier a memo can never be mechanically monitored.
"""

from __future__ import annotations

from tradingagents.memos.schema import EvidenceItem, Memo


def _is_machine_checkable(falsifier) -> bool:
    return (
        bool(falsifier.metric)
        and falsifier.operator is not None
        and falsifier.threshold is not None
    )


def resolve_evidence(
    items: list[EvidenceItem], allowed_refs: set[str],
) -> tuple[list[EvidenceItem], list[str]]:
    """Keep items citing a section we actually read; explain each drop.

    v1 evidence only ever comes from filings the pipeline fetched, so
    non-filing source types are confabulation by construction.
    """
    kept: list[EvidenceItem] = []
    dropped: list[str] = []
    for item in items:
        if item.source_type != "filing":
            dropped.append(
                f"non-filing evidence ({item.source_type}): {item.claim[:80]!r}"
            )
        elif item.source_ref not in allowed_refs:
            dropped.append(
                f"unresolvable citation {item.source_ref!r}: {item.claim[:80]!r}"
            )
        else:
            kept.append(item)
    return kept, dropped


def validate_memo(
    memo: Memo, *, allowed_refs: set[str], known_precedents: set[str],
) -> list[str]:
    """All reasons this memo must be rejected; empty means store it."""
    errors: list[str] = []
    if not memo.block_matches_type():
        errors.append(
            "thesis block does not match thesis_type (fill exactly the "
            f"{memo.thesis_type}_block)"
        )
    if not any(_is_machine_checkable(f) for f in memo.falsifiers):
        errors.append(
            "no machine-checkable falsifier (need metric+operator+threshold "
            "on at least one)"
        )
    for item in memo.evidence:
        if item.source_type == "filing" and item.source_ref not in allowed_refs:
            errors.append(f"evidence cites unread section {item.source_ref!r}")
    for pid in memo.precedent_memo_ids:
        if pid not in known_precedents:
            errors.append(f"precedent memo id {pid!r} does not exist")
    if memo.entry_price_ref <= 0:
        errors.append(f"entry_price_ref must be positive, got {memo.entry_price_ref}")
    if memo.price_target_low > memo.price_target_high:
        errors.append(
            f"price_target_low {memo.price_target_low} exceeds "
            f"price_target_high {memo.price_target_high}"
        )
    return errors
