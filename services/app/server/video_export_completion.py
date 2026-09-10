"""Authoritative completion of a video's frame export."""

from __future__ import annotations

import asyncio
import hashlib
import json
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

from . import durable_jobs
from . import export as export_module
from .sam3 import queue as sam3_queue
from .videos import iso
from .pipeline_mutation import ProjectionMutation, reserve_mutation


class ExportCompletionConflict(ValueError):
    """The reported export does not match the current annotation/job."""


@dataclass(frozen=True)
class FinalizedVideoExport:
    entry: dict
    digest: str
    replayed: bool
    projection: ProjectionMutation

    @property
    def projection_pending(self) -> bool:
        return self.projection.pending

    @property
    def projection_event_seq(self) -> int | None:
        return self.projection.event_seq


def _conflict(message: str) -> ExportCompletionConflict:
    return ExportCompletionConflict(message)


def _revision(value: object, field: str) -> int:
    if type(value) is not int or value < 0:
        raise _conflict(f"{field} invalida")
    return value


def _normalize_result(result: object) -> dict:
    if not isinstance(result, dict):
        raise _conflict("resultado de export invalido")
    allowed = {
        "root",
        "segments",
        "total_frames",
        "jpeg_qscale",
        "frame_naming",
        "ffmpeg_version",
        "annotation_revision",
        "owner",
    }
    required = {
        "root",
        "segments",
        "total_frames",
        "annotation_revision",
        "owner",
    }
    if not required.issubset(result) or not set(result).issubset(allowed):
        raise _conflict("campos do resultado de export invalidos")

    root = result.get("root")
    if not isinstance(root, str) or not root or len(root) > 4096:
        raise _conflict("raiz do resultado invalida")
    raw_segments = result.get("segments")
    if not isinstance(raw_segments, list) or not 0 < len(raw_segments) <= 10_000:
        raise _conflict("segmentos do resultado invalidos")
    segments: list[str] = []
    for segment in raw_segments:
        if (
            not isinstance(segment, str)
            or not segment.startswith("seg_")
            or len(segment) > 128
            or Path(segment).name != segment
            or "/" in segment
            or "\\" in segment
        ):
            raise _conflict("segmentos do resultado invalidos")
        segments.append(segment)
    if len(set(segments)) != len(segments):
        raise _conflict("segmentos do resultado duplicados")

    total = result.get("total_frames")
    if type(total) is not int or total < 0:
        raise _conflict("total de frames invalido")
    qscale = result.get("jpeg_qscale")
    if qscale is not None and (type(qscale) is not int or not 1 <= qscale <= 31):
        raise _conflict("qualidade JPEG invalida")
    naming = result.get("frame_naming")
    if naming is not None and naming != export_module._NAMING_RATIONALE:
        raise _conflict("convencao de nomes de frame invalida")
    version = result.get("ffmpeg_version")
    if version is not None and (
        not isinstance(version, str) or not version or len(version) > 512
    ):
        raise _conflict("versao do ffmpeg invalida")
    revision = _revision(result.get("annotation_revision"), "revisao do resultado")
    owner = export_module.valid_export_owner(result.get("owner"))
    if owner is None:
        raise _conflict("ownership do resultado invalido")
    normalized = {
        "root": root,
        "segments": segments,
        "total_frames": total,
        "annotation_revision": revision,
        "owner": owner,
    }
    if qscale is not None:
        normalized["jpeg_qscale"] = qscale
    if naming is not None:
        normalized["frame_naming"] = naming
    if version is not None:
        normalized["ffmpeg_version"] = version
    return normalized


def _canonical_digest(
    *, object_id: str, video_id: str, job_id: str, result: dict
) -> str:
    payload = {
        "schema_version": 1,
        "object_id": object_id,
        "video_id": video_id,
        "job_id": job_id,
        "result": result,
    }
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _direct_export_root(output_root: Path, raw: str) -> Path:
    candidate = Path(raw)
    if candidate.is_symlink():
        raise _conflict("raiz do resultado insegura")
    try:
        resolved = candidate.resolve()
        parent = output_root.resolve()
    except (OSError, RuntimeError) as exc:
        raise _conflict("raiz do resultado invalida") from exc
    if resolved == parent or resolved.parent != parent:
        raise _conflict("raiz do resultado fora do objeto")
    return resolved


@asynccontextmanager
async def _video_lock(ctx, video_id: str, *, already_held: bool = False):
    if already_held or not durable_jobs.enabled():
        yield
        return
    async with durable_jobs.video_advisory_lock_async(ctx.object_id, video_id):
        await asyncio.to_thread(ctx.store.load)
        yield


async def finalize_video_export(
    ctx,
    video_id: str,
    result: object,
    user: str | None,
    job_id: str,
    *,
    expected_revision: int | None = None,
    expected_root_basename: str | None = None,
    expected_owner: object | None = None,
    expected_total: int | None = None,
    _video_lock_held: bool = False,
) -> FinalizedVideoExport:
    """Validate, persist and enqueue one export; safe to replay after a crash."""
    video = ctx.index.get(video_id)
    if video is None:
        raise _conflict("video nao encontrado")
    if not isinstance(job_id, str) or not job_id or len(job_id) > 128:
        raise _conflict("job_id invalido")
    normalized = _normalize_result(result)
    revision = normalized["annotation_revision"]
    if expected_revision is not None and revision != _revision(
        expected_revision, "revisao esperada"
    ):
        raise _conflict("resultado pertence a outra revisao")
    if expected_total is not None and normalized["total_frames"] != _revision(
        expected_total, "total esperado"
    ):
        raise _conflict("resultado possui total diferente do job")

    owner = export_module.export_owner(ctx, video, revision)
    if normalized["owner"] != owner:
        raise _conflict("resultado pertence a outro video ou revisao")
    if expected_owner is not None and export_module.valid_export_owner(
        expected_owner
    ) != owner:
        raise _conflict("ownership do job nao corresponde ao resultado")

    root = _direct_export_root(ctx.output_root, normalized["root"])
    if expected_root_basename is not None and root.name != expected_root_basename:
        raise _conflict("raiz do resultado difere da raiz do job")
    try:
        selected_root = export_module.export_root_for(ctx, video).resolve()
    except (OSError, RuntimeError, ValueError) as exc:
        raise _conflict("raiz esperada do export invalida") from exc
    if root != selected_root:
        raise _conflict("raiz do resultado nao corresponde ao video")
    if export_module.read_export_owner(root) != owner:
        raise _conflict("marker de ownership ausente ou divergente")

    digest = _canonical_digest(
        object_id=ctx.object_id, video_id=video_id, job_id=job_id, result=normalized
    )
    actor = user if isinstance(user, str) and user else "system"
    mutation = None

    def reserve():
        return reserve_mutation(ctx, video_id, "video_export_completed", {
            "annotation_revision": revision, "job_id": job_id, "digest": digest,
        })

    async with _video_lock(ctx, video_id, already_held=_video_lock_held):
        # Recompute after cross-process reload while holding the shared fence.
        selected_root = export_module.export_root_for(ctx, video).resolve()
        if selected_root != root or export_module.read_export_owner(root) != owner:
            raise _conflict("ownership mudou durante a conclusao")

        def apply(doc: dict):
            nonlocal mutation
            entry = doc["videos"].get(video.relpath)
            if entry is None:
                raise _conflict("anotacao nao encontrada")
            current_revision = _revision(
                entry.get("annotation_revision", 0), "revisao da anotacao"
            )
            if current_revision != revision or entry.get("status") == "no_boom":
                raise _conflict("resultado pertence a uma anotacao antiga")

            completion = entry.get("export_completion")
            if isinstance(completion, dict):
                if completion.get("annotation_revision") != revision:
                    raise _conflict("conclusao gravada com revisao divergente")
                if completion.get("job_id") != job_id:
                    raise _conflict("a revisao ja foi concluida por outro job")
                if entry.get("status") != "done":
                    raise _conflict("conclusao gravada esta inconsistente")
                if entry.get("export") != normalized:
                    raise _conflict("a revisao ja foi concluida com resultado divergente")
                if (
                    completion.get("job_id") == job_id
                    and completion.get("digest") != digest
                ):
                    raise _conflict("o mesmo job apresentou um resultado divergente")
                mutation = reserve()
                return entry, True

            expected_segments = [
                interval.get("segment") for interval in entry.get("intervals") or []
            ]
            if normalized["segments"] != expected_segments:
                raise _conflict("resultado possui segmentos diferentes da anotacao")
            total = sum(
                int(interval.get("frame_count") or 0)
                for interval in entry.get("intervals") or []
            )
            if normalized["total_frames"] != total:
                raise _conflict("resultado possui total diferente da anotacao")
            for segment in normalized["segments"]:
                segment_dir = root / segment
                if segment_dir.is_symlink() or not segment_dir.is_dir():
                    raise _conflict("resultado possui segmento ausente ou inseguro")

            mutation = reserve()
            entry["export"] = normalized
            entry["exported_at"] = iso()
            entry["status"] = "done"
            entry["updated_at"] = iso()
            entry["exported_by"] = actor
            entry["export_completion"] = {
                "schema_version": 1,
                "job_id": job_id,
                "digest": digest,
                "annotation_revision": revision,
            }
            return entry, False

        entry, replayed = await ctx.store.mutate(apply)

        # This deliberately runs for replays too: persistence and queueing span
        # two stores, so a retry must heal a crash between the two operations.
        sam3_queue.bind(ctx.object_id, ctx.output_root)
        if ctx.config.auto_sam3 and normalized["segments"]:
            await asyncio.to_thread(
                sam3_queue.enqueue,
                ctx.object_id,
                video_id=video_id,
                relpath=video.relpath,
                name=video.name,
                export_root=normalized["root"],
                segments=list(normalized["segments"]),
                user=actor,
                annotation_revision=revision,
            )

    if not _video_lock_held:
        await asyncio.to_thread(mutation.complete)
    return FinalizedVideoExport(entry={**entry, **mutation.flags()}, digest=digest,
                                replayed=replayed, projection=mutation)
