"""Focused regression coverage for bilateral fan-in durability."""
from __future__ import annotations

import unittest

from analysis import bilateral_decision as bilateral


def record(decision_id="d1"):
    return {
        "decision_id": decision_id, "system": "footbreak", "fixture": "m1",
        "market": "HIL", "side": "H", "line": 2.5, "condition_signature": "formal-v7",
        "condition_number": 7, "evidence_version": 3, "decision": "COUNTERPART_UNAVAILABLE",
        "signal_book": "hkjc", "signal_quote": 1.82, "counterpart_book": "crown",
        "counterpart_status": "UNAVAILABLE", "counterpart_reason": "collector_timeout",
        "minimum_odds": 1.80, "created_at": "2026-08-21T20:55:00+08:00",
    }


class BilateralDecisionTests(unittest.TestCase):
    def test_unavailable_counterpart_is_a_durable_explicit_decision_and_outbox(self):
        namespace = {}
        first, created = bilateral.persist_decision(namespace, record())
        repeated, duplicate = bilateral.persist_decision(namespace, record())
        self.assertTrue(created)
        self.assertFalse(duplicate)
        self.assertEqual(first["decision"], "COUNTERPART_UNAVAILABLE")
        self.assertEqual(first, repeated)
        self.assertEqual(len(namespace["counterpart_attempts"]), 0)
        self.assertEqual(namespace["decision_outbox"][0]["delivery"], "PENDING")
        self.assertEqual(namespace["decision_outbox"][0]["decision_id"], "d1")
        self.assertIn("provenance_hash", first)

    def test_counterpart_attempt_is_idempotent_and_distinguishes_failure_from_no_market(self):
        namespace = {}
        attempt = {
            "system": "crown", "match_id": "c1", "market": "HDC", "side": "A",
            "line": 0.5, "stage_at": "2026-08-21T20:55:00+08:00",
            "counterpart_status": "UNAVAILABLE",
            "counterpart_reason": "hkjc_local_evidence_unavailable",
        }
        bilateral.append_counterpart_attempt(namespace, attempt)
        bilateral.append_counterpart_attempt(namespace, attempt)
        self.assertEqual(len(namespace["counterpart_attempts"]), 1)
        self.assertEqual(
            namespace["counterpart_attempts"][0]["counterpart_reason"],
            "hkjc_local_evidence_unavailable",
        )

    def test_formal_condition_identity_is_part_of_idempotency_key(self):
        a = bilateral.decision_id(
            system="footbreak", fixture="m", market="HIL", side="H", line=2.5,
            condition_signature="formal-a", evidence_version=1,
        )
        b = bilateral.decision_id(
            system="footbreak", fixture="m", market="HIL", side="H", line=2.5,
            condition_signature="research-r99", evidence_version=1,
        )
        self.assertNotEqual(a, b)

    def test_public_projection_omits_fixture_and_raw_provider_payload(self):
        projection = bilateral.public_decision(record() | {
            "fixture": "private-id", "raw_provider_payload": {"token": "never"},
        })
        self.assertNotIn("fixture", projection)
        self.assertNotIn("raw_provider_payload", projection)
        self.assertEqual(projection["counterpart_reason"], "collector_timeout")


if __name__ == "__main__":
    unittest.main()
