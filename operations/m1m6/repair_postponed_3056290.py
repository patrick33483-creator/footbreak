"""Explicitly approved one-fixture reschedule repair; no general policy change."""
import fcntl
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
from contextlib import closing

from sync_results import parse_header, normalize
from ops import atomic

SID="3056290"
KO=1791216000000
URL="https://livestatic.titan007.com/phone/txt/analysisheader/cn/3/05/3056290.txt"
SECONDARY="https://www.footlive.com/score/torreense-u23-vs-estoril-praia-u23-2026-09-15/"
BASE=Path("/var/lib/crown-m1m6")
DB=Path("/opt/crown-radar-v2/data/crown.db")


def validate(match,prior,raw,observed_at):
    if str(match.get("sid"))!=SID or match.get("kickoff_utc")!=KO:
        raise ValueError("Wrong fixture or rescheduled kickoff")
    if (normalize(match.get("home")),normalize(match.get("away")))!=(
            normalize("杜连斯U23"),normalize("埃斯托里尔U23")):
        raise ValueError("Wrong teams")
    if not prior or prior.get("status") not in ("推迟","延期"):
        raise ValueError("Not the approved old postponed record")
    if any(prior.get(k) is not None for k in ("home_score","away_score")):
        raise ValueError("Unexpected prior score")
    if not 0<prior.get("fetched_at",0)<KO:
        raise ValueError("Postponement is not older than the rescheduled kickoff")
    parsed,reason=parse_header(raw,match,observed_at)
    if reason or not parsed or parsed["source_kickoff"]!=KO:
        raise ValueError(f"Fresh primary source rejected: {reason}")
    if (parsed["home_score"],parsed["away_score"])!=(2,3):
        raise ValueError("Primary score disagrees with independently checked 2:3")
    return {**parsed,"expected_kickoff":KO,"observed_at":observed_at,
            "source_url":URL,"raw_header":raw,
            "repair":"approved_single_fixture_verified_reschedule",
            "secondary_url":SECONDARY,"secondary_score":"2:3"}


def apply_verified(db,expected_match,expected_prior,evidence,now):
    """One atomic row change with optimistic guards and immutable previous data."""
    db.execute("BEGIN IMMEDIATE")
    try:
        match=db.execute("SELECT * FROM matches WHERE sid=?",(SID,)).fetchone()
        prior=db.execute("SELECT * FROM finished_matches WHERE sid=?",(SID,)).fetchone()
        if not match or not prior:
            raise ValueError("Required fixture records disappeared")
        match,prior=dict(match),dict(prior)
        if prior.get("status")=="完" and (prior.get("home_score"),prior.get("away_score"))==(2,3):
            db.rollback()
            return {"written":False,"already_correct":True}
        if prior!=expected_prior:
            raise ValueError("Concurrent result change; no overwrite")
        if any(match.get(k)!=expected_match.get(k) for k in ("sid","kickoff_utc","home","away")):
            raise ValueError("Concurrent fixture change; no overwrite")
        checked=validate(match,prior,evidence["raw_header"],evidence["observed_at"])
        if now-evidence["observed_at"]>120000 or now<evidence["observed_at"]:
            raise ValueError("Primary evidence expired")
        cursor=db.execute("""UPDATE finished_matches SET status='完',home_score=2,away_score=3,
            fetched_at=? WHERE sid=? AND status=? AND fetched_at=?
            AND home_score IS NULL AND away_score IS NULL""",
                          (evidence["observed_at"],SID,prior["status"],prior["fetched_at"]))
        if cursor.rowcount!=1:
            raise ValueError("Guarded update did not affect exactly one row")
        db.execute("""INSERT INTO strategy_result_sync_audit
            (sid,observed_at,written_at,source_url,previous_json,evidence_json)
            VALUES(?,?,?,?,?,?)""",(SID,evidence["observed_at"],now,URL,
                                  json.dumps(prior,ensure_ascii=False),
                                  json.dumps(checked,ensure_ascii=False)))
        after=dict(db.execute("SELECT * FROM finished_matches WHERE sid=?",(SID,)).fetchone())
        db.commit()
        return {"written":True,"previous":prior,"current":after}
    except Exception:
        db.rollback()
        raise


def repair():
    here=Path(__file__).parent
    tests=subprocess.run(["python3","-m","unittest","discover","-s",str(here),"-p",
                          "test_postponed_repair.py"],capture_output=True,text=True,timeout=60)
    assert tests.returncode==0,tests.stderr
    # Only the existing importer and notifier locks; never clear accounting locks.
    with Path("/var/lib/crown-strategy-results/run.lock").open("a") as importer_lock, \
            (BASE/"run.lock").open("a") as notifier_lock:
        fcntl.flock(importer_lock,fcntl.LOCK_EX)
        fcntl.flock(notifier_lock,fcntl.LOCK_EX)
        with closing(sqlite3.connect(f"file:{DB}?mode=ro",uri=True)) as read:
            read.row_factory=sqlite3.Row
            match=dict(read.execute("SELECT * FROM matches WHERE sid=?",(SID,)).fetchone())
            prior=dict(read.execute("SELECT * FROM finished_matches WHERE sid=?",(SID,)).fetchone())
        if prior["status"]=="完" and (prior["home_score"],prior["away_score"])==(2,3):
            return {"summary":{"action":"repair_postponed_3056290","written":False,"already_correct":True,
                               "manual_unlock":False,"forced_sends":0}}
        sys.path.insert(0,"/opt/footbreak")
        from crown.titan import TitanClient
        from crown.config import settings
        raw=TitanClient(settings())._read(URL,encoding="utf-8",timeout=5,attempts=1,hard_deadline=7)
        observed=int(time.time()*1000)
        proof=validate(match,prior,raw,observed)
        backup=BASE/f"postponed-repair-{SID}-{observed}.json"
        atomic(backup,{"match":match,"previous_result":prior,"evidence":proof})
        protected=[Path("/etc/crown-m1m6.json"),BASE/"registry.json",BASE/"performance_epoch.json",
                   Path("/opt/crown-m1m6/policy.py"),Path("/opt/crown-strategy-results/sync_results.py")]
        before={str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in protected}
        with closing(sqlite3.connect(f"file:{DB}?mode=rw",uri=True,timeout=15)) as write:
            write.row_factory=sqlite3.Row
            changed=apply_verified(write,match,prior,proof,int(time.time()*1000))
        assert before=={str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in protected}
        receipt={"action":"repair_postponed_3056290","at_ms":observed,"tests":tests.stderr,
                 "backup":str(backup),"evidence":proof,**changed,
                 "protected_files_unchanged":True,"manual_unlock":False,
                 "notifier_or_search_forced":False,"forced_sends":0}
        atomic(BASE/"postponed_3056290_repair_receipt.json",receipt)
    return {"summary":receipt}


def verify():
    now=int(time.time()*1000)
    with (BASE/"run.lock").open("a") as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        public=json.loads((BASE/"status.json").read_text())
        mirrors=all(json.loads(p.read_text())==public for p in (
            Path("/var/www/crownsystem-v3/m1m6_status.json"),
            Path("/opt/crown-radar-v2/data/m1m6_status.json")))
        with closing(sqlite3.connect(f"file:{BASE/'ledger.sqlite'}?mode=ro",uri=True)) as db:
            db.row_factory=sqlite3.Row
            item=dict(db.execute("SELECT * FROM items WHERE bet_key=?",(SID+":OU:over:2.75",)).fetchone())
            item["payload"]=json.loads(item["payload"])
            item["result_json"]=json.loads(item["result_json"]) if item["result_json"] else None
            batches=[dict(r) for r in db.execute("SELECT * FROM strategy_batches WHERE id IN (96,97)")]
            event=db.execute("SELECT * FROM result_refresh_events WHERE sid=?",(SID,)).fetchone()
            event=dict(event) if event else None
        with closing(sqlite3.connect(f"file:{DB}?mode=ro",uri=True)) as db:
            db.row_factory=sqlite3.Row
            result=dict(db.execute("SELECT * FROM finished_matches WHERE sid=?",(SID,)).fetchone())
    return {"summary":{"action":"verify_postponed_3056290","at_ms":now,"database_writes":0,
                       "forced_sends":0,"mirrors_equal":mirrors},
            "official_result":result,"item":item,"strategy_batches":batches,"result_refresh_event":event,
            "public_row":[r for r in public["ledger"] if r["sid"]==SID],
            "D0105":[r for r in public["rules"] if r["id"]=="D0105"],
            "performance":public["performance_period"],
            "research":json.loads((BASE/"research_status.json").read_text()),
            "repair_receipt":json.loads((BASE/"postponed_3056290_repair_receipt.json").read_text())
                 if (BASE/"postponed_3056290_repair_receipt.json").exists() else None}
