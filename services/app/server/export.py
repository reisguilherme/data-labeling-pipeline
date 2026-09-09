"""Export dos segmentos: os frames e o prompt.json que alimentam o SAM3.

Esta é a fronteira do contrato com o pipeline em ../sam3-annotation-tool.
Ver `_NAMING_RATIONALE` antes de mudar a numeração dos arquivos.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
from pathlib import Path
from uuid import uuid4

from . import ffmpeg
from .config import EXPORT_QSCALE
from .jobs import Job, jobs
from .videos import VideoFile, export_folder_name, iso

log = logging.getLogger("movies-screening-tool.export")

# Por que os arquivos reiniciam em 000000.jpg em cada seg_XX:
#
# `add_new_points_or_box` recebe `frame_idx` como ÍNDICE POSICIONAL dentro de
# `list_frame_names()`. Reiniciando do zero temos
# `número no nome == índice posicional == frame_idx`; o prompt pode apontar para
# qualquer índice local via `prompt_frame_idx`. Se usássemos o
# índice original do vídeo, o primeiro arquivo da pasta seria 001204.jpg enquanto
# o SAM3 o chama de frame 0 — armadilha permanente de off-by-1204 para qualquer
# leitor futuro, e exatamente o que produz máscaras alinhadas ao frame errado.
#
# A rastreabilidade fica preservada e explícita no prompt.json:
#   original_frame = source_start_frame + local_index
_NAMING_RATIONALE = "restart_per_segment"

PROMPT_SCHEMA_VERSION = 1
EXPORT_OWNER_SCHEMA_VERSION = 1
EXPORT_OWNER_MARKER = ".export-owner.json"

_SAM3_USAGE = (
    "for o in objects: predictor.add_new_points_or_box("
    "inference_state=st, frame_idx=prompt_frame_idx, obj_id=o['obj_id'], "
    "box=torch.tensor([o['box_normalized']], dtype=torch.float32), clear_old_points=True)"
)


def export_owner(ctx, video: VideoFile, annotation_revision: int) -> dict:
    if type(annotation_revision) is not int or annotation_revision < 0:
        raise ValueError("annotation_revision invalida")
    return {
        "schema_version": EXPORT_OWNER_SCHEMA_VERSION,
        "object_id": ctx.object_id,
        "video_id": video.video_id,
        "relpath": video.relpath,
        "annotation_revision": annotation_revision,
    }


def valid_export_owner(value: object) -> dict | None:
    if not isinstance(value, dict):
        return None
    revision = value.get("annotation_revision")
    if (
        value.get("schema_version") != EXPORT_OWNER_SCHEMA_VERSION
        or not isinstance(value.get("object_id"), str)
        or not value["object_id"]
        or not isinstance(value.get("video_id"), str)
        or not value["video_id"]
        or not isinstance(value.get("relpath"), str)
        or not value["relpath"]
        or type(revision) is not int
        or revision < 0
    ):
        return None
    return {
        "schema_version": EXPORT_OWNER_SCHEMA_VERSION,
        "object_id": value["object_id"],
        "video_id": value["video_id"],
        "relpath": value["relpath"],
        "annotation_revision": revision,
    }


def same_export_identity(left: object, right: object) -> bool:
    first = valid_export_owner(left)
    second = valid_export_owner(right)
    if first is None or second is None:
        return False
    return all(
        first[key] == second[key]
        for key in ("schema_version", "object_id", "video_id", "relpath")
    )


def read_export_owner(root: Path) -> dict | None:
    marker = root / EXPORT_OWNER_MARKER
    if root.is_symlink() or marker.is_symlink() or not marker.is_file():
        return None
    try:
        # O marker tem menos de 1 KiB. O limite evita transformar metadado
        # corrompido em leitura arbitrariamente grande no request/worker.
        if marker.stat().st_size > 16_384:
            return None
        return valid_export_owner(json.loads(marker.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None


def write_export_owner(root: Path, owner: dict) -> None:
    normalized = valid_export_owner(owner)
    if normalized is None:
        raise ValueError("owner de export invalido")
    if root.is_symlink() or (root.exists() and not root.is_dir()):
        raise ValueError("raiz de export invalida")
    root.mkdir(parents=True, exist_ok=True)
    marker = root / EXPORT_OWNER_MARKER
    if marker.is_symlink():
        raise ValueError("marker de ownership nao pode ser link simbolico")
    temporary = root / f".{EXPORT_OWNER_MARKER}.{uuid4().hex}.tmp"
    try:
        with temporary.open("xb") as stream:
            stream.write(
                json.dumps(normalized, ensure_ascii=False, sort_keys=True).encode("utf-8")
            )
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, marker)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _safe_recorded_root(ctx, raw: object) -> Path | None:
    if not isinstance(raw, str) or not raw:
        return None
    candidate = Path(raw)
    if candidate.is_symlink():
        return None
    try:
        resolved = candidate.resolve()
        output = ctx.output_root.resolve()
    except (OSError, RuntimeError):
        return None
    if resolved == output or resolved.parent != output:
        return None
    return resolved


def export_root_for(ctx, video: VideoFile) -> Path:
    """Return a stable, video-owned root without reusing ambiguous legacy data."""
    entry = ctx.store.entry(video.relpath) if getattr(ctx, "store", None) else None
    recorded = ((entry or {}).get("export") or {}).get("root")
    previous = _safe_recorded_root(ctx, recorded)
    expected = export_owner(
        ctx, video, int((entry or {}).get("annotation_revision") or 0)
    )
    if previous is not None and same_export_identity(
        read_export_owner(previous), expected
    ):
        return previous

    root = ctx.output_root / export_folder_name(video)
    if root.is_symlink() or (root.exists() and not root.is_dir()):
        raise ValueError("raiz deterministica de export invalida")
    if root.exists():
        actual = read_export_owner(root)
        if not same_export_identity(actual, expected):
            raise ValueError(
                "raiz deterministica ja existe sem ownership verificavel"
            )
    return root


def write_prompt_json(
    segment_dir: Path, video: VideoFile, interval: dict, width: int, height: int
) -> None:
    payload = {
        "schema_version": PROMPT_SCHEMA_VERSION,
        "video": video.abspath.name,
        "video_relpath": video.relpath,
        "segment": interval["segment"],
        "interval_index": interval["index"],
        "source_start_frame": interval["start_frame"],
        "source_end_frame": interval["end_frame"],
        "frame_count": interval["frame_count"],
        "frame_naming": _NAMING_RATIONALE,
        "source_frame_of": "source_frame = source_start_frame + local_index",
        "image_width": width,
        "image_height": height,
        "prompt_frame_idx": interval["prompt_frame"] - interval["start_frame"],
        "prompt_frame_file": f"{interval['prompt_frame'] - interval['start_frame']:06d}.jpg",
        "objects": [
            {
                "obj_id": box["obj_id"],
                "label": box["label"],
                # Já é a lista interna exata para torch.tensor([...]) -> (1, 4).
                "box_normalized": box["normalized"],
                "box_pixel": box["pixel"],
            }
            for box in interval["bboxes"]
        ],
        "flags": interval["flags"],
        "sam3_usage": _SAM3_USAGE,
    }
    (segment_dir / "prompt.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def clean_segments(root: Path, keep: set[str]) -> list[str]:
    """Remove pastas seg_* que não fazem mais parte da anotação.

    Encurtar um intervalo nunca pode deixar frames obsoletos para trás — eles
    entrariam no dataset silenciosamente. Só mexe em `seg_*`; qualquer outro
    arquivo do usuário naquela pasta é preservado.
    """
    if not root.is_dir():
        return []
    removed = []
    for entry in root.iterdir():
        if entry.is_dir() and entry.name.startswith("seg_") and entry.name not in keep:
            shutil.rmtree(entry, ignore_errors=True)
            removed.append(entry.name)
    return removed


def validate_segment_names(names) -> list[str]:
    segments = list(names)
    if len(segments) != len(set(segments)) or any(
        not isinstance(name, str)
        or not name.startswith("seg_")
        or "/" in name
        or "\\" in name
        or Path(name).name != name
        for name in segments
    ):
        raise ValueError("nome de segmento invalido")
    return segments


def publish_staged_export(
    root: Path,
    staging: Path,
    segments: list[str],
    owner: dict,
    trash: Path,
) -> None:
    """Publish a complete export generation, restoring the old one on failure.

    All validation happens before the first rename. The caller provides its
    concurrency fence (the local annotation lock or the PostgreSQL advisory
    lock); this function owns the filesystem transaction only.
    """
    segments = validate_segment_names(segments)
    normalized_owner = valid_export_owner(owner)
    if normalized_owner is None:
        raise ValueError("owner de export invalido")
    if root.is_symlink() or (root.exists() and not root.is_dir()):
        raise ValueError("raiz de export invalida")
    if staging.is_symlink() or not staging.is_dir():
        raise ValueError("staging de export invalido")
    if trash.parent != root or trash.name in {"", ".", ".."}:
        raise ValueError("lixeira de publicacao invalida")
    if trash.is_symlink() or (trash.exists() and not trash.is_dir()):
        raise ValueError("lixeira de publicacao invalida")

    staged: list[Path] = []
    for name in segments:
        candidate = staging / name
        if candidate.is_symlink() or not candidate.is_dir():
            raise ValueError(f"segmento staged invalido: {name}")
        staged.append(candidate)

    old_segments: list[Path] = []
    if root.exists():
        if not same_export_identity(read_export_owner(root), normalized_owner):
            raise ValueError("raiz de export existente sem ownership verificavel")
        # Preflight completo antes de mover o primeiro filho. Isso evita uma
        # publicação parcial se um seg_* malicioso aparecer no fim da listagem.
        for child in sorted(root.iterdir(), key=lambda path: path.name):
            if not child.name.startswith("seg_"):
                continue
            if child.is_symlink():
                raise ValueError("segmento publicado nao pode ser link simbolico")
            if not child.is_dir():
                if child.name in segments:
                    raise ValueError("destino de segmento nao e diretorio")
                continue
            old_segments.append(child)
    if trash.exists():
        for child in old_segments:
            destination = trash / child.name
            if destination.exists() or destination.is_symlink():
                raise ValueError("lixeira de publicacao contem segmento conflitante")

    root_created = False
    trash_created = False
    moved_old: list[str] = []
    published: list[str] = []
    try:
        if not root.exists():
            root.mkdir(parents=True)
            root_created = True
        if not trash.exists():
            trash.mkdir()
            trash_created = True
        for child in old_segments:
            child.replace(trash / child.name)
            moved_old.append(child.name)
        for source in staged:
            source.replace(root / source.name)
            published.append(source.name)
        write_export_owner(root, normalized_owner)
    except Exception as exc:
        rollback_errors: list[Exception] = []
        for name in reversed(published):
            current = root / name
            destination = staging / name
            try:
                if (current.exists() or current.is_symlink()) and not (
                    destination.exists() or destination.is_symlink()
                ):
                    current.replace(destination)
            except Exception as rollback_exc:  # noqa: BLE001
                rollback_errors.append(rollback_exc)
        for name in reversed(moved_old):
            previous = trash / name
            destination = root / name
            try:
                if (previous.exists() or previous.is_symlink()) and not (
                    destination.exists() or destination.is_symlink()
                ):
                    previous.replace(destination)
            except Exception as rollback_exc:  # noqa: BLE001
                rollback_errors.append(rollback_exc)
        if trash_created:
            try:
                trash.rmdir()
            except OSError as rollback_exc:
                rollback_errors.append(rollback_exc)
        if root_created:
            try:
                root.rmdir()
            except OSError as rollback_exc:
                rollback_errors.append(rollback_exc)
        if rollback_errors:
            raise RuntimeError(
                f"publicacao falhou e rollback ficou incompleto: {rollback_errors[0]}"
            ) from exc
        raise


def _remove_generated_tree(path: Path, parent: Path) -> None:
    if path.is_symlink():
        raise ValueError("diretorio temporario nao pode ser link simbolico")
    resolved = path.resolve()
    expected_parent = parent.resolve()
    if resolved.parent != expected_parent:
        raise ValueError("diretorio temporario fora da raiz de export")
    if resolved.exists():
        shutil.rmtree(resolved)


async def export_video(
    ctx, video_id: str, entry: dict, *, client_id: str | None = None, user: str | None = None
) -> Job:
    """Exporta todos os intervalos do vídeo, um job de ffmpeg por segmento."""
    video = ctx.index.get(video_id)
    if video is None:
        raise KeyError(video_id)

    intervals = entry.get("intervals", [])
    media = entry.get("media") or ctx.index.cached_probe(video_id) or {}
    root = export_root_for(ctx, video)

    total = sum(interval["frame_count"] for interval in intervals)
    job = jobs.create(
        "export", ctx.object_id, video_id, total, "exportando frames…",
        client_id=client_id, user=user,
    )

    src = ctx.index.resolve_path(video_id)
    binaries = ffmpeg.resolve()

    expected_revision = int(entry.get("annotation_revision") or 0)
    owner = export_owner(ctx, video, expected_revision)
    staging = root.parent / f".{root.name}.export-{uuid4().hex}.part"
    publish_trash = root / f".replace-{uuid4().hex}.trash"

    async def worker() -> None:
        job.state = "running"
        jobs._publish(job)
        done_frames = 0
        segments: list[str] = []
        try:
            validate_segment_names(interval["segment"] for interval in intervals)
            if staging.exists() or staging.is_symlink():
                raise ValueError("staging local de export ja existe")
            staging.mkdir(parents=True)

            for interval in intervals:
                if job._cancelled:
                    job.state = "cancelled"
                    job.message = "cancelado"
                    return
                segment_dir = staging / interval["segment"]
                segment_dir.mkdir()

                sub = jobs.create(
                    "export", ctx.object_id, video_id, interval["frame_count"], "",
                    client_id=client_id, user=user,
                )
                base = done_frames

                def relay(child_job: Job = sub, offset: int = base) -> None:
                    job.current = offset + child_job.current
                    jobs._publish(job)

                sub._subscribers.append(_RelayQueue(relay))

                await jobs.run(
                    sub,
                    ffmpeg.export_segment_argv(
                        src, segment_dir, interval["start_frame"], interval["end_frame"]
                    ),
                )

                if sub.state != "done":
                    job.state = sub.state
                    job.error = sub.error or "export do segmento falhou"
                    return

                produced = len(list(segment_dir.glob("*.jpg")))
                expected = interval["frame_count"]
                if produced != expected:
                    job.state = "error"
                    job.error = (
                        f"{interval['segment']}: ffmpeg produziu {produced} frames, "
                        f"esperado {expected}"
                    )
                    return

                width, height = _frame_size(segment_dir, media)
                await asyncio.to_thread(
                    write_prompt_json, segment_dir, video, interval, width, height
                )

                segments.append(interval["segment"])
                done_frames += expected
                job.current = done_frames
                jobs._publish(job)

            # O fallback local não compartilha o advisory lock do PostgreSQL.
            # Serializamos a fence com as mutações de annotations.json e só
            # tocamos a geração publicada depois de reler a revisão corrente.
            async with ctx.store._lock:
                latest = ctx.store.entry(video.relpath)
                current_revision = int((latest or {}).get("annotation_revision") or 0)
                if (
                    current_revision != expected_revision
                    or (latest or {}).get("status") == "no_boom"
                    or job._cancelled
                ):
                    job.state = "cancelled"
                    job.message = "export obsoleto; anotacao mudou"
                    return
                selected_root = export_root_for(ctx, video)
                if selected_root.resolve() != root.resolve():
                    raise ValueError("raiz do export mudou antes da publicacao")
                await asyncio.to_thread(
                    publish_staged_export,
                    root,
                    staging,
                    segments,
                    owner,
                    publish_trash,
                )

            if publish_trash.exists():
                try:
                    await asyncio.to_thread(
                        _remove_generated_tree, publish_trash, root
                    )
                except OSError:
                    log.warning(
                        "falha ao remover lixeira de export local", exc_info=True
                    )

            job.state = "done"
            job.result = {
                "root": root.as_posix(),
                "segments": segments,
                "total_frames": done_frames,
                "jpeg_qscale": EXPORT_QSCALE,
                "frame_naming": _NAMING_RATIONALE,
                "ffmpeg_version": binaries.version,
                "annotation_revision": expected_revision,
                "owner": owner,
            }
        except Exception as exc:  # noqa: BLE001
            if job._cancelled:
                job.state = "cancelled"
                job.message = "cancelado"
            else:
                job.state = "error"
                job.error = str(exc)
        finally:
            if staging.exists() or staging.is_symlink():
                try:
                    await asyncio.to_thread(
                        _remove_generated_tree, staging, ctx.output_root
                    )
                except Exception:  # noqa: BLE001
                    log.warning("falha ao remover staging de export local", exc_info=True)
            job.finished_at = iso()
            jobs._publish(job)

    asyncio.create_task(worker())
    return job


def _frame_size(segment_dir: Path, media: dict) -> tuple[int, int]:
    """Dimensões reais do frame exportado.

    Nunca confiar em `ffprobe stream=width,height`: com matriz de rotação de ±90°
    ele reporta as dimensões CODIFICADAS enquanto o frame extraído já vem
    rotacionado — normalizar bbox contra as codificadas dá coordenadas
    transpostas e silenciosamente erradas.
    """
    first = next(iter(sorted(segment_dir.glob("*.jpg"))), None)
    if first is not None:
        try:
            from PIL import Image

            with Image.open(first) as image:
                return image.size
        except Exception:  # noqa: BLE001
            pass
    return int(media.get("width") or 0), int(media.get("height") or 0)


class _RelayQueue:
    """Adaptador que faz o job-pai refletir o progresso do job-filho."""

    def __init__(self, callback) -> None:
        self._callback = callback

    def put_nowait(self, _payload) -> None:
        self._callback()
