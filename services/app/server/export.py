"""Export dos segmentos: os frames e o prompt.json que alimentam o SAM3.

Esta é a fronteira do contrato com o pipeline em ../sam3-annotation-tool.
Ver `_NAMING_RATIONALE` antes de mudar a numeração dos arquivos.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import stat
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator
from uuid import uuid4

from . import ffmpeg
from .config import EXPORT_QSCALE
from .jobs import Job, jobs
from .videos import VideoFile, export_folder_name, iso

log = logging.getLogger("movies-screening-tool.export")

# Por que os arquivos reiniciam em 000000.jpg em cada seg_XX:
#
# `add_new_points_or_box` recebe `frame_idx` como ÍNDICE POSICIONAL dentro de
# `list_frame_names()`. Reiniciando do zero temos
# `número no nome == índice posicional == frame_idx`; o prompt pode apontar para
# qualquer índice local via `prompt_frame_idx`. Se usássemos o
# índice original do vídeo, o primeiro arquivo da pasta seria 001204.jpg enquanto
# o SAM3 o chama de frame 0 — armadilha permanente de off-by-1204 para qualquer
# leitor futuro, e exatamente o que produz máscaras alinhadas ao frame errado.
#
# A rastreabilidade fica preservada e explícita no prompt.json:
#   original_frame = source_start_frame + local_index
_NAMING_RATIONALE = "restart_per_segment"

PROMPT_SCHEMA_VERSION = 1
EXPORT_OWNER_SCHEMA_VERSION = 1
EXPORT_OWNER_MARKER = ".export-owner.json"
EXPORT_TRANSACTION_SCHEMA_VERSION = 1
EXPORT_TRANSACTION_SUFFIX = ".export-transaction.json"
EXPORT_PUBLISH_LOCK_SUFFIX = ".export-publish.lock"
_MAX_TRANSACTION_BYTES = 64 * 1024

_SAM3_USAGE = (
    "for o in objects: predictor.add_new_points_or_box("
    "inference_state=st, frame_idx=prompt_frame_idx, obj_id=o['obj_id'], "
    "box=torch.tensor([o['box_normalized']], dtype=torch.float32), clear_old_points=True)"
)


def _lexical(path: Path) -> Path:
    """Absolute path without resolving links (needed for confinement checks)."""
    return Path(os.path.abspath(os.fspath(path)))


def _regular_directory(path: Path) -> bool:
    try:
        info = path.lstat()
    except OSError:
        return False
    return stat.S_ISDIR(info.st_mode) and not path.is_symlink()


def _regular_file(path: Path) -> bool:
    try:
        info = path.lstat()
    except OSError:
        return False
    return stat.S_ISREG(info.st_mode) and not path.is_symlink()


def _validate_tree_entries(root: Path, label: str) -> None:
    """Walk without following links and accept only regular files/directories."""
    pending = [root]
    while pending:
        directory = pending.pop()
        if directory.is_symlink() or not _regular_directory(directory):
            raise ValueError(f"{label} contem symlink ou diretorio inseguro")
        with os.scandir(directory) as entries:
            for entry in entries:
                if entry.is_symlink():
                    raise ValueError(f"{label} contem symlink")
                if entry.is_dir(follow_symlinks=False):
                    pending.append(Path(entry.path))
                elif not entry.is_file(follow_symlinks=False):
                    raise ValueError(f"{label} contem entrada especial")


def _reject_symlink_chain(path: Path, label: str) -> None:
    """Reject every existing component without first following a symlink."""
    absolute = _lexical(path)
    parts = absolute.parts
    if not parts:
        raise ValueError(f"{label} invalido")
    current = Path(parts[0])
    if current.is_symlink():
        raise ValueError(f"{label} nao pode conter symlink")
    for part in parts[1:]:
        current = current / part
        if current.is_symlink():
            raise ValueError(f"{label} nao pode conter symlink")


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _plain_name(value: object) -> str | None:
    if (
        not isinstance(value, str)
        or not value
        or value in {".", ".."}
        or "/" in value
        or "\\" in value
        or Path(value).name != value
    ):
        return None
    return value


def _journal_path(root: Path) -> Path:
    return root.parent / f".{root.name}{EXPORT_TRANSACTION_SUFFIX}"


def _publish_lock_path(root: Path) -> Path:
    return root.parent / f".{root.name}{EXPORT_PUBLISH_LOCK_SUFFIX}"


def _validate_root_location(root: Path) -> None:
    if _plain_name(root.name) is None or ".." in root.parts:
        raise ValueError("raiz de export invalida")
    _reject_symlink_chain(root.parent, "ancestral da raiz de export")
    if not _regular_directory(root.parent):
        raise ValueError("pasta pai da raiz de export invalida")
    if root.is_symlink() or (root.exists() and not _regular_directory(root)):
        raise ValueError("raiz de export invalida")


def _validate_publish_paths(root: Path, staging: Path, trash: Path) -> None:
    _validate_root_location(root)
    expected_parent = _lexical(root.parent)
    staging_name = _plain_name(staging.name)
    if (
        staging_name is None
        or ".." in staging.parts
        or _lexical(staging).parent != expected_parent
        or not staging_name.startswith(f".{root.name}.export-")
        or not staging_name.endswith(".part")
    ):
        raise ValueError("staging de export fora da raiz permitida")
    _reject_symlink_chain(staging.parent, "ancestral do staging")
    if staging.is_symlink() or not _regular_directory(staging):
        raise ValueError("staging de export invalido")

    trash_name = _plain_name(trash.name)
    if (
        trash_name is None
        or ".." in trash.parts
        or _lexical(trash.parent) != _lexical(root)
        or not trash_name.startswith(".replace-")
        or not trash_name.endswith(".trash")
    ):
        raise ValueError("lixeira de publicacao invalida")
    if trash.is_symlink() or (trash.exists() and not _regular_directory(trash)):
        raise ValueError("lixeira de publicacao invalida")


@contextmanager
def _exclusive_publish_lock(root: Path) -> Iterator[None]:
    """Cross-process lock released by the OS even after abrupt termination."""
    _validate_root_location(root)
    lock = _publish_lock_path(root)
    if lock.is_symlink() or (lock.exists() and not _regular_file(lock)):
        raise ValueError("arquivo de lock de export nao pode ser symlink")
    with lock.open("a+b") as handle:
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


def export_owner(ctx, video: VideoFile, annotation_revision: int) -> dict:
    if type(annotation_revision) is not int or annotation_revision < 0:
        raise ValueError("annotation_revision invalida")
    return {
        "schema_version": EXPORT_OWNER_SCHEMA_VERSION,
        "object_id": ctx.object_id,
        "video_id": video.video_id,
        "relpath": video.relpath,
        "annotation_revision": annotation_revision,
    }


def valid_export_owner(value: object) -> dict | None:
    if not isinstance(value, dict):
        return None
    revision = value.get("annotation_revision")
    if (
        value.get("schema_version") != EXPORT_OWNER_SCHEMA_VERSION
        or not isinstance(value.get("object_id"), str)
        or not value["object_id"]
        or not isinstance(value.get("video_id"), str)
        or not value["video_id"]
        or not isinstance(value.get("relpath"), str)
        or not value["relpath"]
        or type(revision) is not int
        or revision < 0
    ):
        return None
    return {
        "schema_version": EXPORT_OWNER_SCHEMA_VERSION,
        "object_id": value["object_id"],
        "video_id": value["video_id"],
        "relpath": value["relpath"],
        "annotation_revision": revision,
    }


def same_export_identity(left: object, right: object) -> bool:
    first = valid_export_owner(left)
    second = valid_export_owner(right)
    if first is None or second is None:
        return False
    return all(
        first[key] == second[key]
        for key in ("schema_version", "object_id", "video_id", "relpath")
    )


def read_export_owner(root: Path) -> dict | None:
    marker = root / EXPORT_OWNER_MARKER
    if root.is_symlink() or marker.is_symlink() or not marker.is_file():
        return None
    try:
        # O marker tem menos de 1 KiB. O limite evita transformar metadado
        # corrompido em leitura arbitrariamente grande no request/worker.
        if marker.stat().st_size > 16_384:
            return None
        return valid_export_owner(json.loads(marker.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None


def write_export_owner(root: Path, owner: dict) -> None:
    normalized = valid_export_owner(owner)
    if normalized is None:
        raise ValueError("owner de export invalido")
    if root.is_symlink() or (root.exists() and not root.is_dir()):
        raise ValueError("raiz de export invalida")
    root.mkdir(parents=True, exist_ok=True)
    marker = root / EXPORT_OWNER_MARKER
    if marker.is_symlink():
        raise ValueError("marker de ownership nao pode ser link simbolico")
    temporary = root / f".{EXPORT_OWNER_MARKER}.{uuid4().hex}.tmp"
    try:
        with temporary.open("xb") as stream:
            stream.write(
                json.dumps(normalized, ensure_ascii=False, sort_keys=True).encode("utf-8")
            )
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, marker)
        _fsync_directory(root)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _write_json_transaction(path: Path, payload: dict) -> None:
    if path.is_symlink() or (path.exists() and not _regular_file(path)):
        raise ValueError("journal de export nao pode ser symlink")
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    if len(encoded) > _MAX_TRANSACTION_BYTES:
        raise ValueError("journal de export excede o limite")
    temporary = path.parent / f".{path.name}.{uuid4().hex}.tmp"
    try:
        with temporary.open("xb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _clear_publish_journal(root: Path) -> None:
    journal = _journal_path(root)
    if journal.is_symlink():
        raise ValueError("journal de export nao pode ser symlink")
    journal.unlink(missing_ok=True)
    _fsync_directory(journal.parent)


def _normalize_publish_transaction(value: object, root: Path) -> dict:
    if not isinstance(value, dict):
        raise ValueError("journal de export invalido")
    root_name = _plain_name(value.get("root"))
    staging_name = _plain_name(value.get("staging"))
    trash_name = _plain_name(value.get("trash"))
    segments = validate_segment_names(value.get("segments") or [])
    old_segments = validate_segment_names(value.get("old_segments") or [])
    owner = valid_export_owner(value.get("owner"))
    previous_raw = value.get("previous_owner")
    previous_owner = None if previous_raw is None else valid_export_owner(previous_raw)
    root_existed = value.get("root_existed")
    if (
        value.get("schema_version") != EXPORT_TRANSACTION_SCHEMA_VERSION
        or root_name != root.name
        or staging_name is None
        or not staging_name.startswith(f".{root.name}.export-")
        or not staging_name.endswith(".part")
        or trash_name is None
        or not trash_name.startswith(".replace-")
        or not trash_name.endswith(".trash")
        or owner is None
        or (previous_raw is not None and previous_owner is None)
        or type(root_existed) is not bool
    ):
        raise ValueError("journal de export invalido")
    if previous_owner is not None and not same_export_identity(previous_owner, owner):
        raise ValueError("journal de export mistura videos diferentes")
    return {
        "schema_version": EXPORT_TRANSACTION_SCHEMA_VERSION,
        "root": root_name,
        "staging": staging_name,
        "trash": trash_name,
        "segments": segments,
        "old_segments": old_segments,
        "owner": owner,
        "previous_owner": previous_owner,
        "root_existed": root_existed,
    }


def _read_publish_transaction(root: Path) -> dict | None:
    journal = _journal_path(root)
    if journal.is_symlink():
        raise ValueError("journal de export nao pode ser symlink")
    if not journal.exists():
        return None
    if not _regular_file(journal):
        raise ValueError("journal de export invalido")
    try:
        if journal.stat().st_size > _MAX_TRANSACTION_BYTES:
            raise ValueError("journal de export excede o limite")
        raw = json.loads(journal.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError("journal de export invalido") from exc
    return _normalize_publish_transaction(raw, root)


def _transaction_paths(root: Path, transaction: dict) -> tuple[Path, Path]:
    _validate_root_location(root)
    staging = root.parent / transaction["staging"]
    trash = root / transaction["trash"]
    if _lexical(staging).parent != _lexical(root.parent):
        raise ValueError("staging registrado fora da raiz permitida")
    if _lexical(trash.parent) != _lexical(root):
        raise ValueError("lixeira registrada fora da raiz permitida")
    if staging.is_symlink() or (staging.exists() and not _regular_directory(staging)):
        raise ValueError("staging registrado e inseguro")
    if trash.is_symlink() or (trash.exists() and not _regular_directory(trash)):
        raise ValueError("lixeira registrada e insegura")
    return staging, trash


def _segment_children(parent: Path, *, allowed: set[str], label: str) -> set[str]:
    if not parent.exists():
        return set()
    if parent.is_symlink() or not _regular_directory(parent):
        raise ValueError(f"{label} inseguro")
    found: set[str] = set()
    for child in parent.iterdir():
        if not child.name.startswith("seg_"):
            continue
        if child.name not in allowed or child.is_symlink() or not _regular_directory(child):
            raise ValueError(f"segmento inesperado ou inseguro em {label}: {child.name}")
        found.add(child.name)
    return found


def _move_directory(source: Path, destination: Path, label: str) -> None:
    if source.is_symlink() or not _regular_directory(source):
        raise ValueError(f"{label} de origem inseguro")
    if destination.exists() or destination.is_symlink():
        raise ValueError(f"{label} de destino conflitante")
    source.replace(destination)


def _remove_orphan_owner_temporaries(root: Path) -> None:
    if not _regular_directory(root):
        return
    prefix = f".{EXPORT_OWNER_MARKER}."
    removed = False
    for child in root.iterdir():
        name = child.name
        if not name.startswith(prefix) or not name.endswith(".tmp"):
            continue
        token = name[len(prefix) : -4]
        if len(token) != 32 or any(char not in "0123456789abcdef" for char in token):
            continue
        if child.is_symlink() or not _regular_file(child):
            raise ValueError("temporario de ownership inseguro")
        child.unlink()
        removed = True
    if removed:
        _fsync_directory(root)


def _restore_publish_transaction(root: Path, transaction: dict) -> None:
    staging, trash = _transaction_paths(root, transaction)
    new_segments = set(transaction["segments"])
    old_segments = set(transaction["old_segments"])
    allowed_root = new_segments | old_segments
    root_segments = _segment_children(root, allowed=allowed_root, label="raiz publicada")
    staged_segments = _segment_children(
        staging, allowed=new_segments, label="staging registrado"
    )
    trashed_segments = _segment_children(
        trash, allowed=old_segments, label="lixeira registrada"
    )

    marker = root / EXPORT_OWNER_MARKER
    if marker.is_symlink():
        raise ValueError("marker de ownership nao pode ser link simbolico")
    marker_present = marker.exists()
    actual_owner = read_export_owner(root) if marker_present else None
    previous_owner = transaction["previous_owner"]
    permitted_owners = [transaction["owner"]]
    if previous_owner is not None:
        permitted_owners.append(previous_owner)
    if marker_present and actual_owner not in permitted_owners:
        raise ValueError("ownership mudou durante recuperacao do export")

    if not _regular_directory(staging):
        staging.mkdir()

    # A copy in trash proves that a same-named entry in root is the newly
    # published one. Move it back first, then restore the old directory.
    for name in transaction["segments"]:
        current = root / name
        staged = staging / name
        previous = trash / name
        old_expected = name in old_segments
        if name in trashed_segments:
            if name in root_segments:
                _move_directory(current, staged, "segmento novo")
                root_segments.remove(name)
                staged_segments.add(name)
            elif name not in staged_segments:
                raise ValueError(f"segmento novo irrecuperavel: {name}")
            _move_directory(previous, current, "segmento anterior")
            trashed_segments.remove(name)
            root_segments.add(name)
        elif old_expected:
            # With no trash, the old directory is either untouched or already
            # restored by an earlier recovery attempt. The staged copy proves
            # that root still contains the old generation.
            if name not in root_segments or name not in staged_segments:
                raise ValueError(f"segmento anterior ambiguo ou ausente: {name}")
        elif name in root_segments:
            _move_directory(current, staged, "segmento novo")
            root_segments.remove(name)
            staged_segments.add(name)

    for name in transaction["old_segments"]:
        if name in new_segments:
            continue
        current = root / name
        previous = trash / name
        if name in trashed_segments:
            if name in root_segments:
                raise ValueError(f"segmento anterior duplicado: {name}")
            _move_directory(previous, current, "segmento anterior")
            trashed_segments.remove(name)
            root_segments.add(name)
        elif name not in root_segments:
            raise ValueError(f"segmento anterior ausente: {name}")

    _fsync_directory(staging)
    if _regular_directory(root):
        _fsync_directory(root)
    if _regular_directory(trash):
        _fsync_directory(trash)

    if previous_owner is None:
        if marker_present:
            marker.unlink()
            _fsync_directory(root)
    elif actual_owner != previous_owner:
        write_export_owner(root, previous_owner)

    _remove_orphan_owner_temporaries(root)

    if trash.exists():
        try:
            trash.rmdir()
        except OSError as exc:
            raise ValueError("lixeira de recuperacao nao ficou vazia") from exc
        if _regular_directory(root):
            _fsync_directory(root)
    if not transaction["root_existed"] and root.exists():
        try:
            root.rmdir()
        except OSError as exc:
            raise ValueError("raiz parcial de export nao ficou vazia") from exc
    _fsync_directory(root.parent)


def _published_transaction_is_complete(root: Path, transaction: dict) -> bool:
    if not _regular_directory(root):
        return False
    if read_export_owner(root) != transaction["owner"]:
        return False
    expected = set(transaction["segments"])
    actual = _segment_children(root, allowed=expected, label="raiz publicada")
    if actual != expected:
        return False
    for name in transaction["segments"]:
        _validate_tree_entries(root / name, f"segmento publicado {name}")
    return True


def _recover_publish_locked(root: Path, expected_owner: dict | None = None) -> str | None:
    transaction = _read_publish_transaction(root)
    if transaction is None:
        return None
    if expected_owner is not None and not same_export_identity(
        transaction["owner"], expected_owner
    ):
        raise ValueError("journal de export pertence a outro video")
    _transaction_paths(root, transaction)
    if _published_transaction_is_complete(root, transaction):
        _clear_publish_journal(root)
        return "committed"
    _restore_publish_transaction(root, transaction)
    _clear_publish_journal(root)
    return "rolled_back"


def _safe_recorded_root(ctx, raw: object) -> Path | None:
    if not isinstance(raw, str) or not raw:
        return None
    candidate = Path(raw)
    if ".." in candidate.parts or candidate.is_symlink():
        return None
    _reject_symlink_chain(ctx.output_root, "raiz de dados do objeto")
    if _lexical(candidate).parent != _lexical(ctx.output_root):
        return None
    _reject_symlink_chain(candidate.parent, "ancestral da raiz registrada")
    try:
        resolved = candidate.resolve()
        output = ctx.output_root.resolve()
    except (OSError, RuntimeError):
        return None
    if resolved == output or resolved.parent != output:
        return None
    return resolved


def export_root_for(ctx, video: VideoFile) -> Path:
    """Return a stable, video-owned root without reusing ambiguous legacy data."""
    entry = ctx.store.entry(video.relpath) if getattr(ctx, "store", None) else None
    recorded = ((entry or {}).get("export") or {}).get("root")
    previous = _safe_recorded_root(ctx, recorded)
    expected = export_owner(
        ctx, video, int((entry or {}).get("annotation_revision") or 0)
    )
    if previous is not None:
        _validate_root_location(previous)
        with _exclusive_publish_lock(previous):
            _recover_publish_locked(previous, expected)
            if same_export_identity(read_export_owner(previous), expected):
                return previous

    root = ctx.output_root / export_folder_name(video)
    _validate_root_location(root)
    with _exclusive_publish_lock(root):
        _recover_publish_locked(root, expected)
        if root.exists():
            actual = read_export_owner(root)
            if not same_export_identity(actual, expected):
                raise ValueError(
                    "raiz deterministica ja existe sem ownership verificavel"
                )
    return root


def write_prompt_json(
    segment_dir: Path, video: VideoFile, interval: dict, width: int, height: int
) -> None:
    payload = {
        "schema_version": PROMPT_SCHEMA_VERSION,
        "video": video.abspath.name,
        "video_relpath": video.relpath,
        "segment": interval["segment"],
        "interval_index": interval["index"],
        "source_start_frame": interval["start_frame"],
        "source_end_frame": interval["end_frame"],
        "frame_count": interval["frame_count"],
        "frame_naming": _NAMING_RATIONALE,
        "source_frame_of": "source_frame = source_start_frame + local_index",
        "image_width": width,
        "image_height": height,
        "prompt_frame_idx": interval["prompt_frame"] - interval["start_frame"],
        "prompt_frame_file": f"{interval['prompt_frame'] - interval['start_frame']:06d}.jpg",
        "objects": [
            {
                "obj_id": box["obj_id"],
                "label": box["label"],
                # Já é a lista interna exata para torch.tensor([...]) -> (1, 4).
                "box_normalized": box["normalized"],
                "box_pixel": box["pixel"],
            }
            for box in interval["bboxes"]
        ],
        "flags": interval["flags"],
        "sam3_usage": _SAM3_USAGE,
    }
    (segment_dir / "prompt.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def clean_segments(root: Path, keep: set[str]) -> list[str]:
    """Remove pastas seg_* que não fazem mais parte da anotação.

    Encurtar um intervalo nunca pode deixar frames obsoletos para trás — eles
    entrariam no dataset silenciosamente. Só mexe em `seg_*`; qualquer outro
    arquivo do usuário naquela pasta é preservado.
    """
    if not root.is_dir():
        return []
    removed = []
    for entry in root.iterdir():
        if entry.is_dir() and entry.name.startswith("seg_") and entry.name not in keep:
            shutil.rmtree(entry, ignore_errors=True)
            removed.append(entry.name)
    return removed


def validate_segment_names(names) -> list[str]:
    segments = list(names)
    if len(segments) != len(set(segments)) or any(
        not isinstance(name, str)
        or not name.startswith("seg_")
        or "/" in name
        or "\\" in name
        or Path(name).name != name
        for name in segments
    ):
        raise ValueError("nome de segmento invalido")
    return segments


def publish_staged_export(
    root: Path,
    staging: Path,
    segments: list[str],
    owner: dict,
    trash: Path,
) -> None:
    """Publish one export transaction with crash-safe, idempotent recovery.

    The owner marker is the commit point. A durable journal is fsynced before
    the first rename, so ``export_root_for`` can either finish that commit or
    restore the previous generation after a process/power failure. The caller
    still owns the annotation/lease fence; this function owns the filesystem
    transaction and a short cross-process lock for this root.
    """
    segments = validate_segment_names(segments)
    normalized_owner = valid_export_owner(owner)
    if normalized_owner is None:
        raise ValueError("owner de export invalido")
    _validate_publish_paths(root, staging, trash)

    staged: list[Path] = []
    for name in segments:
        candidate = staging / name
        if candidate.is_symlink() or not _regular_directory(candidate):
            raise ValueError(f"segmento staged invalido: {name}")
        staged.append(candidate)

    with _exclusive_publish_lock(root):
        _recover_publish_locked(root, normalized_owner)
        _validate_publish_paths(root, staging, trash)
        for source in staged:
            if source.is_symlink() or not _regular_directory(source):
                raise ValueError(f"segmento staged invalido: {source.name}")
            _validate_tree_entries(source, f"segmento staged {source.name}")

        root_existed = root.exists()
        previous_owner = read_export_owner(root) if root_existed else None
        if root_existed and not same_export_identity(previous_owner, normalized_owner):
            raise ValueError("raiz de export existente sem ownership verificavel")

        old_segments: list[Path] = []
        if root_existed:
            for child in sorted(root.iterdir(), key=lambda path: path.name):
                if not child.name.startswith("seg_"):
                    continue
                if child.is_symlink() or not _regular_directory(child):
                    raise ValueError("segmento publicado precisa ser diretorio seguro")
                _validate_tree_entries(child, f"segmento publicado {child.name}")
                old_segments.append(child)
        if trash.exists():
            for child in trash.iterdir():
                raise ValueError(
                    f"lixeira de publicacao nao esta vazia: {child.name}"
                )

        transaction = _normalize_publish_transaction(
            {
                "schema_version": EXPORT_TRANSACTION_SCHEMA_VERSION,
                "root": root.name,
                "staging": staging.name,
                "trash": trash.name,
                "segments": segments,
                "old_segments": [child.name for child in old_segments],
                "owner": normalized_owner,
                "previous_owner": previous_owner,
                "root_existed": root_existed,
            },
            root,
        )
        journal = _journal_path(root)
        if journal.exists() or journal.is_symlink():
            raise ValueError("journal de export anterior nao foi recuperado")
        _write_json_transaction(journal, transaction)

        try:
            if not root_existed:
                root.mkdir()
                _fsync_directory(root.parent)
            if not trash.exists():
                trash.mkdir()
                _fsync_directory(root)
            for child in old_segments:
                child.replace(trash / child.name)
            _fsync_directory(root)
            _fsync_directory(trash)
            for source in staged:
                source.replace(root / source.name)
            _fsync_directory(root)
            _fsync_directory(staging)
            write_export_owner(root, normalized_owner)
            if not _published_transaction_is_complete(root, transaction):
                raise RuntimeError("publicacao de export ficou incompleta")
        except Exception as exc:
            try:
                recovery = _recover_publish_locked(root, normalized_owner)
            except Exception as recovery_exc:  # noqa: BLE001
                raise RuntimeError(
                    f"publicacao falhou e recuperacao ficou incompleta: {recovery_exc}"
                ) from exc
            if recovery == "committed":
                return
            raise

        try:
            _clear_publish_journal(root)
        except OSError:
            # The marker is already durable and therefore committed. A later
            # export_root_for call can safely clear the leftover journal.
            log.warning("nao foi possivel limpar journal de export", exc_info=True)


def _remove_generated_tree(path: Path, parent: Path) -> None:
    if path.is_symlink():
        raise ValueError("diretorio temporario nao pode ser link simbolico")
    resolved = path.resolve()
    expected_parent = parent.resolve()
    if resolved.parent != expected_parent:
        raise ValueError("diretorio temporario fora da raiz de export")
    if resolved.exists():
        shutil.rmtree(resolved)


async def export_video(
    ctx, video_id: str, entry: dict, *, client_id: str | None = None, user: str | None = None
) -> Job:
    """Exporta todos os intervalos do vídeo, um job de ffmpeg por segmento."""
    video = ctx.index.get(video_id)
    if video is None:
        raise KeyError(video_id)

    intervals = entry.get("intervals", [])
    media = entry.get("media") or ctx.index.cached_probe(video_id) or {}
    root = export_root_for(ctx, video)

    total = sum(interval["frame_count"] for interval in intervals)
    job = jobs.create(
        "export", ctx.object_id, video_id, total, "exportando frames…",
        client_id=client_id, user=user,
    )

    src = ctx.index.resolve_path(video_id)
    binaries = ffmpeg.resolve()

    expected_revision = int(entry.get("annotation_revision") or 0)
    owner = export_owner(ctx, video, expected_revision)
    staging = root.parent / f".{root.name}.export-{uuid4().hex}.part"
    publish_trash = root / f".replace-{uuid4().hex}.trash"

    async def worker() -> None:
        job.state = "running"
        jobs._publish(job)
        done_frames = 0
        segments: list[str] = []
        try:
            validate_segment_names(interval["segment"] for interval in intervals)
            if staging.exists() or staging.is_symlink():
                raise ValueError("staging local de export ja existe")
            staging.mkdir(parents=True)

            for interval in intervals:
                if job._cancelled:
                    job.state = "cancelled"
                    job.message = "cancelado"
                    return
                segment_dir = staging / interval["segment"]
                segment_dir.mkdir()

                sub = jobs.create(
                    "export", ctx.object_id, video_id, interval["frame_count"], "",
                    client_id=client_id, user=user,
                )
                base = done_frames

                def relay(child_job: Job = sub, offset: int = base) -> None:
                    job.current = offset + child_job.current
                    jobs._publish(job)

                sub._subscribers.append(_RelayQueue(relay))

                await jobs.run(
                    sub,
                    ffmpeg.export_segment_argv(
                        src, segment_dir, interval["start_frame"], interval["end_frame"]
                    ),
                )

                if sub.state != "done":
                    job.state = sub.state
                    job.error = sub.error or "export do segmento falhou"
                    return

                produced = len(list(segment_dir.glob("*.jpg")))
                expected = interval["frame_count"]
                if produced != expected:
                    job.state = "error"
                    job.error = (
                        f"{interval['segment']}: ffmpeg produziu {produced} frames, "
                        f"esperado {expected}"
                    )
                    return

                width, height = _frame_size(segment_dir, media)
                await asyncio.to_thread(
                    write_prompt_json, segment_dir, video, interval, width, height
                )

                segments.append(interval["segment"])
                done_frames += expected
                job.current = done_frames
                jobs._publish(job)

            # O fallback local não compartilha o advisory lock do PostgreSQL.
            # Serializamos a fence com as mutações de annotations.json e só
            # tocamos a geração publicada depois de reler a revisão corrente.
            async with ctx.store._lock:
                latest = ctx.store.entry(video.relpath)
                current_revision = int((latest or {}).get("annotation_revision") or 0)
                if (
                    current_revision != expected_revision
                    or (latest or {}).get("status") == "no_boom"
                    or job._cancelled
                ):
                    job.state = "cancelled"
                    job.message = "export obsoleto; anotacao mudou"
                    return
                selected_root = export_root_for(ctx, video)
                if selected_root.resolve() != root.resolve():
                    raise ValueError("raiz do export mudou antes da publicacao")
                await asyncio.to_thread(
                    publish_staged_export,
                    root,
                    staging,
                    segments,
                    owner,
                    publish_trash,
                )

            if publish_trash.exists():
                try:
                    await asyncio.to_thread(
                        _remove_generated_tree, publish_trash, root
                    )
                except OSError:
                    log.warning(
                        "falha ao remover lixeira de export local", exc_info=True
                    )

            job.result = {
                "root": root.as_posix(),
                "segments": segments,
                "total_frames": done_frames,
                "jpeg_qscale": EXPORT_QSCALE,
                "frame_naming": _NAMING_RATIONALE,
                "ffmpeg_version": binaries.version,
                "annotation_revision": expected_revision,
                "owner": owner,
            }
            # The local in-process fallback follows the same authoritative
            # commit path as the durable CPU worker. A terminal job therefore
            # always means annotations.json and the SAM3 queue were committed.
            from .video_export_completion import finalize_video_export

            await finalize_video_export(
                ctx,
                video_id,
                job.result,
                user,
                job.job_id,
                expected_revision=expected_revision,
                expected_root_basename=root.name,
                expected_owner=owner,
                expected_total=total,
            )
            job.state = "done"
        except Exception as exc:  # noqa: BLE001
            if job._cancelled:
                job.state = "cancelled"
                job.message = "cancelado"
            else:
                job.state = "error"
                job.error = str(exc)
        finally:
            if staging.exists() or staging.is_symlink():
                try:
                    await asyncio.to_thread(
                        _remove_generated_tree, staging, ctx.output_root
                    )
                except Exception:  # noqa: BLE001
                    log.warning("falha ao remover staging de export local", exc_info=True)
            job.finished_at = iso()
            jobs._publish(job)

    asyncio.create_task(worker())
    return job


def _frame_size(segment_dir: Path, media: dict) -> tuple[int, int]:
    """Dimensões reais do frame exportado.

    Nunca confiar em `ffprobe stream=width,height`: com matriz de rotação de ±90°
    ele reporta as dimensões CODIFICADAS enquanto o frame extraído já vem
    rotacionado — normalizar bbox contra as codificadas dá coordenadas
    transpostas e silenciosamente erradas.
    """
    first = next(iter(sorted(segment_dir.glob("*.jpg"))), None)
    if first is not None:
        try:
            from PIL import Image

            with Image.open(first) as image:
                return image.size
        except Exception:  # noqa: BLE001
            pass
    return int(media.get("width") or 0), int(media.get("height") or 0)


class _RelayQueue:
    """Adaptador que faz o job-pai refletir o progresso do job-filho."""

    def __init__(self, callback) -> None:
        self._callback = callback

    def put_nowait(self, _payload) -> None:
        self._callback()
