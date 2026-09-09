from __future__ import annotations

import logging
import os
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path

from pipeline_core.jobs import PostgresJobQueue
from pipeline_core.storage import MinioBlobStore


class Cancelled(RuntimeError):
    pass


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


def _remove_tree(path: Path, root: Path) -> None:
    resolved = path.resolve()
    expected_root = root.resolve()
    if resolved == expected_root or not resolved.is_relative_to(expected_root):
        raise ValueError(f"recusa remover caminho fora do cache/export: {resolved}")
    if resolved.exists():
        shutil.rmtree(resolved)


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
    staging = proxy.new_staging_generation(out)
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
    staging = proxy.new_staging_generation(final)
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
    from server import export as video_export
    from server import ffmpeg
    from server.config import EXPORT_QSCALE

    ctx = _context(job)
    payload = job["payload"]
    video_id = payload["video_id"]
    video = ctx.index.get(video_id)
    if video is None:
        raise ValueError(f"video nao encontrado: {video_id}")
    entry = ctx.store.entry(video.relpath)
    if entry is None or not entry.get("intervals"):
        raise ValueError("anotacao ou intervalos ausentes")
    intervals = entry["intervals"]
    media = entry.get("media") or ctx.index.cached_probe(video_id) or {}
    root = video_export.export_root_for(ctx, video)
    root.mkdir(parents=True, exist_ok=True)
    keep = {interval["segment"] for interval in intervals}
    video_export.clean_segments(root, keep)
    source = ctx.index.resolve_path(video_id)
    binaries = ffmpeg.resolve()
    total = sum(int(interval["frame_count"]) for interval in intervals)
    completed = 0
    segments: list[str] = []
    for interval in intervals:
        segment_dir = root / interval["segment"]
        if segment_dir.exists():
            _remove_tree(segment_dir, root)
        segment_dir.mkdir(parents=True, exist_ok=True)
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
    return {
        "root": root.as_posix(),
        "segments": segments,
        "total_frames": completed,
        "jpeg_qscale": EXPORT_QSCALE,
        "frame_naming": video_export._NAMING_RATIONALE,
        "ffmpeg_version": binaries.version,
    }


def run_dataset_export(job: dict, queue: PostgresJobQueue, token: str) -> dict:
    from server.dataset import Filters, export
    from server.config import settings
    from server.workspace import workspace

    settings.workspace_root = Path("/workspace")
    workspace.load()
    payload = job["payload"]
    ctx = workspace.context(payload["object_id"])
    ctx.ensure_loaded()
    name = Path(str(payload["name"])).name
    out_dir = ctx.output_root / "_datasets" / name
    raw_filters = payload.get("filters") or {}
    filters = Filters(
        flags=raw_filters.get("flags") or {},
        video_ids=raw_filters.get("video_ids") or [],
        reviewed_only=bool(raw_filters.get("reviewed_only")),
        include_empty=bool(raw_filters.get("include_empty")),
    )

    def progress(current: int, total: int) -> None:
        if not queue.update_progress(
            str(job["id"]),
            token,
            {"current": current, "total": total, "message": "exportando dataset"},
        ):
            raise Cancelled("cancelamento solicitado")

    result = export(
        ctx,
        filters,
        out_dir=out_dir,
        fmt=payload["format"],
        task=payload["task"],
        val_fraction=float(payload.get("val_fraction", 0.2)),
        test_fraction=float(payload.get("test_fraction", 0)),
        workspace_root=workspace.root,
        on_progress=progress,
    )
    store = MinioBlobStore.from_env()
    if store is not None:
        prefix = f"{ctx.object_id}/{name}"
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
    from server.config import settings
    from server.multiclass_dataset import export_multiclass
    from server.workspace import workspace

    root = Path(os.environ.get("MST_WORKSPACE", "/workspace")).resolve()
    settings.workspace_root = root
    workspace.load()
    payload = job["payload"]
    contexts = []
    active_ids = {cfg.object_id for cfg in workspace.list()}
    for object_id in sorted(set(payload.get("object_ids") or [])):
        if object_id not in active_ids:
            raise ValueError(f"objeto inexistente ou arquivado: {object_id}")
        ctx = workspace.context(object_id)
        ctx.ensure_loaded()
        contexts.append(ctx)
    name = Path(str(payload["name"])).name
    out_dir = root / "_datasets" / name
    if out_dir.exists() and not (out_dir / "dataset_manifest.json").is_file():
        _remove_tree(out_dir, root)
    raw_filters = payload.get("filters") or {}

    def progress(current: int, total: int) -> None:
        if not queue.update_progress(
            str(job["id"]), token,
            {"current": current, "total": total, "message": "exportando dataset global"},
        ):
            raise Cancelled("cancelamento solicitado")

    result = export_multiclass(
        contexts,
        raw_filters.get("videos") or [],
        flags=raw_filters.get("flags") or {},
        include_empty=bool(raw_filters.get("include_empty")),
        out_dir=out_dir,
        fmt=payload["format"],
        task=payload["task"],
        val_fraction=float(payload.get("val_fraction", 0.2)),
        test_fraction=float(payload.get("test_fraction", 0)),
        workspace_root=root,
        on_progress=progress,
    )
    store = MinioBlobStore.from_env()
    if store is not None:
        stored = 0
        for path in out_dir.rglob("*"):
            if path.is_file():
                store.put_file(
                    "datasets",
                    f"global/{name}/{path.relative_to(out_dir).as_posix()}",
                    path,
                )
                stored += 1
        result["minio_prefix"] = f"datasets/global/{name}"
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
        partition_managed_paths,
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
    managed, skipped = partition_managed_paths(root, [cfg.videos_root, cfg.output_root])
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
        resolved = path.resolve()
        # Revalida imediatamente antes do passo destrutivo; nenhuma string do
        # payload é usada como alvo.
        if resolved == root or not resolved.is_relative_to(root):
            raise ValueError(f"alvo saiu do workspace durante o purge: {resolved}")
        if resolved.exists():
            shutil.rmtree(resolved)
            removed_paths.append(resolved.as_posix())
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


def _run_proxy_maintenance() -> None:
    """Executa um lote curto de recuperacao sem carregar indices de videos."""
    workspace_root = os.environ.get("MST_WORKSPACE")
    if not workspace_root:
        return
    from server import proxy
    from server.config import settings
    from server.workspace import workspace

    settings.workspace_root = Path(workspace_root)
    workspace.load()
    for config in workspace.list(include_archived=True):
        try:
            workspace.invalidate(config.object_id)
            result = proxy.sweep_cache(workspace.context(config.object_id))
            if result["errors"]:
                log.warning(
                    "sweep de proxy de %s terminou com %s erro(s)",
                    config.object_id,
                    result["errors"],
                )
        except Exception:  # noqa: BLE001 - manutencao nunca derruba consumidor
            log.warning(
                "falha no sweep de proxy de %s", config.object_id, exc_info=True
            )


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
    next_proxy_maintenance = 0.0
    while True:
        now = time.monotonic()
        if now >= next_proxy_maintenance:
            try:
                _run_proxy_maintenance()
            except Exception:  # pragma: no cover - defesa contra regressao futura
                log.warning("falha na manutencao periodica de proxy", exc_info=True)
            next_proxy_maintenance = now + _PROXY_SWEEP_INTERVAL_SECONDS
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
            else:
                raise ValueError(f"job CPU sem handler registrado: {job['kind']}")
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
