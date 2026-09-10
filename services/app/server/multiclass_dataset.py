"""Composição de datasets globais multiclasse a partir de objetos validados."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


GLOBAL_SNAPSHOT_SCHEMA_VERSION = 1


def _stable_multiclass_snapshot(snapshot: dict) -> dict:
    """Return the source identity without observational capture timestamps."""

    stable = deepcopy(snapshot)
    stable.pop("snapshot_id", None)
    stable.pop("created_at", None)
    for item in stable.get("objects") or []:
        source = item.get("dataset_snapshot") or {}
        source.pop("created_at", None)
    return stable


def _multiclass_snapshot_digest(snapshot: dict) -> str:
    """Hash source identity, excluding observational capture timestamps."""

    stable = _stable_multiclass_snapshot(snapshot)
    payload = json.dumps(
        stable,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True)
class _SnapshotObjectContext:
    object_id: str
    label: str
    output_root: Path


def _snapshot_contexts(snapshot: dict, workspace_root: Path) -> list[_SnapshotObjectContext]:
    from .dataset import _resolve_relative_child

    return [
        _SnapshotObjectContext(
            object_id=item["object_id"],
            label=str(item.get("label") or ""),
            output_root=_resolve_relative_child(
                workspace_root,
                item.get("output_path"),
                description="raiz do objeto no snapshot global",
            ),
        )
        for item in snapshot["objects"]
    ]


def split_key(object_id: str, video_id: str) -> str:
    return f"{object_id}:{video_id}"


def namespaced_stem(object_id: str, video_id: str, segment: str, frame: int) -> str:
    return f"{object_id}__{video_id}__{segment}__{frame:06d}"


def class_map(items: Iterable[tuple[str, str]]) -> dict[str, int]:
    ordered = sorted((object_id, label.strip()) for object_id, label in items)
    seen: dict[str, str] = {}
    result: dict[str, int] = {}
    for index, (object_id, label) in enumerate(ordered):
        normalized = label.casefold()
        if not normalized:
            raise ValueError(f"classe vazia no objeto {object_id}")
        if normalized in seen:
            raise ValueError(
                f"classes duplicadas entre {seen[normalized]} e {object_id}: {label}"
            )
        seen[normalized] = object_id
        result[label] = index
    return result


def class_records(contexts) -> list[dict]:
    class_map((ctx.object_id, ctx.label) for ctx in contexts)
    return [
        {"id": index, "object_id": ctx.object_id, "name": ctx.label}
        for index, ctx in enumerate(sorted(contexts, key=lambda item: item.object_id))
    ]


def _completed_video_ids(ctx) -> set[str]:
    from .pipeline_state import inspect_pipeline_entry
    from .sam3 import queue as sam3_queue

    queue = sam3_queue.map_for(ctx.object_id)
    completed: set[str] = set()
    for relpath, entry in (ctx.store.doc.get("videos") or {}).items():
        snapshot = inspect_pipeline_entry(entry, queue.get(relpath), ctx.output_root)
        if snapshot.stage == "completed" and entry.get("video_id"):
            completed.add(str(entry["video_id"]))
    return completed


def _eligible_ids(ctx, selections: list[dict]) -> list[str]:
    completed = _completed_video_ids(ctx)
    if not selections:
        return sorted(completed)
    selected = {
        str(item.get("video_id"))
        for item in selections
        if item.get("object_id") == ctx.object_id and item.get("video_id")
    }
    return sorted(completed & selected)


def validate_multiclass_snapshot(snapshot: dict) -> None:
    from .dataset import validate_snapshot

    if (
        not isinstance(snapshot, dict)
        or snapshot.get("schema_version") != GLOBAL_SNAPSHOT_SCHEMA_VERSION
        or snapshot.get("scope") != "global"
    ):
        raise ValueError("snapshot global de dataset invalido")
    if _multiclass_snapshot_digest(snapshot) != snapshot.get("snapshot_id"):
        raise ValueError("digest do snapshot global divergente")
    objects = snapshot.get("objects")
    if not isinstance(objects, list):
        raise ValueError("objetos ausentes no snapshot global")
    seen: set[str] = set()
    for item in objects:
        if not isinstance(item, dict) or not isinstance(item.get("object_id"), str):
            raise ValueError("objeto invalido no snapshot global")
        object_id = item["object_id"]
        if object_id in seen:
            raise ValueError(f"objeto duplicado no snapshot global: {object_id}")
        seen.add(object_id)
        validate_snapshot(item.get("dataset_snapshot"), object_id=object_id)


def build_multiclass_snapshot(
    contexts,
    selections: list[dict],
    *,
    flags: dict[str, list[str]],
    include_empty: bool,
    task: str,
    workspace_root: Path | None,
) -> dict:
    """Freeze every per-object source while requests may still read live state."""
    from .dataset import (
        Filters,
        TASKS,
        _relative_child,
        _snapshot_digest as _dataset_snapshot_digest,
        build_snapshot,
    )
    from .videos import iso

    if task not in TASKS:
        raise ValueError(f"tarefa desconhecida: {task}")
    if workspace_root is None:
        raise ValueError("workspace nao configurado")
    records = class_records(contexts)
    normalized_selections = sorted(
        {
            (str(item.get("object_id") or ""), str(item.get("video_id") or ""))
            for item in selections
            if item.get("object_id") and item.get("video_id")
        }
    )
    object_snapshots: list[dict] = []
    blocking: list[str] = []
    totals = {
        "segments": 0,
        "videos": 0,
        "frames": 0,
        "frames_with_objects": 0,
        "frames_reviewed": 0,
    }
    by_flag: dict[str, dict[str, int]] = {}
    by_class: list[dict] = []

    for ctx in sorted(contexts, key=lambda item: item.object_id):
        eligible = _eligible_ids(ctx, selections)
        object_filters = Filters(
            flags=flags,
            video_ids=eligible,
            reviewed_only=True,
            include_empty=include_empty,
        )
        if eligible:
            source = build_snapshot(
                ctx,
                object_filters,
                workspace_root,
                task=task,
            )
        else:
            # ``video_ids=[]`` means all videos to the object exporter. Build
            # an explicit empty snapshot without touching an unselected class.
            source = {
                "schema_version": 1,
                "created_at": iso(),
                "object_id": ctx.object_id,
                "task": task,
                "classes": [item["name"] for item in records],
                "filters": {
                    "flags": flags,
                    "video_ids": [],
                    "reviewed_only": True,
                    "include_empty": include_empty,
                },
                "segments": [],
                "export_allowed": True,
                "blocking_reasons": [],
            }
            source["snapshot_id"] = _dataset_snapshot_digest(source)
        segments = source.get("segments") or []
        frame_count = sum(int(item.get("frame_count") or 0) for item in segments)
        videos = {
            str(item.get("video_id") or item.get("video_name") or "")
            for item in segments
        }
        reviewed = sum(
            1
            for segment in segments
            for frame in segment.get("frames") or []
            if frame.get("status") in {"ok", "edited"}
        )
        with_objects = sum(
            1
            for segment in segments
            for frame in segment.get("frames") or []
            if frame.get("has_objects_hint")
        )
        totals["segments"] += len(segments)
        totals["videos"] += len(videos)
        totals["frames"] += frame_count
        totals["frames_reviewed"] += reviewed
        totals["frames_with_objects"] += with_objects
        by_class.append(
            {
                "object_id": ctx.object_id,
                "name": ctx.label,
                "videos": len(videos),
                "frames": frame_count,
            }
        )
        for segment in segments:
            for group, value in (segment.get("flags") or {}).items():
                values = [value] if isinstance(value, str) else (value or [])
                bucket = by_flag.setdefault(group, {})
                for flag in values:
                    bucket[str(flag)] = bucket.get(str(flag), 0) + int(
                        segment.get("frame_count") or 0
                    )
        blocking.extend(
            f"{ctx.object_id}: {reason}"
            for reason in source.get("blocking_reasons") or []
        )
        object_snapshots.append(
            {
                "object_id": ctx.object_id,
                "label": ctx.label,
                "output_path": _relative_child(
                    ctx.output_root,
                    workspace_root,
                    description="raiz do objeto",
                ),
                "eligible_video_ids": eligible,
                "dataset_snapshot": source,
            }
        )

    if not contexts:
        blocking.append("selecione ao menos um objeto")
    if totals["frames"] == 0:
        blocking.append("nenhum frame concluido casa com os filtros")
    snapshot = {
        "schema_version": GLOBAL_SNAPSHOT_SCHEMA_VERSION,
        "scope": "global",
        "created_at": iso(),
        "task": task,
        "classes": records,
        "filters": {
            "flags": flags,
            "videos": [
                {"object_id": object_id, "video_id": video_id}
                for object_id, video_id in normalized_selections
            ],
            "reviewed_only": True,
            "include_empty": include_empty,
        },
        "objects": object_snapshots,
        "totals": totals,
        "by_class": by_class,
        "by_flag": by_flag,
        "completed_only": True,
        "export_allowed": not blocking,
        "blocking_reasons": sorted(set(blocking))[:100],
    }
    snapshot["snapshot_id"] = _multiclass_snapshot_digest(snapshot)
    return snapshot


def bind_multiclass_export_spec(
    snapshot: dict,
    *,
    fmt: str,
    val_fraction: float,
    test_fraction: float,
) -> dict:
    from .dataset import FORMATS, split_for

    validate_multiclass_snapshot(snapshot)
    if fmt not in FORMATS:
        raise ValueError(f"formato desconhecido: {fmt}")
    bound = deepcopy(snapshot)
    keys = {
        split_key(item["object_id"], str(segment.get("video_id") or ""))
        for item in bound["objects"]
        for segment in item["dataset_snapshot"].get("segments") or []
    }
    bound["export_spec"] = {
        "format": fmt,
        "task": bound.get("task"),
        "split": {
            "by": "object_id+video_id",
            "val_fraction": float(val_fraction),
            "test_fraction": float(test_fraction),
            "videos": {
                key: split_for(key, float(val_fraction), float(test_fraction))
                for key in sorted(keys)
            },
        },
    }
    bound.pop("snapshot_id", None)
    bound["snapshot_id"] = _multiclass_snapshot_digest(bound)
    return bound


def preview_multiclass(
    contexts,
    selections: list[dict],
    *,
    flags: dict[str, list[str]],
    include_empty: bool,
    task: str,
    workspace_root: Path | None,
) -> dict:
    snapshot = build_multiclass_snapshot(
        contexts,
        selections,
        flags=flags,
        include_empty=include_empty,
        task=task,
        workspace_root=workspace_root,
    )
    return {
        **snapshot["totals"],
        "classes": snapshot["classes"],
        "by_class": snapshot["by_class"],
        "by_flag": snapshot["by_flag"],
        "task": snapshot["task"],
        "completed_only": snapshot["completed_only"],
        "export_allowed": snapshot["export_allowed"],
        "blocking_reasons": snapshot["blocking_reasons"],
        "snapshot_id": snapshot["snapshot_id"],
    }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _copy_published_artifact(source: Path, destination: Path) -> None:
    """Materialize output bytes so later source edits cannot mutate a dataset."""

    if destination.exists():
        raise FileExistsError(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)


def _merge_yolo(
    parts: list[tuple[object, Path, dict]],
    out_dir: Path,
    records: list[dict],
) -> tuple[dict, list[dict]]:
    counts = {"images": 0, "annotations": 0, "empty": 0, "edited": 0}
    warnings: list[dict] = []
    splits: set[str] = set()
    class_by_object = {item["object_id"]: item["id"] for item in records}
    for ctx, part, manifest in parts:
        class_id = class_by_object[ctx.object_id]
        warnings.extend(
            {"object_id": ctx.object_id, **warning}
            for warning in manifest.get("warnings") or []
        )
        split_dirs = (
            sorted((part / "images").iterdir())
            if (part / "images").is_dir()
            else []
        )
        for split_dir in split_dirs:
            if not split_dir.is_dir():
                continue
            split = split_dir.name
            splits.add(split)
            for image in sorted(split_dir.glob("*.jpg")):
                destination = out_dir / "images" / split / image.name
                _copy_published_artifact(image, destination)
                source_label = part / "labels" / split / f"{image.stem}.txt"
                target_label = out_dir / "labels" / split / f"{image.stem}.txt"
                target_label.parent.mkdir(parents=True, exist_ok=True)
                lines = []
                if source_label.is_file():
                    for raw in source_label.read_text(encoding="utf-8").splitlines():
                        fields = raw.split()
                        if fields:
                            lines.append(" ".join([str(class_id), *fields[1:]]))
                target_label.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
                counts["images"] += 1
                counts["annotations"] += len(lines)
                if not lines:
                    counts["empty"] += 1
    yaml = [
        "# gerado por boom-pipeline — dataset global multiclasse",
        "path: .",
        *[f"{split}: images/{split}" for split in sorted(splits)],
        "",
        "names:",
        *[f"  {item['id']}: {item['name']}" for item in records],
        "",
    ]
    (out_dir / "data.yaml").write_text("\n".join(yaml), encoding="utf-8")
    return counts, warnings


def _merge_coco(
    parts: list[tuple[object, Path, dict]],
    out_dir: Path,
    records: list[dict],
    *,
    generated_at: str,
) -> tuple[dict, list[dict]]:
    buckets: dict[str, dict] = {}
    counts = {"images": 0, "annotations": 0, "empty": 0, "edited": 0}
    warnings: list[dict] = []
    category_by_object = {item["object_id"]: item["id"] + 1 for item in records}
    categories = [
        {"id": item["id"] + 1, "name": item["name"], "supercategory": "object", "object_id": item["object_id"]}
        for item in records
    ]
    for ctx, part, manifest in parts:
        warnings.extend(
            {"object_id": ctx.object_id, **warning}
            for warning in manifest.get("warnings") or []
        )
        for document_path in sorted(part.glob("instances_*.json")):
            split = document_path.stem.removeprefix("instances_")
            document = json.loads(document_path.read_text(encoding="utf-8"))
            bucket = buckets.setdefault(
                split,
                {
                    "info": {
                        "description": "boom-pipeline global multiclass",
                        "date_created": generated_at,
                    },
                    "licenses": [],
                    "images": [],
                    "annotations": [],
                    "categories": categories,
                },
            )
            image_ids: dict[int, int] = {}
            for image in document.get("images") or []:
                source = part / split / image["file_name"]
                _copy_published_artifact(
                    source,
                    out_dir / split / image["file_name"],
                )
                image_id = len(bucket["images"]) + 1
                image_ids[int(image["id"])] = image_id
                bucket["images"].append({**image, "id": image_id, "object_id": ctx.object_id})
                counts["images"] += 1
            annotated_images: set[int] = set()
            for annotation in document.get("annotations") or []:
                image_id = image_ids[int(annotation["image_id"])]
                annotated_images.add(image_id)
                bucket["annotations"].append(
                    {
                        **annotation,
                        "id": len(bucket["annotations"]) + 1,
                        "image_id": image_id,
                        "category_id": category_by_object[ctx.object_id],
                    }
                )
                counts["annotations"] += 1
            counts["empty"] += len(image_ids) - len(annotated_images)
    for split, document in sorted(buckets.items()):
        (out_dir / f"instances_{split}.json").write_text(
            json.dumps(document, ensure_ascii=False), encoding="utf-8"
        )
    return counts, warnings


def export_multiclass(
    contexts,
    selections: list[dict],
    *,
    flags: dict[str, list[str]],
    include_empty: bool,
    out_dir: Path,
    fmt: str,
    task: str,
    val_fraction: float,
    test_fraction: float,
    workspace_root: Path,
    on_progress=None,
    snapshot: dict | None = None,
) -> dict:
    from .dataset import FORMATS, TASKS, Filters, export
    from .videos import iso

    if fmt not in FORMATS or task not in TASKS:
        raise ValueError("formato ou tarefa desconhecida")
    sources: dict[str, dict] = {}
    if snapshot is not None:
        validate_multiclass_snapshot(snapshot)
        if snapshot.get("export_allowed") is False:
            raise ValueError("snapshot global bloqueado")
        if snapshot.get("task") != task:
            raise ValueError("tarefa diverge do snapshot global")
        spec = snapshot.get("export_spec") or {}
        split = spec.get("split") or {}
        if spec and (
            spec.get("format") != fmt
            or spec.get("task") != task
            or split.get("val_fraction") != float(val_fraction)
            or split.get("test_fraction") != float(test_fraction)
        ):
            raise ValueError("parametros divergem do snapshot global")
        contexts = _snapshot_contexts(snapshot, workspace_root)
        records = deepcopy(snapshot.get("classes") or [])
        sources = {
            item["object_id"]: item["dataset_snapshot"]
            for item in snapshot["objects"]
        }
        frozen_filters = snapshot.get("filters") or {}
        flags = frozen_filters.get("flags") or {}
        selections = frozen_filters.get("videos") or []
        include_empty = bool(frozen_filters.get("include_empty"))
    else:
        records = class_records(contexts)
    out_dir.mkdir(parents=True, exist_ok=False)
    parts_root = out_dir / ".parts"
    parts_root.mkdir()
    parts: list[tuple[object, Path, dict]] = []
    total_offset = 0
    if snapshot is None:
        snapshot_frame_counts = {}
    else:
        snapshot_frame_counts = {
            item["object_id"]: sum(
                int(segment.get("frame_count") or 0)
                for segment in item["dataset_snapshot"].get("segments") or []
            )
            for item in snapshot.get("objects") or []
        }
    snapshot_total_frames = sum(snapshot_frame_counts.values())
    try:
        for ctx in sorted(contexts, key=lambda item: item.object_id):
            source_snapshot = (
                sources.get(ctx.object_id) if snapshot is not None else None
            )
            if source_snapshot is not None:
                if not source_snapshot.get("segments"):
                    continue
                eligible = list(
                    next(
                        item.get("eligible_video_ids") or []
                        for item in snapshot["objects"]
                        if item["object_id"] == ctx.object_id
                    )
                )
            else:
                eligible = _eligible_ids(ctx, selections)
                if not eligible:
                    continue
            part = parts_root / ctx.object_id

            def progress(current: int, total: int, *, offset=total_offset) -> None:
                if on_progress:
                    if snapshot is not None:
                        on_progress(
                            min(offset + current, snapshot_total_frames),
                            snapshot_total_frames,
                        )
                    else:
                        on_progress(offset + current, offset + total)

            try:
                result = export(
                    ctx,
                    Filters(
                        flags=flags,
                        video_ids=eligible,
                        reviewed_only=True,
                        include_empty=include_empty,
                    ),
                    out_dir=part,
                    fmt=fmt,
                    task=task,
                    val_fraction=val_fraction,
                    test_fraction=test_fraction,
                    workspace_root=workspace_root,
                    on_progress=progress,
                    snapshot=source_snapshot,
                )
            except ValueError as exc:
                if snapshot is None and "nenhum segmento" in str(exc):
                    continue
                raise
            manifest = json.loads(
                (part / "dataset_manifest.json").read_text(encoding="utf-8")
            )
            parts.append((ctx, part, manifest))
            total_offset += (
                snapshot_frame_counts.get(ctx.object_id, 0)
                if snapshot is not None
                else int(result.get("images") or 0)
            )
            if on_progress and snapshot is not None:
                on_progress(total_offset, snapshot_total_frames)
        if not parts:
            raise ValueError("nenhum vídeo concluído casa com os filtros")

        generated_at = snapshot.get("created_at") if snapshot is not None else iso()
        if fmt == "yolo":
            counts, warnings = _merge_yolo(parts, out_dir, records)
        else:
            counts, warnings = _merge_coco(
                parts,
                out_dir,
                records,
                generated_at=generated_at,
            )

        split_videos: dict[str, str] = {}
        segments: list[dict] = []
        for ctx, _, manifest in parts:
            split_videos.update(manifest.get("split", {}).get("videos") or {})
            segments.extend(
                {"object_id": ctx.object_id, **segment}
                for segment in manifest.get("segments") or []
            )
        if snapshot is not None and snapshot.get("export_spec"):
            expected_splits = snapshot["export_spec"]["split"]["videos"]
            if split_videos != expected_splits:
                raise ValueError("splits gerados divergem do snapshot global")
        shutil.rmtree(parts_root)
        output_artifacts = [
            {
                "path": path.relative_to(out_dir).as_posix(),
                "sha256": _sha256(path),
                "byte_size": path.stat().st_size,
            }
            for path in sorted(out_dir.rglob("*"))
            if path.is_file() and path.name != "dataset_manifest.json"
        ]
        checksums = {
            item["path"]: item["sha256"] for item in output_artifacts
        }
        per_split: dict[str, int] = {}
        for path in (out_dir / "images").glob("*/*.jpg") if fmt == "yolo" else out_dir.glob("*/*.jpg"):
            per_split[path.parent.name] = per_split.get(path.parent.name, 0) + 1
        manifest = {
            "schema_version": 2,
            "scope": "global",
            "generated_at": generated_at,
            "format": fmt,
            "task": task,
            "classes": records,
            "filters": {
                "flags": flags,
                "videos": selections,
                "reviewed_only": True,
                "include_empty": include_empty,
            },
            "split": {
                "by": "object_id+video_id",
                "val_fraction": val_fraction,
                "test_fraction": test_fraction,
                "videos": split_videos,
            },
            "counts": {**counts, "per_split": per_split},
            "warnings": warnings,
            "segments": segments,
            "checksums": checksums,
            "snapshot_id": snapshot.get("snapshot_id") if snapshot is not None else None,
            "source_snapshot": snapshot,
            "output_artifacts": output_artifacts,
        }
        (out_dir / "dataset_manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return {
            "out_dir": out_dir.as_posix(),
            "format": fmt,
            "task": task,
            "segments": len(segments),
            "videos": len(split_videos),
            **counts,
            "per_split": per_split,
            "snapshot_id": snapshot.get("snapshot_id") if snapshot is not None else None,
        }
    except Exception:
        if parts_root.exists():
            shutil.rmtree(parts_root, ignore_errors=True)
        raise


def export_multiclass_snapshot_atomic(
    workspace_root: Path,
    snapshot: dict,
    *,
    out_dir: Path,
    fmt: str,
    task: str,
    val_fraction: float,
    test_fraction: float,
    owner: str,
    on_progress=None,
    before_publish=None,
) -> dict:
    """Generate one frozen global dataset and publish it with one rename."""
    from .dataset import (
        _published_manifest,
        _result_from_manifest,
        _safe_dataset_name,
        _validate_published_artifacts,
        reserve_dataset_target,
    )

    validate_multiclass_snapshot(snapshot)
    spec = snapshot.get("export_spec") or {}
    split = spec.get("split") or {}
    if (
        spec.get("format") != fmt
        or spec.get("task") != task
        or split.get("val_fraction") != float(val_fraction)
        or split.get("test_fraction") != float(test_fraction)
    ):
        raise ValueError("parametros divergem do snapshot global")

    def validate_manifest(manifest: dict) -> None:
        if manifest.get("scope") != "global":
            raise ValueError("escopo do manifesto global divergente")
        if manifest.get("format") != fmt or manifest.get("task") != task:
            raise ValueError("formato ou tarefa do manifesto global divergente")
        if manifest.get("classes") != snapshot.get("classes"):
            raise ValueError("classes do manifesto global divergentes")
        if manifest.get("split") != split:
            raise ValueError("splits do manifesto global divergentes")
        source_snapshot = manifest.get("source_snapshot")
        try:
            validate_multiclass_snapshot(source_snapshot)
        except ValueError as exc:
            raise ValueError("snapshot fonte do manifesto global divergente") from exc
        if (
            source_snapshot.get("snapshot_id") != snapshot.get("snapshot_id")
            or _stable_multiclass_snapshot(source_snapshot)
            != _stable_multiclass_snapshot(snapshot)
        ):
            raise ValueError("snapshot fonte do manifesto global divergente")

    datasets_root = workspace_root / "_datasets"
    expected = datasets_root / _safe_dataset_name(out_dir.name)
    if Path(os.path.abspath(os.fspath(out_dir))) != Path(
        os.path.abspath(os.fspath(expected))
    ):
        raise ValueError("destino global de dataset fora da raiz")

    reserve_dataset_target(datasets_root, out_dir.name, snapshot["snapshot_id"])
    published = _published_manifest(out_dir, snapshot["snapshot_id"])
    if published is not None:
        validate_manifest(published)
        _validate_published_artifacts(out_dir, published)
        return _result_from_manifest(out_dir, published)

    staging_root = datasets_root / ".staging"
    if staging_root.is_symlink():
        raise ValueError("staging global de dataset nao pode ser link simbolico")
    staging_root.mkdir(parents=True, exist_ok=True)
    if not staging_root.is_dir():
        raise ValueError("staging global de dataset invalido")
    owner_key = hashlib.sha256(str(owner).encode("utf-8")).hexdigest()[:16]
    job_id = str(owner).split(":", 1)[0]
    job_key = hashlib.sha256(job_id.encode("utf-8")).hexdigest()[:16]
    name_key = hashlib.sha256(out_dir.name.encode("utf-8")).hexdigest()[:16]
    attempt_prefix = f"{name_key}-{snapshot['snapshot_id'][:16]}-{job_key}-"
    staging = staging_root / f"{attempt_prefix}{owner_key}"
    for previous in list(staging_root.iterdir()):
        if previous == staging or not previous.name.startswith(attempt_prefix):
            continue
        if previous.is_symlink() or not previous.is_dir():
            raise ValueError("staging global de dataset invalido")
        shutil.rmtree(previous)
    if staging.exists():
        if staging.is_symlink() or not staging.is_dir():
            raise ValueError("staging global de dataset invalido")
        shutil.rmtree(staging)

    try:
        result = export_multiclass(
            [],
            [],
            flags={},
            include_empty=False,
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
            raise RuntimeError("exportacao global nao produziu manifesto completo")
        validate_manifest(manifest)
        _validate_published_artifacts(staging, manifest)
        if before_publish is not None:
            before_publish()
        try:
            os.replace(staging, out_dir)
        except OSError:
            concurrent = _published_manifest(out_dir, snapshot["snapshot_id"])
            if concurrent is None:
                raise
            validate_manifest(concurrent)
            _validate_published_artifacts(out_dir, concurrent)
            shutil.rmtree(staging, ignore_errors=True)
            return _result_from_manifest(out_dir, concurrent)
        result["out_dir"] = out_dir.as_posix()
        return result
    except Exception:
        if staging.exists() and not staging.is_symlink():
            shutil.rmtree(staging, ignore_errors=True)
        raise
