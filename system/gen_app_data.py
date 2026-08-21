"""把 predictions.json + sim_ledger.json 打包成儀表板用嘅 data.json。

唔會叫任何外部 API — 純粹由已有輸出重算分佈。
"""
import json
import hashlib
import math
import re
import os
import sys
import datetime as dt
import tempfile
import argparse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import model as M
import predict as P
from record_picks import PREDICTION_ERA, PREDICTION_SCHEMA_VERSION
from condition_portfolio import FIXED_STAKE, PORTFOLIO, STARTING_BANKROLL, STRATEGY

OUT = os.environ.get(
    "FOOTBREAK_DASHBOARD_DATA",
    os.path.join(os.path.dirname(HERE), "hkjc-dashboard", "data.json"),
)
LEDGER_PATH = os.environ.get("FOOTBREAK_LEDGER_PATH", os.path.join(HERE, "sim_ledger.json"))
PROBABILITY_EVIDENCE_PATH = os.environ.get(
    "FOOTBREAK_PROBABILITY_EVIDENCE_PATH",
    os.path.join(HERE, "footbreak_probability_evidence.json"),
)
HKT = dt.timezone(dt.timedelta(hours=8))
DONE_MIN = 130.0        # 開賽後幾分鐘當完場,同 settle.py 一致
HISTORY_DATA_URL = "history.json"
HISTORY_ARTIFACT_SCHEMA = "footbreak-history-v1"


def probability_evidence_public() -> dict:
    """Expose aggregate evidence health only—never fixture/provider identities."""
    try:
        payload = json.loads(open(PROBABILITY_EVIDENCE_PATH, encoding="utf-8").read())
    except (OSError, ValueError, TypeError):
        return {"available": False, "reason": "artifact_missing_or_malformed"}
    if not isinstance(payload, dict) or payload.get("schema_version") != 2 or payload.get("system") != "footbreak":
        return {"available": False, "reason": "artifact_schema_or_system_invalid"}
    coverage = payload.get("coverage")
    if not isinstance(coverage, dict):
        return {"available": False, "reason": "artifact_coverage_invalid"}
    return {
        "available": True, "generated_at": payload.get("generated_at"),
        "source_boundary_at": payload.get("source_boundary_at"),
        "source": payload.get("source"),
        "max_rows": payload.get("max_rows"),
        "coverage": {
            "accepted_rows": coverage.get("accepted_rows"),
            "by_market": coverage.get("by_market") or {},
            "by_path": coverage.get("by_path") or {},
            "excluded": coverage.get("excluded") or {},
        },
    }


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
    stats = _prediction_history_stats(comparable_rows, include_granular=True)
    stats["all_history_audit"] = _prediction_history_stats(rows, include_granular=False)
    stats["scope"] = {
        "model_version": PREDICTION_ERA,
        "schema_version": PREDICTION_SCHEMA_VERSION,
        "rows": len(comparable_rows),
        "all_history_rows": len(rows),
        "description": "目前模型版本；全歷史保留於 all_history_audit。",
    }
    return {"rows": rows, "stats": stats}


def _prediction_history_stats(rows, *, include_granular=True):
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
    from analysis.granular_conditions import mine

    output = {
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
        "learning_status": "collecting_market_level_samples",
    }
    # Never calculate a second mixed-era report inside all_history_audit.
    if include_granular:
        output["granular_conditions"] = mine(rows, system="footbreak")
    return output


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


def _public_bet(bet):
    """Project a condition bet for the dashboard without settlement internals."""
    visible = {
        "bet_id", "portfolio", "strategy", "match_id", "league", "home", "away",
        "kickoff", "fixture_id", "league_id", "market_label", "selected_line",
        "selected_role", "label", "odds", "stake", "stage", "first_stage", "status",
        "simulation_only", "real_betting_enabled", "created_at", "condition_accuracy",
        "condition_hits", "condition_decided", "condition_badge", "condition_odds_tier",
        "code", "market", "side", "line", "condition", "strategy_name",
        "frozen_condition_signature", "condition_number", "frozen_condition_definition",
        "frozen_historical_evidence", "wilson_admission",
        "evidence_version", "evidence_hash",
        "result", "pnl", "settled_at", "score", "settlement_source", "void_reason",
    }
    return {key: value for key, value in bet.items() if key in visible}


def _public_observation(row):
    """Project a non-bet Wilson match without exposing its provider payload."""
    visible = {
        "match_id", "league", "home", "away", "kickoff", "market", "market_label",
        "code", "side", "line", "selected_role", "selected_line", "odds", "stage",
        "created_at", "condition_number", "bet_status", "no_bet_reason",
        "frozen_condition_signature", "wilson_admission", "evidence_version", "evidence_hash",
        # Evidence-only rows must still expose their own auditable settlement
        # state; these values never belong to the simulated bet/PnL stream.
        "status", "result", "settled_at", "settlement_source", "void_reason",
    }
    return {key: value for key, value in row.items() if key in visible}


def _public_crown_execution_bet(row):
    """Expose cross-book outcomes without fixture/provider identifiers."""
    visible = {
        "bet_id", "portfolio", "strategy", "league", "home", "away", "kickoff",
        "market", "market_label", "side", "line", "selected_role", "selected_line",
        "hkjc_signal_odds", "hkjc_signal_observed_at", "crown_execution_odds",
        "crown_execution_observed_at", "stake", "status", "condition_number",
        "evidence_version", "wilson_admission", "result", "pnl", "settled_at",
        "score", "simulation_only",
    }
    return {key: value for key, value in row.items() if key in visible}


def _public_bilateral_decisions(namespace):
    from analysis.bilateral_decision import public_decision
    return [public_decision(row) for row in (namespace.get("decisions") or [])
            if isinstance(row, dict)]


def _wilson_match_projection(row, *, bet_status):
    """Use persisted raw Wilson arithmetic; the dashboard must never rederive it."""
    arithmetic = row.get("wilson_admission") if isinstance(row.get("wilson_admission"), dict) else {}
    display = arithmetic.get("display") if isinstance(arithmetic.get("display"), dict) else {}
    return {
        # Formal dashboard/TG labels must be backed by a persisted native
        # admission, never by a structural discovery-card resemblance.
        "match_class": "authoritative_admission",
        "authoritative": True,
        "notification_eligible": True,
        "condition_number": row.get("condition_number"),
        "market": row.get("market") or row.get("code"),
        "market_label": row.get("market_label"),
        "selected_role": row.get("selected_role"),
        "selected_line": row.get("selected_line", row.get("line")),
        "odds": arithmetic.get("actual_decimal_odds_raw", row.get("odds")),
        "minimum_required_odds": arithmetic.get("minimum_acceptable_odds_raw"),
        # Preserve both the unrounded admission value and its authoritative
        # stored display form.  Browser code must not round/recompute a gate.
        "minimum_required_odds_display": display.get("minimum_acceptable_odds"),
        "evidence_version": row.get("evidence_version"),
        "evidence_hash": row.get("evidence_hash"),
        "bet_status": bet_status,
        "no_bet_reason": row.get("no_bet_reason") if bet_status != "BET" else None,
    }


def _history_version(prediction_history):
    encoded = json.dumps(
        prediction_history, ensure_ascii=False, separators=(",", ":"), sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:20]


def _write_json_atomic(path, payload):
    """Atomically replace a public JSON artifact and retain nginx readability."""
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".dashboard-data-", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o644)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _write_ledger_atomic(path, payload):
    """Persist a local evidence migration without exposing it as dashboard data."""
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".sim-ledger-", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _public_log_entries(rows):
    """Never leak retained legacy/shadow state while reset is pending."""
    output = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        text = json.dumps(row, ensure_ascii=False, sort_keys=True)
        if "影子" in text or "shadow" in text.lower():
            continue
        output.append(row)
    return output[-30:]


def write_empty_bootstrap(out_path):
    """Publish a provider-free first-install dashboard payload.

    This deliberately does not inspect predictions, ledgers, archives, or
    provider caches.  It is only the setup-time escape hatch for a new host
    before its first manual/scheduled local pass.
    """
    generated_at = dt.datetime.now(HKT).isoformat(timespec="seconds")
    prediction_history = {
        "rows": [],
        "stats": _prediction_history_stats([], include_granular=True),
    }
    history_version = _history_version(prediction_history)
    ledger = {
        "bankroll": STARTING_BANKROLL,
        "bets": [],
        "log": [],
        "independent_validation": {
            "schema_version": None,
            "validation_started_at": None,
            "activation_at": None,
            "cutover_at": None,
            "display_name": "Wilson 測試攻略",
            "conditions": {},
            "condition_order": [],
            "observations": [],
            "retired_v1": {},
            "historical_discovery_archive": {},
        },
        "probability_research": {
            "schema_version": None,
            "activation_at": None,
            "cutover_at": None,
            "mode": None,
            "stats": {},
            "evidence_artifact": {
                "available": False,
                "reason": "not_yet_run",
            },
        },
        "crown_execution_test": {
            "display_name": "足破×皇冠執行測試倉（模擬）",
            "bets": [], "stats": {}, "rejections": {},
        },
        "stats": {
            "portfolio": PORTFOLIO,
            "strategy": STRATEGY,
            "starting_bankroll": STARTING_BANKROLL,
            "fixed_stake": FIXED_STAKE,
            "n_pending": 0,
            "n_voided": 0,
            "n_settled": 0,
            "open_stake": 0,
            "open_pct": 0,
            "pnl": 0,
            "turnover": 0,
            "roi": None,
            "n_won": 0,
            "n_lost": 0,
            "n_decided": 0,
            "hits": 0,
            "hit_rate": None,
            "wilson95": None,
            "pushes": 0,
            "cash": STARTING_BANKROLL,
            "equity": STARTING_BANKROLL,
            "by_market": {},
            "odds_tiers": {
                "scope": "active_wilson_bets_and_results_only",
                "system": "footbreak",
                "tiers": [],
                "excluded_diagnostics": {},
            },
            "curve": [],
            "res_counts": {},
            "notify": {
                "last_sent": None,
                "n_bets": 0,
                "n_settled": 0,
                "n_queue": 0,
                "n_sweeps": 0,
                "last_sweep": None,
            },
            "bet_stage": "T-5",
            "rules": {
                "stake": FIXED_STAKE,
                "historical_hit_rate": "Wilson 95% 下限 ≥ 實際損益平衡率 +3pp",
                "minimum_decided": 50,
                "new_t5_only": True,
                "fixture_stake_cap": 1500,
                "fixture_market_cap": 3,
            },
            "n_watch": 0,
            "n_stage_preds": 0,
        },
    }
    history_artifact = {
        "schema_version": HISTORY_ARTIFACT_SCHEMA,
        "generated_at": generated_at,
        "history_data_version": history_version,
        "prediction_history": prediction_history,
    }
    payload = {
        "generated_at": generated_at,
        "n_hidden_ended": 0,
        "matches": [],
        "ledger": ledger,
        "accuracy": None,
        "prediction_history": {"stats": prediction_history["stats"]},
        "history_data_url": HISTORY_DATA_URL,
        "history_data_version": history_version,
        "dashboard_status": {
            "state": "not_yet_run",
            "message": "系統尚未執行首次掃描；暫時未有賽事及預測紀錄。",
        },
    }
    # Keep the sidecar/data ordering identical to normal publication so the
    # browser can never see a bootstrap main payload without its referenced
    # history artifact.
    _write_json_atomic(
        os.path.join(os.path.dirname(os.path.abspath(out_path)), HISTORY_DATA_URL),
        history_artifact,
    )
    _write_json_atomic(out_path, payload)
    return payload


def main(out_path=None):
    out_path = out_path or OUT
    with open(os.path.join(HERE, "predictions.json"), encoding="utf-8") as handle:
        preds = json.load(handle)
    lp = LEDGER_PATH
    if os.path.exists(lp):
        with open(lp, encoding="utf-8") as handle:
            led = json.load(handle)
    else:
        led = {"bets": [], "log": []}

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

    # Public payloads must never display legacy main/shadow rows, including
    # before the guarded reset. Only this isolated fixed-stake portfolio is
    # observable by the dashboard.
    bets = [
        bet for bet in (led.get("bets") or [])
        if isinstance(bet, dict)
        and bet.get("portfolio") == PORTFOLIO
        and bet.get("strategy") == STRATEGY
    ]
    pend = [bet for bet in bets if bet.get("status") == "PENDING"]
    done = [bet for bet in bets if bet.get("status") == "SETTLED"]
    bank = STARTING_BANKROLL
    # Prefer the self-contained Wilson metrics so a freshly migrated ledger
    # renders correctly even before the next scheduler/settlement recompute.
    S = ((led.get("wilson_validation") or {}).get("stats") or led.get("stats") or {})
    _NTF = {"last_sent": None, "n_bets": 0, "n_settled": 0,
            "n_queue": 0, "n_sweeps": 0, "last_sweep": None}
    _nf = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "notify_state.json")
    if os.path.exists(_nf):
        try:
            with open(_nf, encoding="utf-8") as _f:
                _ns = json.load(_f)
            _NTF = {"last_sent": _ns.get("last_sent"),
                    "n_bets": len(_ns.get("condition_simulation_bets") or []),
                    "n_settled": len(_ns.get("settled") or []),
                    "n_queue": len(_ns.get("queue") or []),
                    "n_sweeps": len(_ns.get("sweeps") or []),
                    "last_sweep": (_ns.get("sweeps") or [None])[-1]}
        except Exception:
            pass
    ledger = {
        "bankroll": bank,
        "bets": [_public_bet(bet) for bet in sorted(bets, key=lambda b: b.get("kickoff") or "")],
        "log": _public_log_entries(led.get("log", [])),
        # Compatibility key consumed by the existing dashboard; it projects
        # only Wilson data. v1 stays in its clearly labelled archival snapshot.
        "independent_validation": {
            "schema_version": (led.get("wilson_validation") or {}).get("schema_version"),
            "validation_started_at": (led.get("wilson_validation") or {}).get("activation_at"),
            "activation_at": (led.get("wilson_validation") or {}).get("activation_at"),
            "cutover_at": (led.get("wilson_validation") or {}).get("cutover_at"),
            "display_name": "Wilson 測試攻略",
            "conditions": (led.get("wilson_validation") or {}).get("conditions") or {},
            "condition_order": (led.get("wilson_validation") or {}).get("condition_order") or [],
            "rollover": {
                "batch_size": 20,
                "conditions": {
                    signature: {
                        "condition_number": row.get("condition_number"),
                        "active_evidence": row.get("active_evidence") or {},
                        "last_merged_batch": (row.get("rollover_audit") or [])[-1] if isinstance(row.get("rollover_audit"), list) and row.get("rollover_audit") else None,
                        "pending_progress": row.get("pending_rollover_progress") or {"eligible_decided": 0, "required": 20, "display": "0/20"},
                    }
                    for signature, row in ((led.get("wilson_validation") or {}).get("conditions") or {}).items()
                    if isinstance(row, dict)
                },
            },
            # These matched no-bet rows remain outside ``bets``: they never
            # enter stake/PnL/ROI, while their separately settled outcome can
            # advance only the frozen condition-evidence rollover.
            "observations": [
                _public_observation(row)
                for row in ((led.get("wilson_validation") or {}).get("observations") or [])
                if isinstance(row, dict) and row.get("formal_bet") is False
            ],
            "retired_v1": (led.get("wilson_validation") or {}).get("retired_v1") or {},
            "historical_discovery_archive": (led.get("wilson_validation") or {}).get("retired_v1") or {},
        },
        # A separate research projection.  It is deliberately not a bet
        # portfolio, cannot alter v1 stats/PnL, and carries no notification
        # eligibility into the browser.
        "probability_research": {
            "schema_version": (led.get("footbreak_probability_research") or {}).get("schema_version"),
            "activation_at": (led.get("footbreak_probability_research") or {}).get("activation_at"),
            "cutover_at": (led.get("footbreak_probability_research") or {}).get("cutover_at"),
            "mode": (led.get("footbreak_probability_research") or {}).get("mode"),
            "stats": (led.get("footbreak_probability_research") or {}).get("stats") or {},
            "evidence_artifact": probability_evidence_public(),
        },
        "crown_execution_test": {
            "display_name": "足破×皇冠執行測試倉（模擬）",
            "bets": [
                _public_crown_execution_bet(row)
                for row in ((led.get("footbreak_crown_execution_test") or {}).get("bets") or [])
                if isinstance(row, dict)
                and row.get("portfolio") == "footbreak_crown_execution_test"
            ],
            "stats": {
                key: value for key, value in
                (((led.get("footbreak_crown_execution_test") or {}).get("stats") or {}).items())
                if key not in {"fixture_identity", "source", "provider", "audit"}
            },
            "rejections": (
                ((led.get("footbreak_crown_execution_test") or {}).get("stats") or {})
                .get("rejections") or {}
            ),
            "decisions": _public_bilateral_decisions(
                (led.get("footbreak_crown_execution_test") or {})
            ),
        },
        "stats": {
            "portfolio": PORTFOLIO,
            "strategy": STRATEGY,
            "starting_bankroll": STARTING_BANKROLL,
            "fixed_stake": FIXED_STAKE,
            "n_pending": S.get("n_pending", len(pend)),
            "n_voided": S.get("n_voided", sum(1 for bet in bets if bet.get("status") == "VOIDED")),
            "n_settled": S.get("n_settled", len(done)),
            "open_stake": S.get("open_stake", sum(float(bet.get("stake") or 0) for bet in pend)),
            "open_pct": S.get("open_pct", (sum(float(bet.get("stake") or 0) for bet in pend) / bank) if bank else 0),
            "pnl": S.get("pnl", sum(float(bet.get("pnl") or 0) for bet in done)),
            "turnover": S.get("turnover", sum(float(bet.get("stake") or 0) for bet in done)),
            "roi": S.get("roi"),
            "n_won": sum(1 for bet in done if bet.get("result") in ("Won", "Half Won")),
            "n_lost": sum(1 for bet in done if bet.get("result") in ("Lost", "Half Lost")),
            "n_decided": S.get("n_decided", 0),
            "hits": S.get("hits", 0),
            "hit_rate": S.get("hit_rate"),
            "wilson95": S.get("wilson95"),
            "pushes": S.get("pushes", 0),
            "cash": S.get("cash", bank),
            "equity": S.get("equity", bank),
            "by_market": S.get("by_market", {}),
            # Wilson prospective rows only.  The dashboard never recomputes
            # admission evidence from historical discovery or legacy bets.
            "odds_tiers": S.get("odds_tiers", {
                "scope": "active_wilson_bets_and_results_only",
                "system": "footbreak",
                "tiers": [],
                "excluded_diagnostics": {},
            }),
            "curve": S.get("curve", []),
            "res_counts": {k: sum(1 for b in done if b.get("result") == k)
                           for k in ("Won", "Half Won", "Refunded",
                                     "Half Lost", "Lost")},
            "notify": _NTF,
            "bet_stage": "T-5",
            "rules": {
                "stake": FIXED_STAKE,
                "historical_hit_rate": "Wilson 95% 下限 ≥ 實際損益平衡率 +3pp",
                "minimum_decided": 50,
                "new_t5_only": True,
                "fixture_stake_cap": 1500,
                "fixture_market_cap": 3,
            },
            "n_watch": len(watch),
            "n_stage_preds": sum(len(w.get("stages") or []) for w in watch.values()),
        },
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
    # The historical miner deliberately stays pure and may rerank its output
    # on every run.  Overlay each card with its persisted, exact-condition
    # Wilson evidence so the displayed total and downstream matcher cannot
    # fall back to the pre-migration discovery counts.
    from analysis.wilson_validation import (
        project_dashboard_research_matches,
        project_granular_ranking_evidence,
    )
    raw_ranking = (
        (prediction_history.get("stats") or {})
        .get("granular_conditions", {}).get("ranking") or []
    )
    projected_ranking = project_granular_ranking_evidence(
        led, "footbreak", raw_ranking,
        now=dt.datetime.now(HKT).isoformat(timespec="seconds"),
    )
    _write_ledger_atomic(lp, led)
    if isinstance((prediction_history.get("stats") or {}).get("granular_conditions"), dict):
        prediction_history["stats"]["granular_conditions"]["ranking"] = projected_ranking
    # Match explanations come solely from the frozen T-5 admission decision.
    # A below-minimum quote is a real Wilson match, but not a portfolio bet.
    observations = [
        row for row in ((led.get("wilson_validation") or {}).get("observations") or [])
        if isinstance(row, dict) and row.get("formal_bet") is False
    ]
    by_fixture = {}
    for bet in bets:
        match_id = str(bet.get("match_id") or "")
        if match_id:
            by_fixture.setdefault(match_id, []).append(_wilson_match_projection(bet, bet_status="BET"))
    for row in observations:
        match_id = str(row.get("match_id") or "")
        if match_id:
            by_fixture.setdefault(match_id, []).append(_wilson_match_projection(row, bet_status="NO_BET_LOW_ODDS"))
    for values in by_fixture.values():
        values.sort(key=lambda item: (
            int(item.get("condition_number") or 10**9),
            str(item.get("market") or ""), str(item.get("selected_role") or ""),
        ))
    # Upcoming cards receive only matches evaluated at their currently
    # persisted stage.  T-30 intentionally cannot observe a later T-5 row.
    from analysis.granular_conditions import match_upcoming
    watch_rows = [
        {
            "match_id": str(mid), "stage": stage.get("stage"),
            "kickoff": item.get("kickoff"), "predicted_at": stage.get("ts"),
            "market_predictions": stage.get("market_predictions") or [],
        }
        for mid, item in watch.items() if isinstance(item, dict)
        for stage in (item.get("stages") or []) if isinstance(stage, dict)
    ]
    ranking = projected_ranking
    matches_by_stage = {
        stage: match_upcoming(watch_rows, ranking, system="footbreak", decision_stage=stage)
        for stage in ("T-30", "T-5")
    }
    for item in preds:
        stage = str(item.get("stage") or "")
        item["condition_matches"] = project_dashboard_research_matches(
            matches_by_stage.get(stage, {}).get(str(item.get("match_id")), [])
        )
        item["wilson_matches"] = by_fixture.get(str(item.get("match_id")), [])

    history_version = _history_version(prediction_history)
    history_artifact = {
        "schema_version": HISTORY_ARTIFACT_SCHEMA,
        "generated_at": dt.datetime.now(HKT).isoformat(timespec="seconds"),
        "history_data_version": history_version,
        "prediction_history": prediction_history,
    }
    out = {
        "generated_at": dt.datetime.now(HKT).isoformat(timespec="seconds"),
        "n_hidden_ended": n_hidden,
        "matches": fix_hdc(preds),
        "ledger": fix_hdc(ledger),
        "accuracy": acc,
        # Boot keeps aggregates only. Full records load only when the user
        # opens 預測紀錄, avoiding a heavy JSON request on every refresh.
        "prediction_history": {"stats": prediction_history.get("stats") or {}},
        "history_data_url": HISTORY_DATA_URL,
        "history_data_version": history_version,
    }
    # Sidecar first, then data.json. The browser compares the content marker,
    # so a brief two-file replacement cannot show mixed history generations.
    _write_json_atomic(os.path.join(os.path.dirname(out_path), HISTORY_DATA_URL), history_artifact)
    _write_json_atomic(out_path, out)
    kb = os.path.getsize(out_path) / 1024
    _an = f" · 準繩度 {acc['n_matches']} 場" if acc else ""
    print(f"{len(preds)} 場(隱藏已完結 {n_hidden} 場) · 純預測 {n_fc} 場{_an} · "
          f"{len(bets)} 注 → {out_path} ({kb:.0f} KB)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--bootstrap-empty",
        action="store_true",
        help="write a provider-free first-install empty dashboard payload",
    )
    parser.add_argument("--out", default=OUT, help="dashboard data.json output path")
    args = parser.parse_args()
    if args.bootstrap_empty:
        write_empty_bootstrap(args.out)
    else:
        main(args.out)
