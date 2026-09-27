"""Read-only frozen export for exact production B replay; no sends or DB writes."""
import ast,hashlib,json,sqlite3
from pathlib import Path
from datetime import datetime,timezone
out={"asof_utc":datetime.now(timezone.utc).isoformat(),"pages":{},"code":{},"inventory":{}}
for name in ["results","matches","ogb_fires","u1_fires","strategy_merged","b_raw_notify_receipts"]:
    p=Path("/var/www/crownsystem-v3")/(name+".json")
    raw=p.read_bytes()
    out["pages"][name]={"sha256":hashlib.sha256(raw).hexdigest(),"data":json.loads(raw)}
codepaths=["/usr/local/bin/crown-goldpool-notify.py","/usr/local/bin/ogb_drop_policy.py",
           "/usr/local/bin/u1-notify.py","/usr/local/bin/u1_drop_policy.py",
           "/usr/local/bin/ogb_ahshift_policy.py"]
keep={"ou_side","_is_a_category","b_raw_snapshot","_b_base","is_b_notify_candidate","check_OG_B",
      "parse_ah","_og_base","crown_ou_snap","main","is_notify_candidate",
      "check_u1","snap","is_a_category","check_B_AHSHIFT","crown_ah_raw_snapshot","notify_B_AHSHIFT"}
for name in codepaths:
    p=Path(name);raw=p.read_bytes();text=raw.decode()
    if p.name in ("crown-goldpool-notify.py","u1-notify.py"):
        tree=ast.parse(text)
        funcs={n.name:ast.get_source_segment(text,n) for n in tree.body if isinstance(n,ast.FunctionDef) and n.name in keep}
        constants={}
        for n in tree.body:
            if isinstance(n,ast.Assign) and len(n.targets)==1 and isinstance(n.targets[0],ast.Name) and n.targets[0].id in {"MIN_ODDS_DEC","CUTOFF_MS","B_RAW_READER_VERSION"}:
                constants[n.targets[0].id]=ast.literal_eval(n.value)
        out["code"][name]={"sha256":hashlib.sha256(raw).hexdigest(),"functions":funcs,"constants":constants}
    else:out["code"][name]={"sha256":hashlib.sha256(raw).hexdigest(),"text":text}
p=Path("/var/lib/crown-goldpool-notify/b_drop_policy.json")
out["policy"]=json.loads(p.read_text())
out["notified"]=json.loads(Path("/var/lib/crown-goldpool-notify/state.json").read_text())
out["b_policy_files"]={str(p):json.loads(p.read_text()) for p in Path("/var/lib/crown-goldpool-notify").glob("*policy*.json")}
out["u1_policy"]=json.loads(Path("/var/lib/u1-notify/drop_policy.json").read_text()) if Path("/var/lib/u1-notify/drop_policy.json").exists() else None
out["u1_notified"]=json.loads(Path("/var/lib/u1-notify/state.json").read_text())
out["u1_policy_files"]={str(p):json.loads(p.read_text()) for p in Path("/var/lib/u1-notify").glob("*policy*.json")}
out["checkpoints"]=json.loads(Path("/var/lib/crownsystem-v4/checkpoints.json").read_text())
for root in ["/var/lib/crownsystem-v3","/var/lib/crownsystem-v4","/var/lib/crown-v3","/opt/crownsystem-v4"]:
    p=Path(root)
    if p.exists():
        out["inventory"][root]=[str(f) for f in p.rglob("*") if f.is_file() and f.suffix in {".json",".db",".sqlite"} and "backup" not in str(f).lower()][:250]
db=sqlite3.connect("file:/opt/crown-radar-v2/data/crown.db?mode=ro",uri=True,timeout=15)
db.row_factory=sqlite3.Row;db.execute("PRAGMA query_only=ON");db.execute("BEGIN")
out["tables"]=[dict(r) for r in db.execute("SELECT name,sql FROM sqlite_master WHERE type='table'")]
out["matches"]=[dict(r) for r in db.execute("SELECT * FROM matches ORDER BY kickoff_utc,sid")]
out["finished_matches"]=[dict(r) for r in db.execute("SELECT * FROM finished_matches")]
ids={str(r["sid"]) for name in ["results","matches"] for r in out["pages"][name]["data"].get("matches",[])}
out["snapshots"]=[dict(r) for r in db.execute("SELECT * FROM crown_snapshots WHERE stage IN ('initial','T30','T5')")]
out["model_sids"]=len(ids)
db.close()
out["complete_utc"]=datetime.now(timezone.utc).isoformat()
print(json.dumps(out,ensure_ascii=False,separators=(",",":")))
