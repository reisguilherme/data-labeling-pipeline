"""Config global, escolha do workspace e registro de objetos."""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel
from starlette.responses import JSONResponse

from .. import durable_jobs, ffmpeg, gcs
from ..config import APP_VERSION, CACHE_LIMIT_GB, PROXY_MAX_FRAMES, VIDEO_EXTS, settings
from ..deps import current_user, current_user_optional, get_object, is_local
from ..flags import FLAG_GROUPS, FLAG_GROUPS_VERSION, SUGGEST_RULES
from ..jobs import jobs
from ..locks import locks
from ..object_lifecycle import (
    active_durable_jobs,
    partition_owned_object_paths,
    validate_purge_confirmation,
    validate_unique_label,
)
from ..sam3_session import sessions
from ..users import User, UserExists, users
from ..workspace import ObjectConfig, ObjectContext, slugify, validate_object_id, workspace

router = APIRouter(prefix="/api", tags=["objects"])


def _ffmpeg_info() -> dict:
    try:
        binaries = ffmpeg.resolve()
    except ffmpeg.FFmpegError as exc:
        return {"ok": False, "version": None, "path": None, "error": str(exc)}
    return {
        "ok": True,
        "version": binaries.version,
        "path": binaries.ffmpeg,
        "source": binaries.source,
        "error": None,
    }


def _object_payload(cfg: ObjectConfig) -> dict:
    data = cfg.to_json()
    data["videos_root"] = Path(cfg.videos_root).as_posix()
    data["output_root"] = Path(cfg.output_root).as_posix()
    data["gcs_ready"] = bool(cfg.gcs_uri)
    return data


@router.get("/config")
async def get_config(request: Request) -> dict:
    user = current_user_optional(request)
    return {
        "app_version": APP_VERSION,
        "workspace_root": (
            Path(settings.workspace_root).as_posix() if settings.workspace_root else None
        ),
        "workspace_ready": workspace.ready,
        "objects": [_object_payload(cfg) for cfg in workspace.list()],
        "last_object_id": settings.last_object_id,
        "user": user.to_json() if user else None,
        "users": [u.to_json() for u in users.list()],
        "flag_groups": FLAG_GROUPS,
        "flag_groups_version": FLAG_GROUPS_VERSION,
        "suggest_rules": list(SUGGEST_RULES),
        "proxy_max_frames": PROXY_MAX_FRAMES,
        "cache_limit_gb": CACHE_LIMIT_GB,
        "video_exts": list(VIDEO_EXTS),
        "ffmpeg": _ffmpeg_info(),
        "gcloud": gcs.info(),
        # Só o setup local pode navegar o disco do servidor — ver /fs/list.
        "can_browse_fs": is_local(request),
    }


# -- workspace -------------------------------------------------------------


class WorkspacePayload(BaseModel):
    workspace_root: str


@router.post("/config/workspace")
async def set_workspace(payload: WorkspacePayload, request: Request) -> dict:
    if not is_local(request):
        raise HTTPException(403, "a raiz do workspace só pode ser definida na máquina do servidor")
    root = Path(payload.workspace_root).expanduser()
    try:
        root.mkdir(parents=True, exist_ok=True)
        probe = root / ".write_test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        raise HTTPException(400, f"pasta não é gravável: {exc}") from exc

    workspace.set_root(root.resolve())
    users.bind(root.resolve())
    from ..locks import locks

    locks.bind(root.resolve())
    return await get_config(request)


@router.get("/fs/list")
async def list_dirs(request: Request, path: str | None = Query(default=None)) -> dict:
    """Navegador de pastas server-side, restrito à máquina do servidor.

    Este endpoint enumera QUALQUER diretório do host. Isso era aceitável enquanto
    a ferramenta só escutava em 127.0.0.1; com `--host 0.0.0.0` viraria leitura da
    árvore de diretórios para toda a rede. Escolher a pasta é legitimamente um
    passo local, então a restrição é por localidade — mais honesto que inventar
    uma allowlist de caminhos que alguém vai ampliar depois.
    """
    if not is_local(request):
        raise HTTPException(403, "navegação de pastas só é permitida na máquina do servidor")

    target = Path.home() if path in (None, "", "~") else Path(path).expanduser()
    if not target.exists():
        raise HTTPException(404, f"não existe: {target}")
    if not target.is_dir():
        target = target.parent

    dirs = []
    video_count = 0
    try:
        for entry in sorted(target.iterdir(), key=lambda p: p.name.lower()):
            if entry.name.startswith("."):
                continue
            if entry.is_dir():
                dirs.append({"name": entry.name, "path": str(entry)})
            elif entry.suffix.lower() in VIDEO_EXTS:
                video_count += 1
    except PermissionError as exc:
        raise HTTPException(403, f"sem permissão: {target}") from exc

    parent = str(target.parent) if target.parent != target else None
    return {"path": str(target), "parent": parent, "dirs": dirs, "video_count": video_count}


# -- objetos ---------------------------------------------------------------


@router.get("/objects")
async def list_objects(include_archived: bool = False) -> dict:
    return {
        "objects": [
            _object_payload(cfg)
            for cfg in workspace.list(include_archived=include_archived)
        ]
    }


def _validate_label(object_id: str, label: str) -> None:
    validate_unique_label(
        [(cfg.object_id, cfg.label, cfg.archived) for cfg in workspace.list(include_archived=True)],
        object_id=object_id,
        label=label,
    )


class ObjectIn(BaseModel):
    display_name: str
    object_id: str | None = None
    label: str | None = None
    gcs_uri: str | None = None
    videos_root: str | None = None
    output_root: str | None = None
    suggest_rules: str | None = None


@router.post("/objects", status_code=201)
async def create_object(payload: ObjectIn, user: User = Depends(current_user)) -> dict:
    if not workspace.ready:
        raise HTTPException(409, "escolha a pasta do workspace primeiro")

    object_id = payload.object_id or slugify(payload.display_name)
    try:
        validate_object_id(object_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

    if payload.videos_root and payload.output_root:
        videos_root = Path(payload.videos_root).expanduser()
        output_root = Path(payload.output_root).expanduser()
    else:
        videos_root, output_root = workspace.default_roots(object_id)

    cfg = ObjectConfig(
        object_id=object_id,
        display_name=payload.display_name.strip() or object_id,
        label=(payload.label or object_id).strip(),
        videos_root=videos_root,
        output_root=output_root,
        gcs_uri=payload.gcs_uri or None,
        # None de propósito: as heurísticas por nome de arquivo são do acervo de
        # boom. Herdá-las num objeto novo pré-marcaria flags sem evidência.
        suggest_rules=payload.suggest_rules,
        created_by=user.user_id,
    )
    try:
        _validate_label(object_id, cfg.label)
        workspace.create(cfg)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return _object_payload(cfg)


class ObjectPatch(BaseModel):
    display_name: str | None = None
    label: str | None = None
    gcs_uri: str | None = None
    suggest_rules: str | None = None
    archived: bool | None = None


@router.patch("/objects/{object_id}")
async def update_object(
    object_id: str, payload: ObjectPatch, _: User = Depends(current_user)
) -> dict:
    projection_outcome = {}
    try:
        validate_object_id(object_id)
        changes = payload.model_dump(exclude_unset=True)
        if "display_name" in changes:
            changes["display_name"] = str(changes["display_name"]).strip()
            if not changes["display_name"]:
                raise ValueError("nome de exibição é obrigatório")
        if "label" in changes:
            changes["label"] = str(changes["label"]).strip()
            _validate_label(object_id, changes["label"])
        cfg = workspace.update(object_id, projection_outcome=projection_outcome, **changes)
    except KeyError as exc:
        raise HTTPException(404, f"objeto '{object_id}' não existe") from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {**_object_payload(cfg), **projection_outcome}


@router.post("/objects/{object_id}/archive")
async def archive_object(object_id: str, _: User = Depends(current_user)) -> dict:
    projection_outcome = {}
    try:
        validate_object_id(object_id)
        cfg = workspace.get(object_id)
        if cfg.archived:
            return _object_payload(cfg)
        cfg = workspace.update(object_id, archived=True, projection_outcome=projection_outcome)
    except KeyError as exc:
        raise HTTPException(404, f"objeto '{object_id}' não existe") from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {**_object_payload(cfg), **projection_outcome}


@router.post("/objects/{object_id}/restore")
async def restore_object(object_id: str, _: User = Depends(current_user)) -> dict:
    projection_outcome = {}
    try:
        validate_object_id(object_id)
        cfg = workspace.get(object_id)
        _validate_label(object_id, cfg.label)
        cfg = workspace.update(object_id, archived=False, projection_outcome=projection_outcome)
    except KeyError as exc:
        raise HTTPException(404, f"objeto '{object_id}' não existe") from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {**_object_payload(cfg), **projection_outcome}


class PurgeIn(BaseModel):
    confirmation: str


@router.post("/objects/{object_id}/purge", status_code=202)
async def purge_object(
    object_id: str,
    payload: PurgeIn,
    user: User = Depends(current_user),
) -> dict:
    try:
        validate_object_id(object_id)
        validate_purge_confirmation(object_id, payload.confirmation)
        cfg = workspace.get(object_id)
    except KeyError as exc:
        raise HTTPException(404, f"objeto '{object_id}' não existe") from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if not cfg.archived:
        raise HTTPException(409, "arquive o objeto antes da exclusão permanente")
    if locks.map_for(object_id):
        raise HTTPException(409, "o objeto ainda possui vídeos travados")
    if sessions.active_for_object(object_id):
        raise HTTPException(409, "o objeto ainda possui sessões SAM3 ativas")
    if jobs.active_for_object(object_id):
        raise HTTPException(409, "o objeto ainda possui jobs locais ativos")
    if active_durable_jobs(os.environ.get("DATABASE_URL"), object_id):
        raise HTTPException(409, "o objeto ainda possui jobs duráveis ativos")
    if workspace.root is None:
        raise HTTPException(409, "workspace não configurado")
    registered_roots = [
        (registered.object_id, path)
        for registered in workspace.list(include_archived=True)
        for path in (registered.videos_root, registered.output_root)
    ]
    managed, skipped = partition_owned_object_paths(
        workspace.root,
        object_id,
        [cfg.videos_root, cfg.output_root],
        registered_roots=registered_roots,
    )
    if not durable_jobs.enabled():
        raise HTTPException(503, "PostgreSQL é obrigatório para exclusão auditada")
    job_id = durable_jobs.create(
        kind="object_purge",
        object_id=object_id,
        priority=80,
        idempotency_key=f"object-purge:{object_id}",
        payload={
            "actor": user.user_id,
            "managed_paths": [str(path) for path in managed],
            "skipped_paths": [str(path) for path in skipped],
            "message": "inventariando objeto antes da exclusão",
        },
    )
    return {
        "job_id": job_id,
        "managed_paths": [path.as_posix() for path in managed],
        "skipped_paths": [path.as_posix() for path in skipped],
    }


@router.get("/objects/{object_id}/summary")
async def object_summary(ctx: ObjectContext = Depends(get_object)) -> dict:
    counts = ctx.store.counts(len(ctx.index.all()))
    return {
        "object_id": ctx.object_id,
        "label": ctx.label,
        "counts": counts,
        "videos_root": ctx.videos_root.as_posix(),
        "output_root": ctx.output_root.as_posix(),
        "excluded": ctx.exclusions.count(),
        "downloaded": ctx.downloads.count(),
    }


# -- usuários e sessão ------------------------------------------------------

user_router = APIRouter(prefix="/api", tags=["users"])


@user_router.get("/users")
async def list_users() -> dict:
    return {"users": [u.to_json() for u in users.list()]}


class UserIn(BaseModel):
    display_name: str


@user_router.post("/users", status_code=201)
async def create_user(payload: UserIn) -> dict:
    try:
        user = users.create(payload.display_name)
    except UserExists as exc:
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except OSError as exc:
        # Um 500 aqui vira "Internal Server Error" na tela, que não diz onde
        # procurar. O problema quase sempre é o workspace não ser gravável pelo
        # usuário do container — então a mensagem diz o caminho e a causa.
        raise HTTPException(
            500,
            f"não consegui gravar o cadastro em {users.path}: "
            f"{exc.strerror or exc}. Verifique se a pasta do workspace existe e "
            "é gravável pelo usuário que roda a ferramenta (UID/GID no .env).",
        ) from exc
    return user.to_json()


class LoginIn(BaseModel):
    user_id: str


@user_router.post("/session/login")
async def login(payload: LoginIn) -> JSONResponse:
    user = users.get(payload.user_id)
    if user is None:
        raise HTTPException(404, "usuário não encontrado")
    users.touch(user.user_id)
    users.save()
    response = JSONResponse(user.to_json())
    # Cookie e não header: é o único transporte que <img>, <video> e EventSource
    # mandam sozinhos — e o app carrega frames por <img> o tempo todo.
    response.set_cookie(
        "mst_user", user.user_id, max_age=365 * 24 * 3600, samesite="lax", httponly=False
    )
    return response


@user_router.post("/session/logout")
async def logout() -> JSONResponse:
    response = JSONResponse({"ok": True})
    response.delete_cookie("mst_user")
    return response
