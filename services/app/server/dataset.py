"""Exportação do dataset final: YOLO ou COCO, com recorte por flags.

O que sai daqui é o produto do pipeline inteiro — triagem, SAM3 e revisão
humana. Três decisões merecem explicação, porque são as que separam um dataset
utilizável de um que treina bem e avalia mal:

**O split é por VÍDEO, nunca por frame.** Frames vizinhos do mesmo segmento são
quase idênticos: separá-los entre treino e validação faz a métrica medir
memorização, não generalização. O split é determinístico (hash do nome), então
reexportar dá exatamente o mesmo recorte.

**Snapshots publicados possuem os próprios bytes.** Exportações congeladas
copiam os frames para que uma edição posterior na origem não altere um dataset
já publicado. Exportações legadas sem snapshot ainda tentam hardlink primeiro.

**A revisão humana ganha do SAM3.** É o mesmo merge de server/review.py: onde
houver correção, ela vale; no resto, vale o bruto.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from copy import deepcopy
from contextlib import contextmanager
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path
from typing import Any

from PIL import Image
from pipeline_core.masks import inspect_binary_png
from pipeline_core.review_store import (
    FileMaskReviewStore,
    FrameMaskState,
    MaskInstance,
)
from pipeline_core.sam3_runs import (
    Sam3GenerationError,
    resolve_export_root,
    resolve_segment_dir,
)
from pipeline_core.segmentation import coco_rle, yolo_polygons

from .review import (
    SegmentPaths,
    class_names,
    corners_to_yolo,
    effective_boxes,
    load_review,
)
from .videos import iso

FORMATS = ("yolo", "coco")
TASKS = ("detection", "segmentation")
SNAPSHOT_SCHEMA_VERSION = 1
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class DatasetTargetConflict(RuntimeError):
    """A published/reserved dataset name belongs to another snapshot."""


@dataclass
class Filters:
    """Recorte do dataset. Vazio = sem restrição."""

    # {"tipo_aparicao": ["reflexo"], "dificuldade": ["medio", "dificil"]}
    flags: dict[str, list[str]] = field(default_factory=dict)
    video_ids: list[str] = field(default_factory=list)
    # Só o que passou pela revisão humana.
    reviewed_only: bool = False
    # Frames sem objeto viram exemplos de fundo. Úteis, mas em excesso
    # desbalanceiam — por isso é opção, não padrão.
    include_empty: bool = False


def flags_match(interval_flags: dict, wanted: dict[str, list[str]]) -> bool:
    """Um intervalo casa o filtro se, para CADA grupo pedido, tiver ao menos um
    dos valores. Entre grupos é E; dentro do grupo é OU.

    "dificuldade: médio ou difícil" E "tipo: reflexo" é o recorte que se quer
    normalmente — pedir a interseção entre grupos e a união dentro deles.
    """
    for group, values in wanted.items():
        if not values:
            continue
        actual = interval_flags.get(group)
        if actual is None:
            return False
        if isinstance(actual, str):
            actual = [actual]
        if not set(values) & set(actual):
            return False
    return True


def split_for(video_name: str, val_fraction: float, test_fraction: float = 0.0) -> str:
    digest = hashlib.sha1(video_name.encode("utf-8")).hexdigest()
    bucket = int(digest[:8], 16) / 0xFFFFFFFF
    if bucket < test_fraction:
        return "test"
    if bucket < test_fraction + val_fraction:
        return "val"
    return "train"


@dataclass
class Candidate:
    """Um segmento que entra no dataset."""

    video_id: str
    video_name: str
    relpath: str
    segment: str
    segment_dir: Path
    frame_count: int
    flags: dict
    reviewed: int
    image_width: int
    image_height: int
    labels: dict[int, str]
    has_masks: bool
    # Frozen at selection time.  ``SegmentPaths.out_dir`` follows the mutable
    # video-level current pointer, so resolving it again in the worker could
    # silently export a newer SAM3 generation than the one the user previewed.
    annotation_dir: Path | None = None
    object_id: str = ""
    class_label: str = ""
    snapshot_frames: list[dict] | None = None
    annotation_revision: int = 0
    run_identity: dict = field(default_factory=dict)


@dataclass(frozen=True)
class _FrozenSegmentPaths:
    """Segment paths whose annotation root cannot move during a snapshot."""

    segment_dir: Path
    out_dir: Path
    control_dir: Path

    @property
    def labels_dir(self) -> Path:
        return self.out_dir / "labels"

    @property
    def review_path(self) -> Path:
        return self.out_dir / "review.json"

    @property
    def mask_review_path(self) -> Path:
        return self.out_dir / "mask_review.json"

    @property
    def marker_path(self) -> Path:
        return self.out_dir / "run.json"

    def label_path(self, frame: int) -> Path:
        return self.labels_dir / f"{frame:06d}.txt"

    def frame_path(self, frame: int) -> Path:
        return self.segment_dir / f"{frame:06d}.jpg"


def _freeze_segment_paths(
    segment_dir: Path, annotation_dir: Path | None = None
) -> _FrozenSegmentPaths:
    live = SegmentPaths(segment_dir)
    return _FrozenSegmentPaths(
        segment_dir=segment_dir,
        out_dir=annotation_dir or live.out_dir,
        control_dir=live.control_dir,
    )


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _read_json_payload(
    path: Path, *, required: bool = True
) -> tuple[dict, bytes | None]:
    try:
        payload = path.read_bytes()
    except OSError:
        if required:
            raise ValueError(f"metadado ausente: {path.name}") from None
        return {}, None
    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"metadado invalido: {path.name}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"metadado invalido: {path.name}")
    return value, payload


def _read_json_identity(path: Path, *, required: bool = True) -> tuple[dict, str | None]:
    value, payload = _read_json_payload(path, required=required)
    return value, _sha256_bytes(payload) if payload is not None else None


def _effective_prompt_identity(
    paths: SegmentPaths | _FrozenSegmentPaths,
) -> tuple[str, str | None, str]:
    from pipeline_core.sam3_runs import effective_prompt_override_path

    prompt_path = paths.segment_dir / "prompt.json"
    _, raw_prompt = _read_json_payload(prompt_path)
    assert raw_prompt is not None
    override_path = effective_prompt_override_path(
        paths.segment_dir, migrate_legacy=False
    )
    override, raw_override = (
        _read_json_payload(override_path)
        if override_path is not None
        else ({}, None)
    )
    effective = raw_prompt
    if raw_override is not None and override.get("objects"):
        effective += raw_override
    return (
        _sha256_bytes(raw_prompt),
        _sha256_bytes(raw_override) if raw_override is not None else None,
        _sha256_bytes(effective),
    )


def _read_only_prompt_contract(
    paths: SegmentPaths | _FrozenSegmentPaths,
) -> dict:
    """Read the effective prompt without performing legacy control migration."""

    from pipeline_core.sam3_runs import effective_prompt_override_path

    prompt_path = paths.segment_dir / "prompt.json"
    try:
        raw = json.loads(prompt_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}

    objects = raw.get("objects") or []
    override_path = effective_prompt_override_path(
        paths.segment_dir,
        migrate_legacy=False,
    )
    if override_path is not None:
        try:
            override = json.loads(override_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            override = {}
        if isinstance(override, dict) and isinstance(override.get("objects"), list):
            objects = override["objects"]

    normalized_objects = []
    for position, item in enumerate(objects, start=1):
        if not isinstance(item, dict):
            continue
        box = item.get("box_normalized") or item.get("normalized")
        if not isinstance(box, list) or len(box) != 4:
            continue
        normalized_objects.append(
            {
                "obj_id": int(item.get("obj_id") or position),
                "label": item.get("label") or "",
                "normalized": [float(value) for value in box],
            }
        )

    return {
        "image_width": raw.get("image_width"),
        "image_height": raw.get("image_height"),
        "source_start_frame": raw.get("source_start_frame"),
        "frame_idx": int(raw.get("prompt_frame_idx") or 0),
        "flags": raw.get("flags") or {},
        "label": normalized_objects[0]["label"] if normalized_objects else None,
        "objects": normalized_objects,
    }


def _relative_child(path: Path, root: Path, *, description: str) -> str:
    lexical_root = Path(os.path.abspath(os.fspath(root)))
    lexical = Path(os.path.abspath(os.fspath(path)))
    try:
        relative = lexical.relative_to(lexical_root)
    except ValueError as exc:
        raise ValueError(f"{description} fora da raiz do objeto") from exc
    if not relative.parts:
        raise ValueError(f"{description} invalido")
    current = lexical_root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError(f"{description} nao pode usar link simbolico")
    if lexical.resolve() != path.resolve() or not lexical.resolve().is_relative_to(
        lexical_root.resolve()
    ):
        raise ValueError(f"{description} fora da raiz do objeto")
    return relative.as_posix()


def _resolve_relative_child(root: Path, raw: object, *, description: str) -> Path:
    if not isinstance(raw, str) or not raw or Path(raw).is_absolute():
        raise ValueError(f"{description} invalido")
    target = root / Path(raw)
    _relative_child(target, root, description=description)
    return target


def _snapshot_digest(snapshot: dict) -> str:
    stable = {
        key: value
        for key, value in snapshot.items()
        if key not in {"snapshot_id", "created_at"}
    }
    encoded = json.dumps(
        stable,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return _sha256_bytes(encoded)


def validate_snapshot(snapshot: dict, *, object_id: str | None = None) -> None:
    if not isinstance(snapshot, dict) or snapshot.get("schema_version") != SNAPSHOT_SCHEMA_VERSION:
        raise ValueError("snapshot de dataset invalido")
    snapshot_id = snapshot.get("snapshot_id")
    if not isinstance(snapshot_id, str) or _SHA256.fullmatch(snapshot_id) is None:
        raise ValueError("identidade do snapshot invalida")
    if _snapshot_digest(snapshot) != snapshot_id:
        raise ValueError("digest do snapshot de dataset divergente")
    if object_id is not None and snapshot.get("object_id") != object_id:
        raise ValueError("snapshot pertence a outro objeto")


def bind_export_spec(
    snapshot: dict,
    *,
    fmt: str,
    val_fraction: float,
    test_fraction: float,
) -> dict:
    """Include byte-affecting export options in the immutable reservation id."""
    validate_snapshot(snapshot)
    if fmt not in FORMATS:
        raise ValueError(f"formato desconhecido: {fmt}")
    bound = deepcopy(snapshot)
    bound["export_spec"] = {
        "format": fmt,
        "task": snapshot.get("task"),
        "val_fraction": float(val_fraction),
        "test_fraction": float(test_fraction),
    }
    bound.pop("snapshot_id", None)
    bound["snapshot_id"] = _snapshot_digest(bound)
    return bound


def _snapshot_mask_instances(
    paths: SegmentPaths | _FrozenSegmentPaths,
    *,
    frame: int,
    objects: list[dict],
    checksums: dict[str, str],
    review_entry: dict,
) -> list[dict]:
    if review_entry.get("status") == "edited":
        result: list[dict] = []
        for item in review_entry.get("instances") or []:
            relative = str(item.get("path") or "")
            path = _resolve_relative_child(
                paths.out_dir, relative, description="mascara revisada"
            )
            if not path.is_file():
                raise ValueError(f"mascara revisada ausente: {relative}")
            checksum = str(item.get("sha256") or "")
            if _SHA256.fullmatch(checksum) is None:
                raise ValueError(f"checksum de revisao invalido no frame {frame}")
            result.append(
                {
                    "obj_id": int(item["obj_id"]),
                    "label": str(item.get("label") or ""),
                    "path": relative,
                    "sha256": checksum,
                    "area_pixels": int(item.get("area_pixels") or 0),
                    "bbox_normalized": item.get("bbox_normalized"),
                }
            )
        return sorted(result, key=lambda item: item["obj_id"])

    result = []
    for item in sorted(objects, key=lambda value: int(value["obj_id"])):
        obj_id = int(item["obj_id"])
        relative = f"masks/{obj_id}/{frame:06d}.png"
        checksum = checksums.get(relative)
        if not isinstance(checksum, str) or _SHA256.fullmatch(checksum) is None:
            raise ValueError(f"checksum de mascara ausente: {relative}")
        path = _resolve_relative_child(
            paths.out_dir, relative, description="mascara bruta"
        )
        if not path.is_file():
            raise ValueError(f"mascara bruta ausente: {relative}")
        result.append(
            {
                "obj_id": obj_id,
                "label": str(item.get("label") or ""),
                "path": relative,
                "sha256": checksum,
            }
        )
    return result


def build_snapshot(
    ctx,
    filters: Filters,
    workspace_root: Path | None,
    *,
    task: str = "detection",
) -> dict:
    """Freeze selected metadata without decoding image or mask payloads."""
    if task not in TASKS:
        raise ValueError(f"tarefa desconhecida: {task}")
    names = class_names(workspace_root) if workspace_root else []
    if not names:
        names = [ctx.label]
    candidates = collect(ctx, filters, workspace_root)
    segments: list[dict] = []
    blocking_reasons: list[str] = []
    videos_doc = ctx.store.doc.get("videos") or {}

    for candidate in candidates:
        paths = _freeze_segment_paths(candidate.segment_dir, candidate.annotation_dir)
        entry = videos_doc.get(candidate.relpath) or {}
        prompt_sha, override_sha, effective_prompt_digest = _effective_prompt_identity(paths)
        labels = [
            {"obj_id": obj_id, "label": label}
            for obj_id, label in sorted(candidate.labels.items())
        ]
        frame_records: list[dict] = []
        run_identity: dict[str, Any] = {}
        review_sha: str | None = None

        if candidate.has_masks:
            run, run_sha = _read_json_identity(paths.marker_path)
            review, review_sha = _read_json_identity(paths.mask_review_path, required=False)
            if run.get("status") != "done":
                blocking_reasons.append(
                    f"{candidate.video_name}/{candidate.segment}: run SAM3 incompleto"
                )
            if run.get("prompt_digest") != effective_prompt_digest:
                blocking_reasons.append(
                    f"{candidate.video_name}/{candidate.segment}: digest do prompt divergente"
                )
            artifacts = run.get("artifacts") or {}
            raw_checksums = artifacts.get("checksums") or {}
            checksums = raw_checksums if isinstance(raw_checksums, dict) else {}
            expected_artifacts = {
                f"masks/{int(item['obj_id'])}/{index:06d}.png"
                for item in labels
                for index in range(candidate.frame_count)
            }
            if (
                artifacts.get("format") != "png-1bit-v1"
                or artifacts.get("files") != len(expected_artifacts)
                or not isinstance(raw_checksums, dict)
                or set(checksums) != expected_artifacts
            ):
                blocking_reasons.append(
                    f"{candidate.video_name}/{candidate.segment}: manifesto de artefatos divergente"
                )
            model = run.get("model") or {}
            run_objects = [
                {
                    "obj_id": int(item["obj_id"]),
                    "label": str(item.get("label") or ""),
                    "class_index": item.get("class_index"),
                }
                for item in run.get("objects") or []
                if isinstance(item, dict) and "obj_id" in item
            ]
            if {(item["obj_id"], item["label"]) for item in run_objects} != {
                (item["obj_id"], item["label"]) for item in labels
            }:
                blocking_reasons.append(
                    f"{candidate.video_name}/{candidate.segment}: objetos do run divergem do prompt"
                )
            run_identity = {
                "manifest_sha256": run_sha,
                "runner_version": run.get("runner_version"),
                "started_at": run.get("started_at"),
                "finished_at": run.get("finished_at"),
                "prompt_sha256": prompt_sha,
                "prompt_override_sha256": override_sha,
                "prompt_digest": run.get("prompt_digest"),
                "effective_prompt_digest": effective_prompt_digest,
                "model_id": model.get("model_id"),
                "checkpoint_sha256": model.get("checkpoint_sha256"),
                "sam3_commit": model.get("sam3_commit"),
                "parameters": run.get("params") or {},
                "artifact_format": artifacts.get("format"),
                "artifact_count": artifacts.get("files"),
                "objects": run_objects,
            }
            review_frames = review.get("frames") or {}
            for index in range(candidate.frame_count):
                review_entry = review_frames.get(str(index)) or {}
                try:
                    instances = _snapshot_mask_instances(
                        paths,
                        frame=index,
                        objects=labels,
                        checksums=checksums,
                        review_entry=review_entry,
                    )
                except (KeyError, TypeError, ValueError) as exc:
                    blocking_reasons.append(
                        f"{candidate.video_name}/{candidate.segment}/{index}: {exc}"
                    )
                    instances = []
                if (
                    task == "segmentation"
                    and not instances
                    and review_entry.get("status") != "edited"
                ):
                    blocking_reasons.append(
                        f"{candidate.video_name}/{candidate.segment}/{index}: mascara ausente"
                    )
                label_path = paths.label_path(index)
                positive_hint = False
                try:
                    positive_hint = label_path.stat().st_size > 0
                except OSError:
                    pass
                frame_records.append(
                    {
                        "frame": index,
                        "image": _snapshot_image_identity(paths, index),
                        "status": review_entry.get("status"),
                        "review_revision": int(review_entry.get("revision") or 0),
                        "instances": instances,
                        "has_objects_hint": positive_hint
                        if review_entry.get("status") != "edited"
                        else any(
                            int(item.get("area_pixels") or 0) > 0
                            for item in instances
                        ),
                    }
                )
        else:
            if task == "segmentation":
                blocking_reasons.append(
                    f"{candidate.video_name}/{candidate.segment}: run legado sem mascaras"
                )
            review, review_sha = _read_json_identity(paths.review_path, required=False)
            review.setdefault("frames", {})
            run_identity = {"kind": "legacy_detection", "prompt_sha256": prompt_sha}
            for index in range(candidate.frame_count):
                boxes, status = effective_boxes(paths, index, names, review)
                label_path = paths.label_path(index)
                label_sha = _sha256_file(label_path) if label_path.is_file() else None
                frame_records.append(
                    {
                        "frame": index,
                        "image": _snapshot_image_identity(paths, index),
                        "status": status,
                        "review_revision": int(
                            ((review.get("frames") or {}).get(str(index)) or {}).get("revision")
                            or 0
                        ),
                        "boxes": boxes,
                        "label_sha256": label_sha,
                        "has_objects_hint": bool(boxes),
                    }
                )

        segments.append(
            {
                "object_id": candidate.object_id or ctx.object_id,
                "video_id": candidate.video_id,
                "video_name": candidate.video_name,
                "relpath": candidate.relpath,
                "segment": candidate.segment,
                "segment_path": _relative_child(
                    candidate.segment_dir,
                    ctx.output_root,
                    description="segmento",
                ),
                "annotation_path": _relative_child(
                    paths.out_dir,
                    ctx.output_root,
                    description="raiz da anotacao",
                ),
                "frame_count": candidate.frame_count,
                "flags": candidate.flags,
                "reviewed": candidate.reviewed,
                "image_width": candidate.image_width,
                "image_height": candidate.image_height,
                "labels": {str(key): value for key, value in sorted(candidate.labels.items())},
                "annotation_revision": int(entry.get("annotation_revision") or 0),
                "review_manifest_sha256": review_sha,
                "run": run_identity,
                "frames": frame_records,
            }
        )

    snapshot = {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "created_at": iso(),
        "object_id": ctx.object_id,
        "task": task,
        "classes": names,
        "filters": {
            "flags": filters.flags,
            "video_ids": filters.video_ids,
            "reviewed_only": filters.reviewed_only,
            "include_empty": filters.include_empty,
        },
        "segments": sorted(
            segments,
            key=lambda item: (
                item["object_id"],
                item["video_id"],
                item["relpath"],
                item["segment"],
            ),
        ),
        "export_allowed": not blocking_reasons,
        "blocking_reasons": sorted(set(blocking_reasons))[:100],
    }
    snapshot["snapshot_id"] = _snapshot_digest(snapshot)
    return snapshot


def _snapshot_image_identity(paths: SegmentPaths, frame: int) -> dict:
    path = paths.frame_path(frame)
    try:
        stat = path.stat()
    except OSError as exc:
        raise ValueError(f"frame ausente: {paths.segment_dir.name}/{frame}") from exc
    return {
        "path": _relative_child(path, paths.segment_dir, description="frame"),
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
    }


def collect(ctx, filters: Filters, workspace_root: Path | None) -> list[Candidate]:
    """Segmentos que casam o filtro, olhando o annotations.json e o disco."""
    found: list[Candidate] = []
    wanted_ids = set(filters.video_ids)

    for relpath, entry in (ctx.store.doc.get("videos") or {}).items():
        if entry.get("status") != "done":
            continue
        export = entry.get("export")
        if not export or not export.get("segments"):
            continue
        if wanted_ids and entry.get("video_id") not in wanted_ids:
            continue

        try:
            root = resolve_export_root(ctx.output_root, export["root"])
        except Sam3GenerationError as exc:
            raise ValueError(str(exc)) from exc

        intervals = {i.get("segment"): i for i in entry.get("intervals") or []}

        for segment in export["segments"]:
            interval = intervals.get(segment) or {}
            flags = interval.get("flags") or {}
            if not flags_match(flags, filters.flags):
                continue

            try:
                segment_dir = resolve_segment_dir(root, segment)
            except Sam3GenerationError as exc:
                raise ValueError(str(exc)) from exc
            live_paths = SegmentPaths(segment_dir)
            annotation_dir = live_paths.out_dir
            paths = _freeze_segment_paths(live_paths.segment_dir, annotation_dir)
            has_masks = (paths.out_dir / "masks").is_dir()
            if not paths.labels_dir.is_dir() and not has_masks:
                continue  # o SAM3 ainda não processou este segmento

            review = load_review(paths)
            if has_masks:
                mask_review_path = paths.out_dir / "mask_review.json"
                try:
                    mask_review = json.loads(mask_review_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    mask_review = {}
                reviewed = sum(
                    1
                    for item in (mask_review.get("frames") or {}).values()
                    if item.get("status") in {"ok", "edited"}
                )
            else:
                reviewed = len(review.get("frames") or {})
            frame_count = (
                interval.get("frame_count")
                or len(list(paths.labels_dir.glob("*.txt")))
                or len(list(paths.segment_dir.glob("*.jpg")))
            )
            if filters.reviewed_only and reviewed < frame_count:
                continue

            media = entry.get("media") or {}
            prompt = _read_only_prompt_contract(paths)
            labels = {
                int(item["obj_id"]): item.get("label") or ""
                for item in prompt.get("objects") or []
            }
            found.append(
                Candidate(
                    video_id=entry.get("video_id", ""),
                    video_name=root.name,
                    relpath=relpath,
                    segment=segment,
                    segment_dir=paths.segment_dir,
                    frame_count=frame_count,
                    flags=flags,
                    reviewed=reviewed,
                    image_width=int(prompt.get("image_width") or media.get("width") or 0),
                    image_height=int(prompt.get("image_height") or media.get("height") or 0),
                    labels=labels,
                    has_masks=has_masks,
                    annotation_dir=annotation_dir,
                    object_id=ctx.object_id,
                    class_label=ctx.label,
                )
            )
    return found


def _mask_state(candidate: Candidate, frame: int) -> FrameMaskState:
    if candidate.image_width <= 0 or candidate.image_height <= 0:
        raise ValueError(
            f"dimensoes ausentes em {candidate.video_name}/{candidate.segment}"
        )
    return FileMaskReviewStore(
        candidate.annotation_dir or SegmentPaths(candidate.segment_dir).out_dir,
        image_size=(candidate.image_width, candidate.image_height),
        labels=candidate.labels,
    ).get_frame(frame)


def _mask_boxes(state: FrameMaskState) -> list[dict]:
    return [
        {
            "obj_id": instance.obj_id,
            "label": instance.label,
            "normalized": list(instance.info.bbox_normalized),
        }
        for instance in state.instances
        if instance.info.bbox_normalized is not None
    ]


def preview(
    ctx,
    filters: Filters,
    workspace_root: Path | None,
    *,
    task: str = "detection",
) -> dict:
    """Quanto o filtro seleciona, lendo apenas manifestos e metadados."""
    snapshot = build_snapshot(ctx, filters, workspace_root, task=task)
    frames = with_objects = reviewed = 0
    videos: set[str] = set()
    by_flag: dict[str, dict[str, int]] = {}
    for segment in snapshot["segments"]:
        videos.add(segment["video_id"] or segment["video_name"])
        frames += int(segment["frame_count"])
        reviewed += int(segment["reviewed"])
        with_objects += sum(
            1 for frame in segment["frames"] if frame.get("has_objects_hint")
        )
        for group, value in segment["flags"].items():
            values = [value] if isinstance(value, str) else (value or [])
            bucket = by_flag.setdefault(group, {})
            for item in values:
                bucket[item] = bucket.get(item, 0) + int(segment["frame_count"])

    return {
        "segments": len(snapshot["segments"]),
        "videos": len(videos),
        "frames": frames,
        "frames_with_objects": with_objects,
        "frames_reviewed": reviewed,
        "classes": snapshot["classes"],
        "by_flag": by_flag,
        "task": task,
        "export_allowed": snapshot["export_allowed"],
        "blocking_reasons": snapshot["blocking_reasons"],
    }


def _link_or_copy(source: Path, destination: Path) -> None:
    if destination.exists():
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(source, destination)
    except OSError:
        shutil.copy2(source, destination)


def _materialize_image(
    source: Path,
    destination: Path,
    *,
    immutable: bool,
) -> None:
    """Own snapshot bytes; only legacy live exports may share an inode."""

    if not immutable:
        _link_or_copy(source, destination)
        return
    if destination.exists():
        raise FileExistsError(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)


def _candidates_from_snapshot(ctx, snapshot: dict) -> list[Candidate]:
    candidates: list[Candidate] = []
    for item in snapshot.get("segments") or []:
        segment_dir = _resolve_relative_child(
            ctx.output_root,
            item.get("segment_path"),
            description="segmento do snapshot",
        )
        annotation_dir = _resolve_relative_child(
            ctx.output_root,
            item.get("annotation_path"),
            description="raiz da anotacao do snapshot",
        )
        labels = {
            int(obj_id): str(label)
            for obj_id, label in (item.get("labels") or {}).items()
        }
        frames = item.get("frames")
        if not isinstance(frames, list) or len(frames) != int(item.get("frame_count") or 0):
            raise ValueError("cardinalidade de frames do snapshot divergente")
        candidates.append(
            Candidate(
                video_id=str(item.get("video_id") or ""),
                video_name=str(item.get("video_name") or segment_dir.parent.name),
                relpath=str(item.get("relpath") or ""),
                segment=str(item.get("segment") or segment_dir.name),
                segment_dir=segment_dir,
                frame_count=int(item.get("frame_count") or 0),
                flags=item.get("flags") or {},
                reviewed=int(item.get("reviewed") or 0),
                image_width=int(item.get("image_width") or 0),
                image_height=int(item.get("image_height") or 0),
                labels=labels,
                has_masks=any("instances" in frame for frame in frames),
                annotation_dir=annotation_dir,
                object_id=str(item.get("object_id") or ctx.object_id),
                class_label="",
                snapshot_frames=frames,
                annotation_revision=int(item.get("annotation_revision") or 0),
                run_identity=item.get("run") or {},
            )
        )
    return candidates


def _snapshot_frame(candidate: Candidate, index: int) -> dict:
    if candidate.snapshot_frames is None or index >= len(candidate.snapshot_frames):
        raise ValueError("frame ausente no snapshot")
    frame = candidate.snapshot_frames[index]
    if int(frame.get("frame", -1)) != index:
        raise ValueError("ordem de frames do snapshot divergente")
    return frame


def _snapshot_image(candidate: Candidate, frame: dict) -> Path:
    identity = frame.get("image") or {}
    path = _resolve_relative_child(
        candidate.segment_dir,
        identity.get("path"),
        description="frame do snapshot",
    )
    try:
        stat = path.stat()
    except OSError as exc:
        raise ValueError(f"frame selecionado ausente: {path.name}") from exc
    if stat.st_size != identity.get("size") or stat.st_mtime_ns != identity.get("mtime_ns"):
        raise ValueError(f"frame selecionado mudou apos o snapshot: {path.name}")
    return path


def _snapshot_mask_state(candidate: Candidate, frame: dict) -> FrameMaskState:
    if candidate.image_width <= 0 or candidate.image_height <= 0:
        raise ValueError(
            f"dimensoes ausentes em {candidate.video_name}/{candidate.segment}"
        )
    annotation_dir = candidate.annotation_dir
    if annotation_dir is None:
        raise ValueError("raiz da anotacao ausente no snapshot")
    instances: list[MaskInstance] = []
    for item in frame.get("instances") or []:
        path = _resolve_relative_child(
            annotation_dir,
            item.get("path"),
            description="mascara do snapshot",
        )
        try:
            info = inspect_binary_png(
                path.read_bytes(),
                expected_size=(candidate.image_width, candidate.image_height),
            )
        except OSError as exc:
            raise ValueError(f"mascara selecionada ausente: {path.name}") from exc
        if info.sha256 != item.get("sha256"):
            raise ValueError(f"checksum de mascara divergente: {path.name}")
        instances.append(
            MaskInstance(
                obj_id=int(item["obj_id"]),
                label=str(item.get("label") or ""),
                path=path,
                info=info,
            )
        )
    return FrameMaskState(
        frame=int(frame["frame"]),
        revision=int(frame.get("review_revision") or 0),
        status=frame.get("status"),
        instances=tuple(instances),
    )


def _verified_mask_image(instance: MaskInstance, expected_size: tuple[int, int]) -> Image.Image:
    try:
        payload = instance.path.read_bytes()
    except OSError as exc:
        raise ValueError(f"mascara selecionada ausente: {instance.path.name}") from exc
    info = inspect_binary_png(payload, expected_size=expected_size)
    if info.sha256 != instance.info.sha256:
        raise ValueError(f"checksum de mascara divergente: {instance.path.name}")
    with Image.open(BytesIO(payload)) as image:
        return image.copy()


def export(
    ctx,
    filters: Filters,
    *,
    out_dir: Path,
    fmt: str = "yolo",
    task: str = "detection",
    val_fraction: float = 0.2,
    test_fraction: float = 0.0,
    workspace_root: Path | None = None,
    on_progress=None,
    snapshot: dict | None = None,
) -> dict:
    """Escreve o dataset. Devolve o resumo que vai para o job."""
    if fmt not in FORMATS:
        raise ValueError(f"formato desconhecido: {fmt}")
    if task not in TASKS:
        raise ValueError(f"tarefa desconhecida: {task}")

    if snapshot is not None:
        validate_snapshot(snapshot, object_id=ctx.object_id)
        if snapshot.get("export_allowed") is False:
            raise ValueError("snapshot bloqueado por artefatos incompletos ou invalidos")
        if snapshot.get("task") != task:
            raise ValueError("tarefa diverge do snapshot")
        filters = Filters(**(snapshot.get("filters") or {}))
        names = list(snapshot.get("classes") or [])
    else:
        names = class_names(workspace_root) if workspace_root else []
    if not names:
        names = [ctx.label]
    class_index = {name: index for index, name in enumerate(names)}

    candidates = (
        _candidates_from_snapshot(ctx, snapshot)
        if snapshot is not None
        else collect(ctx, filters, workspace_root)
    )
    if not candidates:
        raise ValueError("nenhum segmento casa com o filtro")
    if task == "segmentation":
        missing = [
            f"{candidate.video_name}/{candidate.segment}"
            for candidate in candidates
            if not candidate.has_masks
        ]
        if missing:
            raise ValueError(
                "exportacao de segmentacao exige mascaras validas: "
                + ", ".join(missing[:3])
            )

    split_keys = {
        f"{candidate.object_id or ctx.object_id}:{candidate.video_id}"
        for candidate in candidates
    }
    splits = {
        key: split_for(key, val_fraction, test_fraction)
        for key in sorted(split_keys)
    }

    out_dir.mkdir(parents=True, exist_ok=True)
    coco: dict[str, dict] = {}
    counts = {"images": 0, "annotations": 0, "empty": 0, "edited": 0}
    per_split: dict[str, int] = {}
    total_frames = sum(c.frame_count for c in candidates) or 1
    seen = 0
    warnings: list[dict] = []

    for candidate in candidates:
        paths = SegmentPaths(candidate.segment_dir)
        review = load_review(paths) if candidate.snapshot_frames is None else {}
        split = splits[f"{candidate.object_id or ctx.object_id}:{candidate.video_id}"]

        for index in range(candidate.frame_count):
            seen += 1
            if on_progress and seen % 25 == 0:
                on_progress(seen, total_frames)

            frozen_frame = (
                _snapshot_frame(candidate, index)
                if candidate.snapshot_frames is not None
                else None
            )
            image_path = (
                _snapshot_image(candidate, frozen_frame)
                if frozen_frame is not None
                else paths.frame_path(index)
            )
            if not image_path.exists():
                continue
            mask_state = (
                _snapshot_mask_state(candidate, frozen_frame)
                if frozen_frame is not None and candidate.has_masks
                else _mask_state(candidate, index)
                if candidate.has_masks
                else None
            )
            if mask_state is not None:
                boxes = _mask_boxes(mask_state)
                status = mask_state.status
                if task == "segmentation" and not mask_state.instances and status != "edited":
                    raise ValueError(
                        f"mascara ausente em {candidate.video_name}/{candidate.segment}/{index}"
                    )
            else:
                if frozen_frame is not None:
                    boxes = list(frozen_frame.get("boxes") or [])
                    status = frozen_frame.get("status")
                else:
                    boxes, status = effective_boxes(paths, index, names, review)
            if status == "edited":
                counts["edited"] += 1
            if not boxes:
                counts["empty"] += 1
                if not filters.include_empty:
                    continue

            stem = (
                f"{candidate.object_id or ctx.object_id}__{candidate.video_id}__"
                f"{candidate.segment}__{index:06d}"
            )
            counts["images"] += 1
            per_split[split] = per_split.get(split, 0) + 1

            if fmt == "yolo":
                _materialize_image(
                    image_path,
                    out_dir / "images" / split / f"{stem}.jpg",
                    immutable=snapshot is not None,
                )
                lines = []
                if task == "segmentation" and mask_state is not None:
                    for instance in mask_state.instances:
                        if instance.info.area_pixels == 0:
                            continue
                        if snapshot is not None:
                            mask = _verified_mask_image(
                                instance,
                                (candidate.image_width, candidate.image_height),
                            )
                            polygonization = yolo_polygons(mask)
                        else:
                            with Image.open(instance.path) as mask:
                                polygonization = yolo_polygons(mask)
                        for warning in polygonization.warnings:
                            warnings.append(
                                {
                                    "video": candidate.relpath,
                                    "segment": candidate.segment,
                                    "frame": index,
                                    "obj_id": instance.obj_id,
                                    "warning": warning,
                                }
                            )
                        index_of = class_index.get(instance.label, 0)
                        for polygon in polygonization.polygons:
                            coordinates = " ".join(
                                f"{coordinate:.6f}"
                                for point in polygon
                                for coordinate in point
                            )
                            lines.append(f"{index_of} {coordinates}")
                            counts["annotations"] += 1
                else:
                    for box in boxes:
                        cx, cy, width, height = corners_to_yolo(box["normalized"])
                        index_of = class_index.get(box.get("label") or "", 0)
                        lines.append(
                            f"{index_of} {cx:.6f} {cy:.6f} {width:.6f} {height:.6f}"
                        )
                        counts["annotations"] += 1
                label_path = out_dir / "labels" / split / f"{stem}.txt"
                label_path.parent.mkdir(parents=True, exist_ok=True)
                label_path.write_text(
                    "\n".join(lines) + ("\n" if lines else ""), encoding="utf-8"
                )
            else:
                _materialize_image(
                    image_path,
                    out_dir / split / f"{stem}.jpg",
                    immutable=snapshot is not None,
                )
                bucket = coco.setdefault(
                    split,
                    {
                        "info": {
                            "description": f"{ctx.object_id} — movies-screening-tool",
                            "date_created": (
                                snapshot.get("created_at") if snapshot is not None else iso()
                            ),
                        },
                        "licenses": [],
                        "images": [],
                        "annotations": [],
                        "categories": [
                            {"id": index + 1, "name": name, "supercategory": "object"}
                            for index, name in enumerate(names)
                        ],
                    },
                )
                image_id = len(bucket["images"]) + 1
                width = candidate.image_width or 0
                height = candidate.image_height or 0
                bucket["images"].append(
                    {
                        "id": image_id,
                        "file_name": f"{stem}.jpg",
                        "width": width,
                        "height": height,
                        # Rastreabilidade até a origem: sem isto, um erro no
                        # dataset não tem como ser levado de volta ao vídeo.
                        "video": candidate.relpath,
                        "segment": candidate.segment,
                        "frame": index,
                        "flags": candidate.flags,
                        "reviewed": status is not None,
                    }
                )
                instances_by_id = (
                    {instance.obj_id: instance for instance in mask_state.instances}
                    if mask_state is not None
                    else {}
                )
                for box in boxes:
                    x1, y1, x2, y2 = box["normalized"]
                    # COCO usa pixels absolutos em [x, y, largura, altura].
                    px = x1 * width
                    py = y1 * height
                    pw = (x2 - x1) * width
                    ph = (y2 - y1) * height
                    annotation = {
                        "id": len(bucket["annotations"]) + 1,
                        "image_id": image_id,
                        "category_id": class_index.get(box.get("label") or "", 0) + 1,
                        "bbox": [round(px, 2), round(py, 2), round(pw, 2), round(ph, 2)],
                        "area": round(pw * ph, 2),
                        "iscrowd": 0,
                    }
                    if task == "segmentation":
                        instance = instances_by_id.get(int(box["obj_id"]))
                        if instance is None:
                            raise ValueError("instancia de mascara ausente durante o export")
                        if snapshot is not None:
                            mask = _verified_mask_image(
                                instance,
                                (candidate.image_width, candidate.image_height),
                            )
                            annotation["segmentation"] = coco_rle(mask)
                        else:
                            with Image.open(instance.path) as mask:
                                annotation["segmentation"] = coco_rle(mask)
                        annotation["area"] = instance.info.area_pixels
                    bucket["annotations"].append(annotation)
                    counts["annotations"] += 1

    if fmt == "yolo":
        lines = [
            "# gerado por movies-screening-tool",
            "path: .",
            *[f"{split}: images/{split}" for split in sorted(per_split)],
            "",
            "names:",
            *[f"  {index}: {name}" for index, name in enumerate(names)],
            "",
        ]
        (out_dir / "data.yaml").write_text("\n".join(lines), encoding="utf-8")
    else:
        for split, bucket in coco.items():
            path = out_dir / f"instances_{split}.json"
            path.write_text(json.dumps(bucket, ensure_ascii=False), encoding="utf-8")

    if snapshot is not None:
        # A seleção usa identidade de metadados para não fazer hash de milhares
        # de JPEGs no request. Revalidamos os mesmos metadados após a geração.
        for candidate in candidates:
            for index in range(candidate.frame_count):
                _snapshot_image(candidate, _snapshot_frame(candidate, index))

    output_artifacts = []
    for path in sorted(out_dir.rglob("*")):
        if path.is_file() and path.name != "dataset_manifest.json":
            output_artifacts.append(
                {
                    "path": path.relative_to(out_dir).as_posix(),
                    "sha256": _sha256_file(path),
                    "byte_size": path.stat().st_size,
                }
            )

    manifest = {
        "schema_version": 2,
        "generated_at": snapshot.get("created_at") if snapshot is not None else iso(),
        "object_id": ctx.object_id,
        "format": fmt,
        "task": task,
        "classes": names,
        "filters": {
            "flags": filters.flags,
            "video_ids": filters.video_ids,
            "reviewed_only": filters.reviewed_only,
            "include_empty": filters.include_empty,
        },
        "split": {
            "by": "video",
            "val_fraction": val_fraction,
            "test_fraction": test_fraction,
            "videos": splits,
        },
        "counts": {**counts, "per_split": per_split},
        "warnings": warnings,
        "snapshot_id": snapshot.get("snapshot_id") if snapshot is not None else None,
        "source_snapshot": snapshot,
        "output_artifacts": output_artifacts,
        "segments": [
            {
                "object_id": c.object_id or ctx.object_id,
                "video_id": c.video_id,
                "video": c.relpath,
                "segment": c.segment,
                "frames": c.frame_count,
                "flags": c.flags,
                "annotation_revision": c.annotation_revision,
                "run": c.run_identity,
            }
            for c in candidates
        ],
    }
    (out_dir / "dataset_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    return {
        "out_dir": out_dir.as_posix(),
        "format": fmt,
        "task": task,
        "segments": len(candidates),
        "videos": len(splits),
        **counts,
        "per_split": per_split,
        "snapshot_id": snapshot.get("snapshot_id") if snapshot is not None else None,
    }


def _safe_dataset_name(name: str) -> str:
    if (
        not isinstance(name, str)
        or not name
        or name in {".", ".."}
        or "/" in name
        or "\\" in name
        or Path(name).is_absolute()
        or Path(name).name != name
    ):
        raise ValueError("nome de dataset invalido")
    return name


def _published_manifest(out_dir: Path, snapshot_id: str) -> dict | None:
    manifest_path = out_dir / "dataset_manifest.json"
    if not manifest_path.is_file():
        return None
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if manifest.get("snapshot_id") != snapshot_id:
        raise DatasetTargetConflict(
            f"dataset '{out_dir.name}' pertence a outro snapshot"
        )
    return manifest


def _validate_published_artifacts(out_dir: Path, manifest: dict) -> None:
    records = manifest.get("output_artifacts")
    if not isinstance(records, list):
        raise ValueError("manifesto publicado sem checksums de artefatos")
    expected: dict[str, dict] = {}
    for record in records:
        if not isinstance(record, dict) or not isinstance(record.get("path"), str):
            raise ValueError("registro de artefato publicado invalido")
        relative = record["path"]
        if relative in expected:
            raise ValueError(f"artefato publicado duplicado: {relative}")
        expected[relative] = record
    actual = {
        path.relative_to(out_dir).as_posix()
        for path in out_dir.rglob("*")
        if path.is_file() and path.name != "dataset_manifest.json"
    }
    if actual != set(expected):
        raise ValueError("cardinalidade de artefato publicado divergente")
    for relative, record in expected.items():
        path = _resolve_relative_child(
            out_dir,
            relative,
            description="artefato publicado",
        )
        if path.stat().st_size != record.get("byte_size"):
            raise ValueError(f"artefato publicado com tamanho divergente: {relative}")
        if _sha256_file(path) != record.get("sha256"):
            raise ValueError(f"artefato publicado com checksum divergente: {relative}")


def _reservation_path(datasets_root: Path, name: str) -> Path:
    reservations = datasets_root / ".reservations"
    if datasets_root.is_symlink() or reservations.is_symlink():
        raise ValueError("raiz de reservas de dataset invalida")
    reservations.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha256(name.encode("utf-8")).hexdigest()
    return reservations / f"{key}.json"


@contextmanager
def _dataset_reservation_lock(datasets_root: Path):
    datasets_root.mkdir(parents=True, exist_ok=True)
    lock_path = datasets_root / ".reservations.lock"
    if datasets_root.is_symlink() or lock_path.is_symlink():
        raise ValueError("raiz de reservas de dataset invalida")
    with lock_path.open("a+b") as handle:
        if os.name == "nt":
            import msvcrt

            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def reserve_dataset_target(datasets_root: Path, name: str, snapshot_id: str) -> Path:
    with _dataset_reservation_lock(datasets_root):
        out_dir, _ = _reserve_dataset_target_locked(datasets_root, name, snapshot_id)
        return out_dir


def _reserve_dataset_target_locked(
    datasets_root: Path, name: str, snapshot_id: str
) -> tuple[Path, bool]:
    """Atomically reserve one human-readable target for one immutable snapshot."""
    name = _safe_dataset_name(name)
    if not isinstance(snapshot_id, str) or _SHA256.fullmatch(snapshot_id) is None:
        raise ValueError("identidade do snapshot invalida")
    datasets_root.mkdir(parents=True, exist_ok=True)
    out_dir = datasets_root / name
    if out_dir.is_symlink():
        raise ValueError("destino de dataset nao pode ser link simbolico")
    if out_dir.exists():
        if _published_manifest(out_dir, snapshot_id) is None:
            raise DatasetTargetConflict(f"ja existe um dataset chamado '{name}'")
        return out_dir, False

    path = _reservation_path(datasets_root, name)
    record = {"name": name, "snapshot_id": snapshot_id}
    encoded = json.dumps(record, sort_keys=True, separators=(",", ":")).encode("utf-8")
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        try:
            current = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise DatasetTargetConflict(f"reserva invalida para '{name}'") from exc
        if current != record:
            raise DatasetTargetConflict(f"dataset '{name}' ja esta reservado")
        return out_dir, False
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        path.unlink(missing_ok=True)
        raise
    return out_dir, True


@contextmanager
def dataset_target_job_reservation(
    datasets_root: Path,
    name: str,
    snapshot_id: str,
):
    """Serialize reservation and durable-job creation, failing closed.

    A database commit can succeed even when its acknowledgement is lost.  In
    that ambiguous state removing the reservation would let a different
    snapshot claim the same target while the first durable job still exists.
    Retry of the same snapshot is idempotent; explicit deletion releases it.
    """

    name = _safe_dataset_name(name)
    with _dataset_reservation_lock(datasets_root):
        out_dir, _ = _reserve_dataset_target_locked(
            datasets_root,
            name,
            snapshot_id,
        )
        yield out_dir


def release_dataset_target(datasets_root: Path, name: str) -> None:
    """Release only the hashed reservation associated with a validated name."""
    name = _safe_dataset_name(name)
    with _dataset_reservation_lock(datasets_root):
        _reservation_path(datasets_root, name).unlink(missing_ok=True)


def _result_from_manifest(out_dir: Path, manifest: dict) -> dict:
    counts = manifest.get("counts") or {}
    videos = (manifest.get("split") or {}).get("videos") or {}
    return {
        "out_dir": out_dir.as_posix(),
        "format": manifest.get("format"),
        "task": manifest.get("task"),
        "segments": len(manifest.get("segments") or []),
        "videos": len(videos),
        "images": int(counts.get("images") or 0),
        "annotations": int(counts.get("annotations") or 0),
        "empty": int(counts.get("empty") or 0),
        "edited": int(counts.get("edited") or 0),
        "per_split": counts.get("per_split") or {},
        "snapshot_id": manifest.get("snapshot_id"),
        "replayed": True,
    }


def export_snapshot_atomic(
    ctx,
    snapshot: dict,
    *,
    out_dir: Path,
    fmt: str,
    task: str,
    val_fraction: float,
    test_fraction: float,
    workspace_root: Path | None,
    owner: str,
    on_progress=None,
    before_publish=None,
) -> dict:
    """Generate in an isolated directory and publish with one directory rename."""
    validate_snapshot(snapshot, object_id=ctx.object_id)
    export_spec = snapshot.get("export_spec")
    if export_spec is not None and export_spec != {
        "format": fmt,
        "task": task,
        "val_fraction": float(val_fraction),
        "test_fraction": float(test_fraction),
    }:
        raise ValueError("parametros de exportacao divergem do snapshot")
    datasets_root = ctx.output_root / "_datasets"
    expected = datasets_root / _safe_dataset_name(out_dir.name)
    if Path(os.path.abspath(os.fspath(out_dir))) != Path(
        os.path.abspath(os.fspath(expected))
    ):
        raise ValueError("destino de dataset fora da raiz")
    reserve_dataset_target(datasets_root, out_dir.name, snapshot["snapshot_id"])
    published = _published_manifest(out_dir, snapshot["snapshot_id"])
    if published is not None:
        _validate_published_artifacts(out_dir, published)
        return _result_from_manifest(out_dir, published)

    staging_root = datasets_root / ".staging"
    if staging_root.is_symlink():
        raise ValueError("staging de dataset nao pode ser link simbolico")
    staging_root.mkdir(parents=True, exist_ok=True)
    if not staging_root.is_dir():
        raise ValueError("staging de dataset invalido")
    owner_key = hashlib.sha256(str(owner).encode("utf-8")).hexdigest()[:16]
    name_key = hashlib.sha256(out_dir.name.encode("utf-8")).hexdigest()[:16]
    staging = staging_root / f"{name_key}-{snapshot['snapshot_id'][:16]}-{owner_key}"
    if staging.exists():
        if staging.is_symlink() or not staging.is_dir():
            raise ValueError("staging de dataset invalido")
        shutil.rmtree(staging)
    staging.mkdir()

    try:
        result = export(
            ctx,
            Filters(**snapshot.get("filters", {})),
            out_dir=staging,
            fmt=fmt,
            task=task,
            val_fraction=val_fraction,
            test_fraction=test_fraction,
            workspace_root=workspace_root,
            on_progress=on_progress,
            snapshot=snapshot,
        )
        manifest = _published_manifest(staging, snapshot["snapshot_id"])
        if manifest is None:
            raise RuntimeError("exportacao nao produziu manifesto completo")
        _validate_published_artifacts(staging, manifest)
        if before_publish is not None:
            before_publish()
        try:
            os.replace(staging, out_dir)
        except OSError:
            concurrent = _published_manifest(out_dir, snapshot["snapshot_id"])
            if concurrent is None:
                raise
            _validate_published_artifacts(out_dir, concurrent)
            shutil.rmtree(staging, ignore_errors=True)
            return _result_from_manifest(out_dir, concurrent)
        result["out_dir"] = out_dir.as_posix()
        return result
    except Exception:
        if staging.exists() and not staging.is_symlink():
            shutil.rmtree(staging, ignore_errors=True)
        raise
