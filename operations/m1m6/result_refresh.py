"""Durable per-match settlement events; one existing search service, no Telegram."""
import sqlite3
import subprocess
import time
from pathlib import Path
from contextlib import closing

DB=Path("/var/lib/crown-m1m6/ledger.sqlite")

def schema(db):
    db.execute("""CREATE TABLE IF NOT EXISTS result_refresh_events(
        sid TEXT PRIMARY KEY,queued_at INTEGER NOT NULL,completed_at INTEGER)""")
    db.execute("""CREATE TABLE IF NOT EXISTS result_refresh_meta(
        key TEXT PRIMARY KEY,value INTEGER NOT NULL)""")
    if "revision" not in {r[1] for r in db.execute("PRAGMA table_info(result_refresh_events)")}:
        db.execute("ALTER TABLE result_refresh_events ADD COLUMN revision INTEGER NOT NULL DEFAULT 1")

def enqueue(db,sid,now,correction=False):
    schema(db)
    if correction:
        db.execute("""INSERT INTO result_refresh_events(sid,queued_at,revision)
            VALUES(?,?,1) ON CONFLICT(sid) DO UPDATE SET queued_at=excluded.queued_at,
            completed_at=NULL,revision=result_refresh_events.revision+1""",(str(sid),now))
        return
    db.execute("INSERT OR IGNORE INTO result_refresh_events(sid,queued_at) VALUES(?,?)",(str(sid),now))

def pending(db):
    schema(db)
    return [r[0] for r in db.execute("SELECT sid FROM result_refresh_events WHERE completed_at IS NULL ORDER BY queued_at,sid")]

def snapshot():
    if not DB.exists():
        return []
    with closing(sqlite3.connect(DB,timeout=10)) as db,db:
        return pending(db)

def snapshot_tokens():
    if not DB.exists():
        return []
    with closing(sqlite3.connect(DB,timeout=10)) as db,db:
        schema(db)
        return [{"sid":r[0],"revision":r[1]} for r in db.execute(
            "SELECT sid,revision FROM result_refresh_events WHERE completed_at IS NULL ORDER BY queued_at,sid")]

def complete_tokens(tokens,now):
    if not tokens or not DB.exists():
        return
    with closing(sqlite3.connect(DB,timeout=10)) as db,db:
        schema(db)
        db.executemany("""UPDATE result_refresh_events SET completed_at=?
            WHERE sid=? AND revision=? AND completed_at IS NULL""",
                       [(now,t["sid"],t["revision"]) for t in tokens])

def status(db):
    exists=db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='result_refresh_events'").fetchone()
    if not exists:
        return {"enabled":True,"pending":0,"completed":0,"latest_queued_at":None,"latest_completed_at":None}
    row=db.execute("""SELECT SUM(completed_at IS NULL),SUM(completed_at IS NOT NULL),
                      MAX(queued_at),MAX(completed_at) FROM result_refresh_events""").fetchone()
    return {"enabled":True,"pending":row[0] or 0,"completed":row[1] or 0,
            "latest_queued_at":row[2],"latest_completed_at":row[3]}

def complete(sids,now):
    if not sids or not DB.exists():
        return
    with closing(sqlite3.connect(DB,timeout=10)) as db,db:
        schema(db)
        db.executemany("UPDATE result_refresh_events SET completed_at=? WHERE sid=? AND completed_at IS NULL",
                       [(now,sid) for sid in sids])

def dispatch():
    if not DB.exists():
        return {"pending":0}
    now=int(time.time()*1000)
    with closing(sqlite3.connect(DB,timeout=10)) as db,db:
        ids=pending(db)
        if not ids:
            return {"pending":0}
        row=db.execute("SELECT value FROM result_refresh_meta WHERE key='last_attempt'").fetchone()
        if row and now-row[0]<60000:
            return {"pending":len(ids),"retry_cooldown":True}
        active=subprocess.run(["systemctl","is-active","crown-strategy-search.service"],
                              capture_output=True,text=True,timeout=5)
        if active.stdout.strip() in ("active","activating","reloading"):
            return {"pending":len(ids),"busy":True}
        db.execute("INSERT OR REPLACE INTO result_refresh_meta VALUES('last_attempt',?)",(now,))
    result=subprocess.run(["systemctl","start","--no-block","crown-strategy-search.service"],
                          capture_output=True,timeout=10)
    return {"pending":len(ids),"requested":result.returncode==0}
