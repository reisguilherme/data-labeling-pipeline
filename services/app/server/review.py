"""Revisão humana das bboxes que o SAM3 gerou.

O SAM3 acerta a maior parte dos frames; a revisão existe para consertar o resto
sem jogar fora o que ele acertou. Por isso o resultado bruto NUNCA é
sobrescrito: `_sam3/labels/*.txt` fica intacto e as correções vivem ao lado, em
`_sam3/review.json`, como um delta.

Isso dá três coisas de graça: dá para medir quanto o modelo acertou (a taxa de
edição é a métrica), dá para reprocessar o SAM3 sem perder o trabalho humano, e
um bug no editor não destrói o resultado da inferência.

Fronteira de formato: em disco os rótulos são YOLO (`classe cx cy w h`), porque
é o que o dataset consome. Para a interface tudo é convertido para
`[x1, y1, x2, y2]` normalizado — o mesmo formato do `prompt.json`, do
`annotations.json` e do BboxCanvas. Um formato só atravessando a UI evita a
classe de bug mais chata daqui: caixa que aparece deslocada porque alguém
esqueceu a conversão num caminho.
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass
from pathlib import Path

from pipeline_core.sam3_runs import (
    active_segment_output,
    effective_prompt_override_path,
    legacy_segment_output,
)

from .videos import iso

SCHEMA_VERSION = 1

REVIEW_FILENAME = "review.json"
MASK_REVIEW_FILENAME = "mask_review.json"

# "ok"     = conferido, a caixa do SAM3 está boa
# "edited" = o humano mandou outra coisa (lista vazia = frame sem objeto)
STATUSES = ("ok", "edited")

_lock = threading.Lock()


# --------------------------------------------------------------------------
# conversão de formato
# --------------------------------------------------------------------------


def yolo_to_corners(cx: float, cy: float, width: float, height: float) -> list[float]:
    """(cx, cy, w, h) -> [x1, y1, x2, y2], tudo normalizado."""
    return [
        max(0.0, cx - width / 2),
        max(0.0, cy - height / 2),
        min(1.0, cx + width / 2),
        min(1.0, cy + height / 2),
    ]


def corners_to_yolo(box: list[float]) -> tuple[float, float, float, float]:
    x1, y1, x2, y2 = box
    return ((x1 + x2) / 2, (y1 + y2) / 2, x2 - x1, y2 - y1)


def class_names(workspace_root: Path) -> list[str]:
    """Mapa de classes que o runner mantém (append-only)."""
    path = workspace_root / "sam3_classes.json"
    if not path.exists():
        return []
    try:
        return list(json.loads(path.read_text(encoding="utf-8")).get("names") or [])
    except (OSError, json.JSONDecodeError):
        return []


def read_yolo_labels(path: Path, names: list[str]) -> list[dict]:
    """Um .txt do runner -> lista de caixas no formato da interface.

    Arquivo ausente e arquivo vazio significam a MESMA coisa aqui: nenhum objeto
    neste frame. A distinção entre "processado e vazio" e "não processado" é
    feita pelo marcador do segmento, não frame a frame.
    """
    if not path.exists():
        return []
    boxes: list[dict] = []
    for position, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        parts = line.split()
        if len(parts) != 5:
            continue
        try:
            index = int(parts[0])
            cx, cy, width, height = (float(v) for v in parts[1:])
        except ValueError:
            continue
        boxes.append(
            {
                "obj_id": position,
                "label": names[index] if 0 <= index < len(names) else str(index),
                "normalized": yolo_to_corners(cx, cy, width, height),
            }
        )
    return boxes


# --------------------------------------------------------------------------
# o delta
# --------------------------------------------------------------------------


@dataclass
class SegmentPaths:
    segment_dir: Path

    @property
    def out_dir(self) -> Path:
        return active_segment_output(self.segment_dir)

    @property
    def control_dir(self) -> Path:
        """Mutable prompt/preview control files, never a published generation."""
        return legacy_segment_output(self.segment_dir)

    @property
    def labels_dir(self) -> Path:
        return self.out_dir / "labels"

    @property
    def review_path(self) -> Path:
        return self.out_dir / REVIEW_FILENAME

    @property
    def mask_review_path(self) -> Path:
        return self.out_dir / MASK_REVIEW_FILENAME

    @property
    def marker_path(self) -> Path:
        return self.out_dir / "run.json"

    def label_path(self, frame: int) -> Path:
        return self.labels_dir / f"{frame:06d}.txt"

    def frame_path(self, frame: int) -> Path:
        return self.segment_dir / f"{frame:06d}.jpg"


def prompt_contract(paths: SegmentPaths) -> dict:
    """Prompt inicial no formato consumido pela interface.

    A fonte é o ``prompt.json`` exportado pela triagem. Um override altera
    somente as caixas; o frame e as dimensões continuam pertencendo ao prompt
    original. Isso permite abrir a pré-anotação antes de existirem labels do
    SAM3 e mantém a mesma regra usada pelo runner.
    """
    prompt_path = paths.segment_dir / "prompt.json"
    if not prompt_path.exists():
        return {}
    try:
        raw = json.loads(prompt_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}

    objects = raw.get("objects") or []
    override_path = effective_prompt_override_path(
        paths.segment_dir, migrate_legacy=True
    )
    if override_path is not None:
        try:
            override = json.loads(override_path.read_text(encoding="utf-8"))
            if isinstance(override.get("objects"), list):
                objects = override["objects"]
        except (OSError, json.JSONDecodeError):
            pass

    normalized_objects = []
    for position, obj in enumerate(objects, start=1):
        box = obj.get("box_normalized") or obj.get("normalized")
        if not isinstance(box, list) or len(box) != 4:
            continue
        normalized_objects.append(
            {
                "obj_id": int(obj.get("obj_id") or position),
                "label": obj.get("label") or "",
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


def load_review(paths: SegmentPaths) -> dict:
    if not paths.review_path.exists():
        return {"schema_version": SCHEMA_VERSION, "frames": {}}
    try:
        data = json.loads(paths.review_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"schema_version": SCHEMA_VERSION, "frames": {}}
    data.setdefault("frames", {})
    return data


def load_mask_review(paths: SegmentPaths) -> dict | None:
    """Revisão canônica de máscaras; ``None`` indica um run legado sem máscaras."""
    if not (paths.out_dir / "masks").is_dir():
        return None
    if not paths.mask_review_path.exists():
        return {"schema_version": SCHEMA_VERSION, "frames": {}}
    try:
        data = json.loads(paths.mask_review_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"schema_version": SCHEMA_VERSION, "frames": {}}
    data.setdefault("frames", {})
    return data


def save_review(paths: SegmentPaths, data: dict) -> None:
    data["schema_version"] = SCHEMA_VERSION
    data["updated_at"] = iso()
    paths.out_dir.mkdir(parents=True, exist_ok=True)
    tmp = paths.review_path.with_name(REVIEW_FILENAME + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(data, ensure_ascii=False, indent=2))
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, paths.review_path)


def effective_boxes(paths: SegmentPaths, frame: int, names: list[str], review: dict) -> tuple[list[dict], str | None]:
    """Caixas que valem para este frame, e o status da revisão.

    A regra inteira do módulo em três linhas: o delta ganha do bruto quando
    existe e diz "edited"; senão, vale o que o SAM3 produziu.
    """
    entry = (review.get("frames") or {}).get(str(frame))
    if entry and entry.get("status") == "edited":
        return list(entry.get("boxes") or []), "edited"
    boxes = read_yolo_labels(paths.label_path(frame), names)
    return boxes, (entry or {}).get("status")


def segment_state(paths: SegmentPaths, frame_count: int, names: list[str]) -> dict:
    """Tudo que a tela de revisão precisa para um segmento, de uma vez.

    Devolver os 49 (ou 799) frames num payload só é deliberado: são poucos KB, e
    poupa uma requisição por frame durante a navegação, que é justamente o que
    precisa ser instantâneo.
    """
    review = load_review(paths)
    mask_review = load_mask_review(paths)
    status_frames = (mask_review if mask_review is not None else review).get("frames") or {}
    frames = []
    reviewed = edited = with_objects = 0

    for index in range(frame_count):
        boxes, legacy_status = effective_boxes(paths, index, names, review)
        status = (status_frames.get(str(index)) or {}).get("status")
        if mask_review is None:
            status = legacy_status
        if status:
            reviewed += 1
        if status == "edited":
            edited += 1
        if boxes:
            with_objects += 1
        frames.append({"frame": index, "boxes": boxes, "status": status})

    return {
        "frame_count": frame_count,
        "reviewed": reviewed,
        "edited": edited,
        "with_objects": with_objects,
        "complete": reviewed >= frame_count and frame_count > 0,
        "reviewed_by": (mask_review if mask_review is not None else review).get("by"),
        "updated_at": (mask_review if mask_review is not None else review).get("updated_at"),
        "frames": frames,
    }


def set_frame(
    paths: SegmentPaths,
    frame: int,
    *,
    status: str,
    boxes: list[dict] | None,
    user: str | None,
) -> dict:
    """Grava o estado de UM frame. Serializado: a tela salva a cada ajuste."""
    if status not in STATUSES:
        raise ValueError(f"status inválido: {status}")

    with _lock:
        review = load_review(paths)
        entry: dict = {"status": status, "at": iso()}
        if status == "edited":
            # Lista vazia é informação, não ausência: significa "olhei e não há
            # objeto aqui", o oposto de "o SAM3 não achou nada".
            entry["boxes"] = [
                {
                    "obj_id": int(box.get("obj_id") or position),
                    "label": box.get("label") or "",
                    "normalized": [float(v) for v in box["normalized"]],
                }
                for position, box in enumerate(boxes or [], start=1)
            ]
        review["frames"][str(frame)] = entry
        if user:
            review["by"] = user
        save_review(paths, review)
    return entry


def confirm_range(
    paths: SegmentPaths, start: int, end: int, *, user: str | None, overwrite: bool = False
) -> int:
    """Marca [start, end] como conferido de uma vez.

    Por padrão NÃO toca em frames já editados: confirmar em bloco é um atalho
    para trechos que estão visivelmente bons, e apagar uma correção humana com
    ele seria uma armadilha.
    """
    with _lock:
        review = load_review(paths)
        frames = review.setdefault("frames", {})
        changed = 0
        stamp = iso()
        for index in range(start, end + 1):
            key = str(index)
            current = frames.get(key)
            if current and current.get("status") == "edited" and not overwrite:
                continue
            if current and current.get("status") == "ok":
                continue
            frames[key] = {"status": "ok", "at": stamp}
            changed += 1
        if user:
            review["by"] = user
        save_review(paths, review)
    return changed


def clear_frame(paths: SegmentPaths, frame: int) -> None:
    """Desfaz a revisão de um frame — volta a valer o resultado do SAM3."""
    with _lock:
        review = load_review(paths)
        review.get("frames", {}).pop(str(frame), None)
        save_review(paths, review)


# --------------------------------------------------------------------------
# visão por vídeo
# --------------------------------------------------------------------------


def progress_for(export_root: Path, segments: list[str], names: list[str]) -> dict:
    """Progresso agregado de um vídeo, sem carregar as caixas."""
    total = reviewed = edited = 0
    per_segment = []

    for segment in segments:
        paths = SegmentPaths(export_root / segment)
        marker = paths.marker_path
        frame_count = 0
        if marker.exists():
            try:
                frame_count = int(
                    json.loads(marker.read_text(encoding="utf-8")).get("frame_count") or 0
                )
            except (OSError, json.JSONDecodeError, ValueError):
                frame_count = 0
        if not frame_count:
            frame_count = len(list(paths.labels_dir.glob("*.txt")))

        review = load_mask_review(paths)
        if review is None:
            review = load_review(paths)
        frames = review.get("frames") or {}
        segment_reviewed = sum(1 for value in frames.values() if value.get("status") in STATUSES)
        segment_edited = sum(1 for value in frames.values() if value.get("status") == "edited")

        total += frame_count
        reviewed += min(segment_reviewed, frame_count)
        edited += segment_edited
        per_segment.append(
            {
                "segment": segment,
                "frame_count": frame_count,
                "reviewed": min(segment_reviewed, frame_count),
                "edited": segment_edited,
                "complete": frame_count > 0 and segment_reviewed >= frame_count,
            }
        )

    return {
        "frame_count": total,
        "reviewed": reviewed,
        "edited": edited,
        "complete": total > 0 and reviewed >= total,
        "segments": per_segment,
    }
