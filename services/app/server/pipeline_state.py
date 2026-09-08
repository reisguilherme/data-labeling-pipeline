"""Estado operacional autoritativo de cada video.

O status manual de triagem continua sendo a entrada do fluxo. A partir dele,
este modulo deriva as etapas SAM3, revisao e concluido dos artefatos reais em
disco. Assim, uma fila marcada como concluida nunca transforma sozinha um video
em "concluido": todas as mascaras precisam existir, ser validas e estar
revisadas.
"""

from __future__ import annotations

import json
import re
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
    validation_status: str = "not_applicable"
    inconsistencies: tuple[str, ...] = ()

    def public_progress(self) -> dict:
        return {
            "expected_frames": self.expected_frames,
            "reviewed_frames": self.reviewed_frames,
            "edited_frames": self.edited_frames,
            "artifacts_valid": self.artifacts_valid,
            "validation_status": self.validation_status,
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
    validation_status: str = "not_applicable",
) -> PipelineSnapshot:
    """Classifica um video sem depender de detalhes do armazenamento."""
    common = {
        "expected_frames": max(0, expected_frames),
        "reviewed_frames": max(0, reviewed_frames),
        "edited_frames": max(0, edited_frames),
        "artifacts_valid": artifacts_valid,
        "validation_status": "invalid" if inconsistencies else validation_status,
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
    if validation_status == "audit_required" and expected_frames > 0:
        return PipelineSnapshot("review", "audit_required", **common)
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


_SCHEMA_VERSION = 1
_ARTIFACT_FORMAT = "png-1bit-v1"
_SHA256 = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True)
class _ExpectedMask:
    segment_name: str
    path: Path
    expected_size: tuple[int, int]
    checksum: str | None


def _schema_int(value: object) -> int | None:
    return value if type(value) is int else None


def _object_ids(value: object) -> list[int] | None:
    if not isinstance(value, list) or not value:
        return None
    result: list[int] = []
    for item in value:
        if not isinstance(item, dict):
            return None
        obj_id = _schema_int(item.get("obj_id"))
        if obj_id is None:
            return None
        result.append(obj_id)
    return result


def inspect_pipeline_entry(
    entry: dict | None,
    sam3: dict | None,
    output_root: Path,
) -> PipelineSnapshot:
    """Inspeciona somente metadados; nunca abre os bytes das mascaras."""
    return _inspect_pipeline_entry(entry, sam3, output_root, audit_masks=False)


def audit_pipeline_entry(
    entry: dict | None,
    sam3: dict | None,
    output_root: Path,
) -> PipelineSnapshot:
    """Valida metadados e, explicitamente, cada PNG bruto esperado."""
    return _inspect_pipeline_entry(entry, sam3, output_root, audit_masks=True)


def _inspect_pipeline_entry(
    entry: dict | None,
    sam3: dict | None,
    output_root: Path,
    *,
    audit_masks: bool,
) -> PipelineSnapshot:
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
    audit_required = False
    inconsistencies: list[str] = []
    expected_masks: list[_ExpectedMask] = []

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

        if _schema_int(run.get("schema_version")) != _SCHEMA_VERSION:
            inconsistencies.append(f"{segment_name}: schema de run.json desconhecido")
            continue
        frame_count = _schema_int(run.get("frame_count"))
        frames_written = _schema_int(run.get("frames_written"))
        if frame_count is None or frame_count <= 0 or frames_written != frame_count:
            inconsistencies.append(f"{segment_name}: contagem de frames invalida")
            continue

        if prompt is None:
            inconsistencies.append(f"{segment_name}: prompt.json ausente ou invalido")
            continue
        if _schema_int(prompt.get("schema_version")) != _SCHEMA_VERSION:
            inconsistencies.append(f"{segment_name}: schema de prompt.json desconhecido")
            continue
        prompt_frame_count = _schema_int(prompt.get("frame_count"))
        width = _schema_int(prompt.get("image_width"))
        height = _schema_int(prompt.get("image_height"))
        prompt_object_ids = _object_ids(prompt.get("objects"))
        if (
            prompt_frame_count is None
            or prompt_frame_count <= 0
            or prompt_frame_count != frame_count
            or width is None
            or width <= 0
            or height is None
            or height <= 0
            or prompt_object_ids is None
        ):
            inconsistencies.append(f"{segment_name}: prompt incompleto")
            continue
        if len(set(prompt_object_ids)) != len(prompt_object_ids):
            inconsistencies.append(f"{segment_name}: obj_id repetido no prompt")
            continue

        if "objects" in run:
            run_object_ids = _object_ids(run.get("objects"))
            if (
                run_object_ids is None
                or len(set(run_object_ids)) != len(run_object_ids)
                or set(run_object_ids) != set(prompt_object_ids)
            ):
                inconsistencies.append(
                    f"{segment_name}: objetos de run.json divergem do prompt"
                )
                continue

        expected_frames += frame_count
        expected_keys = {
            f"masks/{obj_id}/{frame:06d}.png"
            for obj_id in prompt_object_ids
            for frame in range(frame_count)
        }
        checksums: dict[str, str] | None = None
        if "artifacts" not in run:
            audit_required = True
        else:
            artifacts = run.get("artifacts")
            if not isinstance(artifacts, dict):
                inconsistencies.append(f"{segment_name}: manifesto de artefatos invalido")
            else:
                raw_checksums = artifacts.get("checksums")
                artifact_files = _schema_int(artifacts.get("files"))
                if (
                    artifacts.get("format") != _ARTIFACT_FORMAT
                    or artifact_files != len(expected_keys)
                    or not isinstance(raw_checksums, dict)
                    or len(raw_checksums) != len(expected_keys)
                    or set(raw_checksums) != expected_keys
                    or any(
                        not isinstance(checksum, str)
                        or _SHA256.fullmatch(checksum) is None
                        for checksum in raw_checksums.values()
                    )
                ):
                    inconsistencies.append(
                        f"{segment_name}: manifesto de artefatos invalido"
                    )
                else:
                    checksums = raw_checksums

        for relative in sorted(expected_keys):
            expected_masks.append(
                _ExpectedMask(
                    str(segment_name),
                    out / Path(relative),
                    (width, height),
                    checksums.get(relative) if checksums is not None else None,
                )
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

    validation_status = "not_applicable"
    if inconsistencies:
        validation_status = "invalid"
    elif has_unprocessed_segments:
        validation_status = "not_applicable"
    elif audit_required:
        validation_status = "audit_required"
    elif expected_frames > 0:
        validation_status = "manifest"

    if audit_masks and validation_status in {"manifest", "audit_required"}:
        for expected in expected_masks:
            try:
                info = inspect_binary_png(
                    expected.path.read_bytes(), expected_size=expected.expected_size
                )
            except (OSError, MaskValidationError) as exc:
                inconsistencies.append(
                    f"{expected.segment_name}: mascara invalida {expected.path.name}: {exc}"
                )
                continue
            if expected.checksum is not None and info.sha256 != expected.checksum:
                inconsistencies.append(
                    f"{expected.segment_name}: checksum divergente {expected.path.name}"
                )
        validation_status = "invalid" if inconsistencies else "manifest"

    valid = (
        validation_status == "manifest"
        and expected_frames > 0
        and not has_unprocessed_segments
        and not inconsistencies
    )
    inferred_state = sam3_state or (
        "done" if expected_frames > 0 and not has_unprocessed_segments else None
    )
    return classify_pipeline(
        annotation_status,
        inferred_state,
        expected_frames,
        reviewed_frames,
        valid,
        edited_frames,
        tuple(inconsistencies),
        validation_status,
    )
