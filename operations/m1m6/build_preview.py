"""Regenerate the existing offline QA preview, never touch production."""
import json
from pathlib import Path
import sys
from install import render_page

root=Path("/home/user/workspace/m1m6-preview")
data=json.loads(Path(sys.argv[1]).read_text())["public_status"]
data["version"]="自動組合策略驗收預覽（非即時）"
panel=Path(__file__).with_name("panel.html").read_text()
panel=panel.replace('<main id="m1m6-root">','<main id="m1m6-root"><p>驗收預覽：凍結資料，並非即時訊號。</p>',1)
panel='<script>window.M1M6_PREVIEW_DATA='+json.dumps(data,ensure_ascii=False).replace("</","<\\/")+";</script>\n"+panel
for path in root.glob("*.html"):
    path.write_text(render_page(path.read_text(),panel))
