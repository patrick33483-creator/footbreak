import json, urllib.request, base64
AUTH = "Basic " + base64.b64encode(b"radar:toberich").decode()

def get(path):
    req = urllib.request.Request("http://127.0.0.1" + path, headers={"Authorization": AUTH})
    return json.loads(urllib.request.urlopen(req, timeout=20).read())

print("### matches-crown ###")
d = get("/crown-radar/api/matches-crown")
items = d if isinstance(d, list) else d.get("matches", [])
print("total:", len(items))
if items:
    m = items[0]
    print("keys:", sorted(m.keys()))
    print("--- FULL FIRST MATCH ---")
    print(json.dumps(m, ensure_ascii=False, indent=2)[:6000])
    snap = m.get("snapshots") or {}
    print("--- SNAPSHOT KEYS ---")
    print(sorted(snap.keys()))
    for k in list(snap.keys())[:3]:
        v = snap[k]
        print(f"--- SNAP {k} ---")
        print(json.dumps(v, ensure_ascii=False, indent=2)[:1000])

print("")
print("### history ###")
d = get("/crown-radar/api/history")
items = d if isinstance(d, list) else d.get("matches", [])
print("total:", len(items))
done = [m for m in items if isinstance(m.get("result"), dict) and m["result"].get("status") in ("完","完場")]
print("completed:", len(done))
if done:
    m = done[0]
    print("keys:", sorted(m.keys()))
    print("result:", json.dumps(m.get("result"), ensure_ascii=False))
    print("--- SAMPLE (no snapshots) ---")
    print(json.dumps({k:m[k] for k in sorted(m.keys()) if k != "snapshots"}, ensure_ascii=False, indent=2)[:2500])
