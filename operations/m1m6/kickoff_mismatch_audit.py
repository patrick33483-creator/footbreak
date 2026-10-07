"""Read-only source reconciliation for every currently eligible missing result."""
import datetime as dt
import json
from pathlib import Path
import sqlite3
import sys
import time
from contextlib import closing
from sync_results import parse_header, normalize, HKT, REPORT

DB = "/opt/crown-radar-v2/data/crown.db"
LEDGER = "/var/lib/crown-m1m6/ledger.sqlite"


def audit():
    now = int(time.time()*1000)
    with closing(sqlite3.connect(f"file:{LEDGER}?mode=ro", uri=True)) as ledger:
        ledger.row_factory = sqlite3.Row
        ledger.execute("PRAGMA query_only=ON")
        ids = {str(r[0]) for r in ledger.execute("SELECT sid FROM observations UNION SELECT sid FROM items")}
        items = [dict(r) for r in ledger.execute(
            "SELECT sid,status,batch_id,ko,ack_at,result_json FROM items")]
        observation_rules = {}
        for r in ledger.execute("SELECT sid,rule_id FROM observations"):
            observation_rules.setdefault(str(r[0]), []).append(r[1])
    if REPORT.exists():
        projection = json.loads(REPORT.read_text())
        ids.update(str(f["sid"]) for f in projection.get("fires", []) if f.get("status") != "finished")
    selected, exceptions = [], []
    final_count = too_recent = missing_fixture = 0
    with closing(sqlite3.connect(f"file:{DB}?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        ids.update(str(r[0]) for r in db.execute("""
            SELECT m.sid FROM matches m JOIN crown_snapshots s ON s.sid=m.sid
            WHERE m.kickoff_utc>=1789023600000 AND m.kickoff_utc<?
            AND s.stage IN ('initial','T30','T5') AND s.market IN ('AH','OU')
            GROUP BY m.sid HAVING COUNT(DISTINCT s.stage||':'||s.market)=6""", (now-100*60000,)))
        ids.update(str(r[0]) for r in db.execute(
            "SELECT sid FROM heavy_notified_rule UNION SELECT sid FROM heavy_notified"))
        for sid in sorted(ids):
            row = db.execute("SELECT * FROM matches WHERE sid=?", (sid,)).fetchone()
            if not row:
                missing_fixture += 1
                continue
            match = dict(row)
            if now-match["kickoff_utc"] < 100*60000:
                too_recent += 1
                continue
            prior = db.execute("SELECT * FROM finished_matches WHERE sid=?", (sid,)).fetchone()
            if prior and prior["status"] in ("推迟", "延期", "取消", "腰斩", "中断"):
                exceptions.append({"sid": sid, "home": match["home"], "away": match["away"],
                                   "status": prior["status"], "kickoff_utc": match["kickoff_utc"]})
            elif prior and prior["status"] == "完" and prior["home_score"] is not None and prior["away_score"] is not None:
                final_count += 1
            else:
                selected.append(match)
    sys.path.insert(0, "/opt/footbreak")
    from crown.titan import TitanClient
    from crown.config import settings
    client = TitanClient(settings())
    deadline = time.monotonic()+180
    results = []
    for match in selected:
        if time.monotonic() > deadline:
            break
        sid = str(match["sid"])
        url = f"https://livestatic.titan007.com/phone/txt/analysisheader/cn/{sid[0]}/{sid[1:3]}/{sid}.txt"
        entry = {"match": match, "source_url": url,
                 "notified_items": [i for i in items if i["sid"] == sid and i["status"] in ("sent", "sending", "uncertain")],
                 "observation_rule_ids": sorted(set(observation_rules.get(sid, [])))}
        try:
            raw = client._read(url, encoding="utf-8", timeout=5, attempts=1, hard_deadline=7)
            checked = int(time.time()*1000)
            cells = raw.strip().split("^")
            parsed, reason = parse_header(raw, match, checked)
            entry.update(parser_reason=reason, accepted_under_current_policy=bool(parsed),
                         raw_header=raw, checked_at=checked)
            if len(cells) >= 70:
                source_ko = int(dt.datetime.strptime(cells[5], "%Y%m%d%H%M%S").replace(tzinfo=HKT).timestamp()*1000)
                entry["source"] = {"sid": cells[-4], "home": cells[0], "away": cells[1],
                                   "status_code": cells[4], "home_score": cells[10],
                                   "away_score": cells[11], "kickoff_utc": source_ko,
                                   "difference_minutes": (source_ko-match["kickoff_utc"])/60000,
                                   "league": cells[15],
                                   "identity_matches": cells[-4] == sid and normalize(cells[0]) == normalize(match["home"])
                                   and normalize(cells[1]) == normalize(match["away"])}
                # Diagnostic only: this result is never passed to a writer.
                adjusted, other_reason = parse_header(raw, {**match, "kickoff_utc": source_ko}, checked)
                entry["final_if_kickoff_were_verified"] = bool(adjusted)
                entry["other_validation_reason"] = other_reason
        except Exception as exc:
            entry["error_type"] = type(exc).__name__
        results.append(entry)
    pending_sent = [i for i in items if i["status"] in ("sent", "sending", "uncertain") and not i["result_json"]]
    return {"summary": {"action": "kickoff_mismatch_readonly", "at_ms": now,
                        "scope_ids": len(ids), "already_final": final_count,
                        "too_recent": too_recent, "missing_fixture": missing_fixture,
                        "special_status_excluded": len(exceptions), "eligible_missing_results": len(selected),
                        "checked": len(results), "unprocessed": len(selected)-len(results),
                        "kickoff_mismatches": sum(r.get("parser_reason") == "kickoff_mismatch" for r in results),
                        "pending_notified_count": len(pending_sent), "database_writes": 0,
                        "forced_sends": 0, "lock_changes": 0},
            "results": results, "special_status_excluded": exceptions,
            "pending_notified": pending_sent}
