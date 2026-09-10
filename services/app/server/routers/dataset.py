"""Exportação do dataset final (YOLO / COCO)."""

from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from .. import dataset as dataset_module, durable_jobs
from ..dataset import FORMATS, TASKS, Filters
from ..deps import current_client, current_user, get_object
from ..jobs import jobs
from ..users import User
from ..videos import iso
from ..workspace import ObjectContext, workspace

router = APIRouter(prefix="/api/objects/{object_id}/dataset", tags=["dataset"])
global_router = APIRouter(prefix="/api/datasets", tags=["global-dataset"])


class FiltersIn(BaseModel):
    flags: dict[str, list[str]] = Field(default_factory=dict)
    video_ids: list[str] = Field(default_factory=list)
    reviewed_only: bool = False
    include_empty: bool = False

    def to_filters(self) -> Filters:
        return Filters(
            flags={k: v for k, v in self.flags.items() if v},
            video_ids=self.video_ids,
            reviewed_only=self.reviewed_only,
            include_empty=self.include_empty,
        )


class PreviewIn(FiltersIn):
    task: str = "detection"


@router.post("/preview")
async def preview(
    payload: PreviewIn | None = None, ctx: ObjectContext = Depends(get_object)
) -> dict:
    """Quanto o filtro seleciona. POST porque o filtro é um objeto, não query."""
    request = payload or PreviewIn()
    if request.task not in TASKS:
        raise HTTPException(422, f"tarefa deve ser uma de {TASKS}")
    filters = request.to_filters()
    try:
        return await asyncio.to_thread(
            dataset_module.preview, ctx, filters, workspace.root, task=request.task
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


class ExportIn(BaseModel):
    format: str = "yolo"
    task: str = "detection"
    name: str | None = None
    val_fraction: float = 0.2
    test_fraction: float = 0.0
    filters: FiltersIn = Field(default_factory=FiltersIn)


@router.post("/export")
async def export(
    payload: ExportIn,
    ctx: ObjectContext = Depends(get_object),
    user: User = Depends(current_user),
    client_id: str = Depends(current_client),
) -> dict:
    if payload.format not in FORMATS:
        raise HTTPException(422, f"formato deve ser um de {FORMATS}")
    if payload.task not in TASKS:
        raise HTTPException(422, f"tarefa deve ser uma de {TASKS}")
    if not 0 <= payload.val_fraction < 1 or not 0 <= payload.test_fraction < 1:
        raise HTTPException(422, "as frações precisam estar em [0, 1)")
    if payload.val_fraction + payload.test_fraction >= 1:
        raise HTTPException(422, "val + test precisa sobrar algo para o treino")

    stamp = iso().replace(":", "-").split(".")[0]
    name = payload.name or f"{ctx.object_id}-{payload.task}-{payload.format}-{stamp}"
    if (
        Path(name).name != name
        or name in {"", ".", ".."}
        or "/" in name
        or "\\" in name
    ):
        raise HTTPException(422, "nome de dataset invalido")
    # Dentro do output_root: os frames vivem lá, e o hardlink só é grátis dentro
    # do mesmo filesystem.
    out_dir = ctx.output_root / "_datasets" / name
    filters = payload.filters.to_filters()
    try:
        snapshot = await asyncio.to_thread(
            dataset_module.build_snapshot,
            ctx,
            filters,
            workspace.root,
            task=payload.task,
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    snapshot = dataset_module.bind_export_spec(
        snapshot,
        fmt=payload.format,
        val_fraction=payload.val_fraction,
        test_fraction=payload.test_fraction,
    )
    if not snapshot["segments"]:
        raise HTTPException(422, "nenhum segmento casa com o filtro")
    if not snapshot.get("export_allowed", True):
        reasons = snapshot.get("blocking_reasons") or []
        raise HTTPException(
            422,
            f"exportacao bloqueada: {reasons[0] if reasons else 'snapshot incompleto'}",
        )
    try:
        await asyncio.to_thread(
            dataset_module.reserve_dataset_target,
            ctx.output_root / "_datasets",
            name,
            snapshot["snapshot_id"],
        )
    except dataset_module.DatasetTargetConflict as exc:
        raise HTTPException(409, str(exc)) from exc

    if durable_jobs.enabled():
        job_id = await asyncio.to_thread(
            durable_jobs.create,
            kind="dataset_export",
            object_id=ctx.object_id,
            payload={
                "name": name,
                "format": payload.format,
                "task": payload.task,
                "val_fraction": payload.val_fraction,
                "test_fraction": payload.test_fraction,
                "filters": {
                    "flags": filters.flags,
                    "video_ids": filters.video_ids,
                    "reviewed_only": filters.reviewed_only,
                    "include_empty": filters.include_empty,
                },
                "snapshot": snapshot,
                "client_id": client_id,
                "user": user.user_id,
                "message": f"exportando {payload.task} {payload.format}",
            },
            priority=70,
            idempotency_key=f"dataset-export:{ctx.object_id}:{name}",
        )
        return {"job_id": job_id, "name": name, "out_dir": out_dir.as_posix()}

    job = jobs.create(
        "dataset_export", ctx.object_id, "", 0,
        f"exportando {payload.format}…",
        client_id=client_id, user=user.user_id,
    )

    async def worker() -> None:
        job.state = "running"
        jobs._publish(job)

        def on_progress(seen: int, total: int) -> None:
            job.current, job.total = seen, total

        try:
            result = await asyncio.to_thread(
                dataset_module.export_snapshot_atomic,
                ctx,
                snapshot,
                out_dir=out_dir,
                fmt=payload.format,
                task=payload.task,
                val_fraction=payload.val_fraction,
                test_fraction=payload.test_fraction,
                workspace_root=workspace.root,
                owner=job.job_id,
                on_progress=on_progress,
            )
        except ValueError as exc:
            job.state = "error"
            job.error = str(exc)
            job.finished_at = iso()
            jobs._publish(job)
            return
        except Exception as exc:  # noqa: BLE001
            job.state = "error"
            job.error = f"{type(exc).__name__}: {exc}"
            job.finished_at = iso()
            jobs._publish(job)
            return

        job.state = "done"
        job.result = result
        job.current = job.total or 1
        unit = "mascaras" if payload.task == "segmentation" else "caixas"
        job.message = f"{result['images']} imagens, {result['annotations']} {unit}"
        job.finished_at = iso()
        jobs._publish(job)

    asyncio.create_task(worker())
    return {"job_id": job.job_id, "name": name, "out_dir": out_dir.as_posix()}


@router.get("/list")
async def list_datasets(ctx: ObjectContext = Depends(get_object)) -> dict:
    """Datasets já exportados, lidos dos manifestos."""
    import json

    root = ctx.output_root / "_datasets"
    items = []
    if root.is_dir():
        for entry in sorted(root.iterdir(), reverse=True):
            manifest = entry / "dataset_manifest.json"
            if not entry.is_dir() or not manifest.exists():
                continue
            try:
                data = json.loads(manifest.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            items.append(
                {
                    "name": entry.name,
                    "path": entry.as_posix(),
                    "format": data.get("format"),
                    "task": data.get("task", "detection"),
                    "generated_at": data.get("generated_at"),
                    "counts": data.get("counts") or {},
                    "filters": data.get("filters") or {},
                }
            )
    return {"datasets": items}


@router.delete("/{name}")
async def delete_dataset(
    name: str, ctx: ObjectContext = Depends(get_object), _: User = Depends(current_user)
) -> dict:
    import shutil

    root = (ctx.output_root / "_datasets").resolve()
    target = (root / name).resolve()
    # O nome vem da URL: confina antes de apagar recursivamente.
    if not target.is_relative_to(root) or not target.is_dir():
        raise HTTPException(404, "dataset não encontrado")
    shutil.rmtree(target, ignore_errors=True)
    dataset_module.release_dataset_target(root, name)
    return {"deleted": name}


# --------------------------------------------------------------------------
# Dataset global multiclasse
# --------------------------------------------------------------------------


class VideoSelectionIn(BaseModel):
    object_id: str
    video_id: str


class GlobalFiltersIn(BaseModel):
    flags: dict[str, list[str]] = Field(default_factory=dict)
    videos: list[VideoSelectionIn] = Field(default_factory=list)
    include_empty: bool = False


class GlobalPreviewIn(BaseModel):
    object_ids: list[str] = Field(default_factory=list)
    filters: GlobalFiltersIn = Field(default_factory=GlobalFiltersIn)
    task: str = "detection"


def _global_contexts(object_ids: list[str]) -> list[ObjectContext]:
    if not object_ids:
        raise ValueError("selecione ao menos um objeto")
    active = {cfg.object_id for cfg in workspace.list()}
    missing = sorted(set(object_ids) - active)
    if missing:
        raise ValueError(f"objetos inexistentes ou arquivados: {', '.join(missing)}")
    contexts = [workspace.context(object_id) for object_id in sorted(set(object_ids))]
    for ctx in contexts:
        ctx.ensure_loaded()
    return contexts


@global_router.post("/preview")
async def global_preview(payload: GlobalPreviewIn) -> dict:
    from ..multiclass_dataset import preview_multiclass

    if payload.task not in TASKS:
        raise HTTPException(422, f"tarefa deve ser uma de {TASKS}")
    try:
        contexts = await asyncio.to_thread(_global_contexts, payload.object_ids)
        return await asyncio.to_thread(
            preview_multiclass,
            contexts,
            [item.model_dump() for item in payload.filters.videos],
            flags={key: values for key, values in payload.filters.flags.items() if values},
            include_empty=payload.filters.include_empty,
            task=payload.task,
            workspace_root=workspace.root,
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


class GlobalExportIn(GlobalPreviewIn):
    format: str = "yolo"
    name: str | None = None
    val_fraction: float = 0.2
    test_fraction: float = 0.0


@global_router.post("/export", status_code=202)
async def global_export(
    payload: GlobalExportIn,
    user: User = Depends(current_user),
    client_id: str = Depends(current_client),
) -> dict:
    if payload.format not in FORMATS:
        raise HTTPException(422, f"formato deve ser um de {FORMATS}")
    if payload.task not in TASKS:
        raise HTTPException(422, f"tarefa deve ser uma de {TASKS}")
    if not 0 <= payload.val_fraction < 1 or not 0 <= payload.test_fraction < 1:
        raise HTTPException(422, "as frações precisam estar em [0, 1)")
    if payload.val_fraction + payload.test_fraction >= 1:
        raise HTTPException(422, "val + test precisa sobrar algo para o treino")
    try:
        await asyncio.to_thread(_global_contexts, payload.object_ids)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    if workspace.root is None:
        raise HTTPException(409, "workspace não configurado")
    stamp = iso().replace(":", "-").split(".")[0]
    name = payload.name or f"global-{payload.task}-{payload.format}-{stamp}"
    if Path(name).name != name or name in {"", ".", ".."}:
        raise HTTPException(422, "nome de dataset inválido")
    out_dir = workspace.root / "_datasets" / name
    if out_dir.exists():
        raise HTTPException(409, f"já existe um dataset chamado '{name}'")
    if not durable_jobs.enabled():
        raise HTTPException(503, "PostgreSQL é obrigatório para exportação global")
    job_id = await asyncio.to_thread(
        durable_jobs.create,
        kind="dataset_export_global",
        object_id="global",
        priority=70,
        payload={
            "name": name,
            "object_ids": sorted(set(payload.object_ids)),
            "format": payload.format,
            "task": payload.task,
            "val_fraction": payload.val_fraction,
            "test_fraction": payload.test_fraction,
            "filters": {
                "flags": payload.filters.flags,
                "videos": [item.model_dump() for item in payload.filters.videos],
                "include_empty": payload.filters.include_empty,
                "reviewed_only": True,
            },
            "client_id": client_id,
            "user": user.user_id,
            "message": f"exportando dataset global {payload.task} {payload.format}",
        },
    )
    return {"job_id": job_id, "name": name, "out_dir": out_dir.as_posix()}


def _dataset_payload(entry: Path, data: dict, scope: str, object_id: str | None = None) -> dict:
    return {
        "name": entry.name,
        "path": entry.as_posix(),
        "format": data.get("format"),
        "task": data.get("task", "detection"),
        "generated_at": data.get("generated_at"),
        "counts": data.get("counts") or {},
        "filters": data.get("filters") or {},
        "scope": scope,
        "object_id": object_id,
        "classes": data.get("classes") or [],
    }


@global_router.get("")
async def list_global_datasets() -> dict:
    import json

    items: list[dict] = []
    if workspace.root is not None:
        root = workspace.root / "_datasets"
        if root.is_dir():
            for entry in sorted(root.iterdir(), reverse=True):
                manifest = entry / "dataset_manifest.json"
                if not entry.is_dir() or not manifest.is_file():
                    continue
                try:
                    data = json.loads(manifest.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                items.append(_dataset_payload(entry, data, "global"))
    for cfg in workspace.list(include_archived=True):
        root = cfg.output_root / "_datasets"
        if not root.is_dir():
            continue
        for entry in sorted(root.iterdir(), reverse=True):
            manifest = entry / "dataset_manifest.json"
            if not entry.is_dir() or not manifest.is_file():
                continue
            try:
                data = json.loads(manifest.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            items.append(_dataset_payload(entry, data, "object", cfg.object_id))
    return {"datasets": items}
