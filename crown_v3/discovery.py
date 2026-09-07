"""V3 Titan Crown discovery + fetch.
借用 V2 crown/titan.py 內部 helpers, 但唔改 V2 file.
"""
import sys
import re
import json
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, '/opt/footbreak')

import requests

# ===== V2-compatible headers =====
HDRS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Referer": "https://live.titan007.com/",
}

DISCOVERY_URL = "https://live.titan007.com/index2in1.aspx?id=3"
ASIAN_URL = "https://vip.titan007.com/AsianOdds_n.aspx?id={sid}"
OVER_URL = "https://vip.titan007.com/OverDown_n.aspx?id={sid}"


def _get(url: str, timeout: int = 25, encoding: str = "gb18030") -> str:
    r = requests.get(url, headers=HDRS, timeout=timeout)
    r.encoding = encoding
    return r.text


def discover_today() -> list[dict]:
    """從 titan Crown filter 頁抽今日所有場: sid, league, home, away, ko_time_local."""
    html = _get(DISCOVERY_URL)
    if not html or len(html) < 5000:
        return []
    
    # Row pattern: id="chk_<sid>" ... <a id="mt_<sid>">HH:MM</a> ... 
    # <a id="team1_<sid>" ...>HOME</a> ... <a id="team2_<sid>" ...>AWAY</a>
    # League: <a href="...SubLeague.aspx?SclassID=...">LEAGUE</a> before chk_<sid>
    rows = []
    
    # 用 regex 逐 chk_ block 抽
    for m in re.finditer(r'id="chk_(\d{7})"', html):
        sid = m.group(1)
        block_start = max(0, m.start() - 800)
        block_end = min(len(html), m.start() + 3000)
        block = html[block_start:block_end]
        
        # KO time
        ko_m = re.search(rf'id="mt_{sid}">([^<]+)<', block)
        ko = ko_m.group(1).strip() if ko_m else ""
        
        # Teams
        h_m = re.search(rf'id="team1_{sid}"[^>]*>([^<]+)<', block)
        a_m = re.search(rf'id="team2_{sid}"[^>]*>([^<]+)<', block)
        home = h_m.group(1).strip() if h_m else ""
        away = a_m.group(1).strip() if a_m else ""
        
        # League: 揾 chk_<sid> 之前最近一個 SubLeague/League link
        pre = html[max(0, m.start()-1500):m.start()]
        lg_m = re.search(r'>([^<]+)</font></a></span></td>\s*<td[^>]*id="mt_' + sid, html[max(0,m.start()-2000):m.start()+200])
        if not lg_m:
            # fallback: 揾最近 League.aspx / SubLeague.aspx text
            lg_matches = re.findall(r'(?:SubLeague|League)\.aspx\?SclassID=\d+[^>]*>[^<]*<font[^>]*>([^<]+)</font>', pre)
            league = lg_matches[-1] if lg_matches else ""
        else:
            league = lg_m.group(1).strip()
        
        rows.append({
            "sid": sid,
            "league": league,
            "home": home,
            "away": away,
            "ko_local": ko,
        })
    
    return rows


def fetch_crown_row(sid: str) -> dict:
    """拉一場 sid 嘅 Crow row 賠率 (亞讓 + 大小 初+即時)."""
    asian_html = _get(ASIAN_URL.format(sid=sid))
    over_html = _get(OVER_URL.format(sid=sid))
    
    def extract_crow(html: str) -> dict | None:
        """揾第一個 tr 有 'Crow*' text, 抽初主/初盤/初客/即主/即盤/即客."""
        # Table row 內 Crow* 之後有 6+ 個 td, 前 6 個係我地要嘅
        # HTML 內: <tr...><td>chk</td><td>Crow*</td><td>+</td><td>0.88</td><td>平手/半球</td><td>1.00</td><td>0.97</td><td>平手/半球</td><td>0.92</td>...
        # 揾 Crow* 位置, 由該 tr 開始
        # Simplify: 揾 <tr> ... Crow* ... </tr>
        for tr_m in re.finditer(r'<tr[^>]*>(.*?)</tr>', html, flags=re.DOTALL):
            content = tr_m.group(1)
            if 'Crow' not in content:
                continue
            # 抽所有 td text
            tds = re.findall(r'<td[^>]*>(.*?)</td>', content, flags=re.DOTALL)
            # 清 HTML tags
            clean = []
            for td in tds:
                t = re.sub(r'<[^>]+>', '', td).strip()
                t = re.sub(r'\s+', ' ', t)
                clean.append(t)
            # 揾 Crow* index
            crow_i = None
            for i, t in enumerate(clean):
                if 'Crow' in t:
                    crow_i = i
                    break
            if crow_i is None:
                continue
            # 跳過 Crow* 後 1 個 (多盤 col) 再讀 6 個
            start = crow_i + 2
            vals = clean[start:start+6]
            if len(vals) < 6:
                return None
            return {
                "open_home": vals[0],
                "open_line": vals[1],
                "open_away": vals[2],
                "live_home": vals[3],
                "live_line": vals[4],
                "live_away": vals[5],
            }
        return None
    
    return {
        "sid": sid,
        "asian": extract_crow(asian_html),
        "over": extract_crow(over_html),
        "asian_html_len": len(asian_html),
        "over_html_len": len(over_html),
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--mode", choices=["discover", "fetch", "test"], default="test")
    p.add_argument("--sid", help="sid for fetch mode")
    p.add_argument("--limit", type=int, default=5)
    args = p.parse_args()
    
    if args.mode == "discover":
        rows = discover_today()
        print(json.dumps({"count": len(rows), "sample": rows[:args.limit]}, ensure_ascii=False, indent=2))
    elif args.mode == "fetch":
        if not args.sid:
            print("need --sid")
            sys.exit(1)
        out = fetch_crown_row(args.sid)
        print(json.dumps(out, ensure_ascii=False, indent=2))
    elif args.mode == "test":
        # discover + fetch first N
        print("=== Discovery ===")
        rows = discover_today()
        print(f"total: {len(rows)}")
        for r in rows[:args.limit]:
            print(f"  {r['sid']} {r['ko_local']} {r['league']} {r['home']} vs {r['away']}")
        print("\n=== Fetch first 3 ===")
        for r in rows[:3]:
            fetched = fetch_crown_row(r['sid'])
            print(f"\n{r['sid']} {r['home']} vs {r['away']}:")
            print(f"  asian: {fetched['asian']}")
            print(f"  over:  {fetched['over']}")
            time.sleep(1)
