from __future__ import annotations

import unittest
from pathlib import Path

from remanufacturing.acceptance import run


ROOT = Path(__file__).resolve().parents[1]


class AcceptanceTests(unittest.TestCase):
    def test_offline_acceptance(self) -> None:
        result = run(ROOT)
        self.assertEqual(result["status"], "ok")
        self.assertTrue(result["immutable_after_spec_update"])
        self.assertTrue(result["delivery_blocked_while_held"])
        self.assertEqual(result["cert1_pinned_rule"], "transformer-reuse@1")
        self.assertEqual(result["incident"]["held_serials"], ["SN-TR2000-002"])
        self.assertEqual(result["incident"]["delivered_serials"], ["SN-TR2000-001"])
        self.assertEqual(len(result["incident"]["affected_certificates"]), 2)
        self.assertEqual(result["destination_summary"]["reused_count"], 3)
        self.assertEqual(result["destination_summary"]["scrapped_count"], 1)
        self.assertEqual(result["schema"]["missing_tables"], [])
        self.assertEqual(result["schema"]["schema_version"], "1")
        self.assertEqual(len(result["cert1_evidence_sha256"]), 64)


if __name__ == "__main__":
    unittest.main()
