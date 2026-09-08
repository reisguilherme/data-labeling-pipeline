"""Sessão interativa do SAM3: ajustar a caixa inicial vendo o resultado.

O problema que isto resolve: a caixa que a triagem marcou é um retângulo feito à
mão sobre um frame; a partir dela o SAM3 pode segmentar exatamente o objeto — ou
vazar para o fundo inteiro. Descobrir isso depois de propagar 800 frames é caro,
e a correção seria refazer tudo.

Aqui o runner carrega o segmento UMA vez, mantém o estado do SAM3 na GPU e
responde cada ajuste segmentando só o frame do prompt. A partir do segundo teste
a volta é quase imediata, porque o custo real — decodificar os frames e montar o
estado — já foi pago.

A arquitetura continua PULL: o runner nunca precisa ser alcançável pela rede.
Ele abre a sessão e fica num long-poll de comandos até fechar ou expirar. Por
HTTP trafega comando e resultado; a MÁSCARA vai pelo disco compartilhado, como
todo o resto dos dados.
"""

from __future__ import annotations

import asyncio
import secrets
import threading
import time
from dataclasses import dataclass, field

from .videos import iso

# Uma sessão segura VRAM: um segmento 4K com centenas de frames não é barato de
# manter carregado. Sem interação, é largada.
IDLE_TIMEOUT = 300

# Quanto o runner espera por um comando antes de devolver vazio e perguntar de
# novo. Conexão curta, sem virar polling ocupado.
COMMAND_WAIT = 25


@dataclass
class Preview:
    """Resultado de um teste: o que o SAM3 devolveu para aquela caixa."""

    seq: int
    mask_url: str | None = None
    bbox: list[float] | None = None
    area_frac: float | None = None
    error: str | None = None
    at: str = field(default_factory=iso)

    def to_json(self) -> dict:
        return {
            "seq": self.seq,
            "mask_url": self.mask_url,
            "bbox": self.bbox,
            "area_frac": self.area_frac,
            "error": self.error,
            "at": self.at,
        }


@dataclass
class Session:
    session_id: str
    object_id: str
    video_id: str
    segment: str
    segment_dir: str
    frame_idx: int
    user: str | None
    state: str = "opening"  # opening | ready | busy | closed | error
    error: str | None = None
    opened_at: str = field(default_factory=iso)
    expires_at_epoch: float = field(default_factory=lambda: time.time() + IDLE_TIMEOUT)

    pending: dict | None = None
    seq: int = 0
    last: Preview | None = None
    worker: str | None = None

    @property
    def alive(self) -> bool:
        return self.state in ("opening", "ready", "busy")

    def touch(self) -> None:
        self.expires_at_epoch = time.time() + IDLE_TIMEOUT

    def public(self) -> dict:
        return {
            "session_id": self.session_id,
            "object_id": self.object_id,
            "video_id": self.video_id,
            "segment": self.segment,
            "frame_idx": self.frame_idx,
            "state": self.state,
            "error": self.error,
            "expires_in": max(0, round(self.expires_at_epoch - time.time())),
            "preview": self.last.to_json() if self.last else None,
        }


class SessionRegistry:
    def __init__(self) -> None:
        self._sessions: dict[str, Session] = {}
        self._lock = threading.Lock()
        self._events: dict[str, asyncio.Event] = {}
        self._arrivals: asyncio.Event | None = None

    # -- expiração preguiçosa ---------------------------------------------

    def _expire(self) -> None:
        """Sem task de fundo: toda leitura filtra. Mesma disciplina das travas
        de vídeo, e pelo mesmo motivo — um processo que morreu não deixa VRAM
        presa para sempre."""
        now = time.time()
        for session in list(self._sessions.values()):
            if session.alive and session.expires_at_epoch <= now:
                session.state = "closed"
                session.error = "sessão expirou por inatividade"
                self._wake(session.session_id)

    def get(self, session_id: str) -> Session | None:
        with self._lock:
            self._expire()
            return self._sessions.get(session_id)

    def for_segment(self, object_id: str, video_id: str, segment: str) -> Session | None:
        with self._lock:
            self._expire()
            for session in self._sessions.values():
                if (
                    session.alive
                    and session.object_id == object_id
                    and session.video_id == video_id
                    and session.segment == segment
                ):
                    return session
        return None

    def active_for_object(self, object_id: str) -> list[Session]:
        with self._lock:
            self._expire()
            return [
                session for session in self._sessions.values()
                if session.alive and session.object_id == object_id
            ]

    # -- ciclo de vida -----------------------------------------------------

    def open(
        self,
        *,
        object_id: str,
        video_id: str,
        segment: str,
        segment_dir: str,
        frame_idx: int,
        user: str | None,
    ) -> Session:
        # Reusar a sessão viva do mesmo segmento é o ponto: abrir outra
        # carregaria o segmento de novo na GPU sem necessidade.
        existing = self.for_segment(object_id, video_id, segment)
        if existing is not None:
            existing.touch()
            return existing

        session = Session(
            session_id=secrets.token_urlsafe(10),
            object_id=object_id,
            video_id=video_id,
            segment=segment,
            segment_dir=segment_dir,
            frame_idx=frame_idx,
            user=user,
        )
        with self._lock:
            self._sessions[session.session_id] = session
        self._wake_arrivals()
        return session

    def take(self, worker: str) -> Session | None:
        """Próxima sessão a abrir. Chamado pelo runner."""
        with self._lock:
            self._expire()
            for session in self._sessions.values():
                if session.state == "opening" and session.worker is None:
                    session.worker = worker
                    return session
        return None

    def mark_ready(self, session_id: str) -> Session | None:
        session = self.get(session_id)
        if session is None or not session.alive:
            return None
        session.state = "ready"
        session.touch()
        self._wake(session_id)
        return session

    def fail(self, session_id: str, error: str) -> Session | None:
        session = self.get(session_id)
        if session is None:
            return None
        session.state = "error"
        session.error = error
        self._wake(session_id)
        return session

    def close(self, session_id: str) -> bool:
        session = self.get(session_id)
        if session is None:
            return False
        session.state = "closed"
        self._wake(session_id)
        return True

    # -- comandos ----------------------------------------------------------

    def request_preview(self, session_id: str, boxes: list[dict]) -> Session | None:
        session = self.get(session_id)
        if session is None or not session.alive:
            return None
        session.seq += 1
        session.state = "busy"
        session.pending = {"kind": "preview", "seq": session.seq, "boxes": boxes}
        session.touch()
        self._wake(session_id)
        return session

    def take_command(self, session_id: str) -> dict | None:
        session = self.get(session_id)
        if session is None:
            return None
        if not session.alive:
            return {"kind": "close"}
        command = session.pending
        session.pending = None
        return command

    def deliver(
        self,
        session_id: str,
        *,
        seq: int,
        mask_url: str | None,
        bbox: list[float] | None,
        area_frac: float | None,
        error: str | None,
    ) -> Session | None:
        session = self.get(session_id)
        if session is None:
            return None
        # Resultado atrasado de um teste que já foi substituído por outro:
        # descarta, em vez de mostrar a máscara da caixa errada.
        if seq != session.seq:
            return session
        session.last = Preview(
            seq=seq, mask_url=mask_url, bbox=bbox, area_frac=area_frac, error=error
        )
        session.state = "ready"
        session.touch()
        self._wake(session_id)
        return session

    # -- long poll ---------------------------------------------------------

    def event(self, session_id: str) -> asyncio.Event:
        if session_id not in self._events:
            self._events[session_id] = asyncio.Event()
        return self._events[session_id]

    def _wake(self, session_id: str) -> None:
        event = self._events.get(session_id)
        if event is not None:
            try:
                event.set()
            except RuntimeError:
                pass

    def arrivals(self) -> asyncio.Event:
        if self._arrivals is None:
            self._arrivals = asyncio.Event()
        return self._arrivals

    def _wake_arrivals(self) -> None:
        if self._arrivals is not None:
            try:
                self._arrivals.set()
            except RuntimeError:
                pass

    def prune(self) -> None:
        with self._lock:
            self._expire()
            dead = [
                sid
                for sid, session in self._sessions.items()
                if not session.alive and session.expires_at_epoch + 600 < time.time()
            ]
            for sid in dead:
                self._sessions.pop(sid, None)
                self._events.pop(sid, None)


sessions = SessionRegistry()
