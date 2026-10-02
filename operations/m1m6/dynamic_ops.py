"""Inspection/preflight/deployment for the user's explicit dynamic-cycle scope."""
import json
from pathlib import Path
import subprocess
from ops import run

HERE=Path(__file__).parent
DEST=Path("/opt/crown-m1m6")
BASE=Path("/var/lib/crown-m1m6")
NGINX=Path("/etc/nginx/sites-enabled/unified-dashboard")
FILES=("policy.py","notifier.py","dynamic_rules.py","strategy_runtime.py","research_cycle.py",
       "refresh_api.py","panel.html","test_policy.py","test_dynamic.py")


def inspect():
    nginx=run(["nginx","-T"])
    # nginx configuration has routing/auth file paths, not file contents.
    return {"summary":{"action":"dynamic_inspect"},"nginx":nginx["stdout"],
            "units":{x:run(["systemctl","cat",x])["stdout"] for x in
                     ("crown-m1m6.service","crown-strategy-results.service","crown-strategy-results.timer")},
            "port_in_use":run(["ss","-ltnp","sport = :8786"])["stdout"]}


def preflight():
    from research_cycle import compute
    from time import time
    p=Path("/var/lib/crown-m1m6/registry.json")
    before=json.loads(p.read_text()) if p.exists() else {}
    r=compute(int(time()*1000),before)
    test=run(["python3","-m","unittest","discover","-s",str(Path(__file__).parent),"-p","test_*.py"])
    return {"summary":{"action":"dynamic_preflight","search":r["search"],
                       "active":sum(s["active"] for s in r["strategies"]),
                       "test_returncode":test["returncode"],"tests":test["stderr"]},
            "registry":r}


def deploy():
    import fcntl,shutil,sqlite3,time
    from ops import atomic
    from install import PAGES,render_page
    from research_cycle import compute
    from repair_results import invariant_functions
    if invariant_functions(Path("/opt/crown-strategy-results/sync_results.py").read_text())!=invariant_functions((HERE/"sync_results.py").read_text()):
        raise RuntimeError("Result parser or settlement write changed")
    tests=run(["python3","-m","unittest","discover","-s",str(HERE),"-p","test_*.py"],True)
    now=int(time.time()*1000)
    regpath=BASE/"registry.json"
    prior=json.loads(regpath.read_text()) if regpath.exists() else {}
    initial=compute(now,prior)
    config=Path("/etc/crown-m1m6.json")
    cfg=json.loads(config.read_text())
    ng=NGINX.read_text()
    location="""
    # CROWN_DYNAMIC_REFRESH_AUTHENTICATED
    location = /crownsystem-v3/api/strategy-refresh {
        auth_basic "v3_2026";
        auth_basic_user_file /etc/nginx/.htpasswd-crownsystem-v3;
        proxy_pass http://127.0.0.1:8786/refresh;
        proxy_set_header Host $http_host;
        proxy_set_header X-Authenticated-User $remote_user;
        proxy_connect_timeout 3s;
        proxy_read_timeout 15s;
        add_header Cache-Control "no-store";
    }
"""
    if "CROWN_DYNAMIC_REFRESH_AUTHENTICATED" not in ng:
        anchor="    location = /crownsystem-v3 { return 301 /crownsystem-v3/; }"
        if ng.count(anchor)!=1:
            raise RuntimeError("Unexpected nginx routing anchor")
        ng=ng.replace(anchor,location+"\n"+anchor)
    units={
      "crown-strategy-search.service":"""[Unit]
Description=Crown full-grid discovery and existing-strategy revalidation
After=network-online.target
[Service]
Type=oneshot
ExecStart=/usr/bin/python3 /opt/crown-m1m6/research_cycle.py
TimeoutStartSec=12min
Nice=15
UMask=0022
NoNewPrivileges=true
PrivateTmp=true
ProtectHome=true
ProtectSystem=strict
ReadWritePaths=/var/lib/crown-m1m6 /var/www/crownsystem-v3 /opt/crown-radar-v2/data
""",
      "crown-strategy-search.timer":"""[Unit]
Description=Refresh Crown results and all strategy windows every three hours
[Timer]
OnBootSec=2min
OnUnitActiveSec=3h
AccuracySec=10s
Unit=crown-strategy-search.service
[Install]
WantedBy=timers.target
""",
      "crown-strategy-refresh-api.service":"""[Unit]
Description=Authenticated Crown full-cycle manual refresh bridge
After=network.target
[Service]
Type=simple
ExecStart=/usr/bin/python3 /opt/crown-m1m6/refresh_api.py
Restart=on-failure
RestartSec=3
NoNewPrivileges=true
PrivateTmp=true
ProtectHome=true
ProtectSystem=strict
[Install]
WantedBy=multi-user.target
"""}
    backup=BASE/("dynamic-v4-backup-"+str(now));backup.mkdir()
    paths=[config,NGINX,*PAGES,regpath,Path("/opt/crown-strategy-results/sync_results.py"),
           *[DEST/f for f in FILES],*[Path("/etc/systemd/system")/f for f in units]]
    originals={p:p.read_bytes() if p.exists() else None for p in paths}
    for p,b in originals.items():
        if b is not None:
            (backup/str(p).lstrip("/").replace("/","__")).write_bytes(b)
    run(["systemctl","stop","crown-m1m6.timer"],True)
    try:
        with (BASE/"run.lock").open("a") as lock:
            fcntl.flock(lock,fcntl.LOCK_EX)
            db=sqlite3.connect(BASE/"ledger.sqlite");snap=sqlite3.connect(backup/"ledger.sqlite")
            db.backup(snap);snap.close()
            before=list(db.execute("SELECT * FROM items ORDER BY bet_key"))
            db.close()
            try:
                for f in FILES:
                    shutil.copy2(HERE/f,DEST/f)
                shutil.copy2(HERE/"sync_results.py","/opt/crown-strategy-results/sync_results.py")
                for p in PAGES:
                    p.write_text(render_page(p.read_text(),(HERE/"panel.html").read_text()))
                for name,content in units.items():
                    (Path("/etc/systemd/system")/name).write_text(content)
                NGINX.write_text(ng)
                run(["nginx","-t"],True)
                atomic(regpath,initial)
                archive=BASE/"research_history";archive.mkdir(exist_ok=True)
                atomic(archive/f"{now}.json",initial)
                cfg.update(version="CROWN-DYNAMIC-PER-STRATEGY-v4",mode="dynamic_per_strategy_batch",
                           batch_lock_enabled=True,lock_scope="per_strategy",
                           batch_scope="each merged strategy, same kickoff append",
                           no_result_policy="lock corresponding strategy until all official results",
                           dynamic_activated_at=cfg.get("dynamic_activated_at",now),
                           search_interval_hours=3)
                atomic(config,cfg)
                # Import open pre-upgrade sends into per-strategy locks without
                # modifying the original notification payload/receipt/result.
                import notifier,strategy_runtime as rt
                db=notifier.state_db();rt.schema(db)
                defs={s["id"]:s for s in initial["strategies"]}
                for row in db.execute("SELECT * FROM items WHERE result_json IS NULL AND status IN ('sent','sending','uncertain')").fetchall():
                    h=json.loads(row["payload"])
                    h["rules"]=[rid for rid in h.get("rules",[]) if rid in defs]
                    h["rule_descriptions"]={rid:defs[rid].get("description","") for rid in h["rules"]}
                    if h["rules"]:
                        rt.attach(db,[h],initial["strategies"],now)
                if before!=[tuple(r) for r in db.execute("SELECT * FROM items ORDER BY bet_key")]:
                    raise RuntimeError("Original notification items changed")
                db.close()
            except Exception:
                for p,b in originals.items():
                    if b is not None:
                        p.write_bytes(b)
                    elif p.exists():
                        p.unlink()
                raise
        run(["systemctl","daemon-reload"],True)
        run(["systemctl","reload","nginx"],True)
        run(["systemctl","enable","--now","crown-strategy-refresh-api.service","crown-strategy-search.timer"],True)
        run(["systemctl","start","--no-block","crown-strategy-search.service"],True)
    finally:
        run(["systemctl","start","crown-m1m6.timer"],True)
    return {"summary":{"action":"dynamic_deploy","version":cfg["version"],
                       "tests":tests["stderr"],"initial_search":initial["search"],
                       "historical_items_unchanged":True,"parser_write_semantics_unchanged":True,
                       "backup":str(backup),"full_cycle_started":True,
                       "per_strategy_locks":True,"manual_refresh_route_installed":True}}
