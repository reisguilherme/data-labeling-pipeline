"""CPU repair handler: fresh object context and canonical metadata only."""
from __future__ import annotations

from contextlib import contextmanager

from . import durable_jobs
from .pipeline_reconcile import reconcile_video
from .workspace import ObjectContext, workspace


class ProjectionRepairLeaseLost(RuntimeError):
    """The repair no longer owns permission to publish its projection."""


def run_projection_reconcile(job: dict) -> dict:
    payload = job["payload"]
    object_id = payload["object_id"]
    video_id = payload["video_id"]
    if not isinstance(object_id, str) or not object_id or not isinstance(video_id, str) or not video_id:
        raise ValueError("reconciliation requires object_id and video_id")
    job_id = str(job.get("id") or "")
    token = str(job.get("lease_token") or "")

    def require_owner(owned):
        if not (owned is not None and owned.get("job_id") == job_id
                and owned.get("kind") == "pipeline_projection_reconcile"
                and owned.get("worker_kind") == "cpu"
                and owned.get("object_id") == object_id and owned.get("video_id") == video_id):
            raise ProjectionRepairLeaseLost("lease lost or scope changed for pipeline projection repair")

    # Reject stale/cancelled jobs before opening canonical metadata. This short
    # preflight releases its row lock before the reconciler waits for video_fence.
    require_owner(durable_jobs.renew_owned_lease(job_id, token))

    @contextmanager
    def publication_fence():
        # The reconciler enters this after video_fence and its metadata read.
        # Keep the job row locked through reserve/apply; never heartbeat through
        # a second connection from inside this block.
        with durable_jobs.owned_job_lease(job_id, token) as owned:
            require_owner(owned)
            yield

    workspace.load()
    # Do not reuse workspace.context's process cache. The payload identity is
    # only an enqueue deduplication key; current metadata is read by reconcile.
    ctx = ObjectContext(workspace.get(object_id))
    record = reconcile_video(ctx, video_id, publication_fence=publication_fence)
    return {"object_id": object_id, "video_id": video_id,
            "projection_event_seq": record.event_seq if record else None}
