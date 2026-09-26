"""Read-only exact recent V3 notification evidence. No network sends."""
import json,re,hashlib,subprocess
from pathlib import Path
out={"code":{},"json":{}}
names=["/usr/local/bin/crown-goldpool-notify.py","/usr/local/bin/crown-u1-notify.py","/opt/crownsystem-v3/heavy_watch.py"]
names += [str(p) for p in Path("/usr/local/bin").glob("*") if p.suffix==".py" and ("u1" in p.name.lower() or "ce-notif" in p.name.lower())]
out["units"]=subprocess.run(["systemctl","list-timers","--all","--no-pager"],capture_output=True,text=True).stdout
for name in sorted(set(names)):
    p=Path(name)
    if p.exists():
        s=p.read_text()
        s=re.sub(r"\b\d{8,}:[A-Za-z0-9_-]{25,}","[REDACTED]",s)
        out["code"][name]={"sha256":hashlib.sha256(p.read_bytes()).hexdigest(),"text":s}
files=[]
for root in ["/var/lib/crown-goldpool-notify","/var/lib/crown-u1-notify","/var/lib/crownsystem-v3","/var/lib/crown-ad-flat-ce-notify","/var/www/crownsystem-v3"]:
    p=Path(root)
    if p.exists(): files.extend(p.glob("*.json"))
for p in files:
    if any(x in p.name.lower() for x in ["secret","config","policy","fallback","backtest","audit"]):continue
    try:
        j=json.loads(p.read_text())
        def recent(v):
            s=json.dumps(v)
            return any(x in s for x in ["1790424","1790423","2026-09-26","09-26 20:00"])
        if isinstance(j,list): j=[v for v in j if recent(v)]
        elif isinstance(j,dict):
            j={k:([v for v in v if recent(v)] if isinstance(v,list) else {a:b for a,b in v.items() if recent(b)} if isinstance(v,dict) else v) for k,v in j.items()}
        out["json"][str(p)]=j
    except Exception: pass
print(json.dumps(out,ensure_ascii=False))
