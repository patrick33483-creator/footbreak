"""Bounded browser ownership/source inspection; never change live services."""
import collections
import os
from pathlib import Path
import re
import time
from browser_tmp_cleanup import ROOT, NAME, health
from disk_readonly import command


def process(pid):
    p = Path("/proc") / str(pid)
    try:
        args = (p / "cmdline").read_bytes().decode("utf-8", "replace").split("\0")
        status = (p / "status").read_text()
        parent = int(re.search(r"^PPid:\s+(\d+)", status, re.M).group(1))
        cgroup = (p / "cgroup").read_text().strip()
        # Do not export full command arguments, tokens, URLs or environment.
        scripts = [a for a in args if re.fullmatch(r"/(?:opt|usr/local)/[A-Za-z0-9_./-]+\.(?:py|js)", a)]
        return {"pid": int(pid), "ppid": parent, "exe": os.readlink(p / "exe"),
                "scripts": scripts, "cgroup": cgroup,
                "profiles": sorted(set(NAME.findall(" ".join(args)))),
                "browser": any("chromium" in a or "chrome" in a for a in args[:1]),
                "browser_type": next((a for a in args if a.startswith("--type=")), None)}
    except (OSError, AttributeError, ValueError):
        return None


def inspect():
    report = {"summary": {"action": "browser_growth_readonly", "at_ms": int(time.time()*1000),
                          "deletes": 0, "database_writes": 0, "service_changes": 0},
              "health": health()}
    processes = {}
    for p in Path("/proc").iterdir():
        if p.name.isdigit():
            value = process(p.name)
            if value:
                processes[value["pid"]] = value
    selected = {}
    for pid, p in processes.items():
        if not p["browser"]:
            continue
        parent = pid
        for _ in range(6):
            if parent not in processes or parent < 2:
                break
            selected[parent] = processes[parent]
            parent = processes[parent]["ppid"]
    report["processes"] = list(selected.values())
    units = sorted({m for p in selected.values()
                    for m in re.findall(r"[A-Za-z0-9_.@-]+\.service", p["cgroup"])})
    report["units"] = {u: command(["systemctl", "show", u,
                         "-p", "ActiveState", "-p", "Result", "-p", "NRestarts",
                         "-p", "KillMode", "-p", "TimeoutStopUSec", "-p", "MainPID",
                         "-p", "Restart", "-p", "ExecMainStartTimestamp"]) for u in units}
    counts = collections.Counter()
    examples = []
    now = time.time()
    end = time.monotonic()+20
    with os.scandir(ROOT) as entries:
        for e in entries:
            if time.monotonic() > end:
                counts["incomplete"] = 1
                break
            if not NAME.fullmatch(e.name):
                continue
            try:
                s = e.stat(follow_symlinks=False)
            except FileNotFoundError:
                continue
            age = now-s.st_ctime
            counts["total"] += 1
            counts["modified_within_1h"] += age < 3600
            counts["modified_within_6h"] += age < 21600
            counts["modified_within_24h"] += age < 86400
            if age < 3600 and len(examples) < 8:
                examples.append({"name": e.name, "ctime": s.st_ctime})
    report["temp_counts_by_ctime"] = dict(counts)
    report["recent_examples"] = examples
    # Only owned application source, no dependencies, data or environment files.
    roots = [Path("/opt")/n for n in (
        "crown-mobile-fallback", "crown-live-fallback", "odds-mobile-reserve",
        "pinnacle-mobile", "live-titan-collect", "titan-sync", "crown-radar-v2",
        "odds-radar", "racing-radar")]
    paths = {Path(s) for p in selected.values() for s in p["scripts"]}
    for root in roots:
        if root.is_dir():
            for pattern in ("*.py", "*.js", "src/*.py", "src/*.js", "scripts/*.py", "scripts/*.js"):
                paths.update(root.glob(pattern))
    wanted = re.compile(r"chromium|playwright|browser\.close|context\.close|user.data.dir|"
                        r"mkdtemp|rmtree|SIGKILL|kill\(|finally|磁碟|可用：|disk_usage|"
                        r"statvfs|DISK.*(?:WARN|CRIT)|free.*(?:percent|pct)", re.I)
    secret = re.compile(r"token|password|secret|authorization|api.key|cookie|chat.?id|https?://", re.I)
    excerpts = {}
    for p in sorted(paths)[:200]:
        try:
            if p.stat().st_size > 700000:
                continue
            lines = p.read_text(errors="replace").splitlines()
        except OSError:
            continue
        hits = set()
        for i, line in enumerate(lines):
            if wanted.search(line):
                hits.update(range(max(0,i-3),min(len(lines),i+5)))
        if hits:
            excerpts[str(p)] = [
                f"{i+1}: " + ("[sensitive line omitted]" if secret.search(lines[i]) else lines[i][:300])
                for i in sorted(hits)][:150]
    report["source_excerpts"] = excerpts
    report["cleanup_config"] = command(["systemctl", "cat", "crown-tmp-cleanup.service",
                                         "crown-tmp-cleanup.timer"])
    return report
