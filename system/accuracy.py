"""純預測準繩度記分板。

同「注單盈虧」完全分開 —— 呢度唔理有冇落注、唔理 EV,
淨係問一件事:**模型估嘅賽果,準唔準?**

資料源:sim_ledger.json["watch"] 已經追蹤咗每一場、每一個階段
(首預 / T-30 / T-5)嘅 `final` 參數。呢度由參數重砌完整分佈,
再同 OpticOdds 真實賽果對數,計:

  · 1X2  ── 命中率、Brier(多類)、log loss、對比「一律揀盤口熱門」
  · 大細  ── 2.5 球線命中率同 Brier
  · 兩隊入球 ── 命中率同 Brier
  · 角球  ── 9.5 線命中率同 Brier
  · 準確比分 ── 頭一名 / 頭五名命中率
  · 入球 / 角球總數 ── 平均絕對誤差、八成區間覆蓋率

再按階段同信念分桶,睇下「信念高」係咪真係「估得準」。
校準曲線用 1X2 每一個邊嘅預測機率分箱。

用法:
    python3 accuracy.py               # 結算新完場嘅,更新 accuracy.json
    python3 accuracy.py --no-fetch    # 唔叫 API,淨係用已快取賽果重算
    python3 accuracy.py --print       # 出人睇嘅報告
"""
import json
import math
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from analysis.learning_store import LearningStore

import model as M
import settle as S

HKT = timezone(timedelta(hours=8))
HERE = os.path.dirname(os.path.abspath(__file__))
LEDGER = os.path.join(HERE, "sim_ledger.json")
OUT = os.path.join(HERE, "accuracy.json")
HISTORY_OUT = os.path.join(HERE, "accuracy_history.json")
GRANULAR_RANKING_OUT = os.path.join(HERE, "granular_condition_ranking.json")
PREDICTION_ARCHIVE = os.environ.get(
    "FOOTBREAK_PREDICTION_ARCHIVE_PATH",
    os.path.join(HERE, "prediction_history_archive.json"),
)

SETTLE_AFTER_MIN = 130
STAGES = ["首預", "T-30", "T-5"]
PREDICTION_ERA = "2026-08-10-market-learning-v2"
PREDICTION_SCHEMA_VERSION = 2
NON_RESULT_STATUS_MARKERS = (
    "SUSPEND", "POSTPON", "CANCEL", "ABANDON", "VOID", "REFUND",
)
SCOREABLE_MARKETS = {"HDC", "HIL", "CHL"}
CONF_BINS = [(0, 45), (45, 52), (52, 58), (58, 64), (64, 200)]
CONF_LBL = ["<45", "45–52", "52–58", "58–64", "≥64"]


def scoreable_market_predictions(value):
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


def has_scoreable_market_prediction(stage):
    return bool(scoreable_market_predictions(stage.get("market_predictions")))


def _atomic_json(path, payload):
    directory = os.path.dirname(path)
    fd, temp_path = tempfile.mkstemp(prefix=".accuracy.", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf8") as handle:
            json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    finally:
        if os.path.exists(temp_path):
            os.unlink(temp_path)


# ─────────────────────────────────────────── 由參數重砌分佈

def rebuild(final, now):
    """由 stage 嘅 final/now 參數重砌 1X2、入球、角球分佈。"""
    if not final or not final.get("lh") or not final.get("la"):
        return None
    phi = (now or {}).get("phi")
    mu = final.get("mu")
    if mu and phi:
        phi = M.cap_phi(mu, phi)
    mat = M.score_matrix(final["lh"], final["la"], final.get("rho") or 0.0)
    gp = M.goals_pmf(mat)
    cp = M.corner_pmf(mu, phi) if (mu and phi) else None

    ph = pdr = pa = btts = 0.0
    tops = []
    for i, row in enumerate(mat):
        for j, p in enumerate(row):
            if i > j:
                ph += p
            elif i == j:
                pdr += p
            else:
                pa += p
            if i and j:
                btts += p
            tops.append((p, i, j))
    tops.sort(reverse=True)
    return {
        "p": [ph, pdr, pa],
        "btts": btts,
        "goals": gp,
        "corners": cp,
        "tops": [(i, j) for _, i, j in tops[:5]],
        "lh": final["lh"], "la": final["la"], "mu": mu,
    }


def ou(dist, line):
    if not dist:
        return None
    return sum(dist[int(line) + 1:])


def band(dist, lo=0.10, hi=0.90):
    if not dist:
        return None
    c, q = 0.0, {lo: None, hi: None}
    for i, p in enumerate(dist):
        c += p
        for k in q:
            if q[k] is None and c >= k:
                q[k] = i
    n = len(dist) - 1
    return (q[lo] if q[lo] is not None else 0, q[hi] if q[hi] is not None else n)


# ─────────────────────────────────────────── 逐場逐階段評分

def score_stage(st, res):
    """一個階段預測 vs 一個賽果 → 一份評分 dict(冇得計嘅欄位留 None)。"""
    d = rebuild(st.get("final"), st.get("now"))
    market_predictions = scoreable_market_predictions(st.get("market_predictions"))
    gh, ga = res["goals_home"], res["goals_away"]
    tot = gh + ga
    ct = res.get("corners_total")
    normalized_res = {**res, "goals_total": res.get("goals_total", tot)}
    act = 0 if gh > ga else (1 if gh == ga else 2)

    # 舊快照有時只保存咗正式市場方向，冇保存完整 Poisson 參數。
    # 讓球／入球大細／角球仍然可以用真實賽果獨立結算，唔應該因為
    # 缺 final/now 而整個階段消失。
    if not d:
        return {
            "stage": st.get("stage"),
            "predicted_at": st.get("ts"),
            "conf": st.get("conviction"),
            "wdl_pick": None,
            "wdl_act": act,
            "wdl_hit": None,
            "score_act": f"{gh}-{ga}",
            "goals_act": tot,
            "corners_act": ct,
            "market_predictions": market_predictions,
            "market_grades": score_market_predictions(
                market_predictions, normalized_res
            ),
        }

    # 1X2 ── 0 主勝 1 和 2 客勝
    p = d["p"]
    sp = sum(p) or 1.0
    p = [x / sp for x in p]
    pick = max(range(3), key=lambda i: p[i])
    brier = sum((p[i] - (1.0 if i == act else 0.0)) ** 2 for i in range(3))
    ll = -math.log(max(p[act], 1e-9))

    r = {
        "stage": st.get("stage"),
        "predicted_at": st.get("ts"),
        "conf": st.get("conviction"),
        "wdl_pick": pick, "wdl_act": act, "wdl_hit": int(pick == act),
        "wdl_p": round(p[act], 4), "wdl_pmax": round(p[pick], 4),
        "wdl_brier": round(brier, 5), "wdl_ll": round(ll, 5),
        "goals_act": tot, "goals_exp": round(d["lh"] + d["la"], 3),
        "goals_err": round(abs(d["lh"] + d["la"] - tot), 3),
        "score_act": f"{gh}-{ga}",
        "score_top": f"{d['tops'][0][0]}-{d['tops'][0][1]}",
        "score_hit1": int((gh, ga) == d["tops"][0]),
        "score_hit5": int((gh, ga) in d["tops"]),
        "market_predictions": market_predictions,
        "market_grades": score_market_predictions(
            market_predictions, normalized_res
        ),
    }

    # 大細 2.5
    o25 = ou(d["goals"], 2.5)
    if o25 is not None:
        a = 1 if tot > 2.5 else 0
        r["o25_p"] = round(o25, 4)
        r["o25_hit"] = int((o25 >= 0.5) == bool(a))
        r["o25_brier"] = round((o25 - a) ** 2, 5)

    # 兩隊入球
    a = 1 if (gh > 0 and ga > 0) else 0
    r["btts_p"] = round(d["btts"], 4)
    r["btts_hit"] = int((d["btts"] >= 0.5) == bool(a))
    r["btts_brier"] = round((d["btts"] - a) ** 2, 5)

    # 入球八成區間覆蓋
    gb = band(d["goals"])
    if gb:
        r["goals_band"] = list(gb)
        r["goals_cover"] = int(gb[0] <= tot <= gb[1])

    # 角球
    if ct is not None and d["corners"]:
        c95 = ou(d["corners"], 9.5)
        a = 1 if ct > 9.5 else 0
        r["corners_act"] = ct
        r["corners_exp"] = round(d["mu"], 2)
        r["corners_err"] = round(abs(d["mu"] - ct), 2)
        r["c95_p"] = round(c95, 4)
        r["c95_hit"] = int((c95 >= 0.5) == bool(a))
        r["c95_brier"] = round((c95 - a) ** 2, 5)
        cb = band(d["corners"])
        if cb:
            r["corners_band"] = list(cb)
            r["corners_cover"] = int(cb[0] <= ct <= cb[1])
    return r


_SETTLEMENT_TARGET = {
    "Won": 1.0, "Half Won": 0.75, "Refunded": 0.5,
    "Half Lost": 0.25, "Lost": 0.0,
}


def score_market_predictions(predictions, res):
    """按當時實際主線逐一評分；不以有冇落注或 EV 作篩選。"""
    grades = []
    for prediction in scoreable_market_predictions(predictions):
        code = prediction.get("code")
        try:
            probability = float(prediction["probability"])
            bet = {
                "code": code,
                "condition": prediction["condition"],
                "side": prediction["side"],
                "stake": 1.0,
                "odds": 2.0,
            }
            status, _ = S.settle_bet(bet, res)
        except (KeyError, TypeError, ValueError, ZeroDivisionError):
            status = None
        if not status:
            grades.append({
                **prediction,
                "grade_status": "NOT_APPLICABLE",
                "reason": "corners_result_missing" if code == "CHL" else "invalid_prediction_or_result",
            })
            continue
        target = _SETTLEMENT_TARGET[status]
        p = min(.999999, max(.000001, probability))
        grades.append({
            **prediction,
            "grade_status": "GRADED",
            "settlement": status,
            "target": target,
            "hit": None if status == "Refunded" else status in {"Won", "Half Won"},
            "brier": round((p - target) ** 2, 6),
            "log_loss": round(
                -(target * math.log(p) + (1 - target) * math.log(1 - p)), 6
            ),
        })
    return grades


def _persist_learning_result(mid, w, res, rows):
    path = os.environ.get("LEARNING_DB_PATH")
    if not path:
        return
    provenance = {
        "fixture_id": w.get("fixture_id"),
        "hkjc_match_id": mid,
        "corners_total": res.get("corners_total"),
        "raw_source": res.get("source"),
    }
    with LearningStore(path) as store:
        result = store.record_result(
            "footbreak",
            mid,
            home_score=res.get("goals_home"),
            away_score=res.get("goals_away"),
            terminal_status="finished",
            source=str(res.get("source") or "verified_result"),
            provenance=provenance,
        )
        for stage, score in rows:
            snapshot_id = stage.get("learning_snapshot_id")
            if not snapshot_id:
                continue
            if score.get("wdl_pick") is not None:
                store.record_grade(
                    snapshot_id,
                    "WDL",
                    str(score.get("wdl_pick")),
                    "GRADED",
                    {
                        "hit": score.get("wdl_hit"),
                        "brier": score.get("wdl_brier"),
                        "log_loss": score.get("wdl_ll"),
                        "actual": score.get("wdl_act"),
                    },
                    result_id=result["result_id"],
                )
            for grade in score.get("market_grades") or []:
                store.record_grade(
                    snapshot_id,
                    str(grade.get("code") or "UNKNOWN"),
                    f"{grade.get('condition')}|{grade.get('side')}",
                    str(grade.get("grade_status") or "NOT_APPLICABLE"),
                    grade,
                result_id=result["result_id"],
            )


def _persist_learning_exclusion(mid, w, stages, status, source):
    """Record an explicit verified no-contest as a terminal learning result."""
    path = os.environ.get("LEARNING_DB_PATH")
    if not path:
        return
    with LearningStore(path) as store:
        result = store.record_result(
            "footbreak",
            mid,
            terminal_status=str(status or "REFUNDED"),
            source=source,
            provenance={
                "fixture_id": w.get("fixture_id"),
                "hkjc_match_id": mid,
                "terminal_reason": "fixture_not_played",
            },
        )
        for stage in stages:
            snapshot_id = stage.get("learning_snapshot_id")
            if not snapshot_id:
                continue
            for prediction in scoreable_market_predictions(
                stage.get("market_predictions")
            ):
                store.record_grade(
                    snapshot_id,
                    str(prediction.get("code") or "UNKNOWN"),
                    f"{prediction.get('condition')}|{prediction.get('side')}",
                    "NOT_APPLICABLE",
                    {
                        **prediction,
                        "reason": "fixture_not_played",
                        "terminal_status": status,
                    },
                    result_id=result["result_id"],
                )


def _merge_watch(archived, live):
    """Merge persistent prediction snapshots with the current live board."""
    merged = dict(archived or {})
    for mid, current in (live or {}).items():
        mid = str(mid)
        previous = merged.get(mid) or {}
        row = {**previous, **current}
        stages = {}
        for stage in (previous.get("stages") or []) + (current.get("stages") or []):
            if not isinstance(stage, dict):
                continue
            key = (stage.get("prediction_era"), stage.get("stage"))
            old = stages.get(key)
            if old is None or str(stage.get("ts") or "") >= str(old.get("ts") or ""):
                stages[key] = stage
        row["stages"] = list(stages.values())
        merged[mid] = row
    return merged


def _load_archived_watch(live):
    try:
        with open(PREDICTION_ARCHIVE, encoding="utf8") as handle:
            archived = json.load(handle)
        if not isinstance(archived, dict):
            archived = {}
    except (OSError, ValueError, TypeError):
        archived = {}
    return _merge_watch(archived, live)


# ─────────────────────────────────────────── 匯總

def _agg(rows):
    """一組評分 → 匯總指標。"""
    if not rows:
        return None

    def mean(k):
        v = [r[k] for r in rows if r.get(k) is not None]
        return round(sum(v) / len(v), 4) if v else None

    def rate(k):
        v = [r[k] for r in rows if r.get(k) is not None]
        return {"n": len(v), "hit": sum(v),
                "pct": round(100.0 * sum(v) / len(v), 1)} if v else None

    result = {
        "n": len(rows),
        "wdl": rate("wdl_hit"),
        "wdl_brier": mean("wdl_brier"),
        "wdl_ll": mean("wdl_ll"),
        "wdl_conf_mean": mean("wdl_pmax"),
        "o25": rate("o25_hit"), "o25_brier": mean("o25_brier"),
        "btts": rate("btts_hit"), "btts_brier": mean("btts_brier"),
        "c95": rate("c95_hit"), "c95_brier": mean("c95_brier"),
        "score1": rate("score_hit1"), "score5": rate("score_hit5"),
        "goals_mae": mean("goals_err"), "goals_cover": rate("goals_cover"),
        "corners_mae": mean("corners_err"), "corners_cover": rate("corners_cover"),
    }
    result["by_market"] = {
        code: _market_agg(rows, code) for code in ("HDC", "HIL", "CHL")
    }
    return result


def _market_agg(rows, code=None):
    from analysis.market_statistics import market_metrics
    return market_metrics(rows, code)


def calibration(rows, nbins=5):
    """1X2 校準:每場三個邊都當一個樣本,按預測機率分箱。"""
    edges = [0, .1, .2, .35, .55, 1.01]
    bins = [{"lo": edges[i], "hi": edges[i + 1], "n": 0, "sp": 0.0, "hit": 0}
            for i in range(nbins)]
    for r in rows:
        d = r.get("_p3")
        if not d:
            continue
        for k in range(3):
            pk = d[k]
            for b in bins:
                if b["lo"] <= pk < b["hi"]:
                    b["n"] += 1
                    b["sp"] += pk
                    b["hit"] += int(k == r["wdl_act"])
                    break
    out = []
    for b in bins:
        if not b["n"]:
            continue
        out.append({
            "lbl": f"{int(b['lo'] * 100)}–{int(min(b['hi'], 1) * 100)}%",
            "n": b["n"],
            "pred": round(100 * b["sp"] / b["n"], 1),
            "act": round(100 * b["hit"] / b["n"], 1),
        })
    return out


# ─────────────────────────────────────────── 主流程

def run(fetch=True):
    with open(LEDGER, encoding="utf8") as handle:
        led = json.load(handle)
    watch = _load_archived_watch(led.get("watch") or {})
    now = datetime.now(HKT)

    eligible = []
    for mid, w in watch.items():
        if not any(
            stage.get("prediction_era") == PREDICTION_ERA
            for stage in (w.get("stages") or [])
        ):
            continue
        try:
            kickoff = datetime.strptime(w["kickoff"], "%Y-%m-%d %H:%M").replace(tzinfo=HKT)
        except Exception:
            continue
        if (now - kickoff).total_seconds() / 60 >= SETTLE_AFTER_MIN:
            eligible.append((str(mid), w, kickoff))
    official = {}
    official_statuses = {}
    if fetch and eligible:
        try:
            official = S.fetch_hkjc_results(
                {mid for mid, _, _ in eligible},
                {kickoff.strftime("%Y-%m-%d") for _, _, kickoff in eligible},
            )
            official_statuses = S.fetch_hkjc_statuses(
                {mid for mid, _, _ in eligible},
                {kickoff.strftime("%Y-%m-%d") for _, _, kickoff in eligible},
            )
        except Exception:
            official = {}
            official_statuses = {}

    scored, matches, missing_results, excluded_results = [], [], [], []
    titan_client, titan_rows, crown_titan_ids = None, [], {}
    titan_result_dates = {
        kickoff.strftime("%Y-%m-%d")
        for _, _, kickoff in eligible
    }
    for mid, w in watch.items():
        learning_stages = [
            stage for stage in (w.get("stages") or [])
            if stage.get("prediction_era") == PREDICTION_ERA
            and has_scoreable_market_prediction(stage)
        ]
        if not learning_stages:
            continue
        try:
            ko = datetime.strptime(w["kickoff"], "%Y-%m-%d %H:%M").replace(tzinfo=HKT)
        except Exception:
            continue
        if (now - ko).total_seconds() / 60 < SETTLE_AFTER_MIN:
            continue
        # Footbreak is an HKJC board.  Scores and terminal states must come
        # from HKJC; external exact-fixture services are corner-detail
        # fallbacks only and may never replace the official score.
        res = official.get(str(mid))
        # Corner enrichment is mandatory after an official score is acquired.
        if res and any(
            prediction.get("code") == "CHL"
            for stage in learning_stages
            for prediction in scoreable_market_predictions(
                stage.get("market_predictions")
            )
        ):
            try:
                res = S.merge_missing_corners(res, w.get("fixture_id"))
            except Exception:
                # 角球後備來源失敗唔可以阻止讓球／入球大細正常評分。
                pass
            if res.get("corners_total") is None and fetch:
                try:
                    import titan_results as T
                    if titan_client is None:
                        titan_client, titan_rows = T.fetch_titan_result_rows(
                            dates=titan_result_dates
                        )
                        crown_titan_ids = T.load_crown_titan_match_map()
                    titan_record = {**w, "match_id": mid}
                    if crown_titan_ids.get(str(mid)):
                        titan_record["titan_match_id"] = crown_titan_ids[str(mid)]
                    res = T.merge_titan_corners(
                        res, titan_record,
                        client=titan_client, rows=titan_rows,
                    )
                except Exception:
                    # Titan is a corner-only fallback; every other market must
                    # still grade even when that source is temporarily absent.
                    pass
        if not res:
            state = official_statuses.get(str(mid)) or {}
            status = str(state.get("status") or "").upper()
            refund = bool(state.get("refund_pools") or state.get("payout_refund_pools"))
            if refund or any(marker in status for marker in NON_RESULT_STATUS_MARKERS):
                source = str(
                    state.get("source")
                    or "hkjc_official_exact_id_terminal_status"
                )
                _persist_learning_exclusion(
                    mid,
                    w,
                    learning_stages,
                    status or "REFUNDED",
                    source,
                )
                excluded_results.append({
                    "match_id": mid, "home": w.get("home"), "away": w.get("away"),
                    "league": w.get("league"), "kickoff": w.get("kickoff"),
                    "status": status or "REFUNDED", "source": source,
                    "reason": "non_result_terminal_state",
                })
                continue
            missing_results.append({
                "match_id": mid, "fixture_id": w.get("fixture_id"),
                "home": w.get("home"), "away": w.get("away"),
                "league": w.get("league"), "kickoff": w.get("kickoff"),
                "reason": "result_not_returned",
            })
            continue

        rows = []
        learning_rows = []
        for st in learning_stages:
            sc = score_stage(st, res)
            if not sc:
                continue
            d = rebuild(st.get("final"), st.get("now"))
            if d:
                sp = sum(d["p"]) or 1.0
                sc["_p3"] = [x / sp for x in d["p"]]
            sc["match_id"] = mid
            rows.append(sc)
            learning_rows.append((st, sc))
            scored.append(sc)
        if rows:
            _persist_learning_result(mid, w, res, learning_rows)
            last = rows[-1]
            matches.append({
                "match_id": mid, "home": w.get("home"), "away": w.get("away"),
                "league": w.get("league"), "kickoff": w.get("kickoff"),
                "score": last["score_act"],
                "result_source": res.get("source"),
                "corners": last.get("corners_act"),
                "stages": [{k: v for k, v in r.items() if not k.startswith("_")}
                           for r in rows],
            })

    # accuracy_history 係長期記錄，唔可以因為 live watch 清理舊賽事而
    # 覆寫成空白。新結果按 match/stage 取代舊結果，其餘完整保留。
    previous_matches = []
    try:
        with open(HISTORY_OUT, encoding="utf8") as handle:
            previous_matches = json.load(handle).get("matches") or []
    except (OSError, ValueError, TypeError, AttributeError):
        previous_matches = []
    merged_matches = {
        str(match.get("match_id")): match
        for match in previous_matches
        if isinstance(match, dict) and match.get("match_id") is not None
    }
    for match in matches:
        mid = str(match.get("match_id"))
        previous = merged_matches.get(mid) or {}
        stage_map = {
            stage.get("stage"): stage
            for stage in (previous.get("stages") or [])
            if isinstance(stage, dict) and stage.get("stage")
        }
        for stage in match.get("stages") or []:
            if isinstance(stage, dict) and stage.get("stage"):
                stage_map[stage["stage"]] = stage
        merged_matches[mid] = {
            **previous,
            **match,
            "stages": list(stage_map.values()),
        }
    matches = list(merged_matches.values())
    matches.sort(key=lambda m: str(m.get("kickoff") or ""), reverse=True)
    scored = []
    for match in matches:
        mid = str(match.get("match_id"))
        for stage in match.get("stages") or []:
            if isinstance(stage, dict):
                scored.append({**stage, "match_id": mid})
    stage_rows = {
        s: [r for r in scored if r["stage"] == s]
        for s in STAGES
    }
    by_stage = {s: _agg(stage_rows[s]) for s in STAGES}
    by_stage_market = {
        s: {
            code: _market_agg(stage_rows[s], code)
            for code in ("HDC", "HIL", "CHL")
        }
        for s in STAGES
    }
    by_conf = []
    for (lo, hi), lbl in zip(CONF_BINS, CONF_LBL):
        sub = [r for r in scored if r.get("conf") is not None and lo <= r["conf"] < hi]
        a = _agg(sub)
        if a:
            by_conf.append({"lbl": lbl, **a})

    # 最後一個階段(即開賽前最接近嘅一次)每場只計一次
    final_rows = []
    for m in matches:
        final_rows.append([r for r in scored
                           if r["match_id"] == m["match_id"]][-1])

    full_out = {
        "prediction_era": PREDICTION_ERA,
        "schema_version": PREDICTION_SCHEMA_VERSION,
        "generated_at": now.isoformat(timespec="seconds"),
        "n_matches": len(matches), "n_preds": len(scored),
        "n_missing_result": len(missing_results),
        "missing_results": missing_results,
        "n_excluded_result": len(excluded_results),
        "excluded_results": excluded_results,
        "overall": _agg(scored),
        "market_overall": _market_agg(scored),
        "latest": _agg(final_rows),
        "by_stage": {k: v for k, v in by_stage.items() if v},
        "by_stage_market": by_stage_market,
        "by_conf": by_conf,
        "calibration": calibration(scored),
        "matches": matches,
    }
    out = {**full_out, "matches": matches[:200]}
    _atomic_json(HISTORY_OUT, full_out)
    # The T-5 worker is deadline-bound and must never mine history itself.
    # Publish a settled-only discovery artifact during the ordinary accuracy
    # pass, using exactly the rows that are already persisted in history.
    from analysis.granular_conditions import mine
    ranking_rows = [
        {
            "match_id": match.get("match_id"),
            "kickoff": match.get("kickoff"),
            "stage": stage.get("stage"),
            "predicted_at": stage.get("predicted_at") or stage.get("ts"),
            "market_grades": stage.get("market_grades") or [],
        }
        for match in matches if isinstance(match, dict)
        for stage in (match.get("stages") or []) if isinstance(stage, dict)
    ]
    _atomic_json(GRANULAR_RANKING_OUT, {
        "schema_version": 1,
        "system": "footbreak",
        "generated_at": now.isoformat(timespec="seconds"),
        "ranking": mine(ranking_rows, system="footbreak")["ranking"],
    })
    _atomic_json(OUT, out)
    return out


def _r(x, s="—"):
    return s if x is None else x


def report(a):
    L = []
    L.append(f"純預測準繩度 · {a['n_matches']} 場 / {a['n_preds']} 次階段預測")
    if a["n_missing_result"]:
        L.append(f"({a['n_missing_result']} 場攞唔到賽果)")
    o = a["overall"]
    if not o:
        return "\n".join(L + ["尚未有已完場而又有預測嘅賽事。"])

    def line(name, rt, br):
        if not rt:
            return f"  {name:<10} —"
        b = f"Brier {br}" if br is not None else ""
        return f"  {name:<10} {rt['hit']}/{rt['n']} = {rt['pct']}%   {b}"

    L.append("")
    L.append(f"【全部階段 n={o['n']}】")
    L.append(line("1X2", o["wdl"], o["wdl_brier"]))
    L.append(f"             log loss {_r(o['wdl_ll'])} · 平均自信 {_r(o['wdl_conf_mean'])}")
    L.append(line("大細2.5", o["o25"], o["o25_brier"]))
    L.append(line("兩隊入球", o["btts"], o["btts_brier"]))
    L.append(line("角球9.5", o["c95"], o["c95_brier"]))
    L.append(line("準確比分", o["score1"], None))
    L.append(line("頭五比分", o["score5"], None))
    L.append(f"  入球誤差    平均 {_r(o['goals_mae'])} 球 · 八成區間覆蓋 "
             f"{o['goals_cover']['pct'] if o['goals_cover'] else '—'}%")
    L.append(f"  角球誤差    平均 {_r(o['corners_mae'])} 個 · 八成區間覆蓋 "
             f"{o['corners_cover']['pct'] if o['corners_cover'] else '—'}%")

    if a["by_stage"]:
        L.append("")
        L.append("【按階段】")
        for s, v in a["by_stage"].items():
            w = v["wdl"]
            L.append(f"  {s:<5} n={v['n']:<4} 1X2 {w['pct'] if w else '—'}% · "
                     f"Brier {_r(v['wdl_brier'])} · 大細 "
                     f"{v['o25']['pct'] if v['o25'] else '—'}%")
    if a["by_conf"]:
        L.append("")
        L.append("【按信念】(信念高係咪真係準啲?)")
        for v in a["by_conf"]:
            w = v["wdl"]
            L.append(f"  {v['lbl']:<7} n={v['n']:<4} 1X2 {w['pct'] if w else '—'}% · "
                     f"Brier {_r(v['wdl_brier'])}")
    if a["calibration"]:
        L.append("")
        L.append("【1X2 校準】(講 X% 嘅事,實際發生率係?)")
        for c in a["calibration"]:
            L.append(f"  {c['lbl']:<9} n={c['n']:<4} 預測 {c['pred']}% → 實際 {c['act']}%")
    return "\n".join(L)


if __name__ == "__main__":
    a = run(fetch="--no-fetch" not in sys.argv)
    if "--print" in sys.argv:
        print(report(a))
    else:
        o = a["overall"]
        w = (o or {}).get("wdl")
        print(f"準繩度: {a['n_matches']} 場 / {a['n_preds']} 次預測 · "
              f"1X2 {w['pct'] if w else '—'}% · Brier {(o or {}).get('wdl_brier', '—')} "
              f"→ {OUT}")
