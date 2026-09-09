"""Export dos segmentos: os frames e o prompt.json que alimentam o SAM3.

Esta é a fronteira do contrato com o pipeline em ../sam3-annotation-tool.
Ver `_NAMING_RATIONALE` antes de mudar a numeração dos arquivos.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
from pathlib import Path

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

_SAM3_USAGE = (
    "for o in objects: predictor.add_new_points_or_box("
    "inference_state=st, frame_idx=prompt_frame_idx, obj_id=o['obj_id'], "
    "box=torch.tensor([o['box_normalized']], dtype=torch.float32), clear_old_points=True)"
)


def export_root_for(ctx, video: VideoFile) -> Path:
    output_root = ctx.output_root
    taken = {
        entry.name
        for entry in output_root.iterdir()
        if entry.is_dir() and not entry.name.startswith("_")
    } if output_root.exists() else set()
    taken.discard(export_folder_name(video))
    return output_root / export_folder_name(video, taken)


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
    root.mkdir(parents=True, exist_ok=True)

    total = sum(interval["frame_count"] for interval in intervals)
    job = jobs.create(
        "export", ctx.object_id, video_id, total, "exportando frames…",
        client_id=client_id, user=user,
    )

    src = ctx.index.resolve_path(video_id)
    binaries = ffmpeg.resolve()

    async def worker() -> None:
        job.state = "running"
        jobs._publish(job)

        keep = {interval["segment"] for interval in intervals}
        await asyncio.to_thread(clean_segments, root, keep)

        done_frames = 0
        segments: list[str] = []

        for interval in intervals:
            segment_dir = root / interval["segment"]
            if segment_dir.exists():
                await asyncio.to_thread(shutil.rmtree, segment_dir, True)
            segment_dir.mkdir(parents=True, exist_ok=True)

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
                job.finished_at = iso()
                jobs._publish(job)
                return

            produced = len(list(segment_dir.glob("*.jpg")))
            expected = interval["frame_count"]
            if produced != expected:
                job.state = "error"
                job.error = (
                    f"{interval['segment']}: ffmpeg produziu {produced} frames, "
                    f"esperado {expected}"
                )
                job.finished_at = iso()
                jobs._publish(job)
                return

            width, height = _frame_size(segment_dir, media)
            await asyncio.to_thread(
                write_prompt_json, segment_dir, video, interval, width, height
            )

            segments.append(interval["segment"])
            done_frames += expected
            job.current = done_frames
            jobs._publish(job)

        job.state = "done"
        job.result = {
            "root": root.as_posix(),
            "segments": segments,
            "total_frames": done_frames,
            "jpeg_qscale": EXPORT_QSCALE,
            "frame_naming": _NAMING_RATIONALE,
            "ffmpeg_version": binaries.version,
            "annotation_revision": int(entry.get("annotation_revision") or 0),
        }
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
