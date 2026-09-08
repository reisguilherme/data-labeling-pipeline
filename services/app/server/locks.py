"""Trava de vídeo: um vídeo em triagem por vez, por pessoa.

Estado volátil de sessão, deliberadamente FORA do annotations.json. Um heartbeat
a cada 30 s por usuário reescreveria um documento de ~500 KB (mais a cópia .bak,
mais o snapshot a cada 50 saves) a cada poucos segundos, segurando o lock da
store e competindo com os saves de verdade. O annotations.json é artefato durável
do dataset; sessão não tem o que fazer lá.

Expiração é preguiçosa — toda leitura filtra o que venceu. Isso dá o
comportamento de reinício de graça: o locks.json é lido só para auditoria e o que
estiver vencido cai na primeira consulta, então em no máximo um TTL a mesa está
limpa e cada cliente vivo readquire no próximo heartbeat.
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from .videos import iso

# Três batidas perdidas antes de liberar. Um browser que morreu segura o vídeo
# por no máximo TTL_SECONDS.
HEARTBEAT_SECONDS = 30
TTL_SECONDS = 90

_MIRROR_DEBOUNCE = 5.0


@dataclass
class Lock:
    object_id: str
    video_id: str
    relpath: str
    user: str
    client_id: str
    acquired_at: str
    heartbeat_at: str
    expires_at: float  # monotônico-ish: time.time(), comparado só localmente
    stolen_from: str | None = None

    def to_json(self) -> dict:
        return {
            "object_id": self.object_id,
            "video_id": self.video_id,
            "relpath": self.relpath,
            "user": self.user,
            "client_id": self.client_id,
            "acquired_at": self.acquired_at,
            "heartbeat_at": self.heartbeat_at,
            # Instante ABSOLUTO, não duração restante. Gravar "faltam 90 s" faria
            # o _load somar 90 s ao momento do restart — uma trava abandonada
            # ontem renasceria por mais um TTL a cada vez que o servidor sobe.
            "expires_at_epoch": round(self.expires_at, 3),
            "expires_in": max(0, round(self.expires_at - time.time())),
            "stolen_from": self.stolen_from,
        }

    def public(self) -> dict:
        """O que a biblioteca mostra num card travado."""
        return {"user": self.user, "since": self.acquired_at}


class LockHeld(Exception):
    def __init__(self, lock: Lock) -> None:
        super().__init__(f"vídeo em uso por {lock.user}")
        self.lock = lock


class LockRegistry:
    def __init__(self) -> None:
        self.path: Path | None = None
        self._locks: dict[tuple[str, str], Lock] = {}
        self._lock = threading.Lock()
        self._last_mirror = 0.0

    def bind(self, workspace_root: Path) -> None:
        self.path = workspace_root / "locks.json"
        self._load()

    def _load(self) -> None:
        """Retoma as travas ainda válidas; descarta as que venceram fora do ar.

        Uma trava só sobrevive a um restart se o instante de expiração dela ainda
        estiver no futuro. Um arquivo sem `expires_at_epoch` é de uma versão
        anterior e é sempre descartado — melhor liberar um vídeo a mais do que
        deixar alguém preso atrás de uma trava fantasma.
        """
        if self.path is None or not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        now = time.time()
        for item in data.get("locks", []):
            if "object_id" not in item or "video_id" not in item:
                continue
            expires_at = float(item.get("expires_at_epoch") or 0)
            if expires_at <= now:
                continue
            self._locks[(item["object_id"], item["video_id"])] = Lock(
                object_id=item["object_id"],
                video_id=item["video_id"],
                relpath=item.get("relpath", ""),
                user=item.get("user", "?"),
                client_id=item.get("client_id", ""),
                acquired_at=item.get("acquired_at", ""),
                heartbeat_at=item.get("heartbeat_at", ""),
                expires_at=expires_at,
                stolen_from=item.get("stolen_from"),
            )

    def _mirror(self, *, force: bool = False) -> None:
        if self.path is None:
            return
        now = time.time()
        if not force and now - self._last_mirror < _MIRROR_DEBOUNCE:
            return
        self._last_mirror = now
        payload = {
            "schema_version": 1,
            "updated_at": iso(),
            "locks": [lock.to_json() for lock in self._locks.values()],
        }
        try:
            tmp = self.path.with_name("locks.json.tmp")
            tmp.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            os.replace(tmp, self.path)
        except OSError:
            pass

    # -- consulta ----------------------------------------------------------

    def _live(self, key: tuple[str, str]) -> Lock | None:
        lock = self._locks.get(key)
        if lock is None:
            return None
        if lock.expires_at <= time.time():
            self._locks.pop(key, None)
            return None
        return lock

    def get(self, object_id: str, video_id: str) -> Lock | None:
        with self._lock:
            return self._live((object_id, video_id))

    def map_for(self, object_id: str) -> dict[str, Lock]:
        """Travas vivas do objeto, por video_id — usado na listagem."""
        now = time.time()
        with self._lock:
            expired = [
                key for key, lock in self._locks.items() if lock.expires_at <= now
            ]
            for key in expired:
                self._locks.pop(key, None)
            return {
                video_id: lock
                for (oid, video_id), lock in self._locks.items()
                if oid == object_id
            }

    def held_by(self, object_id: str, video_id: str, client_id: str) -> bool:
        lock = self.get(object_id, video_id)
        return lock is not None and lock.client_id == client_id

    # -- escrita -----------------------------------------------------------

    def acquire(
        self,
        object_id: str,
        video_id: str,
        relpath: str,
        user: str,
        client_id: str,
        *,
        force: bool = False,
    ) -> Lock:
        key = (object_id, video_id)
        now = time.time()
        with self._lock:
            current = self._live(key)
            if current is not None and current.client_id != client_id and not force:
                raise LockHeld(current)

            stolen_from = (
                current.user
                if current is not None and current.client_id != client_id
                else None
            )
            if current is not None and current.client_id == client_id:
                # Renovação: preserva quando a triagem começou de verdade.
                current.heartbeat_at = iso()
                current.expires_at = now + TTL_SECONDS
                self._mirror()
                return current

            lock = Lock(
                object_id=object_id,
                video_id=video_id,
                relpath=relpath,
                user=user,
                client_id=client_id,
                acquired_at=iso(),
                heartbeat_at=iso(),
                expires_at=now + TTL_SECONDS,
                stolen_from=stolen_from,
            )
            self._locks[key] = lock
            self._mirror(force=True)
            return lock

    def heartbeat(self, object_id: str, video_id: str, client_id: str) -> Lock | None:
        with self._lock:
            lock = self._live((object_id, video_id))
            if lock is None or lock.client_id != client_id:
                return None
            lock.heartbeat_at = iso()
            lock.expires_at = time.time() + TTL_SECONDS
            self._mirror()
            return lock

    def release(self, object_id: str, video_id: str, client_id: str) -> bool:
        key = (object_id, video_id)
        with self._lock:
            lock = self._locks.get(key)
            if lock is None or lock.client_id != client_id:
                return False
            self._locks.pop(key, None)
            self._mirror(force=True)
            return True

    def release_all_for_client(self, client_id: str) -> int:
        with self._lock:
            keys = [
                key for key, lock in self._locks.items() if lock.client_id == client_id
            ]
            for key in keys:
                self._locks.pop(key, None)
            if keys:
                self._mirror(force=True)
            return len(keys)


locks = LockRegistry()
