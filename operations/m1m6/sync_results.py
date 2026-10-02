"""Bounded, exact-ID final-score repair for the existing strategy history.

No betting, odds, fixture, model or notification writes. Defaults to read-only.
"""
import argparse
import datetime as dt
import fcntl
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import time
import unicodedata

DB = "/opt/crown-radar-v2/data/crown.db"
REPORT = Path("/var/www/crownsystem-v3/strategy_merged.json")
STATE = Path("/var/lib/crown-strategy-results")
M1M6_LEDGER = Path("/var/lib/crown-m1m6/ledger.sqlite")
HKT = dt.timezone(dt.timedelta(hours=8))


def normalize(value):
    text = unicodedata.normalize("NFKC", str(value or ""))
    text = re.sub(r"\((?:中|中立|中立场地)\)", "", text)
    return re.sub(r"\s+", "", text).casefold()


def parse_header(raw, match, now):
    """Fail closed unless final, identity aligned, and regulation score explicit."""
    cells = raw.strip().split("^")
    if len(cells) < 16:
        return None, "malformed_header"
    # The mobile header carries the same provider fixture ID near its tail.
    # Do not rely on the requested URL alone to establish identity.
    if len(cells) < 70 or cells[-4].strip() != str(match["sid"]):
        return None, "fixture_id_mismatch"
    if cells[4].strip() != "-1":
        return None, "not_final"
    if not all(re.fullmatch(r"\d{1,2}", cells[i]) for i in (10, 11)):
        return None, "invalid_score"
    home, away = int(cells[10]), int(cells[11])
    if max(home, away) > 30:
        return None, "score_out_of_range"
    try:
        kickoff = int(dt.datetime.strptime(cells[5], "%Y%m%d%H%M%S")
                      .replace(tzinfo=HKT).timestamp() * 1000)
    except ValueError:
        return None, "invalid_kickoff"
    if abs(kickoff - match["kickoff_utc"]) > 15 * 60000:
        return None, "kickoff_mismatch"
    if now - kickoff < 100 * 60000:
        return None, "too_recent"
    if normalize(cells[0]) != normalize(match["home"]) or normalize(cells[1]) != normalize(match["away"]):
        return None, "team_mismatch"
    return dict(sid=str(match["sid"]), status="完", home_score=home,
                away_score=away, source_kickoff=kickoff, source_home=cells[0],
                source_away=cells[1], source_league=cells[15]), None


def connect(readonly=True):
    connection = sqlite3.connect("file:" + DB + ("?mode=ro" if readonly else "?mode=rw"),
                                 uri=True, timeout=10)
    connection.row_factory = sqlite3.Row
    return connection


def candidates(now):
    # The merged page is supplementary, not the sole source of strategy fires.
    # Crown Radar's original strategy page reads heavy_notified_rule directly.
    ids = set()
    urgent_ids = set()
    # New notifier has its own ledger; every matched observation contributes to
    # rolling gates, including matches suppressed while another batch is open.
    if M1M6_LEDGER.exists():
        with sqlite3.connect(f"file:{M1M6_LEDGER}?mode=ro", uri=True) as ledger:
            ids.update(str(r[0]) for r in ledger.execute(
                "SELECT sid FROM observations UNION SELECT sid FROM items"))
            urgent_ids.update(str(r[0]) for r in ledger.execute(
                "SELECT i.sid FROM items i JOIN batches b ON i.batch_id=b.id "
                "WHERE b.status='open' AND i.status IN ('sent','sending','uncertain') "
                "AND i.result_json IS NULL"))
    if REPORT.exists():
        try:
            report = json.loads(REPORT.read_text())
            ids.update(str(f["sid"]) for f in report["fires"]
                       if f.get("status") != "finished")
        except (ValueError, KeyError, TypeError):
            pass  # A malformed projection must not block the authoritative DB.
    attempts_path = STATE / "last-attempts.json"
    attempts = json.loads(attempts_path.read_text()) if attempts_path.exists() else {}
    selected, already_final = [], []
    with connect() as db:
        # The dynamic grid must not see only old notified winners. Include all
        # six-snapshot fixtures in the approved Crown research date range.
        ids.update(str(r[0]) for r in db.execute("""
            SELECT m.sid FROM matches m JOIN crown_snapshots s ON s.sid=m.sid
            WHERE m.kickoff_utc>=1789023600000 AND m.kickoff_utc<?
              AND s.stage IN ('initial','T30','T5') AND s.market IN ('AH','OU')
            GROUP BY m.sid HAVING COUNT(DISTINCT s.stage||':'||s.market)=6
            """,(now-100*60000,)))
        ids.update(str(r[0]) for r in db.execute(
            "SELECT sid FROM heavy_notified_rule UNION SELECT sid FROM heavy_notified"))
        for sid in sorted(ids):
            m = db.execute("SELECT * FROM matches WHERE sid=?", (sid,)).fetchone()
            if not m or not sid.isdigit() or now - m["kickoff_utc"] < 100 * 60000:
                continue
            prior = db.execute("SELECT * FROM finished_matches WHERE sid=?", (sid,)).fetchone()
            if prior and prior["status"] in ("推迟", "延期", "取消", "腰斩", "中断"):
                continue  # These require a verified reschedule, never a guessed FT.
            if prior and prior["status"] == "完" and prior["home_score"] is not None and prior["away_score"] is not None:
                already_final.append(sid)
                continue
            selected.append(dict(m))
    # Rotate failed IDs rather than allowing 24 old failures to starve new games.
    return sorted(selected, key=lambda x: (str(x["sid"]) not in urgent_ids,
                                           attempts.get(str(x["sid"]), 0),
                                           x["kickoff_utc"]))[:24], already_final


def write_results(rows, captured_at):
    written, conflicts = [], []
    with connect(False) as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute("""CREATE TABLE IF NOT EXISTS strategy_result_sync_audit (
          id INTEGER PRIMARY KEY, sid TEXT NOT NULL, observed_at INTEGER NOT NULL,
          written_at INTEGER NOT NULL, source_url TEXT NOT NULL,
          previous_json TEXT, evidence_json TEXT NOT NULL)""")
        for row in rows:
            sid = row["sid"]
            current_match = db.execute("SELECT * FROM matches WHERE sid=?", (sid,)).fetchone()
            if not current_match or current_match["kickoff_utc"] != row["expected_kickoff"]:
                conflicts.append({"sid": sid, "reason": "concurrent_fixture_change"})
                continue
            if normalize(current_match["home"]) != normalize(row["source_home"]) or normalize(current_match["away"]) != normalize(row["source_away"]):
                conflicts.append({"sid": sid, "reason": "concurrent_identity_change"})
                continue
            prior = db.execute("SELECT * FROM finished_matches WHERE sid=?", (sid,)).fetchone()
            if prior and prior["status"] in ("推迟", "延期", "取消", "腰斩", "中断"):
                conflicts.append({"sid": sid, "reason": "concurrent_postponement"})
                continue
            if prior and prior["status"] == "完" and prior["home_score"] is not None and prior["away_score"] is not None:
                if (prior["home_score"], prior["away_score"]) != (row["home_score"], row["away_score"]):
                    conflicts.append({"sid": sid, "reason": "existing_final_conflict"})
                continue
            stamp = int(time.time() * 1000)
            db.execute("""INSERT INTO finished_matches
              (sid,status,home_score,away_score,ht_home_score,ht_away_score,fetched_at)
              VALUES(?,'完',?,?,NULL,NULL,?)
              ON CONFLICT(sid) DO UPDATE SET status=excluded.status,
              home_score=excluded.home_score,away_score=excluded.away_score,
              fetched_at=excluded.fetched_at""",
                       (sid, row["home_score"], row["away_score"], captured_at))
            db.execute("""INSERT INTO strategy_result_sync_audit
              (sid,observed_at,written_at,source_url,previous_json,evidence_json)
              VALUES(?,?,?,?,?,?)""", (sid, captured_at, stamp, row["source_url"],
              json.dumps(dict(prior), ensure_ascii=False) if prior else None,
              json.dumps(row, ensure_ascii=False)))
            written.append(sid)
    return written, conflicts


def atomic_json(path, payload):
    temp = path.with_suffix(".tmp")
    with temp.open("w") as stream:
        json.dump(payload, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, path)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()
    if args.write:
        STATE.mkdir(mode=0o700, parents=True, exist_ok=True)
        lock = (STATE / "run.lock").open("w")
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    now = int(time.time() * 1000)
    matches, already_final = candidates(now)
    sys.path.insert(0, "/opt/footbreak")
    from crown.titan import TitanClient
    from crown.config import settings
    client = TitanClient(settings())
    deadline = time.monotonic() + 85
    accepted, skipped, errors = [], [], []
    for match in matches:
        if time.monotonic() >= deadline:
            break
        sid = match["sid"]
        url = f"https://livestatic.titan007.com/phone/txt/analysisheader/cn/{sid[0]}/{sid[1:3]}/{sid}.txt"
        try:
            raw = client._read(url, encoding="utf-8", timeout=5, attempts=1,
                               hard_deadline=min(7, max(.1, deadline-time.monotonic())))
            row, reason = parse_header(raw, match, int(time.time() * 1000))
            if row is None:
                skipped.append({"sid": sid, "reason": reason})
                continue
            row.update(source_url=url, expected_kickoff=match["kickoff_utc"],
                       observed_at=int(time.time() * 1000), raw_header=raw)
            accepted.append(row)
        except Exception as exc:
            errors.append({"sid": sid, "type": type(exc).__name__})
    captured = int(time.time() * 1000)
    written, conflicts = write_results(accepted, captured) if args.write and accepted else ([], [])
    pending_count = len(matches) - len(written)
    payload = dict(version=1, checked_at_hkt=dt.datetime.now(HKT).isoformat(),
                   dry_run=not args.write, candidates=len(matches), already_final=already_final,
                   accepted=accepted, written=written, skipped=skipped, errors=errors,
                   conflicts=conflicts, unprocessed=len(matches)-len(accepted)-len(skipped)-len(errors),
                   status="degraded" if errors or conflicts else "pending" if pending_count else "ok",
                   pending_candidates_after=pending_count)
    if args.write:
        attempts_path = STATE / "last-attempts.json"
        attempts = json.loads(attempts_path.read_text()) if attempts_path.exists() else {}
        for row in accepted + skipped + errors:
            attempts[str(row["sid"])] = captured
        atomic_json(attempts_path, attempts)
        atomic_json(STATE / "last-run.json", payload)
    print(json.dumps(payload, ensure_ascii=False, indent=2), flush=True)
    if errors or conflicts:
        sys.exit(1)


if __name__ == "__main__":
    main()
