"""Read-only normalization of live research records into canonical triples."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, overload

from ops import events
from ops.backtest.models import MIN_BACKTEST_CUTOFF, DecisionAction, stable_hash


@dataclass(frozen=True)
class ImportedDecision:
    source_id: str
    memo_id: str
    symbol: str
    observed_at: datetime
    action: DecisionAction
    reason: str
    price: Decimal | None = None
    quantity: Decimal | None = None


@dataclass(frozen=True)
class ImportedOutcome:
    """A realized live price outcome, retaining its exact decision inputs."""

    source_id: str
    memo_id: str
    symbol: str
    entry_decision_id: str
    exit_decision_id: str
    entry_at: datetime
    exit_at: datetime
    entry_price: Decimal
    exit_price: Decimal
    quantity: Decimal | None
    realized_return: Decimal


@dataclass(frozen=True)
class ProvenanceIssue:
    source_id: str
    reason: str
    kind: str
    memo_id: str | None = None
    symbol: str | None = None
    observed_at: datetime | None = None


@dataclass(frozen=True)
class CanonicalTriple:
    memo: Any
    decisions: tuple[ImportedDecision, ...]
    outcomes: tuple[ImportedOutcome, ...] = ()
    provenance_issues: tuple[ProvenanceIssue, ...] = ()


@dataclass(frozen=True)
class LiveNormalization(Sequence[CanonicalTriple]):
    """Tuple-compatible result plus provenance that cannot attach to a memo."""

    triples: tuple[CanonicalTriple, ...]
    provenance_issues: tuple[ProvenanceIssue, ...] = ()

    def __len__(self) -> int:
        return len(self.triples)

    @overload
    def __getitem__(self, index: int) -> CanonicalTriple: ...

    @overload
    def __getitem__(self, index: slice) -> tuple[CanonicalTriple, ...]: ...

    def __getitem__(
        self, index: int | slice,
    ) -> CanonicalTriple | tuple[CanonicalTriple, ...]:
        return self.triples[index]


@dataclass(frozen=True)
class _Fill:
    source_id: str
    client_order_id: str
    symbol: str
    side: str
    observed_at: datetime
    price: Decimal
    quantity: Decimal | None


def _source_id(event: Mapping[str, Any]) -> str:
    identity = {
        "kind": event.get("kind"), "at": event.get("at"),
        "payload": event.get("payload"),
    }
    try:
        digest = stable_hash(identity)
    except TypeError:
        digest = stable_hash(repr(identity))
    return f"live-{digest[:24]}"


def _decimal(value: object, *, positive: bool = True) -> Decimal | None:
    if value is None:
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if not parsed.is_finite() or (positive and parsed <= 0):
        return None
    return parsed


def _issue(
    event: Mapping[str, Any], reason: str, *, memo_id: str | None = None,
    symbol: str | None = None,
) -> ProvenanceIssue:
    observed_at = event.get("at")
    payload = event.get("payload")
    if isinstance(payload, Mapping):
        memo_id = memo_id or str(payload.get("memo_id") or "")
        symbol = symbol or str(payload.get("symbol") or "").strip().upper()
    return ProvenanceIssue(
        source_id=_source_id(event), reason=reason, kind=str(event.get("kind") or ""),
        memo_id=memo_id or None, symbol=symbol or None,
        observed_at=observed_at if isinstance(observed_at, datetime) else None,
    )


def _parse_fill(event: Mapping[str, Any]) -> tuple[_Fill | None, ProvenanceIssue | None]:
    payload = event.get("payload")
    if not isinstance(payload, Mapping):
        return None, _issue(event, "malformed fill payload")
    observed_at = event.get("at")
    client_order_id = str(payload.get("client_order_id") or "")
    symbol = str(payload.get("symbol") or "").strip().upper()
    side = str(payload.get("side") or "").strip().upper()
    price = _decimal(payload.get("price"))
    quantity = _decimal(payload.get("quantity"))
    if (
        not isinstance(observed_at, datetime)
        or observed_at.tzinfo is None
        or observed_at.utcoffset() is None
        or not client_order_id
        or not symbol
        or side not in {"BUY", "SELL"}
        or price is None
    ):
        return None, _issue(
            event, "malformed fill provenance", symbol=symbol or None,
        )
    return _Fill(
        _source_id(event), client_order_id, symbol, side, observed_at, price, quantity,
    ), None


def _matching_sell_fill(
    fills: Sequence[_Fill], *, symbol: str, observed_at: datetime,
    used: set[str],
) -> _Fill | None:
    eligible = [
        fill for fill in fills
        if fill.side == "SELL" and fill.symbol == symbol
        and fill.observed_at <= observed_at and fill.source_id not in used
    ]
    return max(eligible, key=lambda fill: (fill.observed_at, fill.source_id), default=None)


def _event_decision(
    event: Mapping[str, Any], *, fills_by_client: Mapping[str, _Fill],
    fills: Sequence[_Fill], used_fills: set[str],
) -> tuple[ImportedDecision | None, ProvenanceIssue | None]:
    kind = event.get("kind")
    if kind not in {
        events.KIND_RESEARCH_POSITION_OPENED,
        events.KIND_RESEARCH_POSITION_CLOSED,
    }:
        return None, _issue(event, "event kind is outside the live research adapter")
    payload = event.get("payload")
    if not isinstance(payload, Mapping):
        return None, _issue(event, "malformed research lifecycle payload")
    memo_id = str(payload.get("memo_id") or "")
    symbol = str(payload.get("symbol") or "").strip().upper()
    observed_at = event.get("at")
    if (
        not memo_id or not symbol or not isinstance(observed_at, datetime)
        or observed_at.tzinfo is None or observed_at.utcoffset() is None
    ):
        return None, _issue(
            event, "missing memo/symbol/timestamp provenance",
            memo_id=memo_id, symbol=symbol,
        )
    quantity = None
    provenance_issue = None
    if kind == events.KIND_RESEARCH_POSITION_OPENED:
        action = DecisionAction.BUY
        reason = "live research position opened"
        client_order_id = str(payload.get("client_order_id") or "")
        fill = fills_by_client.get(client_order_id) if client_order_id else None
        if fill is not None and fill.symbol == symbol and fill.side == "BUY":
            price, quantity = fill.price, fill.quantity
            used_fills.add(fill.source_id)
        else:
            price = _decimal(payload.get("price"))
            provenance_issue = _issue(
                event, "opening decision has no matching BUY fill",
                memo_id=memo_id, symbol=symbol,
            )
    else:
        action = DecisionAction.SELL
        reason = str(payload.get("reason") or "live research position closed")
        fill = _matching_sell_fill(
            fills, symbol=symbol, observed_at=observed_at, used=used_fills,
        )
        if fill is not None:
            used_fills.add(fill.source_id)
            quantity = fill.quantity
        price = _decimal(payload.get("price")) or (fill.price if fill is not None else None)
        if price is None:
            provenance_issue = _issue(
                event, "closing decision has no usable price",
                memo_id=memo_id, symbol=symbol,
            )
    identity = {
        "kind": kind, "at": observed_at, "memo_id": memo_id,
        "symbol": symbol, "payload": payload,
    }
    return ImportedDecision(
        source_id=f"live-{stable_hash(identity)[:24]}", memo_id=memo_id,
        symbol=symbol, observed_at=observed_at, action=action,
        reason=reason, price=price, quantity=quantity,
    ), provenance_issue


def _outcomes(decisions: Sequence[ImportedDecision]) -> tuple[ImportedOutcome, ...]:
    open_decision: ImportedDecision | None = None
    outcomes: list[ImportedOutcome] = []
    for decision in decisions:
        if decision.action is DecisionAction.BUY:
            open_decision = decision
            continue
        if decision.action is not DecisionAction.SELL or open_decision is None:
            continue
        if open_decision.price is not None and decision.price is not None:
            identity = {
                "entry": open_decision.source_id, "exit": decision.source_id,
                "entry_price": open_decision.price, "exit_price": decision.price,
            }
            outcomes.append(ImportedOutcome(
                source_id=f"live-outcome-{stable_hash(identity)[:24]}",
                memo_id=decision.memo_id, symbol=decision.symbol,
                entry_decision_id=open_decision.source_id,
                exit_decision_id=decision.source_id,
                entry_at=open_decision.observed_at, exit_at=decision.observed_at,
                entry_price=open_decision.price, exit_price=decision.price,
                quantity=open_decision.quantity or decision.quantity,
                realized_return=(decision.price - open_decision.price) / open_decision.price,
            ))
        open_decision = None
    return tuple(outcomes)


def normalize_live_research(
    *, memo_store, research_journal, cutoff: date = MIN_BACKTEST_CUTOFF,
) -> LiveNormalization:
    """Read live stores without mutations and retain all normalization failures."""
    memos = {
        memo.memo_id: memo
        for memo in memo_store.list()
        if memo.as_of_date >= cutoff
    }
    raw_events = tuple(research_journal.read_events())
    issues: list[ProvenanceIssue] = []
    fills: list[_Fill] = []
    for raw in raw_events:
        if not isinstance(raw, Mapping):
            issues.append(ProvenanceIssue(
                source_id=f"live-{stable_hash(repr(raw))[:24]}",
                reason="journal row is not a mapping", kind="",
            ))
            continue
        if raw.get("kind") == events.KIND_FILL:
            fill, issue = _parse_fill(raw)
            if issue is not None:
                issues.append(issue)
            if fill is not None:
                fills.append(fill)
    fills.sort(key=lambda row: (row.observed_at, row.source_id))
    fills_by_client = {fill.client_order_id: fill for fill in fills}
    used_fills: set[str] = set()
    decisions: dict[str, list[ImportedDecision]] = {memo_id: [] for memo_id in memos}
    for raw in raw_events:
        if not isinstance(raw, Mapping) or raw.get("kind") == events.KIND_FILL:
            continue
        decision, issue = _event_decision(
            raw, fills_by_client=fills_by_client, fills=fills, used_fills=used_fills,
        )
        if issue is not None:
            issues.append(issue)
        if decision is None:
            continue
        if decision.memo_id not in memos:
            issues.append(_issue(
                raw, "lifecycle event references a memo outside the imported corpus",
                memo_id=decision.memo_id, symbol=decision.symbol,
            ))
            continue
        decisions[decision.memo_id].append(decision)
    for fill in fills:
        if fill.source_id not in used_fills:
            issues.append(ProvenanceIssue(
                source_id=fill.source_id, reason="fill is not linked to a research lifecycle event",
                kind=events.KIND_FILL, symbol=fill.symbol, observed_at=fill.observed_at,
            ))
    ordered_issues = tuple(sorted(
        {issue.source_id + "\0" + issue.reason: issue for issue in issues}.values(),
        key=lambda issue: (
            issue.observed_at.isoformat() if issue.observed_at else "",
            issue.source_id, issue.reason,
        ),
    ))
    triples = []
    for memo_id in sorted(memos):
        ordered_decisions = tuple(sorted(
            decisions[memo_id], key=lambda item: (item.observed_at, item.source_id),
        ))
        triples.append(CanonicalTriple(
            memo=memos[memo_id], decisions=ordered_decisions,
            outcomes=_outcomes(ordered_decisions),
            provenance_issues=tuple(
                issue for issue in ordered_issues if issue.memo_id == memo_id
            ),
        ))
    return LiveNormalization(tuple(triples), ordered_issues)
