"""Modelos de anotação e normalização das entradas vindas da UI."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from . import flags as flags_module

Status = Literal["pending", "in_progress", "done", "no_boom"]


class BBoxIn(BaseModel):
    obj_id: int | None = None
    # Sem default fixo: o rótulo vem do objeto que está sendo anotado e é
    # resolvido no servidor (build_interval). Se o cliente mandar um, ele é
    # ignorado — assim uma aba aberta no objeto errado não contrabandeia um
    # label estranho para dentro do dataset.
    label: str | None = None
    # Fonte da verdade: [x1, y1, x2, y2] normalizado, na ordem exata do SAM3.
    normalized: list[float] = Field(min_length=4, max_length=4)

    @field_validator("normalized")
    @classmethod
    def _check(cls, value: list[float]) -> list[float]:
        x1, y1, x2, y2 = value
        if not all(0.0 <= v <= 1.0 for v in value):
            raise ValueError("coordenadas normalizadas precisam estar em [0, 1]")
        if x2 <= x1 or y2 <= y1:
            raise ValueError("bbox precisa ter largura e altura positivas")
        return value


class IntervalIn(BaseModel):
    start_frame: int = Field(ge=0)
    end_frame: int = Field(ge=0)
    prompt_frame: int | None = None
    bboxes: list[BBoxIn] = Field(default_factory=list)
    flags: dict[str, Any] = Field(default_factory=dict)
    notes: str = ""


class VideoEntryIn(BaseModel):
    expected_revision: int = Field(ge=0, strict=True)
    lock_token: str = Field(min_length=8, max_length=128)
    status: Status = "in_progress"
    intervals: list[IntervalIn] = Field(default_factory=list)
    notes: str = ""


def build_interval(
    payload: IntervalIn,
    order: int,
    *,
    width: int,
    height: int,
    fps: float | None,
    start_time_offset: float,
    label: str,
) -> dict:
    """Materializa um intervalo, derivando tudo o que a UI não precisa mandar."""
    start, end = payload.start_frame, payload.end_frame
    prompt = payload.prompt_frame if payload.prompt_frame is not None else start

    bboxes = []
    for position, box in enumerate(payload.bboxes, start=1):
        x1, y1, x2, y2 = box.normalized
        bboxes.append(
            {
                # obj_id sequencial a partir de 1, como o notebook do SAM3 faz.
                "obj_id": box.obj_id if box.obj_id and box.obj_id > 0 else position,
                "label": label,
                "normalized": [round(v, 10) for v in (x1, y1, x2, y2)],
                # Derivado, só para humanos e ferramentas estilo CVAT.
                "pixel": [
                    round(x1 * width),
                    round(y1 * height),
                    round(x2 * width),
                    round(y2 * height),
                ],
            }
        )

    def at(frame: int) -> float | None:
        if not fps:
            return None
        return round(start_time_offset + frame / fps, 6)

    return {
        "index": order,
        "segment": f"seg_{order:02d}",
        "start_frame": start,
        "end_frame": end,
        "frame_count": end - start + 1,
        "start_time_sec": at(start),
        "end_time_sec": at(end),
        "prompt_frame": prompt,
        "bboxes": bboxes,
        "flags": flags_module.normalize(payload.flags),
        "notes": payload.notes,
    }


def validate_intervals(intervals: list[IntervalIn], frame_count: int | None) -> list[str]:
    errors: list[str] = []

    for position, interval in enumerate(intervals):
        tag = f"intervalo {position + 1}"

        if interval.end_frame < interval.start_frame:
            errors.append(f"{tag}: fim ({interval.end_frame}) antes do início ({interval.start_frame})")
        if frame_count is not None and interval.end_frame >= frame_count:
            errors.append(
                f"{tag}: fim {interval.end_frame} passa do último frame ({frame_count - 1})"
            )

        prompt = interval.prompt_frame
        if prompt is not None and not (interval.start_frame <= prompt <= interval.end_frame):
            errors.append(f"{tag}: frame do prompt fora do intervalo")

        if not interval.bboxes:
            errors.append(f"{tag}: precisa de pelo menos um bbox")

        errors.extend(f"{tag}: {problem}" for problem in flags_module.validate(interval.flags))

    ordered = sorted(intervals, key=lambda i: i.start_frame)
    for previous, current in zip(ordered, ordered[1:]):
        if current.start_frame <= previous.end_frame:
            errors.append(
                f"intervalos sobrepostos: [{previous.start_frame}-{previous.end_frame}] "
                f"e [{current.start_frame}-{current.end_frame}]"
            )

    return errors
