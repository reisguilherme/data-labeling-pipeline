"""Leitura e escrita das anotações + disparo do export."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .. import export as export_module
from .. import flags as flags_module
from .. import durable_jobs, proxy
from ..deps import current_client, current_user
from ..locks import locks
from ..models import VideoEntryIn, build_interval, validate_intervals
from ..sam3 import queue as sam3_queue
from ..deps import get_object
from ..users import User
from ..videos import iso
from ..workspace import ObjectContext

router = APIRouter(prefix="/api/objects/{object_id}", tags=["annotations"])

_HISTORY_LIMIT = 20


def _require(ctx: ObjectContext, video_id: str):
    video = ctx.index.get(video_id)
    if video is None:
        raise HTTPException(404, "vídeo não encontrado")
    return video


def _require_lock(ctx: ObjectContext, video_id: str, client_id: str, force: bool) -> None:
    """A trava é validada na ESCRITA, não só na interface.

    Sem isto, uma aba aberta antes de outra pessoa assumir o vídeo sobrescreveria
    o trabalho dela em silêncio — exatamente a falha que a trava existe para
    evitar. `force=1` é a saída consciente.
    """
    if force:
        return
    lock = locks.get(ctx.object_id, video_id)
    if lock is not None and lock.client_id != client_id:
        raise HTTPException(
            409,
            {
                "detail": f"{lock.user} está com este vídeo em triagem",
                "lock": lock.public(),
            },
        )


def _stamp_history(entry: dict, previous: dict, user: User, action: str) -> None:
    history = list(previous.get("history") or [])
    history.append(
        {
            "at": iso(),
            "by": user.user_id,
            "action": action,
            "status": entry.get("status"),
            "intervals": len(entry.get("intervals") or []),
        }
    )
    entry["history"] = history[-_HISTORY_LIMIT:]
    entry["created_by"] = previous.get("created_by") or user.user_id
    entry["updated_by"] = user.user_id


async def _media_for(ctx: ObjectContext, video_id: str) -> dict:
    media = ctx.index.cached_probe(video_id)
    if media is None:
        media = await ctx.index.probe(video_id, count_packets=True)
    exact = proxy.is_complete(ctx, video_id)
    if exact is not None:
        media = {
            **media,
            "frame_count": exact,
            "frame_count_source": "proxy_extraction",
            "frame_count_exact": True,
        }
    return media


@router.get("/annotations")
async def get_all(ctx: ObjectContext = Depends(get_object)) -> dict:
    return ctx.store.doc


@router.get("/annotations/{video_id}")
async def get_one(video_id: str, ctx: ObjectContext = Depends(get_object)) -> dict:
    video = _require(ctx, video_id)
    entry = ctx.store.entry(video.relpath)
    # Sugestão pelo nome do arquivo: pré-marca a categoria em intervalos NOVOS.
    # As regras são POR OBJETO — o vocabulário do acervo de boom não vale para
    # outro objeto, e sugerir sem evidência é pior que deixar em branco.
    suggested = flags_module.suggest_from_name(video.name, ctx.config.suggest_rules)
    lock = locks.get(ctx.object_id, video_id)

    if entry is None:
        return {
            "video_id": video_id,
            "relpath": video.relpath,
            "name": video.name,
            "status": "pending",
            "intervals": [],
            "notes": "",
            "exported_at": None,
            "export": None,
            "suggested_flags": suggested,
            "lock": lock.public() if lock else None,
        }
    return {**entry, "suggested_flags": suggested, "lock": lock.public() if lock else None}


@router.put("/annotations/{video_id}")
async def put_one(
    video_id: str,
    payload: VideoEntryIn,
    force: bool = False,
    ctx: ObjectContext = Depends(get_object),
    user: User = Depends(current_user),
    client_id: str = Depends(current_client),
) -> dict:
    video = _require(ctx, video_id)
    _require_lock(ctx, video_id, client_id, force)
    media = await _media_for(ctx, video_id)

    errors = validate_intervals(payload.intervals, media.get("frame_count"))
    if errors:
        raise HTTPException(422, {"errors": errors})

    width = int(media.get("width") or 0)
    height = int(media.get("height") or 0)
    fps = media.get("fps")
    offset = float(media.get("start_time_sec") or 0.0)

    ordered = sorted(payload.intervals, key=lambda i: i.start_frame)
    intervals = [
        build_interval(
            interval,
            order,
            width=width,
            height=height,
            fps=fps,
            start_time_offset=offset,
            label=ctx.label,
        )
        for order, interval in enumerate(ordered)
    ]

    previous = ctx.store.entry(video.relpath) or {}
    entry = {
        "video_id": video_id,
        "relpath": video.relpath,
        "abspath": video.abspath.as_posix(),
        "name": video.name,
        "size_bytes": video.size_bytes,
        "file_mtime": video.file_mtime,
        "missing": not video.abspath.exists(),
        "media": media,
        "status": payload.status,
        "proxy_mode": proxy.status(ctx, video_id, media.get("frame_count"))["mode"],
        "notes": payload.notes,
        "created_at": previous.get("created_at") or iso(),
        "updated_at": iso(),
        "exported_at": previous.get("exported_at"),
        "export": previous.get("export"),
        "intervals": intervals,
    }
    _stamp_history(entry, previous, user, "save")

    await ctx.store.put_entry(video.relpath, entry)
    return entry


class NoBoomPayload(BaseModel):
    delete_exported: bool = True
    notes: str = ""


@router.post("/annotations/{video_id}/no-object")
async def mark_no_object(
    video_id: str,
    payload: NoBoomPayload,
    force: bool = False,
    ctx: ObjectContext = Depends(get_object),
    user: User = Depends(current_user),
    client_id: str = Depends(current_client),
) -> dict:
    video = _require(ctx, video_id)
    _require_lock(ctx, video_id, client_id, force)
    media = await _media_for(ctx, video_id)
    previous = ctx.store.entry(video.relpath) or {}

    if payload.delete_exported and previous.get("export"):
        export_module.clean_segments(Path(previous["export"]["root"]), set())

    entry = {
        **previous,
        "video_id": video_id,
        "relpath": video.relpath,
        "abspath": video.abspath.as_posix(),
        "name": video.name,
        "media": media,
        "status": "no_boom",
        "intervals": [],
        "notes": payload.notes or previous.get("notes", ""),
        "created_at": previous.get("created_at") or iso(),
        "updated_at": iso(),
        "exported_at": None,
        "export": None,
    }
    _stamp_history(entry, previous, user, "no_object")

    await ctx.store.put_entry(video.relpath, entry)
    return entry


@router.post("/videos/{video_id}/export")
async def export_video(
    video_id: str,
    force: bool = False,
    ctx: ObjectContext = Depends(get_object),
    user: User = Depends(current_user),
    client_id: str = Depends(current_client),
) -> dict:
    video = _require(ctx, video_id)
    _require_lock(ctx, video_id, client_id, force)
    entry = ctx.store.entry(video.relpath)
    if entry is None or not entry.get("intervals"):
        raise HTTPException(409, "salve pelo menos um intervalo antes de exportar")

    if durable_jobs.enabled():
        total = sum(int(interval.get("frame_count") or 0) for interval in entry["intervals"])
        job_id = durable_jobs.create(
            kind="video_export",
            object_id=ctx.object_id,
            payload={
                "video_id": video_id,
                "client_id": client_id,
                "user": user.user_id,
                "total": total,
                "message": "exportando frames",
            },
            priority=50,
            idempotency_key=f"video-export:{ctx.object_id}:{video_id}",
        )
        return {"job_id": job_id, "total": total}

    job = await export_module.export_video(
        ctx, video_id, entry, client_id=client_id, user=user.user_id
    )
    return {"job_id": job.job_id, "total": job.total}


class FinishPayload(BaseModel):
    job_id: str


@router.post("/videos/{video_id}/export/finish")
async def finish_export(
    video_id: str,
    payload: FinishPayload,
    ctx: ObjectContext = Depends(get_object),
    user: User = Depends(current_user),
) -> dict:
    """Grava o resultado do export na anotação, depois que o job termina."""
    from ..jobs import jobs

    video = _require(ctx, video_id)
    local_job = jobs.get(payload.job_id)
    durable_job = None
    if local_job is None and durable_jobs.enabled():
        durable_job = durable_jobs.get(payload.job_id)
    if local_job is None and durable_job is None:
        raise HTTPException(404, "job não encontrado")
    job_object_id = local_job.object_id if local_job is not None else durable_job["object_id"]
    job_state = local_job.state if local_job is not None else durable_job["state"]
    job_result = local_job.result if local_job is not None else durable_job["result"]
    if job_object_id != ctx.object_id:
        raise HTTPException(409, "job pertence a outro objeto")
    if job_state != "done":
        raise HTTPException(409, f"job está em {job_state}")

    def apply(doc: dict) -> dict:
        entry = doc["videos"].get(video.relpath)
        if entry is None:
            raise HTTPException(404, "anotação não encontrada")
        entry["export"] = job_result
        entry["exported_at"] = iso()
        entry["status"] = "done"
        entry["updated_at"] = iso()
        entry["exported_by"] = user.user_id
        return entry

    entry = await ctx.store.mutate(apply)

    # Enfileira para o SAM3 aqui, no servidor, e não no cliente: assim toda
    # exportação entra na fila independentemente de quem exportou ou de qual
    # aba, e o front só precisa saber MOSTRAR o estado, nunca criá-lo.
    if ctx.config.auto_sam3 and job_result.get("segments"):
        sam3_queue.enqueue(
            ctx.object_id,
            video_id=video_id,
            relpath=video.relpath,
            name=video.name,
            export_root=job_result["root"],
            segments=list(job_result["segments"]),
            user=user.user_id,
        )

    return entry
