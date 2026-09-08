from __future__ import annotations

import threading
import time
from collections.abc import Callable


class WorkerStatusRegistry:
    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.time,
        stale_after: float = 75,
    ) -> None:
        self._clock = clock
        self._stale_after = stale_after
        self._lock = threading.Lock()
        self._state = "unavailable"
        self._message: str | None = None
        self._worker_id: str | None = None
        self._reported_at: float | None = None

    def report(self, state: str, message: str | None, worker_id: str) -> None:
        if state not in {"loading", "ready", "busy", "error"}:
            raise ValueError(f"estado de worker invalido: {state}")
        with self._lock:
            # Um container com erro de configuracao reinicia e publica
            # ``loading`` novamente. Nao esconda a causa durante esse ciclo; um
            # ``ready`` posterior confirma que a configuracao foi corrigida.
            if state == "loading" and self._state == "error" and self._worker_id == worker_id:
                return
            self._state = state
            self._message = message
            self._worker_id = worker_id
            self._reported_at = self._clock()

    def public(self) -> dict:
        with self._lock:
            state = self._state
            message = self._message
            worker_id = self._worker_id
            reported_at = self._reported_at
        if reported_at is None or (
            state != "error" and self._clock() - reported_at > self._stale_after
        ):
            state = "unavailable"
            message = "worker SAM3 sem heartbeat"
        return {
            "state": state,
            "message": message,
            "worker_id": worker_id,
            "age_seconds": None if reported_at is None else max(0, round(self._clock() - reported_at)),
        }


worker_status = WorkerStatusRegistry()
