"""Stage engine v2 CLI —— tick / dry-run / dashboard 入口。

用法：
    python -m stage_engine_v2 tick
    python -m stage_engine_v2 tick --now 2026-08-29T00:30:00+08:00
    python -m stage_engine_v2 dry-run --crown-data /path/to/crown.json --now ...
    python -m stage_engine_v2 dashboard  # 從 v2 ledger 寫 dashboard

所有 command 都係 idempotent。可以隨便重跑。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .fixtures import DEFAULT_CROWN_DATA_PATH, refresh_fixtures, HKT
from .native_queue import DEFAULT_NATIVE_QUEUE_DIR, load_native_payloads
from .operator_results import (
    DEFAULT_OPERATOR_RESULTS_PATH,
    load_operator_history_rows,
    merge_operator_history_rows,
    project_verified_crown_scores,
)
from .predictor import build_prediction
from .publisher import decide_publish
from .result_sync import (
    DEFAULT_AUTOMATIC_RESULTS_PATH,
    load_automatic_history_rows,
    merge_automatic_history_rows,
    sync_results,
)
from .scheduler import due_stages
from .segmented_conditions import build_segmented_conditions
from .telegram import DEFAULT_CONDITION_SENT_LOG, DEFAULT_SENT_LOG, send_condition_alerts, send_stage
from .writer import DEFAULT_LEDGER_PATH, already_fired, load_ledger, record_stage, save_ledger

DEFAULT_DASHBOARD_PATH = Path("/var/www/stage_engine_v2/data.json")


def _native_payload_for(
    fx: Any,
    stage: str,
    payloads: dict[tuple[str, str], dict[str, Any]],
) -> dict[str, Any] | None:
    """Match only explicit IDs carried by the same Crown fixture card."""
    identities = [fx.id]
    raw = fx.raw if isinstance(fx.raw, dict) else {}
    for key in (
        "match_id", "id", "titan_match_id", "native_fixture_id",
        "hkjc_match_id", "pinnapi_event_id",
    ):
        value = raw.get(key)
        if value:
            identities.append(str(value))
    for match_id in dict.fromkeys(identities):
        payload = payloads.get((match_id, stage))
        if payload is not None:
            return payload
    return None


def _parse_now(raw: str | None) -> datetime:
    if raw is None:
        return datetime.now(timezone.utc)
    text = raw.strip()
    try:
        dt = datetime.fromisoformat(text.replace(" ", "T"))
    except ValueError as exc:
        raise SystemExit(f"invalid --now: {raw!r} ({exc})")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=HKT)
    return dt.astimezone(timezone.utc)


def _run_tick(
    *,
    crown_data_path: Path,
    ledger_path: Path,
    dashboard_path: Path,
    sent_log_path: Path,
    now_utc: datetime,
    window_hours: int,
    dry_run: bool,
    native_queue_dir: Path = DEFAULT_NATIVE_QUEUE_DIR,
    condition_sent_log_path: Path = DEFAULT_CONDITION_SENT_LOG,
    operator_results_path: Path = DEFAULT_OPERATOR_RESULTS_PATH,
    automatic_results_path: Path = DEFAULT_AUTOMATIC_RESULTS_PATH,
) -> dict[str, Any]:
    started_at = datetime.now(timezone.utc)
    fixtures = refresh_fixtures(
        crown_data_path=crown_data_path,
        window_hours=window_hours,
        now_utc=now_utc,
    )
    ledger = load_ledger(ledger_path)
    native_payloads = load_native_payloads(native_queue_dir)

    fired: list[dict[str, Any]] = []
    skipped_no_prediction: list[str] = []

    for fx in fixtures:
        done = already_fired(ledger, fx.id)
        for stage in due_stages(fx, now_utc, done):
            pred_body = build_prediction(
                fx,
                stage,
                now_utc=now_utc,
                native_payload=_native_payload_for(fx, stage, native_payloads),
            )
            if pred_body is None:
                skipped_no_prediction.append(f"{fx.id}:{stage}")
                continue
            pred_body["stage"] = stage
            publish_ok, publish_reason = decide_publish(pred_body, stage)
            pred_body["publish_decision"] = publish_ok
            pred_body["publish_reason"] = publish_reason
            record_stage(ledger, fx, stage, pred_body)
            if publish_ok:
                notify_result = send_stage(
                    {**pred_body, "stage": stage},
                    sent_log_path=sent_log_path,
                )
            else:
                notify_result = {
                    "sent": False, "shadow": False, "skipped": True,
                    "reason": f"gate_rejected:{publish_reason}",
                }
            fired.append({
                "fixture_id": fx.id,
                "stage": stage,
                "publish": publish_ok,
                "publish_reason": publish_reason,
                "notify": notify_result,
            })

    condition_notify: list[dict[str, Any]] = []
    if not dry_run:
        save_ledger(ledger, ledger_path)
        condition_notify = _write_dashboard(
            ledger,
            dashboard_path,
            now_utc=now_utc,
            history_rows=_load_history_rows(
                crown_data_path,
                ledger=ledger,
                operator_results_path=operator_results_path,
                automatic_results_path=automatic_results_path,
            ),
            condition_sent_log_path=condition_sent_log_path,
        )

    finished_at = datetime.now(timezone.utc)
    return {
        "ok": True,
        "dry_run": dry_run,
        "started_at_utc": started_at.isoformat(),
        "finished_at_utc": finished_at.isoformat(),
        "elapsed_seconds": (finished_at - started_at).total_seconds(),
        "now_utc": now_utc.isoformat(),
        "fixtures_upcoming": len(fixtures),
        "fired_count": len(fired),
        "fired": fired,
        "skipped_no_prediction": skipped_no_prediction,
        "condition_notify_count": len(condition_notify),
        "condition_notify_sent": sum(1 for r in condition_notify if r.get("sent")),
    }


def _write_dashboard(
    ledger: dict[str, Any],
    dashboard_path: Path,
    *,
    now_utc: datetime,
    history_rows: list[dict[str, Any]] | None = None,
    condition_sent_log_path: Path = DEFAULT_CONDITION_SENT_LOG,
    send_alerts: bool = True,
) -> list[dict[str, Any]]:
    fixtures_out = []
    for slot in (ledger.get("fixtures") or {}).values():
        if not isinstance(slot, dict):
            continue
        stages = slot.get("stages") or {}
        stage_summary = {
            name: {
                "predicted_at_utc": row.get("predicted_at_utc"),
                "lead_market": row.get("lead_market"),
                "lead_code": row.get("lead_code"),
                "lead_label": row.get("lead_label"),
                "lead_side": row.get("lead_side"),
                "lead_line": row.get("lead_line"),
                "lead_odds": row.get("lead_odds"),
                "lead_prob": row.get("lead_prob"),
                "lead_ev": row.get("lead_ev"),
                "conviction": row.get("conviction"),
                "publish_decision": row.get("publish_decision"),
                "publish_reason": row.get("publish_reason"),
                "source_stage": row.get("source_stage"),
                "source_predicted_at": row.get("source_predicted_at"),
                "stage_payload_source": row.get("stage_payload_source"),
                "prediction_model": row.get("prediction_model"),
                "input_policy": row.get("input_policy"),
                "input_cutoff_at": row.get("input_cutoff_at"),
                "opening_snapshot_hash": row.get("opening_snapshot_hash"),
                "opening_model_status": row.get("opening_model_status"),
                "team_history_as_of": row.get("team_history_as_of"),
                "team_history_sample": row.get("team_history_sample"),
                "late_inputs_used": row.get("late_inputs_used"),
            }
            for name, row in stages.items()
            if isinstance(row, dict)
        }
        fixtures_out.append({
            "id": slot.get("id"),
            "league": slot.get("league"),
            "home": slot.get("home"),
            "away": slot.get("away"),
            "kickoff_utc": slot.get("kickoff_utc"),
            "kickoff_hkt": slot.get("kickoff_hkt"),
            "source": slot.get("source"),
            "stages": stage_summary,
        })
    fixtures_out.sort(key=lambda r: str(r.get("kickoff_utc") or ""))
    segmented_conditions = build_segmented_conditions(
        ledger,
        history_rows or [],
        generated_at=now_utc.astimezone(HKT).isoformat(),
    )
    payload = {
        "schema_version": "stage-engine-v2",
        "generated_at_utc": now_utc.isoformat(),
        "generated_at_hkt": now_utc.astimezone(HKT).isoformat(),
        "fixtures_count": len(fixtures_out),
        "fixtures": fixtures_out,
        "segmented_conditions": segmented_conditions,
    }
    dashboard_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = dashboard_path.with_suffix(dashboard_path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(dashboard_path)
    if not send_alerts:
        return []
    return send_condition_alerts(
        segmented_conditions,
        sent_log_path=condition_sent_log_path,
    )


def _load_history_rows(
    crown_data_path: Path,
    *,
    ledger: dict[str, Any] | None = None,
    operator_results_path: Path = DEFAULT_OPERATOR_RESULTS_PATH,
    automatic_results_path: Path = DEFAULT_AUTOMATIC_RESULTS_PATH,
) -> list[dict[str, Any]]:
    """Read Crown history and merge a validated V2-only operator overlay."""
    rows: list[dict[str, Any]] = []
    try:
        payload = json.loads(crown_data_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        payload = {}
    history = payload.get("prediction_history")
    if isinstance(history, dict) and isinstance(history.get("rows"), list):
        rows = [row for row in history["rows"] if isinstance(row, dict)]
    else:
        relative = str(payload.get("history_data_url") or "").strip()
        if relative and "/" not in relative and "\\" not in relative:
            try:
                sidecar = json.loads(
                    (crown_data_path.parent / relative).read_text(encoding="utf-8")
                )
            except (OSError, json.JSONDecodeError):
                sidecar = {}
            history = sidecar.get("prediction_history")
            sidecar_rows = history.get("rows") if isinstance(history, dict) else None
            if isinstance(sidecar_rows, list):
                rows = [row for row in sidecar_rows if isinstance(row, dict)]
    if ledger is None:
        return rows
    rows = [*rows, *project_verified_crown_scores(rows, ledger)]
    automatic_rows = load_automatic_history_rows(automatic_results_path, ledger)
    rows = merge_automatic_history_rows(rows, automatic_rows)
    operator_rows = load_operator_history_rows(operator_results_path, ledger)
    return merge_operator_history_rows(rows, operator_rows)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser("stage_engine_v2")
    sub = p.add_subparsers(dest="cmd", required=True)

    for name in ("tick", "dry-run"):
        sp = sub.add_parser(name)
        sp.add_argument("--crown-data", default=str(DEFAULT_CROWN_DATA_PATH))
        sp.add_argument("--ledger", default=str(DEFAULT_LEDGER_PATH))
        sp.add_argument("--dashboard", default=str(DEFAULT_DASHBOARD_PATH))
        sp.add_argument("--sent-log", default=str(DEFAULT_SENT_LOG))
        sp.add_argument("--condition-sent-log", default=str(DEFAULT_CONDITION_SENT_LOG))
        sp.add_argument(
            "--operator-results",
            default=str(DEFAULT_OPERATOR_RESULTS_PATH),
            help="V2-only, identity-locked operator result overlay",
        )
        sp.add_argument(
            "--automatic-results",
            default=str(DEFAULT_AUTOMATIC_RESULTS_PATH),
            help="V2-only, identity-locked automatic result cache",
        )
        sp.add_argument("--now", default=None,
                        help="覆蓋現在時間（ISO 格式，缺 tz 當 HKT）")
        sp.add_argument("--window-hours", type=int, default=48)
        sp.add_argument(
            "--native-queue-dir",
            default=str(DEFAULT_NATIVE_QUEUE_DIR),
        )

    sp = sub.add_parser("dashboard")
    sp.add_argument("--ledger", default=str(DEFAULT_LEDGER_PATH))
    sp.add_argument("--dashboard", default=str(DEFAULT_DASHBOARD_PATH))

    sp = sub.add_parser("settle")
    sp.add_argument("--crown-data", default=str(DEFAULT_CROWN_DATA_PATH))
    sp.add_argument("--ledger", default=str(DEFAULT_LEDGER_PATH))
    sp.add_argument("--dashboard", default=str(DEFAULT_DASHBOARD_PATH))
    sp.add_argument("--condition-sent-log", default=str(DEFAULT_CONDITION_SENT_LOG))
    sp.add_argument("--operator-results", default=str(DEFAULT_OPERATOR_RESULTS_PATH))
    sp.add_argument("--automatic-results", default=str(DEFAULT_AUTOMATIC_RESULTS_PATH))
    sp.add_argument("--lookback-days", type=int, default=7)
    sp.add_argument("--max-seconds", type=float, default=30.0)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd in ("tick", "dry-run"):
        result = _run_tick(
            crown_data_path=Path(args.crown_data),
            ledger_path=Path(args.ledger),
            dashboard_path=Path(args.dashboard),
            sent_log_path=Path(args.sent_log),
            now_utc=_parse_now(args.now),
            window_hours=args.window_hours,
            dry_run=(args.cmd == "dry-run"),
            native_queue_dir=Path(args.native_queue_dir),
            condition_sent_log_path=Path(args.condition_sent_log),
            operator_results_path=Path(args.operator_results),
            automatic_results_path=Path(args.automatic_results),
        )
        json.dump(result, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
        return 0
    if args.cmd == "settle":
        started = time.monotonic()
        print("stage-v2-settle: load-ledger", flush=True)
        ledger_path = Path(args.ledger)
        ledger = load_ledger(ledger_path)
        print(
            f"stage-v2-settle: sync-results fixtures={len(ledger.get('fixtures') or {})}",
            flush=True,
        )
        result = sync_results(
            ledger,
            path=Path(args.automatic_results),
            lookback_days=args.lookback_days,
            max_seconds=args.max_seconds,
        )
        # The 30-second prediction tick owns the append-only ledger and can
        # commit while the provider request above is in flight.  Reload before
        # projection so a fast settlement pass never publishes an older
        # snapshot over a newly recorded stage.
        print(
            f"stage-v2-settle: sync-complete elapsed={time.monotonic() - started:.1f}s "
            f"due={result.get('due')} settled={result.get('settled_now')}",
            flush=True,
        )
        ledger = load_ledger(ledger_path)
        print("stage-v2-settle: load-history", flush=True)
        history_rows = _load_history_rows(
            Path(args.crown_data),
            ledger=ledger,
            operator_results_path=Path(args.operator_results),
            automatic_results_path=Path(args.automatic_results),
        )
        print(
            f"stage-v2-settle: write-dashboard history_rows={len(history_rows)}",
            flush=True,
        )
        _write_dashboard(
            ledger,
            Path(args.dashboard),
            now_utc=datetime.now(timezone.utc),
            history_rows=history_rows,
            condition_sent_log_path=Path(args.condition_sent_log),
            send_alerts=False,
        )
        print(
            f"stage-v2-settle: complete elapsed={time.monotonic() - started:.1f}s",
            flush=True,
        )
        json.dump(result, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
        return 0
    if args.cmd == "dashboard":
        ledger = load_ledger(Path(args.ledger))
        _write_dashboard(ledger, Path(args.dashboard), now_utc=datetime.now(timezone.utc))
        print(f"dashboard written to {args.dashboard}")
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
