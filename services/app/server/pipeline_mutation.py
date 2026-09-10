"""Explicit reserve-before-publication, reconcile-after-unlocking protocol."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from . import pipeline_projection, pipeline_reconcile


@dataclass
class ProjectionMutation:
    ctx: Any
    video_id: str
    intent: pipeline_projection.ProjectionIntent | None
    pending: bool = True
    event_seq: int | None = None

    def flags(self) -> dict:
        return {
            "projection_pending": self.pending,
            "projection_event_seq": self.event_seq,
        }

    def complete(self) -> dict:
        """Call only after the canonical commit and all outer locks release."""
        if self.intent is None:
            return self.flags()
        try:
            record = pipeline_reconcile.reconcile_video(
                self.ctx, self.video_id, intent=self.intent
            )
            self.pending = record is None or record.projection_status != "current"
        except Exception as exc:
            self.pending = True
            try:
                pipeline_projection.fail_intent(self.intent, exc)
            except Exception:
                pass  # The durable pending intent survives a database outage.
        return self.flags()


def reserve_mutation(
    ctx, video_id: str, event_kind: str, source_identity: dict
) -> ProjectionMutation:
    """A reservation error must escape before any canonical state changes."""
    intent = pipeline_projection.reserve_intent(
        object_id=ctx.object_id,
        video_id=video_id,
        event_kind=event_kind,
        source_identity=source_identity,
    )
    return ProjectionMutation(
        ctx, video_id, intent,
        pending=intent is not None,
        event_seq=intent.event_seq if intent else None,
    )
