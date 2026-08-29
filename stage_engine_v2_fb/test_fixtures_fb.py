import json
from datetime import datetime, timezone

from stage_engine_v2_fb.cli_fb import _run_tick
from stage_engine_v2_fb.fixtures_fb import load_ledger_fixtures


def test_loads_recent_persisted_watch_stages(tmp_path):
    path = tmp_path / "ledger.json"
    path.write_text(json.dumps({
        "watch": {
            "raw-key": {
                "match_id": "F1",
                "league": "L",
                "home": "H",
                "away": "A",
                "kickoff_hkt": "2026-08-29T23:00:00+08:00",
                "stages": [{"stage": "T-5", "lead": {"odds": 1.9}}],
            }
        }
    }), encoding="utf-8")
    fixtures = load_ledger_fixtures(
        path,
        now_utc=datetime(2026, 8, 29, 16, 0, tzinfo=timezone.utc),
    )
    assert len(fixtures) == 1
    assert fixtures[0].id == "F1"
    assert fixtures[0].raw["stages"][0]["stage"] == "T-5"


def test_tick_projects_persisted_past_t30_and_t5_without_reconstructing(tmp_path):
    source = tmp_path / "source.json"
    source.write_text(json.dumps({
        "watch": {
            "F1": {
                "match_id": "F1",
                "league": "L",
                "home": "H",
                "away": "A",
                "kickoff_hkt": "2026-08-29T23:00:00+08:00",
                "stages": [
                    {
                        "stage": "T-30",
                        "ts": "2026-08-29T22:30:01+08:00",
                        "lead": {
                            "market": "HIL", "label": "O 2.5",
                            "odds": 1.90, "prob": 0.55, "ev": 0.045,
                        },
                    },
                    {
                        "stage": "T-5",
                        "ts": "2026-08-29T22:55:01+08:00",
                        "lead": {
                            "market": "HIL", "label": "O 2.5",
                            "odds": 1.95, "prob": 0.56, "ev": 0.092,
                        },
                    },
                ],
            }
        }
    }), encoding="utf-8")
    stale_public = tmp_path / "public.json"
    stale_public.write_text('{"matches":[]}', encoding="utf-8")
    ledger = tmp_path / "v2.json"
    result = _run_tick(
        data_path=stale_public,
        source_ledger_path=source,
        ledger_path=ledger,
        dashboard_path=tmp_path / "dashboard.json",
        sent_log_path=tmp_path / "sent.jsonl",
        now_utc=datetime(2026, 8, 29, 16, 5, tzinfo=timezone.utc),
        window_hours=48,
        dry_run=False,
    )
    assert result["fired_count"] == 2
    saved = json.loads(ledger.read_text(encoding="utf-8"))
    stages = saved["fixtures"]["F1"]["stages"]
    assert set(stages) == {"T-30", "T-5"}
    assert stages["T-30"]["legacy_stage_ts"] == "2026-08-29T22:30:01+08:00"
    assert stages["T-5"]["legacy_stage_ts"] == "2026-08-29T22:55:01+08:00"
    assert stages["T-5"]["projection_catchup"] is True
    assert stages["T-5"]["publish_decision"] is False
