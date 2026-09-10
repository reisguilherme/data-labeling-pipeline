from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from pipeline_core.jobs import PostgresJobQueue
from pipeline_core.storage import MinioBlobStore


class Cancelled(RuntimeError):
    pass


_COMPLETION_RESPONSE_LIMIT = 64 * 1024


def _report_video_export_completion(
    job: dict,
    lease_token: str,
    result: dict,
) -> dict:
    """Ask the API to commit a published export before acknowledging its job."""
    base_url = os.environ.get("MST_API", "").strip().rstrip("/")
    worker_token = os.environ.get("MST_WORKER_TOKEN", "")
    if not base_url or not worker_token:
        raise RuntimeError("MST_API e MST_WORKER_TOKEN sao obrigatorios no worker")
    parsed = urllib.parse.urlsplit(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username:
        raise RuntimeError("MST_API invalida")
    payload = job.get("payload") or {}
    object_id = payload.get("object_id")
    video_id = payload.get("video_id")
    if not isinstance(object_id, str) or not object_id:
        raise RuntimeError("job de export sem object_id")
    if not isinstance(video_id, str) or not video_id:
        raise RuntimeError("job de export sem video_id")
    path = (
        f"/api/objects/{urllib.parse.quote(object_id, safe='')}"
        f"/videos/{urllib.parse.quote(video_id, safe='')}/export/complete"
    )
    body = json.dumps(
        {
            "job_id": str(job["id"]),
            "lease_token": lease_token,
            "result": result,
        },
        separators=(",", ":"),
    ).encode("utf-8")
    request = urllib.request.Request(
        base_url + path,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-MST-Worker": worker_token,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
            encoded = response.read(_COMPLETION_RESPONSE_LIMIT + 1)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"API rejeitou conclusao do export (HTTP {exc.code})") from None
    except urllib.error.URLError as exc:
        raise RuntimeError("API de conclusao do export indisponivel") from exc
    if len(encoded) > _COMPLETION_RESPONSE_LIMIT:
        raise RuntimeError("resposta da conclusao do export excedeu o limite")
    try:
        decoded = json.loads(encoded)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("resposta invalida da conclusao do export") from exc
    if not isinstance(decoded, dict) or decoded.get("ok") is not True:
        raise RuntimeError("API nao confirmou a conclusao do export")
    return decoded


def _context(job: dict):
    from server.config import settings
    from server.workspace import workspace

    settings.workspace_root = Path(os.environ.get("MST_WORKSPACE", "/workspace"))
    workspace.load()
    object_id = job["payload"]["object_id"]
    # API e worker são processos diferentes. O contexto mantém o
    # annotations.json em memória, portanto reutilizá-lo aqui faria um job novo
    # enxergar o snapshot do job anterior e acusar "anotacao ou intervalos
    # ausentes" logo depois de a API ter salvado o intervalo.
    workspace.invalidate(object_id)
    ctx = workspace.context(object_id)
    ctx.ensure_loaded()
    return ctx


def _validated_tree_child(path: Path, root: Path) -> Path:
    candidate = Path(os.path.abspath(os.fspath(path)))
    lexical_root = Path(os.path.abspath(os.fspath(root)))
    try:
        relative = candidate.relative_to(lexical_root)
    except ValueError as exc:
        raise ValueError(f"recusa remover caminho fora do cache/export: {candidate}") from exc
    if not relative.parts or lexical_root.is_symlink():
        raise ValueError(f"recusa remover caminho fora do cache/export: {candidate}")
    current = lexical_root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError(f"recusa remover symlink de cache/export: {current}")
    resolved = candidate.resolve()
    expected_root = lexical_root.resolve()
    if resolved == expected_root or not resolved.is_relative_to(expected_root):
        raise ValueError(f"recusa remover caminho fora do cache/export: {resolved}")
    return candidate


def _remove_tree(path: Path, root: Path) -> None:
    candidate = _validated_tree_child(path, root)
    if candidate.exists():
        candidate = _validated_tree_child(candidate, root)
        shutil.rmtree(candidate)


def _export_root_from_basename(ctx, basename: str) -> Path:
    """Resolve exactly one non-symlink child below an object's output root."""
    if (
        not isinstance(basename, str)
        or not basename
        or basename in {".", ".."}
        or "/" in basename
        or "\\" in basename
        or Path(basename).is_absolute()
        or Path(basename).name != basename
    ):
        raise ValueError("raiz de export invalida")
    output_root = ctx.output_root.resolve()
    target = ctx.output_root / basename
    if target.is_symlink():
        raise ValueError("raiz de export nao pode ser link simbolico")
    resolved = target.resolve()
    if resolved.parent != output_root or resolved == output_root:
        raise ValueError("raiz de export fora do objeto")
    if target.exists() and not target.is_dir():
        raise ValueError("raiz de export existente nao e diretorio")
    return target


def _payload_revision(payload: dict) -> int:
    revision = payload.get("annotation_revision", 0)
    if type(revision) is not int or revision < 0:
        raise ValueError("annotation_revision invalida")
    return revision


def _entry_revision(entry: dict | None) -> int:
    if entry is None:
        return 0
    revision = entry.get("annotation_revision", 0)
    return revision if type(revision) is int and revision >= 0 else 0


def _stale_result(expected: int, current: int) -> dict:
    return {
        "stale": True,
        "annotation_revision": expected,
        "current_revision": current,
    }


def _staging_export_root(root: Path, job_id: object, lease_token: object) -> Path:
    identity = f"{job_id}:{lease_token}"
    token = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
    return root.parent / f".{root.name}.export-{token}.part"


def _trash_export_root(root: Path, job_id: object, operation: str) -> Path:
    token = hashlib.sha256(str(job_id).encode("utf-8")).hexdigest()[:16]
    return root / f".{operation}-{token}.trash"


def _run_ffmpeg(
    argv: list[str],
    job: dict,
    queue: PostgresJobQueue,
    token: str,
    *,
    offset: int = 0,
    total: int = 0,
) -> None:
    from server.jobs import ffmpeg_progress

    last_lines: list[str] = []
    process = subprocess.Popen(  # noqa: S603 - argv is built by server.ffmpeg
        argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    try:
        assert process.stdout is not None
        for line in process.stdout:
            text = line.strip()
            if text:
                last_lines.append(text)
                del last_lines[:-30]
            frame = ffmpeg_progress(line)
            if frame is None:
                continue
            if not queue.update_progress(
                str(job["id"]),
                token,
                {
                    "current": offset + frame,
                    "total": total,
                    "message": job["payload"].get("message", "processando video"),
                },
            ):
                process.terminate()
                raise Cancelled("cancelamento solicitado")
        return_code = process.wait()
        if return_code != 0:
            raise RuntimeError("ffmpeg falhou: " + "\n".join(last_lines[-10:]))
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()


def run_proxy_full(job: dict, queue: PostgresJobQueue, token: str) -> dict:
    from server import ffmpeg, proxy

    ctx = _context(job)
    payload = job["payload"]
    video_id = payload["video_id"]
    out = proxy.proxy_dir(ctx, video_id)
    staging = proxy.new_staging_generation(out, ctx.cache_dir)
    for tier in proxy.TIERS:
        (staging / tier).mkdir(parents=True, exist_ok=True)
    source = ctx.index.resolve_path(video_id)
    total = int(payload.get("frame_count") or 0)
    try:
        _run_ffmpeg(
            ffmpeg.proxy_full_argv(source, staging / proxy.SMALL, staging / proxy.FULL),
            job,
            queue,
            token,
            total=total,
        )
        validated = proxy.validate_generation(staging)
        if not queue.update_progress(
            str(job["id"]),
            token,
            {"current": validated.frames, "total": total, "message": "publicando proxy"},
        ):
            raise Cancelled("cancelamento solicitado antes da publicação")

        def publish_guard() -> bool:
            if not queue.update_progress(
                str(job["id"]),
                token,
                {"current": validated.frames, "total": total, "message": "publicando proxy"},
            ):
                raise Cancelled("cancelamento solicitado no commit do proxy")
            return True

        generation = proxy.publish_generation(
            staging,
            out,
            ctx.cache_dir,
            kind="full",
            job_id=str(job["id"]),
            publish_guard=publish_guard,
        )
        ctx.index.update_frame_count(video_id, generation.frames, "proxy_extraction")
        return {
            "frames": generation.frames,
            "dir": str(generation.path),
            "generation": generation.token,
        }
    except Exception:
        _remove_tree(staging, ctx.cache_dir)
        raise


def run_proxy_window(job: dict, queue: PostgresJobQueue, token: str) -> dict:
    from server import ffmpeg, proxy

    ctx = _context(job)
    payload = job["payload"]
    video_id = payload["video_id"]
    start, end = int(payload["start"]), int(payload["end"])
    final = proxy.window_dir(ctx, video_id, start, end)
    staging = proxy.new_staging_generation(final, ctx.cache_dir)
    for tier in proxy.TIERS:
        (staging / tier).mkdir(parents=True, exist_ok=True)
    source = ctx.index.resolve_path(video_id)
    expected = end - start + 1
    try:
        _run_ffmpeg(
            ffmpeg.proxy_window_argv(
                source, staging / proxy.SMALL, staging / proxy.FULL, start, end
            ),
            job,
            queue,
            token,
            total=expected,
        )
        validated = proxy.validate_generation(staging, expected_frames=expected)
        if not queue.update_progress(
            str(job["id"]),
            token,
            {"current": validated.frames, "total": expected, "message": "publicando janela"},
        ):
            raise Cancelled("cancelamento solicitado antes da publicação")

        def publish_guard() -> bool:
            if not queue.update_progress(
                str(job["id"]),
                token,
                {
                    "current": validated.frames,
                    "total": expected,
                    "message": "publicando janela",
                },
            ):
                raise Cancelled("cancelamento solicitado no commit da janela")
            return True

        generation = proxy.publish_generation(
            staging,
            final,
            ctx.cache_dir,
            kind="window",
            job_id=str(job["id"]),
            start=start,
            end=end,
            expected_frames=expected,
            publish_guard=publish_guard,
        )
        proxy._touch(final)
        try:
            proxy._evict_if_needed(ctx)
        except Exception:
            logging.getLogger(__name__).warning(
                "falha na manutenção LRU após publicar janela", exc_info=True
            )
        return {
            "start": start,
            "end": end,
            "frames": generation.frames,
            "generation": generation.token,
        }
    except Exception:
        _remove_tree(staging, ctx.cache_dir)
        raise


def run_video_export(job: dict, queue: PostgresJobQueue, token: str) -> dict:
    from server import durable_jobs
    from server import export as video_export
    from server import ffmpeg
    from server.config import EXPORT_QSCALE

    ctx = _context(job)
    payload = job["payload"]
    video_id = payload["video_id"]
    video = ctx.index.get(video_id)
    if video is None:
        raise ValueError(f"video nao encontrado: {video_id}")
    expected_revision = _payload_revision(payload)

    with durable_jobs.video_advisory_lock(payload["object_id"], video_id):
        if not queue.update_progress(
            str(job["id"]),
            token,
            {
                "current": 0,
                "total": int(payload.get("total") or 0),
                "message": "preparando export",
            },
        ):
            raise Cancelled("cancelamento solicitado")
        ctx.store.load()
        entry = ctx.store.entry(video.relpath)
        current_revision = _entry_revision(entry)
        if current_revision != expected_revision or (entry or {}).get("status") == "no_boom":
            return _stale_result(expected_revision, current_revision)
        if entry is None or not entry.get("intervals"):
            raise ValueError("anotacao ou intervalos ausentes")
        intervals = entry["intervals"]
        media = entry.get("media") or ctx.index.cached_probe(video_id) or {}
        expected_owner = video_export.export_owner(ctx, video, expected_revision)
        declared_owner = payload.get("owner")
        if (
            declared_owner is not None
            and video_export.valid_export_owner(declared_owner) != expected_owner
        ):
            raise ValueError("owner do job de export nao corresponde ao video")
        selected_root = video_export.export_root_for(ctx, video)
        basename = payload.get("root_basename")
        if basename is None:
            # Compatibilidade com jobs enfileirados antes do fence por revisão.
            basename = selected_root.name
        root = _export_root_from_basename(ctx, basename)
        if root.resolve() != selected_root.resolve():
            raise ValueError("raiz do job de export nao corresponde ao video")
        if root.exists() and not video_export.same_export_identity(
            video_export.read_export_owner(root), expected_owner
        ):
            raise ValueError("raiz de export existente sem ownership verificavel")

    keep = {interval["segment"] for interval in intervals}
    if any(
        not isinstance(name, str)
        or not name.startswith("seg_")
        or "/" in name
        or "\\" in name
        or Path(name).name != name
        for name in keep
    ):
        raise ValueError("nome de segmento invalido")

    staging = _staging_export_root(root, job["id"], token)
    if staging.exists() or staging.is_symlink():
        if staging.is_symlink():
            raise ValueError("staging de export nao pode ser link simbolico")
        _remove_tree(staging, ctx.output_root)
    staging.mkdir()
    publish_trash = _trash_export_root(
        root, f"{job['id']}:{time.time_ns()}", "replace"
    )
    source = ctx.index.resolve_path(video_id)
    binaries = ffmpeg.resolve()
    total = sum(int(interval["frame_count"]) for interval in intervals)
    completed = 0
    segments: list[str] = []
    try:
        for interval in intervals:
            segment_dir = staging / interval["segment"]
            segment_dir.mkdir()
            expected = int(interval["frame_count"])
            _run_ffmpeg(
                ffmpeg.export_segment_argv(
                    source, segment_dir, interval["start_frame"], interval["end_frame"]
                ),
                job,
                queue,
                token,
                offset=completed,
                total=total,
            )
            produced = len(list(segment_dir.glob("*.jpg")))
            if produced != expected:
                raise RuntimeError(
                    f"{interval['segment']}: ffmpeg produziu {produced} frames, esperado {expected}"
                )
            width, height = video_export._frame_size(segment_dir, media)
            video_export.write_prompt_json(segment_dir, video, interval, width, height)
            segments.append(interval["segment"])
            completed += expected
            if not queue.update_progress(
                str(job["id"]),
                token,
                {"current": completed, "total": total, "message": "exportando frames"},
            ):
                raise Cancelled("cancelamento solicitado")

        # FFmpeg roda sem segurar o lock. Só a publicação é serializada e
        # revalida tanto a revisão quanto a lease imediatamente antes de tocar
        # os diretórios efetivos.
        with durable_jobs.video_advisory_lock(ctx.object_id, video_id):
            ctx.store.load()
            latest = ctx.store.entry(video.relpath)
            current_revision = _entry_revision(latest)
            if (
                current_revision != expected_revision
                or (latest or {}).get("status") == "no_boom"
            ):
                return _stale_result(expected_revision, current_revision)
            if not queue.update_progress(
                str(job["id"]),
                token,
                {"current": completed, "total": total, "message": "publicando frames"},
            ):
                raise Cancelled("cancelamento solicitado")
            selected_root = video_export.export_root_for(ctx, video)
            if selected_root.resolve() != root.resolve():
                raise ValueError("raiz do export mudou antes da publicacao")
            video_export.publish_staged_export(
                root,
                staging,
                segments,
                expected_owner,
                publish_trash,
            )

        if publish_trash.exists():
            try:
                _remove_tree(publish_trash, root)
            except OSError:
                logging.getLogger(__name__).warning(
                    "falha ao remover lixeira de export publicada", exc_info=True
                )

        return {
            "root": root.as_posix(),
            "segments": segments,
            "total_frames": completed,
            "jpeg_qscale": EXPORT_QSCALE,
            "frame_naming": video_export._NAMING_RATIONALE,
            "ffmpeg_version": binaries.version,
            "annotation_revision": expected_revision,
            "owner": expected_owner,
        }
    finally:
        if staging.exists():
            _remove_tree(staging, ctx.output_root)


def run_video_export_cleanup(job: dict, queue: PostgresJobQueue, token: str) -> dict:
    from server import durable_jobs
    from server import export as video_export

    ctx = _context(job)
    payload = job["payload"]
    video_id = payload["video_id"]
    relpath = payload.get("relpath")
    if not isinstance(relpath, str) or not relpath:
        video = getattr(ctx, "index", None)
        video = video.get(video_id) if video is not None else None
        if video is None:
            raise ValueError(f"video nao encontrado: {video_id}")
        relpath = video.relpath
    expected_revision = _payload_revision(payload)
    basename = payload.get("root_basename")
    root = _export_root_from_basename(ctx, basename)
    trash = _trash_export_root(root, job["id"], "cleanup")
    if trash.is_symlink():
        raise ValueError("lixeira de cleanup nao pode ser link simbolico")

    def deferred(reason: str) -> dict:
        return {
            "deferred": True,
            "reason": reason,
            "removed": [],
            "root": root.as_posix(),
            "annotation_revision": expected_revision,
        }

    removed: list[str] = []
    with durable_jobs.video_advisory_lock(payload["object_id"], video_id):
        if not queue.update_progress(
            str(job["id"]),
            token,
            {"current": 0, "total": 1, "message": "limpando export anterior"},
        ):
            raise Cancelled("cancelamento solicitado")
        ctx.store.load()
        entry = ctx.store.entry(relpath)
        current_revision = _entry_revision(entry)
        marker = (entry or {}).get("export_cleanup") or {}
        if (
            current_revision != expected_revision
            or (entry or {}).get("status") != "no_boom"
            or marker.get("root_basename") != basename
        ):
            return _stale_result(expected_revision, current_revision)

        expected_owner = video_export.valid_export_owner(payload.get("owner"))
        marker_owner = video_export.valid_export_owner(marker.get("owner"))
        if expected_owner is None:
            return deferred("owner_missing")
        if (
            expected_owner["object_id"] != payload.get("object_id")
            or expected_owner["video_id"] != video_id
            or expected_owner["relpath"] != relpath
            or marker_owner != expected_owner
        ):
            return deferred("owner_mismatch")

        if root.exists():
            actual_owner = video_export.read_export_owner(root)
            if actual_owner is None:
                return deferred("owner_marker_missing")
            if actual_owner != expected_owner:
                return deferred("owner_mismatch")
            candidates: list[Path] = []
            for child in sorted(root.iterdir(), key=lambda path: path.name):
                if not child.name.startswith("seg_"):
                    continue
                if child.is_symlink():
                    raise ValueError("segmento de export nao pode ser link simbolico")
                if not child.is_dir():
                    continue
                candidates.append(child)

            # A estrutura inteira e a lease são validadas antes do primeiro
            # rename. Depois que o commit começa não há mais cancelamento capaz
            # de deixar metade dos segmentos no root e metade na lixeira.
            if trash.exists() and not trash.is_dir():
                raise ValueError("lixeira de cleanup invalida")
            for child in candidates:
                destination = trash / child.name
                if destination.exists() or destination.is_symlink():
                    raise ValueError("lixeira de cleanup contem segmento conflitante")
                if not queue.update_progress(
                    str(job["id"]),
                    token,
                    {
                        "current": len(removed),
                        "total": 1,
                        "message": "limpando export anterior",
                    },
                ):
                    raise Cancelled("cancelamento solicitado")

            trash_created = False
            moved: list[Path] = []
            try:
                if candidates and not trash.exists():
                    trash.mkdir()
                    trash_created = True
                for child in candidates:
                    child.replace(trash / child.name)
                    moved.append(child)
                    removed.append(child.name)
            except Exception as exc:
                rollback_errors: list[Exception] = []
                for child in reversed(moved):
                    previous = trash / child.name
                    try:
                        if (previous.exists() or previous.is_symlink()) and not (
                            child.exists() or child.is_symlink()
                        ):
                            previous.replace(child)
                    except Exception as rollback_exc:  # noqa: BLE001
                        rollback_errors.append(rollback_exc)
                if trash_created:
                    try:
                        trash.rmdir()
                    except OSError as rollback_exc:
                        rollback_errors.append(rollback_exc)
                if rollback_errors:
                    raise RuntimeError(
                        f"cleanup falhou e rollback ficou incompleto: {rollback_errors[0]}"
                    ) from exc
                raise

    # O lock protege apenas o fence e os renames atômicos. A exclusão de
    # milhares de JPEGs acontece depois, para não bloquear ações da interface.
    if trash.exists():
        _remove_tree(trash, root)
    return {
        "removed": removed,
        "root": root.as_posix(),
        "annotation_revision": expected_revision,
    }


def run_dataset_export(job: dict, queue: PostgresJobQueue, token: str) -> dict:
    from server.dataset import (
        _safe_dataset_name,
        export_snapshot_atomic,
        validate_snapshot,
    )
    from server.config import settings
    from server.workspace import workspace

    settings.workspace_root = Path(os.environ.get("MST_WORKSPACE", "/workspace"))
    workspace.load()
    payload = job["payload"]
    ctx = workspace.context(payload["object_id"])
    ctx.ensure_loaded()
    name = _safe_dataset_name(str(payload["name"]))
    out_dir = ctx.output_root / "_datasets" / name
    snapshot = payload.get("snapshot")
    validate_snapshot(snapshot, object_id=ctx.object_id)

    def progress(current: int, total: int) -> None:
        if not queue.update_progress(
            str(job["id"]),
            token,
            {"current": current, "total": total, "message": "exportando dataset"},
        ):
            raise Cancelled("cancelamento solicitado")

    def before_publish() -> None:
        if not queue.update_progress(
            str(job["id"]),
            token,
            {"message": "publicando dataset"},
        ):
            raise Cancelled("lease perdido antes da publicacao do dataset")

    result = export_snapshot_atomic(
        ctx,
        snapshot,
        out_dir=out_dir,
        fmt=payload["format"],
        task=payload["task"],
        val_fraction=float(payload.get("val_fraction", 0.2)),
        test_fraction=float(payload.get("test_fraction", 0)),
        workspace_root=workspace.root,
        owner=f"{job['id']}:{token}",
        on_progress=progress,
        before_publish=before_publish,
    )
    store = MinioBlobStore.from_env()
    if store is not None:
        prefix = f"{ctx.object_id}/{name}/generations/{snapshot['snapshot_id']}"
        stored = 0
        for path in out_dir.rglob("*"):
            if not path.is_file():
                continue
            store.put_file(
                "datasets",
                f"{prefix}/{path.relative_to(out_dir).as_posix()}",
                path,
            )
            stored += 1
        result["minio_prefix"] = f"datasets/{prefix}"
        result["minio_files"] = stored
    return result


def run_global_dataset_export(job: dict, queue: PostgresJobQueue, token: str) -> dict:
    from server import durable_jobs
    from server.dataset import _safe_dataset_name
    from server.config import settings
    from server.multiclass_dataset import (
        export_multiclass_snapshot_atomic,
        validate_multiclass_snapshot,
    )

    root = Path(os.environ.get("MST_WORKSPACE", "/workspace")).resolve()
    settings.workspace_root = root
    payload = job["payload"]
    snapshot = payload.get("snapshot")
    validate_multiclass_snapshot(snapshot)
    name = _safe_dataset_name(str(payload["name"]))
    out_dir = root / "_datasets" / name

    def progress(current: int, total: int) -> None:
        if not queue.update_progress(
            str(job["id"]), token,
            {"current": current, "total": total, "message": "exportando dataset global"},
        ):
            raise Cancelled("cancelamento solicitado")

    def before_publish() -> None:
        if not queue.update_progress(
            str(job["id"]),
            token,
            {"message": "publicando dataset global"},
        ):
            raise Cancelled("lease perdido antes da publicacao do dataset global")

    @contextmanager
    def publication_fence():
        with durable_jobs.owned_job_lease(str(job["id"]), token) as owned:
            if owned is None or owned.get("kind") != "dataset_export_global":
                raise Cancelled("lease perdido no commit do dataset global")
            yield

    result = export_multiclass_snapshot_atomic(
        root,
        snapshot,
        out_dir=out_dir,
        fmt=payload["format"],
        task=payload["task"],
        val_fraction=float(payload.get("val_fraction", 0.2)),
        test_fraction=float(payload.get("test_fraction", 0)),
        owner=f"{job['id']}:{token}",
        on_progress=progress,
        before_publish=before_publish,
        publication_fence=publication_fence,
    )
    store = MinioBlobStore.from_env()
    if store is not None:
        prefix = f"global/{name}/generations/{snapshot['snapshot_id']}"
        stored = 0
        manifest_path = out_dir / "dataset_manifest.json"
        if not manifest_path.is_file():
            raise RuntimeError("dataset global publicado sem manifesto")
        # The manifest is the generation commit marker for MinIO readers.  A
        # failed upload may leave immutable blobs behind, but never a manifest
        # that advertises an incomplete generation.
        artifacts = sorted(
            path
            for path in out_dir.rglob("*")
            if path.is_file() and path != manifest_path
        )
        total_files = len(artifacts) + 1
        for index, path in enumerate(artifacts):
            if not queue.update_progress(
                str(job["id"]),
                token,
                {
                    "current": index,
                    "total": total_files,
                    "message": "sincronizando dataset global",
                },
            ):
                raise Cancelled(
                    "lease perdido durante upload do dataset global"
                )
            store.put_file(
                "datasets",
                f"{prefix}/{path.relative_to(out_dir).as_posix()}",
                path,
            )
            stored += 1
        if not queue.update_progress(
            str(job["id"]),
            token,
            {
                "current": len(artifacts),
                "total": total_files,
                "message": "publicando manifesto do dataset global",
            },
        ):
            raise Cancelled("lease perdido antes do manifesto do dataset global")
        # The database row remains locked through the irreversible commit-marker
        # PUT. A cancellation already committed wins the lock and prevents this
        # upload; one arriving afterwards observes the publication as first.
        with publication_fence():
            store.put_file(
                "datasets",
                f"{prefix}/dataset_manifest.json",
                manifest_path,
            )
        stored += 1
        result["minio_prefix"] = f"datasets/{prefix}"
        result["minio_files"] = stored
    return result


def run_gcs_download(job: dict, queue: PostgresJobQueue, token: str) -> dict:
    from server import gcs
    from server.config import settings
    from server.workspace import workspace

    settings.workspace_root = Path("/workspace")
    workspace.load()
    payload = job["payload"]
    ctx = workspace.context(payload["object_id"])
    ctx.ensure_loaded()
    items = payload.get("items") or []
    ctx.incoming_dir.mkdir(parents=True, exist_ok=True)
    ctx.videos_root.mkdir(parents=True, exist_ok=True)
    blob_store = MinioBlobStore.from_env()
    completed = 0
    for item in items:
        if not gcs.safe_name(str(item.get("name") or "")):
            raise ValueError(f"nome GCS inseguro: {item.get('name')!r}")
        if not queue.update_progress(
            str(job["id"]),
            token,
            {"current": completed, "total": len(items), "message": f"baixando {item['name']}"},
        ):
            raise Cancelled("cancelamento solicitado")
        staged = ctx.incoming_dir / item["name"]
        destination = ctx.videos_root / item["name"]
        gcs.download_file(item["uri"], staged)
        try:
            os.replace(staged, destination)
        except OSError:
            shutil.move(str(staged), str(destination))
        ctx.downloads.record(
            item["name"],
            relpath=item["name"],
            gcs_uri=item["uri"],
            size_bytes=item.get("size_bytes"),
            generation=item.get("generation"),
            updated=item.get("updated"),
            user=payload.get("user"),
            job_id=str(job["id"]),
        )
        if blob_store is not None:
            blob_store.put_file("videos", f"{ctx.object_id}/{item['name']}", destination)
        completed += 1
        ctx.downloads.save(payload.get("gcs_uri"))
        if not queue.update_progress(
            str(job["id"]),
            token,
            {"current": completed, "total": len(items), "message": "download GCS"},
        ):
            raise Cancelled("cancelamento solicitado")
    ctx.rescan()
    return {"downloaded": completed, "requested": len(items)}


def run_object_purge(job: dict, queue: PostgresJobQueue, token: str) -> dict:
    """Executa purge somente depois do inventário e das validações de confinamento."""
    from server.config import settings
    from server.object_lifecycle import (
        delete_project_records,
        partition_owned_object_paths,
        preserve_exported_datasets,
        remove_minio_object_prefix,
        write_purge_inventory,
    )
    from server.workspace import workspace

    root = Path(os.environ.get("MST_WORKSPACE", "/workspace")).resolve()
    settings.workspace_root = root
    workspace.load()
    object_id = str(job["payload"]["object_id"])
    cfg = workspace.get(object_id)
    if not cfg.archived:
        raise ValueError("objeto deixou de estar arquivado; purge cancelado")
    registered_roots = [
        (registered.object_id, path)
        for registered in workspace.list(include_archived=True)
        for path in (registered.videos_root, registered.output_root)
    ]
    managed, skipped = partition_owned_object_paths(
        root,
        object_id,
        [cfg.videos_root, cfg.output_root],
        registered_roots=registered_roots,
    )
    if [str(path) for path in managed] != list(job["payload"].get("managed_paths") or []):
        raise ValueError("raízes do objeto mudaram desde a solicitação; purge cancelado")

    queue.update_progress(
        str(job["id"]), token,
        {"current": 1, "total": 5, "message": "gerando inventário e checksums"},
    )
    inventory_path, inventory = write_purge_inventory(
        workspace_root=root,
        object_id=object_id,
        roots=managed,
        skipped_paths=skipped,
    )
    store = MinioBlobStore.from_env()
    if store is not None:
        store.put_file(
            "backups",
            f"purges/{object_id}/{inventory_path.name}",
            inventory_path,
        )

    queue.update_progress(
        str(job["id"]), token,
        {"current": 2, "total": 5, "message": "preservando datasets exportados"},
    )
    preserved = preserve_exported_datasets(root, object_id, managed)

    queue.update_progress(
        str(job["id"]), token,
        {"current": 3, "total": 5, "message": "removendo blobs do objeto"},
    )
    minio_removed = remove_minio_object_prefix(store, object_id) if store is not None else 0

    queue.update_progress(
        str(job["id"]), token,
        {"current": 4, "total": 5, "message": "removendo metadados e raízes gerenciadas"},
    )
    database = delete_project_records(
        os.environ["DATABASE_URL"], object_id, actor=job["payload"].get("actor")
    )
    removed_paths: list[str] = []
    for path in managed:
        still_managed, _ = partition_owned_object_paths(
            root,
            object_id,
            [path],
            registered_roots=registered_roots,
        )
        # Revalida imediatamente antes do passo destrutivo; nenhuma string do
        # payload é usada como alvo.
        if still_managed != [path]:
            raise ValueError(f"alvo perdeu ownership durante o purge: {path}")
        if path.exists():
            shutil.rmtree(path)
            removed_paths.append(path.as_posix())
    workspace.remove_registration(object_id)
    queue.update_progress(
        str(job["id"]), token,
        {"current": 5, "total": 5, "message": "exclusão concluída"},
    )
    return {
        "object_id": object_id,
        "inventory": inventory_path.as_posix(),
        "inventory_files": inventory["file_count"],
        "inventory_bytes": inventory["total_bytes"],
        "preserved_datasets": preserved,
        "minio_versions_removed": minio_removed,
        "removed_paths": removed_paths,
        "skipped_paths": [path.as_posix() for path in skipped],
        **database,
    }


log = logging.getLogger("pipeline.cpu-worker")
_PROXY_SWEEP_INTERVAL_SECONDS = 300.0


@dataclass(frozen=True, slots=True)
class _ProxyMaintenanceTarget:
    object_id: str
    cache_dir: Path


def _proxy_maintenance_targets(
    workspace_root: Path,
) -> tuple[_ProxyMaintenanceTarget, ...]:
    """Le um snapshot do registro sem tocar no Workspace mutavel do processo."""
    from server.workspace import ObjectConfig

    try:
        raw = (workspace_root / "objects.json").read_text(encoding="utf-8")
        payload = json.loads(raw)
    except (OSError, ValueError, RecursionError):
        return ()
    if not isinstance(payload, dict) or not isinstance(payload.get("objects"), list):
        return ()

    targets: list[_ProxyMaintenanceTarget] = []
    for raw in payload["objects"]:
        if not isinstance(raw, dict) or not raw.get("object_id"):
            continue
        try:
            config = ObjectConfig.from_json(raw, workspace_root)
        except (KeyError, TypeError, ValueError, OSError):
            continue
        targets.append(
            _ProxyMaintenanceTarget(
                object_id=config.object_id,
                cache_dir=config.output_root / "_cache",
            )
        )
    return tuple(targets)


def _run_proxy_maintenance() -> None:
    """Executa um lote curto de recuperacao sem carregar indices de videos."""
    workspace_root = os.environ.get("MST_WORKSPACE")
    if not workspace_root:
        return
    from server import proxy

    for target in _proxy_maintenance_targets(Path(workspace_root).resolve()):
        try:
            result = proxy.sweep_cache(target)
            if result["errors"]:
                log.warning(
                    "sweep de proxy de %s terminou com %s erro(s)",
                    target.object_id,
                    result["errors"],
                )
        except Exception:  # noqa: BLE001 - manutencao nunca derruba consumidor
            log.warning(
                "falha no sweep de proxy de %s", target.object_id, exc_info=True
            )


class _ProxyMaintenanceScheduler:
    """Agenda maintenance fora do claim loop, com no maximo um voo ativo."""

    def __init__(
        self, runner: Callable[[], None], *, interval_seconds: float
    ) -> None:
        self._runner = runner
        self._interval_seconds = float(interval_seconds)
        self._next_due = 0.0
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def _run(self) -> None:
        try:
            self._runner()
        except Exception:  # noqa: BLE001 - maintenance nunca derruba o worker
            log.warning("falha na maintenance periodica de proxy", exc_info=True)

    def maybe_start(self, now: float) -> bool:
        with self._lock:
            if now < self._next_due:
                return False
            if self._thread is not None and self._thread.is_alive():
                return False
            self._next_due = now + self._interval_seconds
            self._thread = threading.Thread(
                target=self._run,
                name="proxy-maintenance",
                daemon=True,
            )
            try:
                self._thread.start()
            except Exception:  # noqa: BLE001 - claim loop continua sem maintenance
                self._thread = None
                log.warning("nao foi possivel iniciar maintenance", exc_info=True)
                return False
            return True

    def wait(self, *, timeout: float | None = None) -> bool:
        """Hook de teste/shutdown; o loop normal nunca espera maintenance."""
        with self._lock:
            thread = self._thread
        if thread is None:
            return True
        thread.join(timeout=timeout)
        return not thread.is_alive()


def _settle_job_best_effort(job_id: str, action) -> None:
    """Perder a lease abandona o resultado; nunca encerra o consumidor CPU."""
    try:
        action()
    except Exception:  # noqa: BLE001 - qualquer falha será recuperada pela lease/reaper
        log.warning(
            "não foi possível finalizar job %s; lease será reavaliada",
            job_id,
            exc_info=True,
        )


def main() -> int:
    import psycopg

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    url = os.environ["DATABASE_URL"]
    queue = PostgresJobQueue(lambda: psycopg.connect(url))
    worker_id = os.environ.get("CPU_WORKER_ID", f"cpu-{socket.gethostname()}")
    maintenance = _ProxyMaintenanceScheduler(
        _run_proxy_maintenance,
        interval_seconds=_PROXY_SWEEP_INTERVAL_SECONDS,
    )
    while True:
        maintenance.maybe_start(time.monotonic())
        job = queue.claim(worker_id=worker_id, worker_kind="cpu", lease_seconds=180)
        if job is None:
            time.sleep(2)
            continue
        token = str(job["lease_token"])
        stop_heartbeat = threading.Event()
        lease_errors: list[Exception] = []

        def keep_lease() -> None:
            while not stop_heartbeat.wait(45):
                try:
                    if not queue.heartbeat(str(job["id"]), token, lease_seconds=180):
                        lease_errors.append(Cancelled("cancelamento solicitado"))
                        return
                except Exception as exc:  # noqa: BLE001
                    lease_errors.append(exc)
                    return

        heartbeat = threading.Thread(target=keep_lease, name=f"lease-{job['id']}", daemon=True)
        heartbeat.start()
        try:
            if job["kind"] == "dataset_export":
                result = run_dataset_export(job, queue, token)
            elif job["kind"] == "dataset_export_global":
                result = run_global_dataset_export(job, queue, token)
            elif job["kind"] == "object_purge":
                result = run_object_purge(job, queue, token)
            elif job["kind"] == "gcs_download":
                result = run_gcs_download(job, queue, token)
            elif job["kind"] == "proxy_full":
                result = run_proxy_full(job, queue, token)
            elif job["kind"] == "proxy_window":
                result = run_proxy_window(job, queue, token)
            elif job["kind"] == "video_export":
                result = run_video_export(job, queue, token)
            elif job["kind"] == "video_export_cleanup":
                result = run_video_export_cleanup(job, queue, token)
            else:
                raise ValueError(f"job CPU sem handler registrado: {job['kind']}")
            if lease_errors:
                raise lease_errors[0]
            if job["kind"] == "video_export" and not result.get("stale"):
                _report_video_export_completion(job, token, result)
                if lease_errors:
                    raise lease_errors[0]
            stop_heartbeat.set()
            heartbeat.join(timeout=5)
            _settle_job_best_effort(
                str(job["id"]),
                lambda: queue.finish(
                    str(job["id"]), token, state="done", result=result
                ),
            )
        except Cancelled as exc:
            stop_heartbeat.set()
            heartbeat.join(timeout=5)
            _settle_job_best_effort(
                str(job["id"]),
                lambda: queue.finish(
                    str(job["id"]), token, state="cancelled", error=str(exc)
                ),
            )
        except Exception as exc:  # noqa: BLE001
            stop_heartbeat.set()
            heartbeat.join(timeout=5)
            log.exception("job %s falhou", job["id"])
            _settle_job_best_effort(
                str(job["id"]),
                lambda: queue.retry_or_fail(str(job["id"]), token, str(exc)),
            )


if __name__ == "__main__":
    raise SystemExit(main())
