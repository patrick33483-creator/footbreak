#!/usr/bin/env python3
"""Restore /crownsystem-v3/history.html no-auth exception block that got
stripped by nginx_strip_v4.py (over-eager)."""
import os, re, shutil, time

FILES = [
    "/opt/footbreak/deploy/nginx-unified-dashboard.conf",
    "/etc/nginx/sites-available/unified-dashboard",
]

BLOCK = """
    location = /crownsystem-v3/history.html {
        auth_basic off;
        alias /var/www/crownsystem-v3/history.html;
        default_type text/html;
    }
"""

STAMP = str(int(time.time()))
for f in FILES:
    if not os.path.exists(f):
        print(f"MISSING: {f}")
        continue
    src = open(f).read()
    if "location = /crownsystem-v3/history.html" in src:
        print(f"[{f}] already present")
        continue
    # Find any existing /crownsystem-v3/ exception block for matches.json to
    # anchor the insertion; if not found, prepend right after the first
    # `server {` opening brace.
    m = re.search(r'(location = /crownsystem-v3/matches\.json\s*\{[^}]*\})', src)
    shutil.copy(f, f + ".bak2." + STAMP)
    if m:
        idx = m.end()
        new = src[:idx] + "\n" + BLOCK + src[idx:]
    else:
        # fallback: insert before first `location /` at the top
        m2 = re.search(r'(\bserver\s*\{)', src)
        if not m2:
            print(f"[{f}] can't find anchor; skip")
            continue
        idx = m2.end()
        new = src[:idx] + "\n" + BLOCK + src[idx:]
    open(f, 'w').write(new)
    print(f"[{f}] inserted, backup at .bak2.{STAMP}")
