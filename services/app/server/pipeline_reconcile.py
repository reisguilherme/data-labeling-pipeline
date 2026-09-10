"""Metadata-only repair of the durable per-video pipeline projection."""

from __future__ import annotations

import json
from collections.abc import Callable
from contextlib import AbstractContextManager, nullcontext

from .pipeline_projection import (
    ProjectionIntent,
    ProjectionRecord,
    apply_intent,
    fail_intent,
    reserve_repair_intent,
)
from . import pipeline_projection
from .pipeline_state import PipelineSnapshot, PipelineSource, derive_pipeline_source
from .sam3 import queue as sam3_queue
from .video_fence import video_fence
from .workspace import workspace


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


def _invalid_annotation_source(
    *, object_id: str, video_id: str, present: bool
) -> PipelineSource:
    return PipelineSource(
        identity={
            "schema_version": 1,
            "object": {"object_id": object_id, "archived": False},
            "annotation": {"state": "invalid"},
            "video": {"video_id": video_id, "present": present},
        },
        snapshot=PipelineSnapshot(
            stage="triage",
            status="invalid",
            artifacts_valid=False,
            validation_status="invalid",
            inconsistencies=("annotations.json invalido",),
        ),
    )


def _read_annotation_entry(ctx, relpath: str) -> tuple[object | None, bool]:
    """Read one annotation entry without invoking AnnotationStore recovery writes."""

    path = ctx.annotations_path
    if not path.exists():
        return None, False
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None, True
    if not isinstance(document, dict):
        return None, True
    videos = document.get("videos")
    counts = document.get("counts")
    if not isinstance(videos, dict) or not isinstance(counts, dict):
        return None, True
    return videos.get(relpath), False


def derive_read_only_pipeline_source(
    ctx,
    video_id: str,
    *,
    archived: bool | None = None,
) -> PipelineSource:
    """Derive canonical metadata without creating directories or backup files."""

    object_id = str(getattr(ctx, "object_id", "") or "").strip()
    if archived is None:
        archived = bool(getattr(getattr(ctx, "config", None), "archived", False))
    if archived:
        return _terminal_source(
            object_id=object_id,
            video_id=video_id,
            archived=True,
            present=None,
        )
    video = ctx.index.get(video_id)
    if video is None:
        return _terminal_source(
            object_id=object_id,
            video_id=video_id,
            archived=False,
            present=False,
        )
    entry, invalid = _read_annotation_entry(ctx, video.relpath)
    if invalid:
        return _invalid_annotation_source(
            object_id=object_id,
            video_id=video_id,
            present=True,
        )
    sam3 = sam3_queue.public(object_id, video.relpath)
    return derive_pipeline_source(entry, sam3, ctx.output_root)


def _record_failure(intent: ProjectionIntent | None, error: Exception) -> None:
    if intent is None:
        return
    try:
        fail_intent(intent, error)
    except Exception:
        # The canonical exception is the useful failure.  A second database
        # outage while recording it must not replace that diagnostic.
        pass


def _authoritative_archived(ctx, object_id: str) -> bool:
    """Reload object lifecycle metadata without constructing/scanning a context."""

    config = getattr(ctx, "config", None)
    if workspace.ready:
        # Workspace.get refreshes only objects.json and invalidates stale cached
        # contexts.  It deliberately does not scan the video's media root.
        config = workspace.get(object_id)
    return bool(getattr(config, "archived", False))


def reconcile_video(
    ctx,
    video_id: str,
    *,
    intent: ProjectionIntent | None = None,
    publication_fence: Callable[[], AbstractContextManager] | None = None,
    read_only: bool = False,
) -> ProjectionRecord | None:
    """Rebuild and conditionally apply one video's projection.

    A caller-provided intent is only an event-kind hint.  The store always
    revalidates the current active event for the freshly derived identity;
    this is what makes a cached A intent safe after an A -> B -> A cycle.
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

    selected: ProjectionIntent | None = None
    try:
        # The source read and event allocation must share the same fence as
        # canonical writers.  Otherwise an old read could reserve a newer
        # event_seq while another process is publishing newer metadata.
        with video_fence(object_id, video_id), pipeline_projection.object_fence(object_id):
            archived = _authoritative_archived(ctx, object_id)
            if read_only:
                source = derive_read_only_pipeline_source(
                    ctx,
                    video_id,
                    archived=archived,
                )
            elif archived:
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

            commit_guard = publication_fence() if publication_fence else nullcontext()
            # Writers use the same outer order: video -> publication/job ->
            # projection.  The guard revalidates ownership at commit time.
            with commit_guard:
                selected, current = reserve_repair_intent(
                    object_id=object_id,
                    video_id=video_id,
                    source_identity=source.identity,
                    snapshot=source.snapshot_dict,
                )
                if current is not None:
                    return current
                if selected is None:
                    return None
                return apply_intent(selected, source.snapshot_dict)
    except Exception as exc:
        _record_failure(selected, exc)
        raise
