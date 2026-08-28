"""同時加 stage-v2 + kwan-v2 兩個 location block 落 unified-dashboard source of truth。
兩個都 proxy 去自己 port backend，實現：
  /stage-v2/  → 127.0.0.1:8083  (舊 realm 「皇冠系統V2」)
  /kwan-v2/   → 127.0.0.1:8084  (新 realm 「kwan_v2_2026」)

先 idempotent 檢查，已存在就跳過對應 block。
Insertion 位置：喺 fallback `location / {` 之前。
"""
import re
from pathlib import Path

p = Path("/opt/footbreak/deploy/nginx-unified-dashboard.conf")
txt = p.read_text()

stage_v2_block = """    location ^~ /stage-v2/ {
        proxy_pass http://127.0.0.1:8083/;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
"""

kwan_v2_block = """    location ^~ /kwan-v2/ {
        proxy_pass http://127.0.0.1:8084/;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
"""

# 揾 fallback `location / {`（尾）
m = re.search(r"(    location / \{)", txt)
if not m:
    print("!!! fallback 'location /' 揾唔到 — abort")
    raise SystemExit(1)

insert_pos = m.start()

blocks_to_add = ""
if "/stage-v2/" not in txt:
    blocks_to_add += stage_v2_block
    print("→ 加入 stage-v2 block")
else:
    print("→ stage-v2 block 已存在，skip")

if "/kwan-v2/" not in txt:
    blocks_to_add += kwan_v2_block
    print("→ 加入 kwan-v2 block")
else:
    print("→ kwan-v2 block 已存在，skip")

if not blocks_to_add:
    print("(nothing to patch)")
    raise SystemExit(0)

new_txt = txt[:insert_pos] + blocks_to_add + "\n" + txt[insert_pos:]
p.write_text(new_txt)
print(f"patched OK (insert_pos={insert_pos}, added {len(blocks_to_add)} bytes)")
