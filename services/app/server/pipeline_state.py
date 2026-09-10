"""Estado operacional autoritativo de cada video.

O status manual de triagem continua sendo a entrada do fluxo. A partir dele,
este modulo deriva as etapas SAM3, revisao e concluido dos artefatos reais em
disco. Assim, uma fila marcada como concluida nunca transforma sozinha um video
em "concluido": todas as mascaras precisam existir, ser validas e estar
revisadas.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from pipeline_core.masks import MaskValidationError, inspect_binary_png
from pipeline_core.sam3_runs import (
    Sam3GenerationError,
    active_segment_output,
    current_manifest_path,
    effective_prompt_override_path,
    generation_root,
    legacy_segment_output,
    load_current_manifest,
    resolve_export_root,
    resolve_segment_dir,
)

from .review import mask_review_manifest_identity


_MAX_CONTROL_JSON_BYTES = 16 * 1024 * 1024
_WALL_CLOCK_KEYS = {
    "at",
    "created_at",
    "enqueued_at",
    "exported_at",
    "finished_at",
    "projected_at",
    "published_at",
    "requested_at",
    "started_at",
    "updated_at",
}


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

    def projection_dict(self) -> dict:
        """Stable database payload; no timestamps or filesystem metadata."""
        return {
            "pipeline_stage": self.stage,
            "stage_status": self.status,
            "expected_frames": self.expected_frames,
            "reviewed_frames": self.reviewed_frames,
            "edited_frames": self.edited_frames,
            "artifacts_valid": self.artifacts_valid,
            "validation_status": self.validation_status,
            "inconsistencies": list(self.inconsistencies),
            "complete": (
                self.stage == "completed"
                and self.status == "validated"
                and self.validation_status == "manifest"
                and self.artifacts_valid
                and self.expected_frames > 0
                and self.reviewed_frames >= self.expected_frames
            ),
        }


@dataclass(frozen=True)
class PipelineSource:
    identity: dict[str, Any]
    snapshot: PipelineSnapshot

    @property
    def snapshot_dict(self) -> dict:
        return self.snapshot.projection_dict()


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


def _without_wall_clock(value):
    if isinstance(value, dict):
        return {
            str(key): _without_wall_clock(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
            if str(key) not in _WALL_CLOCK_KEYS
            and not str(key).endswith("_timestamp")
        }
    if isinstance(value, list):
        return [_without_wall_clock(item) for item in value]
    return value


def _metadata_digest(value: Any) -> str:
    payload = json.dumps(
        _without_wall_clock(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _read_control_json(path: Path) -> tuple[dict, dict | None]:
    """Read one small control document and return identity plus parsed value."""
    if not path.exists():
        return {"state": "missing", "sha256": None}, None
    if path.is_symlink() or not path.is_file():
        return {"state": "invalid", "sha256": None}, None
    try:
        with path.open("rb") as stream:
            raw = stream.read(_MAX_CONTROL_JSON_BYTES + 1)
    except OSError:
        return {"state": "invalid", "sha256": None}, None
    if len(raw) > _MAX_CONTROL_JSON_BYTES:
        return {
            "state": "invalid",
            "sha256": hashlib.sha256(raw).hexdigest(),
            "oversized": True,
        }, None
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {"state": "invalid", "sha256": hashlib.sha256(raw).hexdigest()}, None
    if not isinstance(value, dict):
        return {"state": "invalid", "sha256": hashlib.sha256(raw).hexdigest()}, None
    try:
        digest = _metadata_digest(value)
    except (TypeError, ValueError):
        return {"state": "invalid", "sha256": hashlib.sha256(raw).hexdigest()}, None
    return {
        "state": "valid",
        "sha256": digest,
        "schema_version": value.get("schema_version"),
    }, value


def _read_json(path: Path) -> dict | None:
    _identity, value = _read_control_json(path)
    return value


def _manifest_identity(path: Path, *, schema_optional: bool = False) -> tuple[dict, dict | None]:
    identity, value = _read_control_json(path)
    if value is None:
        return identity, None
    schema = value.get("schema_version")
    if schema is None and schema_optional:
        return identity, value
    if schema is None:
        identity["state"] = "legacy"
    elif type(schema) is not int or schema != _SCHEMA_VERSION:
        identity["state"] = "invalid"
    return identity, value


def _annotation_identity(entry: dict | None) -> tuple[dict, list[str], list[str]]:
    invalid: list[str] = []
    audit: list[str] = []
    if entry is None:
        return {
            "state": "missing",
            "status": "pending",
            "revision": 0,
            "export": {"state": "missing", "sha256": None},
            "export_completion": {"state": "missing", "sha256": None},
        }, invalid, audit
    if not isinstance(entry, dict):
        return {
            "state": "invalid",
            "status": None,
            "revision": None,
            "export": {"state": "invalid", "sha256": None},
            "export_completion": {"state": "invalid", "sha256": None},
        }, ["anotacao invalida"], audit

    status = entry.get("status", "pending")
    if status not in {"pending", "in_progress", "done", "no_boom"}:
        invalid.append("status da anotacao invalido")
    revision = entry.get("annotation_revision")
    if revision is None:
        if status in {"done", "no_boom"}:
            audit.append("anotacao legada sem revision")
        revision = 0
    elif type(revision) is not int or revision < 0:
        invalid.append("annotation_revision invalida")
        revision = None

    export = entry.get("export")
    if export is None:
        export_identity = {"state": "missing", "sha256": None}
    elif not isinstance(export, dict):
        export_identity = {"state": "invalid", "sha256": None}
        invalid.append("identidade do export invalida")
    else:
        try:
            export_digest = _metadata_digest(export)
        except (TypeError, ValueError):
            export_digest = None
        root = export.get("root")
        segments = export.get("segments")
        export_revision = export.get("annotation_revision")
        owner = export.get("owner")
        export_state = "valid"
        if (
            not isinstance(root, str)
            or not root
            or not isinstance(segments, list)
            or not segments
            or any(not isinstance(item, str) or not item for item in segments)
            or len(set(segments)) != len(segments)
        ):
            export_state = "invalid"
            invalid.append("identidade do export invalida")
        if export_revision is None or owner is None:
            if export_state != "invalid":
                export_state = "legacy"
            audit.append("export legado sem ownership completo")
        elif (
            type(export_revision) is not int
            or revision is None
            or export_revision != revision
            or not isinstance(owner, dict)
            or owner.get("video_id") != entry.get("video_id")
            or owner.get("relpath") != entry.get("relpath")
            or owner.get("annotation_revision") != export_revision
        ):
            export_state = "invalid"
            invalid.append("ownership do export diverge da anotacao")
        export_identity = {
            "state": export_state,
            "sha256": export_digest,
            "root": root if isinstance(root, str) else None,
            "segments": list(segments) if isinstance(segments, list) else None,
            "annotation_revision": (
                export_revision if type(export_revision) is int else None
            ),
        }

    completion = entry.get("export_completion")
    if completion is None:
        completion_identity = {"state": "missing", "sha256": None}
    elif isinstance(completion, dict):
        try:
            completion_digest = _metadata_digest(completion)
        except (TypeError, ValueError):
            completion_digest = None
        completion_identity = {
            "state": "valid" if completion_digest is not None else "invalid",
            "sha256": completion_digest,
        }
        if completion_digest is None:
            invalid.append("conclusao do export invalida")
    else:
        completion_identity = {"state": "invalid", "sha256": None}
        invalid.append("conclusao do export invalida")

    return {
        "state": "invalid" if invalid else ("legacy" if audit else "valid"),
        "status": status if isinstance(status, str) else None,
        "revision": revision,
        "export": export_identity,
        "export_completion": completion_identity,
    }, invalid, audit


def _sam3_job_identity(sam3: dict | None) -> tuple[dict, list[str]]:
    if sam3 is None:
        return {"state": None, "run_id": None, "annotation_revision": None}, []
    if not isinstance(sam3, dict):
        return {
            "state": "invalid",
            "run_id": None,
            "annotation_revision": None,
        }, ["estado da fila SAM3 invalido"]
    state = sam3.get("state")
    revision = sam3.get("annotation_revision")
    invalid = []
    if state not in {"queued", "leased", "running", "done", "error", "cancelled"}:
        invalid.append("estado da fila SAM3 invalido")
    if revision is not None and (type(revision) is not int or revision < 0):
        invalid.append("revisao da fila SAM3 invalida")
    return {
        "state": state,
        "run_id": sam3.get("run_id") if isinstance(sam3.get("run_id"), str) else None,
        "annotation_revision": revision if type(revision) is int else None,
    }, invalid


def _export_root(entry: dict, output_root: Path) -> Path | None:
    export = entry.get("export") or {}
    raw = export.get("root")
    if not raw:
        return None
    return resolve_export_root(output_root, str(raw))


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


def _projection_downgrade(
    snapshot: PipelineSnapshot,
    *,
    invalid: list[str],
    audit: list[str],
) -> PipelineSnapshot:
    if invalid:
        inconsistencies = tuple(dict.fromkeys((*snapshot.inconsistencies, *invalid)))
        if snapshot.stage == "discarded":
            return replace(
                snapshot,
                artifacts_valid=False,
                validation_status="invalid",
                inconsistencies=inconsistencies,
            )
        return PipelineSnapshot(
            stage="sam3",
            status="invalid",
            expected_frames=snapshot.expected_frames,
            reviewed_frames=snapshot.reviewed_frames,
            edited_frames=snapshot.edited_frames,
            artifacts_valid=False,
            validation_status="invalid",
            inconsistencies=inconsistencies,
        )
    if audit and snapshot.validation_status not in {"invalid", "not_applicable"}:
        return PipelineSnapshot(
            stage="review",
            status="audit_required",
            expected_frames=snapshot.expected_frames,
            reviewed_frames=snapshot.reviewed_frames,
            edited_frames=snapshot.edited_frames,
            artifacts_valid=False,
            validation_status="audit_required",
            inconsistencies=snapshot.inconsistencies,
        )
    return snapshot


def derive_pipeline_source(
    entry: dict | None,
    sam3: dict | None,
    output_root: Path,
) -> PipelineSource:
    """Derive a deterministic source identity and metadata-only projection.

    Only named control documents are opened.  Mask directories are never
    enumerated and PNG bytes are reserved for the explicit deep-audit path.
    """

    annotation, invalid, audit = _annotation_identity(entry)
    sam3_job, sam3_invalid = _sam3_job_identity(sam3)
    invalid.extend(sam3_invalid)
    identity: dict[str, Any] = {
        "schema_version": 1,
        "annotation": annotation,
        "sam3_job": sam3_job,
        "sam3_generation": {
            "state": "missing",
            "generation_id": None,
            "manifest_sha256": None,
        },
        "segments": [],
    }

    raw_entry = entry if isinstance(entry, dict) else {}
    raw_export = raw_entry.get("export")
    raw_segments = raw_export.get("segments") if isinstance(raw_export, dict) else []
    segments = (
        list(raw_segments)
        if isinstance(raw_segments, list)
        and all(isinstance(item, str) and item for item in raw_segments)
        else []
    )
    root: Path | None = None
    if isinstance(raw_export, dict) and raw_export.get("root"):
        try:
            root = _export_root(raw_entry, output_root)
        except Sam3GenerationError as exc:
            invalid.append(str(exc))

    current: dict | None = None
    pointer_value: dict | None = None
    pointer_identity = {"state": "missing", "sha256": None}
    if root is not None:
        pointer_identity, pointer_value = _manifest_identity(
            current_manifest_path(root)
        )
        if pointer_identity["state"] == "invalid":
            invalid.append("ponteiro SAM3 invalido")
        elif pointer_value is not None:
            generation_id = pointer_value.get("generation_id")
            manifest_sha = pointer_value.get("manifest_sha256")
            immutable = pointer_value.get("format") == "immutable-generation-v1"
            generation_control = {"state": "missing", "sha256": None}
            generation_identifier_invalid = False
            if isinstance(generation_id, str) and generation_id:
                try:
                    manifest_path = generation_root(root, generation_id) / "generation.json"
                except Sam3GenerationError:
                    generation_identifier_invalid = True
                    generation_control = {"state": "invalid", "sha256": None}
                else:
                    generation_control, _ = _manifest_identity(manifest_path)
            if immutable and (
                not isinstance(generation_id, str)
                or not generation_id
                or generation_identifier_invalid
                or not isinstance(manifest_sha, str)
                or _SHA256.fullmatch(manifest_sha) is None
                or generation_control["state"] != "valid"
            ):
                invalid.append("manifesto da geracao SAM3 invalido")
                generation_state = "invalid"
            else:
                try:
                    current = load_current_manifest(root)
                except Sam3GenerationError as exc:
                    invalid.append(str(exc))
                    generation_state = "invalid"
                else:
                    generation_state = "valid" if immutable else "legacy"
                    if not immutable:
                        audit.append("ponteiro SAM3 legado")
            identity["sam3_generation"] = {
                "state": generation_state,
                "generation_id": generation_id if isinstance(generation_id, str) else None,
                "manifest_sha256": manifest_sha if isinstance(manifest_sha, str) else None,
                "pointer_sha256": pointer_identity.get("sha256"),
                "manifest_control_sha256": generation_control.get("sha256"),
            }

    legacy_run_found = False
    for segment_name in segments:
        segment_identity: dict[str, Any] = {
            "segment": segment_name,
            "prompt": {"state": "missing", "sha256": None},
            "prompt_override": {"state": "missing", "sha256": None},
            "run": {"state": "missing", "sha256": None},
            "mask_review": {
                "state": "missing",
                "sha256": None,
                "max_revision": 0,
                "reviewed_frames": 0,
            },
        }
        identity["segments"].append(segment_identity)
        if root is None:
            continue
        try:
            segment = resolve_segment_dir(root, segment_name)
        except Sam3GenerationError as exc:
            invalid.append(f"{segment_name}: {exc}")
            continue

        prompt_identity, _ = _manifest_identity(segment / "prompt.json")
        segment_identity["prompt"] = prompt_identity
        if prompt_identity["state"] == "missing":
            invalid.append(f"{segment_name}: prompt.json ausente")
        elif prompt_identity["state"] == "legacy":
            audit.append(f"{segment_name}: prompt.json legado")
        elif prompt_identity["state"] == "invalid":
            invalid.append(f"{segment_name}: prompt.json invalido")

        try:
            override_path = effective_prompt_override_path(segment)
        except Sam3GenerationError as exc:
            invalid.append(f"{segment_name}: {exc}")
            override_path = None
        if override_path is not None:
            override_identity, _ = _manifest_identity(
                override_path, schema_optional=True
            )
            segment_identity["prompt_override"] = override_identity
            if override_identity["state"] == "invalid":
                invalid.append(f"{segment_name}: prompt_override.json invalido")

        out = legacy_segment_output(segment)
        if pointer_value is not None and current is not None:
            try:
                out = active_segment_output(segment)
            except Sam3GenerationError as exc:
                invalid.append(f"{segment_name}: {exc}")
                continue

        run_identity, _ = _manifest_identity(out / "run.json")
        segment_identity["run"] = run_identity
        if run_identity["state"] == "legacy":
            audit.append(f"{segment_name}: run.json legado")
        elif run_identity["state"] == "invalid":
            invalid.append(f"{segment_name}: run.json invalido")
        if pointer_value is None and run_identity["state"] != "missing":
            legacy_run_found = True

        review_identity = mask_review_manifest_identity(out / "mask_review.json")
        segment_identity["mask_review"] = review_identity
        if review_identity["state"] == "legacy":
            audit.append(f"{segment_name}: mask_review.json legado")
        elif review_identity["state"] == "invalid":
            invalid.append(f"{segment_name}: mask_review.json invalido")

    if pointer_value is None and legacy_run_found:
        identity["sam3_generation"] = {
            "state": "legacy",
            "generation_id": None,
            "manifest_sha256": None,
            "pointer_sha256": pointer_identity.get("sha256"),
        }
        audit.append("run SAM3 legado sem geracao imutavel")
    elif pointer_identity["state"] == "invalid":
        identity["sam3_generation"] = {
            "state": "invalid",
            "generation_id": None,
            "manifest_sha256": None,
            "pointer_sha256": pointer_identity.get("sha256"),
        }

    snapshot = inspect_pipeline_entry(entry, sam3, output_root)
    snapshot = _projection_downgrade(snapshot, invalid=invalid, audit=audit)
    return PipelineSource(identity=identity, snapshot=snapshot)


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
    try:
        root = _export_root(entry, output_root)
    except Sam3GenerationError as exc:
        return classify_pipeline(
            annotation_status,
            sam3_state,
            0,
            0,
            False,
            inconsistencies=(str(exc),),
            validation_status="invalid",
        )
    if root is None or not segments:
        return classify_pipeline(annotation_status, sam3_state, 0, 0, False)
    try:
        generation = load_current_manifest(root)
    except Sam3GenerationError as exc:
        return classify_pipeline(
            annotation_status,
            sam3_state,
            0,
            0,
            False,
            inconsistencies=(str(exc),),
            validation_status="invalid",
        )
    if generation is not None:
        current_revision = entry.get("annotation_revision", 0)
        generation_revision = generation.get("annotation_revision")
        if (
            type(current_revision) is not int
            or type(generation_revision) is not int
            or generation_revision != current_revision
        ):
            return classify_pipeline(
                annotation_status,
                sam3_state,
                0,
                0,
                False,
                inconsistencies=(
                    "annotation_revision da geracao SAM3 diverge da anotacao atual",
                ),
                validation_status="invalid",
            )

    expected_frames = 0
    reviewed_frames = 0
    edited_frames = 0
    has_unprocessed_segments = False
    audit_required = False
    can_audit = True
    inconsistencies: list[str] = []
    expected_masks: list[_ExpectedMask] = []

    for segment_name in segments:
        try:
            segment = resolve_segment_dir(root, segment_name)
        except Sam3GenerationError as exc:
            inconsistencies.append(f"{segment_name}: {exc}")
            continue
        try:
            out = active_segment_output(segment)
        except Sam3GenerationError as exc:
            inconsistencies.append(f"{segment_name}: {exc}")
            continue
        run_path = out / "run.json"
        prompt_path = segment / "prompt.json"
        run = _read_json(run_path)
        prompt = _read_json(prompt_path)
        if run is None:
            # Antes da primeira propagacao ainda nao existe run.json. Isso e um
            # trabalho pronto para SAM3, nao um artefato corrompido. Um arquivo
            # presente mas ilegivel, por outro lado, representa uma tentativa
            # interrompida e precisa continuar visivel como invalida.
            if run_path.exists():
                inconsistencies.append(f"{segment_name}: run.json invalido")
            else:
                has_unprocessed_segments = True
                can_audit = False
            continue
        if "status" not in run:
            audit_required = True
        elif run.get("status") != "done":
            inconsistencies.append(f"{segment_name}: run SAM3 nao concluido")
            continue

        if "schema_version" not in run:
            audit_required = True
        elif _schema_int(run.get("schema_version")) != _SCHEMA_VERSION:
            inconsistencies.append(f"{segment_name}: schema de run.json desconhecido")
            continue

        if prompt is None:
            if prompt_path.exists():
                inconsistencies.append(f"{segment_name}: prompt.json invalido")
            else:
                audit_required = True
                can_audit = False
            continue
        if "schema_version" not in prompt:
            audit_required = True
        elif _schema_int(prompt.get("schema_version")) != _SCHEMA_VERSION:
            inconsistencies.append(f"{segment_name}: schema de prompt.json desconhecido")
            continue

        run_frame_count = _schema_int(run.get("frame_count"))
        prompt_frame_count = _schema_int(prompt.get("frame_count"))
        if "frame_count" in run and (
            run_frame_count is None or run_frame_count <= 0
        ):
            inconsistencies.append(f"{segment_name}: contagem de frames invalida")
            continue
        if "frame_count" in prompt and (
            prompt_frame_count is None or prompt_frame_count <= 0
        ):
            inconsistencies.append(f"{segment_name}: contagem de frames invalida")
            continue
        if (
            run_frame_count is not None
            and prompt_frame_count is not None
            and run_frame_count != prompt_frame_count
        ):
            inconsistencies.append(f"{segment_name}: contagem de frames divergente")
            continue
        frame_count = run_frame_count or prompt_frame_count
        if frame_count is None:
            audit_required = True
            can_audit = False
            continue
        if run_frame_count is None or prompt_frame_count is None:
            audit_required = True

        expected_frames += frame_count
        review_path = out / "mask_review.json"
        review = _read_json(review_path) if review_path.exists() else {
            "schema_version": _SCHEMA_VERSION,
            "frames": {},
        }
        if (
            review is None
            or review.get("schema_version") != _SCHEMA_VERSION
            or not isinstance(review.get("frames"), dict)
        ):
            inconsistencies.append(f"{segment_name}: mask_review.json invalido")
            continue
        frames = review["frames"]
        for frame in range(frame_count):
            reviewed = frames.get(str(frame)) or {}
            if reviewed.get("status") in {"ok", "edited"}:
                reviewed_frames += 1
                if reviewed.get("status") == "edited":
                    edited_frames += 1

        frames_written = _schema_int(run.get("frames_written"))
        if "frames_written" not in run:
            audit_required = True
        elif frames_written != frame_count:
            inconsistencies.append(f"{segment_name}: contagem de frames invalida")
            continue

        width = _schema_int(prompt.get("image_width"))
        height = _schema_int(prompt.get("image_height"))
        if "image_width" not in prompt or "image_height" not in prompt:
            audit_required = True
            can_audit = False
            continue
        if width is None or width <= 0 or height is None or height <= 0:
            inconsistencies.append(f"{segment_name}: dimensoes invalidas no prompt")
            continue

        effective_objects = prompt.get("objects")
        override_supplies_objects = False
        try:
            override_path = effective_prompt_override_path(segment)
        except Sam3GenerationError as exc:
            inconsistencies.append(f"{segment_name}: {exc}")
            continue
        if override_path is not None:
            override = _read_json(override_path)
            if override is None:
                inconsistencies.append(f"{segment_name}: prompt_override.json invalido")
                continue
            if override.get("objects"):
                effective_objects = override.get("objects")
                override_supplies_objects = True

        if "objects" not in prompt and not override_supplies_objects:
            audit_required = True
            can_audit = False
            continue
        prompt_object_ids = _object_ids(effective_objects)
        if prompt_object_ids is None:
            inconsistencies.append(f"{segment_name}: objetos invalidos no prompt")
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
                if "format" not in artifacts:
                    audit_required = True
                elif artifacts.get("format") != _ARTIFACT_FORMAT:
                    inconsistencies.append(
                        f"{segment_name}: formato de artefato desconhecido"
                    )
                if "files" not in artifacts:
                    audit_required = True
                elif _schema_int(artifacts.get("files")) != len(expected_keys):
                    inconsistencies.append(
                        f"{segment_name}: cardinalidade de artefatos invalida"
                    )
                if "checksums" not in artifacts:
                    audit_required = True
                else:
                    raw_checksums = artifacts.get("checksums")
                    if (
                        not isinstance(raw_checksums, dict)
                        or len(raw_checksums) != len(expected_keys)
                        or set(raw_checksums) != expected_keys
                        or any(
                            not isinstance(checksum, str)
                            or _SHA256.fullmatch(checksum) is None
                            for checksum in raw_checksums.values()
                        )
                    ):
                        inconsistencies.append(
                            f"{segment_name}: checksums de artefatos invalidos"
                        )
                    else:
                        checksums = raw_checksums

        if audit_masks:
            for relative in sorted(expected_keys):
                expected_masks.append(
                    _ExpectedMask(
                        str(segment_name),
                        out / Path(relative),
                        (width, height),
                        checksums.get(relative) if checksums is not None else None,
                    )
                )

    validation_status = "not_applicable"
    if inconsistencies:
        validation_status = "invalid"
    elif has_unprocessed_segments:
        validation_status = "not_applicable"
    elif audit_required:
        validation_status = "audit_required"
    elif expected_frames > 0:
        validation_status = "manifest"

    if (
        audit_masks
        and can_audit
        and validation_status in {"manifest", "audit_required"}
    ):
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
