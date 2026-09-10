"""Metadata-only repair of the durable per-video pipeline projection."""

from __future__ import annotations

from typing import Any

from .pipeline_projection import (
    ProjectionIntent,
    ProjectionRecord,
    apply_intent,
    fail_intent,
    reserve_intent,
)
from .pipeline_state import PipelineSnapshot, PipelineSource, derive_pipeline_source
from .sam3 import queue as sam3_queue
from .video_fence import video_fence


def _terminal_source(
    *, object_id: str, video_id: str, archived: bool, present: bool | None
) -> PipelineSource:
    if archived:
        snapshot = PipelineSnapshot(
            stage="discarded",
            status="archived",
            artifacts_valid=False,
            validation_status="not_applicable",
        )
    else:
        snapshot = PipelineSnapshot(
            stage="triage",
            status="missing",
            artifacts_valid=False,
            validation_status="invalid",
            inconsistencies=("video ausente do indice canonico",),
        )
    return PipelineSource(
        identity={
            "schema_version": 1,
            "object": {"object_id": object_id, "archived": archived},
            "video": {"video_id": video_id, "present": present},
        },
        snapshot=snapshot,
    )


def _same_pending_intent(
    intent: ProjectionIntent | None,
    *,
    object_id: str,
    video_id: str,
    source_identity: dict[str, Any],
) -> bool:
    return bool(
        intent is not None
        and intent.status == "pending"
        and intent.object_id == object_id
        and intent.video_id == video_id
        and intent.source_identity == source_identity
    )


def _record_failure(intent: ProjectionIntent | None, error: Exception) -> None:
    if intent is None:
        return
    try:
        fail_intent(intent, error)
    except Exception:
        # The canonical exception is the useful failure.  A second database
        # outage while recording it must not replace that diagnostic.
        pass


def reconcile_video(
    ctx,
    video_id: str,
    *,
    intent: ProjectionIntent | None = None,
) -> ProjectionRecord | None:
    """Rebuild and conditionally apply one video's projection.

    A matching pending intent is resumed directly.  If canonical metadata has
    changed since that intent was reserved, a new monotonic intent is reserved
    and the store's conditional apply supersedes the stale event.
    """

    object_id = str(getattr(ctx, "object_id", "") or "").strip()
    video_id = str(video_id or "").strip()
    if not object_id:
        raise ValueError("object_id nao pode ser vazio")
    if not video_id:
        raise ValueError("video_id nao pode ser vazio")
    if intent is not None and (
        intent.object_id != object_id or intent.video_id != video_id
    ):
        raise ValueError("intent pertence a outro objeto ou video")

    selected: ProjectionIntent | None = intent
    try:
        # The source read and event allocation must share the same fence as
        # canonical writers.  Otherwise an old read could reserve a newer
        # event_seq while another process is publishing newer metadata.
        with video_fence(object_id, video_id):
            archived = bool(
                getattr(getattr(ctx, "config", None), "archived", False)
            )
            if archived:
                source = _terminal_source(
                    object_id=object_id,
                    video_id=video_id,
                    archived=True,
                    present=None,
                )
            else:
                ensure_loaded = getattr(ctx, "ensure_loaded", None)
                if callable(ensure_loaded):
                    ensure_loaded()
                video = ctx.index.get(video_id)
                if video is None:
                    source = _terminal_source(
                        object_id=object_id,
                        video_id=video_id,
                        archived=False,
                        present=False,
                    )
                else:
                    # Cross-process writers publish annotations.json atomically.
                    # Reload immediately before deriving instead of trusting this
                    # process's potentially stale in-memory snapshot.
                    ctx.store.load()
                    entry = ctx.store.entry(video.relpath)
                    sam3 = sam3_queue.public(object_id, video.relpath)
                    source = derive_pipeline_source(entry, sam3, ctx.output_root)

            if not _same_pending_intent(
                intent,
                object_id=object_id,
                video_id=video_id,
                source_identity=source.identity,
            ):
                # A stale caller-provided intent belongs to the old canonical
                # identity.  If reserving the replacement fails, leave that old
                # event untouched instead of attributing the database failure to
                # unrelated source metadata.
                selected = None
                selected = reserve_intent(
                    object_id=object_id,
                    video_id=video_id,
                    event_kind="reconcile",
                    source_identity=source.identity,
                )
            if selected is None:
                return None
            return apply_intent(selected, source.snapshot_dict)
    except Exception as exc:
        _record_failure(selected, exc)
        raise
