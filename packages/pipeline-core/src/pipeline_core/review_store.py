"""Revisões imutáveis de máscaras com concorrência otimista."""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .masks import MaskInfo, inspect_binary_png
from .storage import MinioBlobStore, object_key_for

SCHEMA_VERSION = 1
STATUSES = ("ok", "edited")
_lock = threading.RLock()


class RevisionConflict(RuntimeError):
    """A revisão salva mudou desde que o cliente carregou o frame."""


@dataclass(frozen=True)
class MaskEdit:
    obj_id: int
    label: str
    png: bytes


@dataclass(frozen=True)
class MaskInstance:
    obj_id: int
    label: str
    path: Path
    info: MaskInfo


@dataclass(frozen=True)
class FrameMaskState:
    frame: int
    revision: int
    status: str | None
    instances: tuple[MaskInstance, ...]
    reviewed_by: str | None = None


class FileMaskReviewStore:
    """Adapter de transição para segmentos existentes no filesystem.

    Mantém o mesmo contrato que o adapter PostgreSQL/MinIO: bruto imutável,
    revisão versionada e compare-and-swap por ``expected_revision``.
    """

    def __init__(
        self,
        out_dir: Path,
        *,
        image_size: tuple[int, int],
        labels: dict[int, str],
    ) -> None:
        self.out_dir = out_dir
        self.image_size = image_size
        self.labels = labels
        self.manifest_path = out_dir / "mask_review.json"

    def _load(self) -> dict:
        if not self.manifest_path.exists():
            return {"schema_version": SCHEMA_VERSION, "frames": {}}
        try:
            data = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(
                f"manifesto de revisao ilegivel: {self.manifest_path}"
            ) from exc
        if (
            not isinstance(data, dict)
            or data.get("schema_version") != SCHEMA_VERSION
            or not isinstance(data.get("frames"), dict)
        ):
            raise ValueError(
                f"manifesto de revisao invalido: {self.manifest_path}"
            )
        return data

    def _mask_path(self, value: str | Path) -> Path:
        relative = Path(value)
        if relative.is_absolute() or not relative.parts or any(
            part in ("", ".", "..") for part in relative.parts
        ):
            raise ValueError(f"caminho de mascara invalido: {value!r}")
        if self.out_dir.is_symlink() or not self.out_dir.is_dir():
            raise ValueError(f"raiz de mascaras insegura: {self.out_dir}")
        target = self.out_dir / relative
        current = self.out_dir
        for part in relative.parts:
            if current.is_symlink():
                raise ValueError(f"symlink proibido em mascara: {current}")
            current = current / part
        if current.is_symlink():
            raise ValueError(f"symlink proibido em mascara: {current}")
        if not target.resolve(strict=False).is_relative_to(self.out_dir.resolve(strict=True)):
            raise ValueError(f"mascara fora da geracao: {target}")
        return target

    def _instance_from_manifest(self, item: dict) -> MaskInstance:
        path = self._mask_path(str(item.get("path") or ""))
        info = inspect_binary_png(path.read_bytes(), expected_size=self.image_size)
        if item.get("sha256") is not None and item.get("sha256") != info.sha256:
            raise ValueError(f"checksum da revisao diverge: {path}")
        if item.get("area_pixels") is not None and int(item["area_pixels"]) != info.area_pixels:
            raise ValueError(f"area da revisao diverge: {path}")
        if item.get("bbox_normalized") is not None and tuple(
            item["bbox_normalized"]
        ) != tuple(info.bbox_normalized or ()):
            raise ValueError(f"bbox da revisao diverge: {path}")
        return MaskInstance(
            obj_id=int(item["obj_id"]),
            label=item.get("label") or "",
            path=path,
            info=info,
        )

    def _raw_instances(self, frame: int) -> tuple[MaskInstance, ...]:
        found = []
        for obj_id, label in sorted(self.labels.items()):
            path = self._mask_path(
                Path("masks") / str(obj_id) / f"{frame:06d}.png"
            )
            if not path.exists():
                continue
            found.append(
                MaskInstance(
                    obj_id=obj_id,
                    label=label,
                    path=path,
                    info=inspect_binary_png(path.read_bytes(), expected_size=self.image_size),
                )
            )
        return tuple(found)

    def get_frame(self, frame: int) -> FrameMaskState:
        data = self._load()
        entry = data["frames"].get(str(frame)) or {}
        status = entry.get("status")
        instances: tuple[MaskInstance, ...]
        if status == "edited":
            loaded = []
            for item in entry.get("instances") or []:
                loaded.append(self._instance_from_manifest(item))
            instances = tuple(loaded)
        else:
            instances = self._raw_instances(frame)
        return FrameMaskState(
            frame=frame,
            revision=int(entry.get("revision") or 0),
            status=status,
            instances=instances,
            reviewed_by=entry.get("by"),
        )

    def save_frames(
        self,
        updates: list[dict],
        *,
        user: str | None = None,
        before_commit: Callable[[int, dict], None] | None = None,
    ) -> list[FrameMaskState]:
        """Valida e persiste vários frames com uma única troca do manifesto."""
        if not updates:
            return []

        with _lock:
            data = self._load()
            prepared: list[tuple[int, dict, list[tuple[Path, bytes]]]] = []
            seen: set[int] = set()

            # Prepara o lote inteiro primeiro. Conflito ou PNG inválido em qualquer
            # frame aborta antes que o manifesto visível seja alterado.
            for update in updates:
                frame = int(update["frame"])
                if frame < 0:
                    raise ValueError(f"frame invalido: {frame}")
                if frame in seen:
                    raise ValueError(f"frame duplicado no lote: {frame}")
                seen.add(frame)

                status = str(update.get("status") or "")
                if status not in STATUSES:
                    raise ValueError(f"status inválido: {status}")
                instances = list(update.get("instances") or [])
                if not all(isinstance(item, MaskEdit) for item in instances):
                    raise ValueError(f"edicao de mascara invalida no frame {frame}")
                retained = {int(value) for value in (update.get("retain_obj_ids") or [])}
                edited_ids = {edit.obj_id for edit in instances}
                if retained & edited_ids:
                    raise ValueError("uma instancia nao pode ser alterada e retida ao mesmo tempo")
                unknown = (retained | edited_ids) - set(self.labels)
                if unknown:
                    raise ValueError(f"obj_id desconhecido: {sorted(unknown)}")

                current = data["frames"].get(str(frame)) or {}
                actual_revision = int(current.get("revision") or 0)
                expected_revision = int(update.get("expected_revision") or 0)
                if actual_revision != expected_revision:
                    raise RevisionConflict(
                        f"frame {frame}: revisão esperada {expected_revision}, atual {actual_revision}"
                    )

                history = list(current.get("history") or [])
                if actual_revision > 0 and not history:
                    history.append(
                        {key: value for key, value in current.items() if key != "history"}
                    )
                revision = actual_revision + 1
                entry: dict = {"revision": revision, "status": status, "by": user}
                writes: list[tuple[Path, bytes]] = []

                if status == "edited":
                    effective_before = {
                        item.obj_id: item for item in self.get_frame(frame).instances
                    }
                    revision_dir = Path("reviews") / f"{frame:06d}" / f"rev_{revision:06d}"
                    stored = []
                    for edit in instances:
                        info = inspect_binary_png(edit.png, expected_size=self.image_size)
                        relative = revision_dir / f"{edit.obj_id}.png"
                        writes.append((self.out_dir / relative, edit.png))
                        stored.append(
                            {
                                "obj_id": edit.obj_id,
                                "label": edit.label,
                                "path": relative.as_posix(),
                                "sha256": info.sha256,
                                "area_pixels": info.area_pixels,
                                "bbox_normalized": info.bbox_normalized,
                            }
                        )
                    for obj_id in sorted(retained):
                        previous = effective_before.get(obj_id)
                        if previous is None:
                            raise ValueError(f"mascara retida nao existe: obj_id {obj_id}")
                        stored.append(
                            {
                                "obj_id": previous.obj_id,
                                "label": previous.label,
                                "path": previous.path.relative_to(self.out_dir).as_posix(),
                                "sha256": previous.info.sha256,
                                "area_pixels": previous.info.area_pixels,
                                "bbox_normalized": previous.info.bbox_normalized,
                            }
                        )
                    stored.sort(key=lambda item: int(item["obj_id"]))
                    entry["instances"] = stored
                    entry["deleted_obj_ids"] = sorted(
                        set(self.labels) - edited_ids - retained
                    )

                history.append({key: value for key, value in entry.items() if key != "history"})
                entry["history"] = history
                prepared.append((frame, entry, writes))

            # PNGs editados são imutáveis e podem ser gravados antes do ponteiro
            # atômico (mask_review.json). Um erro posterior deixa no máximo um
            # arquivo órfão, nunca uma revisão parcial visível.
            for _, _, writes in prepared:
                for target, payload in writes:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    temporary = target.with_suffix(".png.tmp")
                    temporary.write_bytes(payload)
                    os.replace(temporary, target)

            if before_commit is not None:
                for frame, entry, _ in prepared:
                    before_commit(frame, entry)

            for frame, entry, _ in prepared:
                data["frames"][str(frame)] = entry
            self.out_dir.mkdir(parents=True, exist_ok=True)
            temporary_manifest = self.manifest_path.with_suffix(".json.tmp")
            temporary_manifest.write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            os.replace(temporary_manifest, self.manifest_path)

            store = MinioBlobStore.from_env()
            workspace = os.environ.get("MST_WORKSPACE")
            if store is not None and workspace:
                for _, _, writes in prepared:
                    for path, _ in writes:
                        store.put_file("masks", object_key_for(Path(workspace), path), path)
                store.put_file(
                    "masks",
                    object_key_for(Path(workspace), self.manifest_path),
                    self.manifest_path,
                )

        return [
            FrameMaskState(
                frame=frame,
                revision=int(entry["revision"]),
                status=str(entry["status"]),
                instances=(),
                reviewed_by=user,
            )
            for frame, entry, _ in prepared
        ]

    def save_frame(
        self,
        frame: int,
        *,
        expected_revision: int,
        status: str,
        instances: list[MaskEdit],
        retain_obj_ids: list[int] | None = None,
        user: str | None = None,
        before_commit: Callable[[dict], None] | None = None,
    ) -> FrameMaskState:
        if status not in STATUSES:
            raise ValueError(f"status inválido: {status}")
        with _lock:
            retained = set(retain_obj_ids or [])
            edited_ids = {edit.obj_id for edit in instances}
            if retained & edited_ids:
                raise ValueError("uma instancia nao pode ser alterada e retida ao mesmo tempo")
            unknown = (retained | edited_ids) - set(self.labels)
            if unknown:
                raise ValueError(f"obj_id desconhecido: {sorted(unknown)}")
            effective_before = {item.obj_id: item for item in self.get_frame(frame).instances}
            data = self._load()
            current = data["frames"].get(str(frame)) or {}
            actual_revision = int(current.get("revision") or 0)
            if actual_revision != expected_revision:
                raise RevisionConflict(
                    f"revisão esperada {expected_revision}, atual {actual_revision}"
                )
            history = list(current.get("history") or [])
            if actual_revision > 0 and not history:
                history.append(
                    {key: value for key, value in current.items() if key != "history"}
                )
            revision = actual_revision + 1
            entry: dict = {"revision": revision, "status": status, "by": user}
            if status == "edited":
                revision_dir = Path("reviews") / f"{frame:06d}" / f"rev_{revision:06d}"
                stored = []
                for edit in instances:
                    info = inspect_binary_png(edit.png, expected_size=self.image_size)
                    relative = revision_dir / f"{edit.obj_id}.png"
                    target = self.out_dir / relative
                    target.parent.mkdir(parents=True, exist_ok=True)
                    temporary = target.with_suffix(".png.tmp")
                    temporary.write_bytes(edit.png)
                    os.replace(temporary, target)
                    stored.append(
                        {
                            "obj_id": edit.obj_id,
                            "label": edit.label,
                            "path": relative.as_posix(),
                            "sha256": info.sha256,
                            "area_pixels": info.area_pixels,
                            "bbox_normalized": info.bbox_normalized,
                        }
                    )
                for obj_id in sorted(retained):
                    previous = effective_before.get(obj_id)
                    if previous is None:
                        raise ValueError(f"mascara retida nao existe: obj_id {obj_id}")
                    stored.append(
                        {
                            "obj_id": previous.obj_id,
                            "label": previous.label,
                            "path": previous.path.relative_to(self.out_dir).as_posix(),
                            "sha256": previous.info.sha256,
                            "area_pixels": previous.info.area_pixels,
                            "bbox_normalized": previous.info.bbox_normalized,
                        }
                    )
                stored.sort(key=lambda item: int(item["obj_id"]))
                entry["instances"] = stored
                entry["deleted_obj_ids"] = sorted(
                    set(self.labels) - edited_ids - retained
                )
            history.append({key: value for key, value in entry.items() if key != "history"})
            entry["history"] = history
            if before_commit is not None:
                before_commit(entry)
            data["frames"][str(frame)] = entry
            self.out_dir.mkdir(parents=True, exist_ok=True)
            temporary_manifest = self.manifest_path.with_suffix(".json.tmp")
            temporary_manifest.write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            os.replace(temporary_manifest, self.manifest_path)
            store = MinioBlobStore.from_env()
            workspace = os.environ.get("MST_WORKSPACE")
            if store is not None and workspace:
                for item in entry.get("instances") or []:
                    if int(item["obj_id"]) not in edited_ids:
                        continue
                    path = self.out_dir / item["path"]
                    store.put_file("masks", object_key_for(Path(workspace), path), path)
                store.put_file(
                    "masks",
                    object_key_for(Path(workspace), self.manifest_path),
                    self.manifest_path,
                )
        return self.get_frame(frame)
