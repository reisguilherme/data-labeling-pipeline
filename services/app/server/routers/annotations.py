"""Leitura e escrita das anotações + disparo do export."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
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
log = logging.getLogger(__name__)


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
            "revision": entry.get("annotation_revision", 0),
        }
    )
    entry["history"] = history[-_HISTORY_LIMIT:]
    entry["created_by"] = previous.get("created_by") or user.user_id
    entry["updated_by"] = user.user_id


async def _media_for_write(
    ctx: ObjectContext, video_id: str, *, allow_probe: bool = True
) -> dict:
    """Metadata-only media lookup used by latency-sensitive mutations."""
    media = dict(ctx.index.cached_probe(video_id) or {})
    exact = proxy.is_complete(ctx, video_id)
    if not media and allow_probe:
        media = await ctx.index.probe(video_id, count_packets=False)
    if exact is not None:
        media = {
            **media,
            "frame_count": exact,
            "frame_count_source": "proxy_extraction",
            "frame_count_exact": True,
        }
    return media


def _next_revision(previous: dict) -> int:
    value = previous.get("annotation_revision", 0)
    return (value if type(value) is int and value >= 0 else 0) + 1


def _effective_media(previous: dict, media_hint: dict) -> dict:
    return {**(previous.get("media") or {}), **media_hint}


def _proxy_mode(previous: dict, media: dict) -> str:
    if media.get("frame_count_source") == "proxy_extraction":
        return "full"
    prior = previous.get("proxy_mode")
    if prior in {"full", "window"}:
        return prior
    return proxy.plan_for("", media.get("frame_count")).mode


def _cleanup_basename(output_root: Path, export_data: dict | None) -> str | None:
    raw = (export_data or {}).get("root")
    if not isinstance(raw, str) or not raw:
        return None
    try:
        root = Path(raw).resolve()
        parent = output_root.resolve()
    except (OSError, RuntimeError):
        return None
    if root == parent or root.parent != parent or root.name in {"", ".", ".."}:
        return None
    if any(separator in root.name for separator in ("/", "\\")):
        return None
    return root.name


@asynccontextmanager
async def _video_mutation_lock(ctx: ObjectContext, video_id: str):
    if not durable_jobs.enabled():
        yield
        return
    async with durable_jobs.video_advisory_lock_async(ctx.object_id, video_id):
        # Cada processo mantém seu próprio snapshot em memória. O advisory lock
        # serializa o arquivo compartilhado; recarregar aqui impede um processo
        # antigo de apagar a revisão gravada por outro.
        await asyncio.to_thread(ctx.store.load)
        yield


async def _cancel_stale_exports(ctx: ObjectContext, video_id: str, revision: int) -> None:
    if not durable_jobs.enabled():
        return
    try:
        await asyncio.to_thread(
            durable_jobs.cancel_stale_video_exports,
            object_id=ctx.object_id,
            video_id=video_id,
            before_revision=revision,
        )
    except Exception:  # noqa: BLE001 - revision fencing remains authoritative
        log.warning("falha ao cancelar exports antigos de %s", video_id, exc_info=True)


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
    media_hint = await _media_for_write(ctx, video_id)

    errors = validate_intervals(payload.intervals, media_hint.get("frame_count"))
    if errors:
        raise HTTPException(422, {"errors": errors})

    ordered = sorted(payload.intervals, key=lambda i: i.start_frame)
    def apply(doc: dict) -> dict:
        previous = doc["videos"].get(video.relpath) or {}
        media = _effective_media(previous, media_hint)
        width = int(media.get("width") or 0)
        height = int(media.get("height") or 0)
        fps = media.get("fps")
        offset = float(media.get("start_time_sec") or 0.0)
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
            "proxy_mode": _proxy_mode(previous, media),
            "notes": payload.notes,
            "created_at": previous.get("created_at") or iso(),
            "updated_at": iso(),
            "exported_at": previous.get("exported_at"),
            "export": previous.get("export"),
            "intervals": intervals,
            "annotation_revision": _next_revision(previous),
        }
        _stamp_history(entry, previous, user, "save")
        doc["videos"][video.relpath] = entry
        return entry

    async with _video_mutation_lock(ctx, video_id):
        entry = await ctx.store.mutate(apply)
    await _cancel_stale_exports(ctx, video_id, entry["annotation_revision"])
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
    media_hint = await _media_for_write(ctx, video_id, allow_probe=False)
    cleanup: dict | None = None

    def apply(doc: dict) -> dict:
        nonlocal cleanup
        previous = doc["videos"].get(video.relpath) or {}
        revision = _next_revision(previous)
        previous_export = previous.get("export")
        basename = _cleanup_basename(ctx.output_root, previous_export)
        if payload.delete_exported and previous_export:
            cleanup = {
                "status": "pending" if basename else "deferred",
                "annotation_revision": revision,
                "root_basename": basename,
                "previous_export": previous_export,
                "requested_at": iso(),
            }
        entry = {
            **previous,
            "video_id": video_id,
            "relpath": video.relpath,
            "abspath": video.abspath.as_posix(),
            "name": video.name,
            "media": _effective_media(previous, media_hint),
            "status": "no_boom",
            "intervals": [],
            "notes": payload.notes or previous.get("notes", ""),
            "created_at": previous.get("created_at") or iso(),
            "updated_at": iso(),
            "exported_at": None,
            "export": None,
            "annotation_revision": revision,
        }
        if cleanup is not None:
            entry["export_cleanup"] = cleanup
        else:
            entry.pop("export_cleanup", None)
        _stamp_history(entry, previous, user, "no_object")
        doc["videos"][video.relpath] = entry
        return entry

    async with _video_mutation_lock(ctx, video_id):
        entry = await ctx.store.mutate(apply)
    revision = entry["annotation_revision"]
    await _cancel_stale_exports(ctx, video_id, revision)
    if cleanup is not None and cleanup.get("root_basename") and durable_jobs.enabled():
        try:
            cleanup_job_id = await asyncio.to_thread(
                durable_jobs.create,
                kind="video_export_cleanup",
                object_id=ctx.object_id,
                payload={
                    "video_id": video_id,
                    "relpath": video.relpath,
                    "annotation_revision": revision,
                    "root_basename": cleanup["root_basename"],
                    "previous_export": cleanup["previous_export"],
                    "message": "limpando export anterior",
                },
                priority=45,
                idempotency_key=(
                    f"video-export-cleanup:{ctx.object_id}:{video_id}:r{revision}"
                ),
            )
            cleanup["status"] = "queued"
            cleanup["job_id"] = cleanup_job_id
            entry["cleanup_job_id"] = cleanup_job_id
        except Exception:  # decision is already durable
            cleanup["status"] = "deferred"
            cleanup["error"] = "fila de limpeza temporariamente indisponivel"
            log.warning(
                "falha ao enfileirar cleanup de export para %s",
                video_id,
                exc_info=True,
            )
        async with _video_mutation_lock(ctx, video_id):
            await ctx.store.mutate(
                lambda doc: _update_cleanup_marker(
                    doc, video.relpath, revision, cleanup, entry.get("cleanup_job_id")
                )
            )
    elif cleanup is not None and cleanup.get("status") == "pending":
        cleanup["status"] = "deferred"
        cleanup["error"] = "fila duravel indisponivel"
        async with _video_mutation_lock(ctx, video_id):
            await ctx.store.mutate(
                lambda doc: _update_cleanup_marker(doc, video.relpath, revision, cleanup, None)
            )
    return entry


def _update_cleanup_marker(
    doc: dict, relpath: str, revision: int, cleanup: dict, job_id: str | None
) -> dict:
    current = doc["videos"].get(relpath)
    if current is None or current.get("annotation_revision", 0) != revision:
        return current or {}
    current["export_cleanup"] = cleanup
    if job_id:
        current["cleanup_job_id"] = job_id
    else:
        current.pop("cleanup_job_id", None)
    current["updated_at"] = iso()
    return current


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

    if durable_jobs.enabled():
        async with _video_mutation_lock(ctx, video_id):
            entry = ctx.store.entry(video.relpath)
            if entry is None or not entry.get("intervals"):
                raise HTTPException(409, "salve pelo menos um intervalo antes de exportar")
            total = sum(
                int(interval.get("frame_count") or 0) for interval in entry["intervals"]
            )
            revision = int(entry.get("annotation_revision") or 0)
            export_root = await asyncio.to_thread(export_module.export_root_for, ctx, video)
            job_id = await asyncio.to_thread(
                durable_jobs.create,
                kind="video_export",
                object_id=ctx.object_id,
                payload={
                    "video_id": video_id,
                    "relpath": video.relpath,
                    "client_id": client_id,
                    "user": user.user_id,
                    "total": total,
                    "message": "exportando frames",
                    "annotation_revision": revision,
                    "root_basename": export_root.name,
                },
                priority=50,
                idempotency_key=f"video-export:{ctx.object_id}:{video_id}:r{revision}",
            )
        return {"job_id": job_id, "total": total}

    if entry is None or not entry.get("intervals"):
        raise HTTPException(409, "salve pelo menos um intervalo antes de exportar")
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
        durable_job = await asyncio.to_thread(durable_jobs.get, payload.job_id)
    if local_job is None and durable_job is None:
        raise HTTPException(404, "job não encontrado")
    job_object_id = local_job.object_id if local_job is not None else durable_job["object_id"]
    job_state = local_job.state if local_job is not None else durable_job["state"]
    job_result = local_job.result if local_job is not None else durable_job["result"]
    job_revision = (
        job_result.get("annotation_revision")
        if local_job is not None
        else durable_job.get("annotation_revision")
    )
    if job_object_id != ctx.object_id:
        raise HTTPException(409, "job pertence a outro objeto")
    if job_state != "done":
        raise HTTPException(409, f"job está em {job_state}")

    def apply(doc: dict) -> dict:
        entry = doc["videos"].get(video.relpath)
        if entry is None:
            raise HTTPException(404, "anotação não encontrada")
        current_revision = int(entry.get("annotation_revision") or 0)
        if (job_revision is None and current_revision != 0) or (
            job_revision is not None and current_revision != int(job_revision)
        ):
            raise HTTPException(
                409,
                "resultado pertence a uma revisao antiga da anotacao",
            )
        entry["export"] = job_result
        entry["exported_at"] = iso()
        entry["status"] = "done"
        entry["updated_at"] = iso()
        entry["exported_by"] = user.user_id
        return entry

    async with _video_mutation_lock(ctx, video_id):
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
