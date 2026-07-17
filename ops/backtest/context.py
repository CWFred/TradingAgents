"""Point-in-time context gates for frozen backtest memo generation.

Every prompt input passes through this module.  Unknown dates fail closed and
remain visible in the manifest as exclusions; future artifacts are never
silently approximated or allowed to influence the manifest hash.
"""
from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from ops.backtest.models import ContextExclusion, ContextItem, ContextManifest


@dataclass(frozen=True)
class ContextArtifact:
    """Untrusted context input before its availability date is validated."""

    kind: object
    source_ref: object
    available_at: object
    content: Any
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class FrozenFiling:
    """The filing metadata exposed to the research brain from one manifest item."""

    ticker: str
    accession_number: str
    form: str
    filing_date: date
    report_date: date | None = None
    cik: int = 0
    primary_document: str = ""
    primary_doc_description: str = ""
    items: tuple[str, ...] = ()


def _value(record: object, name: str, default: Any = None) -> Any:
    if isinstance(record, Mapping):
        return record.get(name, default)
    return getattr(record, name, default)


def _as_date(value: object) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    try:
        return date.fromisoformat(raw)
    except ValueError:
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00")).date()
        except ValueError:
            return None


def _identity(record: object) -> tuple[str, str]:
    raw_kind = _value(record, "kind")
    raw_ref = _value(record, "source_ref")
    kind = raw_kind.strip() if isinstance(raw_kind, str) and raw_kind.strip() else "unknown"
    source_ref = raw_ref.strip() if isinstance(raw_ref, str) and raw_ref.strip() else "unknown"
    return kind, source_ref


def _exclusion(
    record: object,
    reason: str,
    *,
    available_at: date | None = None,
) -> ContextExclusion:
    kind, source_ref = _identity(record)
    return ContextExclusion(
        source_ref=source_ref,
        kind=kind,
        reason=reason,
        available_at=available_at,
    )


def filter_context_items(
    artifacts: Iterable[object],
    *,
    asof: date,
) -> tuple[tuple[ContextItem, ...], tuple[ContextExclusion, ...]]:
    """Validate and partition prompt artifacts against ``asof``.

    An item needs non-empty ``kind`` and ``source_ref`` plus a parseable
    availability date.  Included items are rebuilt through ``ContextItem`` so
    their ids and hashes derive only from validated data.
    """
    included_by_id: dict[str, ContextItem] = {}
    excluded: list[ContextExclusion] = []
    for artifact in artifacts:
        kind, source_ref = _identity(artifact)
        if kind == "unknown" or source_ref == "unknown":
            excluded.append(_exclusion(artifact, "missing kind or source_ref"))
            continue

        raw_available = _value(artifact, "available_at")
        available_at = _as_date(raw_available)
        if raw_available is None or (isinstance(raw_available, str) and not raw_available.strip()):
            excluded.append(_exclusion(artifact, "missing available_at"))
            continue
        if available_at is None:
            excluded.append(_exclusion(artifact, "malformed available_at"))
            continue
        if available_at > asof:
            excluded.append(
                _exclusion(
                    artifact,
                    f"available after case asof {asof.isoformat()}",
                    available_at=available_at,
                )
            )
            continue

        metadata = _value(artifact, "metadata", {})
        if not isinstance(metadata, Mapping):
            excluded.append(
                _exclusion(artifact, "malformed metadata", available_at=available_at)
            )
            continue
        content = _value(artifact, "content")
        if not isinstance(content, str):
            excluded.append(
                _exclusion(artifact, "malformed content", available_at=available_at)
            )
            continue
        item = ContextItem.create(
            kind=kind,
            source_ref=source_ref,
            available_at=available_at,
            content=content,
            metadata=dict(metadata),
        )
        included_by_id[item.item_id] = item

    included = tuple(
        sorted(
            included_by_id.values(),
            key=lambda item: (item.available_at, item.kind, item.source_ref, item.item_id),
        )
    )
    exclusions = tuple(
        sorted(
            excluded,
            key=lambda item: (
                item.available_at or date.min,
                item.kind,
                item.source_ref,
                item.reason,
            ),
        )
    )
    return included, exclusions


def build_context_manifest(
    *,
    case_id: str,
    asof: date,
    artifacts: Iterable[object],
    substitutions: Sequence[str] = (),
) -> ContextManifest:
    """Build a stable manifest whose included set is point-in-time safe."""
    included, excluded = filter_context_items(artifacts, asof=asof)
    manifest = ContextManifest.create(
        case_id=case_id,
        asof=asof,
        included=included,
        excluded=excluded,
        substitutions=tuple(sorted(set(substitutions))),
    )
    manifest.validate_point_in_time()
    return manifest


def manifest_filing_adapters(
    manifest: ContextManifest,
    *,
    symbol: str,
) -> tuple[Callable[..., list[FrozenFiling]], Callable[[object], str]]:
    """Build filing readers backed exclusively by sealed manifest rows.

    Neither adapter has an upstream fallback. A filing absent from the
    manifest is therefore impossible for the research brain to discover or
    fetch during generation.
    """
    manifest.validate_point_in_time()
    normalized_symbol = symbol.strip().upper()
    filings: list[FrozenFiling] = []
    text_by_accession: dict[str, str] = {}
    for item in manifest.included:
        if item.kind.strip().lower().replace("-", "_") not in {
            "filing", "sec_filing", "edgar_filing",
        }:
            continue
        metadata = dict(item.metadata)
        item_symbol = str(metadata.get("symbol", metadata.get("ticker", symbol))).upper()
        if item_symbol != normalized_symbol:
            continue
        accession = str(metadata.get("accession_number", item.source_ref)).strip()
        form = str(metadata.get("form", "")).strip()
        if not accession or not form:
            # Metadata needed to classify a filing must itself be frozen.
            continue
        filing_date = _as_date(metadata.get("filing_date")) or item.available_at
        report_date = _as_date(metadata.get("report_date"))
        try:
            cik = int(metadata.get("cik", 0))
        except (TypeError, ValueError):
            cik = 0
        raw_items = metadata.get("items", ())
        if isinstance(raw_items, str):
            raw_items = (raw_items,)
        if not isinstance(raw_items, (list, tuple)):
            raw_items = ()
        filing = FrozenFiling(
            ticker=normalized_symbol,
            accession_number=accession,
            form=form,
            filing_date=filing_date,
            report_date=report_date,
            cik=cik,
            primary_document=str(metadata.get("primary_document", "")),
            primary_doc_description=str(metadata.get("primary_doc_description", "")),
            items=tuple(str(value) for value in raw_items),
        )
        filings.append(filing)
        text_by_accession[accession] = item.content
    filings.sort(
        key=lambda filing: (filing.filing_date, filing.accession_number), reverse=True,
    )

    def list_frozen_filings(
        ticker: str,
        *,
        forms: Iterable[str] | None = None,
        since: date | None = None,
        limit: int = 100,
        **_kwargs: Any,
    ) -> list[FrozenFiling]:
        if ticker.strip().upper() != normalized_symbol or limit <= 0:
            return []
        requested_forms = set(forms) if forms is not None else None
        return [
            filing for filing in filings
            if (requested_forms is None or filing.form in requested_forms)
            and (since is None or filing.filing_date >= since)
        ][:limit]

    def fetch_frozen_text(filing: object) -> str:
        accession = str(_value(filing, "accession_number", "")).strip()
        if accession not in text_by_accession:
            raise KeyError(f"filing {accession!r} is not present in the frozen manifest")
        return text_by_accession[accession]

    return list_frozen_filings, fetch_frozen_text


def manifest_price_fetcher(
    manifest: ContextManifest,
    *,
    symbol: str,
) -> Callable[[str], object | None]:
    """Build a ``PriceContext`` reader using only manifest price items."""
    from ops.research.prices import PriceContext

    manifest.validate_point_in_time()
    normalized_symbol = symbol.strip().upper()
    closes: dict[date, Decimal] = {}
    splits: dict[date, Decimal] = {}

    def add_decimal(target: dict[date, Decimal], when: object, value: object) -> None:
        session = _as_date(when)
        if session is None or session > manifest.asof:
            return
        try:
            parsed = Decimal(str(value))
        except (InvalidOperation, TypeError, ValueError):
            return
        if parsed > 0:
            target[session] = parsed

    for item in manifest.included:
        if item.kind.strip().lower().replace("-", "_") not in {
            "price", "price_bar", "price_history", "market_price",
        }:
            continue
        metadata = dict(item.metadata)
        item_symbol = str(metadata.get("symbol", metadata.get("ticker", symbol))).upper()
        if item_symbol != normalized_symbol:
            continue
        try:
            payload = json.loads(item.content)
        except (json.JSONDecodeError, TypeError):
            payload = item.content
        if isinstance(payload, Mapping):
            raw_closes = payload.get("closes")
            if isinstance(raw_closes, Mapping):
                for when, value in raw_closes.items():
                    add_decimal(closes, when, value)
            raw_splits = payload.get("splits")
            if isinstance(raw_splits, Mapping):
                for when, value in raw_splits.items():
                    add_decimal(splits, when, value)
            add_decimal(
                closes,
                payload.get("session", payload.get("date", item.available_at)),
                payload.get("close", payload.get("adjusted_close")),
            )
            add_decimal(
                splits,
                payload.get("session", payload.get("date", item.available_at)),
                payload.get("split"),
            )
        else:
            add_decimal(closes, item.available_at, metadata.get("close", payload))
            add_decimal(splits, item.available_at, metadata.get("split"))

    context = PriceContext(closes=closes, splits=splits) if closes else None

    def fetch_frozen_prices(requested_symbol: str) -> object | None:
        if requested_symbol.strip().upper() != normalized_symbol:
            return None
        return context

    return fetch_frozen_prices


def asof_gated_filings(
    list_filings: Callable[..., Sequence[object]],
    *,
    asof: date,
    fetch_limit: int = 1000,
) -> Callable[..., list[object]]:
    """Wrap the research brain's filing reader with a strict filing-date gate.

    The wrapper fetches beyond the caller's requested limit before filtering;
    otherwise a page of future amendments could hide the newest eligible 10-K
    or 10-Q.  Missing/malformed filing dates are excluded.
    """
    if fetch_limit <= 0:
        raise ValueError("fetch_limit must be positive")

    def gated(
        ticker: str,
        *,
        forms: Iterable[str] | None = None,
        since: date | None = None,
        limit: int = 100,
        **kwargs: Any,
    ) -> list[object]:
        if limit <= 0:
            return []
        requested_forms = set(forms) if forms is not None else None
        rows = list_filings(
            ticker,
            forms=forms,
            since=since,
            limit=max(limit, fetch_limit),
            **kwargs,
        )
        eligible: list[tuple[date, str, object]] = []
        for filing in rows:
            filed = _as_date(_value(filing, "filing_date"))
            if filed is None or filed > asof:
                continue
            if since is not None and filed < since:
                continue
            form = _value(filing, "form")
            if requested_forms is not None and form not in requested_forms:
                continue
            accession = str(_value(filing, "accession_number", ""))
            eligible.append((filed, accession, filing))
        eligible.sort(key=lambda row: (row[0], row[1]), reverse=True)
        return [row[2] for row in eligible[:limit]]

    return gated


def filter_precedent_memos(memos: Iterable[object], *, asof: date) -> tuple[object, ...]:
    """Return only memos from strictly earlier cases, in stable order."""
    eligible: list[tuple[date, str, object]] = []
    for memo in memos:
        memo_asof = _as_date(_value(memo, "as_of_date"))
        if memo_asof is None or memo_asof >= asof:
            continue
        eligible.append((memo_asof, str(_value(memo, "memo_id", "")), memo))
    eligible.sort(key=lambda row: (row[0], row[1]))
    return tuple(row[2] for row in eligible)


def filter_eligible_lessons(lessons: Iterable[object], *, asof: date) -> tuple[object, ...]:
    """Return active lessons proven eligible before this case.

    Strictly-earlier eligibility prevents same-date case ordering from changing
    prompt contents and enforces the design's "future case" learning rule.
    """
    eligible: list[tuple[date, str, object]] = []
    for lesson in lessons:
        eligible_from = _as_date(_value(lesson, "eligible_from"))
        if eligible_from is None or eligible_from >= asof:
            continue
        if _value(lesson, "active", True) is not True:
            continue
        eligible.append((eligible_from, str(_value(lesson, "lesson_id", "")), lesson))
    eligible.sort(key=lambda row: (row[0], row[1]))
    return tuple(row[2] for row in eligible)


def filter_temporal_records(
    records: Iterable[object],
    *,
    asof: date,
    date_field: str,
) -> tuple[object, ...]:
    """Fail-closed date gate for prompt-adjacent records such as price bars."""
    eligible: list[tuple[date, int, object]] = []
    for index, record in enumerate(records):
        record_date = _as_date(_value(record, date_field))
        if record_date is None or record_date > asof:
            continue
        eligible.append((record_date, index, record))
    eligible.sort(key=lambda row: (row[0], row[1]))
    return tuple(row[2] for row in eligible)
