"""`ops research report` (Phase D task 8): the quarterly calibration report.

build_report reads ONLY the memo store and the two equity journals (research
and baseline) — no broker, no MCP, no quotes, no LLM — so it is always safe
to run and never drifts from `ops status`'s journal-only discipline (see
ops/status.py). Every number the report shows is either a straight count or
a mean over the *resolved* corpus (memos with a human-judged outcome); open
and passed-but-unresolved memos are counted in section 1 only.

Six sections (each a top-level dict key and a markdown `##`):

1. corpus            — counts by status/thesis_type/conviction_tier, date range.
2. outcome_matrix     — the right/wrong-process x made/lost-money 2x2.
3. scenario_calibration — stated (Σ p*return) vs realized return, small-corpus honest.
4. bought_vs_passed   — mean realized return, positioned vs shadow-tracked.
5. sleeve_vs_baseline — equity return over the overlapping snapshot window.
6. per_model          — resolved-memo stats grouped by authored_by_model.

format_report is a pure renderer over the dict (markdown, `#`/`##` headers +
pipe tables) — mirrors ops/status.py's build/format split. Every section
degrades to a literal "no data yet" when its inputs are empty, so the report
is safe to run on day one before any memo has resolved.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from ops import events
from tradingagents.memos.schema import OutcomeLabel

# The four outcome-label cells, in a fixed order so every rendering (dict and
# markdown) is deterministic regardless of corpus contents.
_OUTCOME_LABELS: tuple[OutcomeLabel, ...] = (
    "thesis_right_made_money",
    "thesis_right_lost_money",
    "thesis_wrong_made_money",
    "thesis_wrong_lost_money",
)

_UNATTRIBUTED = "(unattributed)"

# Below this many resolved memos, calibration statistics are noise (design
# doc: "below ~30-50 the corpus is noise; be honest at any n") — the section
# reports the honesty string instead of numbers.
_SMALL_CORPUS_THRESHOLD = 5


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _model_label(authored_by_model: str) -> str:
    return authored_by_model if authored_by_model else _UNATTRIBUTED


def _build_corpus_section(all_memos: list[Any]) -> dict[str, Any]:
    by_status = {"open": 0, "passed": 0, "resolved": 0}
    by_thesis_type = {"value": 0, "event": 0}
    by_conviction_tier = {"starter": 0, "medium": 0, "high": 0}
    for memo in all_memos:
        by_status[memo.status] += 1
        by_thesis_type[memo.thesis_type] += 1
        by_conviction_tier[memo.conviction_tier] += 1
    created_ats = [memo.created_at for memo in all_memos]
    return {
        "total": len(all_memos),
        "by_status": by_status,
        "by_thesis_type": by_thesis_type,
        "by_conviction_tier": by_conviction_tier,
        "oldest_memo_at": min(created_ats) if created_ats else None,
        "newest_memo_at": max(created_ats) if created_ats else None,
    }


def _build_outcome_matrix(resolved: list[Any]) -> dict[str, Any]:
    cells: dict[str, dict[str, Any]] = {
        label: {"count": 0, "mean_realized_return_pct": None} for label in _OUTCOME_LABELS
    }
    returns_by_label: dict[str, list[float]] = {label: [] for label in _OUTCOME_LABELS}
    for memo in resolved:
        label = memo.resolution.outcome_label
        returns_by_label[label].append(memo.resolution.realized_return_pct)
    for label in _OUTCOME_LABELS:
        rets = returns_by_label[label]
        cells[label] = {"count": len(rets), "mean_realized_return_pct": _mean(rets)}
    return {"n": len(resolved), "cells": cells}


def _build_scenario_calibration(resolved: list[Any]) -> dict[str, Any]:
    # Exclude memos with empty scenarios; count them as unscored.
    scored = [m for m in resolved if m.scenarios]
    unscored_count = len(resolved) - len(scored)
    n = len(scored)

    if n == 0:
        return {
            "n": 0, "empty": True, "too_small": False, "unscored": unscored_count,
            "mean_signed_gap_pct": None, "mean_abs_gap_pct": None,
            "directional_hit_rate": None,
        }
    if n < _SMALL_CORPUS_THRESHOLD:
        return {
            "n": n, "empty": False, "too_small": True, "unscored": unscored_count,
            "mean_signed_gap_pct": None, "mean_abs_gap_pct": None,
            "directional_hit_rate": None,
        }
    gaps: list[float] = []
    hits = 0
    for memo in scored:
        stated = sum(s.probability * s.return_pct for s in memo.scenarios)
        realized = memo.resolution.realized_return_pct
        gaps.append(stated - realized)
        if (stated > 0) == (realized > 0):
            hits += 1
    return {
        "n": n, "empty": False, "too_small": False, "unscored": unscored_count,
        "mean_signed_gap_pct": _mean(gaps),
        "mean_abs_gap_pct": _mean([abs(g) for g in gaps]),
        "directional_hit_rate": hits / n,
    }


def _build_bought_vs_passed(resolved: list[Any], research_journal) -> dict[str, Any]:
    opened_memo_ids = {
        ev["payload"]["memo_id"] for ev in research_journal.read_events()
        if ev["kind"] == events.KIND_RESEARCH_POSITION_OPENED
    }
    bought = [m for m in resolved if m.memo_id in opened_memo_ids]
    passed = [m for m in resolved if m.memo_id not in opened_memo_ids]
    return {
        "n_resolved": len(resolved),
        "bought": {
            "count": len(bought),
            "mean_realized_return_pct": _mean([m.resolution.realized_return_pct for m in bought]),
        },
        "passed": {
            "count": len(passed),
            "mean_realized_return_pct": _mean([m.resolution.realized_return_pct for m in passed]),
        },
    }


def _series_view(snapshots_in_window: list[dict[str, Any]]) -> dict[str, Any]:
    first = snapshots_in_window[0]
    last = snapshots_in_window[-1]
    first_equity = float(first["equity"])
    last_equity = float(last["equity"])
    return_pct = (last_equity - first_equity) / first_equity if first_equity else None
    return {
        "first_at": first["at"], "first_equity": first_equity,
        "last_at": last["at"], "last_equity": last_equity,
        "return_pct": return_pct,
    }


def _build_sleeve_vs_baseline(research_journal, baseline_journal) -> dict[str, Any]:
    sleeve_snaps = sorted(
        (s for s in research_journal.read_equity_snapshots() if s["kind"] == "research_run"),
        key=lambda s: s["at"],
    )
    baseline_snaps = sorted(
        (s for s in baseline_journal.read_equity_snapshots() if s["kind"] == "baseline_run"),
        key=lambda s: s["at"],
    )
    if not sleeve_snaps or not baseline_snaps:
        return {"available": False, "window_start": None, "window_end": None,
                "sleeve": None, "baseline": None}

    window_start = max(sleeve_snaps[0]["at"], baseline_snaps[0]["at"])
    window_end = min(sleeve_snaps[-1]["at"], baseline_snaps[-1]["at"])
    if window_start > window_end:
        return {"available": False, "window_start": None, "window_end": None,
                "sleeve": None, "baseline": None}

    sleeve_in_window = [s for s in sleeve_snaps if window_start <= s["at"] <= window_end]
    baseline_in_window = [s for s in baseline_snaps if window_start <= s["at"] <= window_end]
    if not sleeve_in_window or not baseline_in_window:
        return {"available": False, "window_start": None, "window_end": None,
                "sleeve": None, "baseline": None}

    return {
        "available": True,
        "window_start": window_start,
        "window_end": window_end,
        "sleeve": _series_view(sleeve_in_window),
        "baseline": _series_view(baseline_in_window),
    }


def _build_per_model(resolved: list[Any]) -> dict[str, Any]:
    models: dict[str, dict[str, Any]] = {}
    for memo in resolved:
        label = _model_label(memo.authored_by_model)
        entry = models.setdefault(
            label, {"returns": [], "outcome_counts": dict.fromkeys(_OUTCOME_LABELS, 0)}
        )
        entry["returns"].append(memo.resolution.realized_return_pct)
        entry["outcome_counts"][memo.resolution.outcome_label] += 1

    rendered = {
        label: {
            "count": len(data["returns"]),
            "mean_realized_return_pct": _mean(data["returns"]),
            "outcome_counts": data["outcome_counts"],
        }
        for label, data in sorted(models.items())
    }
    return {"n_resolved": len(resolved), "models": rendered}


def build_report(
    *, memo_store, research_journal, baseline_journal, now: datetime | None = None,
) -> dict[str, Any]:
    """Assemble the calibration report dict from the memo store + journals
    alone. Pure aggregation: no network, no broker, no quotes, no LLM — see
    module docstring.
    """
    when = now if now is not None else datetime.now(timezone.utc)
    all_memos = memo_store.list()
    resolved = memo_store.resolved_corpus()

    return {
        "generated_at": when,
        "corpus": _build_corpus_section(all_memos),
        "outcome_matrix": _build_outcome_matrix(resolved),
        "scenario_calibration": _build_scenario_calibration(resolved),
        "bought_vs_passed": _build_bought_vs_passed(resolved, research_journal),
        "sleeve_vs_baseline": _build_sleeve_vs_baseline(research_journal, baseline_journal),
        "per_model": _build_per_model(resolved),
    }


def _pct(value: float | None) -> str:
    return f"{value:+.2%}" if value is not None else "n/a"


def _fmt_dt(value: datetime | None) -> str:
    return value.isoformat() if value is not None else "n/a"


_OUTCOME_TITLES = {
    "thesis_right_made_money": "Thesis right, made money",
    "thesis_right_lost_money": "Thesis right, lost money",
    "thesis_wrong_made_money": "Thesis wrong, made money (luck)",
    "thesis_wrong_lost_money": "Thesis wrong, lost money",
}


def _format_corpus(section: dict[str, Any]) -> list[str]:
    lines = ["## 1. Corpus"]
    if section["total"] == 0:
        lines.append("no data yet")
        return lines
    lines.append("")
    lines.append("| Status | Count |")
    lines.append("|---|---|")
    for status, count in section["by_status"].items():
        lines.append(f"| {status} | {count} |")
    lines.append("")
    lines.append(
        "By thesis type: "
        + ", ".join(f"{k} {v}" for k, v in section["by_thesis_type"].items())
    )
    lines.append(
        "By conviction tier: "
        + ", ".join(f"{k} {v}" for k, v in section["by_conviction_tier"].items())
    )
    lines.append(f"Oldest memo: {_fmt_dt(section['oldest_memo_at'])}")
    lines.append(f"Newest memo: {_fmt_dt(section['newest_memo_at'])}")
    return lines


def _format_outcome_matrix(section: dict[str, Any]) -> list[str]:
    lines = ["## 2. Outcome 2x2"]
    if section["n"] == 0:
        lines.append("no data yet")
        return lines
    cells = section["cells"]
    lines.append("")
    lines.append(f"Resolved memos: {section['n']}")
    lines.append("")
    lines.append("| | Made money | Lost money |")
    lines.append("|---|---|---|")
    for row_label, made_key, lost_key in (
        ("Thesis right", "thesis_right_made_money", "thesis_right_lost_money"),
        ("Thesis wrong", "thesis_wrong_made_money", "thesis_wrong_lost_money"),
    ):
        made, lost = cells[made_key], cells[lost_key]
        lines.append(
            f"| {row_label} "
            f"| {made['count']} ({_pct(made['mean_realized_return_pct'])}) "
            f"| {lost['count']} ({_pct(lost['mean_realized_return_pct'])}) |"
        )
    luck = cells["thesis_wrong_made_money"]
    lines.append("")
    lines.append(
        f"Off-diagonal (luck, not skill): thesis_wrong_made_money — "
        f"{luck['count']} memo(s), mean realized {_pct(luck['mean_realized_return_pct'])}"
    )
    return lines


def _format_scenario_calibration(section: dict[str, Any]) -> list[str]:
    lines = ["## 3. Scenario calibration"]
    if section["empty"]:
        lines.append("no data yet")
        if section["unscored"] > 0:
            lines.append(f"unscored (no stated scenarios): {section['unscored']}")
        return lines
    if section["too_small"]:
        lines.append(f"corpus too small (n={section['n']} < 5) — numbers are noise")
        if section["unscored"] > 0:
            lines.append(f"unscored (no stated scenarios): {section['unscored']}")
        return lines
    lines.append("")
    lines.append(f"Resolved memos: {section['n']}")
    lines.append(f"Mean signed gap (stated - realized): {_pct(section['mean_signed_gap_pct'])}")
    lines.append(f"Mean absolute gap: {_pct(section['mean_abs_gap_pct'])}")
    lines.append(
        f"Directional hit rate (stated-positive vs realized-positive agreement): "
        f"{section['directional_hit_rate']:.0%}"
    )
    if section["unscored"] > 0:
        lines.append(f"unscored (no stated scenarios): {section['unscored']}")
    return lines


def _format_bought_vs_passed(section: dict[str, Any]) -> list[str]:
    lines = ["## 4. Bought vs passed"]
    if section["n_resolved"] == 0:
        lines.append("no data yet")
        return lines
    lines.append("")
    lines.append("| Group | Count | Mean realized return |")
    lines.append("|---|---|---|")
    for label, key in (("Bought", "bought"), ("Passed (shadow-tracked)", "passed")):
        group = section[key]
        lines.append(
            f"| {label} | {group['count']} | {_pct(group['mean_realized_return_pct'])} |"
        )
    return lines


def _format_sleeve_vs_baseline(section: dict[str, Any]) -> list[str]:
    lines = ["## 5. Sleeve vs baseline"]
    if not section["available"]:
        lines.append("no data yet")
        return lines
    lines.append("")
    lines.append(
        f"Overlapping window: {_fmt_dt(section['window_start'])} "
        f"to {_fmt_dt(section['window_end'])}"
    )
    lines.append("")
    lines.append("| Series | First equity | Last equity | Return |")
    lines.append("|---|---|---|---|")
    for label, key in (("Sleeve (research)", "sleeve"), ("Baseline", "baseline")):
        s = section[key]
        lines.append(
            f"| {label} | ${s['first_equity']:.2f} | ${s['last_equity']:.2f} "
            f"| {_pct(s['return_pct'])} |"
        )
    return lines


def _format_per_model(section: dict[str, Any]) -> list[str]:
    lines = ["## 6. Per-model attribution"]
    if section["n_resolved"] == 0:
        lines.append("no data yet")
        return lines
    lines.append("")
    lines.append(
        "| Model | Count | Mean realized return | Right+Money | Right+Lost "
        "| Wrong+Money (luck) | Wrong+Lost |"
    )
    lines.append("|---|---|---|---|---|---|---|")
    for label, stats in section["models"].items():
        oc = stats["outcome_counts"]
        lines.append(
            f"| {label} | {stats['count']} | {_pct(stats['mean_realized_return_pct'])} "
            f"| {oc['thesis_right_made_money']} | {oc['thesis_right_lost_money']} "
            f"| {oc['thesis_wrong_made_money']} | {oc['thesis_wrong_lost_money']} |"
        )
    return lines


def format_report(report: dict[str, Any]) -> str:
    """Markdown rendering of build_report's dict (the CLI's only job)."""
    lines = [
        "# Research calibration report",
        f"Generated: {_fmt_dt(report['generated_at'])}",
        "",
    ]
    lines += _format_corpus(report["corpus"])
    lines.append("")
    lines += _format_outcome_matrix(report["outcome_matrix"])
    lines.append("")
    lines += _format_scenario_calibration(report["scenario_calibration"])
    lines.append("")
    lines += _format_bought_vs_passed(report["bought_vs_passed"])
    lines.append("")
    lines += _format_sleeve_vs_baseline(report["sleeve_vs_baseline"])
    lines.append("")
    lines += _format_per_model(report["per_model"])
    return "\n".join(lines)
