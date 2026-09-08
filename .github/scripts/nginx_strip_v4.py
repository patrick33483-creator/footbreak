#!/usr/bin/env python3
"""Strip crownsystem-v4 routes from nginx template + live site file.

Idempotent. Backs up before edit."""
import os, re, shutil, time
FILES = [
    "/opt/footbreak/deploy/nginx-unified-dashboard.conf",  # template (source of truth)
    "/etc/nginx/sites-available/unified-dashboard",         # live copy
]
STAMP = str(int(time.time()))

for f in FILES:
    if not os.path.exists(f):
        print(f"MISSING: {f}")
        continue
    src = open(f).read()
    shutil.copy(f, f + ".bak." + STAMP)
    # Remove any location block referencing crownsystem-v4
    # Strategy: split on 'location', keep block only if it does NOT mention crownsystem-v4
    parts = re.split(r'(?=\n\s*location\s)', src)
    new_parts = []
    for p in parts:
        if 'crownsystem-v4' in p:
            print(f"[{f}] STRIP block: {p.strip()[:120]}")
            continue
        new_parts.append(p)
    out = ''.join(new_parts)
    if out == src:
        print(f"[{f}] no change")
    else:
        open(f, 'w').write(out)
        print(f"[{f}] stripped, backup at .bak.{STAMP}")
