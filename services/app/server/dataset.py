"""Exportação do dataset final: YOLO ou COCO, com recorte por flags.

O que sai daqui é o produto do pipeline inteiro — triagem, SAM3 e revisão
humana. Três decisões merecem explicação, porque são as que separam um dataset
utilizável de um que treina bem e avalia mal:

**O split é por VÍDEO, nunca por frame.** Frames vizinhos do mesmo segmento são
quase idênticos: separá-los entre treino e validação faz a métrica medir
memorização, não generalização. O split é determinístico (hash do nome), então
reexportar dá exatamente o mesmo recorte.

**Hardlink em vez de cópia.** Os frames são 4K; o acervo tem ~10 mil. Copiar
duplicaria dezenas de GB para nada. Cai para cópia se o filesystem recusar.

**A revisão humana ganha do SAM3.** É o mesmo merge de server/review.py: onde
houver correção, ela vale; no resto, vale o bruto.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image
from pipeline_core.review_store import FileMaskReviewStore, FrameMaskState
from pipeline_core.segmentation import coco_rle, yolo_polygons

from .review import (
    SegmentPaths,
    class_names,
    corners_to_yolo,
    effective_boxes,
    load_review,
    prompt_contract,
)
from .videos import iso

FORMATS = ("yolo", "coco")
TASKS = ("detection", "segmentation")


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
    object_id: str = ""
    class_label: str = ""


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

        root = Path(export["root"])
        if not root.is_dir():
            candidate_root = ctx.output_root / root.name
            if not candidate_root.is_dir():
                continue
            root = candidate_root

        intervals = {i.get("segment"): i for i in entry.get("intervals") or []}

        for segment in export["segments"]:
            interval = intervals.get(segment) or {}
            flags = interval.get("flags") or {}
            if not flags_match(flags, filters.flags):
                continue

            paths = SegmentPaths(root / segment)
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
            prompt = prompt_contract(paths)
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
        candidate.segment_dir / "_sam3",
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
    """Quanto o filtro seleciona, sem escrever nada."""
    if task not in TASKS:
        raise ValueError(f"tarefa desconhecida: {task}")
    names = class_names(workspace_root) if workspace_root else []
    candidates = collect(ctx, filters, workspace_root)

    frames = with_objects = reviewed = 0
    videos: dict[str, int] = {}
    by_flag: dict[str, dict[str, int]] = {}
    blocking_reasons: list[str] = []

    for candidate in candidates:
        videos[candidate.video_name] = videos.get(candidate.video_name, 0) + candidate.frame_count
        reviewed += candidate.reviewed
        paths = SegmentPaths(candidate.segment_dir)
        review = load_review(paths)
        for index in range(candidate.frame_count):
            if candidate.has_masks:
                try:
                    state = _mask_state(candidate, index)
                    boxes = _mask_boxes(state)
                    if task == "segmentation" and not state.instances and state.status != "edited":
                        blocking_reasons.append(
                            f"{candidate.video_name}/{candidate.segment}/{index}: mascara ausente"
                        )
                except (OSError, ValueError) as exc:
                    boxes = []
                    if task == "segmentation":
                        blocking_reasons.append(
                            f"{candidate.video_name}/{candidate.segment}/{index}: artefato invalido ({exc})"
                        )
            else:
                boxes, _ = effective_boxes(paths, index, names, review)
                if task == "segmentation" and index == 0:
                    blocking_reasons.append(
                        f"{candidate.video_name}/{candidate.segment}: run legado sem mascaras"
                    )
            frames += 1
            if boxes:
                with_objects += 1
        for group, value in candidate.flags.items():
            values = [value] if isinstance(value, str) else (value or [])
            bucket = by_flag.setdefault(group, {})
            for item in values:
                bucket[item] = bucket.get(item, 0) + candidate.frame_count

    return {
        "segments": len(candidates),
        "videos": len(videos),
        "frames": frames,
        "frames_with_objects": with_objects,
        "frames_reviewed": reviewed,
        "classes": names,
        "by_flag": by_flag,
        "task": task,
        "export_allowed": not blocking_reasons,
        "blocking_reasons": blocking_reasons[:100],
    }


def _link_or_copy(source: Path, destination: Path) -> None:
    if destination.exists():
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(source, destination)
    except OSError:
        shutil.copy2(source, destination)


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
) -> dict:
    """Escreve o dataset. Devolve o resumo que vai para o job."""
    if fmt not in FORMATS:
        raise ValueError(f"formato desconhecido: {fmt}")
    if task not in TASKS:
        raise ValueError(f"tarefa desconhecida: {task}")

    names = class_names(workspace_root) if workspace_root else []
    if not names:
        names = [ctx.label]
    class_index = {name: index for index, name in enumerate(names)}

    candidates = collect(ctx, filters, workspace_root)
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
        review = load_review(paths)
        split = splits[f"{candidate.object_id or ctx.object_id}:{candidate.video_id}"]

        for index in range(candidate.frame_count):
            seen += 1
            if on_progress and seen % 25 == 0:
                on_progress(seen, total_frames)

            image_path = paths.frame_path(index)
            if not image_path.exists():
                continue
            mask_state = _mask_state(candidate, index) if candidate.has_masks else None
            if mask_state is not None:
                boxes = _mask_boxes(mask_state)
                status = mask_state.status
                if task == "segmentation" and not mask_state.instances and status != "edited":
                    raise ValueError(
                        f"mascara ausente em {candidate.video_name}/{candidate.segment}/{index}"
                    )
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
                _link_or_copy(image_path, out_dir / "images" / split / f"{stem}.jpg")
                lines = []
                if task == "segmentation" and mask_state is not None:
                    for instance in mask_state.instances:
                        if instance.info.area_pixels == 0:
                            continue
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
                _link_or_copy(image_path, out_dir / split / f"{stem}.jpg")
                bucket = coco.setdefault(
                    split,
                    {
                        "info": {
                            "description": f"{ctx.object_id} — movies-screening-tool",
                            "date_created": iso(),
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

    manifest = {
        "generated_at": iso(),
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
        "segments": [
            {"video": c.relpath, "segment": c.segment, "frames": c.frame_count, "flags": c.flags}
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
    }
