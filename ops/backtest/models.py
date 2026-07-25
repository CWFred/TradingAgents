"""Canonical domain models for the backtest and learning loop.

The expensive generation plane, deterministic replay plane, and future live
adapter all exchange these values.  Models are deliberately storage-neutral:
SQLite serialization belongs in :mod:`ops.backtest.store`, while stable hashes
are defined here so every producer fingerprints the same logical payload.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Any, Literal

MIN_BACKTEST_CUTOFF = date(2025, 6, 1)


class CutoffViolation(ValueError):
    """A case or context artifact predates the effective model cutoff."""


class CaseSource(str, Enum):
    POINT_IN_TIME = "point-in-time"
    CURRENT_UNIVERSE_RECONSTRUCTION = "exploratory/current-universe-reconstruction"
    LIVE_IMPORT = "live-import"


class DecisionAction(str, Enum):
    BUY = "BUY"
    PASS = "PASS"
    HOLD = "HOLD"
    SELL = "SELL"


class ExecutionSide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OutcomeState(str, Enum):
    MATURE = "mature"
    PENDING = "pending"
    UNPRICEABLE = "unpriceable"


class OutcomeLabel(str, Enum):
    WIN = "win"
    WASH = "wash"
    LOSS = "loss"
    PENDING = "pending"
    UNPRICEABLE = "unpriceable"


class ThesisCorrectness(str, Enum):
    RIGHT = "right"
    WRONG = "wrong"
    INDETERMINATE = "indeterminate"


class ProcessOutcomeQuadrant(str, Enum):
    RIGHT_THESIS_WORKED = "right-thesis-worked"
    RIGHT_THESIS_UNLUCKY = "right-thesis-unlucky"
    WRONG_THESIS_LUCKY = "wrong-thesis-lucky"
    WRONG_THESIS_LOST = "wrong-thesis-lost"
    UNGRADED = "ungraded"


def _jsonable(value: Any) -> Any:
    """Convert domain values to a deterministic, lossless JSON shape."""
    if dataclasses.is_dataclass(value):
        return {
            field_.name: _jsonable(getattr(value, field_.name))
            for field_ in dataclasses.fields(value)
        }
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (set, frozenset)):
        converted = [_jsonable(item) for item in value]
        return sorted(converted, key=lambda item: canonical_json(item))
    if isinstance(value, (tuple, list)):
        return [_jsonable(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f"unsupported value in canonical JSON: {type(value).__name__}")


def canonical_json(value: Any) -> str:
    """Canonical JSON used for durable payloads and cache identities."""
    return json.dumps(
        _jsonable(value), sort_keys=True, separators=(",", ":"),
        ensure_ascii=False, allow_nan=False,
    )


def stable_hash(value: Any) -> str:
    """SHA-256 of :func:`canonical_json`, returned as lowercase hex."""
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def canonical_value(value: Any) -> Any:
    """Return the JSON-native representation used by durable payload fields."""
    return json.loads(canonical_json(value))


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def require_aware(value: datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")


def enforce_cutoff(asof: date, cutoff: date = MIN_BACKTEST_CUTOFF) -> None:
    if cutoff < MIN_BACKTEST_CUTOFF:
        raise ValueError(
            f"effective cutoff {cutoff} cannot precede hard minimum {MIN_BACKTEST_CUTOFF}"
        )
    if asof < cutoff:
        raise CutoffViolation(
            f"case asof {asof} precedes effective backtest cutoff {cutoff}"
        )


@dataclass(frozen=True)
class BacktestCase:
    case_id: str
    sleeve: str
    symbol: str
    asof: date
    trigger: Mapping[str, Any] = field(default_factory=dict)
    source: CaseSource = CaseSource.POINT_IN_TIME
    score: Decimal | None = None
    created_at: datetime = field(default_factory=utcnow)

    def __post_init__(self) -> None:
        if not self.case_id.strip():
            raise ValueError("case_id must not be empty")
        if not self.sleeve.strip():
            raise ValueError("sleeve must not be empty")
        if not self.symbol.strip():
            raise ValueError("symbol must not be empty")
        require_aware(self.created_at, "created_at")

    @classmethod
    def create(
        cls,
        *,
        sleeve: str,
        symbol: str,
        asof: date,
        trigger: Mapping[str, Any] | None = None,
        source: CaseSource | str = CaseSource.POINT_IN_TIME,
        score: Decimal | None = None,
        cutoff: date = MIN_BACKTEST_CUTOFF,
        created_at: datetime | None = None,
    ) -> BacktestCase:
        enforce_cutoff(asof, cutoff)
        normalized_sleeve = sleeve.strip().lower()
        normalized_symbol = symbol.strip().upper()
        parsed_source = source if isinstance(source, CaseSource) else CaseSource(source)
        identity = {
            "sleeve": normalized_sleeve,
            "symbol": normalized_symbol,
            "asof": asof,
        }
        return cls(
            case_id=f"case-{stable_hash(identity)[:24]}",
            sleeve=normalized_sleeve,
            symbol=normalized_symbol,
            asof=asof,
            trigger=canonical_value(trigger or {}),
            source=parsed_source,
            score=score,
            created_at=created_at or utcnow(),
        )

    def validate_cutoff(self, cutoff: date = MIN_BACKTEST_CUTOFF) -> None:
        enforce_cutoff(self.asof, cutoff)


Case = BacktestCase


@dataclass(frozen=True)
class PriceBar:
    symbol: str
    session: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    adjusted_open: Decimal
    adjusted_high: Decimal
    adjusted_low: Decimal
    adjusted_close: Decimal
    volume: Decimal = Decimal("0")
    dividend: Decimal = Decimal("0")
    split_ratio: Decimal = Decimal("1")
    provider: str = ""
    fetched_at: datetime = field(default_factory=utcnow)

    def __post_init__(self) -> None:
        if not self.symbol.strip():
            raise ValueError("bar symbol must not be empty")
        require_aware(self.fetched_at, "fetched_at")
        for name in (
            "open", "high", "low", "close", "adjusted_open", "adjusted_high",
            "adjusted_low", "adjusted_close",
        ):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be positive")
        if self.volume < 0 or self.dividend < 0 or self.split_ratio <= 0:
            raise ValueError("volume/dividend must be nonnegative and split_ratio positive")


Bar = PriceBar


@dataclass(frozen=True)
class ContextItem:
    item_id: str
    kind: str
    source_ref: str
    available_at: date
    content: str
    content_hash: str
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def create(
        cls,
        *,
        kind: str,
        source_ref: str,
        available_at: date,
        content: str,
        metadata: Mapping[str, Any] | None = None,
    ) -> ContextItem:
        if not kind.strip() or not source_ref.strip():
            raise ValueError("context kind and source_ref must not be empty")
        content_hash = stable_hash({"content": content})
        identity = {
            "kind": kind,
            "source_ref": source_ref,
            "available_at": available_at,
            "content_hash": content_hash,
        }
        return cls(
            item_id=f"ctx-{stable_hash(identity)[:24]}",
            kind=kind.strip(),
            source_ref=source_ref.strip(),
            available_at=available_at,
            content=content,
            content_hash=content_hash,
            metadata=canonical_value(metadata or {}),
        )

    def validate_asof(self, asof: date) -> None:
        if self.available_at > asof:
            raise CutoffViolation(
                f"context item {self.source_ref!r} available {self.available_at} after {asof}"
            )


@dataclass(frozen=True)
class ContextExclusion:
    kind: str
    source_ref: str
    reason: str
    available_at: date | None = None


@dataclass(frozen=True)
class ContextManifest:
    manifest_id: str
    case_id: str
    asof: date
    included: tuple[ContextItem, ...]
    excluded: tuple[ContextExclusion, ...]
    substitutions: tuple[str, ...]
    manifest_hash: str
    created_at: datetime = field(default_factory=utcnow)

    @classmethod
    def create(
        cls,
        *,
        case_id: str,
        asof: date,
        included: Sequence[ContextItem] = (),
        excluded: Sequence[ContextExclusion] = (),
        substitutions: Sequence[str] = (),
        created_at: datetime | None = None,
    ) -> ContextManifest:
        ordered_included = tuple(sorted(included, key=lambda item: item.item_id))
        ordered_excluded = tuple(
            sorted(excluded, key=lambda item: (item.kind, item.source_ref, item.reason))
        )
        payload = {
            "case_id": case_id,
            "asof": asof,
            "included": ordered_included,
            "excluded": ordered_excluded,
            "substitutions": sorted(set(substitutions)),
        }
        manifest_hash = stable_hash(payload)
        manifest = cls(
            manifest_id=f"manifest-{manifest_hash[:24]}",
            case_id=case_id,
            asof=asof,
            included=ordered_included,
            excluded=ordered_excluded,
            substitutions=tuple(sorted(set(substitutions))),
            manifest_hash=manifest_hash,
            created_at=created_at or utcnow(),
        )
        manifest.validate_point_in_time()
        return manifest

    def validate_point_in_time(self) -> None:
        for item in self.included:
            item.validate_asof(self.asof)


@dataclass(frozen=True)
class Decision:
    decision_id: str
    run_id: str
    case_id: str
    sequence: int
    observed_session: date
    action: DecisionAction
    reason: str
    settings_hash: str
    observed_price: Decimal | None = None
    memo_key: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.sequence < 0:
            raise ValueError("decision sequence must be nonnegative")
        if not self.settings_hash:
            raise ValueError("settings_hash must not be empty")


@dataclass(frozen=True)
class Execution:
    execution_id: str
    run_id: str
    case_id: str
    decision_id: str
    session: date
    side: ExecutionSide
    price: Decimal
    quantity: Decimal
    notional: Decimal

    def __post_init__(self) -> None:
        if self.price <= 0 or self.quantity <= 0 or self.notional <= 0:
            raise ValueError("execution price, quantity, and notional must be positive")


@dataclass(frozen=True)
class HorizonOutcome:
    run_id: str
    case_id: str
    horizon_sessions: int
    state: OutcomeState
    label: OutcomeLabel
    stock_return: Decimal | None = None
    benchmark_return: Decimal | None = None
    excess_return: Decimal | None = None
    utility: Decimal | None = None
    entry_session: date | None = None
    horizon_session: date | None = None
    detail: str = ""

    def __post_init__(self) -> None:
        if self.horizon_sessions <= 0:
            raise ValueError("horizon_sessions must be positive")


@dataclass(frozen=True)
class CaseResult:
    run_id: str
    case_id: str
    initial_action: DecisionAction
    status: Literal["complete", "pending", "unpriceable", "failed"]
    primary_horizon: int
    primary_label: OutcomeLabel
    actual_return: Decimal | None = None
    max_drawdown: Decimal | None = None
    exit_session: date | None = None
    exit_reason: str | None = None
    quadrant: ProcessOutcomeQuadrant = ProcessOutcomeQuadrant.UNGRADED


@dataclass(frozen=True)
class ThesisAssessment:
    assessment_key: str
    memo_key: str
    case_id: str
    correctness: ThesisCorrectness
    rationale: str
    evidence_cutoff: date
    model_id: str
    prompt_version: str
    evidence: tuple[str, ...] = ()
    created_at: datetime = field(default_factory=utcnow)


@dataclass(frozen=True)
class Lesson:
    lesson_id: str
    sleeve: str
    text: str
    source_case_ids: tuple[str, ...]
    eligible_from: date
    fingerprint: str
    tags: tuple[str, ...] = ("backtest-lesson",)
    created_at: datetime = field(default_factory=utcnow)


@dataclass(frozen=True)
class ExperimentRecord:
    experiment_id: str
    sleeve: str
    seed: int
    holdout_case_ids: tuple[str, ...]
    lesson_fingerprint: str
    status: Literal["planned", "running", "complete", "failed"] = "planned"
    control_metrics: Mapping[str, Any] = field(default_factory=dict)
    treated_metrics: Mapping[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=utcnow)
