"""Composição de datasets globais multiclasse a partir de objetos validados."""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from typing import Iterable


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


def preview_multiclass(
    contexts,
    selections: list[dict],
    *,
    flags: dict[str, list[str]],
    include_empty: bool,
    task: str,
    workspace_root: Path | None,
) -> dict:
    from .dataset import Filters, preview

    records = class_records(contexts)
    totals = {
        "segments": 0,
        "videos": 0,
        "frames": 0,
        "frames_with_objects": 0,
        "frames_reviewed": 0,
    }
    by_flag: dict[str, dict[str, int]] = {}
    by_class: list[dict] = []
    blocking: list[str] = []
    for ctx in sorted(contexts, key=lambda item: item.object_id):
        eligible = _eligible_ids(ctx, selections)
        if not eligible:
            by_class.append({"object_id": ctx.object_id, "name": ctx.label, "videos": 0, "frames": 0})
            continue
        result = preview(
            ctx,
            Filters(
                flags=flags,
                video_ids=eligible,
                reviewed_only=True,
                include_empty=include_empty,
            ),
            workspace_root,
            task=task,
        )
        for key in totals:
            totals[key] += int(result[key])
        by_class.append(
            {
                "object_id": ctx.object_id,
                "name": ctx.label,
                "videos": int(result["videos"]),
                "frames": int(result["frames"]),
            }
        )
        blocking.extend(
            f"{ctx.object_id}: {reason}" for reason in result.get("blocking_reasons") or []
        )
        for group, values in (result.get("by_flag") or {}).items():
            bucket = by_flag.setdefault(group, {})
            for value, count in values.items():
                bucket[value] = bucket.get(value, 0) + int(count)

    if not contexts:
        blocking.append("selecione ao menos um objeto")
    if totals["frames"] == 0:
        blocking.append("nenhum frame concluído casa com os filtros")
    return {
        **totals,
        "classes": records,
        "by_class": by_class,
        "by_flag": by_flag,
        "task": task,
        "completed_only": True,
        "export_allowed": not blocking,
        "blocking_reasons": blocking[:100],
    }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _merge_yolo(parts: list[tuple[object, Path, dict]], out_dir: Path, records: list[dict]) -> tuple[dict, list[dict]]:
    from .dataset import _link_or_copy

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
        for split_dir in (part / "images").iterdir() if (part / "images").is_dir() else []:
            if not split_dir.is_dir():
                continue
            split = split_dir.name
            splits.add(split)
            for image in split_dir.glob("*.jpg"):
                destination = out_dir / "images" / split / image.name
                _link_or_copy(image, destination)
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


def _merge_coco(parts: list[tuple[object, Path, dict]], out_dir: Path, records: list[dict]) -> tuple[dict, list[dict]]:
    from .dataset import _link_or_copy
    from .videos import iso

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
        for document_path in part.glob("instances_*.json"):
            split = document_path.stem.removeprefix("instances_")
            document = json.loads(document_path.read_text(encoding="utf-8"))
            bucket = buckets.setdefault(
                split,
                {
                    "info": {"description": "boom-pipeline global multiclass", "date_created": iso()},
                    "licenses": [],
                    "images": [],
                    "annotations": [],
                    "categories": categories,
                },
            )
            image_ids: dict[int, int] = {}
            for image in document.get("images") or []:
                source = part / split / image["file_name"]
                _link_or_copy(source, out_dir / split / image["file_name"])
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
    for split, document in buckets.items():
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
) -> dict:
    from .dataset import FORMATS, TASKS, Filters, export
    from .videos import iso

    if fmt not in FORMATS or task not in TASKS:
        raise ValueError("formato ou tarefa desconhecida")
    records = class_records(contexts)
    out_dir.mkdir(parents=True, exist_ok=False)
    parts_root = out_dir / ".parts"
    parts_root.mkdir()
    parts: list[tuple[object, Path, dict]] = []
    total_offset = 0
    try:
        for ctx in sorted(contexts, key=lambda item: item.object_id):
            eligible = _eligible_ids(ctx, selections)
            if not eligible:
                continue
            part = parts_root / ctx.object_id

            def progress(current: int, total: int, *, offset=total_offset) -> None:
                if on_progress:
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
                )
            except ValueError as exc:
                if "nenhum segmento" in str(exc):
                    continue
                raise
            manifest = json.loads((part / "dataset_manifest.json").read_text(encoding="utf-8"))
            parts.append((ctx, part, manifest))
            total_offset += int(result.get("images") or 0)
        if not parts:
            raise ValueError("nenhum vídeo concluído casa com os filtros")

        if fmt == "yolo":
            counts, warnings = _merge_yolo(parts, out_dir, records)
        else:
            counts, warnings = _merge_coco(parts, out_dir, records)

        split_videos: dict[str, str] = {}
        segments: list[dict] = []
        for ctx, _, manifest in parts:
            split_videos.update(manifest.get("split", {}).get("videos") or {})
            segments.extend(
                {"object_id": ctx.object_id, **segment}
                for segment in manifest.get("segments") or []
            )
        shutil.rmtree(parts_root)
        checksums = {
            path.relative_to(out_dir).as_posix(): _sha256(path)
            for path in out_dir.rglob("*")
            if path.is_file()
        }
        per_split: dict[str, int] = {}
        for path in (out_dir / "images").glob("*/*.jpg") if fmt == "yolo" else out_dir.glob("*/*.jpg"):
            per_split[path.parent.name] = per_split.get(path.parent.name, 0) + 1
        manifest = {
            "schema_version": 2,
            "scope": "global",
            "generated_at": iso(),
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
        }
    except Exception:
        if parts_root.exists():
            shutil.rmtree(parts_root, ignore_errors=True)
        raise
