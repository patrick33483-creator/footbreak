"""Match-wide conflict warnings accumulated across scans and registry revisions."""
import json
from policy import eligible_price,key

RELEASE_BEFORE_MS=120000
VERSION="match-conflict-warning-v1-Tminus2"
ATTEMPTED=("sent","sending","uncertain")


def schema(db):
    db.execute("""CREATE TABLE IF NOT EXISTS signal_guard(
        sid TEXT PRIMARY KEY,ko INTEGER NOT NULL,seen TEXT NOT NULL,
        conflict_at INTEGER,updated_at INTEGER NOT NULL,
        warning_attempt_at INTEGER,warning_status TEXT,warning_message_id INTEGER)""")


def filter_live(db,live,gates,now,activated_at,rules):
    """Observe BEFORE strategy-lock selection; retain signals with clear warnings."""
    schema(db)
    byrule={r["id"]:r for r in rules}
    groups={}
    for h in live:
        rule=byrule.get(h["rule_id"])
        if (not rule or not rule["active"] or not gates.get(h["rule_id"],{}).get("pass")
                or not eligible_price(h) or now+1500>=h["ko"]
                or h["six_t5_min_at"]<max(activated_at,rule["version_at"])):
            continue
        groups.setdefault(str(h["sid"]),[]).append(h)
    allowed=[]
    counts={"collecting":0,"conflict_warning":0,"ready":0}
    for sid,hits in groups.items():
        old=db.execute("SELECT seen,conflict_at FROM signal_guard WHERE sid=?",(sid,)).fetchone()
        seen=json.loads(old["seen"]) if old else {}
        conflict_at=old["conflict_at"] if old else None
        previous=db.execute("""SELECT bet_key,payload FROM items WHERE sid=?
            AND status IN ('sent','sending','uncertain')""",(sid,)).fetchall()
        # Historic successful/uncertain sends remain evidence even after registry changes.
        evidence=hits+[json.loads(r["payload"]) for r in previous]
        for h in evidence:
            sides=seen.setdefault(h["market"],{})
            sides[h["side"]]=sorted(set(sides.get(h["side"],[])) |
                                   set(h.get("rules") or [h["rule_id"]]))
        conflict=({"over","under"}<=set(seen.get("OU",{})) or
                  {"home","away"}<=set(seen.get("AH",{})))
        if conflict and conflict_at is None:
            conflict_at=now
        db.execute("""INSERT INTO signal_guard(sid,ko,seen,conflict_at,updated_at) VALUES(?,?,?,?,?)
            ON CONFLICT(sid) DO UPDATE SET seen=excluded.seen,
            conflict_at=excluded.conflict_at,updated_at=excluded.updated_at""",
            (sid,hits[0]["ko"],json.dumps(seen,ensure_ascii=False),conflict_at,now))
        if conflict_at is not None:
            counts["conflict_warning"]+=1
        if now<hits[0]["ko"]-RELEASE_BEFORE_MS:
            counts["collecting"]+=1
        else:
            allowed.extend({**h,"conflict_warning":conflict_at is not None,
                            "conflict_evidence":seen if conflict_at is not None else {}} for h in hits)
            counts["ready"]+=1
    db.commit()
    return allowed,counts


def status(db):
    exists=db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='signal_guard'").fetchone()
    conflicts=[dict(r) for r in db.execute(
        "SELECT * FROM signal_guard WHERE conflict_at IS NOT NULL ORDER BY conflict_at DESC LIMIT 20"
    )] if exists else []
    for r in conflicts:
        r["seen"]=json.loads(r["seen"])
    return {"version":VERSION,"release_before_seconds":RELEASE_BEFORE_MS//1000,
            "scope":"whole_match_warning_not_veto","recent_conflicts":conflicts}


def warning_text(seen):
    labels={"over":"買大","under":"買細","home":"買主","away":"買客"}
    lines=["條件衝突，不建議投注","同一場出現相反方向；以下只保留訊號供核對，並非建議兩邊下注。"]
    for market,pair in [("OU",("over","under")),("AH",("home","away"))]:
        sides=seen.get(market,{})
        if set(pair)<=set(sides):
            lines.append(" ／ ".join(f"{labels[s]}：{'、'.join(sides[s])}" for s in pair))
    return "\n".join(lines)


def bundle_message(hits,batch_id,seen):
    from notifier import message
    prefix=warning_text(seen)+"\n\n" if seen else ""
    text=prefix+"\n\n".join(message(h,batch_id) for h in hits)
    if len(text)>3900:
        # Telegram has a size limit. Keep the warning and directions before
        # shortening condition details; explicitly label that details are abridged.
        from policy import fmt
        labels={"over":"買大","under":"買細","home":"買主","away":"買客"}
        h=hits[0]
        rows=[f"皇冠重點策略｜同場訊號（記錄組 #{batch_id}）",
              f"{h['home']} vs {h['away']}｜{fmt(h['ko'])} 香港時間"]
        for h in hits:
            rows.append(f"{'／'.join(h['rules'])}：{labels[h['side']]}，盤線{h['line']:g}（主隊視角讓球／大小球盤），港賠{h['hk']:.2f}")
        text=prefix+"\n".join(rows)
        text=text[:3800]+"\n條件明細較長，完整版本保留於策略帳本。"
    return text


def deliver(db,fresh,groups,creds):
    """One Telegram per match, including prior opposite signals when needed.

    Warning-only updates are not new betting ledger items or new strategy locks.
    Attempt state is durable before network I/O; uncertain outcomes never auto-retry.
    """
    from notifier import send,nowms
    schema(db)
    bundles={}
    for h in fresh:
        bundles.setdefault(str(h["sid"]),[]).append(h)
    now=nowms()
    for row in db.execute("""SELECT sid,ko FROM signal_guard
          WHERE conflict_at IS NOT NULL AND warning_attempt_at IS NULL
          AND ko>? AND ko<=?""",(now+1500,now+RELEASE_BEFORE_MS)).fetchall():
        if db.execute("""SELECT 1 FROM items WHERE sid=? AND status IN
            ('sent','sending','uncertain') LIMIT 1""",(row["sid"],)).fetchone():
            bundles.setdefault(row["sid"],[])
    sent_count=0
    for sid,new in bundles.items():
        row=db.execute("SELECT * FROM signal_guard WHERE sid=?",(sid,)).fetchone()
        seen=json.loads(row["seen"]) if row and row["conflict_at"] is not None else {}
        prior=db.execute("""SELECT payload,batch_id FROM items WHERE sid=?
            AND status IN ('sent','sending','uncertain') ORDER BY attempt_at""",(sid,)).fetchall()
        payloads={key(h):h for h in ([json.loads(r["payload"]) for r in prior] if seen else [])+new}
        if not payloads:
            continue
        hits=list(payloads.values())
        ko=hits[0]["ko"]
        batch_id=groups.get(ko) or (prior[0]["batch_id"] if prior else None)
        at=nowms()
        valid=[h for h in new if eligible_price(h) and at+1500<h["ko"]]
        invalid=[h for h in new if h not in valid]
        for h in invalid:
            db.execute("UPDATE items SET status='skipped',error='price floor or kickoff deadline' WHERE bet_key=?",(key(h),))
        if at+1500>=ko or (new and not valid):
            db.commit()
            continue
        if invalid:
            hits=[h for h in hits if key(h) not in {key(x) for x in invalid}]
        for h in valid:
            db.execute("UPDATE items SET status='sending',attempt_at=? WHERE bet_key=?",(at,key(h)))
        if seen:
            db.execute("""UPDATE signal_guard SET warning_attempt_at=?,warning_status='sending'
                WHERE sid=?""",(at,sid))
        db.commit()
        status,ack,error=send(bundle_message(hits,batch_id,seen),ko,creds)
        ack_at=ack.get("date",0)*1000 if ack else None
        if status=="sent" and (not ack_at or ack_at>=ko):
            status,error="uncertain","ack missing or not pre-kickoff"
        message_id=ack.get("message_id") if ack else None
        for h in valid:
            db.execute("UPDATE items SET status=?,ack_at=?,message_id=?,error=? WHERE bet_key=?",
                       (status,ack_at,message_id,error,key(h)))
            sent_count+=status=="sent"
        if seen:
            db.execute("UPDATE signal_guard SET warning_status=?,warning_message_id=? WHERE sid=?",
                       (status,message_id,sid))
        db.commit()
    return sent_count
