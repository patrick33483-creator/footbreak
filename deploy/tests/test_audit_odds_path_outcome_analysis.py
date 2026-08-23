from __future__ import annotations
import importlib.util
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / 'audit-odds-path-outcome-analysis.py'
spec = importlib.util.spec_from_file_location('audit_odds_path', SCRIPT)
mod = importlib.util.module_from_spec(spec); assert spec.loader; spec.loader.exec_module(mod)

def stage(name, market='HDC', side='H', line=-0.5, odds=1.9, outcome=True):
    settlement = 'Won' if outcome else 'Lost'
    return {'stage': name, 'market_predictions': [{'code': market, 'side': side, 'line': line, 'odds': odds}], 'market_grades': ([{'code': market, 'side': side, 'line': line, 'grade_status': 'GRADED', 'settlement': settlement, 'hit': outcome}] if name == 'T-5' else [])}
def ledger(n=50):
    return {'watch': {f'f{i}': {'match_id': f'f{i}', 'kickoff': f'2026-01-{1+i//2:02d} 12:00', 'stages': [stage('T-30', odds=1.8 + (i%3)*.1, outcome=bool(i%2)), stage('T-5', odds=1.82 + (i%4)*.1, outcome=bool(i%2))]} for i in range(n)}}

class OddsPathAuditTests(unittest.TestCase):
    def test_pairing_and_grade_exclusions(self):
        data = ledger(2)
        data['watch']['f0']['stages'][1]['market_grades'][0].update({'settlement': 'Refunded', 'hit': None})
        data['watch']['f1']['stages'][0]['market_predictions'][0]['side'] = 'A'
        rows, diag = mod.ledger_rows(data, 'footbreak')
        self.assertEqual(rows, [])
        self.assertEqual(diag['push_or_void'], 1)
        self.assertEqual(diag['unpaired_t30_t5'], 2)
    def test_chronological_group_split_has_no_fixture_leakage(self):
        rows, _ = mod.ledger_rows(ledger(20), 'footbreak')
        train, test = mod.chronological_split(rows)
        self.assertTrue(train and test)
        self.assertLessEqual(max(r['kickoff'] for r in train), min(r['kickoff'] for r in test))
        self.assertFalse({r['fixture'] for r in train} & {r['fixture'] for r in test})
        boundary = max(r['kickoff'] for r in train)
        self.assertFalse(any(r['kickoff'] == boundary for r in test))
    def test_wilson_and_explicit_half_policy(self):
        self.assertAlmostEqual(mod.wilson_lower(10, 10), 0.722467, places=5)
        s = stage('T-5'); s['market_grades'][0].update({'settlement': 'Half Won', 'hit': True})
        self.assertEqual(mod.grade_binary(s, 'HDC', 'H', '-0.5'), (True, 'decided'))
        s['market_grades'][0]['hit'] = None
        self.assertEqual(mod.grade_binary(s, 'HDC', 'H', '-0.5')[1], 'nonbinary_or_unrecognized_grade')
    def test_exact_settled_bet_is_only_missing_grade_fallback(self):
        data = ledger(1)
        data['watch']['f0']['stages'][1]['market_grades'] = []
        data['bets'] = [{'match_id': 'f0', 'code': 'HDC', 'side': 'H', 'condition': '-0.5',
                         'status': 'SETTLED', 'result': 'Won'}]
        rows, _ = mod.ledger_rows(data, 'footbreak')
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]['outcome'])
    def test_train_only_scaler_centroid_and_determinism(self):
        rows, _ = mod.ledger_rows(ledger(60), 'footbreak'); train, test = mod.chronological_split(rows)
        spec = mod.vectorizer_fit(train, mod.FAMILIES['market_side_t30_odds_return'])
        before = list(spec['means']); mod.vectorizer_apply(test, spec)
        self.assertEqual(before, spec['means'])
        a = mod.analyze_source(ledger(60), 'footbreak'); b = mod.analyze_source(ledger(60), 'footbreak')
        self.assertEqual(a, b)
    def test_feature_family_cap(self):
        self.assertTrue(all(len(features) <= 3 for features in mod.FAMILIES.values()))

if __name__ == '__main__': unittest.main()
