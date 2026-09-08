"""Listagem da biblioteca de um objeto."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException
from starlette.responses import FileResponse, Response

from .. import media
from ..deps import get_object
from ..locks import locks
from ..pipeline_state import inspect_pipeline_entry
from ..sam3 import queue as sam3_queue
from ..workspace import ObjectContext

router = APIRouter(prefix="/api/objects/{object_id}", tags=["library"])


def video_payload(
    ctx: ObjectContext, video, media_info: dict | None, lock=None, sam3=None
) -> dict:
    entry = ctx.store.entry(video.relpath)
    status = entry.get("status", "pending") if entry else "pending"
    intervals = entry.get("intervals", []) if entry else []
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


@router.get("/videos")
async def list_videos(
    search: str | None = None,
    status: str | None = None,
    sort: str = "name",
    ctx: ObjectContext = Depends(get_object),
) -> dict:
    held = locks.map_for(ctx.object_id)
    sam3_state = sam3_queue.map_for(ctx.object_id)
    items = [
        video_payload(
            ctx,
            video,
            ctx.index.cached_probe(video.video_id),
            held.get(video.video_id),
            sam3_state.get(video.relpath),
        )
        for video in ctx.index.all()
    ]
    counts = ctx.store.counts(len(items))
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
