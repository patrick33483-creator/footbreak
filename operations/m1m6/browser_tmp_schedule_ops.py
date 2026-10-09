"""Install only the approved browser-temp maintenance unit, then verify."""
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import time
from browser_tmp_cleanup import health
from disk_readonly import command

DEST = Path("/opt/crown-browser-tmp-cleanup")
UNIT = "crown-browser-tmp-cleanup"
STATE = Path("/var/lib/crown-m1m6/browser_tmp_schedule_status.json")
MARKER = "# Crown approved browser temp maintenance v1\n"
SERVICE = MARKER + """[Unit]
Description=Safely reclaim unused browser temporary data below 32 GiB free

[Service]
Type=oneshot
ExecStart=/usr/bin/python3 -B /opt/crown-browser-tmp-cleanup/browser_tmp_schedule.py
TimeoutStartSec=8min
UMask=0077
Nice=10
IOSchedulingClass=idle
NoNewPrivileges=yes
PrivateTmp=no
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths=/tmp/snap-private-tmp/snap.chromium/tmp /var/lib/crown-m1m6
StandardOutput=journal
StandardError=journal
"""
TIMER = MARKER + """[Unit]
Description=Check approved browser temporary cleanup every two hours

[Timer]
OnCalendar=*-*-* 00/2:00:00 Asia/Hong_Kong
AccuracySec=30s
Persistent=true
Unit=crown-browser-tmp-cleanup.service

[Install]
WantedBy=timers.target
"""
PROTECTED = [Path("/etc/crown-m1m6.json"), Path("/opt/crown-m1m6/policy.py"),
             Path("/var/lib/crown-m1m6/performance_epoch.json")]


def checked(args, timeout=30):
    p = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    if p.returncode:
        raise RuntimeError(f"{args[0]} failed ({p.returncode}): {p.stderr[-1200:]}")
    return p.stdout


def verify():
    timer = checked(["systemctl", "show", UNIT+".timer", "-p", "ActiveState",
                     "-p", "UnitFileState", "-p", "NextElapseUSecRealtime", "-p", "TimersCalendar"])
    return {"summary": {"action": "browser_tmp_schedule_verify", "at_ms": int(time.time()*1000),
                         "timer_enabled": "UnitFileState=enabled" in timer,
                         "timer_active": "ActiveState=active" in timer},
            "timer": timer,
            "unit_files": command(["systemctl", "cat", UNIT+".service", UNIT+".timer"]),
            "service": command(["systemctl", "show", UNIT+".service", "-p", "Result",
                                 "-p", "ExecMainStatus", "-p", "ActiveState",
                                 "-p", "ExecMainExitTimestamp"]),
            "schedule_status": json.loads(STATE.read_text()) if STATE.exists() else None,
            "health": health(),
            "journal": command(["journalctl", "-u", UNIT+".service", "-n", "12",
                                 "--no-pager", "-o", "cat"])}


def install():
    hashes = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in PROTECTED}
    units = Path("/etc/systemd/system")
    for suffix in ("service", "timer"):
        p = units/(UNIT+"."+suffix)
        if p.exists() and not p.read_text().startswith(MARKER):
            raise RuntimeError("Refuse to overwrite an unrecognized existing unit")
    if DEST.exists() and not (DEST/".approved-browser-temp-v1").exists():
        raise RuntimeError("Refuse to overwrite an unrecognized application directory")
    DEST.mkdir(mode=0o755, exist_ok=True)
    (DEST/".approved-browser-temp-v1").touch()
    for name in ("browser_tmp_cleanup.py", "disk_readonly.py", "browser_tmp_schedule.py"):
        shutil.copyfile(Path(__file__).parent/name, DEST/name)
        (DEST/name).chmod(0o644)
    for suffix, text in (("service", SERVICE), ("timer", TIMER)):
        (units/(UNIT+"."+suffix)).write_text(text)
    checked(["systemd-analyze", "verify", str(units/(UNIT+".service")),
             str(units/(UNIT+".timer"))])
    checked(["systemctl", "daemon-reload"])
    # Run the actual installed service once, respecting the same capacity gate.
    checked(["systemctl", "start", UNIT+".service"], timeout=490)
    checked(["systemctl", "enable", "--now", UNIT+".timer"])
    assert hashes == {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in PROTECTED}
    result = verify()
    assert result["summary"]["timer_active"] and result["summary"]["timer_enabled"]
    result["summary"].update(action="browser_tmp_schedule_install",
                              protected_config_unchanged=True,
                              existing_services_restarted=0,
                              platform_automation_created=False)
    return result
