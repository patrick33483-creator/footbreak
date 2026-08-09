import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from system import run_predict


class KnownFixtureIdsTest(unittest.TestCase):
    def test_reads_confirmed_fixture_ids_from_ledger(self):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "sim_ledger.json").write_text(
                json.dumps({
                    "watch": {
                        "5001": {"fixture_id": 12345, "stages": []},
                        "5002": {"fixture_id": None, "stages": []},
                    }
                }),
                encoding="utf-8",
            )
            with patch.object(run_predict, "HERE", directory):
                self.assertEqual(
                    run_predict.known_fixture_ids(),
                    {"5001": "12345"},
                )


if __name__ == "__main__":
    unittest.main()
