"""Synchronize corrected final scores without resending or touching batch locks."""
import json
from policy import settle, valid_result
from result_refresh import enqueue

VERSION = "score-revision-sync-v1"


def schema(db):
    db.execute("""CREATE TABLE IF NOT EXISTS result_source_versions(
        sid TEXT PRIMARY KEY,score TEXT NOT NULL,source_at INTEGER NOT NULL,
        observed_at INTEGER NOT NULL,revision INTEGER NOT NULL DEFAULT 1)""")
    db.execute("""CREATE TABLE IF NOT EXISTS result_score_corrections(
        id INTEGER PRIMARY KEY,sid TEXT NOT NULL,observed_at INTEGER NOT NULL,
        source_at INTEGER NOT NULL,old_score TEXT,new_score TEXT NOT NULL,
        kind TEXT NOT NULL,bet_key TEXT,old_result_json TEXT,new_result_json TEXT,
        source_json TEXT NOT NULL,version TEXT NOT NULL)""")


def synchronize(db, finished, now, kickoffs):
    """Atomic audit + correction + revisioned event. Caller owns its connection.

    Only final-to-final updates are in scope. Invalid/nonfinal/older evidence is
    ignored. Original notification payload, delivery and all locks stay intact.
    First source observation seeds a baseline, not thousands of refresh events.
    Existing settled items are compared independently, repairing pre-v1 drift.
    """
    schema(db)
    db.execute("SAVEPOINT score_revision_sync")
    counts = {"sources_changed": 0, "items_corrected": 0, "stale_ignored": 0,
              "queued_sids": [], "version": VERSION}
    try:
        items = [dict(r) for r in db.execute("""SELECT bet_key,sid,ko,payload,result_json
            FROM items WHERE status IN ('sent','sending','uncertain')
            AND result_json IS NOT NULL""")]
        kos = {str(s): ko for s, ko in kickoffs.items()}
        kos.update({str(r["sid"]): r["ko"] for r in items})
        tracked = {r["sid"]: dict(r) for r in db.execute("SELECT * FROM result_source_versions")}
        accepted = {}
        changed = set()
        for sid, ko in kos.items():
            f = finished.get(sid)
            if not valid_result(f, ko, now):
                continue
            score = f"{f['home_score']}:{f['away_score']}"
            at = f["fetched_at"]
            previous = tracked.get(sid)
            if previous and at < previous["source_at"]:
                counts["stale_ignored"] += 1
                continue
            accepted[sid] = f
            if previous is None:
                db.execute("INSERT INTO result_source_versions VALUES(?,?,?,?,1)",
                           (sid, score, at, now))
            elif previous["score"] != score:
                db.execute("""INSERT INTO result_score_corrections
                    (sid,observed_at,source_at,old_score,new_score,kind,source_json,version)
                    VALUES(?,?,?,?,?,'source_revision',?,?)""",
                           (sid, now, at, previous["score"], score,
                            json.dumps(f, ensure_ascii=False), VERSION))
                db.execute("""UPDATE result_source_versions SET score=?,source_at=?,
                    observed_at=?,revision=revision+1 WHERE sid=?""", (score, at, now, sid))
                changed.add(sid)
                counts["sources_changed"] += 1
            elif at > previous["source_at"]:
                db.execute("UPDATE result_source_versions SET source_at=? WHERE sid=?", (at, sid))
        for item in items:
            sid = str(item["sid"])
            f = accepted.get(sid)
            if not f:
                continue
            old = json.loads(item["result_json"])
            if f["fetched_at"] < (old.get("result_at") or 0):
                counts["stale_ignored"] += 1
                continue
            new = settle(json.loads(item["payload"]), f)
            if all(old.get(k) == new[k] for k in ("score", "result", "pnl")):
                continue
            encoded = json.dumps(new, ensure_ascii=False)
            db.execute("""INSERT INTO result_score_corrections
                (sid,observed_at,source_at,old_score,new_score,kind,bet_key,
                 old_result_json,new_result_json,source_json,version)
                VALUES(?,?,?,?,?,'notification_settlement',?,?,?,?,?)""",
                       (sid, now, f["fetched_at"], old.get("score"), new["score"],
                        item["bet_key"], item["result_json"], encoded,
                        json.dumps(f, ensure_ascii=False), VERSION))
            db.execute("UPDATE items SET result_json=? WHERE bet_key=?", (encoded, item["bet_key"]))
            changed.add(sid)
            counts["items_corrected"] += 1
        for sid in sorted(changed):
            enqueue(db, sid, now, correction=True)
        counts["queued_sids"] = sorted(changed)
        db.execute("RELEASE SAVEPOINT score_revision_sync")
        db.commit()
        return counts
    except Exception:
        db.execute("ROLLBACK TO SAVEPOINT score_revision_sync")
        db.execute("RELEASE SAVEPOINT score_revision_sync")
        raise
