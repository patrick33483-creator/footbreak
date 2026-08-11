"""Immutable SQLite store for learning from Footbreak and Crown predictions.

The store deliberately records every materially different prediction attempt.  It
never overwrites a prediction, result, or grade: repeated submissions of the
same canonical content are idempotent, while changed content receives the next
attempt number.  That makes later backtests auditable and protects them from
post-kickoff data leakage.
"""
from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import threading
from collections.abc import Mapping
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


VALID_SYSTEMS = frozenset(("footbreak", "crown"))
VALID_STAGES = frozenset(("首預", "T-30", "T-5"))
LATEST_SCHEMA_VERSION = 1


class LearningStore:
    """A small, dependency-free, append-only prediction learning database.

    Args:
        path: SQLite database path, or ``":memory:"`` for an ephemeral store.

    ``record_snapshot`` retains post-kickoff submissions with
    ``pre_kickoff=False`` rather than deleting them.  Consumers should use that
    field to exclude quarantined rows from any train or evaluation data set.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).expanduser().parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(
            self.path,
            timeout=30,
            isolation_level=None,
            check_same_thread=False,
        )
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.execute("PRAGMA busy_timeout = 30000")
        self._connection.execute("PRAGMA journal_mode = WAL")
        self._connection.execute("PRAGMA synchronous = NORMAL")
        self._migrate()

    def __enter__(self) -> "LearningStore":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    def close(self) -> None:
        """Close the underlying SQLite connection."""
        with self._lock:
            if self._connection is not None:
                self._connection.close()
                self._connection = None  # type: ignore[assignment]

    def record_snapshot(
        self,
        system: str,
        fixture_id: str | int,
        stage: str,
        generated_at: str | datetime,
        kickoff: str | datetime,
        payload: Any,
        *,
        model_version: str = "unknown",
        schema_version: str = "1",
        model_run_id: int | None = None,
    ) -> dict[str, Any]:
        """Append one prediction snapshot and return its immutable identity.

        Canonically identical payloads for the same system, fixture, and stage
        return the original snapshot.  A changed payload gets the next
        ``attempt`` for that system/fixture/stage.  A generated time at or after
        kickoff is retained and marked quarantined instead of being dropped.
        """
        system = self._valid_system(system)
        fixture_id = self._identifier(fixture_id, "fixture_id")
        if stage not in VALID_STAGES:
            raise ValueError(f"stage must be one of {sorted(VALID_STAGES)!r}")
        generated = self._timestamp(generated_at, "generated_at")
        scheduled = self._timestamp(kickoff, "kickoff")
        pre_kickoff = int(generated < scheduled)
        payload_json, payload_sha256 = self._json_with_hash(payload, "payload")
        model_version = self._nonempty(model_version, "model_version")
        schema_version = self._nonempty(schema_version, "schema_version")
        if model_run_id is not None and (
            isinstance(model_run_id, bool) or not isinstance(model_run_id, int)
        ):
            raise TypeError("model_run_id must be an integer or None")

        with self._write_transaction():
            duplicate = self._connection.execute(
                """
                SELECT snapshot_id, attempt, pre_kickoff, payload_sha256
                FROM prediction_snapshots
                WHERE system = ? AND fixture_id = ? AND stage = ?
                  AND payload_sha256 = ? AND pre_kickoff = ?
                """,
                (system, fixture_id, stage, payload_sha256, pre_kickoff),
            ).fetchone()
            if duplicate is not None:
                return self._snapshot_response(duplicate, idempotent=True)

            attempt = int(self._connection.execute(
                """
                SELECT COALESCE(MAX(attempt), 0) + 1
                FROM prediction_snapshots
                WHERE system = ? AND fixture_id = ? AND stage = ?
                """,
                (system, fixture_id, stage),
            ).fetchone()[0])
            cursor = self._connection.execute(
                """
                INSERT INTO prediction_snapshots (
                    system, fixture_id, stage, attempt, generated_at, kickoff,
                    pre_kickoff, payload_json, payload_sha256, model_version,
                    schema_version, model_run_id, recorded_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    system,
                    fixture_id,
                    stage,
                    attempt,
                    generated,
                    scheduled,
                    pre_kickoff,
                    payload_json,
                    payload_sha256,
                    model_version,
                    schema_version,
                    model_run_id,
                    self._now(),
                ),
            )
            return {
                "snapshot_id": int(cursor.lastrowid),
                "attempt": attempt,
                "idempotent": False,
                "pre_kickoff": bool(pre_kickoff),
                "quarantined": not bool(pre_kickoff),
                "payload_sha256": payload_sha256,
            }

    def record_result(
        self,
        system: str,
        fixture_id: str | int,
        *,
        home_score: int | None = None,
        away_score: int | None = None,
        home_corners: int | None = None,
        away_corners: int | None = None,
        terminal_status: str = "finished",
        provenance: Any = None,
        source: str = "unspecified",
        observed_at: str | datetime | None = None,
    ) -> dict[str, Any]:
        """Append a provenance-bearing result version for a fixture.

        A result can first be recorded as provisional and later superseded by a
        final result.  Every changed result becomes a new immutable attempt.
        """
        system = self._valid_system(system)
        fixture_id = self._identifier(fixture_id, "fixture_id")
        terminal_status = self._nonempty(terminal_status, "terminal_status")
        source = self._nonempty(source, "source")
        provenance_value = {"source": source, "details": provenance}
        values = {
            "home_score": self._nonnegative_int(home_score, "home_score"),
            "away_score": self._nonnegative_int(away_score, "away_score"),
            "home_corners": self._nonnegative_int(home_corners, "home_corners"),
            "away_corners": self._nonnegative_int(away_corners, "away_corners"),
            "terminal_status": terminal_status,
            "provenance": provenance_value,
        }
        provenance_json = self._canonical_json(provenance_value)
        result_sha256 = self._sha256(self._canonical_json(values))
        observed = (
            self._timestamp(observed_at, "observed_at")
            if observed_at is not None
            else self._now()
        )

        with self._write_transaction():
            duplicate = self._connection.execute(
                """
                SELECT result_id, result_attempt
                FROM results
                WHERE system = ? AND fixture_id = ? AND result_sha256 = ?
                """,
                (system, fixture_id, result_sha256),
            ).fetchone()
            if duplicate is not None:
                return {
                    "result_id": int(duplicate["result_id"]),
                    "attempt": int(duplicate["result_attempt"]),
                    "idempotent": True,
                    "result_sha256": result_sha256,
                }
            attempt = int(self._connection.execute(
                """
                SELECT COALESCE(MAX(result_attempt), 0) + 1
                FROM results WHERE system = ? AND fixture_id = ?
                """,
                (system, fixture_id),
            ).fetchone()[0])
            cursor = self._connection.execute(
                """
                INSERT INTO results (
                    system, fixture_id, result_attempt, home_score, away_score,
                    home_corners, away_corners, terminal_status, provenance_json,
                    result_sha256, observed_at, recorded_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    system,
                    fixture_id,
                    attempt,
                    values["home_score"],
                    values["away_score"],
                    values["home_corners"],
                    values["away_corners"],
                    terminal_status,
                    provenance_json,
                    result_sha256,
                    observed,
                    self._now(),
                ),
            )
            return {
                "result_id": int(cursor.lastrowid),
                "attempt": attempt,
                "idempotent": False,
                "result_sha256": result_sha256,
            }

    def record_grade(
        self,
        snapshot_id: int | Mapping[str, Any],
        market: str,
        target: str,
        state: str,
        metrics: Any = None,
        *,
        result_id: int | None = None,
    ) -> dict[str, Any]:
        """Append a market/target grade for one prediction snapshot.

        Changed grading calculations are revisions, not updates.  Passing a
        snapshot response returned by ``record_snapshot`` is supported as a
        convenience.
        """
        if isinstance(snapshot_id, Mapping):
            snapshot_id = snapshot_id.get("snapshot_id")  # type: ignore[assignment]
        if isinstance(snapshot_id, bool) or not isinstance(snapshot_id, int):
            raise TypeError("snapshot_id must be an integer or snapshot response")
        market = self._nonempty(market, "market")
        target = self._nonempty(target, "target")
        state = self._nonempty(state, "state")
        if result_id is not None and (
            isinstance(result_id, bool) or not isinstance(result_id, int)
        ):
            raise TypeError("result_id must be an integer or None")
        metrics_json, metrics_sha256 = self._json_with_hash(
            {} if metrics is None else metrics, "metrics"
        )
        grade_sha256 = self._sha256(
            self._canonical_json(
                {
                    "state": state,
                    "metrics_sha256": metrics_sha256,
                    "result_id": result_id,
                }
            )
        )

        with self._write_transaction():
            snapshot = self._connection.execute(
                """
                SELECT system, fixture_id FROM prediction_snapshots
                WHERE snapshot_id = ?
                """,
                (snapshot_id,),
            ).fetchone()
            if snapshot is None:
                raise KeyError(f"unknown snapshot_id: {snapshot_id}")
            if result_id is not None:
                result = self._connection.execute(
                    "SELECT system, fixture_id FROM results WHERE result_id = ?",
                    (result_id,),
                ).fetchone()
                if result is None:
                    raise KeyError(f"unknown result_id: {result_id}")
                if (result["system"], result["fixture_id"]) != (
                    snapshot["system"],
                    snapshot["fixture_id"],
                ):
                    raise ValueError("result_id must belong to the snapshot fixture")

            duplicate = self._connection.execute(
                """
                SELECT grade_id, grade_attempt FROM grades
                WHERE snapshot_id = ? AND market = ? AND target = ?
                  AND grade_sha256 = ?
                """,
                (snapshot_id, market, target, grade_sha256),
            ).fetchone()
            if duplicate is not None:
                return {
                    "grade_id": int(duplicate["grade_id"]),
                    "attempt": int(duplicate["grade_attempt"]),
                    "idempotent": True,
                    "grade_sha256": grade_sha256,
                }
            attempt = int(self._connection.execute(
                """
                SELECT COALESCE(MAX(grade_attempt), 0) + 1
                FROM grades
                WHERE snapshot_id = ? AND market = ? AND target = ?
                """,
                (snapshot_id, market, target),
            ).fetchone()[0])
            cursor = self._connection.execute(
                """
                INSERT INTO grades (
                    snapshot_id, result_id, market, target, grade_attempt, state,
                    metrics_json, metrics_sha256, grade_sha256, recorded_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    snapshot_id,
                    result_id,
                    market,
                    target,
                    attempt,
                    state,
                    metrics_json,
                    metrics_sha256,
                    grade_sha256,
                    self._now(),
                ),
            )
            return {
                "grade_id": int(cursor.lastrowid),
                "attempt": attempt,
                "idempotent": False,
                "grade_sha256": grade_sha256,
            }

    def record_model_run(
        self,
        system: str,
        *,
        model_version: str,
        schema_version: str,
        manifest: Any,
        run_key: str | None = None,
        status: str = "completed",
        started_at: str | datetime | None = None,
        ended_at: str | datetime | None = None,
    ) -> dict[str, Any]:
        """Record an optional immutable manifest for a model execution."""
        system = self._valid_system(system)
        model_version = self._nonempty(model_version, "model_version")
        schema_version = self._nonempty(schema_version, "schema_version")
        status = self._nonempty(status, "status")
        if run_key is not None:
            run_key = self._nonempty(run_key, "run_key")
        manifest_json, manifest_sha256 = self._json_with_hash(manifest, "manifest")
        started = self._timestamp(started_at, "started_at") if started_at else None
        ended = self._timestamp(ended_at, "ended_at") if ended_at else None
        if started and ended and ended < started:
            raise ValueError("ended_at must not precede started_at")

        with self._write_transaction():
            if run_key is not None:
                duplicate = self._connection.execute(
                    "SELECT model_run_id FROM model_runs WHERE system = ? AND run_key = ?",
                    (system, run_key),
                ).fetchone()
                if duplicate is not None:
                    return {
                        "model_run_id": int(duplicate["model_run_id"]),
                        "idempotent": True,
                        "manifest_sha256": manifest_sha256,
                    }
            cursor = self._connection.execute(
                """
                INSERT INTO model_runs (
                    system, run_key, model_version, schema_version, status,
                    manifest_json, manifest_sha256, started_at, ended_at, recorded_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    system,
                    run_key,
                    model_version,
                    schema_version,
                    status,
                    manifest_json,
                    manifest_sha256,
                    started,
                    ended,
                    self._now(),
                ),
            )
            return {
                "model_run_id": int(cursor.lastrowid),
                "idempotent": False,
                "manifest_sha256": manifest_sha256,
            }

    def summary(self) -> dict[str, Any]:
        """Return compact operational counts without exposing mutable records."""
        with self._lock:
            self._ensure_open()
            snapshot_counts = self._connection.execute(
                """
                SELECT system, COUNT(*) AS total,
                       SUM(pre_kickoff) AS pre_kickoff,
                       SUM(CASE WHEN pre_kickoff = 0 THEN 1 ELSE 0 END) AS quarantined
                FROM prediction_snapshots GROUP BY system
                """
            ).fetchall()
            result_counts = {
                row["system"]: int(row["total"])
                for row in self._connection.execute(
                    "SELECT system, COUNT(*) AS total FROM results GROUP BY system"
                ).fetchall()
            }
            grade_counts = {
                row["system"]: int(row["total"])
                for row in self._connection.execute(
                    """
                    SELECT snapshots.system AS system, COUNT(*) AS total
                    FROM grades
                    JOIN prediction_snapshots AS snapshots
                      ON snapshots.snapshot_id = grades.snapshot_id
                    GROUP BY snapshots.system
                    """
                ).fetchall()
            }
            systems: dict[str, dict[str, int]] = {}
            for system in VALID_SYSTEMS:
                row = next((item for item in snapshot_counts if item["system"] == system), None)
                systems[system] = {
                    "snapshots": int(row["total"]) if row else 0,
                    "pre_kickoff_snapshots": int(row["pre_kickoff"] or 0) if row else 0,
                    "quarantined_snapshots": int(row["quarantined"] or 0) if row else 0,
                    "results": result_counts.get(system, 0),
                    "grades": grade_counts.get(system, 0),
                }
            return {
                "schema_version": LATEST_SCHEMA_VERSION,
                "snapshots": sum(item["snapshots"] for item in systems.values()),
                "pre_kickoff_snapshots": sum(
                    item["pre_kickoff_snapshots"] for item in systems.values()
                ),
                "quarantined_snapshots": sum(
                    item["quarantined_snapshots"] for item in systems.values()
                ),
                "results": sum(item["results"] for item in systems.values()),
                "grades": sum(item["grades"] for item in systems.values()),
                "model_runs": int(
                    self._connection.execute("SELECT COUNT(*) FROM model_runs").fetchone()[0]
                ),
                "systems": systems,
            }

    def backtest_rows(
        self, system: str
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Return immutable WDL and formal-market rows for chronological tests.

        Only the last valid pre-kickoff snapshot for each fixture/stage is
        eligible.  The latest immutable grade revision is used for each target.
        Quarantined post-kickoff attempts can never enter either output.
        """
        system = self._valid_system(system)
        with self._lock:
            self._ensure_open()
            rows = self._connection.execute(
                """
                WITH ranked_snapshots AS (
                    SELECT snapshots.*,
                           ROW_NUMBER() OVER (
                               PARTITION BY system, fixture_id, stage
                               ORDER BY generated_at DESC, snapshot_id DESC
                           ) AS snapshot_rank
                    FROM prediction_snapshots AS snapshots
                    WHERE system = ? AND pre_kickoff = 1
                ),
                ranked_grades AS (
                    SELECT grades.*,
                           ROW_NUMBER() OVER (
                               PARTITION BY snapshot_id, market, target
                               ORDER BY grade_attempt DESC, grade_id DESC
                           ) AS grade_rank
                    FROM grades
                )
                SELECT snapshots.fixture_id, snapshots.stage,
                       snapshots.generated_at, snapshots.kickoff,
                       snapshots.payload_json, grades.market, grades.target,
                       grades.state, grades.metrics_json
                FROM ranked_snapshots AS snapshots
                JOIN ranked_grades AS grades
                  ON grades.snapshot_id = snapshots.snapshot_id
                 AND grades.grade_rank = 1
                WHERE snapshots.snapshot_rank = 1
                ORDER BY snapshots.kickoff, snapshots.fixture_id,
                         snapshots.stage, grades.market, grades.target
                """,
                (system,),
            ).fetchall()

        wdl_rows: list[dict[str, Any]] = []
        market_rows: list[dict[str, Any]] = []
        for row in rows:
            payload = json.loads(row["payload_json"])
            metrics = json.loads(row["metrics_json"])
            common = {
                "match_id": str(row["fixture_id"]),
                "kickoff": datetime.fromisoformat(row["kickoff"]),
                "stage": str(row["stage"]),
                "predicted_at": str(row["generated_at"]),
                "conf": payload.get("conviction"),
            }
            if row["market"] == "WDL" and row["state"] == "GRADED":
                item = self._wdl_backtest_row(system, payload, metrics)
                if item is not None:
                    wdl_rows.append({**common, **item})
                continue
            if row["market"] not in {"HDC", "HIL", "CHL"}:
                continue
            if row["state"] != "GRADED":
                continue
            try:
                market_rows.append({
                    **common,
                    "market": str(row["market"]),
                    "target_key": str(row["target"]),
                    "probability": float(metrics["probability"]),
                    "target": float(metrics["target"]),
                    "hit": (
                        None
                        if metrics.get("hit") is None
                        else int(bool(metrics["hit"]))
                    ),
                    "brier": float(metrics["brier"]),
                    "log_loss": float(metrics["log_loss"]),
                })
            except (KeyError, TypeError, ValueError):
                continue
        return wdl_rows, market_rows

    @staticmethod
    def _wdl_backtest_row(
        system: str, payload: dict[str, Any], metrics: dict[str, Any]
    ) -> dict[str, Any] | None:
        if system == "footbreak":
            try:
                return {
                    "hit": int(metrics["hit"]),
                    "brier": float(metrics["brier"]),
                    "log_loss": float(metrics["log_loss"]),
                }
            except (KeyError, TypeError, ValueError):
                return None

        outcome = payload.get("outcome")
        actual = metrics.get("actual")
        key = {"主勝": "home", "和局": "draw", "客勝": "away"}.get(actual)
        if not isinstance(outcome, dict) or key is None:
            return None
        try:
            probabilities = {
                name: float(outcome[name]) for name in ("home", "draw", "away")
            }
            total = sum(probabilities.values())
            probabilities = {
                name: probability / total
                for name, probability in probabilities.items()
            }
            brier = sum(
                (
                    probabilities[name]
                    - (1.0 if name == key else 0.0)
                ) ** 2
                for name in probabilities
            )
            log_loss = -math.log(max(probabilities[key], 1e-9))
        except (KeyError, TypeError, ValueError, ZeroDivisionError):
            return None
        return {
            "hit": int(bool(metrics.get("hit"))),
            "brier": brier,
            "log_loss": log_loss,
        }

    def challenger_rows(self, system: str) -> tuple[list[dict[str, Any]], dict[str, int]]:
        """Return read-only, pre-kickoff market examples for a challenger.

        Unlike :meth:`backtest_rows`, this deliberately retains the immutable
        prediction payload.  The challenger feature extractor is responsible
        for using its explicit pre-kickoff whitelist; result/grade content is
        limited here to the target and the champion probability.  Every stage
        remains present, so a model may use a prior *prediction* stage without
        ever splitting one fixture across chronological partitions.

        This method issues no INSERT, UPDATE, or DELETE statements.  Late
        snapshots stay quarantined and are counted in ``diagnostics`` only.
        """
        system = self._valid_system(system)
        with self._lock:
            self._ensure_open()
            diagnostics_row = self._connection.execute(
                """
                SELECT
                    SUM(CASE WHEN pre_kickoff = 1 THEN 1 ELSE 0 END) AS pre_kickoff,
                    SUM(CASE WHEN pre_kickoff = 0 THEN 1 ELSE 0 END) AS quarantined
                FROM prediction_snapshots
                WHERE system = ?
                """,
                (system,),
            ).fetchone()
            rows = self._connection.execute(
                """
                WITH ranked_snapshots AS (
                    SELECT snapshots.*,
                           ROW_NUMBER() OVER (
                               PARTITION BY system, fixture_id, stage
                               ORDER BY generated_at DESC, snapshot_id DESC
                           ) AS snapshot_rank
                    FROM prediction_snapshots AS snapshots
                    WHERE system = ? AND pre_kickoff = 1
                ),
                ranked_grades AS (
                    SELECT grades.*,
                           ROW_NUMBER() OVER (
                               PARTITION BY snapshot_id, market, target
                               ORDER BY grade_attempt DESC, grade_id DESC
                           ) AS grade_rank
                    FROM grades
                )
                SELECT snapshots.fixture_id, snapshots.stage, snapshots.generated_at,
                       snapshots.kickoff, snapshots.payload_json,
                       grades.market, grades.target, grades.metrics_json
                FROM ranked_snapshots AS snapshots
                JOIN ranked_grades AS grades
                  ON grades.snapshot_id = snapshots.snapshot_id
                 AND grades.grade_rank = 1
                WHERE snapshots.snapshot_rank = 1
                  AND grades.state = 'GRADED'
                  AND grades.market IN ('HDC', 'HIL', 'CHL')
                ORDER BY snapshots.kickoff, snapshots.fixture_id,
                         snapshots.stage, grades.market, grades.target
                """,
                (system,),
            ).fetchall()

        output: list[dict[str, Any]] = []
        malformed = 0
        for row in rows:
            try:
                payload = json.loads(row["payload_json"])
                metrics = json.loads(row["metrics_json"])
                probability = float(metrics["probability"])
                target = float(metrics["target"])
                kickoff = datetime.fromisoformat(row["kickoff"])
                generated_at = datetime.fromisoformat(row["generated_at"])
                if (
                    not isinstance(payload, dict)
                    or not math.isfinite(probability)
                    or not math.isfinite(target)
                    or not 0.0 <= probability <= 1.0
                    or not 0.0 <= target <= 1.0
                    or generated_at >= kickoff
                ):
                    raise ValueError("invalid challenger source row")
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                malformed += 1
                continue
            output.append({
                "match_id": str(row["fixture_id"]),
                "kickoff": kickoff,
                "predicted_at": generated_at,
                "stage": str(row["stage"]),
                "market": str(row["market"]),
                "target_key": str(row["target"]),
                "probability": probability,
                "target": target,
                "payload": payload,
            })
        return output, {
            "pre_kickoff_snapshots": int(diagnostics_row["pre_kickoff"] or 0),
            "quarantined_snapshots": int(diagnostics_row["quarantined"] or 0),
            "malformed_or_nonfinite_rows_excluded": malformed,
        }

    def _migrate(self) -> None:
        with self._lock:
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version INTEGER PRIMARY KEY,
                    applied_at TEXT NOT NULL
                )
                """
            )
            applied = {
                int(row[0])
                for row in self._connection.execute(
                    "SELECT version FROM schema_migrations"
                ).fetchall()
            }
            for version, script in _MIGRATIONS:
                if version in applied:
                    continue
                try:
                    self._connection.executescript(
                        "BEGIN IMMEDIATE;\n"
                        + script
                        + (
                            "INSERT INTO schema_migrations (version, applied_at) "
                            f"VALUES ({version}, '{self._now()}');\nCOMMIT;"
                        )
                    )
                except Exception:
                    self._connection.rollback()
                    raise

    @contextmanager
    def _write_transaction(self) -> Iterator[None]:
        with self._lock:
            self._ensure_open()
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                yield
            except Exception:
                self._connection.rollback()
                raise
            else:
                self._connection.commit()

    def _ensure_open(self) -> None:
        if self._connection is None:
            raise RuntimeError("LearningStore is closed")

    @staticmethod
    def _canonical_json(value: Any) -> str:
        try:
            return json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("value must be JSON serializable without NaN or Infinity") from exc

    @classmethod
    def _json_with_hash(cls, value: Any, name: str) -> tuple[str, str]:
        try:
            canonical = cls._canonical_json(value)
        except ValueError as exc:
            raise ValueError(f"{name} {exc}") from exc
        return canonical, cls._sha256(canonical)

    @staticmethod
    def _sha256(text: str) -> str:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="microseconds")

    @staticmethod
    def _timestamp(value: str | datetime, name: str) -> str:
        if isinstance(value, datetime):
            parsed = value
        elif isinstance(value, str):
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError as exc:
                raise ValueError(f"{name} must be an ISO-8601 timestamp") from exc
        else:
            raise TypeError(f"{name} must be a datetime or ISO-8601 string")
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError(f"{name} must include a timezone offset")
        return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds")

    @staticmethod
    def _valid_system(system: str) -> str:
        if system not in VALID_SYSTEMS:
            raise ValueError(f"system must be one of {sorted(VALID_SYSTEMS)!r}")
        return system

    @staticmethod
    def _identifier(value: str | int, name: str) -> str:
        if isinstance(value, bool):
            raise TypeError(f"{name} must be a non-empty string or integer")
        text = str(value).strip()
        if not text:
            raise ValueError(f"{name} must not be empty")
        return text

    @staticmethod
    def _nonempty(value: str, name: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{name} must be a non-empty string")
        return value.strip()

    @staticmethod
    def _nonnegative_int(value: int | None, name: str) -> int | None:
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"{name} must be a non-negative integer or None")
        return value

    @staticmethod
    def _snapshot_response(row: sqlite3.Row, *, idempotent: bool) -> dict[str, Any]:
        pre_kickoff = bool(row["pre_kickoff"])
        return {
            "snapshot_id": int(row["snapshot_id"]),
            "attempt": int(row["attempt"]),
            "idempotent": idempotent,
            "pre_kickoff": pre_kickoff,
            "quarantined": not pre_kickoff,
            "payload_sha256": str(row["payload_sha256"]),
        }


_MIGRATIONS: tuple[tuple[int, str], ...] = (
    (
        1,
        """
        CREATE TABLE model_runs (
            model_run_id INTEGER PRIMARY KEY,
            system TEXT NOT NULL CHECK (system IN ('footbreak', 'crown')),
            run_key TEXT,
            model_version TEXT NOT NULL,
            schema_version TEXT NOT NULL,
            status TEXT NOT NULL,
            manifest_json TEXT NOT NULL,
            manifest_sha256 TEXT NOT NULL,
            started_at TEXT,
            ended_at TEXT,
            recorded_at TEXT NOT NULL,
            UNIQUE (system, run_key)
        );

        CREATE TABLE prediction_snapshots (
            snapshot_id INTEGER PRIMARY KEY,
            system TEXT NOT NULL CHECK (system IN ('footbreak', 'crown')),
            fixture_id TEXT NOT NULL,
            stage TEXT NOT NULL CHECK (stage IN ('首預', 'T-30', 'T-5')),
            attempt INTEGER NOT NULL CHECK (attempt > 0),
            generated_at TEXT NOT NULL,
            kickoff TEXT NOT NULL,
            pre_kickoff INTEGER NOT NULL CHECK (pre_kickoff IN (0, 1)),
            payload_json TEXT NOT NULL,
            payload_sha256 TEXT NOT NULL CHECK (length(payload_sha256) = 64),
            model_version TEXT NOT NULL,
            schema_version TEXT NOT NULL,
            model_run_id INTEGER REFERENCES model_runs(model_run_id),
            recorded_at TEXT NOT NULL,
            UNIQUE (system, fixture_id, stage, attempt),
            UNIQUE (system, fixture_id, stage, payload_sha256, pre_kickoff)
        );
        CREATE INDEX prediction_snapshots_fixture_idx
            ON prediction_snapshots (system, fixture_id, stage, attempt);
        CREATE INDEX prediction_snapshots_valid_kickoff_idx
            ON prediction_snapshots (pre_kickoff, kickoff);

        CREATE TABLE results (
            result_id INTEGER PRIMARY KEY,
            system TEXT NOT NULL CHECK (system IN ('footbreak', 'crown')),
            fixture_id TEXT NOT NULL,
            result_attempt INTEGER NOT NULL CHECK (result_attempt > 0),
            home_score INTEGER CHECK (home_score IS NULL OR home_score >= 0),
            away_score INTEGER CHECK (away_score IS NULL OR away_score >= 0),
            home_corners INTEGER CHECK (home_corners IS NULL OR home_corners >= 0),
            away_corners INTEGER CHECK (away_corners IS NULL OR away_corners >= 0),
            terminal_status TEXT NOT NULL,
            provenance_json TEXT NOT NULL,
            result_sha256 TEXT NOT NULL CHECK (length(result_sha256) = 64),
            observed_at TEXT NOT NULL,
            recorded_at TEXT NOT NULL,
            UNIQUE (system, fixture_id, result_attempt),
            UNIQUE (system, fixture_id, result_sha256)
        );
        CREATE INDEX results_fixture_idx
            ON results (system, fixture_id, result_attempt);

        CREATE TABLE grades (
            grade_id INTEGER PRIMARY KEY,
            snapshot_id INTEGER NOT NULL REFERENCES prediction_snapshots(snapshot_id),
            result_id INTEGER REFERENCES results(result_id),
            market TEXT NOT NULL,
            target TEXT NOT NULL,
            grade_attempt INTEGER NOT NULL CHECK (grade_attempt > 0),
            state TEXT NOT NULL,
            metrics_json TEXT NOT NULL,
            metrics_sha256 TEXT NOT NULL CHECK (length(metrics_sha256) = 64),
            grade_sha256 TEXT NOT NULL CHECK (length(grade_sha256) = 64),
            recorded_at TEXT NOT NULL,
            UNIQUE (snapshot_id, market, target, grade_attempt),
            UNIQUE (snapshot_id, market, target, grade_sha256)
        );
        CREATE INDEX grades_snapshot_idx
            ON grades (snapshot_id, market, target, grade_attempt);

        CREATE TRIGGER prediction_snapshots_immutable_update
        BEFORE UPDATE ON prediction_snapshots
        BEGIN
            SELECT RAISE(ABORT, 'prediction_snapshots are immutable');
        END;
        CREATE TRIGGER prediction_snapshots_immutable_delete
        BEFORE DELETE ON prediction_snapshots
        BEGIN
            SELECT RAISE(ABORT, 'prediction_snapshots are immutable');
        END;
        CREATE TRIGGER results_immutable_update
        BEFORE UPDATE ON results
        BEGIN
            SELECT RAISE(ABORT, 'results are immutable');
        END;
        CREATE TRIGGER results_immutable_delete
        BEFORE DELETE ON results
        BEGIN
            SELECT RAISE(ABORT, 'results are immutable');
        END;
        CREATE TRIGGER grades_immutable_update
        BEFORE UPDATE ON grades
        BEGIN
            SELECT RAISE(ABORT, 'grades are immutable');
        END;
        CREATE TRIGGER grades_immutable_delete
        BEFORE DELETE ON grades
        BEGIN
            SELECT RAISE(ABORT, 'grades are immutable');
        END;
        """,
    ),
)
