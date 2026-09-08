from __future__ import annotations

import sys
import unittest
from pathlib import Path

CORE_SRC = Path(__file__).resolve().parents[1] / "packages" / "pipeline-core" / "src"
sys.path.insert(0, str(CORE_SRC))

from pipeline_core.jobs import CLAIM_SQL, priority_for


class DurableJobContractTests(unittest.TestCase):
    def test_interactive_preview_has_priority_over_propagation(self) -> None:
        self.assertLess(priority_for("sam3_preview"), priority_for("sam3_propagation"))

    def test_claim_uses_skip_locked_and_expired_leases(self) -> None:
        normalized = " ".join(CLAIM_SQL.upper().split())
        self.assertIn("FOR UPDATE SKIP LOCKED", normalized)
        self.assertIn("LEASE_EXPIRES_AT", normalized)
        self.assertIn("ORDER BY PRIORITY", normalized)

    def test_optional_worker_kind_has_explicit_postgres_type(self) -> None:
        normalized = " ".join(CLAIM_SQL.split())
        self.assertIn("%(worker_kind)s::text IS NULL", normalized)

    def test_optional_video_id_has_explicit_postgres_type(self) -> None:
        source = (
            Path(__file__).resolve().parents[1]
            / "services"
            / "app"
            / "server"
            / "durable_jobs.py"
        ).read_text(encoding="utf-8")
        normalized = " ".join(source.split())
        self.assertIn("(%s::text IS NULL OR payload->>'video_id' <> %s::text)", normalized)


if __name__ == "__main__":
    unittest.main()
