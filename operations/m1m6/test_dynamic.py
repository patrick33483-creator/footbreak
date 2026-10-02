import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import dynamic_rules as d
import strategy_runtime as rt
import notifier
from policy import gates,key


class DynamicTests(unittest.TestCase):
    def rule(self,rid,locks=None):
        return {"id":rid,"active":True,"version":1,"version_at":0,
                "lock_ids":locks or [rid],"market":"OU","side":"under","clauses":[[d.atom("hour",0)]]}

    def hit(self,rid,sid,ko):
        return {"rule_id":rid,"sid":sid,"ko":ko,"market":"OU","side":"under",
                "line":3.25,"hk":.8,"six_t5_min_at":10,
                "strategy_description":"test","strategy_version":1}

    def test_independent_locks_allow_other_strategy_and_same_kickoff(self):
        rules=[self.rule("M1"),self.rule("M2")]
        live=[self.hit("M1","1",10000),self.hit("M1","2",20000),self.hit("M2","3",20000)]
        pending=[{"ko":10000,"lock_ids":["M1"]}]
        sel=rt.choose(live,{x["id"]:{"pass":True} for x in rules},100,0,pending,rules,{})
        self.assertEqual({h["sid"] for h in sel},{"1","3"})

    def test_earliest_only_with_same_kickoff_append(self):
        rules=[self.rule("M1")]
        hits=[self.hit("M1","1",10000),self.hit("M1","2",10000),self.hit("M1","3",20000)]
        self.assertEqual(len(rt.choose(hits,{"M1":{"pass":True}},100,0,[],rules,{})),2)

    def test_shared_lineage_cannot_bypass_in_same_scan_or_next_scan(self):
        rules=[self.rule("D1",["M1","D1"]),self.rule("D2",["M1","D2"])]
        hits=[self.hit("D1","1",10000),self.hit("D2","2",20000)]
        g={r["id"]:{"pass":True} for r in rules}
        self.assertEqual(len(rt.choose(hits,g,100,0,[],rules,{})),1)
        self.assertEqual(rt.choose([hits[1]],g,100,0,[{"ko":10000,"lock_ids":["M1"]}],rules,{}),[])

    def test_gate_cutover_and_deadline(self):
        r=self.rule("M1");h=self.hit("M1","1",10000)
        self.assertEqual(rt.choose([h],{"M1":{"pass":False}},100,0,[],[r],{}),[])
        self.assertEqual(rt.choose([h],{"M1":{"pass":True}},100,11,[],[r],{}),[])
        self.assertEqual(rt.choose([h],{"M1":{"pass":True}},9000,0,[],[r],{}),[])

    def test_price_floor_in_live_selection_and_feature_rows(self):
        r=self.rule("M1");h=self.hit("M1","1",10000);h["hk"]=.69
        self.assertEqual(rt.choose([h],{"M1":{"pass":True}},100,0,[],[r],{}),[])
        h["hk"]=.70
        self.assertEqual(len(rt.choose([h],{"M1":{"pass":True}},100,0,[],[r],{})),1)
        from test_policy import RulesTest
        t=RulesTest();t.setUp()
        t.s[("T5","OU")]["home_odds"]=.69
        t.s[("T5","OU")]["away_odds"]=.70
        rows,_=d.feature_rows(t.m,t.s,{}, {},t.now)
        self.assertNotIn(("OU","over"),[(x["market"],x["side"]) for x in rows])
        self.assertIn(("OU","under"),[(x["market"],x["side"]) for x in rows])
        self.assertTrue(all(x["hk"]>=.7 for x in rows))

    def test_low_price_history_cannot_qualify_scanner_or_existing_strategy(self):
        rows=[{"sid":str(i),"ko":i,"market":"OU","side":"under","line":3.25,
               "features":{"hour":0},"result":"W","pnl":.69,"hk":.69} for i in range(30)]
        with patch.object(d,"atoms_for",return_value=[]):
            found,_=d.scan(rows)
        self.assertEqual(found,[])
        old={"strategies":[{**self.rule("M1"),"members":[]}],"sequence":0}
        reg=d.registry([],rows,old,100)
        self.assertEqual(reg["existing_strategy_checks"][0]["gate"]["20"]["n"],0)
        self.assertFalse(reg["strategies"][0]["active"])
        self.assertEqual(reg["strategies"][0]["lock_ids"],["M1"])
        for row in rows:
            row.update(hk=.70,pnl=.70)
        with patch.object(d,"atoms_for",return_value=[]):
            found,_=d.scan(rows)
        self.assertEqual(len(found),1)

    def test_one_bet_multiple_reasons_and_deduped_existing(self):
        rules=[self.rule("M1"),self.rule("M2")]
        hits=[self.hit(r["id"],"1",10000) for r in rules]
        g={r["id"]:{"pass":True} for r in rules}
        selected=rt.choose(hits,g,100,0,[],rules,{})
        self.assertEqual(len(selected),1)
        self.assertEqual(selected[0]["rules"],["M1","M2"])
        old={key(hits[0]):{"status":"sent","result_json":None}}
        self.assertEqual(len(rt.choose(hits,g,100,0,[],rules,old)),1) # associate, never resend
        old[key(hits[0])]["result_json"]="{}"
        self.assertEqual(rt.choose(hits,g,100,0,[],rules,old),[])

    def test_links_wait_all_results_and_settle_independently(self):
        with tempfile.TemporaryDirectory() as tmp,patch.object(notifier,"STATE",Path(tmp)):
            db=notifier.state_db();rt.schema(db)
            rules=[self.rule("M1"),self.rule("M2")]
            hits=[self.hit("M1","1",10000),self.hit("M1","2",10000),self.hit("M2","3",20000)]
            selected=rt.choose(hits,{r["id"]:{"pass":True} for r in rules},100,0,[],rules,{})
            notifier.prepare_items(db,selected,100);rt.attach(db,selected,rules,100)
            db.execute("UPDATE items SET status='sent'");db.commit()
            self.assertEqual(len(rt.reconcile(db,200)),2)
            db.execute("UPDATE items SET result_json='{}' WHERE sid IN ('1','3')");db.commit()
            p=rt.reconcile(db,300)
            self.assertEqual([b["strategy_id"] for b in p],["M1"])
            self.assertEqual(p[0]["waiting"],1)
            db.execute("UPDATE items SET result_json='{}' WHERE sid='2'");db.commit()
            self.assertEqual(rt.reconcile(db,400),[])
            db.close()

    def test_merge_or_recomputed_and_stable_id(self):
        rows=[{"sid":str(i),"ko":i,"market":"OU","side":"under","line":3.25,
               "features":{"hour":0,"ou_line":3.25,"ah_line":0},"result":"W","pnl":.8,"hk":.8} for i in range(30)]
        a={"market":"OU","side":"under","clauses":[[d.atom("hour",0),d.atom("ou_line",3.25),d.atom("ah_line",.75,"le")]]}
        b=copy.deepcopy(a);b["clauses"][0][-1]=d.atom("ah_line",0,"le")
        for c in (a,b):
            c["fingerprint"]=d.signature("OU_under",c["clauses"]);c["gate"]=gates(rows)
        groups=d.merge([a,b],rows)
        self.assertEqual(len(groups),1)
        self.assertEqual(len(groups[0]["members"]),2)
        self.assertEqual(groups[0]["gate"]["20"]["n"],20)
        reg=d.registry(groups,rows,{},100)
        again=d.registry(groups,rows,reg,200)
        active=lambda r:[s for s in r["strategies"] if s["active"]][0]
        self.assertEqual(active(reg)["id"],active(again)["id"])
        self.assertEqual(active(reg)["version"],active(again)["version"])
        self.assertEqual(active(reg)["version_at"],active(again)["version_at"])

    def test_model_future_or_wrong_line_unavailable(self):
        cp={"T5":{"locked_at_ms":999,"prediction":{"pred_ah":"主","fair_lines":{"ah_h":0,"ah_p_home_nv":.6}}}}
        self.assertEqual(d.saved_model(cp,"T5","AH",1000,900,0),(None,None))
        self.assertEqual(d.saved_model(cp,"T5","AH",1000,999,.25),("home",None))

    def test_seed_conditions_equivalent_to_original(self):
        from test_policy import RulesTest
        t=RulesTest();t.setUp()
        rows,_=d.feature_rows(t.m,t.s,{}, {},t.now)
        selected=[s["id"] for s in d.seeds() if d.matched_rows(rows,s)]
        self.assertEqual(selected,["M1"])

    def test_existing_inactive_strategy_always_recalculated(self):
        old={"strategies":[{**self.rule("D1"),"born_at":0,"active":False,"members":[]}],"sequence":1}
        rows=[{"sid":str(i),"ko":i,"market":"OU","side":"under","line":3.25,
               "features":{"hour":0},"result":"W" if i<19 else "L","pnl":.8 if i<19 else -1,"hk":.8} for i in range(20)]
        reg=d.registry([],rows,old,100)
        self.assertTrue(reg["existing_strategy_checks"][0]["gate"]["20"]["pass"])
        self.assertEqual(reg["strategies"][0]["gate"]["20"]["wins"],19)
        rows[-2].update(result="L",pnl=-1)
        reg=d.registry([],rows,reg,200)
        self.assertTrue(reg["existing_strategy_checks"][0]["gate"]["20"]["pass"])
        rows[-3].update(result="L",pnl=-1)
        reg=d.registry([],rows,reg,300)
        self.assertFalse(reg["existing_strategy_checks"][0]["gate"]["20"]["pass"])

    def test_scanner_and_runtime_share_new_boundaries(self):
        for n,wins,expected in [(20,18,True),(20,17,False),(30,26,True),(30,25,False)]:
            rows=[{"sid":str(i),"ko":i,"market":"OU","side":"under","line":3.25,
                   "features":{},"result":"W" if i<wins else "L",
                   "pnl":.8 if i<wins else -1,"hk":.8} for i in range(n)]
            with patch.object(d,"atoms_for",return_value=[]):
                found,_=d.scan(rows)
            self.assertEqual(bool(found),expected,(n,wins))
            self.assertEqual(gates(rows)["pass"],expected)

    def test_dynamic_tick_end_to_end_dedupe_and_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);cfg=root/"config.json";rp=root/"registry.json"
            cfg.write_text(json.dumps({"enabled":True,"activated_at":0,"dynamic_activated_at":0}))
            rules=d.seeds()
            for r in rules:
                r["active"]=True
            rp.write_text(json.dumps({"updated_at":100,"gate_version":d.GATE_VERSION,"strategies":rules,"search":{},"next_search_at":10800100}))
            gate=gates([{"sid":str(i),"ko":i,"result":"W","pnl":.8,"hk":.8} for i in range(30)])
            h={**self.hit("M1","1",10000),"ko_hkt":"test","t5_at":10,
               "home":"主","away":"客","league":"test"}
            h2={**h,"sid":"2","ko":20000}
            other={**h2,"sid":"3","rule_id":"M2"}
            with patch.object(notifier,"STATE",root),patch.object(notifier,"CONFIG",cfg),\
                 patch.object(notifier,"REGISTRY",rp),patch.object(notifier,"nowms",return_value=100),\
                 patch.object(notifier,"collect",return_value=({r["id"]:[] for r in rules},[h,h2,other],[],{},{})),\
                 patch.object(notifier,"gates",return_value=gate),\
                 patch.object(notifier,"env",return_value={"TELEGRAM_BOT_TOKEN":"test","TELEGRAM_CHAT_ID":"test"}),\
                 patch.object(notifier,"send",return_value=("sent",{"date":1,"message_id":1},None)) as send,\
                 patch.object(notifier,"atomic") as publish:
                current=json.loads(rp.read_text())
                rp.write_text(json.dumps({**current,"gate_version":"old-thresholds"}))
                self.assertEqual(notifier.tick()["sent"],0)
                self.assertTrue(publish.call_args.args[1]["registry_stale"])
                rp.write_text(json.dumps(current))
                out=notifier.tick()
                self.assertEqual(out["sent"],2)
                self.assertEqual(out["strategy_pending_batches"],2)
                self.assertEqual(notifier.tick()["sent"],0)
                self.assertEqual(send.call_count,2)
                for removed in ("放行門檻","上述為已結算","本訊號只代表","每條策略獨立封鎖",
                                "不同策略可各自運行","盤價已變"):
                    self.assertNotIn(removed,send.call_args.args[0])
                self.assertIn("近20場",send.call_args.args[0])
                data=publish.call_args.args[1]
                self.assertEqual(data["mode"],"dynamic_per_strategy_batch")
                self.assertEqual(len(data["rules"]),6)

    def test_existing_passing_strategy_retained_without_new_leaf(self):
        import research_cycle
        s={**self.rule("D1"),"born_at":0,"active":False,"members":[]}
        rows=[{"sid":str(i),"ko":i,"market":"OU","side":"under","line":3.25,
               "features":{"hour":0},"result":"W","pnl":.8,"hk":.8} for i in range(20)]
        with patch.object(research_cycle.notifier,"source",return_value=None),\
             patch.object(research_cycle,"universe",return_value=(rows,{})),\
             patch.object(research_cycle,"scan",return_value=([],{"enumerated":0})):
            out=research_cycle.compute(100,{"strategies":[s],"sequence":1})
        self.assertEqual(out["search"]["existing_rechecked"],1)
        self.assertTrue(out["strategies"][0]["active"])

    def test_refresh_api_auth_origin_and_busy_dedup(self):
        import http.client,threading
        from types import SimpleNamespace
        from http.server import ThreadingHTTPServer
        from refresh_api import Handler
        server=ThreadingHTTPServer(("127.0.0.1",0),Handler)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        host=f"127.0.0.1:{server.server_port}"
        def post(headers):
            c=http.client.HTTPConnection(host);c.request("POST","/refresh",headers=headers)
            r=c.getresponse();code=r.status;data=json.loads(r.read());c.close();return code,data
        try:
            self.assertEqual(post({})[0],403)
            headers={"X-Crown-Refresh":"1","X-Authenticated-User":"test","Origin":"http://evil.test"}
            self.assertEqual(post(headers)[0],403)
            headers["Origin"]="http://"+host
            with patch("refresh_api.subprocess.run",return_value=SimpleNamespace(stdout="active",returncode=0)) as run:
                status,data=post(headers)
                self.assertEqual(status,202);self.assertTrue(data["busy"]);self.assertEqual(run.call_count,1)
            with patch("refresh_api.subprocess.run",side_effect=[
                SimpleNamespace(stdout="inactive",returncode=3),SimpleNamespace(returncode=0)]) as run:
                status,data=post(headers)
                self.assertEqual(status,202);self.assertFalse(data["busy"]);self.assertEqual(run.call_count,2)
        finally:
            server.shutdown();server.server_close();thread.join()


if __name__=="__main__":
    unittest.main()
