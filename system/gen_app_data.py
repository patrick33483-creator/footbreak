"""把 predictions.json + sim_ledger.json 打包成儀表板用嘅 data.json。

唔會叫任何外部 API — 純粹由已有輸出重算分佈。
"""
import json
import math
import re
import os
import sys
import datetime as dt
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import model as M
import predict as P
import staking as K
from record_picks import PREDICTION_ERA, PREDICTION_SCHEMA_VERSION

OUT = os.path.join(os.path.dirname(HERE), "hkjc-dashboard", "data.json")
HKT = dt.timezone(dt.timedelta(hours=8))
DONE_MIN = 130.0        # 開賽後幾分鐘當完場,同 settle.py 一致


def dists(r):
    f, now = r["final"], r["now"]
    phi = now.get("phi")
    if f.get("mu") and phi:
        phi = M.cap_phi(f["mu"], phi)   # 舊記錄嘅 phi 未壓過離散度下限,顯示前補回
    mat = M.score_matrix(f["lh"], f["la"], f.get("rho") or 0.0)
    gp = M.goals_pmf(mat)
    cp = M.corner_pmf(f["mu"], phi) if (f.get("mu") and phi) else None

    # 壓縮:比分矩陣只留 0-6,總入球 0-7+,角球 0-20+
    N = 7
    m2 = [[round(mat[i][j], 5) for j in range(N)] for i in range(N)]
    g2 = [round(x, 5) for x in gp[:8]]
    g2[-1] = round(1 - sum(g2[:-1]), 5)
    c2 = None
    if cp:
        c2 = [round(x, 5) for x in cp[:21]]
        c2[-1] = round(max(0.0, 1 - sum(c2[:-1])), 5)

    tops = []
    for i in range(len(mat)):
        for j in range(len(mat[i])):
            tops.append((mat[i][j], i, j))
    tops.sort(reverse=True)
    return {
        "matrix": m2, "goals_dist": g2, "corners_dist": c2, "phi": phi,
        "top_scores": [{"s": f"{i}-{j}", "p": round(p, 5)} for p, i, j in tops[:8]],
    }


def _ou(dist, line):
    """由離散分佈計「大過 line」嘅機率(line 用 .5,唔會有走水)。"""
    if not dist:
        return None
    k = int(line) + 1
    return round(sum(dist[k:]), 4)


def _band(dist, lo=0.10, hi=0.90):
    """回傳 (中位數, lo 分位, hi 分位)。"""
    if not dist:
        return None
    out, c = [], 0.0
    q = {lo: None, 0.5: None, hi: None}
    for i, p in enumerate(dist):
        c += p
        for k in q:
            if q[k] is None and c >= k:
                q[k] = i
    n = len(dist) - 1
    return [q[0.5] if q[0.5] is not None else n,
            q[lo] if q[lo] is not None else 0,
            q[hi] if q[hi] is not None else n]


def forecast(r):
    """每場一份輕量「純預測」摘要 —— 唔理有冇注,淨係講模型估咩賽果。

    同 dists() 分開:dists() 出完整矩陣(重),只有今次真正跑過嘅場先有;
    forecast() 由 final 參數重新砌,連 archived 場都有,而且細好多。
    """
    f = r.get("final") or {}
    if not f.get("lh") or not f.get("la"):
        return None
    now = r.get("now") or {}
    phi = now.get("phi")
    if f.get("mu") and phi:
        phi = M.cap_phi(f["mu"], phi)
    mat = M.score_matrix(f["lh"], f["la"], f.get("rho") or 0.0)
    gp = M.goals_pmf(mat)
    cp = M.corner_pmf(f["mu"], phi) if (f.get("mu") and phi) else None

    ph = pd_ = pa = 0.0
    btts = 0.0
    tops = []
    for i, row in enumerate(mat):
        for j, p in enumerate(row):
            if i > j:
                ph += p
            elif i == j:
                pd_ += p
            else:
                pa += p
            if i and j:
                btts += p
            tops.append((p, i, j))
    tops.sort(reverse=True)

    g = [round(x, 5) for x in gp[:9]]
    g[-1] = round(max(0.0, 1 - sum(g[:-1])), 5)
    c = None
    if cp:
        c = [round(x, 5) for x in cp[:21]]
        c[-1] = round(max(0.0, 1 - sum(c[:-1])), 5)

    return {
        "lh": round(f["lh"], 3), "la": round(f["la"], 3),
        "total": round(f["lh"] + f["la"], 3),
        "sup": round(f["lh"] - f["la"], 3),
        "mu": round(f["mu"], 2) if f.get("mu") else None,
        "p": [round(ph, 4), round(pd_, 4), round(pa, 4)],
        "btts": round(btts, 4),
        "tops": [{"s": f"{i}-{j}", "p": round(p, 4)} for p, i, j in tops[:5]],
        "goals": g, "gband": _band(gp),
        "o15": _ou(gp, 1.5), "o25": _ou(gp, 2.5), "o35": _ou(gp, 3.5),
        "corners": c, "cband": _band(cp) if cp else None,
        "c85": _ou(cp, 8.5) if cp else None,
        "c95": _ou(cp, 9.5) if cp else None,
        "c105": _ou(cp, 10.5) if cp else None,
    }


WDL_LABELS = ("主勝", "和局", "客勝")
HISTORY_STAGES = ("首預", "T-30", "T-5")
SCOREABLE_MARKETS = {"HDC", "HIL", "CHL"}
PREDICTION_ARCHIVE = os.environ.get(
    "FOOTBREAK_PREDICTION_ARCHIVE_PATH",
    os.path.join(HERE, "prediction_history_archive.json"),
)


def _scoreable_market_predictions(value):
    rows = []
    for prediction in value or []:
        if not isinstance(prediction, dict):
            continue
        if prediction.get("code") not in SCOREABLE_MARKETS:
            continue
        if prediction.get("side") not in {"H", "A", "L"}:
            continue
        raw_line = prediction.get("line")
        if raw_line is None:
            raw_line = prediction.get("condition")
        try:
            line = float(raw_line)
            probability = float(prediction.get("probability"))
        except (TypeError, ValueError):
            continue
        if not math.isfinite(line) or not math.isfinite(probability):
            continue
        rows.append({
            **prediction,
            "condition": line,
            "line": line,
            "probability": probability,
        })
    return rows


def sync_prediction_archive(watch, path=None):
    """Persist every scoreable prediction stage independently of live watch."""
    path = path or PREDICTION_ARCHIVE
    try:
        with open(path, encoding="utf-8") as handle:
            archived = json.load(handle)
        if not isinstance(archived, dict):
            archived = {}
    except (OSError, ValueError, TypeError):
        archived = {}

    # 舊版本可能已將 NaN 盤口寫入 archive。每次同步先清洗整個 archive，
    # 確保壞資料不會因為賽事已離開 live watch 而永久留在預測紀錄。
    cleaned_archive = {}
    for mid, match in archived.items():
        if not isinstance(match, dict):
            continue
        cleaned_stages = []
        for stage in match.get("stages") or []:
            if not isinstance(stage, dict):
                continue
            predictions = _scoreable_market_predictions(stage.get("market_predictions"))
            if predictions:
                cleaned_stages.append({**stage, "market_predictions": predictions})
        if cleaned_stages:
            cleaned_archive[str(mid)] = {**match, "stages": cleaned_stages}
    archived = cleaned_archive

    for mid, current in (watch or {}).items():
        stages = []
        for stage in current.get("stages") or []:
            predictions = _scoreable_market_predictions(stage.get("market_predictions"))
            if (
                stage.get("prediction_era") == "2026-08-10-market-learning-v2"
                and predictions
            ):
                stages.append({**stage, "market_predictions": predictions})
        if not stages:
            continue
        mid = str(mid)
        previous = archived.get(mid) or {}
        stage_map = {
            (stage.get("prediction_era"), stage.get("stage")): stage
            for stage in (previous.get("stages") or [])
            if isinstance(stage, dict)
        }
        for stage in stages:
            key = (stage.get("prediction_era"), stage.get("stage"))
            old = stage_map.get(key)
            if old is None or str(stage.get("ts") or "") >= str(old.get("ts") or ""):
                stage_map[key] = stage
        archived[mid] = {
            **previous,
            **{key: value for key, value in current.items() if key != "stages"},
            "stages": list(stage_map.values()),
        }

    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".prediction-history.", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(archived, handle, ensure_ascii=False, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return archived


def build_prediction_history(watch, bets, accuracy):
    """把足破全量三段預測轉成皇冠儀表板同款紀錄。

    watch 係正式預測來源；accuracy 只補賽果與評分。模擬注與純預測
    仍然分開，只有實際建立過注單嘅階段先標示「有模擬注」。
    """
    watch = watch or {}
    bets = bets or []
    accuracy = accuracy or {}

    scored = {}
    match_results = {}
    excluded_results = {
        str(row.get("match_id")): row
        for row in (accuracy.get("excluded_results") or [])
    }
    for match in accuracy.get("matches") or []:
        mid = str(match.get("match_id"))
        match_results[mid] = match
        for stage_score in match.get("stages") or []:
            stage = stage_score.get("stage")
            if stage:
                scored[(mid, stage)] = stage_score

    bet_by_stage = {}
    for bet in bets:
        mid = str(bet.get("match_id"))
        stage = bet.get("first_stage") or bet.get("stage") or "T-5"
        bet_by_stage.setdefault((mid, stage), bet)

    rows = []
    seen = set()

    def add_row(mid, match, stage, snap=None):
        key = (mid, stage)
        if key in seen:
            return
        snap = snap or {}
        score = scored.get(key) or {}
        market_predictions = _scoreable_market_predictions(
            snap.get("market_predictions") or score.get("market_predictions")
        )
        # 「純預測紀錄」係市場方向學習集。只有主客和、或者根本冇
        # 保存讓球／入球大細／角球方向嘅舊快照，一律唔顯示亦唔計分。
        if not market_predictions:
            return
        seen.add(key)
        result = match_results.get(mid) or {}
        excluded = excluded_results.get(mid)

        fc = None
        if snap.get("final"):
            try:
                fc = forecast(snap)
            except Exception:
                fc = None
        probs = (fc or {}).get("p") or []
        pick_idx = score.get("wdl_pick")
        if pick_idx is None and probs:
            pick_idx = max(range(len(probs)), key=lambda i: probs[i])
        probability = score.get("wdl_pmax")
        if probability is None and pick_idx is not None and len(probs) > pick_idx:
            probability = probs[pick_idx]

        actual_idx = score.get("wdl_act")
        actual = (WDL_LABELS[actual_idx]
                  if isinstance(actual_idx, int) and 0 <= actual_idx < len(WDL_LABELS)
                  else None)
        bet = bet_by_stage.get(key)
        no_bet_reason = snap.get("no_bet_reason")
        if not no_bet_reason and not bet:
            no_bet_reason = "未達模擬投注條件"

        rows.append({
            "prediction_era": snap.get("prediction_era") or accuracy.get("prediction_era"),
            "match_id": mid,
            "home": match.get("home"),
            "away": match.get("away"),
            "league": match.get("league"),
            "kickoff": match.get("kickoff"),
            "stage": stage,
            "predicted_at": snap.get("ts"),
            "forecast": (
                WDL_LABELS[pick_idx]
                if isinstance(pick_idx, int) and 0 <= pick_idx < len(WDL_LABELS)
                else "資料不足（不列入準繩度）"
            ),
            "probability": probability,
            "likely_score": ((fc or {}).get("tops") or [{}])[0].get("s")
                            or score.get("score_top"),
            "conviction": snap.get("conviction", score.get("conf")),
            "simulated_bet": bool(bet),
            "bet_label": bet.get("label") if bet else None,
            "no_bet_reason": no_bet_reason,
            "actual": actual,
            "score": result.get("score") or score.get("score_act"),
            "result_detail": {
                "corners_total": (
                    result.get("corners")
                    if result.get("corners") is not None
                    else score.get("corners_act")
                ),
            },
            "result_source": result.get("result_source"),
            "correct": (
                bool(score.get("wdl_hit"))
                if actual and score.get("wdl_hit") is not None else None
            ),
            "result_status": ("已核對" if actual else
                              "不計" if excluded else "待賽果"),
            "excluded_reason": excluded.get("status") if excluded else None,
            "wdl_brier": score.get("wdl_brier"),
            "wdl_ll": score.get("wdl_ll"),
            "market_predictions": market_predictions,
            "market_grades": score.get("market_grades") or [],
        })

    for mid, match in watch.items():
        mid = str(mid)
        for snap in match.get("stages") or []:
            stage = snap.get("stage")
            if (stage in HISTORY_STAGES
                    and snap.get("prediction_era") == "2026-08-10-market-learning-v2"):
                add_row(mid, match, stage, snap)

    # accuracy_history 係全量留存。就算舊 watch 日後被整理，已核對紀錄都不會消失。
    for match in accuracy.get("matches") or []:
        mid = str(match.get("match_id"))
        for stage_score in match.get("stages") or []:
            stage = stage_score.get("stage")
            if stage in HISTORY_STAGES:
                add_row(mid, match, stage)

    rows.sort(key=lambda r: (str(r.get("kickoff") or ""),
                             str(r.get("predicted_at") or "")), reverse=True)
    # The visible scorecard must be comparable to the immutable learning DB:
    # current model era only.  ``rows`` remains the full audit history.
    comparable_rows = [
        row for row in rows if row.get("prediction_era") == PREDICTION_ERA
    ]
    # Historical recovery is a private, read-only overlay.  It decorates this
    # generated payload only; the archive/watch and their raw snapshots remain
    # byte-for-byte unchanged.
    from analysis.odds_recovery import overlay_rows
    rows = overlay_rows(rows, "footbreak")
    comparable_rows = [
        row for row in rows if row.get("prediction_era") == PREDICTION_ERA
    ]
    stats = _prediction_history_stats(comparable_rows)
    stats["all_history_audit"] = _prediction_history_stats(rows)
    stats["scope"] = {
        "model_version": PREDICTION_ERA,
        "schema_version": PREDICTION_SCHEMA_VERSION,
        "rows": len(comparable_rows),
        "all_history_rows": len(rows),
        "description": "目前模型版本；全歷史保留於 all_history_audit。",
    }
    return {"rows": rows, "stats": stats}


def _prediction_history_stats(rows):
    graded_rows = [r for r in rows if r.get("result_status") == "已核對"]
    pending_rows = [r for r in rows if r.get("result_status") == "待賽果"]
    excluded_rows = [r for r in rows if r.get("result_status") == "不計"]
    wdl_graded_rows = [r for r in graded_rows if r.get("correct") is not None]
    hits = sum(1 for r in wdl_graded_rows if r.get("correct") is True)

    by_stage = {}
    for stage in HISTORY_STAGES:
        stage_rows = [r for r in rows if r.get("stage") == stage]
        stage_graded = [r for r in stage_rows if r.get("correct") is not None]
        stage_hits = sum(1 for r in stage_graded if r.get("correct") is True)
        by_stage[stage] = {
            "predictions": len(stage_rows),
            "graded": len(stage_graded),
            "hits": stage_hits,
            "accuracy": (stage_hits / len(stage_graded)) if stage_graded else None,
        }

    from analysis.market_statistics import MARKETS, market_metrics

    by_market = {
        code: market_metrics(rows, code)
        for code in MARKETS
    }
    by_stage_market = {
        stage: {
            code: market_metrics(
                [row for row in rows if row.get("stage") == stage],
                code,
            )
            for code in MARKETS
        }
        for stage in HISTORY_STAGES
    }

    from analysis.three_stage_consensus import (
        calculate_three_stage_consensus,
        calculate_three_stage_transitions,
    )

    return {
        "matches": len({r.get("match_id") for r in rows}),
        "predictions": len(rows),
        "graded": len(graded_rows),
        "pending": len(pending_rows),
        "excluded": len(excluded_rows),
        # Explicitly WDL/1X2-only; market hit rates live in by_market.
        "wdl_graded": len(wdl_graded_rows),
        "wdl_hits": hits,
        "wdl_accuracy": (hits / len(wdl_graded_rows)) if wdl_graded_rows else None,
        # Legacy aliases are retained for API readers, but the dashboard uses
        # the explicit WDL names to avoid suggesting a cross-market total.
        "hits": hits,
        "accuracy": (hits / len(wdl_graded_rows)) if wdl_graded_rows else None,
        "by_stage": by_stage,
        "by_market": by_market,
        "by_stage_market": by_stage_market,
        "market_overall": market_metrics(rows),
        "three_stage_consensus": calculate_three_stage_consensus(rows),
        "three_stage_transitions": calculate_three_stage_transitions(rows),
        "learning_status": "collecting_market_level_shadow_samples",
    }


# --------------------------------------------------------- 讓球標籤正規化
# 馬會 HDC condition 係主隊視角,舊記錄嘅 label 直接印咗原文,買客隊時
# 容易被誤讀。呢層純顯示改寫,唔會動 bet_id / condition / 結算邏輯。

_RE_HDCTXT = re.compile(r"\s*(?:(?:受讓|讓)\s*[0-9.]+|平手)\s*$")
_RE_PAREN = re.compile(r"[（(]\s*馬會盤[^）)]*[）)]\s*$")


def _fix_one(label, home, away, side=None, cond=None):
    """把讓球標籤正規化成「隊名 讓/受讓 X(馬會盤 <選邊視角盤口>)」。

    冪等:已經係新格式嘅標籤會先剝走括弧同讓球描述,再重新組裝。
    """
    if not isinstance(label, str):
        return label
    body = _RE_PAREN.sub("", label).strip()
    pre = ""
    if body.startswith("讓球"):
        pre, body = "讓球 ", body[2:].strip()
    body = _RE_HDCTXT.sub("", body).strip()      # 剝走舊有「受讓 0.25」/「平手」
    c = cond
    if c is None:
        parts = body.rsplit(" ", 1)
        if len(parts) != 2:
            return label
        body, c = parts[0].strip(), parts[1].strip()
    if cond is not None:
        parts = body.rsplit(" ", 1)
        if len(parts) == 2 and M.hdc_value(parts[1].strip()) is not None:
            body = parts[0]                      # 剝走舊有原始盤口尾巴
    team = body.strip()
    if not team or M.hdc_value(c) is None:
        return label
    s2 = side or ("H" if team == home else "A" if team == away else None)
    if s2 is None:
        return label
    return pre + M.hdc_label(team, c, s2)


def fix_hdc(obj, home=None, away=None):
    """遞歸改寫任何讓球市場嘅 label 成「隊名 讓/受讓 X(馬會盤 …)」。"""
    if isinstance(obj, list):
        for x in obj:
            fix_hdc(x, home, away)
        return obj
    if not isinstance(obj, dict):
        return obj
    h = obj.get("home") or home
    a = obj.get("away") or away
    lbl = obj.get("label")
    if isinstance(lbl, str):
        is_hdc = (obj.get("code") == "HDC" or obj.get("market") == "讓球"
                  or lbl.startswith("讓球 "))
        if is_hdc:
            obj["label"] = _fix_one(lbl, h, a, obj.get("side"),
                                    obj.get("condition"))
    for k in ("from", "to"):
        if isinstance(obj.get(k), str) and obj[k].startswith("讓球 "):
            obj[k] = _fix_one(obj[k], h, a)
    for v in obj.values():
        if isinstance(v, (dict, list)):
            fix_hdc(v, h, a)
    return obj


def main():
    preds = json.load(open(os.path.join(HERE, "predictions.json"), encoding="utf-8"))
    lp = os.path.join(HERE, "sim_ledger.json")
    led = json.load(open(lp, encoding="utf-8")) if os.path.exists(lp) else {"bets": [], "log": []}

    for r in preds:
        try:
            r["dist"] = dists(r)
        except Exception as e:
            r["dist"] = None
            r["dist_error"] = str(e)
        # 瘦身:候選只留 EV 頭 12 個
        r["candidates"] = sorted(r.get("candidates") or [],
                                 key=lambda c: -c["ev"])[:12]

    # 把三段預測記錄掛返落每一場
    watch = led.get("watch", {})
    for r in preds:
        w = watch.get(str(r["match_id"]))
        r["stages"] = (w or {}).get("stages", [])
    # 只出現喺 watch 但今次冇跑到嘅場,也要保留(例如已過 T-5)
    have = {str(r["match_id"]) for r in preds}
    for mid, w in watch.items():
        if mid in have or not w.get("stages"):
            continue
        last = w["stages"][-1]
        preds.append({
            "match_id": mid, "home": w["home"], "away": w["away"],
            "home_en": w.get("home_en"), "away_en": w.get("away_en"),
            "league": w["league"], "kickoff_hkt": w["kickoff"],
            "venue": w.get("venue"), "venue_city": w.get("venue_city"),
            "stage": last["stage"], "conviction": last.get("conviction"),
            "final": last.get("final"), "open": last.get("open"),
            "now": last.get("now"), "movement": last.get("movement"),
            "adjustments": last.get("adjustments") or [],
            "mults": last.get("mults"), "outcome": last.get("outcome"),
            "candidates": [], "pick": last.get("pick") if last.get("can_bet") else None,
            "no_bet_reason": last.get("no_bet_reason"),
            "stages": w["stages"], "archived": True, "dist": None,
        })
    # 已完結嘅賽事唔擺喺面板(開賽 + DONE_MIN 分鐘後當完場)。
    # 記錄仍然留喺 predictions.json / sim_ledger.json,只係唔顯示。
    _now = dt.datetime.now(HKT)
    def _ended(r):
        try:
            ko = dt.datetime.fromisoformat(str(r.get("kickoff_hkt")))
        except Exception:
            return False
        if ko.tzinfo is None:
            ko = ko.replace(tzinfo=HKT)
        return (_now - ko).total_seconds() / 60.0 >= DONE_MIN
    n_all = len(preds)
    preds = [r for r in preds if not _ended(r)]
    n_hidden = n_all - len(preds)
    preds.sort(key=lambda r: r["kickoff_hkt"])

    # 純預測摘要 —— 每場都有(包括 archived),同有冇注完全無關
    n_fc = 0
    for r in preds:
        try:
            r["fc"] = forecast(r)
            n_fc += 1 if r["fc"] else 0
        except Exception:
            r["fc"] = None

    bets = led.get("bets", [])
    shadow_bets = led.get("shadow_bets", [])
    pend = [b for b in bets if b["status"] == "PENDING"]
    done = [b for b in bets if b["status"] == "SETTLED"]
    bank = led.get("bankroll", P.BANKROLL)
    S = led.get("stats") or {}
    _STK = K.stage()
    _NTF = {"last_sent": None, "n_bets": 0, "n_settled": 0,
            "n_queue": 0, "n_sweeps": 0, "last_sweep": None}
    _nf = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "notify_state.json")
    if os.path.exists(_nf):
        try:
            with open(_nf, encoding="utf-8") as _f:
                _ns = json.load(_f)
            _NTF = {"last_sent": _ns.get("last_sent"),
                    "n_bets": len(_ns.get("bets") or []),
                    "n_settled": len(_ns.get("settled") or []),
                    "n_queue": len(_ns.get("queue") or []),
                    "n_sweeps": len(_ns.get("sweeps") or []),
                    "last_sweep": (_ns.get("sweeps") or [None])[-1]}
        except Exception:
            pass
    ledger = {
        "bankroll": bank,
        "bets": sorted(bets, key=lambda b: b["kickoff"]),
        "shadow_bets": sorted(shadow_bets, key=lambda b: b.get("kickoff") or ""),
        "log": led.get("log", [])[-30:],
        "stats": {
            "n_pending": len(pend),
            "n_voided": sum(1 for b in bets if b["status"] == "VOIDED"),
            "n_settled": len(done),
            "open_stake": sum(b["stake"] for b in pend),
            "open_pct": (sum(b["stake"] for b in pend) / bank) if bank else 0,
            "pnl": sum(b.get("pnl") or 0 for b in done),
            "turnover": sum(b["stake"] for b in done),
            "roi": (sum(b.get("pnl") or 0 for b in done)
                    / sum(b["stake"] for b in done)) if done else None,
            "n_won": sum(1 for b in done if (b.get("pnl") or 0) > 0),
            "n_lost": sum(1 for b in done if (b.get("pnl") or 0) < 0),
            "n_decided": S.get("n_decided", 0),
            "hits": S.get("hits", 0),
            "hit_rate": S.get("hit_rate"),
            "equity": S.get("equity", bank),
            "by_market": S.get("by_market", {}),
            "curve": S.get("curve", []),
            "res_counts": {k: sum(1 for b in done if b.get("result") == k)
                           for k in ("Won", "Half Won", "Refunded",
                                     "Half Lost", "Lost")},
            "daily_cap": bank * 1.00,
            "open_cap": bank * 1.00,
            "single_cap_pct": _STK["cap"],
            "conf_floor": P.CONF_FLOOR,
            "staking": _STK,
            "notify": _NTF,
            "bet_stage": "T-5",
            "n_watch": len(watch),
            "n_stage_preds": sum(len(w.get("stages") or []) for w in watch.values()),
        },
        "shadow_stats": led.get("shadow_stats") or {},
    }

    # 純預測準繩度記分板(accuracy.py 出,冇就當冇)
    acc = None
    _ap = os.path.join(HERE, "accuracy.json")
    if os.path.exists(_ap):
        try:
            with open(_ap, encoding="utf-8") as _f:
                acc = json.load(_f)
        except Exception:
            acc = None
    acc_history = acc
    _ahp = os.path.join(HERE, "accuracy_history.json")
    if os.path.exists(_ahp):
        try:
            with open(_ahp, encoding="utf-8") as _f:
                acc_history = json.load(_f)
        except Exception:
            acc_history = acc
    prediction_history = build_prediction_history(
        sync_prediction_archive(watch), bets, acc_history
    )

    out = {
        "generated_at": dt.datetime.now(HKT).isoformat(timespec="seconds"),
        "n_hidden_ended": n_hidden,
        "matches": fix_hdc(preds),
        "ledger": fix_hdc(ledger),
        "accuracy": acc,
        "prediction_history": prediction_history,
    }
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    json.dump(out, open(OUT, "w", encoding="utf-8"), ensure_ascii=False,
              separators=(",", ":"))
    kb = os.path.getsize(OUT) / 1024
    _an = f" · 準繩度 {acc['n_matches']} 場" if acc else ""
    print(f"{len(preds)} 場(隱藏已完結 {n_hidden} 場) · 純預測 {n_fc} 場{_an} · "
          f"{len(bets)} 注 → {OUT} ({kb:.0f} KB)")


if __name__ == "__main__":
    main()
