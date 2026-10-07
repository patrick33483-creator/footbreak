"""Approved exact-ID score import with a documented one-hour kickoff exception."""
import fcntl
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
from contextlib import closing

from sync_results import parse_header, normalize, write_results
from ops import atomic

SID = "3092501"
RECORDED_KO = 1791313200000
SOURCE_KO = 1791316800000
URL = "https://livestatic.titan007.com/phone/txt/analysisheader/cn/3/09/3092501.txt"
SECONDARY = "https://www.espn.com/soccer/match/_/gameId/401900572/turks-and-caicos-islands-montserrat"
BASE = Path("/var/lib/crown-m1m6")
DB = Path("/opt/crown-radar-v2/data/crown.db")
RECEIPT = BASE/"result_3092501_repair_receipt.json"


def validate(match, raw, now):
    if str(match.get("sid")) != SID or match.get("kickoff_utc") != RECORDED_KO:
        raise ValueError("Not the approved fixture/kickoff discrepancy")
    if (normalize(match.get("home")), normalize(match.get("away"))) != (
        normalize("蒙特塞拉特"), normalize("特克斯和凯科斯群岛")):
        raise ValueError("Wrong identity")
    # Only the independently verified source kickoff is passed to the unchanged
    # parser. The original fixture and notification timestamps remain untouched.
    row, reason = parse_header(raw, {**match, "kickoff_utc": SOURCE_KO}, now)
    if reason or not row or row["source_kickoff"] != SOURCE_KO:
        raise ValueError(f"Source rejected: {reason}")
    if (row["home_score"], row["away_score"]) != (4, 1):
        raise ValueError("Source disagrees with verified ESPN final")
    if row["source_league"] != "中北美国联":
        raise ValueError("Wrong competition")
    return {**row, "source_url": URL, "expected_kickoff": RECORDED_KO,
            "observed_at": now, "raw_header": raw,
            "exception": "user_approved_exact_fixture_score_import_20261007",
            "recorded_kickoff_preserved": RECORDED_KO,
            "verified_source_kickoff": SOURCE_KO, "kickoff_difference_ms": 3600000,
            "secondary_url": SECONDARY, "secondary_final_score": "4:1",
            "notification_timing_not_reclassified": True}


def repair():
    tests = subprocess.run(["python3", "-m", "unittest", "discover", "-s", str(Path(__file__).parent),
                            "-p", "test_result_3092501.py"], capture_output=True, text=True, timeout=40)
    assert tests.returncode == 0, tests.stderr
    with Path("/var/lib/crown-strategy-results/run.lock").open("a") as importer, \
         (BASE/"run.lock").open("a") as notifier:
        fcntl.flock(importer, fcntl.LOCK_EX)
        fcntl.flock(notifier, fcntl.LOCK_EX)
        with closing(sqlite3.connect(f"file:{DB}?mode=ro", uri=True)) as db:
            db.row_factory = sqlite3.Row
            match = dict(db.execute("SELECT * FROM matches WHERE sid=?", (SID,)).fetchone())
            prior = db.execute("SELECT * FROM finished_matches WHERE sid=?", (SID,)).fetchone()
            if prior:
                prior = dict(prior)
                if (prior["status"], prior["home_score"], prior["away_score"]) == ("完", 4, 1):
                    return {"summary": {"action": "repair_result_3092501", "already_correct": True,
                                        "written": [], "forced_sends": 0, "manual_unlock": False}}
                raise ValueError("Unexpected existing result; refusing overwrite")
        sys.path.insert(0, "/opt/footbreak")
        from crown.titan import TitanClient
        from crown.config import settings
        raw = TitanClient(settings())._read(URL, encoding="utf-8", timeout=5, attempts=1, hard_deadline=7)
        observed = int(time.time()*1000)
        evidence = validate(match, raw, observed)
        backup = BASE/f"result-3092501-before-{observed}.json"
        atomic(backup, {"match": match, "previous_result": prior, "evidence": evidence})
        protected = [Path("/etc/crown-m1m6.json"), BASE/"registry.json",
                     BASE/"performance_epoch.json", Path("/opt/crown-m1m6/policy.py"),
                     Path("/opt/crown-strategy-results/sync_results.py")]
        hashes = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in protected}
        # Existing official-score writer rechecks fixture identity and recorded KO,
        # inserts the previous-state/raw-evidence audit, and rejects any conflict.
        written, conflicts = write_results([evidence], observed)
        assert not conflicts and written == [SID], (written, conflicts)
        assert hashes == {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in protected}
        receipt = {"action": "repair_result_3092501", "at_ms": observed,
                   "written": written, "backup": str(backup), "evidence": evidence,
                   "tests": tests.stderr, "protected_files_unchanged": True,
                   "fixture_and_notification_times_unchanged": True,
                   "manual_unlock": False, "forced_sends": 0, "notifier_or_search_forced": False}
        atomic(RECEIPT, receipt)
    return {"summary": receipt}


def verify():
    with (BASE/"run.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        public = json.loads((BASE/"status.json").read_text())
        mirrors = all(json.loads(p.read_text()) == public for p in (
            Path("/var/www/crownsystem-v3/m1m6_status.json"),
            Path("/opt/crown-radar-v2/data/m1m6_status.json")))
        with closing(sqlite3.connect(f"file:{BASE/'ledger.sqlite'}?mode=ro", uri=True)) as db:
            db.row_factory = sqlite3.Row
            items = [dict(r) for r in db.execute("SELECT * FROM items WHERE sid=?", (SID,))]
            for item in items:
                item["payload"] = json.loads(item["payload"])
                item["result_json"] = json.loads(item["result_json"]) if item["result_json"] else None
            batch = dict(db.execute("SELECT * FROM strategy_batches WHERE id=109").fetchone())
            event = db.execute("SELECT * FROM result_refresh_events WHERE sid=?", (SID,)).fetchone()
            event = dict(event) if event else None
            accounting = dict(db.execute("SELECT * FROM batches WHERE id=58").fetchone())
        with closing(sqlite3.connect(f"file:{DB}?mode=ro", uri=True)) as db:
            db.row_factory = sqlite3.Row
            result = db.execute("SELECT * FROM finished_matches WHERE sid=?", (SID,)).fetchone()
            result = dict(result) if result else None
    return {"summary": {"action": "verify_result_3092501", "at_ms": int(time.time()*1000),
                        "mirrors_equal": mirrors, "database_writes": 0, "forced_sends": 0},
            "official_result": result, "items": items, "strategy_batch": batch, "accounting_batch": accounting,
            "event": event, "D0010": [r for r in public["rules"] if r["id"] == "D0010"],
            "public_row": [r for r in public["ledger"] if r["sid"] == SID],
            "performance": public["performance_period"],
            "research": json.loads((BASE/"research_status.json").read_text()),
            "receipt": json.loads(RECEIPT.read_text()) if RECEIPT.exists() else None}
