from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock

CORE_SRC = Path(__file__).resolve().parents[1] / "packages" / "pipeline-core" / "src"
sys.path.insert(0, str(CORE_SRC))

from pipeline_core.jobs import CLAIM_SQL, PostgresJobQueue, priority_for


class DurableJobContractTests(unittest.TestCase):
    def test_interactive_preview_has_priority_over_propagation(self) -> None:
        self.assertLess(priority_for("sam3_preview"), priority_for("sam3_propagation"))

    def test_claim_uses_skip_locked_and_expired_leases(self) -> None:
        normalized = " ".join(CLAIM_SQL.upper().split())
        self.assertIn("FOR UPDATE SKIP LOCKED", normalized)
        self.assertIn("LEASE_EXPIRES_AT", normalized)
        self.assertIn("ORDER BY PRIORITY", normalized)

    def test_claim_reaps_expired_cancelled_and_exhausted_jobs(self) -> None:
        normalized = " ".join(CLAIM_SQL.upper().split())
        self.assertIn("CANCEL_REQUESTED OR ATTEMPTS >= MAX_ATTEMPTS", normalized)
        self.assertIn("'CANCELLED'::", normalized)
        self.assertIn("'ERROR'", normalized)

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

    def test_every_owned_lease_mutation_rejects_expired_lease(self) -> None:
        methods = (
            lambda queue: queue.heartbeat("job", "00000000-0000-0000-0000-000000000001", lease_seconds=30),
            lambda queue: queue.update_progress(
                "job", "00000000-0000-0000-0000-000000000001", {"current": 1}
            ),
            lambda queue: queue.retry_or_fail(
                "job", "00000000-0000-0000-0000-000000000001", "failed"
            ),
            lambda queue: queue.finish(
                "job",
                "00000000-0000-0000-0000-000000000001",
                state="done",
                result={"ok": True},
            ),
        )

        for method in methods:
            with self.subTest(method=method):
                connection = MagicMock()
                cursor = MagicMock()
                connection.cursor.return_value.__enter__.return_value = cursor
                cursor.fetchone.return_value = (False,)
                cursor.rowcount = 1
                queue = PostgresJobQueue(lambda: connection)

                method(queue)

                sql = " ".join(cursor.execute.call_args.args[0].upper().split())
                self.assertIn("LEASE_EXPIRES_AT > NOW()", sql)


if __name__ == "__main__":
    unittest.main()
