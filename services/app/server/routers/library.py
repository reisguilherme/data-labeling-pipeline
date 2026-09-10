"""Listagem da biblioteca de um objeto."""

from __future__ import annotations

import asyncio
from bisect import bisect_right
from collections import OrderedDict
from dataclasses import replace
import logging
import threading

from fastapi import APIRouter, Depends, HTTPException
from starlette.responses import FileResponse, Response

from .. import durable_jobs, media, pipeline_projection
from ..deps import get_object
from ..locks import locks
from ..pipeline_state import PipelineSnapshot, derive_pipeline_source, inspect_pipeline_entry
from ..sam3 import queue as sam3_queue
from ..singleflight import AsyncSingleFlight
from ..workspace import ObjectContext

router = APIRouter(prefix="/api/objects/{object_id}", tags=["library"])
_listing_singleflight = AsyncSingleFlight()
_STORE_ENTRY_UNSET = object()
_REPAIR_LIMIT = 20
_repair_cursors: OrderedDict[str, str] = OrderedDict()
_repair_cursor_lock = threading.Lock()
_STAGE_STATUSES = {
    "triage": {"pending", "in_progress", "missing"},
    "discarded": {"discarded", "archived"},
    "sam3": {"ready", "queued", "leased", "running", "error", "cancelled", "invalid"},
    "review": {"waiting", "in_progress", "audit_required"},
    "completed": {"validated"},
}
log = logging.getLogger(__name__)


def video_payload(
    ctx: ObjectContext,
    video,
    media_info: dict | None,
    lock=None,
    sam3=None,
    *,
    store_entry=_STORE_ENTRY_UNSET,
    pipeline=None,
) -> dict:
    entry = (
        ctx.store.entry(video.relpath)
        if store_entry is _STORE_ENTRY_UNSET
        else store_entry
    )
    status = entry.get("status", "pending") if entry else "pending"
    intervals = entry.get("intervals", []) if entry else []
    if pipeline is None:
        pipeline = inspect_pipeline_entry(entry, sam3, ctx.output_root)
    return {
        "video_id": video.video_id,
        "relpath": video.relpath,
        "name": video.name,
        "size_bytes": video.size_bytes,
        "file_mtime": video.file_mtime,
        "status": status,
        "interval_count": len(intervals),
        "missing": not video.abspath.exists(),
        "probed": media_info is not None,
        "duration_sec": (media_info or {}).get("duration_sec"),
        "width": (media_info or {}).get("width"),
        "height": (media_info or {}).get("height"),
        "frame_count": (media_info or {}).get("frame_count"),
        "browser_playable": (media_info or {}).get("browser_playable"),
        "updated_by": (entry or {}).get("updated_by"),
        # URL já escopada no objeto: o cache do browser é chaveado por URL e o
        # video_id (sha1 do relpath) não é salgado por objeto — dois objetos com
        # o mesmo relpath serviriam a thumb um do outro.
        "thumb_url": f"/api/objects/{ctx.object_id}/videos/{video.video_id}/thumb",
        "lock": lock.public() if lock else None,
        "sam3": sam3,
        "pipeline_stage": pipeline.stage,
        "stage_status": pipeline.status,
        "stage_progress": pipeline.public_progress(),
    }


def _verified_snapshot(record, source):
    """Accept only a current, exactly matching and well-formed projection."""
    if record is None:
        return None, "missing"
    state = getattr(record, "projection_status", "stale")
    if state != "current":
        return None, "pending" if state == "pending" else "stale"
    if record.source_identity != source.identity:
        return None, "stale"
    value = record.snapshot
    try:
        counts = [value[key] for key in ("expected_frames", "reviewed_frames", "edited_frames")]
        if any(type(count) is not int or not 0 <= count <= 2**53 - 1 for count in counts):
            return None, "stale"
        expected, reviewed, edited = counts
        if not edited <= reviewed <= expected:
            return None, "stale"
        stage, status = value["pipeline_stage"], value["stage_status"]
        if not isinstance(stage, str) or not isinstance(status, str) or status not in _STAGE_STATUSES.get(stage, set()):
            return None, "stale"
        if type(value["complete"]) is not bool or value["complete"] != (stage == "completed"):
            return None, "stale"
        if type(value["artifacts_valid"]) is not bool or value["validation_status"] not in {
            "manifest", "invalid", "audit_required", "not_applicable",
        }:
            return None, "stale"
        diagnostics = value["inconsistencies"]
        if not isinstance(diagnostics, list) or len(diagnostics) > 64:
            return None, "stale"
        for diagnostic in diagnostics:
            if not isinstance(diagnostic, str) or not diagnostic.strip() or len(diagnostic) > 1000:
                return None, "stale"
            if any(ord(char) < 32 for char in diagnostic):
                return None, "stale"
            diagnostic.encode("utf-8")
        if stage == "completed":
            if not (value["artifacts_valid"] and value["validation_status"] == "manifest"
                    and expected > 0 and reviewed == expected and not diagnostics):
                return None, "stale"
        if stage == "review":
            if expected <= 0:
                return None, "stale"
            if status == "audit_required":
                if value["validation_status"] != "audit_required" or value["artifacts_valid"]:
                    return None, "stale"
            elif not (value["artifacts_valid"] and value["validation_status"] == "manifest"
                      and not diagnostics and reviewed < expected
                      and (reviewed == 0) == (status == "waiting")):
                return None, "stale"
        if stage == "sam3":
            if status == "ready" and (value["artifacts_valid"] or diagnostics):
                return None, "stale"
            if status == "invalid" and (value["artifacts_valid"] or not diagnostics or value["validation_status"] != "invalid"):
                return None, "stale"
        return PipelineSnapshot(
            stage=value["pipeline_stage"], status=value["stage_status"],
            expected_frames=counts[0], reviewed_frames=counts[1], edited_frames=counts[2],
            artifacts_valid=value["artifacts_valid"], validation_status=value["validation_status"],
            inconsistencies=tuple(value["inconsistencies"]),
        ), "current"
    except (KeyError, TypeError, UnicodeError):
        return None, "stale"


def _next_repair_batch(object_id: str, repairs: list[dict]) -> list[dict]:
    """Round-robin through metadata repairs, including persistent failures."""
    ordered = sorted(repairs, key=lambda item: item["video_id"])
    ids = [item["video_id"] for item in ordered]
    with _repair_cursor_lock:
        start = bisect_right(ids, _repair_cursors.get(object_id, ""))
        batch = (ordered[start:] + ordered[:start])[:_REPAIR_LIMIT]
        if batch:
            _repair_cursors[object_id] = batch[-1]["video_id"]
            _repair_cursors.move_to_end(object_id)
            # Bound process memory across object archive/delete churn.
            if len(_repair_cursors) > 256:
                _repair_cursors.popitem(last=False)
        return batch


def _build_video_listing(
    ctx: ObjectContext,
    search: str | None = None,
    status: str | None = None,
    sort: str = "name",
) -> dict:
    held = locks.map_for(ctx.object_id)
    sam3_state = sam3_queue.map_for(ctx.object_id)
    videos = ctx.index.all()
    store_entries, counts = ctx.store.listing_snapshot(len(videos))
    try:
        projections = pipeline_projection.get_many(ctx.object_id, [v.video_id for v in videos])
    except Exception:
        log.warning("Pipeline projection lookup unavailable for object %s", ctx.object_id)
        projections = {}
    items = []
    repairs = []
    for video in videos:
        entry = store_entries.get(video.relpath)
        sam3 = sam3_state.get(video.relpath)
        source = derive_pipeline_source(entry, sam3, ctx.output_root)
        record = projections.get(video.video_id)
        pipeline, projection_status = _verified_snapshot(record, source)
        if pipeline is None:
            pipeline = source.snapshot
            if pipeline.stage == "completed":
                pipeline = replace(pipeline, stage="review", status="projection_pending")
            repairs.append({"video_id": video.video_id, "source_identity": source.identity})
        item = video_payload(
            ctx,
            video,
            ctx.index.cached_probe(video.video_id),
            held.get(video.video_id),
            sam3,
            store_entry=entry,
            pipeline=pipeline,
        )
        item["projection_status"] = projection_status
        projected_at = getattr(record, "projected_at", None)
        item["projected_at"] = projected_at.isoformat() if projected_at else None
        items.append(item)
    if repairs:
        try:
            durable_jobs.enqueue_projection_reconciles(ctx.object_id, _next_repair_batch(ctx.object_id, repairs))
        except Exception:
            log.warning("Pipeline projection repair queue unavailable for object %s", ctx.object_id)
    pipeline_counts: dict[str, int] = {}
    pipeline_status_counts: dict[str, int] = {}
    for item in items:
        stage = item["pipeline_stage"]
        stage_status = f"{stage}:{item['stage_status']}"
        pipeline_counts[stage] = pipeline_counts.get(stage, 0) + 1
        pipeline_status_counts[stage_status] = pipeline_status_counts.get(stage_status, 0) + 1

    if search:
        needle = search.lower()
        items = [i for i in items if needle in i["relpath"].lower()]
    if status and status != "all":
        items = [i for i in items if i["status"] == status]

    if sort == "mtime":
        items.sort(key=lambda i: i["file_mtime"], reverse=True)
    elif sort == "size":
        items.sort(key=lambda i: i["size_bytes"], reverse=True)
    elif sort == "status":
        order = {"pending": 0, "in_progress": 1, "done": 2, "no_boom": 3}
        items.sort(key=lambda i: (order.get(i["status"], 9), i["relpath"].lower()))
    else:
        items.sort(key=lambda i: i["relpath"].lower())

    return {
        "object_id": ctx.object_id,
        "label": ctx.label,
        "videos_root": ctx.videos_root.as_posix(),
        "scanned_at": ctx.index.scanned_at,
        "total": counts["total"],
        "counts": counts,
        "pipeline_counts": pipeline_counts,
        "pipeline_status_counts": pipeline_status_counts,
        "videos": items,
    }


@router.get("/videos")
async def list_videos(
    search: str | None = None,
    status: str | None = None,
    sort: str = "name",
    ctx: ObjectContext = Depends(get_object),
) -> dict:
    key = (ctx.object_id, ctx.index.scanned_at, search, status, sort)
    return await _listing_singleflight.run(
        key,
        lambda: asyncio.to_thread(_build_video_listing, ctx, search, status, sort),
    )


@router.post("/videos/rescan")
async def rescan(ctx: ObjectContext = Depends(get_object)) -> dict:
    await asyncio.to_thread(ctx.rescan)
    return await list_videos(ctx=ctx)


@router.get("/videos/{video_id}/thumb")
async def get_thumb(video_id: str, ctx: ObjectContext = Depends(get_object)) -> Response:
    video = ctx.index.get(video_id)
    if video is None:
        raise HTTPException(404, "vídeo não encontrado")

    cached = ctx.index.cached_probe(video_id)
    duration = (cached or {}).get("duration_sec")
    if duration is None:
        try:
            probed = await ctx.index.probe(video_id)
            duration = probed.get("duration_sec")
        except Exception:  # noqa: BLE001 — thumb é cosmética, segue sem duração
            duration = None

    path = await media.ensure_thumb(
        ctx, video_id, ctx.index.resolve_path(video_id), duration
    )
    if path is None:
        raise HTTPException(422, "não foi possível gerar a thumbnail")

    return FileResponse(
        path,
        media_type="image/jpeg",
        headers={"Cache-Control": "public, max-age=86400"},
    )
