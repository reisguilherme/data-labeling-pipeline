"""Entrada de vídeos: download do GCS e descarte por regra.

As duas metades andam juntas de propósito — as regras de exclusão são aplicadas
ANTES de baixar, então o lixo conhecido ("SOMBRA DE BOOM") nunca chega a ocupar
banda nem disco.
"""

from __future__ import annotations

import asyncio
import os
import shutil
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .. import durable_jobs, gcs
from ..deps import current_client, current_user, get_object
from ..jobs import jobs
from ..manifest import InvalidPattern, compile_patterns, match_pattern
from ..users import User
from ..videos import iso
from ..workspace import ObjectContext, workspace

router = APIRouter(prefix="/api/objects/{object_id}", tags=["ingest"])


# --------------------------------------------------------------------------
# regras de exclusão
# --------------------------------------------------------------------------


class PatternsIn(BaseModel):
    exclude_patterns: list[dict]


@router.get("/exclusions")
async def get_exclusions(ctx: ObjectContext = Depends(get_object)) -> dict:
    return {
        "exclude_patterns": ctx.config.exclude_patterns,
        "excluded": ctx.exclusions.all(),
        "trash_dir": ctx.trash_dir.as_posix(),
    }


@router.put("/exclusions/patterns")
async def set_patterns(
    payload: PatternsIn,
    ctx: ObjectContext = Depends(get_object),
    _: User = Depends(current_user),
) -> dict:
    try:
        compile_patterns(payload.exclude_patterns)
    except InvalidPattern as exc:
        # Falha aqui, não dentro de um job às três da manhã.
        raise HTTPException(422, str(exc)) from exc
    workspace.update(ctx.object_id, exclude_patterns=payload.exclude_patterns)
    return {"exclude_patterns": payload.exclude_patterns}


@router.post("/exclusions/apply")
async def apply_exclusions(
    apply: bool = False,
    force: bool = False,
    ctx: ObjectContext = Depends(get_object),
    user: User = Depends(current_user),
) -> dict:
    """Aplica as regras à biblioteca já baixada. Dry-run por padrão.

    A guarda que sustenta a feature inteira: vídeo que não está `pending` só é
    movido com `force`. Mover um vídeo já exportado orfanaria tanto as pastas
    seg_* quanto a entrada de anotação.
    """
    try:
        compiled = compile_patterns(ctx.config.exclude_patterns)
    except InvalidPattern as exc:
        raise HTTPException(422, str(exc)) from exc
    if not compiled:
        return {"applied": False, "matches": [], "moved": [], "blocked": []}

    matches, blocked = [], []
    hit_by_name = {}
    for video in ctx.index.all():
        name = Path(video.relpath).name
        pattern = match_pattern(name, compiled)
        if pattern is None:
            continue
        hit_by_name[name] = pattern
        status = ctx.store.status_of(video.relpath)
        item = {
            "video_id": video.video_id,
            "relpath": video.relpath,
            "name": name,
            "status": status,
            "size_bytes": video.size_bytes,
            "pattern": pattern.to_json(),
        }
        if status != "pending" and not force:
            blocked.append(item)
        else:
            matches.append(item)

    if not apply:
        return {"applied": False, "matches": matches, "blocked": blocked, "moved": []}

    moved = []
    ctx.trash_dir.mkdir(parents=True, exist_ok=True)
    for item in matches:
        source = ctx.videos_root / item["relpath"]
        if not source.exists():
            continue
        destination = ctx.trash_dir / item["name"]
        if destination.exists():
            destination = ctx.trash_dir / f"{destination.stem}__{item['video_id'][:6]}{destination.suffix}"
        try:
            # Mesmo volume: rename atômico, sem copiar bytes.
            os.replace(source, destination)
        except OSError as exc:
            item["error"] = str(exc)
            blocked.append(item)
            continue
        ctx.exclusions.record(
            item["name"],
            relpath=item["relpath"],
            reason="pattern",
            pattern=hit_by_name.get(item["name"]),
            user=user.user_id,
            moved_to=destination.as_posix(),
            size_bytes=item["size_bytes"],
        )
        moved.append(item)

    ctx.exclusions.save()
    await asyncio.to_thread(ctx.rescan)
    return {"applied": True, "matches": matches, "blocked": blocked, "moved": moved}


class DiscardIn(BaseModel):
    video_id: str
    note: str = ""


@router.post("/exclusions/discard")
async def discard_video(
    payload: DiscardIn,
    force: bool = False,
    ctx: ObjectContext = Depends(get_object),
    user: User = Depends(current_user),
) -> dict:
    """Descarte manual de um vídeo, direto do card da biblioteca."""
    video = ctx.index.get(payload.video_id)
    if video is None:
        raise HTTPException(404, "vídeo não encontrado")

    status = ctx.store.status_of(video.relpath)
    if status != "pending" and not force:
        raise HTTPException(
            409,
            f"vídeo está como '{status}' — descartar orfanaria a anotação e os "
            "frames exportados. Use force=1 se for mesmo isso.",
        )

    name = Path(video.relpath).name
    ctx.trash_dir.mkdir(parents=True, exist_ok=True)
    destination = ctx.trash_dir / name
    if destination.exists():
        destination = ctx.trash_dir / f"{destination.stem}__{video.video_id[:6]}{destination.suffix}"
    try:
        os.replace(ctx.videos_root / video.relpath, destination)
    except OSError as exc:
        raise HTTPException(500, f"não foi possível mover: {exc}") from exc

    ctx.exclusions.record(
        name,
        relpath=video.relpath,
        reason="manual",
        pattern=None,
        user=user.user_id,
        moved_to=destination.as_posix(),
        size_bytes=video.size_bytes,
        note=payload.note,
    )
    ctx.exclusions.save()
    await asyncio.to_thread(ctx.rescan)
    return {"discarded": name, "moved_to": destination.as_posix()}


class RestoreIn(BaseModel):
    name: str


@router.post("/exclusions/restore")
async def restore_video(
    payload: RestoreIn,
    ctx: ObjectContext = Depends(get_object),
    _: User = Depends(current_user),
) -> dict:
    record = ctx.exclusions.all().get(payload.name)
    if record is None:
        raise HTTPException(404, "não há registro de descarte com esse nome")

    source = Path(record.get("moved_to") or "")
    # Confinamento: o caminho vem de um arquivo JSON, então é revalidado.
    if not source.is_absolute() or not source.resolve().is_relative_to(
        ctx.trash_dir.resolve()
    ):
        raise HTTPException(400, "registro aponta para fora da lixeira")
    if not source.exists():
        raise HTTPException(404, f"arquivo não está mais na lixeira: {source}")

    destination = ctx.videos_root / (record.get("relpath") or payload.name)
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.replace(source, destination)
    except OSError as exc:
        raise HTTPException(500, f"não foi possível restaurar: {exc}") from exc

    ctx.exclusions.restore(payload.name)
    ctx.exclusions.save()
    await asyncio.to_thread(ctx.rescan)
    return {"restored": payload.name, "relpath": record.get("relpath")}


# --------------------------------------------------------------------------
# gcloud: listar -> prever -> baixar
# --------------------------------------------------------------------------

# Listagem em memória por objeto: o download só aceita nomes que apareceram aqui.
_listing_cache: dict[str, dict] = {}


class ListIn(BaseModel):
    gcs_uri: str | None = None


@router.post("/gcs/list")
async def gcs_list(
    payload: ListIn | None = None,
    ctx: ObjectContext = Depends(get_object),
    _: User = Depends(current_user),
) -> dict:
    """Lista o prefixo e classifica cada blob. POST, não GET: dispara um
    subprocesso, e GET convida browser e proxy a fazerem prefetch disso."""
    uri = (payload.gcs_uri if payload else None) or ctx.config.gcs_uri
    if not uri:
        raise HTTPException(
            409, "este objeto não tem bucket configurado (defina gcs_uri)"
        )

    try:
        remote = await asyncio.to_thread(gcs.list_uri, uri)
    except gcs.GcsError as exc:
        raise HTTPException(502, str(exc)) from exc

    try:
        compiled = compile_patterns(ctx.config.exclude_patterns)
    except InvalidPattern as exc:
        raise HTTPException(422, str(exc)) from exc

    # A ORDEM DOS BALDES IMPORTA e é parte do contrato:
    #   downloaded     -> no manifesto E no disco
    #   missing        -> no manifesto, sumiu do disco (alguém apagou; reofertar)
    #   excluded       -> já descartado antes, não insistir
    #   matches_pattern-> casa uma regra: pular sem baixar
    #   skipped        -> extensão que não é de vídeo
    #   new            -> baixar
    buckets: dict[str, list[dict]] = {
        "downloaded": [], "missing": [], "excluded": [],
        "matches_pattern": [], "skipped": [], "new": [],
    }

    for item in remote:
        data = item.to_json()
        if not gcs.safe_name(item.name):
            data["reason"] = "nome inseguro para virar arquivo local"
            buckets["skipped"].append(data)
            continue
        record = ctx.downloads.get(item.name)
        if record is not None:
            on_disk = (ctx.videos_root / (record.get("relpath") or item.name)).exists()
            (buckets["downloaded"] if on_disk else buckets["missing"]).append(data)
            continue
        if ctx.exclusions.has(item.name):
            buckets["excluded"].append(data)
            continue
        pattern = match_pattern(item.name, compiled)
        if pattern is not None:
            data["pattern"] = pattern.to_json()
            buckets["matches_pattern"].append(data)
            continue
        if not gcs.is_video(item.name):
            data["reason"] = "extensão não é de vídeo"
            buckets["skipped"].append(data)
            continue
        buckets["new"].append(data)

    _listing_cache[ctx.object_id] = {
        "uri": gcs.normalize_uri(uri),
        "listed_at": iso(),
        "files": {item.name: item for item in remote},
    }

    return {
        "gcs_uri": gcs.normalize_uri(uri),
        "listed_at": iso(),
        "total": len(remote),
        "counts": {key: len(value) for key, value in buckets.items()},
        "buckets": buckets,
    }


class DownloadIn(BaseModel):
    names: list[str]


@router.post("/gcs/download")
async def gcs_download(
    payload: DownloadIn,
    ctx: ObjectContext = Depends(get_object),
    user: User = Depends(current_user),
    client_id: str = Depends(current_client),
) -> dict:
    cached = _listing_cache.get(ctx.object_id)
    if not cached:
        raise HTTPException(409, "liste o bucket antes de baixar")

    try:
        compiled = compile_patterns(ctx.config.exclude_patterns)
    except InvalidPattern as exc:
        raise HTTPException(422, str(exc)) from exc

    # Revalidação: o array vem do cliente e nunca é tratado como confiável.
    selected = []
    for name in payload.names:
        if not gcs.safe_name(name):
            raise HTTPException(400, f"nome inseguro: {name!r}")
        item = cached["files"].get(name)
        if item is None:
            raise HTTPException(400, f"'{name}' não está na listagem atual do bucket")
        if ctx.exclusions.has(name) or match_pattern(name, compiled) is not None:
            continue
        selected.append(item)

    if not selected:
        raise HTTPException(409, "nada para baixar depois de aplicar as regras")

    if durable_jobs.enabled():
        job_id = await asyncio.to_thread(
            durable_jobs.create,
            kind="gcs_download",
            object_id=ctx.object_id,
            payload={
                "items": [item.to_json() for item in selected],
                "gcs_uri": cached["uri"],
                "client_id": client_id,
                "user": user.user_id,
                "message": f"baixando {len(selected)} arquivos",
            },
            priority=60,
        )
        return {"job_id": job_id, "total": len(selected)}

    try:
        binary = gcs.resolve()
    except gcs.GcsError as exc:
        raise HTTPException(502, str(exc)) from exc

    job = jobs.create(
        "gcs_download", ctx.object_id, "", len(selected),
        f"baixando {len(selected)} arquivos…",
        client_id=client_id, user=user.user_id,
    )

    staging = ctx.incoming_dir
    staging.mkdir(parents=True, exist_ok=True)
    ctx.videos_root.mkdir(parents=True, exist_ok=True)
    env = gcs.child_env()

    async def worker() -> None:
        job.state = "running"
        jobs._publish(job)
        done = 0

        for offset in range(0, len(selected), gcs.CHUNK):
            if job._cancelled:
                break
            chunk = selected[offset : offset + gcs.CHUNK]
            job.message = f"arquivos {offset + 1}–{offset + len(chunk)} de {len(selected)}"
            jobs._publish(job)

            try:
                argv = gcs.download_argv(binary, [item.uri for item in chunk], staging)
            except gcs.GcsError as exc:
                job.state = "error"
                job.error = str(exc)
                job.finished_at = iso()
                jobs._publish(job)
                return

            result = await asyncio.to_thread(_run_chunk, argv, env)
            if result is not None:
                job.state = "error"
                job.error = result
                job.finished_at = iso()
                jobs._publish(job)
                return

            # Publicação: só entra na pasta de vídeos o arquivo já completo.
            for item in chunk:
                staged = staging / item.name
                if not staged.exists():
                    continue
                destination = ctx.videos_root / item.name
                try:
                    os.replace(staged, destination)
                except OSError:
                    shutil.move(str(staged), str(destination))
                ctx.downloads.record(
                    item.name,
                    relpath=item.name,
                    gcs_uri=item.uri,
                    size_bytes=item.size_bytes,
                    generation=item.generation,
                    updated=item.updated,
                    user=user.user_id,
                    job_id=job.job_id,
                )
                done += 1

            # Flush a cada bloco: uma queda no meio não pode virar "rebaixar tudo".
            ctx.downloads.save(cached["uri"])
            job.current = done
            jobs._publish(job)

        await asyncio.to_thread(ctx.rescan)
        job.state = "cancelled" if job._cancelled else "done"
        job.result = {"downloaded": done, "requested": len(selected)}
        job.message = f"{done} arquivos baixados"
        job.finished_at = iso()
        jobs._publish(job)

    asyncio.create_task(worker())
    return {"job_id": job.job_id, "total": len(selected)}


def _run_chunk(argv: list[str], env: dict[str, str]) -> str | None:
    """Roda um `gcloud storage cp`. Devolve None em sucesso, ou o erro."""
    import subprocess
    import sys

    no_window = (
        {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}
    )
    result = subprocess.run(
        argv, capture_output=True, text=True, env=env, timeout=3600, **no_window
    )
    if result.returncode != 0:
        return (result.stderr or "").strip()[-2000:] or f"gcloud saiu com {result.returncode}"
    return None
