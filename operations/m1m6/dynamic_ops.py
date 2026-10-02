"""Inspection/preflight/deployment for the user's explicit dynamic-cycle scope."""
import json
from pathlib import Path
import subprocess
from ops import run


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
