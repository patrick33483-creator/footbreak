#!/opt/titan-sync/venv/bin/python3
"""
Titan Crow (id=3) fixtures sync.
Fetches https://live.titan007.com/index2in1_big.aspx?id=3 with headless Chromium,
extracts sIds via DOM, writes /var/www/stage_engine_v2/titan_today.json.

Runs every hour via titan-crow-sync.timer.
"""
import json
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

OUT = Path("/var/www/stage_engine_v2/titan_today.json")
URL = "https://live.titan007.com/index2in1_big.aspx?id=3"
HKT = timezone(timedelta(hours=8))


def main() -> int:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
        try:
            ctx = browser.new_context(
                user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36",
                viewport={"width": 1400, "height": 900},
                locale="zh-HK",
                timezone_id="Asia/Hong_Kong",
            )
            page = ctx.new_page()
            page.goto(URL, wait_until="networkidle", timeout=45_000)
            # Wait for fixture rows to appear
            try:
                page.wait_for_selector('tr[id^="tr1_"]', timeout=30_000)
            except Exception as e:
                print(f"[titan-sync] warn: fixture rows not visible: {e}", file=sys.stderr)

            fixtures = page.evaluate(
                """
                (() => {
                  const rows = Array.from(document.querySelectorAll('tr[id^="tr1_"]'));
                  return rows.map(r => {
                    const m = r.id.match(/tr1_(\\d+)/);
                    if (!m) return null;
                    const sid = m[1];
                    const cells = Array.from(r.querySelectorAll('td')).map(c => c.textContent.trim());
                    return {
                      sid,
                      league: cells[1] || '',
                      kickoff: cells[2] || '',
                      status: cells[3] || '',
                      home: cells[4] || '',
                      score: cells[5] || '',
                      away: cells[6] || ''
                    };
                  }).filter(Boolean);
                })()
                """
            )
        finally:
            browser.close()

    now = datetime.now(timezone.utc)
    payload = {
        "generated_at_utc": now.isoformat(),
        "generated_at_hkt": now.astimezone(HKT).isoformat(),
        "source_url": URL,
        "count": len(fixtures),
        "sids": [f["sid"] for f in fixtures],
        "fixtures": fixtures,
    }

    OUT.parent.mkdir(parents=True, exist_ok=True)
    tmp = OUT.with_suffix(OUT.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1))
    tmp.replace(OUT)
    OUT.chmod(0o644)
    print(f"[titan-sync] wrote {len(fixtures)} fixtures to {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
