"""Persistência do annotations.json consolidado.

Um único documento em memória é a cópia autoritativa durante a vida do processo;
o disco é o espelho durável, reescrito atomicamente a cada save.
"""

from __future__ import annotations

import asyncio
import copy
import json
import os
import shutil
import threading
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
        self._state_lock = threading.RLock()
        self._saves = 0
        self.loaded = False

    # -- ciclo de vida -----------------------------------------------------

    def load(self) -> dict:
        path = self.annotations_path
        if not path.exists():
            loaded_doc = _empty_doc()
        else:
            try:
                loaded_doc = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                backup = path.with_suffix(".corrupt.json")
                shutil.copy2(path, backup)
                raise RuntimeError(
                    f"annotations.json ilegível ({exc}); cópia preservada em {backup}"
                ) from exc
            loaded_doc.setdefault("videos", {})
            loaded_doc.setdefault("counts", {})
        with self._state_lock:
            self._doc = loaded_doc
            self.loaded = True
            return copy.deepcopy(self._doc)

    @property
    def doc(self) -> dict:
        with self._state_lock:
            return copy.deepcopy(self._doc)

    # -- leitura -----------------------------------------------------------

    def entry(self, relpath: str) -> dict | None:
        with self._state_lock:
            return copy.deepcopy(self._doc["videos"].get(relpath))

    def status_of(self, relpath: str) -> str:
        entry = self.entry(relpath)
        return entry.get("status", "pending") if entry else "pending"

    def counts(self, total: int | None = None) -> dict[str, int]:
        """Contagem por status. `total` vem do índice em disco; sem ele, usa só
        os vídeos que têm entrada."""
        with self._state_lock:
            return self._counts_for(self._doc["videos"], total)

    def listing_snapshot(
        self, total: int | None = None
    ) -> tuple[dict[str, dict], dict[str, int]]:
        """Return entries and counts from one coherent in-memory revision."""
        with self._state_lock:
            entries = copy.deepcopy(self._doc["videos"])
            counts = self._counts_for(self._doc["videos"], total)
        return entries, counts

    @staticmethod
    def _counts_for(entries: dict[str, dict], total: int | None) -> dict[str, int]:
        counts = {status: 0 for status in STATUSES}
        for entry in entries.values():
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
            with self._state_lock:
                self._doc["videos"][relpath] = copy.deepcopy(entry)
            await asyncio.to_thread(self._flush)
        return entry

    async def mutate(self, fn) -> Any:
        """Aplica `fn(doc)` sob o lock e persiste. Retorna o que `fn` devolver."""
        async with self._lock:
            with self._state_lock:
                result = fn(self._doc)
            await asyncio.to_thread(self._flush)
        return result

    async def mutate_threaded(self, fn: Callable[[dict], Any]) -> Any:
        """Run a blocking callback on a private snapshot, serialized with writers.

        Readers keep the last committed document throughout the callback and I/O.
        Cancellation waits for the thread (including its possible commit) before
        releasing the writer and caller's outer fences; it cannot stop a thread.
        """
        async with self._lock:
            def commit() -> Any:
                with self._state_lock:
                    candidate = copy.deepcopy(self._doc)
                result = fn(candidate)
                # Copy before publishing: an unsupported callback result must not
                # turn a successful disk commit into an apparent failed mutation.
                committed_result = copy.deepcopy(result)
                self._flush(candidate)
                return committed_result

            operation = asyncio.create_task(asyncio.to_thread(commit))
            try:
                return await asyncio.shield(operation)
            except asyncio.CancelledError:
                # A second cancel must not let an active thread escape its locks.
                while not operation.done():
                    try:
                        await asyncio.shield(operation)
                    except asyncio.CancelledError:
                        continue
                    except BaseException:
                        break
                if not operation.cancelled():
                    try:
                        operation.result()
                    except BaseException:
                        pass
                raise

    def _flush(self, candidate: dict | None = None) -> None:
        path = self.annotations_path
        try:
            total = self._total_provider() or None
        except Exception:  # noqa: BLE001
            total = None

        def serialize(target: dict) -> str:
            target["schema_version"] = SCHEMA_VERSION
            target["generated_by"] = f"{APP_NAME} {APP_VERSION}"
            target["flag_groups_version"] = FLAG_GROUPS_VERSION
            target["object_id"] = self.object_id
            target["label"] = self.label
            # Caminhos com "/" mantêm o JSON legível/portável para a Spark.
            target["videos_root"] = self.videos_root.as_posix()
            target["output_root"] = self.output_root.as_posix()
            target["updated_at"] = iso()
            target["videos"] = dict(sorted(target["videos"].items()))
            target["counts"] = self._counts_for(target["videos"], total)
            return json.dumps(target, ensure_ascii=False, indent=2)

        if candidate is None:
            with self._state_lock:
                payload = serialize(self._doc)
        else:
            # `candidate` is private to the writer thread. Serializing it without
            # the state lock keeps reads of the last committed document responsive.
            payload = serialize(candidate)

        # O asyncio.Lock do writer permanece adquirido até o fim deste método,
        # preservando a ordem dos replaces. O lock de estado fica livre durante
        # o I/O para que leitores obtenham snapshots em memória.
        path.parent.mkdir(parents=True, exist_ok=True)

        if path.exists():
            try:
                shutil.copy2(path, path.with_name("annotations.bak.json"))
            except OSError:
                pass

        # tmp no MESMO diretório: os.replace só é atômico dentro de um volume.
        tmp = path.with_name("annotations.json.tmp")
        try:
            with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, path)
        except BaseException:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            raise

        with self._state_lock:
            if candidate is not None:
                self._doc = candidate
            self._saves += 1
            snapshot_due = self._saves % _HISTORY_EVERY == 0
        if snapshot_due:
            self._snapshot(path)

    def _snapshot(self, path: Path) -> None:
        history = self.cache_dir / "history"
        history.mkdir(parents=True, exist_ok=True)
        stamp = iso().replace(":", "-")
        try:
            shutil.copy2(path, history / f"annotations-{stamp}.json")
        except OSError:
            pass
