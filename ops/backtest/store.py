"""Versioned SQLite store for the backtest and learning loop.

This database is intentionally separate from both the live memo corpus and
the live journals.  All connections enable foreign keys and use explicit
transactions; payload JSON is canonical so hashes and rerenders remain stable.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

from ops.backtest.models import (
    MIN_BACKTEST_CUTOFF,
    BacktestCase,
    CaseSource,
    ContextExclusion,
    ContextItem,
    ContextManifest,
    ExperimentRecord,
    Lesson,
    ThesisAssessment,
    ThesisCorrectness,
    canonical_json,
    enforce_cutoff,
    stable_hash,
)

SCHEMA_VERSION = 3
BUSY_TIMEOUT_MS = 5_000


class SchemaVersionError(RuntimeError):
    """The database schema is newer than this application understands."""


class CaseConflictError(ValueError):
    """The same stable case identity was presented with different content."""


_SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS schema_metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cutoff_probes (
    probe_id TEXT PRIMARY KEY,
    model_id TEXT NOT NULL,
    tested_cutoff TEXT NOT NULL,
    prompts_json TEXT NOT NULL,
    responses_json TEXT NOT NULL,
    rubric_json TEXT NOT NULL,
    contaminated INTEGER NOT NULL CHECK (contaminated IN (0, 1)),
    recommended_cutoff TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cases (
    case_id TEXT PRIMARY KEY,
    sleeve TEXT NOT NULL,
    symbol TEXT NOT NULL,
    asof TEXT NOT NULL,
    trigger_json TEXT NOT NULL,
    source TEXT NOT NULL,
    score TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (sleeve, symbol, asof)
);
CREATE INDEX IF NOT EXISTS idx_cases_asof ON cases(asof, symbol);

CREATE TABLE IF NOT EXISTS context_manifests (
    manifest_id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL UNIQUE REFERENCES cases(case_id) ON DELETE CASCADE,
    asof TEXT NOT NULL,
    manifest_hash TEXT NOT NULL UNIQUE,
    manifest_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS generation_jobs (
    generation_key TEXT PRIMARY KEY,
    case_id TEXT NOT NULL REFERENCES cases(case_id) ON DELETE CASCADE,
    status TEXT NOT NULL CHECK (status IN ('pending', 'running', 'complete', 'failed')),
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    claimed_at TEXT,
    completed_at TEXT,
    last_error TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_generation_jobs_queue
    ON generation_jobs(status, created_at, generation_key);

CREATE TABLE IF NOT EXISTS frozen_memos (
    memo_key TEXT PRIMARY KEY,
    case_id TEXT NOT NULL REFERENCES cases(case_id) ON DELETE CASCADE,
    manifest_id TEXT REFERENCES context_manifests(manifest_id) ON DELETE RESTRICT,
    brain_version TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    evidence_model_id TEXT NOT NULL,
    thesis_model_id TEXT NOT NULL,
    context_hash TEXT NOT NULL,
    lesson_fingerprint TEXT NOT NULL,
    conditioning_hash TEXT NOT NULL,
    recommendation TEXT NOT NULL,
    conviction TEXT,
    guardrail_status TEXT NOT NULL,
    guardrail_reason TEXT,
    memo_json TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (
        case_id, brain_version, prompt_version, evidence_model_id,
        thesis_model_id, context_hash, lesson_fingerprint
    )
);

CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    sleeve TEXT NOT NULL,
    start_date TEXT NOT NULL,
    end_date TEXT NOT NULL,
    benchmark TEXT NOT NULL,
    settings_json TEXT NOT NULL,
    settings_hash TEXT NOT NULL,
    resolved_config_json TEXT NOT NULL,
    metadata_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('planned', 'running', 'complete', 'failed')),
    created_at TEXT NOT NULL,
    completed_at TEXT
);

CREATE TABLE IF NOT EXISTS run_cases (
    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    case_id TEXT NOT NULL REFERENCES cases(case_id) ON DELETE RESTRICT,
    ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
    PRIMARY KEY (run_id, case_id),
    UNIQUE (run_id, ordinal)
);

CREATE TABLE IF NOT EXISTS decisions (
    decision_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    case_id TEXT NOT NULL,
    sequence INTEGER NOT NULL CHECK (sequence >= 0),
    observed_session TEXT NOT NULL,
    action TEXT NOT NULL CHECK (action IN ('BUY', 'PASS', 'HOLD', 'SELL')),
    observed_price TEXT,
    reason TEXT NOT NULL,
    settings_hash TEXT NOT NULL,
    memo_key TEXT REFERENCES frozen_memos(memo_key) ON DELETE RESTRICT,
    metadata_json TEXT NOT NULL,
    FOREIGN KEY (run_id, case_id) REFERENCES run_cases(run_id, case_id) ON DELETE CASCADE,
    UNIQUE (run_id, case_id, sequence)
);

CREATE TABLE IF NOT EXISTS executions (
    execution_id TEXT PRIMARY KEY,
    decision_id TEXT NOT NULL REFERENCES decisions(decision_id) ON DELETE CASCADE,
    run_id TEXT NOT NULL,
    case_id TEXT NOT NULL,
    session TEXT NOT NULL,
    side TEXT NOT NULL CHECK (side IN ('BUY', 'SELL')),
    price TEXT NOT NULL,
    quantity TEXT NOT NULL,
    notional TEXT NOT NULL,
    FOREIGN KEY (run_id, case_id) REFERENCES run_cases(run_id, case_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS falsifier_observations (
    run_id TEXT NOT NULL,
    case_id TEXT NOT NULL,
    decision_id TEXT NOT NULL REFERENCES decisions(decision_id) ON DELETE CASCADE,
    session TEXT NOT NULL,
    falsifier_index INTEGER NOT NULL CHECK (falsifier_index >= 0),
    name TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    observed TEXT,
    detail TEXT NOT NULL,
    FOREIGN KEY (run_id, case_id) REFERENCES run_cases(run_id, case_id) ON DELETE CASCADE,
    PRIMARY KEY (decision_id, session, falsifier_index)
);

CREATE TABLE IF NOT EXISTS horizon_outcomes (
    run_id TEXT NOT NULL,
    case_id TEXT NOT NULL,
    horizon_sessions INTEGER NOT NULL CHECK (horizon_sessions > 0),
    state TEXT NOT NULL CHECK (state IN ('mature', 'pending', 'unpriceable')),
    label TEXT NOT NULL CHECK (label IN ('win', 'wash', 'loss', 'pending', 'unpriceable')),
    stock_return TEXT,
    benchmark_return TEXT,
    excess_return TEXT,
    utility TEXT,
    entry_session TEXT,
    horizon_session TEXT,
    detail TEXT NOT NULL,
    FOREIGN KEY (run_id, case_id) REFERENCES run_cases(run_id, case_id) ON DELETE CASCADE,
    PRIMARY KEY (run_id, case_id, horizon_sessions)
);

CREATE TABLE IF NOT EXISTS case_results (
    run_id TEXT NOT NULL,
    case_id TEXT NOT NULL,
    initial_action TEXT NOT NULL CHECK (initial_action IN ('BUY', 'PASS', 'HOLD', 'SELL')),
    status TEXT NOT NULL CHECK (status IN ('complete', 'pending', 'unpriceable', 'failed')),
    primary_horizon INTEGER NOT NULL CHECK (primary_horizon > 0),
    primary_label TEXT NOT NULL,
    actual_return TEXT,
    max_drawdown TEXT,
    exit_session TEXT,
    exit_reason TEXT,
    quadrant TEXT NOT NULL,
    FOREIGN KEY (run_id, case_id) REFERENCES run_cases(run_id, case_id) ON DELETE CASCADE,
    PRIMARY KEY (run_id, case_id)
);

CREATE TABLE IF NOT EXISTS thesis_assessments (
    assessment_key TEXT PRIMARY KEY,
    memo_key TEXT NOT NULL REFERENCES frozen_memos(memo_key) ON DELETE CASCADE,
    case_id TEXT NOT NULL REFERENCES cases(case_id) ON DELETE CASCADE,
    correctness TEXT NOT NULL CHECK (correctness IN ('right', 'wrong', 'indeterminate')),
    rationale TEXT NOT NULL,
    evidence_cutoff TEXT NOT NULL,
    model_id TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (memo_key, evidence_cutoff, model_id, prompt_version)
);

CREATE TABLE IF NOT EXISTS lessons (
    lesson_id TEXT PRIMARY KEY,
    sleeve TEXT NOT NULL,
    text TEXT NOT NULL,
    eligible_from TEXT NOT NULL,
    fingerprint TEXT NOT NULL UNIQUE,
    tags_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS lesson_sources (
    lesson_id TEXT NOT NULL REFERENCES lessons(lesson_id) ON DELETE CASCADE,
    case_id TEXT NOT NULL REFERENCES cases(case_id) ON DELETE RESTRICT,
    assessment_key TEXT REFERENCES thesis_assessments(assessment_key) ON DELETE RESTRICT,
    PRIMARY KEY (lesson_id, case_id)
);

CREATE TABLE IF NOT EXISTS experiments (
    experiment_id TEXT PRIMARY KEY,
    sleeve TEXT NOT NULL,
    seed INTEGER NOT NULL,
    lesson_fingerprint TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('planned', 'running', 'complete', 'failed')),
    control_metrics_json TEXT NOT NULL,
    treated_metrics_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    completed_at TEXT
);

CREATE TABLE IF NOT EXISTS experiment_cases (
    experiment_id TEXT NOT NULL REFERENCES experiments(experiment_id) ON DELETE CASCADE,
    case_id TEXT NOT NULL REFERENCES cases(case_id) ON DELETE RESTRICT,
    ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
    control_memo_key TEXT REFERENCES frozen_memos(memo_key) ON DELETE RESTRICT,
    treated_memo_key TEXT REFERENCES frozen_memos(memo_key) ON DELETE RESTRICT,
    PRIMARY KEY (experiment_id, case_id),
    UNIQUE (experiment_id, ordinal)
);
"""

_SCHEMA_V2 = """
CREATE TABLE IF NOT EXISTS distillation_runs (
    distillation_key TEXT PRIMARY KEY,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS lesson_distillations (
    distillation_key TEXT NOT NULL
        REFERENCES distillation_runs(distillation_key) ON DELETE CASCADE,
    lesson_id TEXT NOT NULL REFERENCES lessons(lesson_id) ON DELETE CASCADE,
    ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
    PRIMARY KEY (distillation_key, lesson_id),
    UNIQUE (distillation_key, ordinal)
);

CREATE TABLE IF NOT EXISTS lesson_assessments (
    lesson_id TEXT NOT NULL REFERENCES lessons(lesson_id) ON DELETE CASCADE,
    assessment_key TEXT NOT NULL
        REFERENCES thesis_assessments(assessment_key) ON DELETE RESTRICT,
    PRIMARY KEY (lesson_id, assessment_key)
);
"""

def _parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"stored datetime is not timezone-aware: {value!r}")
    return parsed


class BacktestStore:
    """Transactional owner of one isolated ``backtest.sqlite`` database."""

    def __init__(
        self,
        path: str | Path,
        *,
        cutoff: date = MIN_BACKTEST_CUTOFF,
        busy_timeout_ms: int = BUSY_TIMEOUT_MS,
    ) -> None:
        enforce_cutoff(cutoff, cutoff)
        if busy_timeout_ms < 0:
            raise ValueError("busy_timeout_ms must be nonnegative")
        self._path = str(path)
        self._cutoff = cutoff
        self._lock = threading.RLock()
        self._savepoint_counter = 0
        if self._path != ":memory:":
            Path(self._path).expanduser().parent.mkdir(parents=True, exist_ok=True)
            self._path = str(Path(self._path).expanduser())
        self._conn = sqlite3.connect(
            self._path, isolation_level=None, check_same_thread=False,
        )
        self._conn.row_factory = sqlite3.Row
        try:
            self._conn.execute("PRAGMA foreign_keys = ON")
            self._conn.execute(f"PRAGMA busy_timeout = {int(busy_timeout_ms)}")
            if self._path != ":memory:":
                self._conn.execute("PRAGMA journal_mode = WAL")
            self._migrate()
        except Exception:
            self._conn.close()
            raise

    @property
    def path(self) -> str:
        return self._path

    @property
    def cutoff(self) -> date:
        """Configured cutoff before sealed probe advances are applied."""
        return self._cutoff

    @property
    def effective_cutoff(self) -> date:
        """Configured cutoff advanced by the latest contaminated probe.

        A schema/query failure is deliberately not swallowed: proceeding with
        only the configured date would reopen cases a sealed probe ruled out.
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT recommended_cutoff FROM cutoff_probes "
                "WHERE contaminated = 1 "
                "ORDER BY created_at DESC, probe_id DESC LIMIT 1"
            ).fetchone()
        if row is None:
            return self._cutoff
        return max(self._cutoff, date.fromisoformat(row["recommended_cutoff"]))

    @property
    def schema_version(self) -> int:
        with self._lock:
            return int(self._conn.execute("PRAGMA user_version").fetchone()[0])

    @property
    def foreign_keys_enabled(self) -> bool:
        with self._lock:
            return bool(self._conn.execute("PRAGMA foreign_keys").fetchone()[0])

    def _migrate(self) -> None:
        with self._lock:
            current = int(self._conn.execute("PRAGMA user_version").fetchone()[0])
            if current > SCHEMA_VERSION:
                raise SchemaVersionError(
                    f"backtest schema {current} is newer than supported {SCHEMA_VERSION}"
                )
            if current == 0:
                # sqlite3.executescript commits any pending transaction, so
                # bracket the migration in the script itself rather than the
                # public transaction() helper.
                self._conn.executescript(
                    "BEGIN IMMEDIATE;\n"
                    + _SCHEMA_V1
                    + "\nINSERT OR REPLACE INTO schema_metadata (key, value) "
                    + "VALUES ('schema_version', '1');\n"
                    + "PRAGMA user_version = 1;\nCOMMIT;"
                )
                current = 1
            if current == 1:
                self._conn.executescript(
                    "BEGIN IMMEDIATE;\n"
                    + _SCHEMA_V2
                    + "\nINSERT OR REPLACE INTO schema_metadata (key, value) "
                    + "VALUES ('schema_version', '2');\n"
                    + "PRAGMA user_version = 2;\nCOMMIT;"
                )
                current = 2
            if current == 2:
                columns = {
                    row[1]
                    for row in self._conn.execute(
                        "PRAGMA table_info(generation_jobs)"
                    ).fetchall()
                }
                self._conn.execute("BEGIN IMMEDIATE")
                try:
                    if "request_json" not in columns:
                        self._conn.execute(
                            "ALTER TABLE generation_jobs ADD COLUMN request_json TEXT"
                        )
                    if "auto_run" not in columns:
                        self._conn.execute(
                            "ALTER TABLE generation_jobs ADD COLUMN auto_run INTEGER "
                            "NOT NULL DEFAULT 0 CHECK (auto_run IN (0, 1))"
                        )
                    self._conn.execute(
                        "CREATE INDEX IF NOT EXISTS idx_generation_jobs_auto_queue "
                        "ON generation_jobs(auto_run, status, created_at, generation_key)"
                    )
                    self._conn.execute(
                        "INSERT OR REPLACE INTO schema_metadata (key, value) "
                        "VALUES ('schema_version', '3')"
                    )
                    self._conn.execute("PRAGMA user_version = 3")
                    self._conn.execute("COMMIT")
                except BaseException:
                    self._conn.execute("ROLLBACK")
                    raise
            columns = {
                row[1]
                for row in self._conn.execute(
                    "PRAGMA table_info(falsifier_observations)"
                ).fetchall()
            }
            if "name" not in columns:
                self._conn.execute(
                    "ALTER TABLE falsifier_observations "
                    "ADD COLUMN name TEXT NOT NULL DEFAULT ''"
                )

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Yield a connection inside an immediate, nestable transaction."""
        with self._lock:
            if self._conn.in_transaction:
                self._savepoint_counter += 1
                name = f"backtest_sp_{self._savepoint_counter}"
                self._conn.execute(f"SAVEPOINT {name}")
                try:
                    yield self._conn
                except BaseException:
                    self._conn.execute(f"ROLLBACK TO {name}")
                    self._conn.execute(f"RELEASE {name}")
                    raise
                else:
                    self._conn.execute(f"RELEASE {name}")
                return
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute("COMMIT")

    def insert_case(self, case: BacktestCase) -> BacktestCase:
        """Insert a case idempotently after enforcing the effective cutoff."""
        case.validate_cutoff(self.effective_cutoff)
        values = (
            case.case_id,
            case.sleeve,
            case.symbol,
            case.asof.isoformat(),
            canonical_json(case.trigger),
            case.source.value,
            str(case.score) if case.score is not None else None,
            case.created_at.isoformat(),
        )
        with self.transaction() as conn:
            existing = conn.execute(
                "SELECT * FROM cases WHERE case_id = ? OR "
                "(sleeve = ? AND symbol = ? AND asof = ?)",
                (case.case_id, case.sleeve, case.symbol, case.asof.isoformat()),
            ).fetchone()
            if existing is not None:
                stored = self._case_from_row(existing)
                if self._case_content(stored) != self._case_content(case):
                    raise CaseConflictError(
                        f"case identity {case.sleeve}/{case.symbol}/{case.asof} "
                        "already exists with different content"
                    )
                return stored
            conn.execute(
                """
                INSERT INTO cases (
                    case_id, sleeve, symbol, asof, trigger_json, source, score, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                values,
            )
        return case

    @staticmethod
    def _case_content(case: BacktestCase) -> str:
        return canonical_json({
            "case_id": case.case_id,
            "sleeve": case.sleeve,
            "symbol": case.symbol,
            "asof": case.asof,
            "trigger": case.trigger,
            "source": case.source,
            "score": case.score,
        })

    @staticmethod
    def _case_from_row(row: sqlite3.Row) -> BacktestCase:
        return BacktestCase(
            case_id=row["case_id"],
            sleeve=row["sleeve"],
            symbol=row["symbol"],
            asof=date.fromisoformat(row["asof"]),
            trigger=json.loads(row["trigger_json"]),
            source=CaseSource(row["source"]),
            score=Decimal(row["score"]) if row["score"] is not None else None,
            created_at=_parse_utc(row["created_at"]),
        )

    def get_case(self, case_id: str) -> BacktestCase | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM cases WHERE case_id = ?", (case_id,),
            ).fetchone()
        return self._case_from_row(row) if row is not None else None

    def list_cases(self, *, sleeve: str | None = None) -> list[BacktestCase]:
        sql = "SELECT * FROM cases"
        params: tuple[str, ...] = ()
        if sleeve is not None:
            sql += " WHERE sleeve = ?"
            params = (sleeve.strip().lower(),)
        sql += " ORDER BY asof, symbol, case_id"
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [self._case_from_row(row) for row in rows]

    def validate_cases_for_replay(self, case_ids: Sequence[str] | None = None) -> None:
        """Re-check persisted dates against the current, possibly advanced cutoff."""
        if case_ids is None:
            cases = self.list_cases()
        else:
            cases = []
            for case_id in case_ids:
                case = self.get_case(case_id)
                if case is None:
                    raise KeyError(f"no backtest case with id {case_id!r}")
                cases.append(case)
        for case in cases:
            case.validate_cutoff(self.effective_cutoff)

    def save_context_manifest(self, manifest: ContextManifest) -> ContextManifest:
        manifest.validate_point_in_time()
        case = self.get_case(manifest.case_id)
        if case is None:
            raise KeyError(f"no backtest case with id {manifest.case_id!r}")
        if manifest.asof != case.asof:
            raise ValueError(
                f"manifest asof {manifest.asof} does not match case asof {case.asof}"
            )
        payload = canonical_json(manifest)
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM context_manifests WHERE case_id = ?",
                (manifest.case_id,),
            ).fetchone()
            if row is not None:
                if row["manifest_hash"] != manifest.manifest_hash:
                    raise CaseConflictError(
                        f"case {manifest.case_id} already has a different frozen manifest"
                    )
                return self._manifest_from_json(row["manifest_json"])
            conn.execute(
                """
                INSERT INTO context_manifests (
                    manifest_id, case_id, asof, manifest_hash, manifest_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    manifest.manifest_id, manifest.case_id, manifest.asof.isoformat(),
                    manifest.manifest_hash, payload, manifest.created_at.isoformat(),
                ),
            )
        return manifest

    @staticmethod
    def _manifest_from_json(raw: str) -> ContextManifest:
        payload = json.loads(raw)
        included = tuple(
            ContextItem(
                item_id=item["item_id"], kind=item["kind"],
                source_ref=item["source_ref"],
                available_at=date.fromisoformat(item["available_at"]),
                content=item["content"], content_hash=item["content_hash"],
                metadata=item.get("metadata", {}),
            )
            for item in payload["included"]
        )
        excluded = tuple(
            ContextExclusion(
                kind=item["kind"], source_ref=item["source_ref"],
                reason=item["reason"],
                available_at=(date.fromisoformat(item["available_at"])
                              if item.get("available_at") else None),
            )
            for item in payload["excluded"]
        )
        return ContextManifest(
            manifest_id=payload["manifest_id"], case_id=payload["case_id"],
            asof=date.fromisoformat(payload["asof"]), included=included,
            excluded=excluded, substitutions=tuple(payload["substitutions"]),
            manifest_hash=payload["manifest_hash"],
            created_at=_parse_utc(payload["created_at"]),
        )

    def get_context_manifest(self, case_id: str) -> ContextManifest | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT manifest_json FROM context_manifests WHERE case_id = ?",
                (case_id,),
            ).fetchone()
        return self._manifest_from_json(row[0]) if row is not None else None

    # --- Frozen generation queue -----------------------------------------

    def get_frozen_memo(self, memo_key: str):
        """Return one terminal generation artifact, if it exists."""
        from ops.backtest.generate import FrozenMemoRecord

        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM frozen_memos WHERE memo_key = ?", (memo_key,),
            ).fetchone()
        if row is None:
            return None
        return FrozenMemoRecord(
            memo_key=row["memo_key"], case_id=row["case_id"],
            manifest_id=row["manifest_id"], brain_version=row["brain_version"],
            prompt_version=row["prompt_version"],
            evidence_model_id=row["evidence_model_id"],
            thesis_model_id=row["thesis_model_id"],
            context_hash=row["context_hash"],
            lesson_fingerprint=row["lesson_fingerprint"],
            conditioning_hash=row["conditioning_hash"],
            recommendation=row["recommendation"], conviction=row["conviction"],
            guardrail_status=row["guardrail_status"],
            guardrail_reason=row["guardrail_reason"], memo_json=row["memo_json"],
            created_at=_parse_utc(row["created_at"]),
        )

    def frozen_memo_for_case(self, case_id: str):
        """Newest frozen artifact for a case, with a stable key tie-break."""
        with self._lock:
            row = self._conn.execute(
                "SELECT memo_key FROM frozen_memos WHERE case_id = ? "
                "ORDER BY created_at DESC, memo_key DESC LIMIT 1",
                (case_id,),
            ).fetchone()
        return self.get_frozen_memo(row[0]) if row is not None else None

    def ensure_generation_job(self, request) -> None:
        """Persist a missing generation request without resetting live work."""
        request.case.validate_cutoff(self.effective_cutoff)
        if request.generation_key != request.memo_key:
            raise ValueError("generation_key and memo_key must match in v1")
        if request.manifest.case_id != request.case.case_id:
            raise ValueError("generation request manifest/case mismatch")
        self.insert_case(request.case)
        self.save_context_manifest(request.manifest)
        request_json = canonical_json({
            "brain_version": request.brain_version,
            "prompt_version": request.prompt_version,
            "evidence_model_id": request.evidence_model_id,
            "thesis_model_id": request.thesis_model_id,
            "lesson_fingerprint": request.lesson_fingerprint,
            "conditioning": request.conditioning,
            "hit_payload": request.hit_payload,
        })
        with self.transaction() as conn:
            frozen = conn.execute(
                "SELECT 1 FROM frozen_memos WHERE memo_key = ?", (request.memo_key,),
            ).fetchone()
            if frozen is not None:
                return
            row = conn.execute(
                "SELECT case_id, request_json FROM generation_jobs "
                "WHERE generation_key = ?",
                (request.generation_key,),
            ).fetchone()
            if row is not None:
                if row["case_id"] != request.case.case_id:
                    raise CaseConflictError(
                        f"generation key {request.generation_key} belongs to another case"
                    )
                if row["request_json"] is None:
                    conn.execute(
                        "UPDATE generation_jobs SET request_json = ? "
                        "WHERE generation_key = ?",
                        (request_json, request.generation_key),
                    )
                elif row["request_json"] != request_json:
                    raise CaseConflictError(
                        f"generation key {request.generation_key} has different request data"
                    )
                return
            conn.execute(
                "INSERT INTO generation_jobs "
                "(generation_key, case_id, status, request_json, created_at) "
                "VALUES (?, ?, 'pending', ?, ?)",
                (
                    request.generation_key, request.case.case_id,
                    request_json,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )

    def enqueue_generation_jobs(self, generation_keys: Sequence[str]) -> int:
        """Opt pending jobs into automatic background processing."""
        keys = tuple(dict.fromkeys(generation_keys))
        if not keys:
            return 0
        placeholders = ",".join("?" for _ in keys)
        with self.transaction() as conn:
            known = int(conn.execute(
                f"SELECT COUNT(*) FROM generation_jobs "
                f"WHERE generation_key IN ({placeholders})",
                keys,
            ).fetchone()[0])
            if known != len(keys):
                raise KeyError("one or more generation jobs do not exist")
            cursor = conn.execute(
                f"UPDATE generation_jobs SET auto_run = 1 "
                f"WHERE generation_key IN ({placeholders}) "
                "AND status IN ('pending', 'running')",
                keys,
            )
            return cursor.rowcount

    def queued_generation_requests(self, *, auto_only: bool = False):
        """Rehydrate durable requests so a daemon can resume after restart."""
        from ops.backtest.generate import GenerationRequest

        where = "AND j.auto_run = 1" if auto_only else ""
        with self._lock:
            rows = self._conn.execute(
                "SELECT j.generation_key, j.request_json, j.case_id "
                "FROM generation_jobs AS j JOIN cases AS c ON c.case_id = j.case_id "
                "WHERE j.status IN ('pending', 'running') " + where + " "
                "ORDER BY c.asof, c.created_at, c.symbol, j.generation_key"
            ).fetchall()
        requests = []
        for row in rows:
            if row["request_json"] is None:
                continue
            case = self.get_case(row["case_id"])
            manifest = self.get_context_manifest(row["case_id"])
            if case is None or manifest is None:
                raise CaseConflictError(
                    f"queued generation {row['generation_key']} lost its frozen inputs"
                )
            payload = json.loads(row["request_json"])
            request = GenerationRequest.create(
                case=case,
                manifest=manifest,
                brain_version=payload["brain_version"],
                prompt_version=payload["prompt_version"],
                evidence_model_id=payload["evidence_model_id"],
                thesis_model_id=payload["thesis_model_id"],
                lesson_fingerprint=payload["lesson_fingerprint"],
                conditioning=payload.get("conditioning", {}),
                hit_payload=payload.get("hit_payload", {}),
            )
            if request.generation_key != row["generation_key"]:
                raise CaseConflictError(
                    f"queued generation {row['generation_key']} no longer hashes identically"
                )
            requests.append(request)
        return tuple(requests)

    def requeue_stale_generation_jobs(self, *, stale_before: datetime) -> int:
        if stale_before.tzinfo is None or stale_before.utcoffset() is None:
            raise ValueError("stale_before must be timezone-aware")
        with self.transaction() as conn:
            cursor = conn.execute(
                "UPDATE generation_jobs SET status = 'pending', claimed_at = NULL, "
                "last_error = 'stale claim requeued' "
                "WHERE status = 'running' AND claimed_at < ?",
                (stale_before.astimezone(timezone.utc).isoformat(),),
            )
            return cursor.rowcount

    def claim_next_generation_job(self, *, auto_only: bool = False):
        """Atomically claim the oldest pending case for resumable generation."""
        from ops.backtest.generate import GenerationClaim

        with self.transaction() as conn:
            auto_clause = "AND j.auto_run = 1" if auto_only else ""
            row = conn.execute(
                "SELECT j.generation_key, j.case_id, j.attempt_count "
                "FROM generation_jobs AS j JOIN cases AS c ON c.case_id = j.case_id "
                "WHERE j.status = 'pending' " + auto_clause + " "
                "ORDER BY c.asof, c.created_at, c.symbol, j.generation_key LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            attempt = int(row["attempt_count"]) + 1
            cursor = conn.execute(
                "UPDATE generation_jobs SET status = 'running', attempt_count = ?, "
                "claimed_at = ?, last_error = NULL "
                "WHERE generation_key = ? AND status = 'pending'",
                (
                    attempt, datetime.now(timezone.utc).isoformat(),
                    row["generation_key"],
                ),
            )
            if cursor.rowcount != 1:
                return None
            return GenerationClaim(row["generation_key"], row["case_id"], attempt)

    def finish_generation_job(self, claim, record) -> None:
        """Atomically freeze the terminal artifact and complete its queue row."""
        if record.memo_key != claim.generation_key or record.case_id != claim.case_id:
            raise ValueError("generation result does not match its claim")
        with self.transaction() as conn:
            job = conn.execute(
                "SELECT status, attempt_count FROM generation_jobs WHERE generation_key = ?",
                (claim.generation_key,),
            ).fetchone()
            if job is None:
                raise KeyError(f"unknown generation job {claim.generation_key!r}")
            existing = conn.execute(
                "SELECT * FROM frozen_memos WHERE memo_key = ?", (record.memo_key,),
            ).fetchone()
            if existing is not None:
                if canonical_json(self.get_frozen_memo(record.memo_key)) != canonical_json(record):
                    raise CaseConflictError(
                        f"memo key {record.memo_key} already has different frozen content"
                    )
                if job["status"] != "complete":
                    conn.execute(
                        "UPDATE generation_jobs SET status = 'complete', auto_run = 0, "
                        "completed_at = ? "
                        "WHERE generation_key = ?",
                        (record.created_at.isoformat(), claim.generation_key),
                    )
                return
            if job["status"] != "running" or int(job["attempt_count"]) != claim.attempt_count:
                raise CaseConflictError("generation claim is stale or no longer running")
            conn.execute(
                """
                INSERT INTO frozen_memos (
                    memo_key, case_id, manifest_id, brain_version, prompt_version,
                    evidence_model_id, thesis_model_id, context_hash,
                    lesson_fingerprint, conditioning_hash, recommendation, conviction,
                    guardrail_status, guardrail_reason, memo_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.memo_key, record.case_id, record.manifest_id,
                    record.brain_version, record.prompt_version,
                    record.evidence_model_id, record.thesis_model_id,
                    record.context_hash, record.lesson_fingerprint,
                    record.conditioning_hash, record.recommendation, record.conviction,
                    record.guardrail_status, record.guardrail_reason, record.memo_json,
                    record.created_at.isoformat(),
                ),
            )
            conn.execute(
                "UPDATE generation_jobs SET status = 'complete', auto_run = 0, completed_at = ?, "
                "last_error = ? WHERE generation_key = ?",
                (
                    record.created_at.isoformat(), record.guardrail_reason,
                    claim.generation_key,
                ),
            )

    def requeue_generation_job(self, claim) -> None:
        """Return an interrupted live claim to pending without freezing it."""
        with self.transaction() as conn:
            cursor = conn.execute(
                "UPDATE generation_jobs SET status = 'pending', claimed_at = NULL, "
                "last_error = 'operator pause' "
                "WHERE generation_key = ? AND status = 'running' AND attempt_count = ?",
                (claim.generation_key, claim.attempt_count),
            )
            if cursor.rowcount != 1:
                raise CaseConflictError("generation claim is stale or no longer running")

    # --- Replay run persistence ------------------------------------------

    def create_run(
        self,
        *,
        run_id: str,
        sleeve: str,
        start_date: date,
        end_date: date,
        benchmark: str,
        settings: object,
        resolved_config: object,
        metadata: object,
        case_ids: Sequence[str],
        created_at: datetime,
    ) -> None:
        if not run_id.strip() or not sleeve.strip() or not benchmark.strip():
            raise ValueError("run_id, sleeve, and benchmark must not be empty")
        if end_date < start_date:
            raise ValueError("run end_date must not precede start_date")
        if created_at.tzinfo is None or created_at.utcoffset() is None:
            raise ValueError("created_at must be timezone-aware")
        if len(set(case_ids)) != len(case_ids):
            raise ValueError("run case_ids must be unique")
        self.validate_cases_for_replay(case_ids)
        settings_json = canonical_json(settings)
        values = (
            run_id, sleeve, start_date.isoformat(), end_date.isoformat(), benchmark,
            settings_json, stable_hash(settings), canonical_json(resolved_config),
            canonical_json(metadata), "running", created_at.isoformat(),
        )
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO runs (
                    run_id, sleeve, start_date, end_date, benchmark, settings_json,
                    settings_hash, resolved_config_json, metadata_json, status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                values,
            )
            conn.executemany(
                "INSERT INTO run_cases (run_id, case_id, ordinal) VALUES (?, ?, ?)",
                [(run_id, case_id, ordinal) for ordinal, case_id in enumerate(case_ids)],
            )

    def save_replay_evaluation(self, replay, outcomes, result) -> None:
        """Persist one complete case replay and its fixed-horizon verdicts."""
        if replay.run_id != result.run_id or replay.case_id != result.case_id:
            raise ValueError("replay/result identity mismatch")
        with self.transaction() as conn:
            conn.executemany(
                """
                INSERT INTO decisions (
                    decision_id, run_id, case_id, sequence, observed_session,
                    action, observed_price, reason, settings_hash, memo_key, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        item.decision_id, item.run_id, item.case_id, item.sequence,
                        item.observed_session.isoformat(), item.action.value,
                        str(item.observed_price) if item.observed_price is not None else None,
                        item.reason, item.settings_hash, item.memo_key,
                        canonical_json(item.metadata),
                    )
                    for item in replay.decisions
                ],
            )
            conn.executemany(
                """
                INSERT INTO executions (
                    execution_id, decision_id, run_id, case_id, session, side,
                    price, quantity, notional
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        item.execution_id, item.decision_id, item.run_id, item.case_id,
                        item.session.isoformat(), item.side.value, str(item.price),
                        str(item.quantity), str(item.notional),
                    )
                    for item in replay.executions
                ],
            )
            conn.executemany(
                """
                INSERT INTO falsifier_observations (
                    run_id, case_id, decision_id, session, falsifier_index,
                    name, status, observed, detail
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        item.run_id, item.case_id, item.decision_id,
                        item.session.isoformat(), item.falsifier_index,
                        item.name, item.status,
                        str(item.observed) if item.observed is not None else None,
                        item.detail,
                    )
                    for item in replay.falsifier_observations
                ],
            )
            conn.executemany(
                """
                INSERT INTO horizon_outcomes (
                    run_id, case_id, horizon_sessions, state, label, stock_return,
                    benchmark_return, excess_return, utility, entry_session,
                    horizon_session, detail
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        item.run_id, item.case_id, item.horizon_sessions,
                        item.state.value, item.label.value,
                        str(item.stock_return) if item.stock_return is not None else None,
                        (str(item.benchmark_return)
                         if item.benchmark_return is not None else None),
                        str(item.excess_return) if item.excess_return is not None else None,
                        str(item.utility) if item.utility is not None else None,
                        item.entry_session.isoformat() if item.entry_session else None,
                        item.horizon_session.isoformat() if item.horizon_session else None,
                        item.detail,
                    )
                    for item in outcomes
                ],
            )
            conn.execute(
                """
                INSERT INTO case_results (
                    run_id, case_id, initial_action, status, primary_horizon,
                    primary_label, actual_return, max_drawdown, exit_session,
                    exit_reason, quadrant
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    result.run_id, result.case_id, result.initial_action.value,
                    result.status, result.primary_horizon, result.primary_label.value,
                    str(result.actual_return) if result.actual_return is not None else None,
                    str(result.max_drawdown) if result.max_drawdown is not None else None,
                    result.exit_session.isoformat() if result.exit_session else None,
                    result.exit_reason, result.quadrant.value,
                ),
            )
            self._refresh_process_quadrants(
                conn, run_id=result.run_id, case_id=result.case_id,
            )

    def finish_run(self, run_id: str, *, status: str = "complete") -> None:
        if status not in {"complete", "failed"}:
            raise ValueError("terminal run status must be complete or failed")
        with self.transaction() as conn:
            cursor = conn.execute(
                "UPDATE runs SET status = ?, completed_at = ? WHERE run_id = ?",
                (status, datetime.now(timezone.utc).isoformat(), run_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"unknown backtest run {run_id!r}")

    # --- Learning caches -------------------------------------------------

    @staticmethod
    def _assessment_from_row(row: sqlite3.Row) -> ThesisAssessment:
        return ThesisAssessment(
            assessment_key=row["assessment_key"], memo_key=row["memo_key"],
            case_id=row["case_id"], correctness=ThesisCorrectness(row["correctness"]),
            rationale=row["rationale"],
            evidence_cutoff=date.fromisoformat(row["evidence_cutoff"]),
            model_id=row["model_id"], prompt_version=row["prompt_version"],
            evidence=tuple(json.loads(row["evidence_json"])),
            created_at=_parse_utc(row["created_at"]),
        )

    @staticmethod
    def _assessment_content(assessment: ThesisAssessment) -> str:
        return canonical_json({
            "assessment_key": assessment.assessment_key,
            "memo_key": assessment.memo_key,
            "case_id": assessment.case_id,
            "correctness": assessment.correctness,
            "rationale": assessment.rationale,
            "evidence_cutoff": assessment.evidence_cutoff,
            "model_id": assessment.model_id,
            "prompt_version": assessment.prompt_version,
            "evidence": assessment.evidence,
        })

    def get_thesis_assessment(self, assessment_key: str) -> ThesisAssessment | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM thesis_assessments WHERE assessment_key = ?",
                (assessment_key,),
            ).fetchone()
        return self._assessment_from_row(row) if row is not None else None

    @staticmethod
    def _refresh_process_quadrants(
        conn: sqlite3.Connection,
        *,
        run_id: str | None = None,
        memo_key: str | None = None,
        case_id: str | None = None,
    ) -> int:
        from ops.backtest.postmortem import process_quadrant

        clauses = ["d.sequence = 0", "d.memo_key IS NOT NULL"]
        params: list[str] = []
        if run_id is not None:
            clauses.append("cr.run_id = ?")
            params.append(run_id)
        if memo_key is not None:
            clauses.append("d.memo_key = ?")
            params.append(memo_key)
        if case_id is not None:
            clauses.append("cr.case_id = ?")
            params.append(case_id)
        rows = conn.execute(
            """
            SELECT cr.run_id, cr.case_id, cr.primary_label, cr.quadrant,
                   a.correctness
            FROM case_results AS cr
            JOIN decisions AS d
              ON d.run_id = cr.run_id AND d.case_id = cr.case_id
            JOIN thesis_assessments AS a
              ON a.assessment_key = (
                  SELECT newest.assessment_key
                  FROM thesis_assessments AS newest
                  WHERE newest.memo_key = d.memo_key
                    AND newest.case_id = cr.case_id
                  ORDER BY newest.evidence_cutoff DESC, newest.created_at DESC,
                           newest.assessment_key DESC
                  LIMIT 1
              )
            WHERE """ + " AND ".join(clauses),
            params,
        ).fetchall()
        changed = 0
        for row in rows:
            quadrant = process_quadrant(
                thesis_correct=ThesisCorrectness(row["correctness"]),
                outcome_label=row["primary_label"],
            ).value
            if quadrant == row["quadrant"]:
                continue
            conn.execute(
                "UPDATE case_results SET quadrant = ? WHERE run_id = ? AND case_id = ?",
                (quadrant, row["run_id"], row["case_id"]),
            )
            changed += 1
        return changed

    def refresh_process_quadrants(self, *, run_id: str | None = None) -> int:
        """Re-cross cached thesis judgments with run-specific outcomes."""
        with self.transaction() as conn:
            return self._refresh_process_quadrants(conn, run_id=run_id)

    def save_thesis_assessment(self, assessment: ThesisAssessment) -> None:
        """Atomically cache one assessment and update every matching run quadrant."""
        if not all((
            assessment.assessment_key.strip(), assessment.memo_key.strip(),
            assessment.case_id.strip(), assessment.rationale.strip(),
            assessment.model_id.strip(), assessment.prompt_version.strip(),
        )):
            raise ValueError("thesis assessment identity and rationale must not be empty")
        if assessment.created_at.tzinfo is None or assessment.created_at.utcoffset() is None:
            raise ValueError("assessment created_at must be timezone-aware")
        with self.transaction() as conn:
            owner = conn.execute(
                "SELECT case_id FROM frozen_memos WHERE memo_key = ?",
                (assessment.memo_key,),
            ).fetchone()
            if owner is None:
                raise KeyError(f"unknown frozen memo {assessment.memo_key!r}")
            if owner["case_id"] != assessment.case_id:
                raise ValueError("assessment memo and case do not match")
            existing = conn.execute(
                "SELECT * FROM thesis_assessments WHERE assessment_key = ?",
                (assessment.assessment_key,),
            ).fetchone()
            if existing is not None:
                stored = self._assessment_from_row(existing)
                if self._assessment_content(stored) != self._assessment_content(assessment):
                    raise CaseConflictError(
                        f"assessment {assessment.assessment_key!r} has different content"
                    )
            else:
                conn.execute(
                    """
                    INSERT INTO thesis_assessments (
                        assessment_key, memo_key, case_id, correctness, rationale,
                        evidence_cutoff, model_id, prompt_version, evidence_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        assessment.assessment_key, assessment.memo_key, assessment.case_id,
                        assessment.correctness.value, assessment.rationale,
                        assessment.evidence_cutoff.isoformat(), assessment.model_id,
                        assessment.prompt_version, canonical_json(assessment.evidence),
                        assessment.created_at.isoformat(),
                    ),
                )
            self._refresh_process_quadrants(
                conn, memo_key=assessment.memo_key, case_id=assessment.case_id,
            )

    @staticmethod
    def _lesson_content(lesson: Lesson) -> str:
        return canonical_json({
            "lesson_id": lesson.lesson_id, "sleeve": lesson.sleeve,
            "text": lesson.text, "source_case_ids": lesson.source_case_ids,
            "eligible_from": lesson.eligible_from, "fingerprint": lesson.fingerprint,
            "tags": lesson.tags,
        })

    def get_distilled_lessons(self, distillation_key: str):
        from ops.backtest.lessons import DistilledLesson

        with self._lock:
            exists = self._conn.execute(
                "SELECT 1 FROM distillation_runs WHERE distillation_key = ?",
                (distillation_key,),
            ).fetchone()
            if exists is None:
                return None
            rows = self._conn.execute(
                """
                SELECT l.* FROM lesson_distillations AS d
                JOIN lessons AS l ON l.lesson_id = d.lesson_id
                WHERE d.distillation_key = ? ORDER BY d.ordinal
                """,
                (distillation_key,),
            ).fetchall()
            results = []
            for row in rows:
                source_rows = self._conn.execute(
                    "SELECT assessment_key FROM lesson_assessments "
                    "WHERE lesson_id = ? ORDER BY assessment_key",
                    (row["lesson_id"],),
                ).fetchall()
                lesson = Lesson(
                    lesson_id=row["lesson_id"], sleeve=row["sleeve"], text=row["text"],
                    source_case_ids=tuple(item[0] for item in self._conn.execute(
                        "SELECT case_id FROM lesson_sources WHERE lesson_id = ? "
                        "ORDER BY case_id", (row["lesson_id"],),
                    ).fetchall()),
                    eligible_from=date.fromisoformat(row["eligible_from"]),
                    fingerprint=row["fingerprint"], tags=tuple(json.loads(row["tags_json"])),
                    created_at=_parse_utc(row["created_at"]),
                )
                results.append(DistilledLesson(
                    lesson, distillation_key, tuple(item[0] for item in source_rows),
                ))
        return tuple(results)

    def save_distilled_lessons(self, distillation_key: str, lessons: Sequence) -> None:
        if not distillation_key.strip():
            raise ValueError("distillation_key must not be empty")
        rows = tuple(lessons)
        if any(row.distillation_key != distillation_key for row in rows):
            raise ValueError("distilled lesson belongs to another cache key")
        if len({row.lesson.lesson_id for row in rows}) != len(rows):
            raise ValueError("distillation contains duplicate lesson ids")
        with self.transaction() as conn:
            existing = conn.execute(
                "SELECT 1 FROM distillation_runs WHERE distillation_key = ?",
                (distillation_key,),
            ).fetchone()
            if existing is not None:
                stored = self.get_distilled_lessons(distillation_key)
                if canonical_json(stored) != canonical_json(rows):
                    raise CaseConflictError(
                        f"distillation {distillation_key!r} has different content"
                    )
                return
            conn.execute(
                "INSERT INTO distillation_runs (distillation_key, created_at) VALUES (?, ?)",
                (distillation_key, datetime.now(timezone.utc).isoformat()),
            )
            for ordinal, row in enumerate(rows):
                lesson = row.lesson
                if lesson.created_at.tzinfo is None or lesson.created_at.utcoffset() is None:
                    raise ValueError("lesson created_at must be timezone-aware")
                assessment_rows = conn.execute(
                    "SELECT assessment_key, case_id FROM thesis_assessments "
                    f"WHERE assessment_key IN ({','.join('?' for _ in row.source_assessment_keys)})",
                    tuple(row.source_assessment_keys),
                ).fetchall() if row.source_assessment_keys else []
                if {item["assessment_key"] for item in assessment_rows} != set(
                    row.source_assessment_keys
                ):
                    raise KeyError("distilled lesson references an unknown assessment")
                if {item["case_id"] for item in assessment_rows} != set(
                    lesson.source_case_ids
                ):
                    raise ValueError("lesson source cases do not match its assessments")
                existing_lesson = conn.execute(
                    "SELECT * FROM lessons WHERE lesson_id = ?", (lesson.lesson_id,),
                ).fetchone()
                if existing_lesson is None:
                    conn.execute(
                        "INSERT INTO lessons "
                        "(lesson_id, sleeve, text, eligible_from, fingerprint, tags_json, created_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (
                            lesson.lesson_id, lesson.sleeve, lesson.text,
                            lesson.eligible_from.isoformat(), lesson.fingerprint,
                            canonical_json(lesson.tags), lesson.created_at.isoformat(),
                        ),
                    )
                else:
                    stored_lesson = Lesson(
                        lesson_id=existing_lesson["lesson_id"],
                        sleeve=existing_lesson["sleeve"], text=existing_lesson["text"],
                        source_case_ids=tuple(item["case_id"] for item in assessment_rows),
                        eligible_from=date.fromisoformat(existing_lesson["eligible_from"]),
                        fingerprint=existing_lesson["fingerprint"],
                        tags=tuple(json.loads(existing_lesson["tags_json"])),
                        created_at=_parse_utc(existing_lesson["created_at"]),
                    )
                    if self._lesson_content(stored_lesson) != self._lesson_content(lesson):
                        raise CaseConflictError(
                            f"lesson {lesson.lesson_id!r} has different content"
                        )
                for case in sorted(set(lesson.source_case_ids)):
                    conn.execute(
                        "INSERT INTO lesson_sources (lesson_id, case_id, assessment_key) "
                        "VALUES (?, ?, NULL)", (lesson.lesson_id, case),
                    )
                conn.executemany(
                    "INSERT INTO lesson_assessments (lesson_id, assessment_key) VALUES (?, ?)",
                    [(lesson.lesson_id, key) for key in row.source_assessment_keys],
                )
                conn.execute(
                    "INSERT INTO lesson_distillations "
                    "(distillation_key, lesson_id, ordinal) VALUES (?, ?, ?)",
                    (distillation_key, lesson.lesson_id, ordinal),
                )

    def get_experiment(self, experiment_id: str) -> ExperimentRecord | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM experiments WHERE experiment_id = ?", (experiment_id,),
            ).fetchone()
            if row is None:
                return None
            cases = self._conn.execute(
                "SELECT case_id FROM experiment_cases WHERE experiment_id = ? "
                "ORDER BY ordinal", (experiment_id,),
            ).fetchall()
        return ExperimentRecord(
            experiment_id=row["experiment_id"], sleeve=row["sleeve"], seed=int(row["seed"]),
            holdout_case_ids=tuple(item[0] for item in cases),
            lesson_fingerprint=row["lesson_fingerprint"], status=row["status"],
            control_metrics=json.loads(row["control_metrics_json"]),
            treated_metrics=json.loads(row["treated_metrics_json"]),
            created_at=_parse_utc(row["created_at"]),
        )

    def save_experiment(self, record: ExperimentRecord) -> None:
        if not all((
            record.experiment_id.strip(), record.sleeve.strip(),
            record.lesson_fingerprint.strip(),
        )):
            raise ValueError("experiment identity must not be empty")
        if record.created_at.tzinfo is None or record.created_at.utcoffset() is None:
            raise ValueError("experiment created_at must be timezone-aware")
        if len(set(record.holdout_case_ids)) != len(record.holdout_case_ids):
            raise ValueError("experiment holdout cases must be unique")
        with self.transaction() as conn:
            known = conn.execute(
                "SELECT case_id FROM cases WHERE case_id IN "
                f"({','.join('?' for _ in record.holdout_case_ids)})",
                tuple(record.holdout_case_ids),
            ).fetchall() if record.holdout_case_ids else []
            if {item[0] for item in known} != set(record.holdout_case_ids):
                raise KeyError("experiment references an unknown holdout case")
            existing = self.get_experiment(record.experiment_id)
            if existing is not None:
                immutable = (
                    existing.sleeve, existing.seed, existing.holdout_case_ids,
                    existing.lesson_fingerprint, existing.created_at,
                )
                incoming = (
                    record.sleeve, record.seed, record.holdout_case_ids,
                    record.lesson_fingerprint, record.created_at,
                )
                if immutable != incoming:
                    raise CaseConflictError(
                        f"experiment {record.experiment_id!r} has different identity"
                    )
                transitions = {
                    "planned": {"planned", "running", "complete", "failed"},
                    "running": {"running", "complete", "failed"},
                    "complete": {"complete"}, "failed": {"failed"},
                }
                if record.status not in transitions[existing.status]:
                    raise ValueError(
                        f"experiment status cannot move {existing.status} -> {record.status}"
                    )
                if existing.status in {"complete", "failed"} and canonical_json(
                    existing
                ) != canonical_json(record):
                    raise CaseConflictError("terminal experiment record is immutable")
                conn.execute(
                    "UPDATE experiments SET status = ?, control_metrics_json = ?, "
                    "treated_metrics_json = ?, completed_at = ? WHERE experiment_id = ?",
                    (
                        record.status, canonical_json(record.control_metrics),
                        canonical_json(record.treated_metrics),
                        (datetime.now(timezone.utc).isoformat()
                         if record.status in {"complete", "failed"} else None),
                        record.experiment_id,
                    ),
                )
                return
            conn.execute(
                """
                INSERT INTO experiments (
                    experiment_id, sleeve, seed, lesson_fingerprint, status,
                    control_metrics_json, treated_metrics_json, created_at, completed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.experiment_id, record.sleeve, record.seed,
                    record.lesson_fingerprint, record.status,
                    canonical_json(record.control_metrics),
                    canonical_json(record.treated_metrics), record.created_at.isoformat(),
                    (record.created_at.isoformat()
                     if record.status in {"complete", "failed"} else None),
                ),
            )
            conn.executemany(
                "INSERT INTO experiment_cases (experiment_id, case_id, ordinal) "
                "VALUES (?, ?, ?)",
                [
                    (record.experiment_id, case, ordinal)
                    for ordinal, case in enumerate(record.holdout_case_ids)
                ],
            )

    def record_cutoff_probe(
        self,
        *,
        probe_id: str,
        model_id: str,
        tested_cutoff: date,
        prompts: object,
        responses: object,
        rubric: object,
        contaminated: bool,
        recommended_cutoff: date,
        created_at: datetime,
    ) -> None:
        """Seal one model-knowledge probe without changing configured cutoff."""
        if not probe_id.strip() or not model_id.strip():
            raise ValueError("probe_id and model_id must not be empty")
        enforce_cutoff(tested_cutoff, tested_cutoff)
        if recommended_cutoff < tested_cutoff:
            raise ValueError("recommended cutoff may not move backward")
        if created_at.tzinfo is None or created_at.utcoffset() is None:
            raise ValueError("created_at must be timezone-aware")
        values = (
            probe_id, model_id, tested_cutoff.isoformat(), canonical_json(prompts),
            canonical_json(responses), canonical_json(rubric), int(contaminated),
            recommended_cutoff.isoformat(), created_at.isoformat(),
        )
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM cutoff_probes WHERE probe_id = ?", (probe_id,),
            ).fetchone()
            if row is not None:
                existing = tuple(row[key] for key in (
                    "probe_id", "model_id", "tested_cutoff", "prompts_json",
                    "responses_json", "rubric_json", "contaminated",
                    "recommended_cutoff", "created_at",
                ))
                if existing != values:
                    raise CaseConflictError(
                        f"cutoff probe {probe_id!r} already exists with different content"
                    )
                return
            conn.execute(
                """
                INSERT INTO cutoff_probes (
                    probe_id, model_id, tested_cutoff, prompts_json, responses_json,
                    rubric_json, contaminated, recommended_cutoff, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                values,
            )

    def table_names(self) -> frozenset[str]:
        """Introspection helper used by migrations/tests, excluding SQLite internals."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        return frozenset(row[0] for row in rows)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> BacktestStore:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
