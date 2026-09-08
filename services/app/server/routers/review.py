"""Revisão das bboxes pré-anotadas pelo SAM3."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from starlette.responses import FileResponse

from .. import review as review_module
from ..deps import current_client, current_user, get_object
from ..locks import locks
from ..mask_api import decode_mask_edits, serialize_frame_state
from ..review import SegmentPaths
from ..sam3_run_index import index_revision, index_revisions
from ..users import User
from ..workspace import ObjectContext, workspace
from pipeline_core.masks import MaskValidationError
from pipeline_core.review_store import FileMaskReviewStore, RevisionConflict

router = APIRouter(prefix="/api/objects/{object_id}", tags=["review"])


def _export_root(ctx: ObjectContext, video_id: str) -> tuple[Path, list[str], object]:
    video = ctx.index.get(video_id)
    if video is None:
        raise HTTPException(404, "vídeo não encontrado")
    entry = ctx.store.entry(video.relpath) or {}
    export = entry.get("export")
    if not export or not export.get("segments"):
        raise HTTPException(409, "este vídeo ainda não foi exportado")

    root = Path(export["root"])
    if not root.is_dir():
        # O caminho pode ter sido gravado antes de o workspace mudar de lugar.
        # Recalcular a partir do registro é mais confiável que confiar nele.
        candidate = ctx.output_root / root.name
        if candidate.is_dir():
            root = candidate
        else:
            raise HTTPException(
                409,
                f"a pasta do export não existe ({root}). Rode "
                "tools/rehome_workspace.py se o workspace mudou de lugar.",
            )
    return root, list(export["segments"]), video


def _segment_paths(root: Path, segments: list[str], segment: str) -> SegmentPaths:
    # `segment` vem da URL: só aceita o que o próprio export declarou, o que
    # dispensa qualquer sanitização de caminho.
    if segment not in segments:
        raise HTTPException(404, f"segmento '{segment}' não pertence a este vídeo")
    return SegmentPaths(root / segment)


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
    video_id: str, segment: str, frame: int, ctx: ObjectContext = Depends(get_object)
):
    """Frame do EXPORT, não do cache de proxy.

    O proxy é descartável e tem evicção LRU; os frames do export são o que o
    SAM3 realmente consumiu e o que vai para o dataset. Revisar contra outra
    imagem que não a anotada seria revisar a coisa errada.
    """
    root, segments, _ = _export_root(ctx, video_id)
    paths = _segment_paths(root, segments, segment)
    path = paths.frame_path(frame)
    if not path.exists():
        raise HTTPException(404, "frame não existe neste segmento")
    return FileResponse(
        path,
        media_type="image/jpeg",
        # O frame N de um segmento exportado nunca muda: reexportar apaga a
        # pasta inteira e gera outra.
        headers={"Cache-Control": "public, max-age=31536000, immutable"},
    )


# --------------------------------------------------------------------------
# estado da revisão
# --------------------------------------------------------------------------


@router.get("/videos/{video_id}/review")
async def video_review(video_id: str, ctx: ObjectContext = Depends(get_object)) -> dict:
    """Progresso do vídeo inteiro, sem carregar as caixas."""
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
    root, segments, video = _export_root(ctx, video_id)
    paths = _segment_paths(root, segments, segment)
    names = review_module.class_names(workspace.root) if workspace.root else []
    state = review_module.segment_state(paths, _frame_count(paths), names)

    prompt = review_module.prompt_contract(paths)

    return {
        "video_id": video_id,
        "name": video.name,
        "segment": segment,
        "segments": segments,
        "classes": names,
        "label": ctx.label,
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


class MaskBatchFrameIn(MaskFrameIn):
    frame: int = Field(ge=0)


class MaskBatchIn(BaseModel):
    frames: list[MaskBatchFrameIn] = Field(min_length=1)


def _mask_url(ctx: ObjectContext, video_id: str, segment: str, frame: int, obj_id: int) -> str:
    return (
        f"/api/objects/{ctx.object_id}/videos/{video_id}/segments/{segment}/"
        f"mask-review/{frame}/{obj_id}.png"
    )


@router.get("/videos/{video_id}/segments/{segment}/mask-review/{frame}")
async def mask_review_state(
    video_id: str, segment: str, frame: int, ctx: ObjectContext = Depends(get_object)
) -> dict:
    root, segments, _ = _export_root(ctx, video_id)
    paths = _segment_paths(root, segments, segment)
    if not (0 <= frame < _frame_count(paths)):
        raise HTTPException(404, "frame não existe neste segmento")
    if not (paths.out_dir / "masks").is_dir():
        raise HTTPException(409, "run legado sem mascaras; reprocesse com o SAM3")
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
    ctx: ObjectContext = Depends(get_object),
):
    root, segments, _ = _export_root(ctx, video_id)
    paths = _segment_paths(root, segments, segment)
    state = _mask_store(paths).get_frame(frame)
    instance = next((item for item in state.instances if item.obj_id == obj_id), None)
    if instance is None or not instance.path.exists():
        raise HTTPException(404, "máscara não encontrada")
    return FileResponse(
        instance.path,
        media_type="image/png",
        headers={"Cache-Control": "private, max-age=31536000, immutable"},
    )


@router.put("/videos/{video_id}/segments/{segment}/mask-review/{frame}")
async def save_mask_review(
    video_id: str,
    segment: str,
    frame: int,
    payload: MaskFrameIn,
    ctx: ObjectContext = Depends(get_object),
    user: User = Depends(current_user),
    client_id: str = Depends(current_client),
) -> dict:
    root, segments, video = _export_root(ctx, video_id)
    _require_lock(ctx, video_id, client_id)
    paths = _segment_paths(root, segments, segment)
    if not (0 <= frame < _frame_count(paths)):
        raise HTTPException(404, "frame não existe neste segmento")
    if not (paths.out_dir / "masks").is_dir():
        raise HTTPException(409, "run legado sem mascaras; reprocesse com o SAM3")
    try:
        edits = decode_mask_edits([item.model_dump() for item in payload.instances])
        state = _mask_store(paths).save_frame(
            frame,
            expected_revision=payload.expected_revision,
            status=payload.status,
            instances=edits,
            retain_obj_ids=payload.retain_obj_ids,
            user=user.user_id,
            before_commit=lambda entry: index_revision(
                object_id=ctx.object_id,
                relpath=video.relpath,
                segment_dir=paths.segment_dir,
                frame_idx=frame,
                entry=entry,
                user=user.user_id,
            ),
        )
    except RevisionConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except (MaskValidationError, ValueError) as exc:
        raise HTTPException(422, str(exc)) from exc
    return serialize_frame_state(
        state,
        mask_url=lambda obj_id: _mask_url(ctx, video_id, segment, frame, obj_id),
    )


@router.put("/videos/{video_id}/segments/{segment}/mask-review")
def save_mask_review_batch(
    video_id: str,
    segment: str,
    payload: MaskBatchIn,
    ctx: ObjectContext = Depends(get_object),
    user: User = Depends(current_user),
    client_id: str = Depends(current_client),
) -> dict:
    """Publica a revisão humana de um trecho em uma única operação."""
    root, segments, video = _export_root(ctx, video_id)
    _require_lock(ctx, video_id, client_id)
    paths = _segment_paths(root, segments, segment)
    frame_count = _frame_count(paths)
    frame_numbers = [item.frame for item in payload.frames]
    if len(set(frame_numbers)) != len(frame_numbers):
        raise HTTPException(422, "o lote contém frames duplicados")
    invalid = [frame for frame in frame_numbers if frame >= frame_count]
    if invalid:
        raise HTTPException(404, f"frame não existe neste segmento: {invalid[0]}")
    if not (paths.out_dir / "masks").is_dir():
        raise HTTPException(409, "run legado sem mascaras; reprocesse com o SAM3")

    updates = []
    try:
        for item in payload.frames:
            updates.append(
                {
                    "frame": item.frame,
                    "expected_revision": item.expected_revision,
                    "status": item.status,
                    "instances": decode_mask_edits(
                        [instance.model_dump() for instance in item.instances]
                    ),
                    "retain_obj_ids": item.retain_obj_ids,
                }
            )
        store = _mask_store(paths)
        pending_index: list[tuple[int, dict]] = []

        def index_batch(frame: int, entry: dict) -> None:
            pending_index.append((frame, entry))
            if len(pending_index) == len(updates):
                index_revisions(
                    object_id=ctx.object_id,
                    relpath=video.relpath,
                    segment_dir=paths.segment_dir,
                    revisions=pending_index,
                    user=user.user_id,
                )

        states = store.save_frames(
            updates,
            user=user.user_id,
            before_commit=index_batch,
        )
    except RevisionConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except (MaskValidationError, ValueError) as exc:
        raise HTTPException(422, str(exc)) from exc

    import json

    manifest = json.loads(store.manifest_path.read_text(encoding="utf-8"))
    reviewed = sum(
        1
        for entry in (manifest.get("frames") or {}).values()
        if entry.get("status") in {"ok", "edited"}
    )
    return {
        "frames": [
            {"frame": state.frame, "revision": state.revision, "status": state.status}
            for state in states
        ],
        "reviewed": reviewed,
        "frame_count": frame_count,
        "complete": reviewed >= frame_count and frame_count > 0,
    }


# --------------------------------------------------------------------------
# escrita
# --------------------------------------------------------------------------


class BoxIn(BaseModel):
    obj_id: int | None = None
    label: str | None = None
    normalized: list[float] = Field(min_length=4, max_length=4)


class FrameIn(BaseModel):
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
async def put_frame(
    video_id: str,
    segment: str,
    frame: int,
    payload: FrameIn,
    ctx: ObjectContext = Depends(get_object),
    user: User = Depends(current_user),
    client_id: str = Depends(current_client),
) -> dict:
    root, segments, _ = _export_root(ctx, video_id)
    _require_lock(ctx, video_id, client_id)
    paths = _segment_paths(root, segments, segment)

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
    try:
        entry = review_module.set_frame(
            paths, frame, status=payload.status, boxes=boxes, user=user.user_id
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return {"frame": frame, **entry}


class ConfirmIn(BaseModel):
    start: int = 0
    end: int
    overwrite: bool = False


@router.post("/videos/{video_id}/segments/{segment}/review/confirm")
async def confirm(
    video_id: str,
    segment: str,
    payload: ConfirmIn,
    ctx: ObjectContext = Depends(get_object),
    user: User = Depends(current_user),
    client_id: str = Depends(current_client),
) -> dict:
    """Confirma um intervalo de frames de uma vez."""
    root, segments, _ = _export_root(ctx, video_id)
    _require_lock(ctx, video_id, client_id)
    paths = _segment_paths(root, segments, segment)
    changed = review_module.confirm_range(
        paths, payload.start, payload.end, user=user.user_id, overwrite=payload.overwrite
    )
    names = review_module.class_names(workspace.root) if workspace.root else []
    state = review_module.segment_state(paths, _frame_count(paths), names)
    return {"confirmed": changed, "reviewed": state["reviewed"], "complete": state["complete"]}


@router.delete("/videos/{video_id}/segments/{segment}/review/{frame}")
async def reset_frame(
    video_id: str,
    segment: str,
    frame: int,
    ctx: ObjectContext = Depends(get_object),
    _: User = Depends(current_user),
    client_id: str = Depends(current_client),
) -> dict:
    """Descarta a revisão de um frame: volta a valer o resultado do SAM3."""
    root, segments, _ = _export_root(ctx, video_id)
    _require_lock(ctx, video_id, client_id)
    paths = _segment_paths(root, segments, segment)
    review_module.clear_frame(paths, frame)
    return {"frame": frame, "status": None}
