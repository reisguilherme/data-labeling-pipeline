"""Immutable SAM3 generation layout and backwards-compatible readers."""

from __future__ import annotations

import json
import hashlib
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .masks import MaskValidationError, inspect_binary_png


SCHEMA_VERSION = 1
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_PUBLISHED_RUN_FIELDS = (
    "schema_version",
    "status",
    "runner_version",
    "prompt_digest",
    "frame_count",
    "frames_written",
    "objects",
    "params",
    "model",
    "artifacts",
    "error",
)
_PUBLISHED_RESULT_FIELDS = (
    "runner_version",
    "segments_total",
    "segments_done",
    "segments_skipped",
    "segments_failed",
    "frames",
    "classes",
)
_PUBLISHED_PARAM_FIELDS = (
    "runner_version",
    "mask_threshold",
    "min_box_px",
    "max_box_frac",
    "max_side",
    "mask_format",
    "model_id",
    "model_sha256",
    "sam3_commit",
)


class Sam3GenerationError(RuntimeError):
    """A generation pointer is malformed or escapes its video export root."""


def video_control_dir(export_root: Path) -> Path:
    return export_root / "_sam3"


def legacy_segment_output(segment_dir: Path) -> Path:
    return segment_dir / "_sam3"


def prompt_override_tombstone_path(segment_dir: Path) -> Path:
    return legacy_segment_output(segment_dir) / "prompt_override.cleared"


def current_manifest_path(export_root: Path) -> Path:
    return video_control_dir(export_root) / "current.json"


def staging_attempt_root(
    export_root: Path, generation_id: str, attempt_id: str
) -> Path:
    generation = _identifier(generation_id, field="generation_id")
    attempt = _identifier(attempt_id, field="attempt_id")
    return video_control_dir(export_root) / "staging" / generation / attempt


def staging_segment_output(
    export_root: Path, generation_id: str, attempt_id: str, segment: str
) -> Path:
    name = _identifier(segment, field="segment")
    return staging_attempt_root(export_root, generation_id, attempt_id) / name


def generation_root(export_root: Path, generation_id: str) -> Path:
    generation = _identifier(generation_id, field="generation_id")
    return video_control_dir(export_root) / "runs" / generation


def mask_object_key(checksum: str) -> str:
    digest = str(checksum).lower()
    if re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        raise Sam3GenerationError("checksum invalido para chave de mascara")
    return f"sha256/{digest[:2]}/{digest}.png"


def _identifier(value: object, *, field: str) -> str:
    text = str(value or "")
    if _IDENTIFIER.fullmatch(text) is None:
        raise Sam3GenerationError(f"{field} invalido no ponteiro SAM3")
    return text


def _iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="milliseconds")


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_json(path: Path, value: dict) -> bytes:
    payload = json.dumps(
        value, ensure_ascii=False, indent=2, sort_keys=True
    ).encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)
    return payload


def _read_object(path: Path, *, description: str) -> dict:
    if path.is_symlink() or not path.is_file():
        raise Sam3GenerationError(f"{description} ausente ou inseguro: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise Sam3GenerationError(f"{description} ilegivel: {path}") from exc
    if not isinstance(value, dict):
        raise Sam3GenerationError(f"{description} precisa ser um objeto JSON: {path}")
    return value


def _assert_confined_path(export_root: Path, candidate: Path, *, description: str) -> None:
    """Reject symlinked ancestors and lexical/resolved escapes from an export."""
    try:
        relative = candidate.relative_to(export_root)
    except ValueError as exc:
        raise Sam3GenerationError(f"{description} fora do export: {candidate}") from exc
    if export_root.is_symlink() or not export_root.is_dir():
        raise Sam3GenerationError(f"export ausente ou inseguro: {export_root}")
    root_resolved = export_root.resolve(strict=True)
    current = export_root
    for part in relative.parts:
        if current.is_symlink():
            raise Sam3GenerationError(f"symlink proibido em {description}: {current}")
        current = current / part
    if current.is_symlink():
        raise Sam3GenerationError(f"symlink proibido em {description}: {current}")
    try:
        resolved = candidate.resolve(strict=False)
    except OSError as exc:
        raise Sam3GenerationError(f"{description} inseguro: {candidate}") from exc
    if not resolved.is_relative_to(root_resolved):
        raise Sam3GenerationError(f"{description} escapa do export: {candidate}")


def resolve_export_root(output_root: Path, recorded_root: str | Path) -> Path:
    """Resolve a persisted export path without ever leaving an object's root."""
    base = Path(output_root)
    if base.is_symlink() or not base.is_dir():
        raise Sam3GenerationError(f"raiz de saida ausente ou insegura: {base}")

    raw = Path(recorded_root)
    portable_name = Path(str(recorded_root).replace("\\", "/")).name
    candidates = [raw if raw.is_absolute() else base / raw]
    if portable_name:
        candidates.append(base / portable_name)

    unsafe = False
    seen: set[str] = set()
    for candidate in candidates:
        key = str(candidate)
        if key in seen:
            continue
        seen.add(key)
        if not candidate.is_dir():
            continue
        try:
            relative = candidate.relative_to(base)
            if not relative.parts:
                raise Sam3GenerationError("o export nao pode ser a raiz do objeto")
            _assert_confined_path(base, candidate, description="export do video")
        except (ValueError, Sam3GenerationError):
            unsafe = True
            continue
        return candidate

    if unsafe:
        raise Sam3GenerationError(
            f"export registrado fora da raiz do objeto: {recorded_root}"
        )
    raise Sam3GenerationError(f"pasta do export nao existe: {recorded_root}")


def resolve_segment_dir(export_root: Path, segment: object) -> Path:
    """Resolve one declared segment without accepting traversal or symlinks."""
    name = _identifier(segment, field="segment")
    candidate = Path(export_root) / name
    _assert_confined_path(Path(export_root), candidate, description="segmento")
    if candidate.is_symlink() or not candidate.is_dir():
        raise Sam3GenerationError(f"segmento ausente ou inseguro: {candidate}")
    return candidate


def _model_identity(value: object, *, description: str) -> dict[str, str]:
    if not isinstance(value, dict):
        raise Sam3GenerationError(f"{description} ausente")
    model_id = str(value.get("model_id") or "")
    checkpoint = str(value.get("checkpoint_sha256") or "").lower()
    commit = str(value.get("sam3_commit") or "").lower()
    if not model_id:
        raise Sam3GenerationError(f"model_id ausente em {description}")
    if re.fullmatch(r"[0-9a-f]{64}", checkpoint) is None:
        raise Sam3GenerationError(f"checkpoint_sha256 invalido em {description}")
    if re.fullmatch(r"[0-9a-f]{40}", commit) is None:
        raise Sam3GenerationError(f"sam3_commit invalido em {description}")
    return {
        "model_id": model_id,
        "checkpoint_sha256": checkpoint,
        "sam3_commit": commit,
    }


def _assert_expected_model(actual: dict[str, str], expected: dict | None) -> None:
    if not expected:
        return
    for field, actual_value in actual.items():
        expected_value = expected.get(field)
        if expected_value is not None and str(expected_value).lower() != actual_value.lower():
            raise Sam3GenerationError(
                f"identidade do modelo diverge em {field}: "
                f"esperado={expected_value!r} recebido={actual_value!r}"
            )


def effective_prompt_override_path(
    segment_dir: Path, *, migrate_legacy: bool = False
) -> Path | None:
    """Resolve the mutable control override, with a one-way legacy migration.

    A short-lived layout placed ``prompt_override.json`` inside the published
    run.  Readers may recover it, but only by copying it into the segment
    control directory; the published generation is never rewritten.
    """
    control = legacy_segment_output(segment_dir) / "prompt_override.json"
    tombstone = prompt_override_tombstone_path(segment_dir)
    if tombstone.is_symlink():
        raise Sam3GenerationError(f"tombstone do override inseguro: {tombstone}")
    if tombstone.exists():
        if not tombstone.is_file():
            raise Sam3GenerationError(f"tombstone do override invalido: {tombstone}")
        return None
    if control.is_symlink():
        raise Sam3GenerationError(f"override do prompt inseguro: {control}")
    if control.is_file():
        return control
    manifest = load_current_manifest(segment_dir.parent)
    if manifest is None:
        return None
    # A re-export may add a segment before a replacement SAM3 generation is
    # available. Absence from the prior pointer means there is no legacy
    # override to recover; it is not pointer corruption.
    raw_entry = manifest["segments"].get(segment_dir.name)
    if not isinstance(raw_entry, dict):
        return None
    published, _ = _segment_output_from_manifest(segment_dir, manifest)
    candidate = published / "prompt_override.json"
    if candidate == control or not candidate.exists():
        return None
    if candidate.is_symlink() or not candidate.is_file():
        raise Sam3GenerationError(f"override legado inseguro: {candidate}")
    try:
        payload = candidate.read_bytes()
        prompt_bytes = (segment_dir / "prompt.json").read_bytes()
    except OSError:
        return None
    expected_digest = str(raw_entry.get("prompt_digest") or "").lower()
    if re.fullmatch(r"[0-9a-f]{64}", expected_digest) is None:
        marker = _read_object(
            published / "run.json", description="run SAM3 legado"
        )
        expected_digest = str(marker.get("prompt_digest") or "").lower()
    if (
        re.fullmatch(r"[0-9a-f]{64}", expected_digest) is None
        or hashlib.sha256(prompt_bytes + payload).hexdigest() != expected_digest
    ):
        # The export/prompt changed after this generation. Reusing its override
        # would silently apply a stale box to a different annotation revision.
        return None
    if not migrate_legacy:
        return candidate

    try:
        control.parent.mkdir(parents=True, exist_ok=True)
        temporary = control.with_name(
            f".{control.name}.{uuid.uuid4().hex}.migrate"
        )
        with temporary.open("wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            # link(temp, target) is an atomic create-if-absent. Unlike linking
            # the legacy file directly, this gives control its own inode.
            os.link(temporary, control)
        except FileExistsError:
            pass
        finally:
            temporary.unlink(missing_ok=True)
    except OSError:
        # Read compatibility remains available even on filesystems without
        # hard-link support; a later explicit save will migrate atomically.
        return candidate
    return control if control.is_file() and not control.is_symlink() else candidate


def _prompt_and_digest(segment_dir: Path) -> tuple[dict, str]:
    prompt_path = segment_dir / "prompt.json"
    if prompt_path.is_symlink() or not prompt_path.is_file():
        raise Sam3GenerationError(f"prompt ausente ou inseguro: {prompt_path}")
    raw = prompt_path.read_bytes()
    try:
        prompt = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise Sam3GenerationError(f"prompt ilegivel: {prompt_path}") from exc
    if not isinstance(prompt, dict):
        raise Sam3GenerationError(f"prompt invalido: {prompt_path}")
    override_path = effective_prompt_override_path(
        segment_dir, migrate_legacy=True
    )
    if override_path is not None:
        if override_path.is_symlink() or not override_path.is_file():
            raise Sam3GenerationError(
                f"override do prompt ausente ou inseguro: {override_path}"
            )
        try:
            override_raw = override_path.read_bytes()
            override = json.loads(override_raw.decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise Sam3GenerationError(
                f"override do prompt ilegivel: {override_path}"
            ) from exc
        if not isinstance(override, dict):
            raise Sam3GenerationError(
                f"override do prompt precisa ser um objeto JSON: {override_path}"
            )
        if override.get("objects"):
            prompt = {**prompt, "objects": override["objects"]}
            raw += override_raw
    return prompt, hashlib.sha256(raw).hexdigest()


def _validate_segment(
    *,
    source_dir: Path,
    output_dir: Path,
    summary: dict,
    model_identity: dict[str, str],
) -> dict:
    if source_dir.is_symlink() or not source_dir.is_dir():
        raise Sam3GenerationError(f"segmento fonte ausente ou inseguro: {source_dir}")
    if output_dir.is_symlink() or not output_dir.is_dir():
        raise Sam3GenerationError(f"staging do segmento ausente ou inseguro: {output_dir}")
    if any(path.is_symlink() for path in output_dir.rglob("*")):
        raise Sam3GenerationError(f"symlink dentro do staging SAM3: {output_dir}")
    if {path.name for path in output_dir.iterdir()} != {
        "run.json",
        "masks",
        "labels",
    }:
        raise Sam3GenerationError(
            f"estrutura inesperada no staging SAM3: {output_dir}"
        )
    marker = _read_object(output_dir / "run.json", description="run SAM3")
    if marker.get("schema_version") != 1 or marker.get("status") != "done":
        raise Sam3GenerationError(f"run SAM3 nao concluido em {output_dir}")
    prompt, digest = _prompt_and_digest(source_dir)
    if marker.get("prompt_digest") != digest:
        raise Sam3GenerationError(
            f"digest do prompt diverge em {source_dir.name}"
        )
    marker_model = _model_identity(
        marker.get("model"), description=f"run {source_dir.name}"
    )
    if marker_model != model_identity:
        raise Sam3GenerationError(
            f"modelo do segmento {source_dir.name} diverge do job"
        )

    try:
        frame_count = int(prompt["frame_count"])
        width = int(prompt["image_width"])
        height = int(prompt["image_height"])
        object_ids = [int(item["obj_id"]) for item in prompt.get("objects") or []]
    except (KeyError, TypeError, ValueError) as exc:
        raise Sam3GenerationError(f"prompt estruturalmente invalido: {source_dir}") from exc
    if frame_count <= 0 or width <= 0 or height <= 0 or not object_ids:
        raise Sam3GenerationError(f"prompt estruturalmente invalido: {source_dir}")
    if len(set(object_ids)) != len(object_ids):
        raise Sam3GenerationError(f"obj_id repetido no prompt: {source_dir}")
    if type(marker.get("frame_count")) is not int or marker["frame_count"] != frame_count:
        raise Sam3GenerationError(f"frame_count divergente em {source_dir.name}")
    if (
        type(marker.get("frames_written")) is not int
        or marker["frames_written"] != frame_count
    ):
        raise Sam3GenerationError(f"labels incompletos em {source_dir.name}")

    prompt_objects = {
        int(item["obj_id"]): str(item.get("label") or "")
        for item in prompt.get("objects") or []
    }
    marker_objects = marker.get("objects")
    if not isinstance(marker_objects, list) or len(marker_objects) != len(prompt_objects):
        raise Sam3GenerationError(f"objetos do run divergem em {source_dir.name}")
    seen_objects: set[int] = set()
    class_by_label: dict[str, int] = {}
    label_by_class: dict[int, str] = {}
    for item in marker_objects:
        if not isinstance(item, dict) or type(item.get("obj_id")) is not int:
            raise Sam3GenerationError(f"objeto invalido no run {source_dir.name}")
        obj_id = item["obj_id"]
        label = str(item.get("label") or "")
        class_index = item.get("class_index")
        if (
            obj_id in seen_objects
            or prompt_objects.get(obj_id) != label
            or type(class_index) is not int
            or class_index < 0
            or (label in class_by_label and class_by_label[label] != class_index)
            or (class_index in label_by_class and label_by_class[class_index] != label)
        ):
            raise Sam3GenerationError(f"objetos do run divergem em {source_dir.name}")
        seen_objects.add(obj_id)
        class_by_label[label] = class_index
        label_by_class[class_index] = label
    if seen_objects != set(prompt_objects):
        raise Sam3GenerationError(f"objetos do run divergem em {source_dir.name}")

    expected_keys = {
        f"masks/{obj_id}/{frame_idx:06d}.png"
        for obj_id in object_ids
        for frame_idx in range(frame_count)
    }
    masks_dir = output_dir / "masks"
    labels_dir = output_dir / "labels"
    actual_mask_files = {
        path.relative_to(output_dir).as_posix()
        for path in masks_dir.rglob("*")
        if path.is_file()
    }
    actual_label_files = {
        path.relative_to(labels_dir).as_posix()
        for path in labels_dir.rglob("*")
        if path.is_file()
    }
    expected_labels = {f"{frame_idx:06d}.txt" for frame_idx in range(frame_count)}
    if actual_mask_files != expected_keys or actual_label_files != expected_labels:
        raise Sam3GenerationError(
            f"estrutura de artefatos diverge em {source_dir.name}"
        )
    artifacts = marker.get("artifacts")
    if not isinstance(artifacts, dict) or artifacts.get("format") != "png-1bit-v1":
        raise Sam3GenerationError(f"manifesto de mascaras invalido em {source_dir.name}")
    checksums = artifacts.get("checksums")
    if (
        not isinstance(checksums, dict)
        or set(checksums) != expected_keys
        or type(artifacts.get("files")) is not int
        or artifacts["files"] != len(expected_keys)
        or type(artifacts.get("empty")) is not int
        or artifacts["empty"] < 0
    ):
        raise Sam3GenerationError(f"cardinalidade de mascaras invalida em {source_dir.name}")
    empty_masks = 0
    for relative, expected_checksum in checksums.items():
        if re.fullmatch(r"[0-9a-f]{64}", str(expected_checksum)) is None:
            raise Sam3GenerationError(f"checksum invalido para {relative}")
        path = output_dir / Path(relative)
        if path.is_symlink() or not path.is_file():
            raise Sam3GenerationError(f"mascara ausente ou insegura: {relative}")
        try:
            info = inspect_binary_png(
                path.read_bytes(), expected_size=(width, height)
            )
        except (OSError, MaskValidationError) as exc:
            raise Sam3GenerationError(f"mascara invalida {relative}: {exc}") from exc
        if info.sha256 != expected_checksum:
            raise Sam3GenerationError(f"checksum divergente para {relative}")
        if info.area_pixels == 0:
            empty_masks += 1
    if artifacts["empty"] != empty_masks:
        raise Sam3GenerationError(
            f"contagem de mascaras vazias diverge em {source_dir.name}"
        )
    for frame_idx in range(frame_count):
        label = output_dir / "labels" / f"{frame_idx:06d}.txt"
        if label.is_symlink() or not label.is_file():
            raise Sam3GenerationError(f"label ausente ou inseguro: {label}")

    # The callback is a claim, while run.json plus mask bytes are evidence.
    # Require all identity-bearing callback fields to match that evidence.
    for field in ("prompt_digest", "frame_count", "frames_written"):
        if summary.get(field) != marker.get(field):
            raise Sam3GenerationError(
                f"resumo do worker diverge do run.json em {source_dir.name}: {field}"
            )
    if summary.get("artifacts") != artifacts:
        raise Sam3GenerationError(
            f"resumo de artefatos diverge do run.json em {source_dir.name}"
        )
    if _model_identity(summary.get("model"), description="resumo do worker") != model_identity:
        raise Sam3GenerationError(
            f"modelo do resumo diverge em {source_dir.name}"
        )
    return marker


def _freeze_raw_generation(root: Path) -> None:
    for directory_name in ("masks", "labels"):
        directory = root / directory_name
        if not directory.exists():
            continue
        for path in directory.rglob("*"):
            if path.is_symlink():
                raise Sam3GenerationError(f"symlink em geracao SAM3: {path}")
            if path.is_file():
                path.chmod(0o444)
        for path in sorted(
            (candidate for candidate in directory.rglob("*") if candidate.is_dir()),
            key=lambda item: len(item.parts),
            reverse=True,
        ):
            path.chmod(0o555)
        directory.chmod(0o555)
    marker = root / "run.json"
    if marker.is_file():
        marker.chmod(0o444)


def _publish_pointer(export_root: Path, generation: dict) -> None:
    pointer = {
        "schema_version": SCHEMA_VERSION,
        "format": "immutable-generation-v1",
        "generation_id": generation["generation_id"],
        "annotation_revision": generation["annotation_revision"],
        "model": generation["model"],
        "manifest_sha256": generation["manifest_sha256"],
        "published_at": generation["published_at"],
        "segments": {
            item["segment"]: {
                "path": (
                    Path("runs")
                    / generation["generation_id"]
                    / item["segment"]
                ).as_posix(),
                "prompt_digest": item["prompt_digest"],
            }
            for item in generation["result"]["segment_runs"]
        },
    }
    target = current_manifest_path(export_root)
    _assert_confined_path(export_root, target, description="ponteiro SAM3")
    _atomic_json(target, pointer)


def _generation_manifest_digest(generation: dict) -> str:
    canonical = json.loads(json.dumps(generation))
    canonical.pop("manifest_sha256", None)
    result = canonical.get("result")
    if isinstance(result, dict):
        result.pop("publication", None)
    payload = json.dumps(
        canonical, ensure_ascii=False, sort_keys=True
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _validate_published_generation(
    export_root: Path, generation: dict, expected_segments: list[str]
) -> None:
    if generation.get("manifest_sha256") != _generation_manifest_digest(generation):
        raise Sam3GenerationError("checksum do manifesto da geracao diverge")
    result = generation.get("result")
    if not isinstance(result, dict):
        raise Sam3GenerationError("resultado ausente no manifesto da geracao")
    model_identity = _model_identity(
        generation.get("model"), description="manifesto da geracao"
    )
    summaries = result.get("segment_runs")
    if not isinstance(summaries, list):
        raise Sam3GenerationError("segment_runs ausente no manifesto da geracao")
    by_name = {
        str(item.get("segment") or ""): item
        for item in summaries
        if isinstance(item, dict)
    }
    if set(by_name) != set(expected_segments) or len(summaries) != len(expected_segments):
        raise Sam3GenerationError("segmentos publicados divergem do job")
    for name in expected_segments:
        summary = by_name[name]
        expected_output = generation_root(
            export_root, str(generation["generation_id"])
        ) / name
        _validate_segment(
            source_dir=export_root / name,
            output_dir=expected_output,
            summary=summary,
            model_identity=model_identity,
        )


def publish_generation(
    *,
    export_root: Path,
    segments: list[str],
    generation_id: str,
    attempt_id: str,
    annotation_revision: int,
    expected_model: dict | None,
    worker_result: dict,
) -> dict:
    """Validate one live attempt and atomically switch the video pointer.

    Staging is lease-private. Published paths are append-only; a retry can
    reactivate the same generation but can never replace its bytes.
    """
    generation = _identifier(generation_id, field="generation_id")
    attempt = _identifier(attempt_id, field="attempt_id")
    if worker_result.get("run_id") != generation:
        raise Sam3GenerationError("run_id do resultado diverge do job")
    raw_revision = worker_result.get("annotation_revision")
    if type(raw_revision) is not int or raw_revision != annotation_revision:
        raise Sam3GenerationError("annotation_revision do resultado diverge do job")
    model_identity = _model_identity(
        worker_result.get("model"), description="resultado do worker"
    )
    _assert_expected_model(model_identity, expected_model)

    names = [_identifier(value, field="segment") for value in segments]
    if len(set(names)) != len(names):
        raise Sam3GenerationError("segmentos repetidos no job SAM3")
    summaries = worker_result.get("segment_runs")
    if not isinstance(summaries, list):
        raise Sam3GenerationError("segment_runs ausente no resultado")
    by_name = {
        str(item.get("segment") or ""): item
        for item in summaries
        if isinstance(item, dict)
    }
    if set(by_name) != set(names) or len(summaries) != len(names):
        raise Sam3GenerationError("segment_runs diverge dos segmentos do job")

    final_root = generation_root(export_root, generation)
    _assert_confined_path(export_root, final_root, description="geracao SAM3")
    generation_manifest_path = final_root / "generation.json"
    if final_root.exists():
        existing = _read_object(
            generation_manifest_path, description="manifesto da geracao SAM3"
        )
        existing_revision = existing.get("annotation_revision")
        if (
            existing.get("generation_id") != generation
            or type(existing_revision) is not int
            or existing_revision != annotation_revision
            or existing.get("model") != model_identity
        ):
            raise Sam3GenerationError(
                f"generation_id {generation} ja publicado com outra identidade"
            )
        _validate_published_generation(export_root, existing, names)
        for name in names:
            _freeze_raw_generation(final_root / name)
        generation_manifest_path.chmod(0o444)
        _publish_pointer(export_root, existing)
        return dict(existing["result"])

    stage_root = staging_attempt_root(export_root, generation, attempt)
    _assert_confined_path(export_root, stage_root, description="staging SAM3")
    if stage_root.is_symlink() or not stage_root.is_dir():
        raise Sam3GenerationError(f"staging ausente ou inseguro: {stage_root}")

    normalized_segments: list[dict] = []
    for name in names:
        source_dir = resolve_segment_dir(export_root, name)
        stage_dir = staging_segment_output(export_root, generation, attempt, name)
        summary = by_name[name]
        if summary.get("staging_dir") != str(stage_dir):
            raise Sam3GenerationError(f"staging_dir divergente em {name}")
        if summary.get("dir") != str(source_dir):
            raise Sam3GenerationError(f"source dir divergente em {name}")
        marker = _validate_segment(
            source_dir=source_dir,
            output_dir=stage_dir,
            summary=summary,
            model_identity=model_identity,
        )
        normalized_marker = {
            key: marker[key]
            for key in _PUBLISHED_RUN_FIELDS
            if key in marker
            and key not in {"model", "objects", "params", "artifacts"}
        }
        normalized_marker.update(
            {
                "model": model_identity,
                "objects": [
                    {
                        key: item[key]
                        for key in ("obj_id", "label", "class_index")
                        if key in item
                    }
                    for item in marker["objects"]
                ],
                "params": {
                    key: marker["params"][key]
                    for key in _PUBLISHED_PARAM_FIELDS
                    if isinstance(marker.get("params"), dict)
                    and key in marker["params"]
                },
                "artifacts": {
                    key: marker["artifacts"][key]
                    for key in ("format", "files", "empty", "checksums")
                },
            }
        )
        normalized_segments.append(
            {
                **normalized_marker,
                # The immutable manifest must survive moving the workspace to
                # another host. Paths are derived from export_root and this
                # server-controlled segment identity when read.
                "segment": name,
            }
        )

    normalized_result = {
        **{
            key: worker_result[key]
            for key in _PUBLISHED_RESULT_FIELDS
            if key in worker_result
        },
        "run_id": generation,
        "annotation_revision": annotation_revision,
        "model": model_identity,
        "segment_runs": normalized_segments,
    }
    generation_manifest = {
        "schema_version": SCHEMA_VERSION,
        "generation_id": generation,
        "annotation_revision": annotation_revision,
        "model": model_identity,
        "published_at": _iso(),
        "result": normalized_result,
    }
    generation_manifest["manifest_sha256"] = _generation_manifest_digest(
        generation_manifest
    )
    normalized_result["publication"] = {
        "generation_id": generation,
        "annotation_revision": annotation_revision,
        "manifest_sha256": generation_manifest["manifest_sha256"],
    }
    _atomic_json(stage_root / "generation.json", generation_manifest)

    final_root.parent.mkdir(parents=True, exist_ok=True)
    _fsync_directory(final_root.parent.parent)
    _assert_confined_path(export_root, final_root, description="geracao SAM3")
    try:
        os.replace(stage_root, final_root)
        _fsync_directory(final_root.parent)
    except OSError:
        # Another completion of the same fenced job won the rename. Its
        # immutable manifest is authoritative and is checked on the retry.
        if not final_root.exists():
            raise
        return publish_generation(
            export_root=export_root,
            segments=segments,
            generation_id=generation,
            attempt_id=attempt,
            annotation_revision=annotation_revision,
            expected_model=expected_model,
            worker_result=worker_result,
        )
    for name in names:
        _freeze_raw_generation(final_root / name)
    _validate_published_generation(export_root, generation_manifest, names)
    (final_root / "generation.json").chmod(0o444)
    _publish_pointer(export_root, generation_manifest)
    return normalized_result


def load_current_manifest(export_root: Path) -> dict | None:
    path = current_manifest_path(export_root)
    if not path.exists():
        return None
    _assert_confined_path(export_root, path, description="ponteiro SAM3")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise Sam3GenerationError(f"ponteiro SAM3 ilegivel: {path}") from exc
    if not isinstance(data, dict) or data.get("schema_version") != SCHEMA_VERSION:
        raise Sam3GenerationError(f"schema invalido no ponteiro SAM3: {path}")
    _identifier(data.get("generation_id"), field="generation_id")
    if not isinstance(data.get("segments"), dict):
        raise Sam3GenerationError(f"segments invalido no ponteiro SAM3: {path}")
    if data.get("format") == "immutable-generation-v1":
        generation_id = _identifier(data.get("generation_id"), field="generation_id")
        manifest_path = generation_root(export_root, generation_id) / "generation.json"
        _assert_confined_path(
            export_root, manifest_path, description="manifesto da geracao SAM3"
        )
        generation = _read_object(
            manifest_path, description="manifesto da geracao SAM3"
        )
        if generation.get("manifest_sha256") != _generation_manifest_digest(generation):
            raise Sam3GenerationError("checksum do manifesto da geracao diverge")
        for field in (
            "generation_id",
            "annotation_revision",
            "model",
            "manifest_sha256",
        ):
            if data.get(field) != generation.get(field):
                raise Sam3GenerationError(
                    f"ponteiro SAM3 diverge do manifesto em {field}"
                )
        summaries = (generation.get("result") or {}).get("segment_runs")
        if not isinstance(summaries, list):
            raise Sam3GenerationError("segment_runs ausente no manifesto da geracao")
        expected_segments = {
            str(item.get("segment") or ""): {
                "path": (
                    Path("runs") / generation_id / str(item.get("segment") or "")
                ).as_posix(),
                "prompt_digest": item.get("prompt_digest"),
            }
            for item in summaries
            if isinstance(item, dict)
        }
        if data["segments"] != expected_segments:
            raise Sam3GenerationError("segmentos do ponteiro divergem do manifesto")
    return data


def _segment_output_from_manifest(segment_dir: Path, manifest: dict) -> tuple[Path, dict]:
    export_root = segment_dir.parent
    segment_name = _identifier(segment_dir.name, field="segment")
    generation_id = _identifier(manifest.get("generation_id"), field="generation_id")
    raw_entry = manifest["segments"].get(segment_name)
    if not isinstance(raw_entry, dict):
        raise Sam3GenerationError(
            f"segmento {segment_name} ausente no ponteiro SAM3"
        )
    expected_relative = Path("runs") / generation_id / segment_name
    if raw_entry.get("path") != expected_relative.as_posix():
        raise Sam3GenerationError(
            f"caminho inesperado para {segment_name} no ponteiro SAM3"
        )
    control = video_control_dir(export_root)
    target = control / expected_relative
    current = control
    for part in expected_relative.parts:
        if current.is_symlink():
            raise Sam3GenerationError(
                f"symlink proibido na geracao publicada: {current}"
            )
        current = current / part
    if current.is_symlink():
        raise Sam3GenerationError(
            f"symlink proibido na geracao publicada: {current}"
        )
    _assert_confined_path(export_root, target, description="geracao publicada")
    if not target.is_dir():
        raise Sam3GenerationError(
            f"geracao publicada ausente para {segment_name}: {target}"
        )
    return target, raw_entry


def active_segment_output(segment_dir: Path) -> Path:
    """Resolve the published output, falling back only when no pointer exists."""
    export_root = segment_dir.parent
    manifest = load_current_manifest(export_root)
    if manifest is None:
        return legacy_segment_output(segment_dir)

    segment_name = _identifier(segment_dir.name, field="segment")
    target, raw_entry = _segment_output_from_manifest(segment_dir, manifest)
    expected_digest = str(raw_entry.get("prompt_digest") or "").lower()
    if re.fullmatch(r"[0-9a-f]{64}", expected_digest) is not None:
        prompt_path = segment_dir / "prompt.json"
        if prompt_path.is_symlink() or not prompt_path.is_file():
            raise Sam3GenerationError(f"prompt ausente ou inseguro: {prompt_path}")
        try:
            prompt_bytes = prompt_path.read_bytes()
        except OSError as exc:
            raise Sam3GenerationError(f"prompt ilegivel: {prompt_path}") from exc
        override_path = effective_prompt_override_path(segment_dir)
        if override_path is not None:
            try:
                override_raw = override_path.read_bytes()
                override = json.loads(override_raw.decode("utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise Sam3GenerationError(
                    f"override do prompt ilegivel: {override_path}"
                ) from exc
            if not isinstance(override, dict):
                raise Sam3GenerationError(
                    f"override efetivo precisa ser um objeto: {override_path}"
                )
            if override.get("objects"):
                prompt_bytes += override_raw
        if hashlib.sha256(prompt_bytes).hexdigest() != expected_digest:
            raise Sam3GenerationError(
                f"prompt atual diverge da geracao publicada em {segment_name}"
            )
    return target
