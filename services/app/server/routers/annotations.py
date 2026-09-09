"""Leitura e escrita das anotações + disparo do export."""

from __future__ import annotations

import asyncio
import copy
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from .. import export as export_module
from .. import flags as flags_module
from .. import durable_jobs, proxy
from ..deps import current_client, current_user
from ..locks import locks
from ..models import VideoEntryIn, build_interval, validate_intervals
from ..deps import get_object
from ..users import User
from ..videos import iso
from ..video_export_completion import (
    ExportCompletionConflict,
    finalize_video_export,
)
from ..workspace import ObjectContext
from .sam3 import require_worker

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
    if not isinstance(export_data, dict):
        return None
    raw = (export_data or {}).get("root")
    if not isinstance(raw, str) or not raw:
        return None
    original = Path(raw)
    if original.is_symlink():
        return None
    try:
        root = original.resolve()
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
    should_enqueue = False
    revision_changed = False

    def apply(doc: dict) -> dict:
        nonlocal cleanup, should_enqueue, revision_changed
        previous = doc["videos"].get(video.relpath) or {}
        previous_cleanup = previous.get("export_cleanup")
        requested_notes = payload.notes or previous.get("notes", "")
        same_decision = (
            previous.get("status") == "no_boom"
            and not previous.get("intervals")
            and previous.get("export") is None
            and requested_notes == previous.get("notes", "")
        )
        if same_decision:
            entry = copy.deepcopy(previous)
            cleanup = (
                copy.deepcopy(previous_cleanup)
                if isinstance(previous_cleanup, dict)
                else None
            )
            if cleanup is not None:
                previous_export = cleanup.get("previous_export")
                previous_export_data = (
                    previous_export if isinstance(previous_export, dict) else {}
                )
                prior_owner = export_module.valid_export_owner(cleanup.get("owner"))
                owner_revision = previous_export_data.get(
                    "annotation_revision",
                    (prior_owner or {}).get("annotation_revision", 0),
                )
                if type(owner_revision) is not int or owner_revision < 0:
                    owner_revision = 0
                cleanup["owner"] = export_module.export_owner(
                    ctx, video, owner_revision
                )
                safe_basename = _cleanup_basename(ctx.output_root, previous_export)
                if safe_basename != cleanup.get("root_basename"):
                    cleanup["status"] = "deferred"
                    cleanup["root_basename"] = None
                    cleanup["error"] = "raiz legada sem ownership verificavel"
                    entry["export_cleanup"] = cleanup
            if (
                payload.delete_exported
                and cleanup is not None
                and cleanup.get("status") in {"pending", "deferred", "queued"}
                and cleanup.get("root_basename")
            ):
                cleanup["status"] = "pending"
                cleanup.pop("error", None)
                cleanup.pop("job_id", None)
                entry["export_cleanup"] = cleanup
                entry.pop("cleanup_job_id", None)
                should_enqueue = True
            doc["videos"][video.relpath] = entry
            return entry

        revision = _next_revision(previous)
        revision_changed = True
        previous_export = previous.get("export")
        basename = _cleanup_basename(ctx.output_root, previous_export)
        if payload.delete_exported and previous_export:
            owner_revision = (
                previous_export.get(
                    "annotation_revision", previous.get("annotation_revision", 0)
                )
                if isinstance(previous_export, dict)
                else previous.get("annotation_revision", 0)
            )
            if type(owner_revision) is not int or owner_revision < 0:
                owner_revision = 0
            owner = export_module.export_owner(ctx, video, owner_revision)
            cleanup = {
                "status": "pending" if basename else "deferred",
                "annotation_revision": revision,
                "root_basename": basename,
                "owner": owner,
                "previous_export": previous_export,
                "requested_at": iso(),
            }
            should_enqueue = basename is not None
        entry = {
            **previous,
            "video_id": video_id,
            "relpath": video.relpath,
            "abspath": video.abspath.as_posix(),
            "name": video.name,
            "media": _effective_media(previous, media_hint),
            "status": "no_boom",
            "intervals": [],
            "notes": requested_notes,
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
    if revision_changed:
        await _cancel_stale_exports(ctx, video_id, revision)
    if should_enqueue and cleanup is not None and durable_jobs.enabled():
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
                    "owner": cleanup["owner"],
                    "previous_export": cleanup["previous_export"],
                    "message": "limpando export anterior",
                },
                priority=45,
                idempotency_key=(
                    f"video-export-cleanup:{ctx.object_id}:{video_id}:r{revision}"
                ),
            )
            updated_cleanup = {**cleanup, "status": "queued", "job_id": cleanup_job_id}
            updated_cleanup.pop("error", None)
        except Exception:  # decision is already durable
            cleanup_job_id = None
            updated_cleanup = {
                **cleanup,
                "status": "deferred",
                "error": "fila de limpeza temporariamente indisponivel",
            }
            updated_cleanup.pop("job_id", None)
            log.warning(
                "falha ao enfileirar cleanup de export para %s",
                video_id,
                exc_info=True,
            )
        async with _video_mutation_lock(ctx, video_id):
            entry = await ctx.store.mutate(
                lambda doc: _update_cleanup_marker(
                    doc, video.relpath, revision, updated_cleanup, cleanup_job_id
                )
            )
    elif should_enqueue and cleanup is not None:
        updated_cleanup = {
            **cleanup,
            "status": "deferred",
            "error": "fila duravel indisponivel",
        }
        updated_cleanup.pop("job_id", None)
        async with _video_mutation_lock(ctx, video_id):
            entry = await ctx.store.mutate(
                lambda doc: _update_cleanup_marker(
                    doc, video.relpath, revision, updated_cleanup, None
                )
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
            owner = export_module.export_owner(ctx, video, revision)
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
                    "owner": owner,
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


_SegmentName = Annotated[
    str, StringConstraints(min_length=5, max_length=128, pattern=r"^seg_[A-Za-z0-9_-]+$")
]


class WorkerExportOwner(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1]
    object_id: str = Field(min_length=1, max_length=128)
    video_id: str = Field(min_length=1, max_length=128)
    relpath: str = Field(min_length=1, max_length=4096)
    annotation_revision: int = Field(ge=0, strict=True)


class WorkerExportResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    root: str = Field(min_length=1, max_length=4096)
    segments: list[_SegmentName] = Field(min_length=1, max_length=10_000)
    total_frames: int = Field(ge=0, strict=True)
    jpeg_qscale: int = Field(ge=1, le=31, strict=True)
    frame_naming: Literal["restart_per_segment"]
    ffmpeg_version: str = Field(min_length=1, max_length=512)
    annotation_revision: int = Field(ge=0, strict=True)
    owner: WorkerExportOwner


class WorkerExportCompletion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: UUID
    lease_token: UUID
    result: WorkerExportResult


@router.post("/videos/{video_id}/export/complete")
async def complete_export_internal(
    video_id: str,
    payload: WorkerExportCompletion,
    ctx: ObjectContext = Depends(get_object),
    _: str = Depends(require_worker),
) -> dict:
    """Commit a frame export while the reporting CPU worker still owns its lease."""
    job_id = str(payload.job_id)
    lease_token = str(payload.lease_token)
    video = _require(ctx, video_id)
    result = payload.result.model_dump(mode="python")
    async with durable_jobs.video_advisory_lock_async(ctx.object_id, video_id):
        await asyncio.to_thread(ctx.store.load)
        async with durable_jobs.owned_job_lease_async(
            job_id, lease_token
        ) as job:
            if job is None:
                raise HTTPException(409, "lease de export invalida ou expirada")
            if (
                job.get("job_id") != job_id
                or job.get("kind") != "video_export"
                or job.get("worker_kind") != "cpu"
                or job.get("object_id") != ctx.object_id
                or job.get("video_id") != video_id
                or job.get("relpath") != video.relpath
            ):
                raise HTTPException(409, "job nao pertence a este export de video")

            owner = job.get("owner")
            revision = job.get("annotation_revision")
            total = job.get("total")
            basename = job.get("root_basename")
            if (
                type(revision) is not int
                or revision < 0
                or type(total) is not int
                or total < 0
                or not isinstance(basename, str)
                or not basename
                or not isinstance(job.get("user"), str)
                or not job["user"]
            ):
                raise HTTPException(409, "payload autoritativo do job e invalido")

            try:
                finalized = await finalize_video_export(
                    ctx,
                    video_id,
                    result,
                    job["user"],
                    job_id,
                    expected_revision=revision,
                    expected_root_basename=basename,
                    expected_owner=owner,
                    expected_total=total,
                    _video_lock_held=True,
                )
            except ExportCompletionConflict as exc:
                raise HTTPException(409, str(exc)) from exc
    return {
        "ok": True,
        "digest": finalized.digest,
        "replayed": finalized.replayed,
    }


@router.post("/videos/{video_id}/export/finish")
async def finish_export(
    video_id: str,
    payload: FinishPayload,
    ctx: ObjectContext = Depends(get_object),
    user: User = Depends(current_user),
) -> dict:
    """Compatibility endpoint; correctness no longer depends on the browser."""
    from ..jobs import jobs

    video = _require(ctx, video_id)
    local_job = jobs.get(payload.job_id)
    durable_job = None
    if local_job is None and durable_jobs.enabled():
        durable_job = await asyncio.to_thread(durable_jobs.get, payload.job_id)
    if local_job is None and durable_job is None:
        raise HTTPException(404, "job não encontrado")
    job_object_id = local_job.object_id if local_job is not None else durable_job["object_id"]
    job_kind = local_job.kind if local_job is not None else durable_job["kind"]
    job_video_id = local_job.video_id if local_job is not None else durable_job["video_id"]
    job_state = local_job.state if local_job is not None else durable_job["state"]
    job_result = local_job.result if local_job is not None else durable_job["result"]
    job_revision = (
        job_result.get("annotation_revision")
        if local_job is not None
        else durable_job.get("annotation_revision")
    )
    expected_kind = "export" if local_job is not None else "video_export"
    if job_kind != expected_kind:
        raise HTTPException(409, "job tem tipo diferente de video_export")
    if job_video_id != video_id:
        raise HTTPException(409, "job pertence a outro video")
    if job_object_id != ctx.object_id:
        raise HTTPException(409, "job pertence a outro objeto")
    if job_state != "done":
        raise HTTPException(409, f"job está em {job_state}")

    if not isinstance(job_result, dict):
        raise HTTPException(409, "job nao possui resultado valido")
    result_revision = job_result.get("annotation_revision")
    if (
        type(job_revision) is not int
        or job_revision < 0
        or type(result_revision) is not int
        or result_revision != job_revision
    ):
        raise HTTPException(409, "resultado possui revisao invalida")
    expected_basename = (
        export_module.export_root_for(ctx, video).name
        if local_job is not None
        else durable_job.get("root_basename")
    )
    result_basename = _cleanup_basename(ctx.output_root, job_result)
    if not expected_basename or result_basename != expected_basename:
        raise HTTPException(409, "resultado possui raiz de export inesperada")
    expected_owner = (
        job_result.get("owner") if local_job is not None else durable_job.get("owner")
    )
    expected_total = (
        job_result.get("total_frames")
        if local_job is not None
        else durable_job.get("total")
    )
    try:
        finalized = await finalize_video_export(
            ctx,
            video_id,
            job_result,
            user.user_id,
            payload.job_id,
            expected_revision=job_revision,
            expected_root_basename=expected_basename,
            expected_owner=expected_owner,
            expected_total=expected_total,
        )
    except ExportCompletionConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    return finalized.entry
