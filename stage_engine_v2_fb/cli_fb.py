"""V2 footbreak CLI —— tick / dry-run / dashboard 入口。

用法同 crown 一樣：
    python -m stage_engine_v2_fb tick
    python -m stage_engine_v2_fb dry-run --data /path/to/footbreak/data.json --now ...
    python -m stage_engine_v2_fb dashboard

分別：
- 讀 /var/www/footbreak/data.json
- Lead 直接由 stages[i].lead 讀（唔重排 EV）
- Ledger 寫 /var/lib/footbreak/stage_engine_v2_fb/ledger.json
- Dashboard 寫 /var/www/stage_engine_v2_fb/data.json

Reuse crown v2 modules（fixtures 讀取、scheduler 時機、publisher gate、
writer 讀寫 ledger、telegram shadow log），只有 predictor 唔同。
"""
from __future__ import annotations
import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from stage_engine_v2.fixtures import refresh_fixtures, HKT
from stage_engine_v2.publisher import decide_publish
from stage_engine_v2.scheduler import due_stages
from stage_engine_v2.telegram import send_stage
from stage_engine_v2.writer import already_fired, load_ledger, record_stage, save_ledger

from .predictor_fb import build_prediction_fb


DEFAULT_FB_DATA_PATH = Path("/var/www/footbreak/data.json")
DEFAULT_FB_LEDGER_PATH = Path("/var/lib/footbreak/stage_engine_v2_fb/ledger.json")
DEFAULT_FB_DASHBOARD_PATH = Path("/var/www/stage_engine_v2_fb/data.json")
DEFAULT_FB_SENT_LOG = Path("/var/lib/footbreak/stage_engine_v2_fb/telegram_sent.jsonl")


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
    data_path: Path,
    ledger_path: Path,
    dashboard_path: Path,
    sent_log_path: Path,
    now_utc: datetime,
    window_hours: int,
    dry_run: bool,
) -> dict[str, Any]:
    started_at = datetime.now(timezone.utc)
    # Reuse crown fixtures loader —— schema 相同（都係 legacy data.json 有 matches list）
    fixtures = refresh_fixtures(
        crown_data_path=data_path,
        window_hours=window_hours,
        now_utc=now_utc,
    )
    ledger = load_ledger(ledger_path)
    fired: list[dict[str, Any]] = []
    skipped_no_prediction: list[str] = []
    for fx in fixtures:
        done = already_fired(ledger, fx.id)
        for stage in due_stages(fx, now_utc, done):
            pred_body = build_prediction_fb(fx, stage, now_utc=now_utc)
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
    if not dry_run:
        save_ledger(ledger, ledger_path)
        _write_dashboard(ledger, dashboard_path, now_utc=now_utc)
    finished_at = datetime.now(timezone.utc)
    return {
        "ok": True,
        "engine": "stage-engine-v2-fb",
        "dry_run": dry_run,
        "started_at_utc": started_at.isoformat(),
        "finished_at_utc": finished_at.isoformat(),
        "elapsed_seconds": (finished_at - started_at).total_seconds(),
        "now_utc": now_utc.isoformat(),
        "fixtures_upcoming": len(fixtures),
        "fired_count": len(fired),
        "fired": fired,
        "skipped_no_prediction": skipped_no_prediction,
    }


def _write_dashboard(
    ledger: dict[str, Any],
    dashboard_path: Path,
    *,
    now_utc: datetime,
) -> None:
    fixtures_out = []
    for slot in (ledger.get("fixtures") or {}).values():
        if not isinstance(slot, dict):
            continue
        stages = slot.get("stages") or {}
        stage_summary = {
            name: {
                "predicted_at_utc": row.get("predicted_at_utc"),
                "lead_market": row.get("lead_market"),
                "lead_label": row.get("lead_label"),
                "lead_odds": row.get("lead_odds"),
                "lead_prob": row.get("lead_prob"),
                "lead_ev": row.get("lead_ev"),
                "conviction": row.get("conviction"),
                "verdict": row.get("verdict"),
                "no_bet_reason": row.get("no_bet_reason"),
                "publish_decision": row.get("publish_decision"),
                "publish_reason": row.get("publish_reason"),
                "legacy_stage_ts": row.get("legacy_stage_ts"),
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
    payload = {
        "schema_version": "stage-engine-v2-fb",
        "generated_at_utc": now_utc.isoformat(),
        "generated_at_hkt": now_utc.astimezone(HKT).isoformat(),
        "fixtures_count": len(fixtures_out),
        "fixtures": fixtures_out,
    }
    dashboard_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = dashboard_path.with_suffix(dashboard_path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(dashboard_path)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser("stage_engine_v2_fb")
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("tick", "dry-run"):
        sp = sub.add_parser(name)
        sp.add_argument("--data", default=str(DEFAULT_FB_DATA_PATH),
                        help="Footbreak legacy data.json path")
        sp.add_argument("--ledger", default=str(DEFAULT_FB_LEDGER_PATH))
        sp.add_argument("--dashboard", default=str(DEFAULT_FB_DASHBOARD_PATH))
        sp.add_argument("--sent-log", default=str(DEFAULT_FB_SENT_LOG))
        sp.add_argument("--now", default=None,
                        help="覆蓋現在時間（ISO 格式，缺 tz 當 HKT）")
        sp.add_argument("--window-hours", type=int, default=48)
    sp = sub.add_parser("dashboard")
    sp.add_argument("--ledger", default=str(DEFAULT_FB_LEDGER_PATH))
    sp.add_argument("--dashboard", default=str(DEFAULT_FB_DASHBOARD_PATH))
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd in ("tick", "dry-run"):
        # 確保 parent dirs 存在
        for p in (Path(args.ledger).parent, Path(args.dashboard).parent, Path(args.sent_log).parent):
            p.mkdir(parents=True, exist_ok=True)
        result = _run_tick(
            data_path=Path(args.data),
            ledger_path=Path(args.ledger),
            dashboard_path=Path(args.dashboard),
            sent_log_path=Path(args.sent_log),
            now_utc=_parse_now(args.now),
            window_hours=args.window_hours,
            dry_run=(args.cmd == "dry-run"),
        )
        json.dump(result, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
        return 0
    if args.cmd == "dashboard":
        ledger = load_ledger(Path(args.ledger))
        Path(args.dashboard).parent.mkdir(parents=True, exist_ok=True)
        _write_dashboard(ledger, Path(args.dashboard), now_utc=datetime.now(timezone.utc))
        print(f"dashboard written to {args.dashboard}")
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
