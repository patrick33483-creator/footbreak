"""Read-only post-deployment health check."""
import hashlib
import importlib.util
import json
import subprocess
from pathlib import Path
from datetime import datetime,timezone
out={"utc":datetime.now(timezone.utc).isoformat(),"files":{},"units":{}}
for name in ["crown-goldpool-notify.py","u1-notify.py","ce-notify.py","crown_notification_display.py"]:
    p=Path("/usr/local/bin")/name
    compile(p.read_text(),str(p),"exec")
    out["files"][name]={"sha256":hashlib.sha256(p.read_bytes()).hexdigest(),"syntax":"ok"}
spec=importlib.util.spec_from_file_location("display","/usr/local/bin/crown_notification_display.py")
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
out["league_fallback"]=m.fixture_fields({"sid":"2927357","home":"庫姆拉","away":"利德雪平"})
for unit in ["crown-goldpool-notify","u1-notify","ce-notify"]:
    p=subprocess.run(["systemctl","show",unit+".service","--property=Result,ExecMainStatus,ExecMainStartTimestamp,ExecMainExitTimestamp,ActiveState"],capture_output=True,text=True,check=True)
    out["units"][unit]=dict(line.split("=",1) for line in p.stdout.splitlines() if "=" in line)
    out["units"][unit]["timer"]=subprocess.run(["systemctl","is-active",unit+".timer"],capture_output=True,text=True).stdout.strip()
print(json.dumps(out,ensure_ascii=False,indent=2))
