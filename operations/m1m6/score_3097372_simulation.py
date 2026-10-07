"""Frozen-source, fixed-rule, memory-only result sensitivity comparison."""
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import time
from contextlib import closing
import notifier
from dynamic_rules import universe, matched_rows, feature_rows
from policy import gates, settle, GATE_VERSION
from sync_results import parse_header

SID = "3097372"
RULES = {"D0026", "D0037", "D0091", "D0110", "D0116"}
SOURCE_KO = 1791278400000
RECORDED_KO = 1791277200000
URL = "https://livestatic.titan007.com/phone/txt/analysisheader/cn/3/09/3097372.txt"
BASE = Path("/var/lib/crown-m1m6")


def audit():
    # Confirm that staged analytical code is identical to the active runtime.
    here = Path(__file__).parent
    for name in ("policy.py", "dynamic_rules.py", "notifier.py"):
        assert (here/name).read_bytes() == (Path("/opt/crown-m1m6")/name).read_bytes(), name
    registry_raw = (BASE/"registry.json").read_bytes()
    registry = json.loads(registry_raw)
    now = int(time.time()*1000)
    source = notifier.source(now)  # This function uses a read-only SQLite snapshot.
    ms, snapshots, finished, checkpoints = source
    match = next(m for m in ms if str(m["sid"]) == SID)
    assert match["kickoff_utc"] == RECORDED_KO, "Recorded kickoff changed; investigate before comparing"
    sys.path.insert(0, "/opt/footbreak")
    from crown.titan import TitanClient
    from crown.config import settings
    raw = TitanClient(settings())._read(URL, encoding="utf-8", timeout=5, attempts=1, hard_deadline=7)
    proof, reason = parse_header(raw, {**match, "kickoff_utc": SOURCE_KO}, int(time.time()*1000))
    assert not reason and proof["source_kickoff"] == SOURCE_KO, reason
    assert (proof["home_score"], proof["away_score"]) == (0, 0), "Fresh regulation score changed"
    prior = finished.get(SID)
    if prior:
        assert (prior["status"], prior["home_score"], prior["away_score"]) == ("完", 0, 0), "Existing score conflict"
    simulated = {"sid": SID, "status": "完", "home_score": 0, "away_score": 0, "fetched_at": now}
    actual_rows, quality = universe(source, now)
    # A separate pipeline may have imported the score since the user's request.
    # Compare a deliberately withheld-result baseline to adding this score, not
    # the misleading no-op of adding an already present score.
    rows = [{**r, "result": None, "pnl": None, "score": None, "result_at": None}
            if r["sid"] == SID else r for r in actual_rows]
    after_rows = [settle(r, simulated) if r["sid"] == SID else r for r in rows]
    corrected_rows, corrected_reason = feature_rows(
        {**match, "kickoff_utc": SOURCE_KO}, snapshots[SID], checkpoints.get(SID, {}),
        {**finished, SID: simulated}, now)
    corrected_all = sorted([r for r in rows if r["sid"] != SID]+corrected_rows,
                           key=lambda r: (r["ko"], int(r["sid"])))
    outputs, changed = [], []
    for rule in registry["strategies"]:
        before = [r for r in matched_rows(rows, rule) if r.get("result")]
        after = [r for r in matched_rows(after_rows, rule) if r.get("result")]
        bg, ag = gates(before), gates(after)
        cg = gates([r for r in matched_rows(corrected_all, rule) if r.get("result")])
        impacted = [r for r in after if r["sid"] == SID]
        if bg != ag:
            changed.append({"id": rule["id"], "pass_before": bg["pass"], "pass_after": ag["pass"],
                            "active": rule["active"]})
        if rule["id"] in RULES or impacted:
            history = lambda records: [{k:r.get(k) for k in
                ("sid","ko","home","away","market","side","line","hk","result","pnl","score")}
                for r in records[-30:]]
            outputs.append({"id": rule["id"], "version": rule["version"], "active": rule["active"],
                            "description": rule.get("description"), "market": rule["market"], "side": rule["side"],
                            "before": bg, "score_only_after": ag, "source_time_after": cg,
                            "actual_current": gates([r for r in matched_rows(actual_rows, rule) if r.get("result")]),
                            "hypothetical_match_results": history(impacted),
                            "before_last30": history(before), "after_last30": history(after),
                            "window_changes": {n: {
                                "entered": sorted(set(ag[n]["sids"])-set(bg[n]["sids"])),
                                "exited": sorted(set(bg[n]["sids"])-set(ag[n]["sids"]))} for n in ("20","30")}})
    assert RULES <= {r["id"] for r in outputs}
    times = {f"{stage}_{market}": {
        "captured_at": q["captured_at"],
        "minutes_before_recorded": (RECORDED_KO-q["captured_at"])/60000,
        "minutes_before_source": (SOURCE_KO-q["captured_at"])/60000}
        for (stage,market), q in snapshots[SID].items()
        if stage in ("initial","T30","T5") and market in ("AH","OU")}
    with closing(sqlite3.connect(f"file:{BASE/'ledger.sqlite'}?mode=ro", uri=True)) as db:
        sent = db.execute("SELECT COUNT(*) FROM items WHERE sid=? AND status IN ('sent','sending','uncertain')",
                          (SID,)).fetchone()[0]
    with closing(sqlite3.connect(f"file:{notifier.CROWN_DB}?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        imports = [dict(r) for r in db.execute(
            "SELECT observed_at,written_at,source_url,evidence_json FROM strategy_result_sync_audit WHERE sid=? ORDER BY id DESC LIMIT 3",
            (SID,))]
    return {"summary": {"action": "score_3097372_memory_only_simulation", "at_ms": now,
                        "gate_version": GATE_VERSION, "registry_updated_at": registry.get("updated_at"),
                        "registry_sha256": hashlib.sha256(registry_raw).hexdigest(),
                        "rules_checked": len(registry["strategies"]), "changed_rules": changed,
                        "target_notified_items": sent, "database_writes": 0, "registry_writes": 0,
                        "lock_changes": 0, "forced_sends": 0,
                        "baseline_already_has_result": prior is not None,
                        "comparison_baseline": "counterfactual_withhold_this_result_at_same_current_snapshot",
                        "qualification_changes": [r for r in changed if r["pass_before"] != r["pass_after"]],
                        "discovery_and_merging_rerun": False},
            "match": match, "source_evidence": {**proof,"url": URL,"raw_header": raw},
            "prior_official_result": prior, "existing_import_audits": imports, "snapshots": times,
            "source_time_quality": {"reason": corrected_reason, "valid_market_rows": len(corrected_rows)},
            "baseline_quality": quality, "strategies": outputs}
