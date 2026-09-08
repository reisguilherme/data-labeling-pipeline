"""Estado operacional autoritativo de cada video.

O status manual de triagem continua sendo a entrada do fluxo. A partir dele,
este modulo deriva as etapas SAM3, revisao e concluido dos artefatos reais em
disco. Assim, uma fila marcada como concluida nunca transforma sozinha um video
em "concluido": todas as mascaras precisam existir, ser validas e estar
revisadas.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from pipeline_core.masks import MaskValidationError, inspect_binary_png


@dataclass(frozen=True)
class PipelineSnapshot:
    stage: str
    status: str
    expected_frames: int = 0
    reviewed_frames: int = 0
    edited_frames: int = 0
    artifacts_valid: bool = False
    inconsistencies: tuple[str, ...] = ()

    def public_progress(self) -> dict:
        return {
            "expected_frames": self.expected_frames,
            "reviewed_frames": self.reviewed_frames,
            "edited_frames": self.edited_frames,
            "artifacts_valid": self.artifacts_valid,
            "inconsistencies": list(self.inconsistencies),
        }


def classify_pipeline(
    annotation_status: str,
    sam3_state: str | None,
    expected_frames: int,
    reviewed_frames: int,
    artifacts_valid: bool,
    edited_frames: int = 0,
    inconsistencies: tuple[str, ...] = (),
) -> PipelineSnapshot:
    """Classifica um video sem depender de detalhes do armazenamento."""
    common = {
        "expected_frames": max(0, expected_frames),
        "reviewed_frames": max(0, reviewed_frames),
        "edited_frames": max(0, edited_frames),
        "artifacts_valid": artifacts_valid,
        "inconsistencies": inconsistencies,
    }

    if annotation_status == "no_boom":
        return PipelineSnapshot("discarded", "discarded", **common)
    if annotation_status in {"pending", "in_progress"}:
        return PipelineSnapshot("triage", annotation_status, **common)

    # O estado atual da fila tem precedencia sobre artefatos de uma execucao
    # anterior. Isso evita mostrar como validado um video cujo reprocessamento
    # atual falhou ou ainda esta rodando.
    if sam3_state in {"queued", "leased", "running", "error", "cancelled"}:
        return PipelineSnapshot("sam3", sam3_state, **common)

    if inconsistencies:
        return PipelineSnapshot("sam3", "invalid", **common)
    if expected_frames <= 0 or not artifacts_valid:
        return PipelineSnapshot("sam3", "ready", **common)
    if reviewed_frames >= expected_frames:
        return PipelineSnapshot("completed", "validated", **common)
    if reviewed_frames > 0:
        return PipelineSnapshot("review", "in_progress", **common)
    return PipelineSnapshot("review", "waiting", **common)


def _read_json(path: Path) -> dict | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _export_root(entry: dict, output_root: Path) -> Path | None:
    export = entry.get("export") or {}
    raw = export.get("root")
    if not raw:
        return None
    candidate = Path(str(raw))
    if candidate.exists():
        return candidate
    # Metadados importados podem carregar o caminho absoluto do host antigo.
    # O nome final do diretorio de video permanece estavel no rehome.
    fallback = output_root / candidate.name
    return fallback if fallback.exists() else candidate


def inspect_pipeline_entry(
    entry: dict | None,
    sam3: dict | None,
    output_root: Path,
) -> PipelineSnapshot:
    """Inspeciona runs, PNGs brutos e manifestos de revisao de um video."""
    entry = entry or {}
    annotation_status = str(entry.get("status") or "pending")
    sam3_state = str(sam3.get("state")) if sam3 and sam3.get("state") else None

    if annotation_status in {"pending", "in_progress", "no_boom"}:
        return classify_pipeline(annotation_status, sam3_state, 0, 0, False)

    export = entry.get("export") or {}
    segments = export.get("segments") or []
    root = _export_root(entry, output_root)
    if root is None or not segments:
        return classify_pipeline(annotation_status, sam3_state, 0, 0, False)

    expected_frames = 0
    reviewed_frames = 0
    edited_frames = 0
    has_unprocessed_segments = False
    inconsistencies: list[str] = []

    for segment_name in segments:
        segment = root / str(segment_name)
        out = segment / "_sam3"
        run_path = out / "run.json"
        run = _read_json(run_path)
        prompt = _read_json(segment / "prompt.json")
        if run is None:
            # Antes da primeira propagacao ainda nao existe run.json. Isso e um
            # trabalho pronto para SAM3, nao um artefato corrompido. Um arquivo
            # presente mas ilegivel, por outro lado, representa uma tentativa
            # interrompida e precisa continuar visivel como invalida.
            if run_path.exists():
                inconsistencies.append(f"{segment_name}: run.json invalido")
            else:
                has_unprocessed_segments = True
            continue
        if run.get("status") != "done":
            inconsistencies.append(f"{segment_name}: run SAM3 nao concluido")
            continue

        frame_count = int(run.get("frame_count") or 0)
        frames_written = int(run.get("frames_written") or frame_count)
        if frame_count <= 0 or frames_written != frame_count:
            inconsistencies.append(f"{segment_name}: contagem de frames invalida")
            continue
        expected_frames += frame_count

        if prompt is None:
            inconsistencies.append(f"{segment_name}: prompt.json ausente ou invalido")
            continue
        width = int(prompt.get("image_width") or 0)
        height = int(prompt.get("image_height") or 0)
        objects = prompt.get("objects") or []
        if width <= 0 or height <= 0 or not isinstance(objects, list) or not objects:
            inconsistencies.append(f"{segment_name}: prompt incompleto")
            continue

        for obj in objects:
            try:
                obj_id = int(obj["obj_id"])
            except (KeyError, TypeError, ValueError):
                inconsistencies.append(f"{segment_name}: obj_id invalido no prompt")
                continue
            for frame in range(frame_count):
                mask = out / "masks" / str(obj_id) / f"{frame:06d}.png"
                if not mask.is_file():
                    inconsistencies.append(
                        f"{segment_name}: mascara ausente obj {obj_id}, frame {frame}"
                    )
                    continue
                try:
                    inspect_binary_png(
                        mask.read_bytes(), expected_size=(width, height)
                    )
                except (OSError, MaskValidationError) as exc:
                    inconsistencies.append(
                        f"{segment_name}: mascara invalida obj {obj_id}, frame {frame}: {exc}"
                    )

        review = _read_json(out / "mask_review.json") or {}
        frames = review.get("frames") or {}
        if isinstance(frames, dict):
            for frame in range(frame_count):
                reviewed = frames.get(str(frame)) or {}
                if reviewed.get("status") in {"ok", "edited"}:
                    reviewed_frames += 1
                    if reviewed.get("status") == "edited":
                        edited_frames += 1

    valid = expected_frames > 0 and not has_unprocessed_segments and not inconsistencies
    inferred_state = sam3_state or ("done" if valid else None)
    return classify_pipeline(
        annotation_status,
        inferred_state,
        expected_frames,
        reviewed_frames,
        valid,
        edited_frames,
        tuple(inconsistencies),
    )
