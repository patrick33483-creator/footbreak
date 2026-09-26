"""Read-only notified selections and stored results, never send or mutate."""
import json,sqlite3
from pathlib import Path
from datetime import datetime,timezone
DAY=1790352000000  # 2026-09-26 00:00 HKT
out={"asof_utc":datetime.now(timezone.utc).isoformat(),"v3_states":{},"v3_pages":{}}
sids=set()
for name in ["crown-goldpool-notify","u1-notify","ce-notify"]:
    p=Path("/var/lib")/name/"state.json"
    j=json.loads(p.read_text())
    rows={k:v for k,v in j.get("sent",{}).items() if v.get("ts",0)*1000>=DAY}
    out["v3_states"][name]=rows
    sids.update(k.split(":")[0] for k in rows)
for name in ["strategy_merged","results","matches","ad_flat_ce_fires","ogb_fires","u1_fires","b_raw_notify_receipts","b_ahshift_receipts"]:
    j=json.loads((Path("/var/www/crownsystem-v3")/(name+".json")).read_text())
    rows=j.get("fires",j.get("matches",j.get("receipts",{})))
    if isinstance(rows,dict): rows=[dict(v,sid=k) for k,v in rows.items()]
    selected=[r for r in rows if str(r.get("sid")) in sids or (r.get("ko_utc_ms") or r.get("kickoff_utc_ms") or 0)>=DAY]
    out["v3_pages"][name]={"generated_at":j.get("generated_at",j.get("generated_utc")),"rows":selected}
db=sqlite3.connect("file:/opt/crown-radar-v2/data/crown.db?mode=ro",uri=True,timeout=10)
db.row_factory=sqlite3.Row
db.execute("PRAGMA query_only=ON");db.execute("BEGIN")
out["radar_notices"]=[dict(r) for r in db.execute("SELECT * FROM heavy_notified_rule WHERE notified_at>=? ORDER BY notified_at",(DAY,))]
sids.update(str(r["sid"]) for r in out["radar_notices"])
out["alow_receipts"]=[dict(r) for r in db.execute("SELECT * FROM alow_notification_receipts WHERE notified_at>=?",(DAY,))]
out["tables"]=[dict(r) for r in db.execute("SELECT name,sql FROM sqlite_master WHERE type='table' AND (name LIKE '%result%' OR name='matches')")]
out["source_matches"]={}
out["other_results"]={}
for sid in sorted(sids):
    r=db.execute("SELECT * FROM matches WHERE sid=?",(sid,)).fetchone()
    out["source_matches"][sid]=dict(r) if r else None
for t in out["tables"]:
    name=t["name"]
    if "result" not in name or not name.replace("_","").isalnum():continue
    cols=[r[1] for r in db.execute(f'PRAGMA table_info("{name}")')]
    if "sid" in cols and sids:
        out["other_results"][name]=[dict(r) for r in db.execute(f'SELECT * FROM "{name}" WHERE sid IN ({",".join("?" for _ in sids)})',list(sids))]
db.close()
print(json.dumps(out,ensure_ascii=False,indent=2))
