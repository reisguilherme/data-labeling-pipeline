"""Persistência do annotations.json consolidado.

Um único documento em memória é a cópia autoritativa durante a vida do processo;
o disco é o espelho durável, reescrito atomicamente a cada save.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .config import APP_NAME, APP_VERSION
from .flags import FLAG_GROUPS_VERSION
from .videos import iso

SCHEMA_VERSION = 2

STATUSES = ("pending", "in_progress", "done", "no_boom")

_HISTORY_EVERY = 50


def _empty_doc() -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_by": f"{APP_NAME} {APP_VERSION}",
        "object_id": None,
        "label": None,
        "videos_root": None,
        "output_root": None,
        "flag_groups_version": FLAG_GROUPS_VERSION,
        "updated_at": iso(),
        "counts": {status: 0 for status in ("total", *STATUSES)},
        "videos": {},
    }


class AnnotationStore:
    """Uma instância por objeto de anotação — ver a docstring de VideoIndex."""

    def __init__(
        self,
        *,
        annotations_path: Path,
        cache_dir: Path,
        videos_root: Path,
        output_root: Path,
        object_id: str,
        label: str,
        total_provider: Callable[[], int | None],
    ) -> None:
        self.annotations_path = annotations_path
        self.cache_dir = cache_dir
        self.videos_root = videos_root
        self.output_root = output_root
        self.object_id = object_id
        self.label = label
        # Injetado em vez de importar o índice: o _flush precisa do total de
        # vídeos em disco para derivar `pending`, e importar o índice aqui era um
        # ciclo que só não quebrava por estar dentro da função.
        self._total_provider = total_provider
        self._doc: dict = _empty_doc()
        self._lock = asyncio.Lock()
        self._saves = 0
        self.loaded = False

    # -- ciclo de vida -----------------------------------------------------

    def load(self) -> dict:
        path = self.annotations_path
        if not path.exists():
            self._doc = _empty_doc()
        else:
            try:
                self._doc = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                backup = path.with_suffix(".corrupt.json")
                shutil.copy2(path, backup)
                raise RuntimeError(
                    f"annotations.json ilegível ({exc}); cópia preservada em {backup}"
                ) from exc
            self._doc.setdefault("videos", {})
            self._doc.setdefault("counts", {})
        self.loaded = True
        return self._doc

    @property
    def doc(self) -> dict:
        return self._doc

    # -- leitura -----------------------------------------------------------

    def entry(self, relpath: str) -> dict | None:
        return self._doc["videos"].get(relpath)

    def status_of(self, relpath: str) -> str:
        entry = self.entry(relpath)
        return entry.get("status", "pending") if entry else "pending"

    def counts(self, total: int | None = None) -> dict[str, int]:
        """Contagem por status. `total` vem do índice em disco; sem ele, usa só
        os vídeos que têm entrada."""
        counts = {status: 0 for status in STATUSES}
        for entry in self._doc["videos"].values():
            status = entry.get("status", "pending")
            if status in counts:
                counts[status] += 1

        if total is None:
            total = sum(counts.values())
        else:
            # Vídeos que existem no disco mas nunca foram tocados não têm entrada.
            counts["pending"] = max(
                total - sum(value for key, value in counts.items() if key != "pending"), 0
            )
        return {"total": total, **counts}

    # -- escrita -----------------------------------------------------------

    async def put_entry(self, relpath: str, entry: dict) -> dict:
        async with self._lock:
            self._doc["videos"][relpath] = entry
            await asyncio.to_thread(self._flush)
        return entry

    async def mutate(self, fn) -> Any:
        """Aplica `fn(doc)` sob o lock e persiste. Retorna o que `fn` devolver."""
        async with self._lock:
            result = fn(self._doc)
            await asyncio.to_thread(self._flush)
        return result

    def _flush(self) -> None:
        path = self.annotations_path
        path.parent.mkdir(parents=True, exist_ok=True)

        self._doc["schema_version"] = SCHEMA_VERSION
        self._doc["generated_by"] = f"{APP_NAME} {APP_VERSION}"
        self._doc["flag_groups_version"] = FLAG_GROUPS_VERSION
        self._doc["object_id"] = self.object_id
        self._doc["label"] = self.label
        # Caminhos com "/" para que o JSON seja legível/portável do lado da Spark.
        self._doc["videos_root"] = self.videos_root.as_posix()
        self._doc["output_root"] = self.output_root.as_posix()
        self._doc["updated_at"] = iso()
        self._doc["videos"] = dict(sorted(self._doc["videos"].items()))

        # Recalcula a partir do índice em disco quando disponível, para que
        # `pending` inclua vídeos ainda sem entrada.
        try:
            total = self._total_provider() or None
        except Exception:  # noqa: BLE001
            total = None
        self._doc["counts"] = self.counts(total)

        payload = json.dumps(self._doc, ensure_ascii=False, indent=2)

        if path.exists():
            try:
                shutil.copy2(path, path.with_name("annotations.bak.json"))
            except OSError:
                pass

        # tmp no MESMO diretório: os.replace só é atômico dentro de um volume.
        tmp = path.with_name("annotations.json.tmp")
        with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)

        self._saves += 1
        if self._saves % _HISTORY_EVERY == 0:
            self._snapshot(path)

    def _snapshot(self, path: Path) -> None:
        history = self.cache_dir / "history"
        history.mkdir(parents=True, exist_ok=True)
        stamp = iso().replace(":", "-")
        try:
            shutil.copy2(path, history / f"annotations-{stamp}.json")
        except OSError:
            pass
