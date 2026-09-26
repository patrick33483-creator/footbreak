"""Locate recent sports notification records. Never output credentials or send."""
import json,sqlite3,subprocess,re
from pathlib import Path
def run(a):
    p=subprocess.run(a,capture_output=True,text=True,timeout=60)
    return p.stdout if p.returncode==0 else ""
out={"containers":run(["docker","ps","--format","{{.Names}} {{.Image}}"]),"ops_states":{},"db_schemas":{}}
for root in [Path("/opt/crown-radar-v2/ops"),Path("/opt/footbreak/ops")]:
    if root.exists():
        for p in root.glob("*/state.json"):
            if any(x in str(p) for x in ["tg","notif","health"]):
                try:out["ops_states"][str(p)]=json.loads(p.read_text())
                except Exception:pass
# Probe sports app DB schemas, not environment/config/keys.
out["opt_dirs"]=[p.name for p in Path("/opt").iterdir() if p.is_dir()]
paths=[]
for root in Path("/opt").iterdir():
    if root.is_dir() and any(x in root.name for x in ["crown","foot","radar"]):
        for pattern in ["*.db","data/*.db","app/data/*.db","backend/data/*.db","state/*.db"]:
            paths.extend(root.glob(pattern))
for path in sorted(set(paths)):
    try:
        db=sqlite3.connect(f"file:{path}?mode=ro",uri=True,timeout=4);db.row_factory=sqlite3.Row
        tables=[dict(r) for r in db.execute("SELECT name,sql FROM sqlite_master WHERE type='table'")]
        relevant=[r for r in tables if any(x in r["name"].lower() for x in ["notif","telegram","outbox","alert","delivery","message"])]
        rows={}
        for t in relevant:
            name=t["name"]
            if not name.replace("_","").isalnum():continue
            raw=[dict(r) for r in db.execute(f'SELECT * FROM "{name}" ORDER BY rowid DESC LIMIT 12')]
            # Sports notifications only, strip credential fields.
            rows[name]=[{k:v for k,v in r.items() if not any(x in k.lower() for x in ["token","password","secret","api_key"])} for r in raw]
        out["db_schemas"][str(path)]={"relevant_tables":relevant,"recent_rows":rows,"other_tables":[r["name"] for r in tables if r not in relevant]}
        db.close()
    except Exception as e:out["db_schemas"][str(path)]={"error":type(e).__name__}
text=json.dumps(out,ensure_ascii=False,indent=2)
text=re.sub(r'bot\d+:[A-Za-z0-9_-]+','bot[redacted]',text)
print(text)
