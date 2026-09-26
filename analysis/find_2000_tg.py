"""Locate recent sports notification records. Never output credentials or send."""
import json,sqlite3,subprocess,re,os
from pathlib import Path
def run(a):
    p=subprocess.run(a,capture_output=True,text=True,timeout=60)
    return p.stdout if p.returncode==0 else ""
out={"containers":run(["docker","ps","--format","{{.Names}} {{.Image}}"]),"ops_states":{},"db_schemas":{}}
out["heavy_paths"]=[]
p=Path("/opt/crownsystem-v3/heavy_watch.py")
if p.exists():
    for n,line in enumerate(p.read_text().splitlines(),1):
        if any(s in line for s in [".db", ".json", "Path(", "def ", "SELECT ", "FROM ", "sqlite3.connect"]):
            if not any(s in line.lower() for s in ["token","secret","password","api_key"]):
                out["heavy_paths"].append([n,line])
out["sports_web_files"]=[]
for root in [Path("/var/www/crownsystem-v3"),Path("/var/www/crownsystem-v4")]:
    if root.exists():
        for base,dirs,names in os.walk(root):
            dirs[:]=[d for d in dirs if d not in {"node_modules",".git","backups","venv",".venv","__pycache__"}]
            out["sports_web_files"].extend(str(Path(base)/n) for n in names if n.endswith((".db",".json")))
out["heavy_data"]={}
if p.exists():
    candidates=re.findall(r"""['"](/[^'"\n]+\.(?:db|json))['"]""",p.read_text())
    candidates+=out["sports_web_files"]
    for f in sorted(set(candidates)):
        q=Path(f)
        if not q.exists() or any(s in q.name.lower() for s in ["config","secret","token"]):continue
        if q.suffix==".json":
            try:
                j=json.loads(q.read_text())
                if isinstance(j,list):j=j[-35:]
                elif isinstance(j,dict):
                    j={k:v[-35:] if isinstance(v,list) else dict(list(v.items())[-35:]) if isinstance(v,dict) else v for k,v in j.items()}
                out["heavy_data"][f]=j
            except Exception:pass
        else:
            try:
                c=sqlite3.connect(f"file:{f}?mode=ro",uri=True);c.row_factory=sqlite3.Row
                tables=[r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")]
                out["heavy_data"][f]={"tables":tables,"rows":{}}
                for name in tables:
                    if any(s in name.lower() for s in ["notif","sent","heavy"]) and name.replace("_","").isalnum():
                        out["heavy_data"][f]["rows"][name]=[dict(r) for r in c.execute(f'SELECT * FROM "{name}" ORDER BY rowid DESC LIMIT 25')]
                c.close()
            except Exception:pass
for root in [Path("/opt/crown-radar-v2/ops"),Path("/opt/footbreak/ops")]:
    if root.exists():
        for p in root.glob("*/state.json"):
            if any(x in str(p) for x in ["tg","notif","health"]):
                try:out["ops_states"][str(p)]=json.loads(p.read_text())
                except Exception:pass
# Probe sports app DB schemas, not environment/config/keys.
out["opt_dirs"]=[p.name for p in Path("/opt").iterdir() if p.is_dir()]
out["other_app_files"]={}
out["json_notifications"]={}
for root in [Path("/opt/crownsystem-v3"),Path("/opt/crownsystem-v4"),Path("/opt/crown-v3"),Path("/var/lib/footbreak")]:
    if not root.exists():continue
    files=[]
    for base,dirs,names in os.walk(root):
        dirs[:]=[d for d in dirs if d not in {"node_modules",".git","backups","venv",".venv","__pycache__"}]
        files.extend(str(Path(base)/n) for n in names)
    out["other_app_files"][str(root)]=[f for f in files if any(f.endswith(s) for s in [".json",".sqlite",".sqlite3",".db",".py",".js"])]
    for f in files:
        p=Path(f)
        if p.suffix==".json" and any(x in p.name.lower() for x in ["notif","telegram","outbox"]):
            try:
                j=json.loads(p.read_text())
                if isinstance(j,list):j=j[-20:]
                elif isinstance(j,dict):
                    j={k:(v[-20:] if isinstance(v,list) else dict(list(v.items())[-20:]) if isinstance(v,dict) else v)
                        for k,v in j.items() if not any(s in k.lower() for s in ["token","secret","password","api_key"])}
                out["json_notifications"][f]=j
            except Exception:pass
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
