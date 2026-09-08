"""Fila de anotação automática pelo SAM3.

O runner roda noutro container, na mesma máquina, e consome esta fila por HTTP.
Os DADOS (frames, prompt.json, rótulos) atravessam pelo bind mount compartilhado;
só o plano de controle passa por aqui, e são algumas centenas de bytes por job.

Por que a fila é HTTP e não um arquivo compartilhado no workspace: a triagem
declara em vários lugares que o estado em memória é autoritativo, e o único
primitivo de concorrência do projeto é `threading.Lock` dentro de um processo.
Uma fila em arquivo criaria um segundo escritor vindo de outro processo, o que
exigiria travas entre processos — uma regressão de coerência real. Aqui, a
triagem continua dona única do estado, e o status já nasce ao lado dos dados que
alimentam o card da biblioteca.

O espelho em disco, ao contrário do `locks.json`, É autoritativo no boot: uma
trava perdida só custa 90 s de espera, mas um vídeo enfileirado que some num
restart nunca mais é processado.
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .videos import iso

SCHEMA_VERSION = 1

# O runner renova o lease a cada heartbeat. Três batidas perdidas e o vídeo
# volta para a fila — mesma lógica de locks.py, e pelo mesmo motivo.
DEFAULT_LEASE_SECONDS = 180

# Depois disto, o problema não é transitório: é este vídeo que derruba o runner.
MAX_ATTEMPTS = 3

STATES = ("queued", "leased", "running", "done", "error", "cancelled")

_HISTORY_LIMIT = 20


@dataclass
class QueueItem:
    video_id: str
    relpath: str
    name: str
    export_root: str
    segments: list[str] = field(default_factory=list)
    state: str = "queued"
    attempts: int = 0
    force: bool = False
    cancel_requested: bool = False
    enqueued_at: str = ""
    enqueued_by: str | None = None
    lease_id: str | None = None
    # Instante ABSOLUTO. Guardar "faltam N segundos" faria o lease renascer a
    # cada restart do servidor — o mesmo erro que já custou caro em locks.py.
    lease_expires_at_epoch: float | None = None
    worker: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    progress: dict = field(default_factory=dict)
    result: dict | None = None
    error: str | None = None
    history: list[dict] = field(default_factory=list)

    @property
    def active(self) -> bool:
        return self.state in ("queued", "leased", "running")

    def note(self, state: str, **extra) -> None:
        self.history.append({"at": iso(), "state": state, **extra})
        del self.history[:-_HISTORY_LIMIT]

    def public(self) -> dict:
        """Só o que o card da biblioteca mostra — mesma disciplina de Lock.public()."""
        return {
            "state": self.state,
            "progress": self.progress,
            "attempts": self.attempts,
            "error": self.error,
            "segments": len(self.segments),
            "finished_at": self.finished_at,
        }

    def to_json(self) -> dict:
        return asdict(self)


class Sam3Queue:
    def __init__(self) -> None:
        self._by_object: dict[str, dict[str, QueueItem]] = {}
        self._paths: dict[str, Path] = {}
        self._lock = threading.Lock()
        # Acordar o long-poll do worker assim que algo entra na fila, em vez de
        # deixá-lo esperando os 25 s inteiros.
        self._arrivals: asyncio.Event | None = None

    # -- persistência ------------------------------------------------------

    def bind(self, object_id: str, output_root: Path) -> None:
        """Registra onde fica o espelho deste objeto e carrega o que houver."""
        path = output_root / "sam3_queue.json"
        with self._lock:
            if self._paths.get(object_id) == path and object_id in self._by_object:
                return
            self._paths[object_id] = path
            self._by_object[object_id] = self._load(path)

    def _load(self, path: Path) -> dict[str, QueueItem]:
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

        items: dict[str, QueueItem] = {}
        for relpath, raw in (data.get("videos") or {}).items():
            item = QueueItem(
                video_id=raw.get("video_id", ""),
                relpath=relpath,
                name=raw.get("name", relpath),
                export_root=raw.get("export_root", ""),
                segments=list(raw.get("segments") or []),
                state=raw.get("state", "queued"),
                attempts=int(raw.get("attempts") or 0),
                force=bool(raw.get("force")),
                enqueued_at=raw.get("enqueued_at", ""),
                enqueued_by=raw.get("enqueued_by"),
                progress=raw.get("progress") or {},
                result=raw.get("result"),
                error=raw.get("error"),
                history=list(raw.get("history") or []),
            )
            # Um lease não sobrevive a um restart: o runner que o detinha morreu
            # junto com a conexão. Devolver para a fila é sempre a resposta certa
            # — o marcador por segmento faz o retrabalho ser barato.
            if item.state in ("leased", "running"):
                item.state = "queued"
                item.note("queued", reason="servidor reiniciou durante o processamento")
            items[relpath] = item
        return items

    def _flush(self, object_id: str) -> None:
        path = self._paths.get(object_id)
        if path is None:
            return
        payload = {
            "schema_version": SCHEMA_VERSION,
            "object_id": object_id,
            "updated_at": iso(),
            "videos": {
                item.relpath: item.to_json()
                for item in self._by_object.get(object_id, {}).values()
            },
        }
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name("sam3_queue.json.tmp")
            with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(json.dumps(payload, ensure_ascii=False, indent=2))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, path)
        except OSError:
            pass

    # -- expiração preguiçosa ---------------------------------------------

    def _expire(self, object_id: str) -> bool:
        """Devolve à fila o que passou do lease. Sem task de fundo: toda leitura
        filtra, o que também dá o comportamento de restart de graça."""
        now = time.time()
        changed = False
        for item in self._by_object.get(object_id, {}).values():
            if item.state not in ("leased", "running"):
                continue
            if (item.lease_expires_at_epoch or 0) > now:
                continue
            item.lease_id = None
            item.lease_expires_at_epoch = None
            item.worker = None
            if item.attempts >= MAX_ATTEMPTS:
                item.state = "error"
                item.error = (
                    f"o lease expirou {item.attempts}x — o runner provavelmente "
                    "está morrendo neste vídeo"
                )
                item.finished_at = iso()
                item.note("error", error="lease expirado demais")
            else:
                item.state = "queued"
                item.note("queued", reason="lease expirou")
            changed = True
        return changed

    # -- operador ----------------------------------------------------------

    def enqueue(
        self,
        object_id: str,
        *,
        video_id: str,
        relpath: str,
        name: str,
        export_root: str,
        segments: list[str],
        user: str | None,
        force: bool = False,
    ) -> QueueItem:
        with self._lock:
            self._expire(object_id)
            items = self._by_object.setdefault(object_id, {})
            existing = items.get(relpath)
            if existing is not None and existing.active:
                return existing
            if existing is not None and existing.state == "done" and not force:
                return existing

            item = QueueItem(
                video_id=video_id,
                relpath=relpath,
                name=name,
                export_root=export_root,
                segments=segments,
                state="queued",
                force=force,
                enqueued_at=iso(),
                enqueued_by=user,
                progress={"segments_done": 0, "segments_total": len(segments)},
            )
            item.note("queued", by=user)
            items[relpath] = item
            self._flush(object_id)
        self._wake()
        return item

    def cancel(self, object_id: str, relpath: str) -> QueueItem | None:
        with self._lock:
            self._expire(object_id)
            item = self._by_object.get(object_id, {}).get(relpath)
            if item is None or not item.active:
                return item
            if item.state == "queued":
                item.state = "cancelled"
                item.finished_at = iso()
                item.note("cancelled")
            else:
                # Já em processamento: o runner vê a flag no próximo heartbeat e
                # para num ponto limpo, entre segmentos.
                item.cancel_requested = True
                item.note("cancel_requested")
            self._flush(object_id)
            return item

    def list(self, object_id: str) -> list[QueueItem]:
        with self._lock:
            if self._expire(object_id):
                self._flush(object_id)
            items = list(self._by_object.get(object_id, {}).values())
        order = {state: index for index, state in enumerate(STATES)}
        return sorted(items, key=lambda i: (order.get(i.state, 9), i.enqueued_at))

    def get(self, object_id: str, relpath: str) -> QueueItem | None:
        with self._lock:
            self._expire(object_id)
            return self._by_object.get(object_id, {}).get(relpath)

    def public(self, object_id: str, relpath: str) -> dict | None:
        item = self.get(object_id, relpath)
        return item.public() if item else None

    def map_for(self, object_id: str) -> dict[str, dict]:
        """relpath -> payload público, para a listagem da biblioteca."""
        return {item.relpath: item.public() for item in self.list(object_id)}

    # -- worker ------------------------------------------------------------

    def take(self, worker: str, lease_seconds: int = DEFAULT_LEASE_SECONDS) -> tuple[str, QueueItem] | None:
        """Entrega o próximo job pendente, de qualquer objeto."""
        with self._lock:
            for object_id in list(self._by_object):
                self._expire(object_id)
                pending = [i for i in self._by_object[object_id].values() if i.state == "queued"]
                if not pending:
                    continue
                pending.sort(key=lambda i: i.enqueued_at)
                item = pending[0]
                item.state = "leased"
                item.lease_id = secrets.token_urlsafe(12)
                item.lease_expires_at_epoch = time.time() + lease_seconds
                item.worker = worker
                item.attempts += 1
                item.started_at = iso()
                item.cancel_requested = False
                item.note("leased", worker=worker, attempt=item.attempts)
                self._flush(object_id)
                return object_id, item
        return None

    def _find_lease(self, lease_id: str) -> tuple[str, QueueItem] | None:
        for object_id, items in self._by_object.items():
            for item in items.values():
                if item.lease_id == lease_id:
                    return object_id, item
        return None

    def heartbeat(
        self, lease_id: str, progress: dict | None, lease_seconds: int = DEFAULT_LEASE_SECONDS
    ) -> tuple[QueueItem, bool] | None:
        with self._lock:
            found = self._find_lease(lease_id)
            if found is None:
                return None
            object_id, item = found
            if (item.lease_expires_at_epoch or 0) <= time.time():
                return None
            item.state = "running"
            item.lease_expires_at_epoch = time.time() + lease_seconds
            if progress:
                item.progress = progress
            self._flush(object_id)
            return item, item.cancel_requested

    def finish(
        self, lease_id: str, *, state: str, result: dict | None, error: str | None
    ) -> tuple[str, QueueItem] | None:
        with self._lock:
            found = self._find_lease(lease_id)
            if found is None:
                return None
            object_id, item = found
            item.state = state if state in ("done", "error", "cancelled") else "error"
            item.result = result
            item.error = error
            item.finished_at = iso()
            item.lease_id = None
            item.lease_expires_at_epoch = None
            item.cancel_requested = False
            if item.state == "done":
                item.progress = {
                    **item.progress,
                    "segments_done": item.progress.get("segments_total", len(item.segments)),
                }
            item.note(item.state, error=error)
            self._flush(object_id)
            return object_id, item

    # -- long poll ---------------------------------------------------------

    def _wake(self) -> None:
        event = self._arrivals
        if event is not None:
            try:
                event.set()
            except RuntimeError:
                pass

    def arrivals(self) -> asyncio.Event:
        if self._arrivals is None:
            self._arrivals = asyncio.Event()
        return self._arrivals

    def has_pending(self) -> bool:
        with self._lock:
            for object_id in list(self._by_object):
                self._expire(object_id)
                if any(i.state == "queued" for i in self._by_object[object_id].values()):
                    return True
        return False


_database_url = os.environ.get("DATABASE_URL")
if _database_url:
    from .sam3_postgres import PostgresSam3Queue

    queue = PostgresSam3Queue(_database_url)
else:
    queue = Sam3Queue()
