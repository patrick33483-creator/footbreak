"""計出下一次真正需要醒嚟嘅時刻。

每場波一世只做三次預測:首預(每晚 23:59 掃全板)、T-30、T-5。
所以唔需要固定每 X 分鐘輪詢,只需要喺每場嘅 T-30 / T-5 之前醒一次。
好多場開賽時間一樣(例如同一時間 20 場),一次醒可以一次過搞掂,
所以每日真正需要醒嘅次數遠少過場數。

用法:
  python3 next_due.py            # 下一個醒點
  python3 next_due.py --queue N  # 未來 N 個醒點
  python3 next_due.py --due      # 排程觸發檢查(exit 0 = 有嘢做)

輸出 JSON:
  {"next": "2026-08-07T20:22:00",   # HKT,冇時區後綴,直接餵俾排程器
   "next_in_min": 61.3,
   "covers": [{"stage": "T-30", "home": ..., "away": ..., "ko": ...}, ...],
   "upcoming": [...]}              # 之後幾個醒點,俾人肉檢查
"""
from __future__ import annotations
import json
import os
import sys
import datetime as dt

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import hkjc_feed as H          # noqa: E402
import run_predict as RP       # noqa: E402

HKT = dt.timezone(dt.timedelta(hours=8))

# 排程器由觸發到真正跑起,通常有幾分鐘延遲,所以要提早醒。
LEAD_T30 = 32.0    # 喺開賽前 32 分鐘醒 → 落喺 T-30 窗口 (20–40)
LEAD_T5 = 10.0     # 一入 T-5 窗口即醒，預留約 9–10 分鐘俾人手決定
GROUP_MIN = 3.0    # 相隔 3 分鐘以內嘅醒點合併做一次
MIN_LEAD = 1.5     # 太近(少過 1.5 分鐘)就唔排,直接跳去下一個


def wake_points(horizon_h: int = 30) -> list[dict]:
    done = RP.done_stages()
    now = dt.datetime.now(dt.timezone.utc)
    pts: list[dict] = []
    for m in H.fetch_matches():
        if m.get("status") != "PREEVENT":
            continue
        ko = H.parse_kickoff(m)
        if not ko:
            continue
        mins = (ko - now).total_seconds() / 60
        if mins <= 0 or mins > horizon_h * 60:
            continue
        seen = done.get(str(m.get("id")), set())
        for stage, lead in (("T-30", LEAD_T30), ("T-5", LEAD_T5)):
            if stage in seen:
                continue
            at = ko - dt.timedelta(minutes=lead)
            if (at - now).total_seconds() / 60 < MIN_LEAD:
                continue
            pts.append({
                "at": at,
                "stage": stage,
                "home": m["homeTeam"]["name_ch"],
                "away": m["awayTeam"]["name_ch"],
                "ko": ko.astimezone(HKT).strftime("%Y-%m-%d %H:%M"),
            })
    pts.sort(key=lambda p: p["at"])
    return pts


def group(pts: list[dict]) -> list[dict]:
    """把相近嘅醒點合併。"""
    out: list[dict] = []
    for p in pts:
        if out and (p["at"] - out[-1]["at"]).total_seconds() / 60 <= GROUP_MIN:
            out[-1]["covers"].append(p)
        else:
            out.append({"at": p["at"], "covers": [p]})
    return out


def queue(n: int = 12):
    """輸出未來 n 個醒點,俾排程器一次過全部排定。"""
    g = group(wake_points())
    now = dt.datetime.now(dt.timezone.utc)
    out = []
    for x in g[:n]:
        out.append({
            "run_at": x["at"].astimezone(HKT).strftime("%Y-%m-%dT%H:%M:%S"),
            "in_min": round((x["at"] - now).total_seconds() / 60, 1),
            "n_matches": len(x["covers"]),
            "stages": sorted({c["stage"] for c in x["covers"]}),
            "label": " / ".join(
                f"{c['stage']} {c['home']}v{c['away']}" for c in x["covers"][:4]
            ) + (f" 等 {len(x['covers'])} 場" if len(x["covers"]) > 4 else ""),
        })
    print(json.dumps({"total_wakes": len(g), "queue": out},
                     ensure_ascii=False, indent=1))


def due() -> int:
    """排程觸發檢查:而家有冇場踏入 T-30 / T-5 窗口而又未做過?

    有 → 印出場次並 exit 0(排程會叫醒 agent 跑 run_all.sh tick)
    冇 → 印一行原因並 exit 1(排程直接跳過,唔會叫大模型,近乎零成本)

    絕對唔可以拋 exception —— 任何錯誤都當「有嘢做」處理(exit 0),
    寧願白行一次,都好過靜靜地漏咗一場落注。
    """
    try:
        done = RP.done_stages()
        now = dt.datetime.now(dt.timezone.utc)
        hits = []
        for m in H.fetch_matches():
            if m.get("status") != "PREEVENT":
                continue
            ko = H.parse_kickoff(m)
            if not ko:
                continue
            mins = (ko - now).total_seconds() / 60
            if mins <= 0 or mins > 60:
                continue
            seen = done.get(str(m.get("id")), set())
            st = RP.due_now(mins, seen)
            if st:
                hits.append("%s %s v %s (%.0f 分鐘後開賽)"
                            % (st, m["homeTeam"]["name_ch"],
                               m["awayTeam"]["name_ch"], mins))
    except Exception as e:                       # noqa: BLE001
        print("DUE 檢查出錯,保守當作有嘢做:%s" % e)
        return 0

    if hits:
        print("到期 %d 場 —— %s" % (len(hits), " / ".join(hits[:6])))
        return 0
    print("冇場踏入 T-30 / T-5 窗口 —— 跳過")
    return 1



def main():
    if "--due" in sys.argv:
        sys.exit(due())
    if "--queue" in sys.argv:
        i = sys.argv.index("--queue")
        n = int(sys.argv[i + 1]) if len(sys.argv) > i + 1 else 12
        return queue(n)
    pts = wake_points()
    if not pts:
        print(json.dumps({"next": None, "reason": "冇任何待做嘅 T-30 / T-5"},
                         ensure_ascii=False))
        return
    g = group(pts)
    now = dt.datetime.now(dt.timezone.utc)
    first = g[0]
    out = {
        "next": first["at"].astimezone(HKT).strftime("%Y-%m-%dT%H:%M:%S"),
        "next_in_min": round((first["at"] - now).total_seconds() / 60, 1),
        "n_wakes_ahead": len(g),
        "covers": [{"stage": c["stage"], "home": c["home"],
                    "away": c["away"], "ko": c["ko"]} for c in first["covers"]],
        "upcoming": [{
            "at": x["at"].astimezone(HKT).strftime("%m-%d %H:%M"),
            "n": len(x["covers"]),
            "stages": sorted({c["stage"] for c in x["covers"]}),
        } for x in g[1:9]],
    }
    print(json.dumps(out, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
