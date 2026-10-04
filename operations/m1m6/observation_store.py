"""Equivalent observation persistence without rewriting unchanged history."""
import json


def persist(db,observations,now,activated_at):
    existing={(r[0],r[1]) for r in db.execute("SELECT sid,rule_id FROM observations")}
    before=db.total_changes
    for h,result in observations:
        identity=(h["sid"],h["rule_id"])
        encoded_result=json.dumps(result,ensure_ascii=False) if result else None
        if identity not in existing:
            db.execute("INSERT OR IGNORE INTO observations VALUES(?,?,?,?,?,?)",
                       (*identity,now,"historical_seed" if h["ko"]<=activated_at else "observed",
                        json.dumps(h,ensure_ascii=False),encoded_result))
            existing.add(identity)
        if result:
            # Still apply corrections and updated evidence; only identical bytes skip.
            db.execute("""UPDATE observations SET result_json=?
                          WHERE sid=? AND rule_id=? AND result_json IS NOT ?""",
                       (encoded_result,*identity,encoded_result))
    db.commit()
    return {"rows":len(observations),"changed":db.total_changes-before}
