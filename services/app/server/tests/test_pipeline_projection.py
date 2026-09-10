from __future__ import annotations

import os
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from server.pipeline_projection import (
    ProjectionIntent,
    ProjectionRecord,
    apply_intent,
    fail_intent,
    get_many,
    pending_intents,
    reserve_intent,
    sanitize_error,
)


class PipelineProjectionUnitTests(unittest.TestCase):
    def test_records_serialize_without_exposing_mutable_internal_state(self) -> None:
        now = datetime(2026, 9, 10, 1, 2, 3, tzinfo=timezone.utc)
        identity = {"annotation": {"revision": 7}, "segments": ["seg_00"]}
        intent = ProjectionIntent(
            event_seq=11,
            object_id="boom",
            video_id="video-1",
            event_kind="annotation_saved",
            source_identity=identity,
            status="pending",
            error=None,
            created_at=now,
            updated_at=now,
        )
        record = ProjectionRecord(
            object_id="boom",
            video_id="video-1",
            event_seq=11,
            source_identity=identity,
            snapshot={"pipeline_stage": "sam3", "complete": False},
            projected_at=now,
        )

        intent_payload = intent.to_dict()
        record_payload = record.to_dict()
        intent_payload["source_identity"]["annotation"]["revision"] = 999
        record_payload["snapshot"]["pipeline_stage"] = "completed"

        self.assertEqual(intent.source_identity["annotation"]["revision"], 7)
        self.assertEqual(record.snapshot["pipeline_stage"], "sam3")
        self.assertEqual(intent_payload["created_at"], "2026-09-10T01:02:03+00:00")
        self.assertEqual(record_payload["projected_at"], "2026-09-10T01:02:03+00:00")

    def test_operations_are_noops_when_database_is_disabled(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            intent = reserve_intent(
                object_id="boom",
                video_id="video-1",
                event_kind="annotation_saved",
                source_identity={"revision": 1},
            )
            self.assertIsNone(intent)
            self.assertIsNone(apply_intent(1, {"pipeline_stage": "sam3"}))
            self.assertIsNone(fail_intent(1, "failure"))
            self.assertEqual(get_many("boom", ["video-1"]), {})
            self.assertEqual(pending_intents(), [])

    def test_errors_are_bounded_and_secrets_are_redacted(self) -> None:
        message = (
            "failed postgresql://pipeline:super-secret@postgres:5432/pipeline "
            "Authorization: Bearer hf_abcd1234 token=audit-secret "
            "password=another-secret " + "x" * 5000
        )

        sanitized = sanitize_error(message)

        self.assertNotIn("super-secret", sanitized)
        self.assertNotIn("hf_abcd1234", sanitized)
        self.assertNotIn("audit-secret", sanitized)
        self.assertNotIn("another-secret", sanitized)
        self.assertIn("[REDACTED]", sanitized)
        self.assertLessEqual(len(sanitized), 1000)

    def test_reservation_rejects_empty_identifiers_before_database_access(self) -> None:
        with self.assertRaisesRegex(ValueError, "object_id"):
            reserve_intent(
                object_id="",
                video_id="video-1",
                event_kind="annotation_saved",
                source_identity={},
                database_url="postgresql://unused",
            )
        with self.assertRaisesRegex(ValueError, "source_identity"):
            reserve_intent(
                object_id="boom",
                video_id="video-1",
                event_kind="annotation_saved",
                source_identity=[],  # type: ignore[arg-type]
                database_url="postgresql://unused",
            )


if __name__ == "__main__":
    unittest.main()
