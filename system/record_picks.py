"""三段預測記錄 + 模擬倉。

規則(用戶定):
  首預  每晚 23:59 掃馬會全板,每場只做一次。參考初盤 / 開盤結構。
  T-30  開賽前 30 分鐘 —— 陣容、傷患出咗,賠率漸定。只記錄,不落注。
  T-5   開賽前 5 分鐘  —— 唯一可以落注嘅時點。綜合天氣、傷患、陣容、
                          賠率變化做最終決定。

所以 sim_ledger.json 有兩層:
  watch  —— 每場嘅三段預測記錄(全部階段都寫)
  bets   —— 真正落注(只由 T-5 產生)
"""
import json
import os
import sys
import tempfile
import datetime as dt

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from analysis.learning_store import LearningStore
import predict as P
from settle import recompute_shadow_stats

LEDGER = os.path.join(HERE, "sim_ledger.json")
HKT = dt.timezone(dt.timedelta(hours=8))

BANKROLL = 50000.0
DAILY_CAP = 1.00   # 用戶指定:單日不設限
OPEN_CAP = 1.00    # 用戶指定:在場不設限
BET_STAGE = "T-5"          # 唯一落注階段
MIN_BET_LEAD_SECONDS = 5   # 少過 5 秒已無實際通知/落注時間
PREDICTION_ERA = "2026-08-10-market-learning-v2"
PREDICTION_SCHEMA_VERSION = 2


def load():
    if os.path.exists(LEDGER):
        with open(LEDGER, encoding="utf-8") as handle:
            d = json.load(handle)
    else:
        d = {"bankroll": BANKROLL, "bets": [], "shadow_bets": [], "log": [],
             "stats": {}, "shadow_stats": {}}
    d.setdefault("watch", {})
    d.setdefault("bets", [])
    d.setdefault("shadow_bets", [])
    d.setdefault("log", [])
    d.setdefault("stats", {})
    d.setdefault("shadow_stats", {})
    return d


def save(led):
    directory = os.path.dirname(LEDGER) or "."
    fd, temporary = tempfile.mkstemp(prefix=".sim-ledger-", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(led, handle, ensure_ascii=False, indent=1)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, LEDGER)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def day_open(led, day):
    return sum(b["stake"] for b in led["bets"]
               if b["status"] == "PENDING" and b["kickoff"][:10] == day)


def total_open(led):
    return sum(b["stake"] for b in led["bets"] if b["status"] == "PENDING")


def _slim_adjs(adjs):
    out = []
    for a in (adjs or []):
        out.append({k: a.get(k) for k in
                    ("tag", "reason", "conf", "goals", "corners", "sup")
                    if a.get(k) is not None})
    return out


def _market_predictions(candidates):
    """每個市場保留一個與賠率/EV無關的正式方向。

    主線優先；同一條主線兩邊之中，揀模型條件勝率較高的一邊。完整盤口
    仍由即時 predictions.json 保留，歷史只需要一個不重複計權的樣本。
    """
    grouped = {}
    for candidate in candidates or []:
        code = candidate.get("code")
        if code not in {"HDC", "HIL", "CHL"}:
            continue
        grouped.setdefault(code, []).append(candidate)
    output = []
    for code, rows in grouped.items():
        main = [row for row in rows if row.get("is_main")]
        pool = main or rows
        def decided_probability(row):
            probability = float(row.get("prob") or 0)
            push = float(row.get("push") or 0)
            return probability / max(1e-9, 1.0 - push)
        best = max(pool, key=lambda row: (decided_probability(row), float(row.get("prob") or 0)))
        output.append({
            "code": code,
            "market": best.get("market"),
            "condition": best.get("condition"),
            # Preserve the selected market's exact numeric line for isolated
            # notification-only T-5 signals.  This does not affect pricing,
            # staking, settlement, model learning, or any official/shadow
            # ledger decision.
            "line": best.get("line", best.get("condition")),
            "side": best.get("side"),
            "label": best.get("label"),
            "odds": best.get("odds"),
            "probability": round(decided_probability(best), 6),
            "win_probability": best.get("prob"),
            "push_probability": best.get("push"),
            "is_main": bool(best.get("is_main")),
            "source": "footbreak_model_current_main_line",
        })
    return sorted(output, key=lambda row: row["code"])


def _shadow_pick(result):
    """Return the isolated no-benchmark T-5 shadow decision, if eligible.

    This intentionally never calls the EV, Kelly, dynamic-threshold, or
    official-staking paths.  It is a confidence-only observation portfolio.
    """
    if result.get("stage") != BET_STAGE:
        return None
    if result.get("sharp_reference_available"):
        return None
    if float(result.get("conviction") or 0) < float(P.CONF_FLOOR):
        return None
    candidates = list(result.get("candidates") or [])
    if not candidates:
        return None

    def decided_probability(candidate):
        probability = float(candidate.get("prob") or 0)
        push = float(candidate.get("push") or 0)
        return probability / max(1e-9, 1.0 - push)

    # Main lines are chosen before probability, so a peripheral alternative
    # never supplants the market's liquid central line merely on probability.
    main = [candidate for candidate in candidates if candidate.get("is_main")]
    pool = main or candidates
    selected = max(
        pool,
        key=lambda candidate: (
            decided_probability(candidate), float(candidate.get("prob") or 0),
        ),
    )
    return {
        "market": selected.get("market"), "code": selected.get("code"),
        "condition": selected.get("condition"), "side": selected.get("side"),
        "label": f"{selected.get('market')} {selected.get('label')}",
        "odds": selected.get("odds"), "prob": selected.get("prob"),
        "push": selected.get("push"), "is_main": bool(selected.get("is_main")),
        "conviction": result.get("conviction"),
        "stake": round(float(P.BANKROLL) * 0.02, 2),
    }


def _record_shadow_bet(ledger, result, match_id, now):
    """Persist one immutable shadow row per fixture without touching official bets."""
    shadow_pick = _shadow_pick(result)
    if not shadow_pick:
        return None
    shadow_bets = ledger.setdefault("shadow_bets", [])
    if any(str(bet.get("match_id")) == str(match_id) for bet in shadow_bets):
        return None
    bet_id = (f"shadow|{match_id}|{shadow_pick['code']}|"
              f"{shadow_pick['condition']}|{shadow_pick['side']}")
    shadow_bets.append({
        "bet_id": bet_id, "match_id": str(match_id),
        "league": result.get("league"), "fixture_id": result.get("fixture_id"),
        "league_id": result.get("league_id"), "home": result.get("home"),
        "away": result.get("away"), "home_en": result.get("home_en"),
        "away_en": result.get("away_en"), "kickoff": result.get("kickoff_hkt"),
        "model_source": result.get("model_source"),
        "sharp_reference_available": False,
        "market": shadow_pick["market"], "code": shadow_pick["code"],
        "condition": shadow_pick["condition"], "side": shadow_pick["side"],
        "label": shadow_pick["label"], "odds": shadow_pick["odds"],
        "stake": shadow_pick["stake"], "model_prob": shadow_pick["prob"],
        "push_prob": shadow_pick["push"], "ev": None, "fair": None,
        "kelly": None, "kelly_full": None, "conviction": shadow_pick["conviction"],
        "confidence_only": True, "shadow_only": True, "portfolio": "shadow",
        "first_stage": BET_STAGE, "stage": BET_STAGE, "status": "PENDING",
        "result": None, "pnl": None, "created_at": now,
        "history": [{"ts": now, "stage": BET_STAGE, "action": "影子注建立",
                     "label": shadow_pick["label"], "odds": shadow_pick["odds"],
                     "stake": shadow_pick["stake"],
                     "reason": "confidence-only；固定 2% 本金；無 PinnAPI 基準；不計 EV"}],
    })
    return bet_id


def _snap(r, now):
    """把一次預測壓成一個階段記錄。"""
    can_bet = bool(r.get("can_bet"))
    pick = r.get("pick") or (r.get("lead_view") if not can_bet else None)
    cands = r.get("candidates") or []
    lead = cands[0] if cands else None
    wx = r.get("weather") or {}
    if can_bet:
        verdict = "落注" if r.get("pick") else "觀望"
    elif pick:
        verdict = "傾向"          # 已過信念門檻,但未到落注時點
    elif lead and lead.get("ev", -1) > 0:
        verdict = "偏向"          # 有正值方向,但信念未夠
    else:
        verdict = "無傾向"
    return {
        "prediction_era": PREDICTION_ERA,
        "schema_version": PREDICTION_SCHEMA_VERSION,
        "stage": r["stage"],
        "ts": now,
        # Fixture context is stored with every immutable stage snapshot so the
        # offline challenger can encode league/home-away context without
        # joining mutable live-watch state.
        "league": r.get("league"),
        "home": r.get("home"),
        "away": r.get("away"),
        "neutral": r.get("neutral"),
        "can_bet": can_bet,
        "mins_to_ko": r.get("mins_to_ko"),
        "conviction": r.get("conviction"),
        "model_source": r.get("model_source"),
        "sharp_reference_available": bool(r.get("sharp_reference_available")),
        "sharp_reference_note": r.get("sharp_reference_note"),
        "verdict": verdict,
        "no_bet_reason": r.get("no_bet_reason"),
        # 只有 T-5 嘅 pick 會變成真注;前兩段只係傾向
        "pick": ({"market": pick["market"], "code": pick["code"],
                  "condition": pick["condition"], "side": pick["side"],
                  "label": f"{pick['market']} {pick['label']}",
                  "odds": pick["odds"], "prob": pick["prob"],
                  "push": pick["push"], "ev": pick["ev"],
                  "kelly": pick["kelly_used"], "stake": pick["stake"]}
                 if pick else None),
        # 就算未夠信念,都記低模型最看好嘅一條
        "lead": ({"market": lead.get("market"), "label": lead.get("label"),
                  "odds": lead.get("odds"), "prob": lead.get("prob"),
                  "ev": lead.get("ev")} if lead else None),
        # 三段推演
        "open": r.get("open"),
        "now": r.get("now"),
        "final": r.get("final"),
        "movement": r.get("movement"),
        "adjustments": _slim_adjs(r.get("adjustments")),
        "mults": r.get("mults"),
        "outcome": r.get("outcome"),
        "market_predictions": _market_predictions(cands),
        # 資訊齊唔齊
        "info": {
            "weather": bool(wx),
            "temp": wx.get("temp_c"),
            "desc": wx.get("desc"),
            "news": bool(r.get("has_news")),
            "hk_lines": r.get("n_hk_lines"),
            "hk_moved": r.get("hk_moved_since_last"),
            "hk_max_move_pct": r.get("hk_max_move_pct"),
        },
    }


STAGE_ORDER = {"首預": 1, "T-30": 2, "T-5": 3}


def _record_learning_snapshot(mid, r, snap):
    path = os.environ.get("LEARNING_DB_PATH")
    if not path:
        return None
    kickoff = dt.datetime.fromisoformat(str(r["kickoff_hkt"]))
    if kickoff.tzinfo is None:
        kickoff = kickoff.replace(tzinfo=HKT)
    payload = {key: value for key, value in snap.items() if key != "ts"}
    with LearningStore(path) as store:
        return store.record_snapshot(
            "footbreak",
            mid,
            str(r["stage"]),
            snap["ts"],
            kickoff.isoformat(),
            payload,
            model_version=PREDICTION_ERA,
            schema_version=str(PREDICTION_SCHEMA_VERSION),
        )


def sync(preds_file="predictions.json"):
    led = load()
    with open(os.path.join(HERE, preds_file), encoding="utf-8") as handle:
        preds = json.load(handle)
    now = dt.datetime.now(HKT).isoformat(timespec="seconds")
    changes, notes = [], []
    fresh_t5_signal_ids = []

    for r in preds:
        mid = str(r["match_id"])
        stage = r["stage"]
        if stage not in STAGE_ORDER:
            continue                      # 待入窗 — 未到第一個預測點,唔記錄
        if stage == BET_STAGE:
            try:
                kickoff = dt.datetime.fromisoformat(r["kickoff_hkt"])
                if kickoff.tzinfo is None:
                    kickoff = kickoff.replace(tzinfo=HKT)
                lead = (kickoff - dt.datetime.now(HKT)).total_seconds()
            except (KeyError, TypeError, ValueError):
                lead = -1
            if lead < MIN_BET_LEAD_SECONDS:
                notes.append(
                    f"⏭ {r.get('home')} v {r.get('away')} — "
                    "T-5 到帳本前已過安全落注時間，沒有建立或更新注單"
                )
                continue

        # ── 1. 寫入 / 更新階段記錄 ────────────────────────────
        w = led["watch"].setdefault(mid, {
            "match_id": mid, "league": r["league"],
            "home": r["home"], "away": r["away"],
            "home_en": r.get("home_en"), "away_en": r.get("away_en"),
            "kickoff": r["kickoff_hkt"],
            "fixture_id": r.get("fixture_id"),
            "league_id": r.get("league_id"),
            "venue": r.get("venue"), "venue_city": r.get("venue_city"),
            "stages": [],
        })
        w["fixture_id"] = w.get("fixture_id") or r.get("fixture_id")
        w["league_id"] = w.get("league_id") or r.get("league_id")
        snap = _snap(r, now)
        if stage == BET_STAGE:
            # This is a data-path completeness flag only.  It deliberately
            # does not alter conviction, pick eligibility, or stake.
            snap["t30_data_complete"] = any(
                item.get("stage") == "T-30" for item in w["stages"]
            )
        learning = _record_learning_snapshot(mid, r, snap)
        if learning:
            snap.update({
                "learning_snapshot_id": learning["snapshot_id"],
                "learning_attempt": learning["attempt"],
                "learning_pre_kickoff": learning["pre_kickoff"],
                "learning_payload_sha256": learning["payload_sha256"],
            })
            if not learning["pre_kickoff"]:
                notes.append(
                    f"⏭ {r.get('home')} v {r.get('away')} — "
                    f"{stage} 賽後重跑已隔離，沒有覆蓋賽前預測"
                )
                continue
        prev = next((x for x in w["stages"] if x["stage"] == stage), None)
        if prev:
            w["stages"][w["stages"].index(prev)] = snap    # 同階段重跑 → 覆蓋
        else:
            w["stages"].append(snap)
            lbl = snap["pick"]["label"] if snap["pick"] else "無明顯傾向"
            notes.append(f"📝 {r['home']} v {r['away']} — {stage} "
                         f"{snap['verdict']}:{lbl}(信念 {r['conviction']:.1f})")
            if stage == BET_STAGE:
                fresh_t5_signal_ids.append(mid)
        w["stages"].sort(key=lambda x: STAGE_ORDER.get(x["stage"], 9))

        # ── 2. 只有 T-5 才可以落注 ───────────────────────────
        if stage != BET_STAGE:
            continue

        # This is deliberately outside the official pick/change path: a
        # missing independent benchmark can create a shadow observation only,
        # never an official bet, notification, learning input or threshold.
        _record_shadow_bet(led, r, mid, now)

        pick = r.get("pick")
        day = r["kickoff_hkt"][:10]
        cur = next((b for b in led["bets"]
                    if b["match_id"] == mid and b["status"] == "PENDING"), None)

        if not pick:
            if cur:      # T-5 重跑後改觀望 → 撤回
                cur["history"].append({"ts": now, "stage": stage,
                                       "action": "轉觀望",
                                       "reason": r["no_bet_reason"],
                                       "conviction": r["conviction"],
                                       "final": r["final"]})
                cur["status"] = "VOIDED"
                cur["void_reason"] = f"{stage} 轉觀望:{r['no_bet_reason']}"
                changes.append(f"❌ {r['home']} v {r['away']} — T-5 撤回"
                               f"({r['no_bet_reason']})")
            else:
                changes.append(f"⏸ {r['home']} v {r['away']} — T-5 最終決定:"
                               f"觀望({r['no_bet_reason']})")
            continue

        label = f"{pick['market']} {pick['label']}"
        want = pick["stake"]
        room_day = max(0.0, BANKROLL * DAILY_CAP - day_open(led, day))
        room_all = max(0.0, BANKROLL * OPEN_CAP - total_open(led))
        capped = min(want, room_day, room_all)

        if cur is None:
            if capped <= 0:
                changes.append(f"⚠ {r['home']} v {r['away']} — 組合上限已滿,唔落注")
                continue
            # 落注時把三段演變一併帶入注單
            path = [{"stage": x["stage"], "ts": x["ts"],
                     "verdict": x["verdict"],
                     "label": (x["pick"] or x["lead"] or {}).get("label"),
                     "odds": (x["pick"] or x["lead"] or {}).get("odds"),
                     "ev": (x["pick"] or x["lead"] or {}).get("ev"),
                     "conviction": x["conviction"],
                     "final": x["final"]}
                    for x in w["stages"] if x["stage"] != BET_STAGE]
            hist = [{"ts": x["ts"], "stage": x["stage"], "action": "預測",
                     "label": (x["pick"] or x["lead"] or {}).get("label"),
                     "odds": (x["pick"] or x["lead"] or {}).get("odds"),
                     "ev": (x["pick"] or x["lead"] or {}).get("ev"),
                     "conviction": x["conviction"],
                     "reason": (None if x["verdict"] == "投注"
                                else x.get("no_bet_reason")),
                     "final": x["final"]}
                    for x in w["stages"] if x["stage"] != BET_STAGE]
            hist.append({"ts": now, "stage": stage, "action": "落注",
                         "label": label, "odds": pick["odds"],
                         "stake": round(capped), "ev": pick["ev"],
                         "conviction": r["conviction"], "final": r["final"]})
            led["bets"].append({
                "bet_id": f"{mid}|{pick['code']}|{pick['condition']}|{pick['side']}",
                "match_id": mid, "league": r["league"],
                "fixture_id": r.get("fixture_id"),
                "league_id": r.get("league_id"),
                "model_source": r.get("model_source"),
                "sharp_reference_available": bool(r.get("sharp_reference_available")),
                "home": r["home"], "away": r["away"],
                "home_en": r.get("home_en"), "away_en": r.get("away_en"),
                "kickoff": r["kickoff_hkt"],
                "market": pick["market"], "code": pick["code"],
                "condition": pick["condition"], "side": pick["side"],
                "label": label, "odds": pick["odds"],
                "stake": round(capped), "model_prob": pick["prob"],
                "push_prob": pick["push"], "ev": pick["ev"],
                "kelly": pick["kelly_used"], "conviction": r["conviction"],
                "kelly_full": pick.get("kelly_raw"),
                "stake_stage": pick.get("stake_stage"),
                "created_at": dt.datetime.now(HKT).isoformat(timespec="seconds"),
                "first_stage": BET_STAGE, "stage": stage, "status": "PENDING",
                "result": None, "pnl": None,
                "path": path, "history": hist,
            })
            diff = [x for x in path
                    if (x.get("label") or "") != label]
            changes.append(f"✅ {r['home']} v {r['away']} — T-5 落注 "
                           f"{label} @{pick['odds']} ${capped:,.0f}"
                           + (f"(與前 {len(diff)} 段預測唔同)" if diff else "(三段一致)"))
            continue

        # T-5 同一場重跑 — 更新
        same = (cur["code"] == pick["code"]
                and cur["condition"] == pick["condition"]
                and cur["side"] == pick["side"])
        if not same:
            cur["history"].append({"ts": now, "stage": stage,
                                   "action": "改盤口",
                                   "from": cur["label"], "to": label,
                                   "odds": pick["odds"], "ev": pick["ev"],
                                   "conviction": r["conviction"],
                                   "final": r["final"]})
            changes.append(f"🔄 {r['home']} v {r['away']} — T-5 由 "
                           f"{cur['label']} 改為 {label} @{pick['odds']}")
            cur.update({"code": pick["code"], "condition": pick["condition"],
                        "side": pick["side"], "label": label,
                        "market": pick["market"]})
        add = 0.0
        if want > cur["stake"]:
            add = min(want - cur["stake"], room_day, room_all)
        cur.update({"odds": pick["odds"], "model_prob": pick["prob"],
                    "push_prob": pick["push"], "ev": pick["ev"],
                    "conviction": r["conviction"], "stage": stage})
        cur.setdefault("fixture_id", r.get("fixture_id"))
        cur.setdefault("league_id", r.get("league_id"))
        if add > 0:
            cur["stake"] = round(cur["stake"] + add)
            cur["history"].append({"ts": now, "stage": stage, "action": "加注",
                                   "add": round(add), "stake": cur["stake"],
                                   "odds": pick["odds"], "ev": pick["ev"],
                                   "conviction": r["conviction"],
                                   "final": r["final"]})
            changes.append(f"➕ {r['home']} v {r['away']} — T-5 加注 "
                           f"${add:,.0f} → ${cur['stake']:,.0f}")
        elif same:
            cur["history"].append({"ts": now, "stage": stage, "action": "維持",
                                   "odds": pick["odds"], "ev": pick["ev"],
                                   "conviction": r["conviction"],
                                   "final": r["final"]})

    led["log"].append({"ts": now, "kind": "預測",
                       "n_changes": len(changes),
                       "changes": changes or ["今次無落注動作"],
                       "notes": notes[:40]})
    # Shadow rows appear before their first result-settlement run.  Refresh
    # their isolated dashboard totals here without recomputing or touching the
    # official portfolio's stats/staking state.
    recompute_shadow_stats(led)
    save(led)
    if fresh_t5_signal_ids:
        # Notification is strictly after the immutable live snapshot has been
        # saved.  A delivery failure is deliberately non-fatal and cannot
        # alter the saved prediction/ledger transaction.
        try:
            import notify
            notify.notify_fresh_t5_signals(led, fresh_t5_signal_ids)
        except Exception as exc:
            notes.append(f"Telegram T-5 訊號發送失敗({type(exc).__name__})；已保存預測，唔影響注單。")
    return changes, notes, led


def migrate_pre_t5(led):
    """規則更新前(T-60/T-30 就落注)嘅舊注單 → 撤回,只保留為預測記錄。"""
    n = 0
    now = dt.datetime.now(HKT).isoformat(timespec="seconds")
    for b in led["bets"]:
        if b.get("status") != "PENDING":
            continue
        if b.get("first_stage") in (None, "T-5"):
            continue
        b["status"] = "VOIDED"
        b["void_reason"] = ("規則更新:只可以喺開賽前 5 分鐘落注,"
                            f"此注原本喺 {b.get('first_stage')} 建立,轉為預測記錄")
        b.setdefault("history", []).append(
            {"ts": now, "stage": b.get("stage"), "action": "轉觀望",
             "reason": "規則更新 — 落注時點統一為 T-5"})
        n += 1
    return n


def migrate_min_odds(led, floor=None):
    """規則更新前(未設最低賠率門檻)嘅短賠率待決注單 → 撤回。"""
    import model as _M
    fl = _M.MIN_ODDS if floor is None else float(floor)
    n = 0
    now = dt.datetime.now(HKT).isoformat(timespec="seconds")
    for b in led["bets"]:
        if b.get("status") != "PENDING":
            continue
        if float(b.get("odds") or 0) >= fl:
            continue
        b["status"] = "VOIDED"
        b["void_reason"] = (f"規則更新:最低賠率門檻 {fl:.2f},"
                            f"此注賠率 {float(b.get('odds') or 0):.2f} 不達標,"
                            f"轉為預測記錄")
        b.setdefault("history", []).append(
            {"ts": now, "stage": b.get("stage"), "action": "轉觀望",
             "reason": f"規則更新 — 最低賠率 {fl:.2f}"})
        n += 1
    return n


def summary(led):
    pend = [b for b in led["bets"] if b["status"] == "PENDING"]
    void = [b for b in led["bets"] if b["status"] == "VOIDED"]
    done = [b for b in led["bets"] if b["status"] == "SETTLED"]
    tot = sum(b["stake"] for b in pend)
    pnl = sum(b.get("pnl") or 0 for b in done)
    n_st = sum(len(w.get("stages") or []) for w in led["watch"].values())
    return {"追蹤賽事": len(led["watch"]), "階段預測": n_st,
            "待決": len(pend), "已撤": len(void), "已結算": len(done),
            "在場注碼": tot, "在場佔本金": f"{tot/BANKROLL:.1%}",
            "累計盈虧": pnl}


def export_csv(led, path=None):
    import csv
    path = path or os.path.join(HERE, "sim_ledger.csv")
    cols = ["kickoff", "league", "home", "away", "market", "label", "odds",
            "stake", "model_prob", "push_prob", "ev", "conviction",
            "first_stage", "stage", "status", "result", "pnl", "n_updates"]
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["開賽時間", "聯賽", "主隊", "客隊", "市場", "投注",
                    "賠率", "注碼", "模型勝率", "走水率", "EV", "信念",
                    "落注階段", "最新階段", "狀態", "賽果", "盈虧", "更新次數"])
        for b in led["bets"]:
            w.writerow([b.get(c) if c != "n_updates" else len(b.get("history") or [])
                        for c in cols])
    return path


def export_watch_csv(led, path=None):
    """三段預測記錄 CSV — 每場每階段一行,方便喺 Excel 對比演變。"""
    import csv
    path = path or os.path.join(HERE, "sim_watch.csv")
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["開賽時間", "聯賽", "主隊", "客隊", "階段", "預測時間",
                    "結論", "建議/最看好", "賠率", "模型勝率", "EV", "信念",
                    "初盤總入球", "現價總入球", "我終值總入球",
                    "初盤主客差", "現價主客差", "我終值主客差",
                    "角球 μ", "氣溫", "有陣容資訊", "馬會線數", "觀望原因"])
        for m in sorted(led["watch"].values(), key=lambda x: x["kickoff"]):
            for s in m.get("stages", []):
                p = s.get("pick") or s.get("lead") or {}
                op, nw, fi = s.get("open") or {}, s.get("now") or {}, s.get("final") or {}
                info = s.get("info") or {}
                w.writerow([m["kickoff"], m["league"], m["home"], m["away"],
                            s["stage"], s["ts"], s["verdict"],
                            p.get("label"), p.get("odds"), p.get("prob"),
                            p.get("ev"), s.get("conviction"),
                            op.get("total"), nw.get("total"), fi.get("total"),
                            op.get("supremacy"), nw.get("supremacy"), fi.get("supremacy"),
                            fi.get("mu"), info.get("temp"),
                            "有" if info.get("news") else "冇",
                            info.get("hk_lines"), s.get("no_bet_reason")])
    return path


if __name__ == "__main__":
    import sys
    led0 = load()
    if "--migrate" in sys.argv:
        n = migrate_pre_t5(led0)
        save(led0)
        print(f"已把 {n} 注舊規則注單轉為預測記錄")
    if "--void-low-odds" in sys.argv:
        n = migrate_min_odds(led0)
        save(led0)
        print(f"已把 {n} 注低於最低賠率門檻嘅注單轉為預測記錄")
    ch, notes, led = sync()
    print("\n".join(notes) if notes else "無新階段預測")
    print()
    print("\n".join(ch) if ch else "無落注動作")
    print()
    for k, v in summary(led).items():
        print(f"  {k}: {v}")
    print("\n注單 CSV →", export_csv(led))
    print("預測 CSV →", export_watch_csv(led))
