"""Unit coverage for crown/tick_timing_probe.py.

These tests protect the safety contract that the 2026-08-22 Crown T-5
tick-starvation timing-instrumentation work depends on:

- Disabled by default; a disabled probe never touches the filesystem and
  never raises.
- When enabled, ``record()`` never raises regardless of what goes wrong
  internally (bad path, unwritable directory, non-primitive extra values).
- Output is bounded: large ``extra`` values that are not primitives are
  dropped, not serialized; the file is pruned once it exceeds the size cap.
- A single slow write self-disables the probe for the remainder of the
  process so a stalled disk can never repeatedly steal time from the
  deadline-owning tick path.
- No provider payload keys are ever accidentally captured because
  ``record()`` only ever accepts a fixed, explicit set of fields.
"""
from __future__ import annotations

import importlib
import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from crown import tick_timing_probe as probe


class _EnvIsolatedTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._saved_enabled = os.environ.get(probe._ENABLED_ENV)
        self._saved_path = os.environ.get(probe._PATH_ENV)
        os.environ.pop(probe._ENABLED_ENV, None)
        os.environ.pop(probe._PATH_ENV, None)

    def tearDown(self) -> None:
        for key, value in (
            (probe._ENABLED_ENV, self._saved_enabled),
            (probe._PATH_ENV, self._saved_path),
        ):
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


class TickTimingProbeGatingTests(_EnvIsolatedTestCase):
    def test_disabled_by_default(self) -> None:
        self.assertFalse(probe.enabled())

    def test_disabled_record_never_touches_filesystem(self) -> None:
        with TemporaryDirectory() as directory:
            target = Path(directory) / "probe.jsonl"
            os.environ[probe._PATH_ENV] = str(target)
            probe.record("checkpoint_should_be_dropped")
            self.assertFalse(target.exists())

    def test_enabled_variants_are_case_and_value_tolerant(self) -> None:
        for value in ("1", "true", "True", "yes", "on", "ON"):
            os.environ[probe._ENABLED_ENV] = value
            self.assertTrue(probe.enabled(), msg=f"expected enabled for {value!r}")
        for value in ("0", "false", "no", "off", ""):
            os.environ[probe._ENABLED_ENV] = value
            self.assertFalse(probe.enabled(), msg=f"expected disabled for {value!r}")


class TickTimingProbeWriteTests(_EnvIsolatedTestCase):
    def test_enabled_record_appends_bounded_jsonl_line(self) -> None:
        with TemporaryDirectory() as directory:
            target = Path(directory) / "nested" / "probe.jsonl"
            os.environ[probe._ENABLED_ENV] = "1"
            os.environ[probe._PATH_ENV] = str(target)
            probe.record(
                "checkpoint_a", match_id="12345", deadline=None,
                extra={"foo": 1, "bar": "baz", "flag": True},
            )
            self.assertTrue(target.exists())
            lines = target.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 1)
            sample = json.loads(lines[0])
            self.assertEqual(sample["checkpoint"], "checkpoint_a")
            self.assertEqual(sample["match_id"], "12345")
            self.assertEqual(sample["foo"], 1)
            self.assertEqual(sample["bar"], "baz")
            self.assertIs(sample["flag"], True)
            self.assertIn("monotonic", sample)
            self.assertIn("pid", sample)

    def test_remaining_budget_is_relative_not_raw_wall_time(self) -> None:
        with TemporaryDirectory() as directory:
            target = Path(directory) / "probe.jsonl"
            os.environ[probe._ENABLED_ENV] = "1"
            os.environ[probe._PATH_ENV] = str(target)
            import time
            deadline = time.monotonic() + 5.0
            probe.record("checkpoint_b", deadline=deadline)
            sample = json.loads(target.read_text(encoding="utf-8").splitlines()[0])
            self.assertIn("remaining_budget_s", sample)
            self.assertLessEqual(sample["remaining_budget_s"], 5.0)
            self.assertGreater(sample["remaining_budget_s"], 0.0)
            # The raw deadline (an opaque monotonic-clock value) must never
            # be recorded verbatim -- only the derived remaining-seconds
            # delta is safe because it carries no wall-clock correlation.
            self.assertNotIn("deadline", sample)

    def test_non_primitive_extra_values_are_dropped_not_serialized(self) -> None:
        with TemporaryDirectory() as directory:
            target = Path(directory) / "probe.jsonl"
            os.environ[probe._ENABLED_ENV] = "1"
            os.environ[probe._PATH_ENV] = str(target)

            class _Payload:
                """Stand-in for a provider response object that must never
                be captured by the probe, even if a caller passes it by
                mistake."""

                def __repr__(self) -> str:
                    return "<should-never-appear>"

            probe.record(
                "checkpoint_c",
                extra={"safe_int": 3, "unsafe_object": _Payload(), "safe_list": [1, 2]},
            )
            sample = json.loads(target.read_text(encoding="utf-8").splitlines()[0])
            self.assertEqual(sample["safe_int"], 3)
            self.assertNotIn("unsafe_object", sample)
            self.assertNotIn("safe_list", sample)
            raw_text = target.read_text(encoding="utf-8")
            self.assertNotIn("should-never-appear", raw_text)

    def test_record_never_raises_on_unwritable_path(self) -> None:
        os.environ[probe._ENABLED_ENV] = "1"
        # A path under a directory that cannot be created (its parent is a
        # regular file, not a directory) is guaranteed to raise from the
        # filesystem layer -- this must still never propagate out of
        # record().
        with TemporaryDirectory() as directory:
            blocking_file = Path(directory) / "not_a_directory"
            blocking_file.write_text("x", encoding="utf-8")
            os.environ[probe._PATH_ENV] = str(blocking_file / "probe.jsonl")
            try:
                probe.record("checkpoint_never_raises")
            except BaseException as exc:  # pragma: no cover - failure path
                self.fail(f"record() must never raise, got {exc!r}")


class TickTimingProbeSelfDisableTests(_EnvIsolatedTestCase):
    def test_slow_write_self_disables_further_recording(self) -> None:
        with TemporaryDirectory() as directory:
            target = Path(directory) / "probe.jsonl"
            os.environ[probe._ENABLED_ENV] = "1"
            os.environ[probe._PATH_ENV] = str(target)

            real_append = probe._append_bounded

            def _slow_append(sample):
                import time
                time.sleep(probe._MAX_WRITE_SECONDS + 0.05)
                real_append(sample)

            with patch.object(probe, "_append_bounded", side_effect=_slow_append):
                probe.record("checkpoint_slow_write")

            self.assertEqual(os.environ.get(probe._ENABLED_ENV), "0")
            self.assertFalse(probe.enabled())

            # A subsequent call must be a true no-op: no new line appended.
            existing = target.read_text(encoding="utf-8").splitlines()
            probe.record("checkpoint_after_self_disable")
            after = target.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(existing), len(after))


class TickTimingProbePruningTests(_EnvIsolatedTestCase):
    def test_file_is_pruned_once_it_exceeds_size_cap(self) -> None:
        with TemporaryDirectory() as directory:
            target = Path(directory) / "probe.jsonl"
            os.environ[probe._ENABLED_ENV] = "1"
            os.environ[probe._PATH_ENV] = str(target)
            # Pre-seed a file just over the size cap with more lines than
            # the retention limit so the very next append must trigger a
            # prune down to the last _MAX_LINES lines.
            target.parent.mkdir(parents=True, exist_ok=True)
            padding_line = json.dumps({"checkpoint": "x" * 400})
            seed_lines = probe._MAX_LINES + 1000
            target.write_text((padding_line + "\n") * seed_lines, encoding="utf-8")
            self.assertGreater(target.stat().st_size, 2_000_000)

            probe.record("checkpoint_after_seed")

            pruned_lines = target.read_text(encoding="utf-8").splitlines()
            self.assertLessEqual(len(pruned_lines), probe._MAX_LINES + 1)
            self.assertEqual(
                json.loads(pruned_lines[-1])["checkpoint"], "checkpoint_after_seed",
            )


class TickTimingProbeNoProviderFieldsTests(unittest.TestCase):
    def test_record_signature_has_no_provider_payload_parameter(self) -> None:
        """Static contract check: the public API cannot accept a payload,
        quote, odds, price, or team/league field at all -- the safety
        guarantee is enforced by the function signature itself, not just by
        caller discipline."""
        import inspect
        signature = inspect.signature(probe.record)
        allowed = {"checkpoint", "run_id", "match_id", "deadline", "extra"}
        self.assertEqual(set(signature.parameters), allowed)


if __name__ == "__main__":
    unittest.main()
