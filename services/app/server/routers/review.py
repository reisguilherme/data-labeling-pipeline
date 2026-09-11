"""Revisão das bboxes pré-anotadas pelo SAM3."""

from __future__ import annotations

import hashlib
import logging
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Iterator

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from starlette.responses import FileResponse

from .. import durable_jobs, review as review_module
from ..deps import current_client, current_user, get_object
from ..locks import locks
from ..pipeline_mutation import reserve_mutation
from ..pipeline_state import certify_completed_review
from ..mask_api import decode_mask_edits, serialize_frame_state
from ..review import SegmentPaths
from ..sam3 import queue as sam3_queue
from ..sam3_run_index import index_revision
from ..users import User
from ..video_fence import async_video_fence, video_fence
from ..workspace import ObjectContext, workspace
from pipeline_core.masks import MaskValidationError
from pipeline_core.review_store import FileMaskReviewStore, RevisionConflict
from pipeline_core.sam3_runs import (
    Sam3GenerationError,
    load_current_manifest,
    resolve_export_root,
    resolve_segment_dir,
)

router = APIRouter(prefix="/api/objects/{object_id}", tags=["review"])
log = logging.getLogger(__name__)


def _mask_run_unavailable() -> HTTPException:
    return HTTPException(
        409,
        {
            "code": "mask_run_unavailable",
            "detail": "run legado sem máscaras; reprocesse com o SAM3",
        },
    )


def _review_identity(frames: dict) -> dict:
    """Prospective effective review content; audit timestamps are not a retry key."""
    return {
        str(frame): {
            key: entry[key]
            for key in ("revision", "status", "boxes", "instances", "deleted_obj_ids")
            if key in entry
        }
        for frame, entry in frames.items()
    }


def _export_version(root: Path) -> str:
    """Fence both the exported frames and the active immutable SAM3 run."""
    marker = root / ".export-owner.json"
    target = marker if marker.is_file() and not marker.is_symlink() else root
    try:
        stat = target.stat()
    except OSError as exc:
        raise HTTPException(409, "export indisponivel durante publicacao") from exc
    try:
        generation = load_current_manifest(root)
    except Sam3GenerationError as exc:
        raise HTTPException(409, f"geracao SAM3 indisponivel: {exc}") from exc
    generation_identity = (
        "legacy"
        if generation is None
        else ":".join(
            (
                str(generation.get("generation_id") or ""),
                str(generation.get("manifest_sha256") or ""),
            )
        )
    )
    identity = ":".join(
        str(value)
        for value in (
            getattr(stat, "st_dev", 0),
            getattr(stat, "st_ino", 0),
            stat.st_mtime_ns,
            stat.st_size,
            generation_identity,
        )
    )
    return hashlib.blake2s(identity.encode("ascii"), digest_size=12).hexdigest()


def _require_export_version(root: Path, expected: str) -> None:
    if not expected or expected != _export_version(root):
        raise HTTPException(
            409,
            "o trecho foi republicado; recarregue a revisao antes de salvar",
        )


@contextmanager
def _locked_export(
    ctx: ObjectContext,
    video_id: str,
    client_id: str,
    expected_version: str,
) -> Iterator[tuple[Path, list[str], object]]:
    """Compartilha o fence do publisher e resolve o export só após adquiri-lo."""
    with video_fence(ctx.object_id, video_id):
        root, segments, video = _export_root(ctx, video_id)
        _require_lock(ctx, video_id, client_id)
        _require_export_version(root, expected_version)
        yield root, segments, video


def _export_root(ctx: ObjectContext, video_id: str) -> tuple[Path, list[str], object]:
    video = ctx.index.get(video_id)
    if video is None:
        raise HTTPException(404, "vídeo não encontrado")
    entry = ctx.store.entry(video.relpath) or {}
    export = entry.get("export")
    if not export or not export.get("segments"):
        raise HTTPException(409, "este vídeo ainda não foi exportado")

    try:
        root = resolve_export_root(ctx.output_root, export["root"])
    except Sam3GenerationError as exc:
        raise HTTPException(409, str(exc)) from exc
    try:
        generation = load_current_manifest(root)
    except Sam3GenerationError as exc:
        raise HTTPException(409, f"geracao SAM3 indisponivel: {exc}") from exc
    if generation is not None:
        generation_revision = generation.get("annotation_revision")
        current_revision = entry.get("annotation_revision", 0)
        if (
            type(generation_revision) is not int
            or type(current_revision) is not int
            or generation_revision != current_revision
        ):
            raise HTTPException(
                409,
                "geracao SAM3 pertence a outra revisao da anotacao; "
                "aguarde ou reprocesse",
            )
    return root, list(export["segments"]), video


def _segment_paths(root: Path, segments: list[str], segment: str) -> SegmentPaths:
    # `segment` vem da URL: só aceita o que o próprio export declarou, o que
    # dispensa qualquer sanitização de caminho.
    if segment not in segments:
        raise HTTPException(404, f"segmento '{segment}' não pertence a este vídeo")
    try:
        return SegmentPaths(resolve_segment_dir(root, segment))
    except Sam3GenerationError as exc:
        raise HTTPException(409, str(exc)) from exc


def _frame_count(paths: SegmentPaths) -> int:
    import json

    if paths.marker_path.exists():
        try:
            value = json.loads(paths.marker_path.read_text(encoding="utf-8")).get("frame_count")
            if value:
                return int(value)
        except (OSError, json.JSONDecodeError, ValueError):
            pass
    return len(sorted(paths.segment_dir.glob("*.jpg")))


def _mask_store(paths: SegmentPaths) -> FileMaskReviewStore:
    prompt = review_module.prompt_contract(paths)
    width = int(prompt.get("image_width") or 0)
    height = int(prompt.get("image_height") or 0)
    if width <= 0 or height <= 0:
        raise HTTPException(409, "prompt sem dimensões válidas para revisar máscaras")
    labels = {
        int(item["obj_id"]): item.get("label") or ""
        for item in prompt.get("objects") or []
    }
    return FileMaskReviewStore(paths.out_dir, image_size=(width, height), labels=labels)


# --------------------------------------------------------------------------
# frames do segmento
# --------------------------------------------------------------------------


@router.get("/videos/{video_id}/segments/{segment}/frames/{frame}.jpg")
async def segment_frame(
    video_id: str,
    segment: str,
    frame: int,
    version: str | None = None,
    ctx: ObjectContext = Depends(get_object),
):
    """Frame do EXPORT, não do cache de proxy.

    O proxy é descartável e tem evicção LRU; os frames do export são o que o
    SAM3 realmente consumiu e o que vai para o dataset. Revisar contra outra
    imagem que não a anotada seria revisar a coisa errada.
    """
    async with async_video_fence(ctx.object_id, video_id):
        root, segments, _ = _export_root(ctx, video_id)
        paths = _segment_paths(root, segments, segment)
        path = paths.frame_path(frame)
        if not path.exists():
            raise HTTPException(404, "frame não existe neste segmento")
        current_version = _export_version(root)
        if version is not None and version != current_version:
            raise HTTPException(409, "o trecho foi republicado; recarregue a revisao")
    return FileResponse(
        path,
        media_type="image/jpeg",
        headers={
            "Cache-Control": (
                "public, max-age=31536000, immutable"
                if version == current_version
                else "private, max-age=0, must-revalidate"
            )
        },
    )


# --------------------------------------------------------------------------
# estado da revisão
# --------------------------------------------------------------------------


@router.get("/videos/{video_id}/review")
async def video_review(video_id: str, ctx: ObjectContext = Depends(get_object)) -> dict:
    """Progresso do vídeo inteiro, sem carregar as caixas."""
    async with async_video_fence(ctx.object_id, video_id):
        root, segments, video = _export_root(ctx, video_id)
        names = review_module.class_names(workspace.root) if workspace.root else []
        progress = review_module.progress_for(root, segments, names)
    return {
        "video_id": video_id,
        "name": video.name,
        "relpath": video.relpath,
        "export_root": root.as_posix(),
        "classes": names,
        **progress,
    }


@router.get("/videos/{video_id}/segments/{segment}/review")
async def segment_review(
    video_id: str, segment: str, ctx: ObjectContext = Depends(get_object)
) -> dict:
    """Todas as caixas do segmento, já com o delta aplicado.

    Payload único em vez de uma chamada por frame: são poucos KB mesmo no
    segmento de 799 frames, e a navegação frame a frame precisa ser instantânea.
    """
    async with async_video_fence(ctx.object_id, video_id):
        root, segments, video = _export_root(ctx, video_id)
        paths = _segment_paths(root, segments, segment)
        names = review_module.class_names(workspace.root) if workspace.root else []
        state = review_module.segment_state(paths, _frame_count(paths), names)
        prompt = review_module.prompt_contract(paths)
        export_version = _export_version(root)

    return {
        "video_id": video_id,
        "name": video.name,
        "segment": segment,
        "segments": segments,
        "classes": names,
        "label": ctx.label,
        "export_version": export_version,
        "prompt": prompt,
        **state,
    }


class MaskInstanceIn(BaseModel):
    obj_id: int = Field(gt=0)
    label: str = ""
    png_base64: str


class MaskFrameIn(BaseModel):
    expected_revision: int = Field(ge=0)
    status: str
    instances: list[MaskInstanceIn] = Field(default_factory=list)
    retain_obj_ids: list[int] = Field(default_factory=list)


class MaskFrameCommitIn(MaskFrameIn):
    export_version: str = Field(min_length=8, max_length=128)


class MaskBatchFrameIn(MaskFrameIn):
    frame: int = Field(ge=0)


class MaskBatchIn(BaseModel):
    export_version: str = Field(min_length=8, max_length=128)
    # Lote vazio é uma finalização idempotente: útil quando os frames já foram
    # persistidos, mas a projeção ainda não foi promovida para Concluídos.
    frames: list[MaskBatchFrameIn] = Field(default_factory=list)


def _mask_url(ctx: ObjectContext, video_id: str, segment: str, frame: int, obj_id: int) -> str:
    return (
        f"/api/objects/{ctx.object_id}/videos/{video_id}/segments/{segment}/"
        f"mask-review/{frame}/{obj_id}.png"
    )


@router.get("/videos/{video_id}/segments/{segment}/mask-review/{frame}")
async def mask_review_state(
    video_id: str,
    segment: str,
    frame: int,
    export_version: str | None = None,
    ctx: ObjectContext = Depends(get_object),
) -> dict:
    async with async_video_fence(ctx.object_id, video_id):
        root, segments, _ = _export_root(ctx, video_id)
        if export_version is not None:
            _require_export_version(root, export_version)
        paths = _segment_paths(root, segments, segment)
        if not (0 <= frame < _frame_count(paths)):
            raise HTTPException(404, "frame não existe neste segmento")
        if not (paths.out_dir / "masks").is_dir():
            raise _mask_run_unavailable()
        state = _mask_store(paths).get_frame(frame)
    return serialize_frame_state(
        state,
        mask_url=lambda obj_id: _mask_url(ctx, video_id, segment, frame, obj_id),
    )


@router.get("/videos/{video_id}/segments/{segment}/mask-review/{frame}/{obj_id}.png")
async def mask_review_image(
    video_id: str,
    segment: str,
    frame: int,
    obj_id: int,
    revision: int | None = None,
    sha256: str | None = None,
    ctx: ObjectContext = Depends(get_object),
):
    async with async_video_fence(ctx.object_id, video_id):
        root, segments, _ = _export_root(ctx, video_id)
        paths = _segment_paths(root, segments, segment)
        state = _mask_store(paths).get_frame(frame)
        instance = next((item for item in state.instances if item.obj_id == obj_id), None)
        if instance is None or not instance.path.exists():
            raise HTTPException(404, "máscara não encontrada")
        versioned = revision is not None or sha256 is not None
        if versioned and (
            revision != state.revision or sha256 != instance.info.sha256
        ):
            raise HTTPException(409, "a mascara mudou; recarregue o frame")
    return FileResponse(
        instance.path,
        media_type="image/png",
        headers={
            "Cache-Control": (
                "private, max-age=31536000, immutable"
                if versioned
                else "private, max-age=0, must-revalidate"
            )
        },
    )


@router.put("/videos/{video_id}/segments/{segment}/mask-review/{frame}")
def save_mask_review(
    video_id: str,
    segment: str,
    frame: int,
    payload: MaskFrameCommitIn,
    ctx: ObjectContext = Depends(get_object),
    user: User = Depends(current_user),
    client_id: str = Depends(current_client),
) -> dict:
    mutation = None
    try:
        edits = decode_mask_edits([item.model_dump() for item in payload.instances])
        with _locked_export(
            ctx, video_id, client_id, payload.export_version
        ) as (root, segments, video), ExitStack() as publication:
            paths = _segment_paths(root, segments, segment)
            if not (0 <= frame < _frame_count(paths)):
                raise HTTPException(404, "frame não existe neste segmento")
            if not (paths.out_dir / "masks").is_dir():
                raise _mask_run_unavailable()
            def before_commit(entry):
                nonlocal mutation
                mutation = reserve_mutation(ctx, video_id, "mask_review_saved", {
                    "export_version": payload.export_version, "segment": segment,
                    "frames": _review_identity({str(frame): entry}),
                })
                index_revision(object_id=ctx.object_id, relpath=video.relpath,
                               segment_dir=paths.segment_dir, frame_idx=frame,
                               entry=entry, user=user.user_id, publication_guard=publication)
            state = _mask_store(paths).save_frame(
                frame,
                expected_revision=payload.expected_revision,
                status=payload.status,
                instances=edits,
                retain_obj_ids=payload.retain_obj_ids,
                user=user.user_id,
                before_commit=before_commit,
            )
    except RevisionConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except (MaskValidationError, ValueError) as exc:
        raise HTTPException(422, str(exc)) from exc
    response = serialize_frame_state(
        state,
        mask_url=lambda obj_id: _mask_url(ctx, video_id, segment, frame, obj_id),
    )
    return {**response, **mutation.complete()}


@router.put("/videos/{video_id}/segments/{segment}/mask-review")
def save_mask_review_batch(
    video_id: str,
    segment: str,
    payload: MaskBatchIn,
    ctx: ObjectContext = Depends(get_object),
    user: User = Depends(current_user),
    client_id: str = Depends(current_client),
) -> dict:
    """Confirma o lote localmente e delega índices/espelhos ao worker CPU."""
    frame_numbers = [item.frame for item in payload.frames]
    mutation = None
    sync_frames: list[dict] = []
    sync_review_sha256 = ""
    certified_source = None
    completion_error: str | None = None
    if len(set(frame_numbers)) != len(frame_numbers):
        raise HTTPException(422, "o lote contém frames duplicados")
    try:
        updates = [
            {
                "frame": item.frame,
                "expected_revision": item.expected_revision,
                "status": item.status,
                "instances": decode_mask_edits(
                    [instance.model_dump() for instance in item.instances]
                ),
                "retain_obj_ids": item.retain_obj_ids,
            }
            for item in payload.frames
        ]
        with _locked_export(
            ctx, video_id, client_id, payload.export_version
        ) as (root, segments, video):
            paths = _segment_paths(root, segments, segment)
            frame_count = _frame_count(paths)
            invalid = [frame for frame in frame_numbers if frame >= frame_count]
            if invalid:
                raise HTTPException(404, f"frame não existe neste segmento: {invalid[0]}")
            if not (paths.out_dir / "masks").is_dir():
                raise _mask_run_unavailable()

            store = _mask_store(paths)
            prospective: list[tuple[int, dict]] = []

            def reserve_batch(frame: int, entry: dict) -> None:
                nonlocal mutation
                prospective.append((frame, entry))
                if len(prospective) == len(updates):
                    mutation = reserve_mutation(ctx, video_id, "mask_review_saved", {
                        "export_version": payload.export_version, "segment": segment,
                        "frames": _review_identity(dict(prospective)),
                    })

            if updates:
                states = store.save_frames(
                    updates,
                    user=user.user_id,
                    before_commit=reserve_batch,
                    # MinIO é réplica, não o commit da revisão. A cópia é feita
                    # por um job durável depois que a resposta interativa sai.
                    mirror=False,
                )
            else:
                states = []
                mutation = reserve_mutation(
                    ctx,
                    video_id,
                    "mask_review_finalized",
                    {
                        "export_version": payload.export_version,
                        "segment": segment,
                        "frames": {},
                    },
                )

            import json

            manifest = json.loads(store.manifest_path.read_text(encoding="utf-8"))
            manifest_frames = manifest.get("frames") or {}
            reviewed = sum(
                1
                for entry in manifest_frames.values()
                if entry.get("status") in {"ok", "edited"}
            )
            sync_frames = [
                {
                    "frame": state.frame,
                    "revision": state.revision,
                }
                for state in states
            ]
            sync_review_sha256 = hashlib.sha256(
                store.manifest_path.read_bytes()
            ).hexdigest()
            names = review_module.class_names(workspace.root) if workspace.root else []
            video_progress = review_module.progress_for(root, segments, names)
            response = {
                "frames": [
                    {
                        "frame": state.frame,
                        "revision": state.revision,
                        "status": state.status,
                    }
                    for state in states
                ],
                "reviewed": reviewed,
                "frame_count": frame_count,
                "complete": reviewed >= frame_count and frame_count > 0,
                "video": video_progress,
            }

            if video_progress["complete"]:
                try:
                    certified_source = certify_completed_review(
                        ctx.store.entry(video.relpath),
                        sam3_queue.public(ctx.object_id, video.relpath),
                        ctx.output_root,
                    )
                except (OSError, RuntimeError, ValueError) as exc:
                    # O lote já está salvo. Devolver sucesso com diagnóstico
                    # evita induzir o operador a repetir e gerar conflito 409.
                    completion_error = str(exc)
    except RevisionConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except (MaskValidationError, ValueError) as exc:
        raise HTTPException(422, str(exc)) from exc

    sync_job_id = None
    if sync_frames:
        try:
            sync_job_id = durable_jobs.enqueue_mask_review_sync(
                object_id=ctx.object_id,
                video_id=video_id,
                relpath=video.relpath,
                segment=segment,
                frames=sync_frames,
                review_sha256=sync_review_sha256,
                user=user.user_id,
            )
        except Exception:  # a revisão local já foi confirmada
            log.warning(
                "não foi possível agendar o espelho da revisão %s/%s",
                video_id,
                segment,
                exc_info=True,
            )

    projection = mutation.complete() if mutation is not None else {
        "projection_pending": False,
        "projection_event_seq": None,
    }
    if projection["projection_pending"]:
        try:
            durable_jobs.enqueue_projection_reconciles(
                ctx.object_id,
                [
                    {
                        "video_id": video_id,
                        "source_identity": (
                            certified_source.identity
                            if certified_source is not None
                            else {
                                "segment": segment,
                                "review_sha256": sync_review_sha256,
                            }
                        ),
                    }
                ],
            )
        except Exception:
            log.warning(
                "não foi possível reagendar a projeção de %s",
                video_id,
                exc_info=True,
            )
    return {
        **response,
        **projection,
        "pipeline": certified_source.snapshot_dict if certified_source else None,
        "completion_error": completion_error,
        "sync_job_id": sync_job_id,
        "sync_pending": bool(sync_frames and sync_job_id is None),
    }


# --------------------------------------------------------------------------
# escrita
# --------------------------------------------------------------------------


class BoxIn(BaseModel):
    obj_id: int | None = None
    label: str | None = None
    normalized: list[float] = Field(min_length=4, max_length=4)


class FrameIn(BaseModel):
    export_version: str = Field(min_length=8, max_length=128)
    status: str = "edited"
    boxes: list[BoxIn] = Field(default_factory=list)


def _require_lock(ctx: ObjectContext, video_id: str, client_id: str) -> None:
    """A revisão escreve no dataset, então respeita a mesma trava da triagem."""
    lock = locks.get(ctx.object_id, video_id)
    if lock is not None and lock.client_id != client_id:
        raise HTTPException(
            409,
            {"detail": f"{lock.user} está com este vídeo aberto", "lock": lock.public()},
        )


@router.put("/videos/{video_id}/segments/{segment}/review/{frame}")
def put_frame(
    video_id: str,
    segment: str,
    frame: int,
    payload: FrameIn,
    ctx: ObjectContext = Depends(get_object),
    user: User = Depends(current_user),
    client_id: str = Depends(current_client),
) -> dict:
    label = ctx.label
    boxes = [
        {
            "obj_id": box.obj_id or position,
            # O rótulo vem do objeto, não do cliente: o mesmo motivo pelo qual a
            # triagem resolve o label no servidor.
            "label": box.label or label,
            "normalized": box.normalized,
        }
        for position, box in enumerate(payload.boxes, start=1)
    ]
    mutation = None
    def before_commit(review):
        nonlocal mutation
        mutation = reserve_mutation(ctx, video_id, "legacy_review_saved", {
            "export_version": payload.export_version, "segment": segment,
            "frames": _review_identity(review.get("frames") or {}),
        })
    try:
        with _locked_export(
            ctx, video_id, client_id, payload.export_version
        ) as (root, segments, _):
            paths = _segment_paths(root, segments, segment)
            entry = review_module.set_frame(
                paths, frame, status=payload.status, boxes=boxes, user=user.user_id,
                before_commit=before_commit,
            )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return {"frame": frame, **entry, **mutation.complete()}


class ConfirmIn(BaseModel):
    export_version: str = Field(min_length=8, max_length=128)
    start: int = 0
    end: int
    overwrite: bool = False


@router.post("/videos/{video_id}/segments/{segment}/review/confirm")
def confirm(
    video_id: str,
    segment: str,
    payload: ConfirmIn,
    ctx: ObjectContext = Depends(get_object),
    user: User = Depends(current_user),
    client_id: str = Depends(current_client),
) -> dict:
    """Confirma um intervalo de frames de uma vez."""
    mutation = None
    def before_commit(review):
        nonlocal mutation
        mutation = reserve_mutation(ctx, video_id, "legacy_review_saved", {
            "export_version": payload.export_version, "segment": segment,
            "frames": _review_identity(review.get("frames") or {}),
        })
    with _locked_export(
        ctx, video_id, client_id, payload.export_version
    ) as (root, segments, _):
        paths = _segment_paths(root, segments, segment)
        changed = review_module.confirm_range(
            paths,
            payload.start,
            payload.end,
            user=user.user_id,
            overwrite=payload.overwrite,
            before_commit=before_commit,
        )
        names = review_module.class_names(workspace.root) if workspace.root else []
        state = review_module.segment_state(paths, _frame_count(paths), names)
        response = {
            "confirmed": changed,
            "reviewed": state["reviewed"],
            "complete": state["complete"],
        }
    return {**response, **mutation.complete()}


@router.delete("/videos/{video_id}/segments/{segment}/review/{frame}")
def reset_frame(
    video_id: str,
    segment: str,
    frame: int,
    export_version: str,
    ctx: ObjectContext = Depends(get_object),
    _: User = Depends(current_user),
    client_id: str = Depends(current_client),
) -> dict:
    """Descarta a revisão de um frame: volta a valer o resultado do SAM3."""
    mutation = None
    def before_commit(review):
        nonlocal mutation
        mutation = reserve_mutation(ctx, video_id, "legacy_review_saved", {
            "export_version": export_version, "segment": segment,
            "frames": _review_identity(review.get("frames") or {}),
        })
    with _locked_export(
        ctx, video_id, client_id, export_version
    ) as (root, segments, _):
        paths = _segment_paths(root, segments, segment)
        review_module.clear_frame(paths, frame, before_commit=before_commit)
    return {"frame": frame, "status": None, **mutation.complete()}
