"""T-5 觀望通知 —— 就算唔落注,都話你知我估幾多。

由 notify.py --watch 呼叫。同一個時點嘅所有觀望場打包成一條訊息,
唔會一場一條轟炸。冪等靠 notify_state.json["watch"]。
"""
import datetime as dt
from datetime import timedelta, timezone

import accuracy as A

HKT = timezone(timedelta(hours=8))
WATCH_WINDOW_MIN = 25.0     # 只報呢個時窗內啱啱做完嘅 T-5


def _esc(x):
    return (str(x if x is not None else "")
            .replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def _ts(s):
    try:
        return dt.datetime.fromisoformat(s)
    except Exception:
        return None


def collect(led, state, window=WATCH_WINDOW_MIN):
    """搵出時窗內、T-5 做咗但冇落注嘅場。回傳 [(key, w, stage)]。"""
    done = set(state.get("watch") or [])
    now = dt.datetime.now(HKT)
    out = []
    for mid, w in (led.get("watch") or {}).items():
        for st in (w.get("stages") or []):
            if st.get("stage") != "T-5":
                continue
            if st.get("verdict") == "落注" or st.get("pick"):
                continue
            key = f"{mid}:T-5"
            if key in done:
                continue
            t = _ts(st.get("ts") or "")
            if t is None:
                continue
            if (now - t).total_seconds() / 60.0 > window:
                continue
            out.append((key, w, st))
    out.sort(key=lambda r: (r[1].get("kickoff") or "", r[1].get("home") or ""))
    return out


def _fc(st):
    """由階段參數重砌分佈。失敗回 None。"""
    try:
        d = A.rebuild(st.get("final"), st.get("now"))
    except Exception:
        return None
    if not d:
        return None
    gp, cp = d.get("goals"), d.get("corners")
    gl, gh = A.band(gp) if gp else (None, None)
    cl, ch = A.band(cp) if cp else (None, None)
    return {
        "p": d["p"],
        "top": d["tops"][0] if d.get("tops") else None,
        "total": (d["lh"] + d["la"]),
        "gband": (gl, gh),
        "mu": d.get("mu"),
        "cband": (cl, ch),
        "o25": A.ou(gp, 2.5) if gp else None,
        "c95": A.ou(cp, 9.5) if cp else None,
        "btts": d.get("btts"),
    }


def build(led, rows):
    n = dt.datetime.now(HKT)
    head = (f"<b>🔭 足破 · T-5 觀望</b>\n"
            f"{_esc(n.strftime('%m/%d %H:%M'))} HKT · {len(rows)} 場冇落注\n"
            f"<i>盤口冇肉所以唔買,但我照樣有睇法 —— 記低咗,之後對數。</i>")

    blocks = []
    for _k, w, st in rows:
        fc = _fc(st)
        conv = st.get("conviction")
        line = [
            f"<b>{_esc(w.get('home'))} v {_esc(w.get('away'))}</b>",
            f"   {_esc(w.get('league'))} · 開賽 {_esc(w.get('kickoff'))} HKT"
            f" · 信念 {('—' if conv is None else f'{float(conv):.1f}')}",
        ]
        if fc:
            ph, pd_, pa = fc["p"]
            tp = fc["top"]
            sc = f"{tp[0]}-{tp[1]}" if tp else "—"
            line.append(
                f"   估 <b>{sc}</b> · 主 {ph * 100:.0f}% 和 {pd_ * 100:.0f}%"
                f" 客 {pa * 100:.0f}%")
            gl, gh = fc["gband"]
            gtxt = (f"   入球 {fc['total']:.1f}"
                    + (f"(八成 {gl}–{gh})" if gl is not None else ""))
            if fc["o25"] is not None:
                gtxt += f" · 大2.5 {fc['o25'] * 100:.0f}%"
            if fc["btts"] is not None:
                gtxt += f" · 兩隊入球 {fc['btts'] * 100:.0f}%"
            line.append(gtxt)
            if fc["mu"]:
                cl, ch = fc["cband"]
                ctxt = (f"   角球 {fc['mu']:.1f}"
                        + (f"(八成 {cl}–{ch})" if cl is not None else ""))
                if fc["c95"] is not None:
                    ctxt += f" · 大9.5 {fc['c95'] * 100:.0f}%"
                line.append(ctxt)
        ld = st.get("lead") or {}
        if ld:
            ev = ld.get("ev")
            line.append(
                f"   最佳候選 {_esc(ld.get('market'))} {_esc(ld.get('label'))}"
                f" @{float(ld.get('odds') or 0):.2f}"
                + (f" · EV {float(ev) * 100:+.1f}%" if ev is not None else ""))
        rsn = st.get("no_bet_reason")
        if rsn:
            line.append(f"   ❌ {_esc(rsn)}")
        blocks.append("\n".join(line))

    foot = ("<i>呢啲場唔會入模擬倉,但每一句估計都寫咗落記錄,"
            "喺儀表板「純預測」分頁可以睇返準繩度。</i>")
    return "\n\n".join([head] + blocks + [foot])
