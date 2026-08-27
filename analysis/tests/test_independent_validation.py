from __future__ import annotations
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
SYSTEM = ROOT / 'system'
if str(SYSTEM) not in sys.path: sys.path.insert(0, str(SYSTEM))
from analysis.independent_validation import (
    DIAGNOSTIC_EVALUATION_LIMIT, DIAGNOSTIC_LABELS, FIXED_STAKE,
    FIXTURE_STAKE_CAP, ensure_namespace, odds_tier_for_odds, odds_tier_metrics,
    prospective_metrics,
    public_diagnostics, record_evaluation_diagnostics, recompute_namespace,
    validation_bets,
)
import condition_portfolio as footbreak
from analysis.granular_conditions import mine
from analysis.wilson_validation import (
    FIXED_STAKE as WILSON_FIXED_STAKE,
    FIXTURE_STAKE_CAP as WILSON_FIXTURE_STAKE_CAP,
    ensure_namespace as ensure_wilson_namespace,
    recompute_namespace as recompute_wilson_namespace,
)

HKT = timezone(timedelta(hours=8))

def candidate(*, decided=20, hits=13, wilson=.52, specificity=2, key='role=主讓'):
    return {'market': 'HDC', 'label': '讓球｜T-5｜方向 主讓', 'key': ['system=footbreak', 'market=HDC', 'path=T-5', key],
            'observed_path': 'T-5', 'decision_stage': 'T-5', 'odds_tier': '≥1.70', 'specificity': specificity,
            'total': {'decided': decided, 'hits': hits, 'accuracy': hits / decided, 'pushes': 0, 'wilson95': [wilson, .8]},
            'selected_side': 'H', 'selected_line': -.25}

def watch(codes=('HDC',)):
    ko = datetime(2099, 1, 1, 20, tzinfo=HKT)
    return {'match_id': 'future', 'league': 'USA - Major League Soccer', 'home': '主隊', 'away': '客隊', 'kickoff': ko.isoformat(),
      'stages': [{'stage': 'T-5', 'ts': (ko-timedelta(minutes=5)).isoformat(), 'kickoff_hkt': ko.isoformat(),
        'market_predictions': [{'code': code, 'side': 'H', 'line': -.25 if code == 'HDC' else 2.5, 'odds': 1.8,
          'observed_at': (ko-timedelta(minutes=6)).isoformat(), 'source': 'hkjc_public_board'} for code in codes]}]}

class IndependentValidationTests(unittest.TestCase):
    def test_non_destructive_migration_and_old_new_isolation(self):
        legacy = {'portfolio': 'footbreak_independent_validation', 'strategy': 'independent-validation-v1',
                  'status': 'PENDING', 'stake': 250, 'pnl': 999}
        ledger = {'bets': [legacy.copy()], 'stats': {'legacy': 1}}
        ns = ensure_wilson_namespace(ledger, 'footbreak', now='2026-08-16T10:00:00+08:00')
        self.assertEqual(ledger['bets'][0]['stake'], 250)
        self.assertEqual(ns['activation_at'], '2026-08-16T10:00:00+08:00')
        self.assertTrue(ns['retired_v1']['read_only'])
        self.assertEqual(ns['retired_v1']['legacy_bets'], [legacy])
        stats = recompute_wilson_namespace(ledger, 'footbreak')
        self.assertEqual(stats['pnl'], 0)
        self.assertEqual(stats['n_pending'], 0)

    def test_t5_caps_freeze_and_conservative_selection(self):
        historical = [
            {'match_id': f'prior-{i}', 'stage': 'T-5',
             'kickoff': (datetime(2026, 1, 1, 20, tzinfo=HKT) + timedelta(days=i)).isoformat(),
             'predicted_at': (datetime(2026, 1, 1, 20, tzinfo=HKT) + timedelta(days=i, minutes=-5)).isoformat(),
             'market_grades': [{'code': 'HDC', 'side': 'H', 'line': -.25, 'odds': 1.8,
                                'grade_status': 'GRADED', 'hit': i < 50}]}
            for i in range(59)
        ]
        ledger = {'bets': []}
        made, audit = footbreak.evaluate_new_t5(ledger, watch(), None, history_rows=historical)
        self.assertEqual(len(made), 1); bet = made[0]
        self.assertEqual(bet['stake'], WILSON_FIXED_STAKE)
        self.assertLessEqual(sum(row['stake'] for row in made), WILSON_FIXTURE_STAKE_CAP)
        # The holdout is a subset of this total and remains audit-only.
        self.assertEqual(bet['frozen_historical_evidence']['decided'], 59)
        frozen = ledger['wilson_validation']['conditions'][bet['frozen_condition_signature']]['historical_evidence'].copy()
        ledger['bets'].extend(made)
        repeated, _ = footbreak.evaluate_new_t5(ledger, watch(), None, history_rows=historical)
        self.assertEqual(repeated, [])
        self.assertEqual(ledger['wilson_validation']['conditions'][bet['frozen_condition_signature']]['historical_evidence'], frozen)
        self.assertEqual(audit[-1]['reason'], 'wilson_candidate_frozen')

    def test_two_market_cap_opposition_timing_and_idempotency(self):
        rows = []
        for index in range(59):
            kickoff = datetime(2026, 2, 1, 20, tzinfo=HKT) + timedelta(days=index)
            rows.append({'match_id': f'prior-{index}', 'stage': 'T-5', 'kickoff': kickoff.isoformat(),
                         'predicted_at': (kickoff - timedelta(minutes=5)).isoformat(),
                         'market_grades': [{'code': 'HDC', 'side': 'H', 'line': -.25, 'odds': 1.8,
                                            'grade_status': 'GRADED', 'hit': True}]})
        ledger = {'bets': []}
        bad = watch(); bad['stages'][0]['post_hoc_backfill'] = True
        made, audit = footbreak.evaluate_new_t5(ledger, bad, None, ranking=rows)
        self.assertEqual(made, []); self.assertEqual(audit[0]['reason'], 'not_first_native_pre_kickoff_t5')
        made, audit = footbreak.evaluate_new_t5({'bets': []}, watch(), None, history_rows=rows)
        self.assertEqual(len(made), 1)
        self.assertEqual(made[0]['selected_side'], 'H')
        # Three valid markets create all three HK$500 simulation entries.
        all_rows = [
            {'match_id': f'{market}-{index}', 'stage': 'T-5', 'kickoff': (datetime(2026, 3, 1, 20, tzinfo=HKT) + timedelta(days=index)).isoformat(),
             'predicted_at': (datetime(2026, 3, 1, 20, tzinfo=HKT) + timedelta(days=index, minutes=-5)).isoformat(),
             'market_grades': [{'code': market, 'side': 'H', 'line': -.25 if market == 'HDC' else 2.5,
                                'odds': 1.8, 'grade_status': 'GRADED', 'hit': True}]}
            for market in ('HDC', 'HIL', 'CHL') for index in range(59)
        ]
        ledger = {'bets': []}
        made, _ = footbreak.evaluate_new_t5(ledger, watch(('HDC','HIL','CHL')), None, history_rows=all_rows)
        self.assertEqual(len(made), 3); self.assertEqual(sum(x['stake'] for x in made), 1500)

    def test_source_evidence_is_required_and_accuracy_never_breaks_a_tie(self):
        """Fixtures must explicitly contain quote evidence; raw accuracy cannot win."""
        no_source = watch()
        del no_source['stages'][0]['market_predictions'][0]['source']
        historical = [
            {'match_id': f'prior-{i}', 'stage': 'T-5', 'kickoff': (datetime(2026, 4, 1, 20, tzinfo=HKT) + timedelta(days=i)).isoformat(),
             'predicted_at': (datetime(2026, 4, 1, 20, tzinfo=HKT) + timedelta(days=i, minutes=-5)).isoformat(),
             'market_grades': [{'code': 'HDC', 'side': 'H', 'line': -.25, 'odds': 1.8, 'grade_status': 'GRADED', 'hit': True}]}
            for i in range(59)
        ]
        made, audit = footbreak.evaluate_new_t5({'bets': []}, no_source, None, history_rows=historical)
        self.assertEqual(made, [])
        self.assertEqual(audit[0]['reason'], 'selected_source_observation_invalid_or_missing')

        # The frozen ranking must supply a complete canonical definition; raw
        # accuracy-only fragments cannot be matched into an admission.
        made, audit = footbreak.evaluate_new_t5({'bets': []}, watch(), None, ranking=[candidate()])
        self.assertEqual(made, [])
        self.assertEqual(audit[0]['reason'], 'no_frozen_historical_condition')

    def test_asian_prospective_status_uses_pnl_and_push_exclusion(self):
        bets = [{'status': 'SETTLED', 'result': 'Won', 'stake': 250, 'odds': 2, 'pnl': 250} for _ in range(30)]
        metrics = prospective_metrics(bets)
        self.assertEqual(metrics['status'], '已驗證')
        self.assertEqual(metrics['weighted_implied_break_even'], .5)
        push = prospective_metrics([{'status': 'SETTLED', 'result': 'Refunded', 'stake': 250, 'odds': 2, 'pnl': 0}])
        self.assertEqual((push['decided'], push['pushes'], push['accuracy']), (0, 1, None))
        half = prospective_metrics([{'status': 'SETTLED', 'result': 'Half Won', 'stake': 250, 'odds': 2, 'pnl': 125}])
        self.assertEqual((half['hits'], half['pnl']), (1, 125))

    def test_odds_tiers_use_exact_boundaries_and_only_active_prospective_portfolio(self):
        def bet(odds, *, result='Won', pnl=100, market='HDC', **extra):
            return {
                'portfolio': 'footbreak_independent_validation',
                'strategy': 'independent-validation-v1',
                'status': 'SETTLED', 'odds': odds, 'stake': 250,
                'result': result, 'pnl': pnl, 'code': market, **extra,
            }

        # Boundary values belong to the higher tier, whereas 1.6999 is
        # diagnostic-only.  A push has real zero PnL but is not decided.
        ledger = {'bets': [
            bet(1.70, result='Won', pnl=175, market='HDC'),
            bet(1.7999, result='Refunded', pnl=0, market='HIL'),
            bet(1.80, result='Lost', pnl=-250, market='HIL'),
            bet(1.90, result='Half Won', pnl=112.5, market='CHL'),
            bet(2.00, result='Won', pnl=250, market='CHL'),
            bet(1.6999, result='Won', pnl=999),
            bet(None, result='Won', pnl=999),
            # Not an active prospective result even though its namespace is
            # malformed to look active.
            bet(1.80, result='Won', pnl=999, post_hoc_backfill=True),
            # Old portfolio and the other system must never cross this
            # system's report boundary.
            {'portfolio': 'footbreak_condition_simulation', 'strategy': 'granular-condition-v1',
             'status': 'SETTLED', 'odds': 2.00, 'stake': 9999, 'result': 'Won', 'pnl': 9999},
            {'portfolio': 'crown_independent_validation', 'strategy': 'independent-validation-v1',
             'status': 'SETTLED', 'odds': 2.00, 'stake': 9999, 'result': 'Won', 'pnl': 9999},
        ]}
        metrics = odds_tier_metrics(ledger, 'footbreak')
        tiers = {tier['key']: tier for tier in metrics['tiers']}
        self.assertEqual(
            [odds_tier_for_odds(value) for value in (1.70, 1.80, 1.90, 2.00, 1.6999)],
            ['1.70-1.79', '1.80-1.89', '1.90-1.99', '2.00-plus', None],
        )
        self.assertEqual((tiers['1.70-1.79']['n_bets'], tiers['1.70-1.79']['n_decided'],
                          tiers['1.70-1.79']['hits'], tiers['1.70-1.79']['pushes']),
                         (2, 1, 1, 1))
        self.assertEqual((tiers['1.70-1.79']['pnl'], tiers['1.70-1.79']['roi']),
                         (175, .35))
        self.assertEqual((tiers['1.80-1.89']['n_bets'], tiers['1.80-1.89']['n_decided'],
                          tiers['1.80-1.89']['hits']), (1, 1, 0))
        self.assertEqual((tiers['1.90-1.99']['n_bets'], tiers['1.90-1.99']['hits']),
                         (1, 1))
        self.assertEqual((tiers['2.00-plus']['n_bets'], tiers['2.00-plus']['pnl']),
                         (1, 250))
        self.assertEqual(metrics['excluded_diagnostics']['below_1_70'], 1)
        self.assertEqual(metrics['excluded_diagnostics']['invalid_or_missing_odds'], 1)
        self.assertEqual(metrics['excluded_diagnostics']['non_prospective_or_post_hoc'], 1)
        self.assertEqual(
            [row['market'] for row in tiers['1.70-1.79']['by_market']],
            ['HDC', 'HIL'],
        )
        self.assertEqual(odds_tier_metrics(ledger, 'crown')['tiers'][-1]['n_bets'], 1)

    def test_diagnostics_are_bounded_replay_safe_and_publicly_chinese(self):
        ledger = {'bets': []}
        namespace = ensure_namespace(ledger, 'footbreak', now='2026-08-16T10:00:00+08:00')
        rejected = [{'market': 'HDC', 'status': 'SKIPPED',
                     'reason': 'selected_odds_invalid_or_missing'}]
        record_evaluation_diagnostics(namespace, 'fixture-1', 'T-5', rejected,
                                      now='2026-08-16T10:01:00+08:00')
        # A replay replaces exactly the same fixture+market+stage decision.
        record_evaluation_diagnostics(namespace, 'fixture-1', 'T-5', rejected,
                                      now='2026-08-16T10:02:00+08:00')
        public = public_diagnostics(namespace)
        self.assertEqual(public['evaluated'], 1)
        self.assertEqual(public['counts']['selected_quote_invalid'], 1)
        self.assertIn('入選盤口', public['labels']['selected_quote_invalid'])
        self.assertEqual(public['labels'], DIAGNOSTIC_LABELS)
        self.assertNotIn('evaluations', public)

        for number in range(DIAGNOSTIC_EVALUATION_LIMIT + 3):
            record_evaluation_diagnostics(
                namespace, f'fixture-{number + 2}', 'T-5',
                [{'market': 'HDC', 'status': 'SKIPPED', 'reason': 'no_granular_match'}],
                now='2026-08-16T10:03:00+08:00',
            )
        self.assertLessEqual(
            len(namespace['diagnostics']['evaluations']),
            DIAGNOSTIC_EVALUATION_LIMIT,
        )
        self.assertEqual(public_diagnostics(namespace)['evaluated'], DIAGNOSTIC_EVALUATION_LIMIT)

    def test_native_t5_line_bucket_evidence_is_frozen_on_admission(self):
        ledger = {'bets': []}
        mismatched = [
            {'match_id': f'prior-{i}', 'stage': 'T-5', 'kickoff': (datetime(2026, 5, 1, 20, tzinfo=HKT) + timedelta(days=i)).isoformat(),
             'predicted_at': (datetime(2026, 5, 1, 20, tzinfo=HKT) + timedelta(days=i, minutes=-5)).isoformat(),
             'market_grades': [{'code': 'HDC', 'side': 'H', 'line': -.5, 'odds': 1.8, 'grade_status': 'GRADED', 'hit': True}]}
            for i in range(59)
        ]
        made, audit = footbreak.evaluate_new_t5(ledger, watch(), None, history_rows=mismatched)
        self.assertEqual(len(made), 1)
        self.assertEqual(next(row for row in audit if row['status'] == 'CREATED')['reason'], 'wilson_candidate_frozen')
        self.assertEqual(made[0]['frozen_historical_evidence']['decided'], 59)

    def test_real_persisted_ranking_and_native_t5_match_exactly_once(self):
        """Regression for the raw-history/dashboard cache schema split."""
        historical = []
        for index in range(59):
            kickoff = datetime(2026, 3, 1, 20, tzinfo=HKT) + timedelta(days=index)
            historical.append({
                "match_id": f"rank-{index}", "stage": "T-5",
                "kickoff": kickoff.isoformat(),
                "predicted_at": (kickoff - timedelta(minutes=5)).isoformat(),
                "market_grades": [{
                    "code": "HDC", "side": "H", "line": -.25, "odds": 1.80,
                    "grade_status": "GRADED", "hit": True,
                }],
            })
        ranking = mine(historical, system="footbreak")["ranking"]
        ledger = {"bets": []}
        made, audit = footbreak.evaluate_new_t5(ledger, watch(), None, ranking=ranking)
        self.assertEqual(len(made), 1)
        self.assertEqual(made[0]["selected_side"], "H")
        self.assertEqual(made[0]["selected_line"], -.25)
        self.assertEqual(next(row for row in audit if row["status"] == "CREATED")["reason"],
                         "wilson_candidate_frozen")
        ledger["bets"].extend(made)
        repeated, repeat_audit = footbreak.evaluate_new_t5(ledger, watch(), None, ranking=ranking)
        self.assertEqual(repeated, [])
        self.assertIn("idempotent_existing_market", {row["reason"] for row in repeat_audit})

if __name__ == '__main__': unittest.main()
