import json
import sqlite3
import unittest
from unittest.mock import patch
import observation_store
import strategy_runtime


class ObservationStoreTests(unittest.TestCase):
    def database(self):
        db=sqlite3.connect(":memory:")
        db.row_factory=sqlite3.Row
        db.execute("""CREATE TABLE observations(sid TEXT,rule_id TEXT,first_seen_at INTEGER,
                   origin TEXT,payload TEXT,result_json TEXT,PRIMARY KEY(sid,rule_id))""")
        self.addCleanup(db.close)
        return db

    def legacy(self,db,observations,now,activated):
        for h,result in observations:
            db.execute("INSERT OR IGNORE INTO observations VALUES(?,?,?,?,?,?)",
                       (h["sid"],h["rule_id"],now,"historical_seed" if h["ko"]<=activated else "observed",
                        json.dumps(h,ensure_ascii=False),json.dumps(result,ensure_ascii=False) if result else None))
            if result:
                db.execute("UPDATE observations SET result_json=? WHERE sid=? AND rule_id=?",
                           (json.dumps(result,ensure_ascii=False),h["sid"],h["rule_id"]))
        db.commit()

    def test_exact_equivalence_new_pending_settled_correction_and_replay(self):
        old,new=self.database(),self.database()
        a={"sid":"1","rule_id":"D1","ko":1,"text":"繁體"}
        b={"sid":"2","rule_id":"D2","ko":200}
        phases=[[(a,None),(b,None)],
                [(a,{**a,"result":"W","pnl":.8}),(b,None)],
                [(a,{**a,"result":"L","pnl":-1}),(b,{**b,"result":"P","pnl":0})],
                [({**a,"text":"新版"},None),(b,None)]]
        for rows in phases:
            self.legacy(old,rows,300,100)
            observation_store.persist(new,rows,300,100)
            query="SELECT * FROM observations ORDER BY sid,rule_id"
            self.assertEqual([tuple(r) for r in old.execute(query)],
                             [tuple(r) for r in new.execute(query)])
        self.assertEqual(observation_store.persist(new,phases[2],500,100)["changed"],0)

    def test_grouped_collect_matches_original_including_inactive_and_low_price(self):
        from dynamic_rules import matched_rows
        rows=[{"sid":str(i),"ko":100,"market":market,"side":side,"hk":price,
               "features":{"tier":"其他"},"six_t5_min_at":1,"result":result}
              for i,(market,side,price,result) in enumerate([
                  ("OU","over",.8,"W"),("OU","under",.9,None),
                  ("AH","home",.69,"W"),("OU","over",.7,None)])]
        rules=[{"id":str(i),"market":market,"side":side,"active":active,"version":1,
                "version_at":0,"lock_ids":[str(i)],"description":"條件",
                "clauses":[[{"family":"tier","op":"eq","value":"其他"}]]}
               for i,(market,side,active) in enumerate([
                   ("OU","over",True),("OU","under",False),("AH","home",True)])]
        history={};live=[];observations=[]
        for s in rules:
            history[s["id"]]=[]
            for r in matched_rows(rows,s):
                hit={**r,"rule_id":s["id"],"strategy_version":s["version"],
                     "strategy_description":s["description"],"strategy_lock_ids":s["lock_ids"]}
                result=hit if hit["result"] else None
                observations.append((hit,result))
                if result:
                    history[s["id"]].append(result)
                if s["active"] and r["ko"]>50 and r["six_t5_min_at"]>=s["version_at"]:
                    live.append(hit)
        with patch.object(strategy_runtime,"universe",return_value=(rows,{})):
            actual=strategy_runtime.collect({"strategies":rules},(None,None,{},None),50)
        self.assertEqual(actual,(history,live,observations,{},{}))
