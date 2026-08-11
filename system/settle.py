"""賽後自動結算。

抓 OpticOdds `/fixtures/results` 拎全場入球同角球數,
按亞洲盤規則判 Won / Half Won / Refunded / Half Lost / Lost,
回寫 sim_ledger.json 嘅 pnl、命中、本金曲線。

用法:
    python3 settle.py            # 結算所有已開賽超過 130 分鐘嘅待決注
    python3 settle.py --all      # 唔理時間,試晒所有待決注
"""
import json
import math
import os
import subprocess
import sys
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

HKT = timezone(timedelta(hours=8))
HERE = os.path.dirname(os.path.abspath(__file__))
LEDGER = os.path.join(HERE, "sim_ledger.json")
PREDS = os.path.join(HERE, "predictions.json")
RESCACHE = os.path.join(HERE, "cache", "results")
os.makedirs(RESCACHE, exist_ok=True)
NON_RESULT_STATUS_MARKERS = (
    "POSTPON", "CANCEL", "SUSPEND", "ABANDON", "VOID", "REFUND",
    "推迟", "推遲", "延期", "取消", "腰斩", "腰斬", "中止",
)

# 賽事平均 ~115 分鐘完場(90+補時+半場+資料入庫延遲)。留 130 分鐘緩衝。
SETTLE_AFTER_MIN = 130


def is_non_result_terminal_status(
    status, *, refund_pools=None, payout_refund_pools=None
):
    """Only explicit provider terminal states can void a pending bet."""
    text = str(status or "").strip().upper()
    return bool(
        refund_pools
        or payout_refund_pools
        or any(marker.upper() in text for marker in NON_RESULT_STATUS_MARKERS)
    )


def write_json_atomic(path, payload):
    """Replace JSON as one filesystem operation so preemption stays safe."""
    directory = os.path.dirname(path) or "."
    fd, temporary = tempfile.mkstemp(prefix=".settle-", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=1)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _hkjc_result_dates(dates):
    """HKJC business date may precede an after-midnight HKT kickoff date."""
    expanded = set(map(str, dates))
    for raw in list(expanded):
        try:
            expanded.add((date.fromisoformat(raw) - timedelta(days=1)).isoformat())
        except ValueError:
            continue
    return expanded


def fetch_hkjc_results(match_ids, dates):
    """HKJC exact-ID official results in the shape used by this settlement module."""
    root = str(Path(__file__).resolve().parents[1])
    if root not in sys.path:
        sys.path.insert(0, root)
    from crown.hkjc import fetch_official_results
    rows = fetch_official_results(
        set(map(str, match_ids)), _hkjc_result_dates(dates)
    )
    return {
        match_id: {
            "fixture_id": match_id,
            "goals_home": row["home_score"],
            "goals_away": row["away_score"],
            "goals_total": row["home_score"] + row["away_score"],
            "corners_home": None,
            "corners_away": None,
            "corners_total": row.get("corners_total"),
            "checked_at": None,
            "source": "hkjc_official",
        }
        for match_id, row in rows.items()
    }


def _call(path, params):
    payload = json.dumps({
        "source_id": "opticodds", "tool_name": "opticodds",
        "arguments": {"path": path, "params": params},
    })
    p = subprocess.run(["external-tool", "call", payload],
                       capture_output=True, text=True, timeout=180)
    if p.returncode != 0:
        raise RuntimeError(p.stderr[:400])
    d = json.loads(p.stdout)
    r = d.get("result", d)
    if "data" not in r:
        raise RuntimeError(str(r)[:300])
    return r["data"]


# ------------------------------------------------------------ 賽果抓取

def fetch_result(fixture_id, refresh=False, require_corners=False):
    """回傳 dict 或 None。已完場嘅賽果會永久快取。"""
    fp = os.path.join(RESCACHE, f"{fixture_id}.json")
    cached = None
    if not refresh and os.path.exists(fp):
        with open(fp, encoding="utf8") as fh:
            cached = json.load(fh)
        if not require_corners or cached.get("corners_total") is not None:
            return cached
    d = _call("/fixtures/results", {"fixture_id": fixture_id})
    rows = d.get("data") or []
    if not rows:
        return cached
    row = rows[0]
    if (row.get("fixture") or {}).get("status") != "completed":
        return None          # 未完場 / 腰斬 / 取消 → 唔快取,下次再試
    out = parse_result(row)
    if out:
        if cached:
            for key in ("corners_home", "corners_away", "corners_total"):
                if out.get(key) is None and cached.get(key) is not None:
                    out[key] = cached[key]
        with open(fp, "w", encoding="utf8") as fh:
            json.dump(out, fh, ensure_ascii=False)
    return out or cached


def fetch_hkjc_statuses(match_ids, dates):
    """HKJC exact-ID states, including suspended/postponed/refunded matches."""
    root = str(Path(__file__).resolve().parents[1])
    if root not in sys.path:
        sys.path.insert(0, root)
    from crown.hkjc import fetch_official_match_statuses
    return fetch_official_match_statuses(
        set(map(str, match_ids)), _hkjc_result_dates(dates)
    )


def parse_result(row):
    fx = row.get("fixture") or {}
    sc = row.get("scores") or {}

    def side_goals(s):
        b = sc.get(s) or {}
        per = b.get("periods") or {}
        p1, p2 = per.get("period_1"), per.get("period_2")
        if p1 is not None and p2 is not None:
            return p1 + p2          # 法定時間(唔計加時)
        return b.get("total")

    gh, ga = side_goals("home"), side_goals("away")
    if gh is None or ga is None:
        return None

    ms = row.get("market_stats") or {}
    st = row.get("stats") or {}

    def side_corners(s):
        v = (ms.get(s) or {}).get("team_total_corners")
        if v is not None:
            return v
        for blk in (st.get(s) or []):
            if blk.get("period") == "all":
                return (blk.get("stats") or {}).get("corner_taken")
        return None

    ch, ca = side_corners("home"), side_corners("away")
    return {
        "fixture_id": fx.get("id"),
        "home": fx.get("home_team_display"),
        "away": fx.get("away_team_display"),
        "goals_home": gh, "goals_away": ga, "goals_total": gh + ga,
        "corners_home": ch, "corners_away": ca,
        "corners_total": (ch + ca) if (ch is not None and ca is not None) else None,
        "checked_at": row.get("last_checked_at"),
        "source": "opticodds_exact_fixture_id",
    }


def merge_missing_corners(result, fixture_id):
    """以安全唯一 fixture ID 補角球；官方入球比分永遠保留。"""
    if not result or result.get("corners_total") is not None or not fixture_id:
        return result
    fallback = fetch_result(fixture_id, require_corners=True)
    if not fallback or fallback.get("corners_total") is None:
        return result
    merged = dict(result)
    for key in ("corners_home", "corners_away", "corners_total"):
        if merged.get(key) is None:
            merged[key] = fallback.get(key)
    official_source = result.get("source") or "official"
    fallback_source = fallback.get("source") or "exact_fixture"
    merged["source"] = f"{official_source}+{fallback_source}"
    return merged


# ------------------------------------------------------------ 盤口結算

def _legs(cond):
    """'0.0/-0.5' -> [0.0,-0.5];  '-0.75' -> [-0.5,-1.0];  '2.5' -> [2.5]"""
    out = []
    for part in str(cond).split("/"):
        part = part.strip().replace("+", "")
        if part in ("", "-"):
            continue
        try:
            out.append(float(part))
        except ValueError:
            return []
    if len(out) == 1:
        v = out[0]
        frac = abs(v) - math.floor(abs(v))
        if abs(frac - 0.25) < 1e-6 or abs(frac - 0.75) < 1e-6:
            return [v - 0.25, v + 0.25]
    return out or [0.0]


def leg_outcome(code, side, line, res):
    """單一半盤結果:+1 贏 / 0 走水 / -1 輸。資料缺失回 None。"""
    if code == "HDC":
        gh, ga = res.get("goals_home"), res.get("goals_away")
        if gh is None or ga is None:
            return None
        m = (gh - ga) + line if side == "H" else (ga - gh) - line
    elif code == "HIL":
        t = res.get("goals_total")
        if t is None:
            return None
        m = (t - line) if side == "H" else (line - t)
    elif code == "CHL":
        t = res.get("corners_total")
        if t is None:
            return None
        m = (t - line) if side == "H" else (line - t)
    else:
        return None
    if m > 1e-9:
        return 1
    if m < -1e-9:
        return -1
    return 0


LABELS = {
    1.0: "Won", 0.5: "Half Won", 0.0: "Refunded",
    -0.5: "Half Lost", -1.0: "Lost",
}


def settle_bet(bet, res):
    """回傳 (label, pnl) 或 (None, None) 代表資料唔齊。"""
    legs = _legs(bet["condition"])
    outs = [leg_outcome(bet["code"], bet["side"], L, res) for L in legs]
    if any(o is None for o in outs):
        return None, None
    stake_leg = bet["stake"] / len(legs)
    dec = float(bet["odds"])
    pnl = 0.0
    score = 0.0
    for o in outs:
        if o == 1:
            pnl += stake_leg * (dec - 1)
            score += 1 / len(legs)
        elif o == -1:
            pnl -= stake_leg
            score -= 1 / len(legs)
    score = round(score * 2) / 2
    return LABELS.get(score, "Refunded"), round(pnl, 2)


# ------------------------------------------------------------ fixture 補資料

def backfill_fixture_ids(led):
    """舊注單冇 fixture_id — 用 predictions.json 嘅英文隊名 + 快取賽程補番。"""
    # Shadow bets use the same verified result providers as official bets.
    # Include both ledgers here so a missing HKJC result can still fall back
    # through the exact OpticOdds fixture ID instead of leaving shadow rows
    # permanently pending.
    need = [
        b for b in [*led.get("bets", []), *led.get("shadow_bets", [])]
        if not b.get("fixture_id")
    ]
    if not need:
        return 0
    preds = {}
    if os.path.exists(PREDS):
        with open(PREDS, encoding="utf8") as handle:
            prediction_rows = json.load(handle)
        for r in prediction_rows:
            preds[str(r["match_id"])] = r
    import sharp as S
    fixtures = None
    n = 0
    for b in need:
        r = preds.get(str(b["match_id"]))
        if not r:
            continue
        if r.get("fixture_id"):
            b["fixture_id"] = r["fixture_id"]
            b["league_id"] = r.get("league_id")
            n += 1
            continue
        # 靠英文隊名 + 開賽時間喺賽程快取入面搵
        if fixtures is None:
            fixtures = S.list_fixtures()
        ko = datetime.strptime(r["kickoff_hkt"], "%Y-%m-%d %H:%M").replace(tzinfo=HKT)
        best, bs = None, 0.0
        for fx in fixtures:
            try:
                sd = datetime.fromisoformat(fx["start_date"].replace("Z", "+00:00"))
            except Exception:
                continue
            if abs((sd - ko).total_seconds()) > 1800:
                continue
            s = (S._sim(r["home_en"], fx.get("home_team_display") or "")
                 + S._sim(r["away_en"], fx.get("away_team_display") or "")) / 2
            if s > bs:
                best, bs = fx, s
        if best and bs >= 0.8:
            b["fixture_id"] = best["id"]
            b["league_id"] = (best.get("league") or {}).get("id")
            b.setdefault("home_en", r.get("home_en"))
            b.setdefault("away_en", r.get("away_en"))
            n += 1
    return n


# ------------------------------------------------------------ 主流程

def run(force=False):
    if not os.path.exists(LEDGER):
        print("冇 sim_ledger.json")
        return
    with open(LEDGER, encoding="utf8") as handle:
        led = json.load(handle)
    led.setdefault("log", [])
    led.setdefault("bets", [])
    led.setdefault("shadow_bets", [])
    nfill = backfill_fixture_ids(led)
    if nfill:
        print(f"補回 {nfill} 注嘅賽事編號")

    now = datetime.now(HKT)
    due = []
    for bet in [*led["bets"], *led["shadow_bets"]]:
        if bet.get("status") != "PENDING":
            continue
        try:
            kickoff = datetime.strptime(bet["kickoff"], "%Y-%m-%d %H:%M").replace(tzinfo=HKT)
        except Exception:
            continue
        if force or (now - kickoff).total_seconds() / 60 >= SETTLE_AFTER_MIN:
            due.append((bet, kickoff))
    changes, unresolved, provider_errors = [], [], []
    official = {}
    official_statuses = {}
    titan_client, titan_rows = None, []
    if due:
        try:
            official = fetch_hkjc_results(
                {str(bet.get("match_id") or "") for bet, _ in due if bet.get("match_id")},
                {kickoff.strftime("%Y-%m-%d") for _, kickoff in due},
            )
        except Exception:
            # Per-bet OpticOdds fallback below remains available.  A source
            # error only becomes fatal if no fallback can settle that bet.
            official = {}
        try:
            official_statuses = fetch_hkjc_statuses(
                {str(bet.get("match_id") or "") for bet, _ in due if bet.get("match_id")},
                {kickoff.strftime("%Y-%m-%d") for _, kickoff in due},
            )
        except Exception:
            official_statuses = {}
    for b in [*led["bets"], *led["shadow_bets"]]:
        if b.get("status") != "PENDING":
            continue
        try:
            ko = datetime.strptime(b["kickoff"], "%Y-%m-%d %H:%M").replace(tzinfo=HKT)
        except Exception:
            continue
        mins = (now - ko).total_seconds() / 60
        if not force and mins < SETTLE_AFTER_MIN:
            continue
        res = official.get(str(b.get("match_id") or ""))
        state = official_statuses.get(str(b.get("match_id") or "")) or {}
        # An exact-ID official played score always wins over a stale or
        # contradictory status response; a provider issue must never convert
        # a played fixture into a refunded/voided bet.
        if is_non_result_terminal_status(
            state.get("status"),
            refund_pools=state.get("refund_pools"),
            payout_refund_pools=state.get("payout_refund_pools"),
        ) and not res:
            terminal = str(state.get("status") or "REFUNDED")
            b["status"] = "VOIDED"
            b["result"] = "Refunded"
            b["pnl"] = 0.0
            b["void_reason"] = f"fixture_not_played:{terminal}"
            b["settled_at"] = now.isoformat(timespec="seconds")
            b["settlement_source"] = "hkjc_official_exact_id_terminal_status"
            b.setdefault("history", []).append({
                "ts": now.isoformat(timespec="seconds"),
                "stage": "結算",
                "action": "賽事不計",
                "result": "Refunded",
                "terminal_status": terminal,
            })
            prefix = "影子結算 " if b.get("portfolio") == "shadow" else ""
            changes.append(f"⚪ {prefix}{b['home']} v {b['away']} — 賽事不計 ({terminal})")
            continue
        corner_provider_error = None
        if res and b.get("code") == "CHL":
            try:
                res = merge_missing_corners(res, b.get("fixture_id"))
            except Exception as e:
                corner_provider_error = f"opticodds_{type(e).__name__}"
            if res.get("corners_total") is None:
                try:
                    import titan_results as T
                    if titan_client is None:
                        titan_client, titan_rows = T.fetch_titan_result_rows()
                    res = T.merge_titan_corners(
                        res, b, client=titan_client, rows=titan_rows
                    )
                except Exception as e:
                    corner_provider_error = (
                        f"{corner_provider_error or 'titan'}+titan_{type(e).__name__}"
                    )
            if res.get("corners_total") is not None:
                corner_provider_error = None
            if corner_provider_error:
                provider_errors.append(
                    f"{b.get('bet_id') or b['match_id']}: {corner_provider_error}"
                )
        if not res:
            fid = b.get("fixture_id")
            if not fid:
                unresolved.append(f"{b['home']} v {b['away']} — 搵唔到賽事編號")
                continue
            try:
                res = fetch_result(fid)
            except Exception as e:
                unresolved.append(f"{b['home']} v {b['away']} — 抓賽果失敗 {type(e).__name__}")
                provider_errors.append(f"{b.get('bet_id') or b['match_id']}: {type(e).__name__}")
                continue
        if not res:
            unresolved.append(f"{b['home']} v {b['away']} — 賽果未出(開賽後 {mins:.0f} 分)")
            continue
        label, pnl = settle_bet(b, res)
        if label is None:
            miss = "角球數" if b["code"] == "CHL" else "比分"
            unresolved.append(f"{b['home']} v {b['away']} — 缺{miss}資料,暫不結算")
            continue
        b["status"] = "SETTLED"
        b["result"] = label
        b["pnl"] = pnl
        b["settled_at"] = now.isoformat(timespec="seconds")
        b["settlement_source"] = res.get("source") or "opticodds"
        corners_display = None
        if res["corners_home"] is not None and res["corners_away"] is not None:
            corners_display = f"{res['corners_home']}-{res['corners_away']}"
        elif res["corners_total"] is not None:
            corners_display = f"總數 {res['corners_total']}"
        b["score"] = {"goals": f"{res['goals_home']}-{res['goals_away']}",
                      "corners": corners_display,
                      "goals_total": res["goals_total"],
                      "corners_total": res["corners_total"]}
        b.setdefault("history", []).append({
            "ts": now.isoformat(timespec="seconds"), "stage": "結算",
            "action": "結算", "result": label, "pnl": pnl,
            "score": b["score"],
        })
        ico = {"Won": "🟢", "Half Won": "🟡", "Refunded": "⚪",
               "Half Lost": "🟠", "Lost": "🔴"}.get(label, "•")
        cx = f" 角球 {b['score']['corners']}" if b["code"] == "CHL" and b["score"]["corners"] else ""
        prefix = "影子結算 " if b.get("portfolio") == "shadow" else ""
        changes.append(f"{ico} {prefix}{b['home']} v {b['away']} — {b['label']} → "
                       f"{label} {pnl:+,.0f}(比分 {b['score']['goals']}{cx})")

    stats = recompute(led)
    if changes or unresolved:
        led["log"].insert(0, {
            "ts": now.isoformat(timespec="seconds"),
            "kind": "結算",
            "changes": changes + [f"⏳ {u}" for u in unresolved],
        })
    write_json_atomic(LEDGER, led)

    for c in changes:
        print(c)
    for u in unresolved:
        print("⏳", u)
    if not changes and not unresolved:
        print("冇注單到結算時間")
    print(f"\n已結算 {stats['n_settled']} 注 · 命中 "
          f"{stats['hit_rate']*100:.1f}% ({stats['hits']}/{stats['n_decided']}) · "
          f"盈虧 {stats['pnl']:+,.0f} · ROI {stats['roi']*100:+.2f}% · "
          f"戶口 ${stats['equity']:,.0f}")
    if provider_errors:
        # A result-source outage is not a valid "no result yet" state.  Let
        # run_all/deploy/run fail so nginx keeps the last known-good dashboard.
        raise RuntimeError(f"賽果 provider failed for {len(provider_errors)} due bet(s)")
    return stats


def recompute(led):
    """重算彙總統計,寫入 led['stats']。"""
    led.setdefault("bets", [])
    led.setdefault("shadow_bets", [])
    done = [b for b in led["bets"] if b.get("status") == "SETTLED"]
    pnl = sum(b.get("pnl") or 0 for b in done)
    turnover = sum(b["stake"] for b in done)
    decided = [b for b in done if b.get("result") != "Refunded"]
    hits = sum(1 for b in decided if b.get("result") in ("Won", "Half Won"))
    by_mkt = {}
    for b in done:
        m = by_mkt.setdefault(b["market"], {"n": 0, "stake": 0.0, "pnl": 0.0, "hit": 0, "dec": 0})
        m["n"] += 1
        m["stake"] += b["stake"]
        m["pnl"] += b.get("pnl") or 0
        if b.get("result") != "Refunded":
            m["dec"] += 1
            if b.get("result") in ("Won", "Half Won"):
                m["hit"] += 1
    for m in by_mkt.values():
        m["roi"] = round(m["pnl"] / m["stake"], 4) if m["stake"] else 0.0
        m["hit_rate"] = round(m["hit"] / m["dec"], 4) if m["dec"] else 0.0
        m["pnl"] = round(m["pnl"], 2)
    # 資金曲線(按結算時間)
    curve, eq = [], led.get("bankroll", 50000)
    for b in sorted(done, key=lambda x: x.get("settled_at") or ""):
        eq += b.get("pnl") or 0
        curve.append({"ts": b.get("settled_at"), "label": f"{b['home']} v {b['away']}",
                      "pnl": round(b.get("pnl") or 0, 2), "equity": round(eq, 2)})
    stats = {
        "n_settled": len(done),
        "n_decided": len(decided),
        "hits": hits,
        "hit_rate": round(hits / len(decided), 4) if decided else 0.0,
        "pnl": round(pnl, 2),
        "turnover": round(turnover, 2),
        "roi": round(pnl / turnover, 4) if turnover else 0.0,
        "equity": round(led.get("bankroll", 50000) + pnl, 2),
        "by_market": by_mkt,
        "curve": curve,
    }
    led["stats"] = stats
    recompute_shadow_stats(led)
    return stats


def recompute_shadow_stats(led):
    """Refresh only the isolated confidence-only portfolio totals."""
    led.setdefault("bets", [])
    led.setdefault("shadow_bets", [])
    shadow = _portfolio_stats(led["shadow_bets"], led.get("bankroll", 50000))
    shadow.update({
        "conf_floor": P_CONF_FLOOR(),
        "bet_stage": "T-5",
        "single_cap_pct": 0.02,
        "mode": "confidence_only_shadow",
        "excluded_from_official": True,
        "comparison": _same_period_comparison(
            led["bets"], led["shadow_bets"], led.get("bankroll", 50000)
        ),
    })
    led["shadow_stats"] = shadow
    return shadow


def P_CONF_FLOOR():
    """Read the configured Footbreak floor without importing prediction code at import time."""
    try:
        import predict as P
        return P.CONF_FLOOR
    except Exception:
        return 58.0


def _portfolio_stats(bets, bankroll):
    settled = [b for b in bets if b.get("status") == "SETTLED"]
    pending = [b for b in bets if b.get("status") == "PENDING"]
    pnl = round(sum(float(b.get("pnl") or 0) for b in settled), 2)
    turnover = round(sum(float(b.get("stake") or 0) for b in settled), 2)
    decided = [b for b in settled if b.get("result") != "Refunded"]
    hits = sum(b.get("result") in ("Won", "Half Won") for b in decided)
    by_market = {}
    for b in settled:
        row = by_market.setdefault(b.get("market") or b.get("code") or "其他",
                                   {"n": 0, "stake": 0.0, "pnl": 0.0, "hit": 0, "dec": 0})
        row["n"] += 1
        row["stake"] += float(b.get("stake") or 0)
        row["pnl"] += float(b.get("pnl") or 0)
        if b.get("result") != "Refunded":
            row["dec"] += 1
            row["hit"] += int(b.get("result") in ("Won", "Half Won"))
    for row in by_market.values():
        row["stake"] = round(row["stake"], 2)
        row["pnl"] = round(row["pnl"], 2)
        row["roi"] = round(row["pnl"] / row["stake"], 4) if row["stake"] else None
        row["hit_rate"] = round(row["hit"] / row["dec"], 4) if row["dec"] else None
    equity = float(bankroll) + pnl
    curve, running = [], float(bankroll)
    for b in sorted(settled, key=lambda b: str(b.get("settled_at") or b.get("created_at") or "")):
        bet_pnl = float(b.get("pnl") or 0)
        running += bet_pnl
        curve.append({"ts": b.get("settled_at") or b.get("created_at"),
                      "label": f"{b.get('home', '')} v {b.get('away', '')}".strip(),
                      "pnl": round(bet_pnl, 2), "equity": round(running, 2)})
    return {
        "n_pending": len(pending), "n_voided": sum(b.get("status") == "VOIDED" for b in bets),
        "n_settled": len(settled), "n_decided": len(decided), "hits": hits,
        "hit_rate": round(hits / len(decided), 4) if decided else None,
        "pnl": pnl, "turnover": turnover,
        "roi": round(pnl / turnover, 4) if turnover else None,
        "open_stake": round(sum(float(b.get("stake") or 0) for b in pending), 2),
        "open_pct": round(sum(float(b.get("stake") or 0) for b in pending) / bankroll, 4) if bankroll else 0,
        "equity": round(equity, 2), "by_market": by_market, "curve": curve,
        "res_counts": {name: sum(b.get("result") == name for b in settled)
                       for name in ("Won", "Half Won", "Refunded", "Half Lost", "Lost")},
    }


def _bet_time(bet):
    value = bet.get("created_at") or bet.get("kickoff")
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=HKT)
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def _same_period_comparison(official, shadow, bankroll):
    dated_shadow = [(bet, _bet_time(bet)) for bet in shadow]
    starts = [timestamp for _, timestamp in dated_shadow if timestamp is not None]
    if not starts:
        return None
    start = min(starts)
    official_period = [bet for bet in official
                       if (timestamp := _bet_time(bet)) is not None and timestamp >= start]
    shadow_period = [bet for bet, timestamp in dated_shadow
                     if timestamp is not None and timestamp >= start]
    return {
        "period_start": start.isoformat(), "definition": "from_first_shadow_bet",
        "official_total_bets": len(official_period), "shadow_total_bets": len(shadow_period),
        "official": _portfolio_stats(official_period, bankroll),
        "shadow": _portfolio_stats(shadow_period, bankroll),
    }


if __name__ == "__main__":
    run(force="--all" in sys.argv)
