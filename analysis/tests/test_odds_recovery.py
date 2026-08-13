from __future__ import annotations
import copy, importlib.util, json, os, socket, stat, sys, tempfile, threading, time, unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from pathlib import Path
from analysis.odds_recovery import (
    apply, canonical_line, choose_quote, evidence_from_paths, overlay_rows,
    prediction_targets, report, snapshot_identity, PrivateResponseCache,
    ProviderFetcher, parse_titan_change_rows, titan_candidate,
    parse_tipsme_chart_ticks, tipsme_candidate, tipsme_crosswalk,
    provider_entries,
)
from crown.prediction_history import calculate_stats
SYSTEM_DIR = Path(__file__).resolve().parents[2] / "system"
if str(SYSTEM_DIR) not in sys.path:
    sys.path.insert(0, str(SYSTEM_DIR))
import gen_app_data

TS = "2026-08-10T10:00:00+08:00"

def row(system="footbreak", odds=None, match_id="event-1"):
    return {"match_id": match_id, "stage": "T-5", "predicted_at": TS,
            "market_predictions": [{"code": "HDC", "line": "-0.50", "side": "H", "odds": odds}]}

def quote(fixture="persisted:event-1", line="-0.5", side="H", odds="1.70", observed="2026-08-10T09:59:00+08:00"):
    from analysis.odds_recovery import _quote
    return _quote(fixture, "HDC", line, side, odds, observed, "fixture", "hash")

class OddsRecoveryTests(unittest.TestCase):
    def test_line_canonicalization_and_exact_boundary(self):
        self.assertEqual(canonical_line("-0.50"), "-0.5")
        self.assertEqual(canonical_line(DecimalLike()), "1.7")
        self.assertEqual(quote(odds="1.70")["odds"], "1.7")

    def test_requires_exact_identity_side_line_and_prior_time(self):
        target = prediction_targets([row()], "footbreak")[0][0]
        exact, reason = choose_quote(target, [quote()])
        self.assertIsNone(reason); self.assertEqual(exact["odds"], "1.7")
        for changed in (
            quote(fixture="persisted:other"), quote(line="-0.25"), quote(side="A"),
            quote(observed="2026-08-10T10:01:00+08:00"),
        ):
            found, why = choose_quote(target, [changed])
            self.assertIsNone(found)
            self.assertIn(why, {"no_exact_fixture_market_line_side_evidence", "only_post_prediction_evidence"})

    def test_chooses_closest_prior_quote(self):
        target = prediction_targets([row()], "footbreak")[0][0]
        found, reason = choose_quote(target, [
            quote(odds="1.80", observed="2026-08-10T09:00:00+08:00"), quote(odds="1.90"),
        ])
        self.assertIsNone(reason); self.assertEqual(found["odds"], "1.9")

    def test_bad_odds_and_ambiguous_fixture_are_unrecoverable(self):
        target = prediction_targets([row()], "footbreak")[0][0]
        self.assertIsNone(quote(odds="NaN")); self.assertIsNone(quote(odds="1.0"))
        self.assertIsNone(choose_quote(target, [quote(fixture="persisted:other")])[0])

    def test_apply_is_idempotent_conflict_fails_and_raw_is_unchanged(self):
        raw = [row()]; before = copy.deepcopy(raw)
        target = prediction_targets(raw, "footbreak")[0][0]
        from analysis.odds_recovery import _entry
        entry = _entry(target, quote())
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "private" / "overlay.json"
            self.assertEqual(apply(path, [entry]), {"added": 1, "already_present": 0})
            self.assertEqual(apply(path, [entry]), {"added": 0, "already_present": 1})
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
            backups = list(path.parent.glob("overlay.json.backup-*"))
            self.assertEqual(len(backups), 1)
            self.assertEqual(stat.S_IMODE(backups[0].stat().st_mode), 0o600)
            bad = _entry(target, quote(odds="1.8"))
            with self.assertRaisesRegex(ValueError, "conflicting"):
                apply(path, [bad])
            projected = overlay_rows(raw, "footbreak", path)
        self.assertEqual(raw, before)
        self.assertEqual(projected[0]["market_predictions"][0]["odds"], 1.7)
        self.assertEqual(projected[0]["market_predictions"][0]["recovery_provenance"], "historical_exact_prior")

    def test_malformed_existing_sidecar_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "overlay.json"
            path.write_text('{"schema_version":1,"entries":[{"selected_odds":"NaN"}],"audit":[]}')
            target = prediction_targets([row()], "footbreak")[0][0]
            from analysis.odds_recovery import _entry
            with self.assertRaisesRegex(ValueError, "malformed_sidecar_entry"):
                apply(path, [_entry(target, quote())])

    def test_malformed_candidate_refuses_without_creating_private_sidecar(self):
        target = prediction_targets([row()], "footbreak")[0][0]
        from analysis.odds_recovery import _entry
        invalid = {**_entry(target, quote()), "selected_odds": "NaN"}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "private" / "overlay.json"
            with self.assertRaisesRegex(ValueError, "non_finite_decimal"):
                apply(path, [invalid])
            self.assertFalse(path.exists())
            self.assertFalse(path.parent.exists())

    def test_inventory_finds_footbreak_saved_board_and_reports_provenance(self):
        with tempfile.TemporaryDirectory() as directory:
            p = Path(directory) / "snapshot.json"
            p.write_text(json.dumps({"match_id": "event-1", "stage": "T-30", "saved_at": "2026-08-10T09:59:00+08:00", "hk_odds": {"HDC": [{"condition": "-0.5", "odds": {"H": 1.7, "A": 2.1}}]}}))
            output, entries = report({"footbreak": [row()], "crown": [row("crown")]}, {"footbreak": [p], "crown": []})
        self.assertEqual(output["systems"]["footbreak"]["missing_total"], 1)
        self.assertEqual(output["systems"]["footbreak"]["recovered_candidate_count"], 1)
        self.assertEqual(len(entries), 1)
        self.assertTrue(entries[0]["evidence_source_hash"])
        self.assertEqual(output["systems"]["crown"]["unrecoverable_reasons"]["no_exact_fixture_market_line_side_evidence"], 1)

    def test_inventory_counts_missing_odds_even_when_identity_is_unrecoverable(self):
        impossible = [{
            "stage": "T-5", "predicted_at": TS,
            "market_predictions": [{"code": "HDC", "line": -0.5, "side": "H", "odds": None}],
        }]
        output, entries = report({"footbreak": impossible}, {"footbreak": []})
        details = output["systems"]["footbreak"]
        self.assertEqual(details["missing_total"], 1)
        self.assertEqual(details["strict_identity_target_total"], 0)
        self.assertEqual(details["recovered_candidate_count"], 0)
        self.assertEqual(details["unrecoverable_reasons"]["missing_stable_fixture_identity"], 1)
        self.assertEqual(entries, [])

    def test_top_level_fixture_key_is_allowed_only_for_known_snapshot_mapping(self):
        board = {
            "event-1": {
                "saved_at": "2026-08-10T09:59:00+08:00",
                "hk_odds": {"HDC": [{"condition": "-0.5", "odds": {"H": 1.7}}]},
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            arbitrary = root / "unrelated.json"
            known = root / "hk_snapshots.json"
            arbitrary.write_text(json.dumps(board))
            known.write_text(json.dumps(board))
            ignored, ignored_reasons, _ = evidence_from_paths("footbreak", [arbitrary], root)
            accepted, accepted_reasons, _ = evidence_from_paths("footbreak", [known], root)
        self.assertEqual(ignored, [])
        self.assertEqual(ignored_reasons["unrecognized_evidence_structure"], 1)
        self.assertEqual(len(accepted), 1)
        self.assertEqual(accepted[0]["fixture_identity"], "persisted:event-1")
        self.assertNotIn("unrecognized_evidence_structure", accepted_reasons)

    def test_evidence_explicitly_declared_for_other_system_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "snapshot.json"
            path.write_text(json.dumps({
                "system": "crown", "match_id": "event-1",
                "saved_at": "2026-08-10T09:59:00+08:00",
                "hk_odds": {"HDC": [{"condition": "-0.5", "odds": {"H": 1.7}}]},
            }))
            output, entries = report({"footbreak": [row()]}, {"footbreak": [path]})
        self.assertEqual(entries, [])
        self.assertEqual(output["systems"]["footbreak"]["recovered_candidate_count"], 0)
        self.assertEqual(
            output["systems"]["footbreak"]["unrecoverable_reasons"]
            ["no_exact_fixture_market_line_side_evidence"],
            1,
        )

    def test_dashboard_match_id_overlays_learning_fixture_id_without_raw_mutation(self):
        dashboard = [row(match_id="shared-persisted-id")]
        target = prediction_targets(dashboard, "footbreak")[0][0]
        from analysis.odds_recovery import _entry
        entry = _entry(target, quote(fixture="persisted:shared-persisted-id"))
        learning = [{
            "fixture_id": "shared-persisted-id", "stage": "T-5",
            "predicted_at": TS, "learning_snapshot_id": "db-only-id",
            "market_predictions": [{"code": "HDC", "line": -0.5, "side": "H", "odds": None}],
        }]
        before = copy.deepcopy(learning)
        self.assertEqual(snapshot_identity(dashboard[0], "footbreak"),
                         snapshot_identity(learning[0], "footbreak"))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "overlay.json"
            apply(path, [entry])
            projected = overlay_rows(learning, "footbreak", path)
            # This is the actual read-only learning/data-health projection,
            # where the persisted field is called fixture_id rather than
            # dashboard match_id.
            from analysis.data_health import build_market_rows
            source_snapshot = {
                "fixture_id": "shared-persisted-id", "stage": "T-5",
                "generated_at": TS, "snapshot_id": "db-only-id",
                "kickoff": "2026-08-10T11:00:00+08:00",
                "payload_json": json.dumps({
                    "market_predictions": [{
                        "code": "HDC", "line": -0.5, "condition": -0.5,
                        "side": "H", "odds": None, "probability": 0.6,
                    }],
                }),
            }
            source_before = copy.deepcopy(source_snapshot)
            old = os.environ.get("ODDS_RECOVERY_SIDECAR")
            os.environ["ODDS_RECOVERY_SIDECAR"] = str(path)
            try:
                health_rows, _ = build_market_rows([source_snapshot], [], "footbreak")
            finally:
                if old is None:
                    os.environ.pop("ODDS_RECOVERY_SIDECAR", None)
                else:
                    os.environ["ODDS_RECOVERY_SIDECAR"] = old
        self.assertEqual(learning, before)
        self.assertEqual(source_snapshot, source_before)
        item = projected[0]["market_predictions"][0]
        self.assertEqual(item["odds"], 1.7)
        self.assertEqual(item["recovery_provenance"], "historical_exact_prior")
        self.assertEqual(health_rows[0]["odds"], 1.7)

    def test_crown_persisted_match_id_wins_over_extra_provider_ids(self):
        dashboard = [row("crown", match_id="crown-persisted-7")]
        dashboard[0].update({"hkjc_match_id": "HK-7", "titan_match_id": "TITAN-7"})
        target = prediction_targets(dashboard, "crown")[0][0]
        from analysis.odds_recovery import _entry
        entry = _entry(target, quote(fixture="persisted:crown-persisted-7"))
        history = [{
            "match_id": "crown-persisted-7", "hkjc_match_id": "DIFFERENT-HK-COPY",
            "titan_match_id": "DIFFERENT-TITAN-COPY", "stage": "T-5",
            "predicted_at": TS,
            "market_predictions": [{"code": "HDC", "line": "-0.50", "side": "H", "odds": None}],
        }]
        before = copy.deepcopy(history)
        self.assertEqual(snapshot_identity(dashboard[0], "crown"),
                         snapshot_identity(history[0], "crown"))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "overlay.json"
            apply(path, [entry])
            projected = overlay_rows(history, "crown", path)
        self.assertEqual(history, before)
        self.assertEqual(projected[0]["market_predictions"][0]["odds"], 1.7)
        self.assertEqual(projected[0]["market_predictions"][0]["recovery_provenance"],
                         "historical_exact_prior")

    def test_public_footbreak_regeneration_projects_overlay_only(self):
        target = prediction_targets([row()], "footbreak")[0][0]
        from analysis.odds_recovery import _entry
        entry = _entry(target, quote())
        script = Path(__file__).resolve().parents[2] / "deploy" / "regenerate-odds-recovery-dashboard.py"
        spec = importlib.util.spec_from_file_location("recovery_dashboard_regeneration", script)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        original = {
            "other_public_data": {"kept": True},
            "prediction_history": {
                "rows": [{**row(), "prediction_era": "2026-08-10-market-learning-v2"}],
                "stats": {"old": "stale"},
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "overlay.json"
            data_path = Path(directory) / "data.json"
            apply(path, [entry])
            sidecar_before = path.read_bytes()
            data_path.write_text(json.dumps(original))
            previous = os.environ.get("ODDS_RECOVERY_SIDECAR")
            os.environ["ODDS_RECOVERY_SIDECAR"] = str(path)
            try:
                self.assertEqual(module.regenerate(data_path)["prediction_history_rows"], 1)
            finally:
                if previous is None:
                    os.environ.pop("ODDS_RECOVERY_SIDECAR", None)
                else:
                    os.environ["ODDS_RECOVERY_SIDECAR"] = previous
            regenerated = json.loads(data_path.read_text())
            self.assertEqual(path.read_bytes(), sidecar_before)
        self.assertEqual(original["prediction_history"]["rows"][0]["market_predictions"][0]["odds"], None)
        self.assertTrue(regenerated["other_public_data"]["kept"])
        item = regenerated["prediction_history"]["rows"][0]["market_predictions"][0]
        self.assertEqual(item["odds"], 1.7)
        self.assertEqual(item["recovery_provenance"], "historical_exact_prior")

    def test_footbreak_and_crown_overlay_reconcile_missing_to_high_tier(self):
        target = prediction_targets([row()], "footbreak")[0][0]
        from analysis.odds_recovery import _entry
        entry = _entry(target, quote(odds="1.70"))
        crown_target = prediction_targets([row("crown")], "crown")[0][0]
        crown_entry = _entry(crown_target, quote(odds="1.70"))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "overlay.json"; apply(path, [entry, crown_entry])
            old = os.environ.get("ODDS_RECOVERY_SIDECAR")
            os.environ["ODDS_RECOVERY_SIDECAR"] = str(path)
            try:
                watch = {"event-1": {"match_id": "event-1", "home": "H", "away": "A",
                    "league": "L", "kickoff": "2026-08-10T11:00:00+08:00", "stages": [{
                    "prediction_era": "2026-08-10-market-learning-v2", "stage": "T-5", "ts": TS,
                    "market_predictions": [{"code": "HDC", "line": -0.5, "condition": -0.5,
                        "side": "H", "probability": .6, "odds": None}]}]}}
                accuracy = {"matches": [{"match_id": "event-1", "home": "H", "away": "A", "league": "L",
                    "kickoff": "2026-08-10T11:00:00+08:00", "stages": [{"stage": "T-5",
                    "market_grades": [{"code": "HDC", "line": -0.5, "condition": -0.5, "side": "H",
                        "odds": None, "grade_status": "GRADED", "hit": True}]}]}]}
                foot = gen_app_data.build_prediction_history(watch, [], accuracy)
                self.assertEqual(foot["stats"]["by_market"]["HDC"]["graded"], 1)
                self.assertEqual(foot["stats"]["by_market"]["HDC"]["odds_groups"]["missing"]["graded"], 0)
                self.assertEqual(foot["stats"]["by_market"]["HDC"]["odds_groups"]["at_or_above_1_70"]["graded"], 1)
                crown_rows = overlay_rows([{
                    "match_id": "event-1", "stage": "T-5", "predicted_at": TS,
                    "market_predictions": [{"code": "HDC", "line": -0.5, "side": "H", "odds": None}],
                    "market_grades": [{"code": "HDC", "line": -0.5, "side": "H", "odds": None,
                        "grade_status": "GRADED", "hit": True}],
                }], "crown", path)
                crown = calculate_stats(crown_rows)
                self.assertEqual(crown["by_market"]["HDC"]["odds_groups"]["at_or_above_1_70"]["graded"], 1)
            finally:
                if old is None: os.environ.pop("ODDS_RECOVERY_SIDECAR", None)
                else: os.environ["ODDS_RECOVERY_SIDECAR"] = old


class ProviderRecoveryTests(unittest.TestCase):
    KICKOFF = "2026-01-01T00:10:00+08:00"

    def target(self, stage="T-30", market="HDC", side="H", line="-0.5"):
        return {
            "system": "crown", "fixture_identity": "persisted:titan-77",
            "snapshot_identity": f"crown|persisted:titan-77|{stage}|2025-12-31T15:00:00+00:00",
            "stage": stage, "market_code": market, "side": side, "line": line,
            "predicted_at": datetime(2025, 12, 31, 15, tzinfo=timezone.utc),
            "row": {"match_id": "titan-77", "titan_match_id": "titan-77",
                    "kickoff": self.KICKOFF},
        }

    def test_titan_opening_locf_line_and_post_kickoff_exclusion(self):
        # The name is deliberately masked: only exact company ID 3 qualifies.
        html = """
        <tr data-company-id="19"><td>1.90</td><td>0.5</td><td>0.20</td><td>12-31 23:30</td><td>即</td></tr>
        <tr data-company-id="3"><td>0.70</td><td>0.5</td><td>0.90</td><td>12-31 23:20</td><td>即</td><td>***</td></tr>
        <tr data-company-id="3"><td>0.80</td><td>0.5</td><td>0.80</td><td>12-31 23:35</td><td>即</td></tr>
        <tr data-company-id="3"><td>0.20</td><td>0.25</td><td>1.90</td><td>12-31 23:36</td><td>即</td></tr>
        <tr data-company-id="3"><td>1.20</td><td>0.5</td><td>0.50</td><td>01-01 00:11</td><td>滚</td></tr>
        <tr data-company-id="3"><td>1.20</td><td>0.5</td><td>0.50</td><td>01-01 00:12</td><td>封</td></tr>
        """
        kickoff = datetime.fromisoformat(self.KICKOFF)
        ticks = parse_titan_change_rows(html, "HDC", kickoff)
        self.assertEqual([row["line"] for row in ticks], ["-0.5", "-0.5", "-0.25"])
        # T-30 target is 23:40, so LOCF is 23:35, not the later/in-play price.
        quote, reason = titan_candidate(self.target(), html, "https://example.test/titan")
        self.assertIsNone(reason)
        self.assertEqual(quote["odds"], "1.8")
        # HK 0.70 normalizes to decimal 1.70 and remains at the high-tier boundary.
        opening, reason = titan_candidate(self.target(stage="首預"), html, "https://example.test/titan")
        self.assertIsNone(reason)
        self.assertEqual(opening["odds"], "1.7")
        self.assertEqual(opening["provider_evidence"]["company_id"], "3")
        self.assertEqual(opening["provider_evidence"]["native_odds_format"], "hong_kong")

    def test_titan_t5_uses_last_prior_irregular_tick_and_year_rollover(self):
        html = """
        <tr companyID="3"><td>0.71</td><td>2.5</td><td>0.92</td><td>12-31 23:57</td><td>即</td></tr>
        <tr companyID="3"><td>0.73</td><td>2.5</td><td>0.89</td><td>01-01 00:03</td><td>即</td></tr>
        """
        target = self.target(stage="T-5", market="HIL", side="H", line="2.5")
        quote, reason = titan_candidate(target, html, "https://example.test/titan-hil")
        self.assertIsNone(reason)
        self.assertEqual(quote["odds"], "1.73")
        self.assertEqual(quote["provider_evidence"]["age_seconds"], 120.0)

    def test_titan_rejects_wrong_bookmaker_and_no_prior_quote(self):
        wrong = '<tr data-company-id="8"><td>0.7</td><td>0.5</td><td>0.9</td><td>12-31 23:30</td><td>即</td></tr>'
        quote, reason = titan_candidate(self.target(), wrong, "https://example.test/titan")
        self.assertIsNone(quote)
        self.assertEqual(reason, "no_exact_fixture_market_line_side_evidence")
        after = '<tr data-company-id="3"><td>0.7</td><td>0.5</td><td>0.9</td><td>12-31 23:50</td><td>即</td></tr>'
        quote, reason = titan_candidate(self.target(), after, "https://example.test/titan")
        self.assertIsNone(quote)
        self.assertEqual(reason, "no_qualifying_prior_quote")

    def test_private_cache_reuses_raw_response_without_network(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = PrivateResponseCache(Path(directory))
            url = "https://example.test/page"
            cache.put(url, b"<html>cached</html>", 200)
            with patch("urllib.request.urlopen", side_effect=AssertionError("network should not run")):
                body, cached, error = ProviderFetcher(cache).get(url)
            self.assertEqual(body, "<html>cached</html>")
            self.assertTrue(cached)
            self.assertIsNone(error)
            self.assertEqual(stat.S_IMODE(Path(directory).stat().st_mode), 0o700)
            self.assertTrue(all(stat.S_IMODE(path.stat().st_mode) == 0o600
                                for path in Path(directory).iterdir()))

    def test_fetcher_rate_limits_starts_and_bounds_concurrency(self):
        active = 0; maximum_active = 0; starts: list[float] = []; lock = threading.Lock()

        class Response:
            status = 200
            def read(self): return b"<html>ok</html>"
            def __enter__(self): return self
            def __exit__(self, *_): return False

        def urlopen(_request, timeout):
            nonlocal active, maximum_active
            self.assertEqual(timeout, 0.5)
            with lock:
                starts.append(time.monotonic())
                active += 1; maximum_active = max(maximum_active, active)
            time.sleep(0.12)
            with lock:
                active -= 1
            return Response()

        with tempfile.TemporaryDirectory() as directory:
            fetcher = ProviderFetcher(
                PrivateResponseCache(Path(directory)), rate_per_second=20,
                retries=0, timeout_seconds=0.5, workers=2,
            )
            with patch("urllib.request.urlopen", side_effect=urlopen):
                pages = fetcher.get_many(f"https://example.test/{number}" for number in range(3))
        self.assertEqual(list(pages), [f"https://example.test/{number}" for number in range(3)])
        self.assertTrue(all(value[0] == "<html>ok</html>" for value in pages.values()))
        self.assertEqual(maximum_active, 2)
        self.assertGreater(maximum_active, 1)
        self.assertEqual(len(starts), 3)
        self.assertTrue(all(later - earlier >= 0.04 for earlier, later in zip(starts, starts[1:])))

    def test_fetcher_deduplicates_urls_and_accounts_terminal_timeouts(self):
        with tempfile.TemporaryDirectory() as directory:
            fetcher = ProviderFetcher(
                PrivateResponseCache(Path(directory)), rate_per_second=100,
                retries=0, timeout_seconds=0.25, workers=8,
            )
            with patch("urllib.request.urlopen", side_effect=socket.timeout("bounded")) as opener:
                pages = fetcher.get_many(["https://example.test/same", "https://example.test/same"])
                # The in-run terminal result is also reused by later callers.
                repeat = fetcher.get("https://example.test/same")
        self.assertEqual(list(pages), ["https://example.test/same"])
        self.assertEqual(pages["https://example.test/same"], (None, False, "TimeoutError"))
        self.assertEqual(repeat, (None, False, "TimeoutError"))
        self.assertEqual(opener.call_count, 1)
        self.assertEqual(fetcher.http_failures, 1)
        self.assertEqual(fetcher.timeout_failures, 1)
        self.assertEqual(opener.call_args.kwargs["timeout"], 0.25)

    def test_provider_entries_deduplicate_pages_and_keep_target_order(self):
        html = """
        <tr data-company-id="3"><td>0.70</td><td>0.5</td><td>0.90</td><td>12-31 23:30</td><td>即</td></tr>
        <tr data-company-id="3"><td>0.80</td><td>0.5</td><td>0.80</td><td>01-01 00:03</td><td>即</td></tr>
        """

        class Response:
            status = 200
            def read(self): return html.encode("gb18030")
            def __enter__(self): return self
            def __exit__(self, *_): return False

        first = self.target(stage="T-5")
        second = self.target(stage="T-30")
        with tempfile.TemporaryDirectory() as directory:
            with patch("urllib.request.urlopen", return_value=Response()) as opener:
                entries, audit = provider_entries(
                    [first, second], providers={"titan"}, cache_dir=Path(directory),
                    rate_per_second=100, retries=0, timeout_seconds=0.5, workers=8,
                )
        self.assertEqual(opener.call_count, 1)
        self.assertEqual([entry["snapshot_identity"] for entry in entries], [
            first["snapshot_identity"], second["snapshot_identity"],
        ])
        self.assertEqual([entry["selected_odds"] for entry in entries], ["1.8", "1.7"])
        self.assertEqual(audit["pages_fetched"], 1)
        self.assertEqual(audit["http_failures"], 0)
        self.assertEqual(audit["timeout_failures"], 0)
        self.assertNotIn(html, json.dumps(audit))

    def test_tipsme_requires_exact_crosswalk_and_timestamped_tick(self):
        target = self.target()
        target["row"] = {"hkjc_match_id": "HK-1", "tipsme_match_id": "TM-1"}
        self.assertIsNone(tipsme_crosswalk(target))
        target["row"]["tipsme_hkjc_match_id"] = "HK-1"
        self.assertEqual(tipsme_crosswalk(target), "TM-1")
        # A visible current price without a timestamp is never historical evidence.
        sparse = '{"market":"hdp","line":"-0.5","home":0.70,"away":0.90,"current":true}'
        self.assertEqual(parse_tipsme_chart_ticks(sparse, "HDC"), [])
        target["row"]["kickoff"] = "2026-08-10T10:00:00+08:00"
        quote, reason = tipsme_candidate(target, sparse, "https://example.test/tipsme")
        self.assertIsNone(quote)
        self.assertEqual(reason, "no_qualifying_prior_quote")

class DecimalLike:
    def __str__(self): return "1.700"

if __name__ == "__main__": unittest.main()
