"""Patch nginx-unified-dashboard.conf 加 kwan-v2 location block（喺 stage-v2 block 之後）。"""
import re
from pathlib import Path

p = Path("/opt/footbreak/deploy/nginx-unified-dashboard.conf")
txt = p.read_text()

new_block = """
    location ^~ /kwan-v2/ {
        proxy_pass http://127.0.0.1:8084/;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
"""

m = re.search(r"(location \^~ /stage-v2/ \{[^}]*\})", txt, re.DOTALL)
if not m:
    print("!!! stage-v2 block 揾唔到 — abort")
    raise SystemExit(1)

insert_pos = m.end()
new_txt = txt[:insert_pos] + new_block + txt[insert_pos:]
p.write_text(new_txt)
print(f"patched: added kwan-v2 block after stage-v2 block (insert_pos={insert_pos})")
