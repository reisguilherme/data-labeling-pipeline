"""CPU repair handler: fresh object context and canonical metadata only."""
from __future__ import annotations

from .pipeline_reconcile import reconcile_video
from .workspace import ObjectContext, workspace


def run_projection_reconcile(job: dict) -> dict:
    payload = job["payload"]
    object_id = payload["object_id"]
    video_id = payload["video_id"]
    if not isinstance(object_id, str) or not object_id or not isinstance(video_id, str) or not video_id:
        raise ValueError("reconciliation requires object_id and video_id")
    workspace.load()
    # Do not reuse workspace.context's process cache. The payload identity is
    # only an enqueue deduplication key; current metadata is read by reconcile.
    ctx = ObjectContext(workspace.get(object_id))
    record = reconcile_video(ctx, video_id)
    return {"object_id": object_id, "video_id": video_id,
            "projection_event_seq": record.event_seq if record else None}
