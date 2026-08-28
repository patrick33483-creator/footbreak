"""Patch nginx-unified-dashboard.conf 加 kwan-v2 location block（喺 stage-v2 block 之後）。
用 brace counting 避免 nested {} 令 regex 提早收尾。
"""
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

# 揾 stage-v2 block 開始位
m = re.search(r"location \^~ /stage-v2/ \{", txt)
if not m:
    # 亦可能唔係 ^~ ，fallback
    m = re.search(r"location\s+[^\n]*/stage-v2/\s*\{", txt)
if not m:
    print("!!! stage-v2 location line 揾唔到 — abort")
    print("--- 頭 3000 char of conf ---")
    print(txt[:3000])
    raise SystemExit(1)

# brace counting：由 block 開始 { 位置開始，數到對應 }
start = m.end() - 1  # 指住 {
depth = 0
i = start
while i < len(txt):
    if txt[i] == "{":
        depth += 1
    elif txt[i] == "}":
        depth -= 1
        if depth == 0:
            block_end = i + 1
            break
    i += 1
else:
    print("!!! stage-v2 block 收尾唔到")
    raise SystemExit(1)

new_txt = txt[:block_end] + new_block + txt[block_end:]
p.write_text(new_txt)
print(f"patched: added kwan-v2 block after stage-v2 block (insert_pos={block_end})")
print(f"stage-v2 block context:")
print(txt[m.start():block_end])
