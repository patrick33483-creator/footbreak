function formatHeavyMsg(hit) {
  // Display-only change: selection rules, dispatch deadlines and dedupe are unchanged.
  let fixture = hit;
  if ((!hit.league || !hit.home || !hit.away || !hit.kickoff_utc) && hit.sid) {
    const stored = db.prepare("SELECT league,home,away,kickoff_utc FROM matches WHERE sid=?").get(hit.sid);
    if (stored) fixture = {...stored, ...Object.fromEntries(Object.entries(hit).filter(([,v])=>v!=null&&v!==""))};
  }
  if (!fixture.league || !fixture.home || !fixture.away || !Number.isFinite(Number(fixture.kickoff_utc))) {
    throw new Error("TG required fixture fields missing; refuse incomplete betting message");
  }
  if (!hit.rules?.length) throw new Error("TG selection missing");
  const names = {"ch-Alow":"A+低盤", "ch-N2":"新2", "ch-BI":"B∩I",
    "ch-U075-v2":"受讓0.75買細"};
  const lines = [
    "<b>皇冠賽前投注通知</b>",
    `<b>聯賽：${escapeHtml(fixture.league)}</b>`,
    `主隊：${escapeHtml(fixture.home)}`,
    `客隊：${escapeHtml(fixture.away)}`,
    `開賽：${formatKickoffHKT(Number(fixture.kickoff_utc))}（香港時間）`,
  ];
  const detail = [];
  for (const r of hit.rules) {
    const m = r.match || {};
    const pick = m.pick || r.pick || ({H:"home",A:"away",O:"over",U:"under"}[r.side]);
    const market = r.market;
    if (!(market === "AH" ? ["home","away"] : market === "OU" ? ["over","under"] : []).includes(pick)) {
      throw new Error("TG selected side invalid");
    }
    // Channel matches already contain Crown prices. Do not replace them with
    // Pinnacle, the opposite side, or the home trigger price on reverse bets.
    const crown = String(r.id||"").startsWith("ch-")
      ? {line:m.t5Line,dec:m.t5Dec}
      : lookupCrownT5(hit.signal,market,m.t5Line,pick);
    const line = Number(crown?.line), price = Number(crown?.dec);
    if (crown?.line == null || crown?.dec == null || !Number.isFinite(line) ||
        !Number.isFinite(price) || price <= 1) throw new Error("TG actual Crown line/price missing");
    let choice, lineText;
    if (market === "AH") {
      const team = pick === "home" ? fixture.home : fixture.away;
      choice = `${pick === "home" ? "買主" : "買客"}（${escapeHtml(team)}）`;
      const selectedLine = pick === "home" ? line : -line;
      lineText = selectedLine === 0 ? "平手（0）" :
        selectedLine < 0 ? `所選球隊讓 ${Math.abs(selectedLine)} 球（${selectedLine}）` :
        `所選球隊受讓 ${selectedLine} 球（+${selectedLine}）`;
    } else {
      choice = `${pick === "over" ? "買大" : "買細"}（全場入球）`;
      lineText = `${line} 球`;
    }
    const name = names[r.id] || r.label || r.id;
    lines.push("", `<b>買乜：${choice}</b>`, `<b>盤口：${lineText}</b>`,
      `<b>賠率：${price.toFixed(2)}（皇冠十進制）</b>`,
      `策略：${escapeHtml(name)}${r.id === "ch-Alow" && pick === "away" ? "｜反向買客" : ""}`);
    if (r.id === "ch-Alow" && Number.isFinite(m.triggerHomeDec)) {
      detail.push(`A+觸發主賠：${m.triggerHomeDec.toFixed(2)}；${escapeHtml(m.priceBand||"")}`);
    }
    if (r.label) detail.push(`${escapeHtml(name)}條件：${escapeHtml(r.label)}`);
  }
  if (detail.length) lines.push("", "條件備註：", ...detail);
  const first = hit.rules[0], pick = first.match?.pick || first.pick;
  const direct = crownDirectLink(fixture.home,fixture.away,first.market,pick);
  if (direct) {
    lines.push("", `皇冠選擇短碼：<code>${escapeHtml(fixture.home)}|${escapeHtml(fixture.away)}|${direct.sel}</code>`,
      `<a href="${CROWN_BASE_URL}">開皇冠</a>`);
  }
  lines.push("", "只供賽前參考；不會自動下注。");
  return lines.join("\n");
}

