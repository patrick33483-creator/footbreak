"""足破 · Telegram 通知系統

只有真正建立注單(T-5 落注)，或者模型候選通過既定安全門檻需要
人工審核時才發通知。預測、排程、掃描完成及結算通知全部停用。

  1. 冪等 —— 已通知過嘅注單記喺 notify_state.json,重複執行唔會再發。
     絕對唔會改 sim_ledger.json。
  2. 自足 —— 訊息由帳本 + watch 快照直接組裝,唔靠模型在場,
     所以排程只需要跑一句 `python3 notify.py`。
用法:
    python3 notify.py              # 發未通知過嘅新注單
    python3 notify.py --dry        # 只印訊息,唔發
    python3 notify.py --window 60  # 只考慮 60 分鐘內建立嘅注單(預設 45)

舊有 --settled / --sched / --sweep / --watch 參數只會安全退出，不會發訊息。
"""
import datetime as dt
import html
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request

try:
    import model as _M
    _KNEE = _M.ODDS_CAP_KNEE
except Exception:
    _KNEE = 0.80

HERE = os.path.dirname(os.path.abspath(__file__))
LEDGER = os.path.join(HERE, "sim_ledger.json")
STATE = os.path.join(HERE, "notify_state.json")

HKT = dt.timezone(dt.timedelta(hours=8))

CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
SOURCE_ID = "telegram_bot_api__pipedream"
TOOL = "telegram_bot_api-send-text-message-or-reply"

DEFAULT_WINDOW_MIN = 45.0          # 只通知近期建立嘅注單,避免補發舊注

SIDE_TXT = {"H": "主", "A": "客", "D": "和"}
RESULT_TXT = {"Won": "贏 ✅", "Lost": "輸 ❌", "Refunded": "走水 ➖",
              "Half Won": "半贏 ✅", "Half Lost": "半輸 ❌"}


# ─────────────────────────── 狀態 ───────────────────────────
def load_state():
    if os.path.exists(STATE):
        try:
            with open(STATE, encoding="utf-8") as f:
                s = json.load(f)
            s.setdefault("bets", [])
            s.setdefault("settled", [])
            s.setdefault("queue", [])
            s.setdefault("sweeps", [])
            s.setdefault("watch", [])
            s.setdefault("reviews", [])
            return s
        except Exception:
            pass
    return {
        "bets": [], "settled": [], "queue": [], "sweeps": [], "watch": [],
        "reviews": [],
    }


def save_state(s):
    tmp = STATE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(s, f, ensure_ascii=False, indent=1)
    os.replace(tmp, STATE)


def load_ledger():
    with open(LEDGER, encoding="utf-8") as f:
        return json.load(f)


# ─────────────────────────── 工具 ───────────────────────────
def esc(x):
    return html.escape(str(x), quote=False)


def created_ts(b):
    """注單建立時間 —— created_at 缺失就用 history 第一筆。"""
    for v in (b.get("created_at"),):
        if v:
            return v
    hist = b.get("history") or []
    return hist[0].get("ts") if hist else None


def age_min(ts):
    if not ts:
        return None
    try:
        t = dt.datetime.fromisoformat(ts)
    except ValueError:
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=HKT)
    return (dt.datetime.now(HKT) - t).total_seconds() / 60.0


def money(x):
    return f"${float(x):,.0f}"


def pick_line(b):
    """可讀盤口 —— 優先用帳本 label(讓球會顯示隊名),否則自行組裝。

    label 格式係「<市場> <選項> <盤口>」,例如「讓球 域堡 0.0/+0.5」、
    「角球大小 角球大 10.5」,所以剝走市場前綴就得。
    """
    code, cond, side = b.get("code"), b.get("condition"), b.get("side")
    lbl, mkt = b.get("label"), b.get("market")
    # 讓球一律重組成選邊視角 ——馬會盤口係主隊視角,直接印原文會誤導。
    if code == "HDC" and cond:
        import model as _M
        team = b.get("home") if side == "H" else b.get("away")
        if not team and lbl:
            body = lbl[len(mkt):].strip() if mkt and lbl.startswith(mkt) else lbl
            team = body.rsplit(" ", 1)[0].strip()
        if team:
            return _M.hdc_label(team, cond, side or "H")
    if lbl:
        t = lbl[len(mkt):].strip() if mkt and lbl.startswith(mkt) else lbl.strip()
        if t:
            return t
    if code in ("HIL", "CHL"):
        return f"{'大' if side == 'H' else '細'} {cond}"
    return f"{SIDE_TXT.get(side, side or '')} {cond or ''}".strip()


def stages_of(led, mid):
    w = (led.get("watch") or {}).get(str(mid)) or {}
    return w.get("stages") or []


def drift_line(led, b):
    """三段預測有冇轉軚。"""
    order = {"首預": 0, "T-30": 1, "T-5": 2}
    st = sorted(stages_of(led, b.get("match_id")),
                key=lambda s: order.get(s.get("stage"), 9))
    if not st:
        return "三段對照:冇快照記錄"

    parts, verdicts = [], []
    for s in st:
        nm = s.get("stage") or "?"
        cv = s.get("conviction")
        vd = s.get("verdict") or "—"
        verdicts.append(vd)
        cvt = "—" if cv is None else f"{float(cv):.1f}"
        parts.append(f"{nm} 信念 {cvt} · {vd}")

    body = "\n".join("   " + esc(p) for p in parts)
    uniq = [v for i, v in enumerate(verdicts) if i == 0 or v != verdicts[i - 1]]
    turns = len(uniq) - 1
    if turns <= 0:
        tag = "三段結論一致 —— 由頭到尾同一方向"
    else:
        tag = f"轉軚 {turns} 次:" + " → ".join(esc(v) for v in uniq)
    return f"<b>三段對照</b>\n{body}\n   <i>{tag}</i>"


def stake_note(b):
    ss = b.get("stake_stage") or {}
    if not ss:
        return ""
    frac = ss.get("fraction")
    ft = "—"
    if frac:
        ft = ("1/3" if abs(frac - 1 / 3) < .01 else
              "1/2" if abs(frac - .5) < .01 else
              "2/3" if abs(frac - 2 / 3) < .01 else f"{frac:.2f}")
    mm = ss.get("market_mult")
    mmt = f" × {mm}(角球折讓)" if mm and mm != 1 else ""
    return (f"注碼階段:{esc(ss.get('label', '—'))} · "
            f"{ft} 凱利{mmt} · 單場上限 {float(ss.get('cap', 0)) * 100:.0f}%"
            f"(賠率低於 {1 + _KNEE:.2f} 按 b/{_KNEE:.2f} 遞減)")


# ─────────────────────────── 訊息 ───────────────────────────
def bet_msg(led, bets):
    n = dt.datetime.now(HKT)
    head = (f"<b>足破 · 新模擬注單</b>\n"
            f"{esc(n.strftime('%m/%d %H:%M'))} HKT · 共 {len(bets)} 注")

    blocks = []
    for b in bets:
        rows = [
            f"<b>{esc(b.get('home'))} v {esc(b.get('away'))}</b>",
            f"   {esc(b.get('league'))} · 開賽 {esc(b.get('kickoff'))} HKT",
            f"   投注：<b>{esc(b.get('market'))} · {esc(pick_line(b))}</b>",
            f"   賠率：<b>{float(b.get('odds', 0)):.2f}</b>",
            f"   注碼：<b>{esc(money(b.get('stake', 0)))}</b>",
        ]
        blocks.append("\n".join(rows))

    total = sum(float(b.get("stake") or 0) for b in bets)
    foot = f"<b>本次總注碼：{esc(money(total))}</b>\n只作模擬，絕不實際投注。"
    return "\n\n".join([head] + blocks + [foot])


def settled_msg(led, bets):
    n = dt.datetime.now(HKT)
    pnl = sum(float(b.get("pnl") or 0) for b in bets)
    head = (f"<b>📊 足破 · 賽果結算</b>\n"
            f"{esc(n.strftime('%m/%d %H:%M'))} HKT · {len(bets)} 注 · "
            f"本批盈虧 <b>{'+' if pnl >= 0 else ''}{esc(money(pnl))}</b>")
    rows = []
    for b in bets:
        p = float(b.get("pnl") or 0)
        rows.append(
            f"{esc(RESULT_TXT.get(b.get('result'), b.get('result') or '?'))}  "
            f"{esc(b.get('home'))} v {esc(b.get('away'))}\n"
            f"   {esc(b.get('market'))} {esc(pick_line(b))} @ "
            f"{float(b.get('odds', 0)):.2f} · 注 {esc(money(b.get('stake', 0)))}"
            f" → <b>{'+' if p >= 0 else ''}{esc(money(p))}</b>")
    s = led.get("stats") or {}
    hr = s.get("hit_rate")
    foot = (f"<b>累計</b> 盈虧 {esc(money(s.get('pnl') or 0))} · "
            f"ROI {('—' if s.get('roi') is None else f'{float(s['roi']) * 100:+.2f}%')} · "
            f"命中 {('—' if hr is None else f'{float(hr) * 100:.1f}% ({s.get('hits')}/{s.get('n_decided')})')} · "
            f"戶口 {esc(money(s.get('equity') if s.get('equity') is not None else led.get('bankroll')))}")
    return "\n\n".join([head] + rows + [foot])


def review_events(report):
    """Return stable, deduplicatable human-review events from a backtest report."""
    events = []
    labels = {"footbreak": "足破", "crown": "皇冠"}
    # The standalone challenger report is still notification-isolated: only a
    # completed frozen prospective v3 window that clears every existing gate
    # may enter this already-existing human-review channel.
    challenger_hil = (
        (((report.get("systems") or {}).get("crown") or {}).get("tests") or {})
        .get("HIL", {}).get("prospective_v3") or {}
    )
    if challenger_hil.get("status") == "candidate_passed_human_review_required":
        version = str(challenger_hil.get("state_version_hash") or "unknown")
        delta = challenger_hil.get("delta") or {}
        events.append({
            "key": f"prospective-v3:crown:HIL:{version}",
            "system": "皇冠",
            "kind": "HIL v3 前瞻影子模型通過",
            "detail": (
                f"凍結後 {challenger_hil.get('prospective_fixtures', 0)} 場/"
                f"{challenger_hil.get('prospective_rows', 0)} 預測 · "
                f"Brier Δ {delta.get('brier')} · "
                f"log loss Δ {delta.get('log_loss')} · "
                f"命中率 Δ {delta.get('accuracy')}"
            ),
        })
    # The Crown CHL frozen prospective shadow uses the same isolated rule: it
    # may only enter this channel after every final gate has already passed.
    challenger_chl = (
        (((report.get("systems") or {}).get("crown") or {}).get("tests") or {})
        .get("CHL", {}).get("prospective_chl") or {}
    )
    if challenger_chl.get("status") == "candidate_passed_human_review_required":
        version = str(challenger_chl.get("state_version_hash") or "unknown")
        delta = challenger_chl.get("delta") or {}
        events.append({
            "key": f"prospective-chl:crown:CHL:{version}",
            "system": "皇冠",
            "kind": "CHL 前瞻凍結影子模型通過",
            "detail": (
                f"策略 {challenger_chl.get('selected_strategy') or 'unknown'} · "
                f"凍結後 {challenger_chl.get('prospective_fixtures', 0)} 場獨立賽事"
                f"（參考 {challenger_chl.get('prospective_rows', 0)} 階段列）· "
                f"Brier Δ {delta.get('brier')} · "
                f"log loss Δ {delta.get('log_loss')} · "
                f"命中率 Δ {delta.get('accuracy')}"
            ),
        })
    for system, payload in (report.get("systems") or {}).items():
        label = labels.get(system, system)
        if payload.get("status") == "ready_for_human_review":
            baseline_matches = payload.get("baseline_matches", 0)
            events.append({
                "key": f"forward:{system}:{baseline_matches}",
                "system": label,
                "kind": "前向驗證批次完成",
                "detail": (
                    f"新賽事 {payload.get('new_matches', 0)} 場 · "
                    f"T-5 覆蓋 {float(payload.get('t5_holdout_coverage') or 0) * 100:.1f}%"
                ),
            })

        upgrade = payload.get("automatic_upgrade_test") or {}
        if upgrade.get("status") == "candidate_passed":
            candidate = str(upgrade.get("recommended_candidate") or "unknown")
            events.append({
                "key": f"upgrade:{system}:{candidate}",
                "system": label,
                "kind": "整體挑戰者通過",
                "detail": (
                    f"候選 {candidate} · "
                    f"已驗證 {upgrade.get('eligible_matches', 0)} 場"
                ),
            })

        market_tests = (
            (payload.get("market_model_upgrade_tests") or {}).get("tests") or {}
        )
        for market, test in market_tests.items():
            if test.get("status") != "candidate_passed_human_review_required":
                continue
            alpha = (test.get("challenger") or {}).get("calibration_alpha")
            delta = test.get("delta") or {}
            events.append({
                "key": f"market:{system}:{market}:{alpha}",
                "system": label,
                "kind": f"{market} 校準挑戰者通過",
                "detail": (
                    f"alpha {alpha} · 驗證 {test.get('eligible_matches', 0)} 場 · "
                    f"Brier Δ {delta.get('brier')} · "
                    f"命中率 Δ {delta.get('accuracy')}"
                ),
            })
        model_tests = (payload.get("model_challenger_tests") or {}).get("tests") or {}
        for market, test in model_tests.items():
            if test.get("status") != "candidate_passed_human_review_required":
                continue
            delta = test.get("delta") or {}
            version = str(test.get("model_version_hash") or "unknown")
            events.append({
                "key": f"challenger:{system}:{market}:{version}",
                "system": label,
                "kind": f"{market} 特徵挑戰模型通過",
                "detail": (
                    f"模型 {str(test.get('model_version') or 'unknown')} · "
                    f"驗證 {test.get('holdout_fixtures', 0)} 場/{test.get('holdout_rows', 0)} 預測 · "
                    f"Brier Δ {delta.get('brier')} · "
                    f"log loss Δ {delta.get('log_loss')} · "
                    f"命中率 Δ {delta.get('accuracy')}"
                ),
            })
    return events


def review_msg(events, generated_at=None):
    rows = [
        "<b>足破 · 模型候選等待人工審核</b>",
        esc(generated_at or dt.datetime.now(HKT).isoformat(timespec="minutes")),
        "",
    ]
    for event in events:
        rows.extend([
            f"<b>{esc(event['system'])} · {esc(event['kind'])}</b>",
            f"   {esc(event['detail'])}",
            "",
        ])
    rows.append("模型未有自動套用；請人工審核後先決定是否升級。")
    return "\n".join(rows)


# ─────────────────────────── 發送 ───────────────────────────
def send(text):
    if not CHAT_ID:
        raise RuntimeError("TELEGRAM_CHAT_ID 未設定")
    if BOT_TOKEN:
        body = json.dumps({
            "chat_id": CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                result = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Telegram 直接發送失敗:{type(exc).__name__}") from exc
        if not result.get("ok"):
            raise RuntimeError(f"Telegram 直接發送失敗:{result.get('description', 'unknown')}")
        return "telegram_bot_api_ok"

    # Backward-compatible local fallback only. Production should always use
    # the long-lived bot token so delivery does not depend on an expiring
    # external-tool session.
    payload = json.dumps({
        "source_id": SOURCE_ID, "tool_name": TOOL,
        "arguments": {"chatId": CHAT_ID, "text": text, "parse_mode": "HTML"},
    }, ensure_ascii=False)
    p = subprocess.run(["external-tool", "call", payload],
                       capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(f"Telegram 發送失敗:{p.stderr.strip()[:400]}")
    return p.stdout.strip()[:200]


# ─────────────────────────── 主流程 ───────────────────────────
def new_bets(led, state, window):
    out = []
    for b in led.get("bets", []):
        if b.get("status") != "PENDING":
            continue
        bid = b.get("bet_id")
        if not bid or bid in state["bets"]:
            continue
        a = age_min(created_ts(b))
        if a is None or a > window:
            continue
        out.append(b)
    out.sort(key=lambda b: b.get("kickoff") or "")
    return out


def new_settled(led, state):
    out = [b for b in led.get("bets", [])
           if b.get("status") == "SETTLED"
           and b.get("bet_id") and b["bet_id"] not in state["settled"]]
    out.sort(key=lambda b: b.get("kickoff") or "")
    return out


# ─────────────────── 排程佇列 / 每晚總結 ───────────────────
def read_queue(n=12):
    """跑 next_due.py 攞應有嘅預測時點佇列。失敗回傳 None。"""
    try:
        out = subprocess.run(
            [sys.executable, os.path.join(HERE, "next_due.py"),
             "--queue", str(n)],
            capture_output=True, text=True, timeout=180, cwd=HERE)
        if out.returncode != 0:
            return None
        return json.loads(out.stdout)
    except Exception:
        return None


def hhmm(iso):
    """'2026-08-08T00:28:00' → '08/08 00:28'"""
    try:
        d = dt.datetime.fromisoformat(iso)
        return d.strftime("%m/%d %H:%M")
    except Exception:
        return str(iso)


def sched_msg(q, added, dropped, note):
    rows = q.get("queue") or []
    now = dt.datetime.now(HKT)
    L = ["<b>🗓 足破 · 預測時點佇列更新</b>",
         "%s HKT" % now.strftime("%m/%d %H:%M"), ""]

    if added:
        L.append("<b>新排入 %d 個時點</b>" % len(added))
        for r in added[:8]:
            L.append("   ＋ %s　%s" % (hhmm(r["run_at"]), esc(r.get("label") or "")))
        if len(added) > 8:
            L.append("   … 另有 %d 個" % (len(added) - 8))
        L.append("")

    if dropped:
        L.append("<b>移出佇列 %d 個</b>(場次改期、取消,或者已經做完)" % len(dropped))
        for r in dropped[:6]:
            L.append("   － %s" % hhmm(r))
        if len(dropped) > 6:
            L.append("   … 另有 %d 個" % (len(dropped) - 6))
        L.append("")

    if rows:
        nxt = rows[0]
        L.append("下一個時點  <b>%s</b>" % hhmm(nxt["run_at"]))
        L.append("   %s" % esc(nxt.get("label") or ""))
        mins = nxt.get("in_min")
        if isinstance(mins, (int, float)):
            L.append("   %.0f 分鐘後" % mins)
        L.append("")
        L.append("佇列現有 <b>%d</b> 個時點,排到 <b>%s</b>"
                 % (len(rows), hhmm(rows[-1]["run_at"])))
    else:
        L.append("佇列而家係空 —— 暫時冇待做嘅 T-30 / T-5。")

    tw = q.get("total_wakes")
    if isinstance(tw, int) and rows and tw > len(rows):
        L.append("未來全板總共仲有 %d 個時點待排,之後會分批補上。" % tw)

    if note:
        L += ["", esc(note)]
    return "\n".join(L)


def sweep_msg(led, q):
    now = dt.datetime.now(HKT)
    watch = led.get("watch") or {}
    bets = led.get("bets") or []
    s = led.get("stats") or {}

    # 今日內做嘅首預
    today = now.date()
    fresh = 0
    for w in watch.values():
        for st in (w.get("stages") or []):
            if st.get("stage") != "首預":
                continue
            try:
                if dt.datetime.fromisoformat(st["ts"]).date() == today:
                    fresh += 1
            except Exception:
                pass
            break

    # 未來 24 小時開賽場數
    soon = 0
    for w in watch.values():
        try:
            ko = dt.datetime.strptime(w["kickoff"], "%Y-%m-%d %H:%M").replace(tzinfo=HKT)
        except Exception:
            continue
        if 0 <= (ko - now).total_seconds() <= 86400:
            soon += 1

    pend = [b for b in bets if b.get("status") == "PENDING"]
    open_stake = sum(b.get("stake") or 0 for b in pend)
    equity = s.get("equity") or 0
    pnl = s.get("pnl") or 0
    n_set = s.get("n_settled") or 0
    n_dec = s.get("n_decided") or 0
    hits = s.get("hits") or 0

    L = ["<b>🌙 足破 · 全板首預完成</b>",
         "%s HKT" % now.strftime("%m/%d %H:%M"), "",
         "<b>今晚掃板</b>",
         "   新做首預 <b>%d</b> 場" % fresh,
         "   追蹤中共 <b>%d</b> 場 · 未來 24 小時開賽 <b>%d</b> 場" % (len(watch), soon),
         "",
         "<b>倉位</b>",
         "   待決注單 <b>%d</b> 注 · 在場注碼 <b>%s</b>" % (len(pend), money(open_stake)),
         "   戶口 <b>%s</b> · 累計盈虧 <b>%s%s</b>"
         % (money(equity), "+" if pnl >= 0 else "-", money(abs(pnl)))]

    if n_dec:
        L.append("   已結算 %d 注 · 命中 %d/%d(%.0f%%) · ROI %.1f%%"
                 % (n_set, hits, n_dec, 100.0 * hits / n_dec,
                    100.0 * (s.get("roi") or 0)))
    else:
        L.append("   仲未有已結算注單 —— 樣本累積中")

    L += ["", "<b>注碼階段</b>", "   " + stake_note_plain()]

    rows = (q or {}).get("queue") or []
    L.append("")
    if rows:
        L.append("<b>明日時點</b>")
        L.append("   下一個 <b>%s</b> · %s" % (hhmm(rows[0]["run_at"]),
                                              esc(rows[0].get("label") or "")))
        L.append("   佇列 %d 個,排到 %s" % (len(rows), hhmm(rows[-1]["run_at"])))
    else:
        L.append("<b>明日時點</b>  暫時未有待做嘅 T-30 / T-5")

    L += ["", "落注一律只喺開賽前 5 分鐘決定。"]
    return "\n".join(L)


def stake_note_plain():
    """注碼階段一句摘要,獨立於注單。"""
    try:
        import staking as K
        st = K.stage()
    except Exception:
        return "讀取失敗"
    frac = st.get("fraction") or 0
    fr = None
    for val, txt in ((1 / 3, "1/3"), (0.5, "1/2"), (2 / 3, "2/3")):
        if abs(frac - val) < 1e-3:
            fr = txt
            break
    if fr is None:
        fr = "%.2f" % frac
    bits = ["%s · %s 凱利 · 單場上限 %.0f%%"
            % (esc(st.get("label") or ""), fr, 100.0 * (st.get("cap") or 0))]
    n = st.get("n_settled")
    sl = st.get("slope")
    if n is not None:
        tail = "已結算 %d 注" % n
        if sl is not None:
            tail += " · 校準斜率 %.2f" % sl
        bits.append(tail)
    return "\n   ".join(bits)



def main(argv):
    dry = "--dry" in argv
    mode_review = "--review" in argv
    mode_settled = "--settled" in argv
    mode_sched = "--sched" in argv
    mode_sweep = "--sweep" in argv
    mode_watch = "--watch" in argv
    note = ""
    if "--note" in argv:
        note = argv[argv.index("--note") + 1]
    window = DEFAULT_WINDOW_MIN
    if "--window" in argv:
        window = float(argv[argv.index("--window") + 1])

    if mode_review:
        report_path = (
            argv[argv.index("--report") + 1]
            if "--report" in argv
            else "/var/lib/footbreak/backtest/latest.json"
        )
        if not os.path.isfile(report_path):
            print("回測報告不存在 —— 不發送")
            return 1
        with open(report_path, encoding="utf-8") as handle:
            report = json.load(handle)
        state = load_state()
        sent = set(state.get("reviews") or [])
        events = [event for event in review_events(report) if event["key"] not in sent]
        if not events:
            print("未有新通過門檻嘅模型候選 —— 不發送")
            return 0
        text = review_msg(events, report.get("generated_at"))
        if dry:
            print(text)
            print(f"\n[dry-run] 會通知 {len(events)} 個審核項目,唔會寫狀態")
            return 0
        send(text)
        state["reviews"] = (
            (state.get("reviews") or []) + [event["key"] for event in events]
        )[-200:]
        state["last_sent"] = dt.datetime.now(HKT).isoformat(timespec="seconds")
        save_state(state)
        print(f"已發模型人工審核通知({len(events)} 項)")
        return 0

    # User preference: Telegram is for new bets and passed model-review gates.
    # Keep the old formatters for historical compatibility, but never send
    # watch/schedule/sweep/settlement messages.
    if mode_watch or mode_sched or mode_sweep or mode_settled:
        print("此通知類型已停用；Telegram 只發新注單")
        return 0

    led = load_ledger()
    state = load_state()

    if mode_watch:
        import watch_msg as WM
        w = float(argv[argv.index("--window") + 1]) if "--window" in argv \
            else WM.WATCH_WINDOW_MIN
        rows = WM.collect(led, state, w)
        if not rows:
            print(f"冇 {w:.0f} 分鐘內新做而未通知嘅 T-5 觀望場 —— 不發送")
            return 0
        text = WM.build(led, rows)
        if dry:
            print(text)
            print(f"\n[dry-run] 會通知 {len(rows)} 場觀望,唔會寫狀態")
            return 0
        send(text)
        state["watch"] = ((state.get("watch") or [])
                          + [k for k, _w, _s in rows])[-800:]
        state["last_sent"] = dt.datetime.now(HKT).isoformat(timespec="seconds")
        save_state(state)
        print(f"已發 T-5 觀望通知({len(rows)} 場)")
        return 0

    if mode_sched:
        q = read_queue(12)
        if q is None:
            print("next_due.py 讀唔到 —— 不發送")
            return 1
        rows = q.get("queue") or []
        cur = [r["run_at"] for r in rows]
        prev = state.get("queue") or []
        added = [r for r in rows if r["run_at"] not in prev]
        dropped = [t for t in prev if t not in cur]
        if not added and not dropped:
            print("排程佇列無變動 —— 不發送")
            return 0
        text = sched_msg(q, added, dropped, note)
        if dry:
            print(text)
            return 0
        send(text)
        state["queue"] = cur
        state["last_sent"] = dt.datetime.now(HKT).isoformat(timespec="seconds")
        save_state(state)
        print("已發排程更新(新增 %d,移出 %d)" % (len(added), len(dropped)))
        return 0

    if mode_sweep:
        day = dt.datetime.now(HKT).strftime("%Y-%m-%d")
        if day in (state.get("sweeps") or []) and not dry:
            print("今日 (%s) 已經發過全板總結 —— 不重複發送" % day)
            return 0
        text = sweep_msg(led, read_queue(12))
        if dry:
            print(text)
            return 0
        send(text)
        state["sweeps"] = ((state.get("sweeps") or []) + [day])[-30:]
        # 總結入面已經報咗明日時點,同步佇列基準,避免緊接住又發一次佇列更新
        _q = read_queue(12) or {}
        state["queue"] = [r["run_at"] for r in (_q.get("queue") or [])]
        state["last_sent"] = dt.datetime.now(HKT).isoformat(timespec="seconds")
        save_state(state)
        print("已發全板首預總結 (%s)" % day)
        return 0

    if mode_settled:
        sel = new_settled(led, state)
        if not sel:
            print("冇未通知嘅結算注單 —— 不發送")
            return 0
        text, key = settled_msg(led, sel), "settled"
    else:
        sel = new_bets(led, state, window)
        if not sel:
            print(f"冇 {window:.0f} 分鐘內新建立而未通知嘅注單 —— 不發送")
            return 0
        text, key = bet_msg(led, sel), "bets"

    if dry:
        print(text)
        print(f"\n[dry-run] 會通知 {len(sel)} 注,唔會寫狀態")
        return 0

    send(text)
    state[key].extend(b["bet_id"] for b in sel)
    state[key] = state[key][-500:]
    state["last_sent"] = dt.datetime.now(HKT).isoformat(timespec="seconds")
    save_state(state)
    print(f"已通知 {len(sel)} 注（{key}）")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
