"""User-approved N2 reactivation only. Optimistic locks, backups, tests, rollback."""
import datetime
import hashlib
import json
import os
import re
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.request

base = Path("/opt/crown-radar-v2")
bundle = Path(sys.argv[1])
payload = json.loads((bundle / "n2_activation_payload.json").read_text())
names = ["rule_matcher.js", "strategy.html", "server.js", "data/rules.json"]
originals = {name: (base / name).read_bytes() for name in names}
for name, expected in payload["expected_sha256"].items():
    assert hashlib.sha256(originals[name]).hexdigest() == expected, f"Concurrent change: {name}"
server = originals["server.js"].decode()
if "--inspect-auth" in sys.argv:
    lines = server.splitlines()
    indexes = set()
    for i, line in enumerate(lines):
        if re.search(r"basic|authoriz|AUTH_USER|AUTH_PASS|BASIC_|401",line,re.I):
            indexes.update(range(max(0,i-3),min(len(lines),i+5)))
    for i in sorted(indexes):
        # Code structure and variable names only; no string literals/credentials.
        print(i+1, re.sub(r"""(["'])(?:(?!\1).)*?\1""", '"[literal]"', lines[i]))
    sys.exit(0)
signal_start = server.index("function buildSignalsPayload(")
signal_end = server.find("\nfunction ", signal_start + 10)
assert "captured_at" in server[signal_start:signal_end if signal_end > 0 else signal_start+15000], "Signal timestamps missing"
for before, after in [
    (payload["history_before"], payload["history_after"]),
    (payload["formatter_before"], payload["formatter_after"]),
    (payload["live_before"], payload["live_after"]),
]:
    assert server.count(before) == 1, "Server patch anchor changed"
    server = server.replace(before, after, 1)
config = json.loads(originals["data/rules.json"])
n2 = next(r for r in config["rules"] if r["id"] == "ch-N2")
assert n2["enabled"] is False, "Already active; refuse to reset activation time"
now_ms = int(time.time() * 1000)
n2.update(payload["rule_patch"])
n2["version_effective_at_ms"] = now_ms
n2["version_effective_at"] = datetime.datetime.fromtimestamp(now_ms/1000,datetime.timezone.utc).isoformat()
for r in config["rules"]:
    if r["id"] != "ch-N2":
        assert r == next(x for x in payload["original_config"]["rules"] if x["id"] == r["id"])
new_files = {
    "rule_matcher.js": payload["matcher"].encode(),
    "strategy.html": payload["html"].encode(),
    "server.js": server.encode(),
    "data/rules.json": (json.dumps(config, ensure_ascii=False, indent=2) + "\n").encode(),
}
def run(args, **kwargs):
    return subprocess.run(args, check=True, capture_output=True, text=True, timeout=120, **kwargs)

def api(path, as_json=True):
    # Reuse existing app credentials only inside its container. Never export/log them.
    code = """
    const fs=require('fs'),vm=require('vm');
    const s=fs.readFileSync('/app/server.js','utf8');
    const read=n=>vm.runInNewContext(s.match(new RegExp('^const '+n+' = .+$','m'))[0]+'\\n'+n,{process});
    const headers={authorization:'Basic '+Buffer.from(read('AUTH_USER')+':'+read('AUTH_PASSWORD')).toString('base64')};
    fetch('http://127.0.0.1:5000'+process.argv[1],{headers,signal:AbortSignal.timeout(25000)})
      .then(async r=>{if(!r.ok)throw new Error('HTTP '+r.status);process.stdout.write(await r.text());})
      .catch(e=>{console.error(e.message);process.exit(1);});
    """
    text = run(["docker","exec","crown-radar-v2","node","-e",code,path]).stdout
    return json.loads(text) if as_json else text

# Validate in production Node runtime BEFORE writes.
run(["docker", "cp", str(bundle), "crown-radar-v2:/tmp/n2-authorized-activation"])
test = run(["docker","exec","crown-radar-v2","node","/tmp/n2-authorized-activation/n2_activation_tests.mjs",
            "/tmp/n2-authorized-activation/n2_activation_payload.json"])
run(["docker","exec","-i","crown-radar-v2","node","--input-type=module","--check"],input=server)
before_api = api("/api/strategy-2plus-overlap")
backup = base / "backups" / ("n2-am8-" + str(now_ms))
backup.mkdir(parents=True, exist_ok=False)
for name, content in originals.items():
    target = backup / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
# Read-only consistent backup, never mutate production SQLite.
src = sqlite3.connect(f"file:{base}/data/crown.db?mode=ro",uri=True)
old_ledger = src.execute("SELECT sid,rule_id,notified_at FROM heavy_notified_rule WHERE rule_id IN ('ch-N2','ch-Alow') ORDER BY sid,rule_id").fetchall()
dst = sqlite3.connect(backup / "crown.db")
src.backup(dst); dst.close(); src.close()
(backup / "pre-history.json").write_text(json.dumps(before_api,ensure_ascii=False))
report = {"activation_ms":now_ms,"activation_utc":n2["version_effective_at"],
          "backup":str(backup),"tests":json.loads(test.stdout),"rule":n2}
try:
    # In-place writes preserve single-file bind-mount inode identity.
    # Config last; all writes are in a no-match HKT afternoon window.
    hkt_hour = (datetime.datetime.now(datetime.timezone.utc).hour + 8) % 24
    assert not 4 <= hkt_hour < 12, "Use a deployment window outside N2 original hours"
    for name, content in new_files.items():
        with (base / name).open("wb") as f:
            f.write(content);f.flush();os.fsync(f.fileno())
    run(["docker","restart","crown-radar-v2"])
    for attempt in range(30):
        try:
            after_api = api("/api/strategy-2plus-overlap")
            live = api("/api/four-channels-live")
            break
        except Exception:
            if attempt == 29: raise
            time.sleep(2)
    assert after_api.get("ok") is not False, after_api
    stats = after_api["summary"]["by_channel"]
    assert stats["新2"]["n"] == 0, "Old samples leaked into reactivated N2"
    for key, val in before_api["summary"]["by_channel"].items():
        if key != "新2":
            assert stats[key] == val, f"Unrelated channel stats changed: {key}"
    assert all(not (x["rule_id"] == "ch-N2") for x in live["rows"]), "Unexpected afternoon N2 live"
    check = run(["docker","exec","crown-radar-v2","node","--input-type=module","-e",
      "import fs from 'fs';import crypto from 'crypto';"
      "console.log(JSON.stringify(Object.fromEntries(['rule_matcher.js','strategy.html','server.js','data/rules.json']"
      ".map(n=>[n,crypto.createHash('sha256').update(fs.readFileSync('/app/'+n)).digest('hex')]))));"])
    actual = json.loads(check.stdout)
    assert actual == {k:hashlib.sha256(v).hexdigest() for k,v in new_files.items()}, "Container stale file"
    assert api("/strategy.html",False) == payload["html"], "Served strategy page differs"
    notice_db = sqlite3.connect(f"file:{base}/data/crown.db?mode=ro",uri=True)
    assert notice_db.execute("SELECT sid,rule_id,notified_at FROM heavy_notified_rule WHERE rule_id IN ('ch-N2','ch-Alow') ORDER BY sid,rule_id").fetchall() == old_ledger, "Legacy notification ledger changed"
    ledger = notice_db.execute("SELECT COUNT(*) FROM heavy_notified_rule WHERE rule_id='ch-N2'").fetchone()[0]
    new_notices = notice_db.execute("SELECT COUNT(*) FROM heavy_notified_rule WHERE rule_id='ch-N2' AND notified_at>=?",(now_ms,)).fetchone()[0]
    notice_db.close()
    assert new_notices == 0, "Unexpected backfill/send"
    notify_enabled = run(["docker","exec","crown-radar-v2","node","-e",
                         "console.log(Boolean(process.env.TELEGRAM_BOT_TOKEN && process.env.TELEGRAM_CHAT_ID))"]).stdout.strip()
    assert notify_enabled == "true", "Telegram configuration missing"
    report.update(status="applied_verified",hashes=actual,new_n2_stats=stats["新2"],
                  old_n2_stats=stats["新2（舊版已停用）"],old_ledger_retained=ledger,
                  new_notifications=new_notices,live_fired=live.get("fired"),
                  notification_configured=True,post_history=after_api,post_live=live)
    (backup / "receipt.json").write_text(json.dumps(report,ensure_ascii=False,indent=2))
    print(json.dumps(report,ensure_ascii=False,indent=2))
except Exception:
    for name, content in originals.items():
        with (base / name).open("wb") as f:
            f.write(content);f.flush();os.fsync(f.fileno())
    run(["docker","restart","crown-radar-v2"])
    print(json.dumps({"status":"rolled_back","backup":str(backup)}))
    raise
