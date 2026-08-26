"""三階段球賽預測執行器。

流程:HKJC 賽事 → 配對 Pinnacle → 擬合初盤/現價 → 加天氣、疲勞、場地、
陣容調整 → 重算比分矩陣 → 對比馬會盤 → 每場出一個結論。
"""
from __future__ import annotations
import json
import math
import os
import sys
import tempfile
import time
import multiprocessing
from multiprocessing.connection import wait as wait_for_connections
import datetime as dt
from dataclasses import asdict

import hkjc_feed as H
import model as M
import sharp as S
import context as C
import predict as P
import staking as K
try:
    import native_stage_state as stage_state
except ImportError:  # package import used by offline tests
    from system import native_stage_state as stage_state

HERE = os.path.dirname(os.path.abspath(__file__))
HKT = dt.timezone(dt.timedelta(hours=8))

# 由人手研究(網上新聞)填入嘅陣容/傷患調整,key = HKJC matchId
NEWS_FILE = os.path.join(HERE, "news_adj.json")


def write_json_atomic(path: str, payload: object) -> None:
    """Durably replace JSON so a killed tick cannot leave a partial file."""
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".tmp-", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=1)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def load_news_adj() -> dict:
    if os.path.exists(NEWS_FILE):
        return json.load(open(NEWS_FILE, encoding="utf-8"))
    return {}


# 三個預測時點:
#   首預  每15分鐘掃全板；只為新發現賽事做一次
#   T-30  開賽前 30 分鐘(陣容/傷患已出,賠率漸定)
#   T-5   開賽前 5 分鐘 —— 唯一落注時點
SWEEP = "首預"
WIN_T30 = (20.0, 40.0)     # T-30 觸發窗(留返排程延遲餘裕)
WIN_T5 = (0.0, 10.0)       # T-5 觸發窗；開賽前最後一刻仍可跑


def stage_of(mins: float, sweep: bool = False) -> str:
    if sweep:
        return SWEEP
    if WIN_T5[0] <= mins <= WIN_T5[1]:
        return "T-5"
    if WIN_T30[0] <= mins <= WIN_T30[1]:
        return "T-30"
    if mins <= WIN_T5[1]:
        return "T-5"
    if mins <= WIN_T30[1]:
        return "T-30"
    return "待入窗"


def due_now(mins: float, done: set) -> str | None:
    """Return the missing native timed stage only while safely pre-kickoff."""
    if WIN_T5[0] < mins <= WIN_T5[1] and "T-5" not in done:
        return "T-5"
    if WIN_T30[0] <= mins <= WIN_T30[1] and "T-30" not in done:
        return "T-30"
    return None


def _ledger_watch(*, strict: bool = False) -> dict:
    """Read the authoritative persisted schedule without silently masking corruption."""
    payload = _ledger_payload(strict=strict)
    watch = payload.get("watch") if isinstance(payload, dict) else None
    if isinstance(watch, dict):
        return watch
    if strict:
        raise ValueError("watch is not a mapping")
    return {}


def _ledger_payload(*, strict: bool = False) -> dict:
    """Read the whole native ledger for manifest/attempt transitions."""
    fp = os.path.join(HERE, "sim_ledger.json")
    if not os.path.exists(fp):
        return {"watch": {}}
    try:
        with open(fp, encoding="utf-8") as handle:
            payload = json.load(handle)
        watch = payload.get("watch")
        if not isinstance(watch, dict):
            raise ValueError("watch is not a mapping")
        return payload
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        if strict:
            raise
        return {}


def done_stages() -> dict:
    """Return only valid persisted stage names; legacy/malformed rows are inert."""
    return {
        str(mid): {
            str(stage.get("stage")) for stage in (row.get("stages") or [])
            if isinstance(stage, dict) and stage.get("stage")
        }
        for mid, row in _ledger_watch().items() if isinstance(row, dict)
    }


def known_fixture_ids() -> dict:
    """Reuse a persisted PinnAPI identity; due ticks never need fixture discovery."""
    return {
        str(mid): str(row["fixture_id"])
        for mid, row in _ledger_watch().items()
        if isinstance(row, dict) and row.get("fixture_id") is not None
    }


def _parse_persisted_kickoff(value):
    try:
        kickoff = dt.datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return kickoff.replace(tzinfo=HKT) if kickoff.tzinfo is None else kickoff.astimezone(HKT)


def persisted_due_stages(horizon_min: float = 90.0, *, strict: bool = False):
    """Return persisted `due_at <= now < kickoff` native work before providers."""
    payload = _ledger_payload(strict=strict)
    watch = payload.get("watch") if isinstance(payload, dict) else {}
    now = dt.datetime.now(HKT)
    due = stage_state.due_stage_work(payload, now=now, horizon_minutes=horizon_min)
    # Compatibility for pre-deploy/read-only callers.  The real tick first
    # persists manifests; this fallback never writes or fabricates a stage.
    if not due:
        for mid, row in (watch.items() if isinstance(watch, dict) else ()):
            if not isinstance(row, dict) or row.get("native_stage_manifest"):
                continue
            kickoff = _parse_persisted_kickoff(row.get("kickoff"))
            if kickoff is None:
                continue
            stage = due_now((kickoff - now).total_seconds() / 60.0, {
                str(item.get("stage")) for item in (row.get("stages") or [])
                if isinstance(item, dict) and item.get("stage")
            })
            if stage:
                due.append({
                    "hkjc_match_id": str(row.get("match_id") or mid),
                    "watch_key": str(mid), "watch": row, "stage": stage,
                    "kickoff_at_hkt": kickoff.isoformat(),
                })
    output = []
    for item in due:
        kickoff = _parse_persisted_kickoff(item.get("kickoff_at_hkt"))
        if kickoff is not None:
            output.append((kickoff, str(item["hkjc_match_id"]), item["watch"], item["stage"]))
    output.sort(key=lambda item: (item[3] != "T-5", item[0], item[1]))
    return output


def pending_watch_match_ids(horizon_min: float = 90.0) -> list[str]:
    return [mid for _kickoff, mid, _row, _stage in persisted_due_stages(horizon_min)]


def fetch_matches_with_due_recovery(mode: str, horizon_min: float) -> list[dict]:
    """Keep discovery out of a due tick; recover only locally scheduled IDs."""
    if mode == "due":
        # Keep the public helper compatible for diagnostic callers while the
        # normal path is still derived from the same authoritative ledger scan.
        ids = pending_watch_match_ids(horizon_min)
        if not ids:
            return []
        rows = []
        for start in range(0, len(ids), 20):
            # hkjc_feed's request timeout is bounded by FOOTBREAK_REMOTE_TIMEOUT_SECONDS.
            rows.extend(H.fetch_matches(match_ids=ids[start:start + 20]))
        return rows
    return [row for row in H.fetch_matches() if row.get("id") is not None]


def urgent_stage_required() -> bool:
    """Used by preemption.  Never claim all-clear when the ledger is unreadable."""
    return bool(persisted_due_stages(90.0, strict=True))


def _fixture_from_watch(row: dict, fixture_id: str) -> dict:
    return {
        "id": str(fixture_id),
        "start_date": str(row.get("kickoff") or ""),
        "home_team_display": row.get("home_en") or row.get("home") or "",
        "away_team_display": row.get("away_en") or row.get("away") or "",
        "league": {"id": row.get("league_id"), "name": row.get("league") or ""},
        "venue_name": row.get("venue"), "venue_location": row.get("venue_city"),
        "venue_neutral": bool(row.get("neutral")), "home_competitors": [], "away_competitors": [],
        "_provider": "pinnapi",
    }

def conviction(base_fit, adj_conf: float, mins: float,
               n_hk_lines: int, has_wx: bool, has_news: bool,
               hk_moved: bool | None) -> float:
    """信念強度(0-100)。以預判質素為主,唔係以 edge 為主。

    校準:一場資料齊全、擬合靚、有陣容消息、已入分析窗嘅賽事約 68-75 分;
    冇陣容消息、季初無數據嘅賽事會跌落 50 以下。上限刻意壓喺 85 —
    呢個模型本質係「銳利盤翻譯器 + 情境修正」,唔應該扮到有 90 分把握。
    """
    c = 40.0
    rmse = base_fit.get("rmse")
    if rmse is not None:
        c += max(-6, min(10, (0.05 - rmse) * 200))      # 擬合越貼越有信心
    n = base_fit.get("n") or 0
    c += min(6, n / 5.0)                                 # 銳利盤線數
    c += min(4, n_hk_lines / 6.0)                        # 馬會盤線數
    c += adj_conf                                        # 情境調整帶嚟嘅加減
    c += 2 if has_wx else 0
    c += 6 if has_news else -4
    # HKJC 真實變盤(同上一個快照比)。冇快照就唔加唔減。
    if hk_moved is False:
        c += 3          # 盤口穩定,雙方睇法一致
    elif hk_moved is True:
        c -= 2          # 臨場有郁,可能有我未掌握嘅資訊
    c += 4 if mins <= 35 else (2 if mins <= 70 else 0)
    return round(max(0.0, min(85.0, c)), 1)


HK_SNAP = os.path.join(HERE, "hk_snapshots.json")


def hk_odds_fingerprint(m) -> dict:
    """記低馬會每條線嘅現價,用嚟同下一階段比較,量度真實變盤。
    (HKJC feed 冇提供最後改盤時間 —— updateAt 只係開賣時間,唔可以當滯後訊號。)"""
    hk = H.flatten_odds(m)
    out = {}
    for code in ("HAD", "HDC", "HIL", "CHL"):
        for ln in (hk.get(code) or []):
            for side, o in (ln.get("odds") or {}).items():
                out[f"{code}|{ln.get('condition')}|{side}"] = o
    return out


def load_hk_snaps() -> dict:
    if os.path.exists(HK_SNAP):
        with open(HK_SNAP, encoding="utf-8") as handle:
            return json.load(handle)
    return {}


def hk_movement(prev: dict | None, cur: dict):
    """回傳 (有無郁, 最大變幅%, 變咗幾多條線)。"""
    if not prev:
        return None, None, None
    keys = set(prev) & set(cur)
    if not keys:
        return None, None, None
    diffs = [(cur[k] - prev[k]) / prev[k] for k in keys if prev[k]]
    if not diffs:
        return None, None, None
    n_moved = sum(1 for d in diffs if abs(d) > 0.005)
    mx = max(diffs, key=abs)
    return (n_moved > 0), round(mx * 100, 2), n_moved


def analyse_match(m, fx, wx_city_override=None, news=None, prev_snap=None,
                  stage_override=None):
    """對單場做完整預判。回傳 dict。"""
    ko = H.parse_kickoff(m)
    mins = (ko - dt.datetime.now(dt.timezone.utc)).total_seconds() / 60
    home_ch = m["homeTeam"]["name_ch"]
    away_ch = m["awayTeam"]["name_ch"]
    hk = H.flatten_odds(m)
    # The HKJC board does not give a per-line update timestamp.  This is the
    # exact time this selected board was observed, not its opening updateAt.
    selected_odds_observed_at = dt.datetime.now(HKT).isoformat(timespec="seconds")
    hname = ((fx or {}).get("home_team_display")
             or m.get("homeTeam", {}).get("name_en") or home_ch)
    aname = ((fx or {}).get("away_team_display")
             or m.get("awayTeam", {}).get("name_en") or away_ch)
    reversed_orientation = bool((fx or {}).get("_orientation_reversed"))

    # PinnAPI is a sharp-market reference, not a prerequisite for producing a
    # football forecast.  When matching or prices are unavailable, fit the
    # existing multi-market model from HKJC's current full price surface.  This
    # keeps forecasts alive without fabricating Pinnacle data.
    now = op = None
    model_source = "hkjc_full_market"
    sharp_reference_available = False
    sharp_error = None
    provider_live = False
    source = "hkjc_full_market"
    data_age_seconds = None
    source_status = "pinnapi_fixture_unmatched" if not fx else "pinnapi_not_requested"
    pinnapi_identity = None
    if fx:
        try:
            prices = S.fetch_odds(
                [fx["id"]], fixture_identities={str(fx["id"]): fx}
            ).get(fx["id"], [])
            provider_live = bool(prices) and all(
                bool(row.get("provider_live")) for row in prices
                if isinstance(row, dict)
            )
            fallback_used = bool(prices) and any(
                row.get("source") == "fallback"
                for row in prices if isinstance(row, dict)
            )
            ages = [
                float(row.get("data_age_seconds"))
                for row in prices
                if isinstance(row, dict) and row.get("data_age_seconds") is not None
            ]
            data_age_seconds = round(max(ages), 3) if ages else None
            pinnapi_identity = {
                "fixture_id": str(fx["id"]),
                "kickoff": fx.get("start_date"),
                "home": fx.get("home_team_display"),
                "away": fx.get("away_team_display"),
            }
            prices = S.orient_prices(prices, reversed_orientation)
            cur_st = S.structure(prices, hname, aname)
            now = P.fit_view(cur_st)
            if now:
                model_source = "pinnapi" if provider_live else "pinnapi_fallback"
                source = "pinnapi_live" if provider_live else "fallback"
                source_status = (
                    "pinnapi_live" if provider_live
                    else "pinnapi_fallback_diagnostic_only"
                )
                # A fallback quote is only a bounded diagnostic/forecast
                # continuity aid.  It is never an independent current market
                # benchmark and therefore can never enable EV, Kelly, an
                # retired portfolio entry, or notification.
                sharp_reference_available = provider_live and not fallback_used
                # PinnAPI does not expose an Optic-style historical opening
                # endpoint.  Preserve Footbreak's first observed valid quote.
                op = P.fit_view(S.opening_structure(fx["id"]))
                if provider_live:
                    S.remember_opening(fx["id"], cur_st)
            else:
                sharp_error = "PinnAPI 盤口不足以擬合"
        except Exception as exc:
            sharp_error = f"{type(exc).__name__}"
            source_status = "pinnapi_live_unavailable_no_safe_fallback"
    else:
        sharp_error = "賽事未能安全配對"
    if not now:
        now = P.fit_view(hk)
        source = "hkjc_full_market"
        provider_live = False
    if not now:
        return {"skip": "PinnAPI 不可用，而馬會全盤面亦不足以擬合模型"}

    # ── 情境調整 ──
    adjs: list[P.Adj] = []
    if sharp_reference_available:
        mv_adj, mv_sig = P.movement_adj(op, now)
        adjs += mv_adj
    else:
        mv_sig = {}

    city = wx_city_override or ((fx or {}).get("venue_location") or "").split(",")[0].strip()
    wx = C.weather_at(city, ko) if city else None
    adjs += P.weather_adj(wx)

    th = ((fx or {}).get("home_competitors") or [{}])[0].get("id")
    ta = ((fx or {}).get("away_competitors") or [{}])[0].get("id")
    fh = C.fatigue(th, ko) if th else {}
    fa = C.fatigue(ta, ko) if ta else {}
    adjs += P.fatigue_adj(fh, fa, (fx or {}).get("league", {}).get("id", ""))
    adjs += P.venue_adj(bool((fx or {}).get("venue_neutral")), "")

    if news:
        for n in news.get("adjustments", []):
            adjs.append(P.Adj(n.get("tag", "陣容"), n.get("reason", ""),
                              goals=n.get("goals", 1.0),
                              corners=n.get("corners", 1.0),
                              supremacy=n.get("supremacy", 0.0),
                              confidence=n.get("confidence", 0.0)))

    lh2, la2, mu2, mults = P.apply(now["lh"], now["la"], now.get("mu"), adjs)
    rho = now["rho"]
    phi = now.get("phi")

    mat, gp, cp, ph, pd, pa = P.outcome_probs(lh2, la2, rho, mu2, phi)

    # ── 對比馬會盤 ──
    n_hk = sum(len(hk.get(k) or []) for k in ("HAD", "HDC", "HIL", "CHL"))
    fg = (lh2, la2, rho, now["rmse"], now["n"])
    fc = (mu2, phi, now.get("c_rmse", 0), now.get("c_n", 0)) if (mu2 and phi) else None
    # A fallback snapshot may produce a forecast, but it must not be compared
    # with current HKJC prices to calculate EV/Kelly or create any bet-like
    # candidate.  A live PinnAPI reference and HKJC-only pure prediction keep
    # their existing behaviour.
    cands = (
        M.evaluate(hk, fg, fc, home_ch, away_ch)
        if source != "fallback" else []
    )

    fp = hk_odds_fingerprint(m)
    prev_fp = (prev_snap or {}).get("fingerprint")
    moved, max_move, n_moved = hk_movement(prev_fp, fp)

    adj_conf = sum(a.confidence for a in adjs)
    conv = conviction(now, adj_conf, mins, n_hk, wx is not None, bool(news), moved)

    # ── 每條候選:EV + 凱利 ──
    rows = []
    for c in cands:
        ev = P.ev_pct(c.prob, c.push, c.odds)
        kf = P.kelly_fraction(c.prob, c.push, c.odds)
        rows.append({"market": c.market, "code": c.code, "condition": c.condition,
                     "side": c.side, "label": c.label, "odds": c.odds,
                     "fair": round(c.fair, 3), "prob": round(c.prob, 4),
                     "push": round(c.push, 4), "ev": round(ev, 4),
                     "kelly_raw": round(kf, 4), "is_main": c.is_main})

    return {
        "match_id": m.get("id"), "home": home_ch, "away": away_ch,
        "home_en": hname, "away_en": aname,
        "fixture_id": (fx or {}).get("id"),
        "league_id": (fx or {}).get("league", {}).get("id"),
        "league": (m.get("tournament", {}).get("nameCH")
                   or (fx or {}).get("league", {}).get("name")),
        "kickoff_hkt": ko.astimezone(HKT).strftime("%Y-%m-%d %H:%M"),
        "mins_to_ko": round(mins, 1),
        "stage": stage_override or stage_of(mins),
        "venue": (fx or {}).get("venue_name"), "venue_city": city,
        "neutral": bool((fx or {}).get("venue_neutral")),
        "model_source": model_source,
        "sharp_reference_available": sharp_reference_available,
        "sharp_reference_note": sharp_error,
        "provider_live": provider_live,
        "source": source,
        "data_age_seconds": data_age_seconds,
        "source_status": source_status,
        "pinnapi_fixture_identity": pinnapi_identity,
        "hk_pool_opened": (m.get("foPools") or [{}])[0].get("updateAt"),
        "selected_odds_observed_at": selected_odds_observed_at,
        "hk_moved_since_last": moved, "hk_max_move_pct": max_move,
        "hk_n_lines_moved": n_moved, "hk_fingerprint": fp,
        "open": ({k: round(v, 4) for k, v in op.items()} if op else None),
        "now": {k: round(v, 4) for k, v in now.items()},
        "movement": mv_sig,
        "weather": wx,
        "fatigue": {"home": fh, "away": fa},
        "adjustments": [asdict(a) for a in adjs],
        "mults": mults,
        "final": {"lh": round(lh2, 3), "la": round(la2, 3),
                  "total": round(lh2 + la2, 3), "supremacy": round(lh2 - la2, 3),
                  "mu": (round(mu2, 2) if mu2 else None), "rho": round(rho, 4)},
        # Preserve the exact fitted inputs used by candidate pricing.  The
        # immutable T-5 snapshot uses these values to recompute Asian
        # quarter-line settlement mass without rounding drift.
        "quarter_line_model": {"lh": lh2, "la": la2, "rho": rho},
        "outcome": {"home": round(ph, 4), "draw": round(pd, 4), "away": round(pa, 4)},
        "top_scores": sorted(
            [{"s": f"{i}-{j}", "p": round(mat[i][j], 4)}
             for i in range(6) for j in range(6)],
            key=lambda x: -x["p"])[:6],
        "conviction": conv,
        "candidates": rows,
        "n_hk_lines": n_hk,
    }


def pick_one(res: dict, min_ev=0.015, conf_floor=P.CONF_FLOOR):
    """每場揀一個結論。回傳 (pick|None, reason)。"""
    conv = res["conviction"]
    if (
        res.get("source") == "fallback"
        or (
            res.get("provider_live") is False
            and res.get("model_source") == "pinnapi_fallback"
        )
    ):
        return None, (
            "PinnAPI 暫存快照只可維持賽前預測／診斷；"
            "禁止 EV、Kelly、正式倉及投注通知"
        )
    if not res.get("sharp_reference_available"):
        return None, (
            "無獨立 PinnAPI 同場基準；保留預測及學習紀錄，"
            "但禁止由馬會自身盤面建立模擬注"
        )
    st = K.stage()
    policies = st.get("entry_thresholds") or K.market_entry_thresholds(
        base_ev=min_ev, base_conf=conf_floor
    )
    for candidate in res["candidates"]:
        candidate["entry_policy"] = policies.get(
            str(candidate.get("code") or ""),
            {
                "min_edge": min_ev,
                "confidence_floor": conf_floor,
                "n_settled": 0,
                "reason": "configured_default",
            },
        )
    res["entry_thresholds"] = policies
    ok = [
        c for c in res["candidates"]
        if c["ev"] >= float(c["entry_policy"]["min_edge"])
        and conv >= float(c["entry_policy"]["confidence_floor"])
    ]
    if not ok:
        best = max(res["candidates"], key=lambda c: c["ev"]) if res["candidates"] else None
        if best:
            policy = best["entry_policy"]
            detail = (
                f"{best['label']}：信念 {conv:.1f}/{policy['confidence_floor']:g}，"
                f"EV {best['ev']:+.2%}/{policy['min_edge']:.2%}，"
                f"市場樣本 {policy.get('n_settled', 0)}"
            )
        else:
            detail = "沒有可評估市場"
        return None, f"未過分市場動態門檻（{detail}）— 觀望"
    # 排序:信念 × 價值。主線優先(流通量高、更可信)
    for c in ok:
        c["score"] = c["ev"] * (1.0 + (0.15 if c["is_main"] else 0))
    best = max(ok, key=lambda c: c["score"])
    # ── 分數凱利:階段控制 + 市場折讓 + 信念縮放,最後才套單場上限 ──
    mkt_mult = float(st["market_mult"].get(best["code"], 1.0))
    frac = best["kelly_raw"] * st["fraction"] * mkt_mult
    frac *= min(1.0, conv / 75.0)          # 信念未夠 75 分按比例縮注
    frac = min(frac, st["cap"])            # 單場硬上限
    best["stake"] = round(P.BANKROLL * frac / 10) * 10
    best["kelly_used"] = round(frac, 4)
    best["stake_stage"] = {
        "level": st["level"], "label": st["label"],
        "fraction": round(st["fraction"], 4), "cap": st["cap"],
        "market_mult": mkt_mult, "n_settled": st["n_settled"],
        "slope": (round(st["slope"], 3) if st["slope"] is not None else None),
    }
    return best, ""


def failed_prediction(m: dict, stage: str, mins: float, reason: str,
                      *, reason_code: str = "source_or_model_unavailable") -> dict:
    """Persist a fail-closed timed decision instead of leaving the UI waiting."""
    ko = H.parse_kickoff(m)
    return {
        "match_id": m.get("id"),
        "home": m.get("homeTeam", {}).get("name_ch"),
        "away": m.get("awayTeam", {}).get("name_ch"),
        "home_en": m.get("homeTeam", {}).get("name_en"),
        "away_en": m.get("awayTeam", {}).get("name_en"),
        "fixture_id": None,
        "league_id": None,
        "league": m.get("tournament", {}).get("nameCH"),
        "kickoff_hkt": ko.astimezone(HKT).strftime("%Y-%m-%d %H:%M"),
        "mins_to_ko": round(mins, 1),
        "stage": stage,
        "conviction": 0.0,
        "candidates": [],
        "pick": None,
        "lead_view": None,
        "can_bet": stage == "T-5",
        "no_bet_reason": reason,
        "provider_live": False,
        "source": "unavailable",
        "data_age_seconds": None,
        "source_status": reason_code,
        "pinnapi_fixture_identity": None,
        "final": None,
        "outcome": None,
        "hk_fingerprint": hk_odds_fingerprint(m),
        "n_hk_lines": 0,
    }


def _due_worker(send, kind: str, payload) -> None:
    """Isolate a single potentially blocking urgent operation in a killable process."""
    try:
        if kind == "analyse":
            match, fixture, stage, previous = payload
            value = analyse_match(match, fixture, news=None, prev_snap=previous, stage_override=stage)
        elif kind == "optional_drain":
            import record_picks
            # Keep the sidecar retry bound to the same ledger selected by this
            # timer instance (including isolated test/recovery directories).
            record_picks.HERE = HERE
            record_picks.LEDGER = os.path.join(HERE, "sim_ledger.json")
            value = record_picks.drain_post_commit_jobs()
        else:
            _persist_urgent_result(payload)
            value = True
        send.send(("ok", value))
    except BaseException as exc:
        send.send(("error", type(exc).__name__))
    finally:
        send.close()


def _bounded_due_call(kind: str, payload, deadline: float):
    """Return an operation result or ``None`` without spending the tick deadline.

    Thread cancellation cannot stop a socket blocked in a provider library.  The
    Linux service therefore forks one fixture operation at a time and terminates
    it on its small share of the monotonic pass budget.  The outer runner retains
    the shared service lock throughout, while each ledger commit remains atomic.
    """
    remaining = deadline - time.monotonic()
    if remaining <= 0 or os.name != "posix":
        return None
    try:
        configured_cap = float(os.getenv("FOOTBREAK_URGENT_CALL_TIMEOUT_SECONDS", "8"))
    except ValueError:
        configured_cap = 8.0
    call_cap = min(8.0, max(0.05, configured_cap), remaining)
    receiver, sender = multiprocessing.get_context("fork").Pipe(duplex=False)
    process = multiprocessing.get_context("fork").Process(target=_due_worker, args=(sender, kind, payload))
    process.start(); sender.close()
    try:
        ready = wait_for_connections([receiver], timeout=call_cap)
        if not ready:
            return None
        status, value = receiver.recv()
        return value if status == "ok" else None
    except EOFError:
        return None
    finally:
        receiver.close()
        if process.is_alive():
            process.terminate()
        process.join(timeout=0.25)
        if process.is_alive():
            process.kill(); process.join(timeout=0.25)


def _persist_urgent_result(result: dict) -> None:
    """Commit one completed timed stage before looking at the next fixture."""
    # Import lazily: run_predict also remains a standalone module in legacy tools.
    import record_picks
    fd, path = tempfile.mkstemp(prefix=".urgent-stage-", suffix=".json", dir=HERE)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump([result], handle, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        # This preserves the established atomic ledger/bet idempotency rules but
        # explicitly suppresses transport; delivery is bounded and separate.
        record_picks.sync(os.path.basename(path), send_notifications=False)
    finally:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass


def _update_native_stage_ledger(mutator):
    """Apply one local manifest/attempt transition as an atomic ledger write."""
    payload = _ledger_payload(strict=True)
    changed, value = mutator(payload)
    if changed:
        write_json_atomic(os.path.join(HERE, "sim_ledger.json"), payload)
    return value


def _repair_native_stage_schedule(now: dt.datetime) -> tuple[int, int]:
    """Materialize future legacy work and expire lapsed jobs without providers."""
    def repair(payload):
        migrated = stage_state.migrate_future_manifests(payload, now=now)
        expired = stage_state.expire_lapsed_work(payload, now=now)
        return bool(migrated or expired), (migrated, expired)
    return _update_native_stage_ledger(repair)


def _start_due_attempts(due, now: dt.datetime) -> dict[tuple[str, str], dict]:
    """Durably journal every selected batch item before any provider read."""
    wanted = {(str(mid), str(stage)) for _ko, mid, _watch, stage in due}

    def start(payload):
        watches = payload.get("watch") if isinstance(payload.get("watch"), dict) else {}
        started = {}
        changed = False
        for raw_id, watch in watches.items():
            if not isinstance(watch, dict):
                continue
            match_id = str(watch.get("match_id") or raw_id)
            for stage in ("T-30", "T-5"):
                if (match_id, stage) not in wanted:
                    continue
                try:
                    event = stage_state.start_attempt(payload, watch, stage, now=now)
                except ValueError:
                    continue
                started[(match_id, stage)] = event
                changed = True
        return changed, started
    return _update_native_stage_ledger(start)


def _finish_due_attempts(attempts, status: str, now: dt.datetime, reason: str) -> None:
    """Append terminal evidence for one or more already-journaled attempts."""
    pending = [row for row in attempts if isinstance(row, dict)]
    if not pending:
        return

    def finish(payload):
        changed = False
        latest = stage_state.latest_attempts(payload)
        for attempt in pending:
            key = (
                str(attempt.get("hkjc_match_id") or ""),
                str(attempt.get("kickoff_at_utc") or ""),
                str(attempt.get("stage") or ""),
            )
            current = latest.get(key)
            if not isinstance(current, dict) or str(current.get("status") or "") in stage_state.TERMINAL:
                continue
            stage_state.finish_attempt(payload, current, status, now=now, reason=reason)
            changed = True
            latest = stage_state.latest_attempts(payload)
        return changed, None
    _update_native_stage_ledger(finish)


def _expired_attempt(attempt: dict, now: dt.datetime, reason: str) -> None:
    _finish_due_attempts([attempt], "EXPIRED", now, reason)


def _fair_rotate_due(due: list, now: dt.datetime) -> list:
    """Rotate same-stage/same-due-minute clusters so no fixture tail always
    starves when one tick's bounded pass budget cannot finish an entire large
    same-kickoff batch.  `persisted_due_stages` already sorts T-5 ahead of
    T-30 and by kickoff/match_id for determinism; this only changes which
    item leads within an identical (stage, due-minute) cluster, using the
    current wall-clock minute so consecutive ticks favour a different item.
    A retry (this tick's own STARTED/FAILED-retryable carryover) therefore
    gets an earlier turn on a later tick instead of being permanently last.
    """
    if len(due) < 2:
        return due
    clusters: dict[tuple[str, str], list] = {}
    order: list[tuple[str, str]] = []
    for item in due:
        kickoff, mid, watch, stage = item
        due_job = (watch.get("native_stage_manifest") or {}).get("jobs", {}).get(stage, {}) \
            if isinstance(watch, dict) else {}
        due_key = str(due_job.get("due_at_utc") or kickoff.isoformat())
        key = (stage, due_key)
        if key not in clusters:
            clusters[key] = []
            order.append(key)
        clusters[key].append(item)
    rotated: list = []
    minute_index = int(now.timestamp() // 60)
    for key in order:
        bucket = clusters[key]
        if len(bucket) > 1:
            offset = minute_index % len(bucket)
            bucket = bucket[offset:] + bucket[:offset]
        rotated.extend(bucket)
    return rotated


def _run_due_tick(match_ids, horizon_min, out, force, stage_filter):
    """Deadline-first local queue: no board discovery, fixture list, or dashboard work."""
    try:
        pass_budget = float(os.getenv("FOOTBREAK_TICK_DEADLINE_SECONDS", "40"))
    except ValueError:
        pass_budget = 40.0
    deadline = time.monotonic() + min(45.0, max(5.0, pass_budget))
    _repair_native_stage_schedule(dt.datetime.now(HKT))
    due = persisted_due_stages(horizon_min, strict=True)
    if stage_filter:
        due = [item for item in due if item[3] == stage_filter]
    if match_ids:
        wanted = {str(item) for item in match_ids}
        due = [item for item in due if item[1] in wanted]
    if not due:
        write_json_atomic(os.path.join(HERE, out), [])
        print("0 場已處理 · 本地到期隊列為空（沒有遠端查詢）")
        return []
    due = _fair_rotate_due(due, dt.datetime.now(HKT))
    attempts = _start_due_attempts(due, dt.datetime.now(HKT))
    # Direct fixture reads are the only mandatory remote operation in a tick.
    by_id = {}
    for start in range(0, len(due), 20):
        if time.monotonic() >= deadline:
            break
        try:
            rows = H.fetch_matches(match_ids=[item[1] for item in due[start:start + 20]])
        except Exception as exc:
            batch = [attempts.get((item[1], item[3])) for item in due[start:start + 20]]
            _finish_due_attempts(
                batch, "FAILED", dt.datetime.now(HKT),
                f"provider_fetch_{type(exc).__name__}",
            )
            print(f"到期賽事直接查詢失敗（{type(exc).__name__}）；已寫入 terminal evidence")
            continue
        by_id.update({str(row.get("id")): row for row in rows if row.get("id") is not None})
    results = []
    for kickoff, mid, watch, scheduled_stage in due:
        if time.monotonic() >= deadline:
            break
        attempt = attempts.get((mid, scheduled_stage))
        if not isinstance(attempt, dict):
            continue
        match = by_id.get(mid)
        if not isinstance(match, dict):
            _finish_due_attempts(
                [attempt], "DATA_MISSING", dt.datetime.now(HKT),
                "provider_fixture_missing",
            )
            continue
        if match.get("status") != "PREEVENT":
            if kickoff <= dt.datetime.now(HKT):
                _expired_attempt(attempt, dt.datetime.now(HKT), "kickoff_elapsed_before_provider_stage")
            else:
                _finish_due_attempts(
                    [attempt], "DATA_MISSING", dt.datetime.now(HKT),
                    f"provider_status_{str(match.get('status') or 'missing').lower()}",
                )
            continue
        ko = H.parse_kickoff(match)
        if ko is None:
            _finish_due_attempts([attempt], "DATA_MISSING", dt.datetime.now(HKT), "provider_kickoff_missing")
            continue
        if ko <= dt.datetime.now(HKT):
            _expired_attempt(attempt, dt.datetime.now(HKT), "kickoff_elapsed_before_analysis")
            continue
        if abs((ko.astimezone(dt.timezone.utc) - kickoff.astimezone(dt.timezone.utc)).total_seconds()) > 60:
            _finish_due_attempts([attempt], "DATA_MISSING", dt.datetime.now(HKT), "provider_kickoff_identity_mismatch")
            continue
        current_due = {
            (entry[1], entry[3]) for entry in persisted_due_stages(horizon_min, strict=True)
        }
        if (mid, scheduled_stage) not in current_due:
            if ko <= dt.datetime.now(HKT):
                _expired_attempt(attempt, dt.datetime.now(HKT), "kickoff_elapsed_before_analysis")
            continue
        fixture_id = None if force else watch.get("fixture_id")
        fixture = _fixture_from_watch(watch, str(fixture_id)) if fixture_id else None
        result = _bounded_due_call(
            "analyse", (match, fixture, scheduled_stage, load_hk_snaps().get(mid)), deadline
        )
        if not isinstance(result, dict):
            _finish_due_attempts([attempt], "FAILED", dt.datetime.now(HKT), "analysis_timeout_or_error")
            print(f"{scheduled_stage} 分析失敗；已寫入 terminal evidence")
            continue
        if result.get("skip"):
            _finish_due_attempts([attempt], "DATA_MISSING", dt.datetime.now(HKT), "analysis_data_missing")
            continue
        if ko <= dt.datetime.now(HKT):
            _expired_attempt(attempt, dt.datetime.now(HKT), "kickoff_elapsed_before_persist")
            continue
        result["mins_to_ko"] = (ko - dt.datetime.now(HKT)).total_seconds() / 60.0
        pick, reason = pick_one(result)
        result["can_bet"] = scheduled_stage == "T-5"
        result["pick"] = pick if result["can_bet"] else None
        result["lead_view"] = pick
        result["no_bet_reason"] = reason if result["can_bet"] else (f"{scheduled_stage} 只作預測,落注統一喺 T-5" if pick else reason)
        result["_native_stage_attempt_id"] = attempt["attempt_id"]
        # Atomic state/bet persistence is intentionally before the next fixture.
        if _bounded_due_call("persist", result, deadline) is not True:
            _finish_due_attempts([attempt], "FAILED", dt.datetime.now(HKT), "persistence_timeout_or_error")
            continue
        results.append(result)
        write_json_atomic(os.path.join(HERE, out), results)
    unfinished = [
        attempt for attempt in attempts.values()
        if isinstance(attempt, dict)
    ]
    if time.monotonic() >= deadline:
        _finish_due_attempts(
            unfinished, "FAILED", dt.datetime.now(HKT), "tick_deadline_elapsed",
        )
    results.sort(key=lambda row: row.get("mins_to_ko", 999999))
    write_json_atomic(os.path.join(HERE, out), results)
    print(
        f"{len(results)} 場到期階段已逐場持久化；"
        "非終結 STARTED 只會在開賽前由重啟恢復，終結失敗已保留證據"
    )
    return results


def main(match_ids=None, horizon_min=700, out="predictions.json", mode="due", force=False, stage_filter=None):
    """Run the deadline-first due queue or the separate full-board sweep."""
    if mode == "due":
        rows = _run_due_tick(match_ids, horizon_min, out, force, stage_filter)
        # Run durable deferred consumers only after the native due path has
        # selected and committed its work. A timeout here cannot roll back or
        # delay a native stage; the queue remains retryable on the next tick.
        _bounded_due_call("optional_drain", None, time.monotonic() + 2.0)
        return rows
    matches = [m for m in fetch_matches_with_due_recovery(mode, horizon_min) if m.get("status") == "PREEVENT"]
    matches.sort(key=lambda row: H.parse_kickoff(row) or dt.datetime.max.replace(tzinfo=dt.timezone.utc))
    try:
        fixtures = S.list_fixtures()
    except Exception:
        fixtures = []
    news_all, snaps = load_news_adj(), load_hk_snaps()
    done = {} if force else done_stages()
    fixture_ids = {} if force else known_fixture_ids()
    fixtures_by_id = {str(fx.get("id")): fx for fx in fixtures if fx.get("id") is not None}
    results = []
    for m in matches:
        mid = str(m.get("id")); ko = H.parse_kickoff(m)
        if not ko or (match_ids and mid not in match_ids): continue
        mins = (ko - dt.datetime.now(dt.timezone.utc)).total_seconds()/60
        if mins <= 0 or mins > horizon_min: continue
        seen = done.get(mid, set())
        stage = SWEEP if mode == "sweep" and SWEEP not in seen else (stage_of(mins) if mode == "all" else None)
        if not stage or (stage_filter and stage != stage_filter): continue
        fx = fixtures_by_id.get(fixture_ids.get(mid))
        if not fx: fx, _ = S.match_fixture(m, fixtures, ko)
        try: r = analyse_match(m, fx, news=news_all.get(mid), prev_snap=snaps.get(mid), stage_override=stage)
        except Exception: continue
        if r.get("skip") or ko <= dt.datetime.now(HKT): continue
        r["mins_to_ko"] = (ko-dt.datetime.now(HKT)).total_seconds()/60; pick, reason = pick_one(r)
        r["can_bet"] = stage == "T-5"; r["pick"] = pick if r["can_bet"] else None; r["lead_view"] = pick; r["no_bet_reason"] = reason
        results.append(r)
    write_json_atomic(os.path.join(HERE, out), results)
    return results


if __name__ == "__main__":
    a = sys.argv[1:]
    mode = "due"
    if "--sweep" in a:
        mode = "sweep"
    elif "--all" in a:
        mode = "all"
    nums = [x for x in a if x.lstrip("-").isdigit()]
    hz = int(nums[0]) if nums else (2160 if mode == "sweep" else 60)
    stage_filter = "T-5" if "--t5-only" in a else ("T-30" if "--t30-only" in a else None)
    main(
        horizon_min=hz,
        mode=mode,
        force="--force" in a,
        stage_filter=stage_filter,
    )
