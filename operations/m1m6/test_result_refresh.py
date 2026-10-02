import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from contextlib import closing
import result_refresh as events

class ResultRefreshTest(unittest.TestCase):
    def test_dedup_one_event_per_match_and_midcycle_event_retained(self):
        with tempfile.TemporaryDirectory() as tmp,patch.object(events,"DB",Path(tmp)/"ledger.sqlite"):
            with closing(sqlite3.connect(events.DB)) as db,db:
                events.enqueue(db,"1",100);events.enqueue(db,"1",101);events.enqueue(db,"2",101)
            ids=events.snapshot()
            self.assertEqual(ids,["1","2"])
            with closing(sqlite3.connect(events.DB)) as db,db:
                events.enqueue(db,"3",102)
            events.complete(ids,200)
            self.assertEqual(events.snapshot(),["3"])
            with closing(sqlite3.connect(events.DB)) as db,db:
                events.enqueue(db,"1",300)
            self.assertEqual(events.snapshot(),["3"])
    def test_idle_dispatch_once_busy_coalesces_and_failure_keeps_events(self):
        with tempfile.TemporaryDirectory() as tmp,patch.object(events,"DB",Path(tmp)/"ledger.sqlite"):
            with closing(sqlite3.connect(events.DB)) as db,db:
                events.enqueue(db,"1",1)
            with patch.object(events.subprocess,"run",return_value=SimpleNamespace(stdout="active",returncode=0)) as run:
                self.assertTrue(events.dispatch()["busy"])
                self.assertEqual(run.call_count,1)
            with patch.object(events.subprocess,"run",side_effect=[
                SimpleNamespace(stdout="inactive",returncode=3),SimpleNamespace(returncode=1)]) as run:
                self.assertFalse(events.dispatch()["requested"])
                self.assertEqual(run.call_count,2)
            self.assertEqual(events.snapshot(),["1"])
            with patch.object(events.subprocess,"run") as run:
                self.assertTrue(events.dispatch()["retry_cooldown"])
                run.assert_not_called()
    def test_empty_queue_no_service_call(self):
        with tempfile.TemporaryDirectory() as tmp,patch.object(events,"DB",Path(tmp)/"ledger.sqlite"):
            with patch.object(events.subprocess,"run") as run:
                self.assertEqual(events.dispatch(),{"pending":0})
                run.assert_not_called()
    def test_successful_dispatch_does_not_consume_before_cycle_success(self):
        with tempfile.TemporaryDirectory() as tmp,patch.object(events,"DB",Path(tmp)/"ledger.sqlite"):
            with closing(sqlite3.connect(events.DB)) as db,db:
                events.enqueue(db,"1",1);events.enqueue(db,"2",2)
            with patch.object(events.subprocess,"run",side_effect=[
                SimpleNamespace(stdout="inactive",returncode=3),SimpleNamespace(returncode=0)]) as run:
                self.assertEqual(events.dispatch(),{"pending":2,"requested":True})
                self.assertEqual(run.call_count,2)
            self.assertEqual(events.snapshot(),["1","2"])
    def test_cycle_consumes_only_after_success_and_uses_two_hours(self):
        import research_cycle as cycle
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            with patch.object(cycle,"STATE",root),patch.object(cycle,"REGISTRY",root/"registry.json"),\
                 patch.object(cycle,"progress"),patch.object(cycle.notifier,"atomic"),\
                 patch.object(events,"snapshot",return_value=["1","2"]),\
                 patch.object(events,"complete") as complete,\
                 patch.object(cycle.time,"time",return_value=100),\
                 patch.object(cycle,"compute",return_value={"search":{},"updated_at":100000}) as compute:
                r=cycle.cycle(sync=False)
                self.assertEqual(r["next_search_at"],7300000)
                complete.assert_called_once_with(["1","2"],100000)
                complete.reset_mock()
                compute.side_effect=RuntimeError("fixture failure")
                with self.assertRaises(RuntimeError):
                    cycle.cycle(sync=False)
                complete.assert_not_called()

if __name__=="__main__":
    unittest.main()
