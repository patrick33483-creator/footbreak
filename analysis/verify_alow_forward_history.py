"""Cross-check frozen direct rules against existing Node matcher and historical settlement."""
import importlib.util
import json
import subprocess
import sys
from collections import Counter
from pathlib import Path
from alow_forward import original_match, classify

replay=Path(sys.argv[1])
evidence=json.loads((replay/"alow-n2-evidence.json").read_text())
db=evidence["databases"]["/opt/crown-radar-v2/data/crown.db"]["rows"]
conf=json.loads(evidence["files"]["data/rules.json"]["content"])
rule={**next(r for r in conf["rules"] if r["id"]=="ch-Alow"),"enabled":True}
by={}
for s in db["crown_snapshots"]:by.setdefault(s["sid"],{})[f"{s['stage']}_{s['market']}"]=s
matches=[{**m,"snapshots":by.get(m["sid"],{})} for m in db["matches"]]
node="""import fs from 'node:fs';
const p=JSON.parse(fs.readFileSync(0,'utf8'));
const {matchAll}=await import('data:text/javascript;base64,'+Buffer.from(p.code).toString('base64'));
console.log(JSON.stringify(p.matches.map(m=>{
const snapshots=Object.fromEntries(Object.entries(m.snapshots).map(([k,s])=>[k,{...s,h:s.handicap,home:s.home_odds,away:s.away_odds}]));
const h=matchAll({...m,snapshots},[p.rule]);return h.length?Math.round(h[0].match.t5Dec*1000):null;})));"""
baseline=json.loads(subprocess.run(["node","--input-type=module","-e",node],input=json.dumps({
    "code":evidence["files"]["rule_matcher.js"]["content"],"matches":matches,"rule":rule}),
    check=True,capture_output=True,text=True).stdout)
ours=[original_match(m) for m in matches]
bad=[m["sid"] for m,a,b in zip(matches,ours,baseline) if a!=b]
assert not bad,bad[:20]
old=[r for r in json.loads((replay/"replay_rows.json").read_text()) if r["rule_id"]=="ch-Alow" and r["settled"]]
counts=Counter(classify(original_match({"snapshots":r["snapshots"]})) for r in old)
assert counts["ALOW-P170-v1"]==44 and counts["ALOW-P200-v1"]==30
print(json.dumps({"matcher_comparisons":len(matches),"mismatches":len(bad),
    "historical_main_counts":{"ALOW-P170-v1":44,"ALOW-P200-v1":30},
    "history_used_for_verification_only":True},indent=2))
