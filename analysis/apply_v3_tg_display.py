"""Audited formatter-only deploy. Never calls notification main or send."""
import ast
import hashlib
import importlib.util
import json
import shutil
import subprocess
import sys
import types
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path("/usr/local/bin")
EXPECTED = {
    "crown-goldpool-notify.py": "079d9db5d31cfedd7bde6307ba1b393438597f58d50026481d88e18bbfe793ca",
    "u1-notify.py": "eee03bff0685535024daa7a412984ea1b33ade077951a84dd529b6494e105324",
    "ce-notify.py": "ae2c38b5f0a5de7044be98aa0322e5692f5a093e0d3a86a25985bf83f4a93565",
}
REPLACEMENTS = {
    "crown-goldpool-notify.py": {
        "format_B_AHSHIFT": '''def format_B_AHSHIFT(m, hit):
    from crown_notification_display import over_message
    return over_message(m, hit["ou_line"], hit["over_hk"],
        "B＋讓球差至少0.25獨立分支",
        ["條件備註：讓球差只作篩選，實際買大球，唔係買讓球。",
         "如同場另有B通知，屬兩個分支；原設定各計一注。"])
''',
    },
    "u1-notify.py": {
        "format_msg": '''def format_msg(m, trig):
    from crown_notification_display import over_message
    r = u1_research()
    return over_message(m, trig["line"], trig["over_odds"], "U1反向買大＋排除降水",
        ["條件備註：模型揀細，但U1實際反向買大，唔係買細。",
         f"固定歷史回測：{r['W']}全贏／{r['HW']}半贏／{r['L']}輸，命中率{r['hit_rate']:.2f}%（非新版正式實績）"])
''',
    },
    "ce-notify.py": {
        "format_msg": '''def format_msg(m, trig):
    from crown_notification_display import home_message
    return home_message(m, trig["line"], trig["home_odds"], "CE v3平手買主",
        [f"命中組合：{trig['ce_combo_label']}",
         f"原策略注碼：HK${STAKE:,}，每場CE一注。"]) + format_hkjc_footer(m)
''',
    },
}
B_OLD = '''        from crown_hit_metrics import research_stats
        r = research_stats()["OG-B"]
        return "\\n".join([
            "B＋排除降水",
            "",
            f"{m.get('home','')} vs {m.get('away','')}",
            f"開賽：{ko} HKT",
            "",
            f"固定歷史回測：{r['W']}全贏／{r['HW']}半贏／{r['L']}輸，命中率{r['hit_rate']:.2f}%",
        ])'''
B_NEW = '''        from crown_hit_metrics import research_stats
        from crown_notification_display import over_message
        r = research_stats()["OG-B"]
        hit = rule_hits[0][1]
        return over_message(m, hit["ou_line"], hit["t5_odds"], "B＋排除同盤降水",
            [f"固定歷史回測：{r['W']}全贏／{r['HW']}半贏／{r['L']}輸，命中率{r['hit_rate']:.2f}%（非新版正式實績）"])'''


def sha(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()


def function_replace(text, name, new):
    node = next(n for n in ast.parse(text).body if isinstance(n, ast.FunctionDef) and n.name == name)
    lines = text.splitlines(keepends=True)
    return "".join(lines[:node.lineno-1]) + new + "".join(lines[node.end_lineno:])


def normalized(text, excluded):
    tree = ast.parse(text)
    tree.body = [n for n in tree.body if not isinstance(n, ast.FunctionDef) or n.name not in excluded]
    return ast.dump(tree, include_attributes=False)


original, changed = {}, {}
for name, expected in EXPECTED.items():
    assert sha(ROOT/name) == expected, f"Concurrent change detected: {name}"
    old = (ROOT/name).read_text()
    new = old
    for func, replacement in REPLACEMENTS[name].items():
        new = function_replace(new, func, replacement)
    excluded = set(REPLACEMENTS[name])
    if name.startswith("crown-goldpool"):
        assert new.count(B_OLD) == 1
        new = new.replace(B_OLD, B_NEW)
        excluded.add("format_msg_multi")
    assert normalized(old, excluded) == normalized(new, excluded), name
    compile(new, name, "exec")
    original[name], changed[name] = old, new

# Test only formatter AST nodes; no module top-level env reads or send calls.
helper_path = Path(__file__).with_name("crown_notification_display.py")
spec = importlib.util.spec_from_file_location("crown_notification_display", helper_path)
display = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = display
spec.loader.exec_module(display)
metrics = types.ModuleType("crown_hit_metrics")
metrics.research_stats = lambda: {"OG-B": {"W": 40, "HW": 14, "L": 24, "hit_rate": 60.3}}
sys.modules["crown_hit_metrics"] = metrics
match = {"sid":"3093308","league":"波蘭丙","home":"韋爾科波爾斯基","away":"弗羅茨瓦夫","kickoff_utc_ms":1790424000000}
samples = {}
tests = 0
for name, text in changed.items():
    ns = {"u1_research":lambda:{"W":1,"HW":1,"L":1,"hit_rate":50},
          "STAKE":1000,"format_hkjc_footer":lambda m:""}
    funcs = [n for n in ast.parse(text).body if isinstance(n, ast.FunctionDef)
             and n.name in {"format_B_AHSHIFT","format_msg_multi","format_msg"}]
    exec(compile(ast.Module(body=funcs, type_ignores=[]), name, "exec"), ns)
    if name.startswith("crown-goldpool"):
        samples["B"] = ns["format_msg_multi"](match,[({"id":"ad-OG-B"},{"ou_line":2.75,"t5_odds":0.86})])
        samples["B-single"] = ns["format_msg"](match,{"id":"ad-OG-B"},{"ou_line":2.75,"t5_odds":0.86})
        samples["B-branch"] = ns["format_B_AHSHIFT"](match,{"ou_line":2.75,"over_hk":0.86})
    elif name.startswith("u1"):
        for hk in [0.60,0.72,0.84,1.14]:
            samples[f"U1-{hk}"] = ns["format_msg"](match,{"line":2.75,"over_odds":hk})
            assert f"{1+hk:.2f}" in samples[f"U1-{hk}"]
            tests += 1
    else:
        samples["CE"] = ns["format_msg"](match,{"line":0,"home_odds":0.75,"ce_combo_label":"原組合"})
for title, text in samples.items():
    for field in ["聯賽：波蘭丙","主隊：","客隊：","開賽：2026-09-26 20:00（香港）","買乜：","盤口：","皇冠賠率：","策略："]:
        assert field in text, (title, field)
        tests += 1
    assert "測試重發" not in text and "已開賽" not in text
    assert ("買主隊" in text) if title == "CE" else ("買乜：全場入球大 2.75" in text)
    tests += 2

stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
backup = Path("/var/backups")/f"crown-tg-display-{stamp}"
backup.mkdir(mode=0o700)
states = [Path(p) for p in ["/var/lib/crown-goldpool-notify/state.json","/var/lib/u1-notify/state.json","/var/lib/ce-notify/state.json"]]
before_states = {str(p):json.loads(p.read_text()).get("sent",{}) for p in states}
for name in changed:
    shutil.copy2(ROOT/name, backup/name)
if (ROOT/helper_path.name).exists():
    shutil.copy2(ROOT/helper_path.name, backup/helper_path.name)
try:
    # Atomic replacements; existing invocations complete, next timer invocation loads new code.
    for dest, content in [(ROOT/helper_path.name,helper_path.read_text()), *[(ROOT/n,s) for n,s in changed.items()]]:
        tmp = dest.with_name(dest.name+".display-tmp")
        tmp.write_text(content)
        tmp.chmod(dest.stat().st_mode & 0o777 if dest.exists() else 0o644)
        tmp.replace(dest)
    for name in changed:
        assert (ROOT/name).read_text() == changed[name]
    for p in states:
        after = json.loads(p.read_text()).get("sent",{})
        assert all(after.get(k) == v for k,v in before_states[str(p)].items()), f"Historical sent changed: {p}"
except Exception:
    for name in changed: shutil.copy2(backup/name, ROOT/name)
    raise
units = {}
for unit in ["crown-goldpool-notify.timer","u1-notify.timer","ce-notify.timer"]:
    units[unit] = subprocess.run(["systemctl","is-active",unit],capture_output=True,text=True).stdout.strip()
print(json.dumps({"status":"applied_verified","utc":datetime.now(timezone.utc).isoformat(),
    "backup":str(backup),"formatter_assertions":tests,"all_nonformatter_ast_unchanged":True,
    "historical_notification_entries_unchanged":True,"sent_by_deployer":0,
    "timers":units,"sha256":{n:sha(ROOT/n) for n in changed},"samples":samples},ensure_ascii=False,indent=2))
