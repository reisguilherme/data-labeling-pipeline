"""Execução de subprocessos ffmpeg com progresso, cancelamento e SSE.

Nada aqui pode bloquear o event loop: todo ffmpeg roda em thread via
`subprocess.Popen`, empurrando progresso de volta com `call_soon_threadsafe`.

Threads em vez de `asyncio.create_subprocess_exec` de propósito: subprocess
asyncio no Windows exige o Proactor loop. É o default hoje, mas qualquer mudança
de policy (uvloop, `--loop`, uma lib setando WindowsSelectorEventLoopPolicy) vira
`NotImplementedError` em runtime. `Popen` funciona sob qualquer policy e
`terminate()` mapeia limpo para TerminateProcess.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import subprocess
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from .videos import iso

log = logging.getLogger("movies-screening-tool.jobs")

JobKind = Literal[
    "proxy_full", "proxy_window", "export", "gcs_download", "dataset_export"
]
JobState = Literal["queued", "running", "done", "cancelled", "error"]

# Extração e export saturam CPU; paralelizar só causa thrash de disco e decode.
# Continua 1 mesmo com vários usuários: a máquina é uma só. O que muda com a LAN
# é que a espera passa a ser compartilhada — daí `queue_pos` no payload, para a
# interface dizer "2 na frente" em vez de parecer travada.
_heavy = asyncio.Semaphore(1)

_counter = itertools.count(1)

# No Windows, evita abrir uma janela de console para cada ffmpeg.
_NO_WINDOW = {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}


def ffmpeg_progress(line: str) -> int | None:
    """Progresso a partir de `-progress pipe:1` do ffmpeg: "frame=123" -> 123."""
    if not line.startswith("frame="):
        return None
    try:
        return int(line.split("=", 1)[1])
    except ValueError:
        return None


@dataclass
class Job:
    job_id: str
    kind: JobKind
    object_id: str
    video_id: str
    state: JobState = "queued"
    current: int = 0
    total: int = 0
    message: str = ""
    error: str | None = None
    result: dict = field(default_factory=dict)
    started_at: str = field(default_factory=iso)
    finished_at: str | None = None
    # Quem disparou. `client_id` é por ABA (não por usuário): trocar de vídeo só
    # pode cancelar as extrações da própria aba, senão um usuário abrindo um
    # vídeo mataria a extração 4K de outro.
    client_id: str | None = None
    user: str | None = None
    queue_pos: int = 0

    _process: subprocess.Popen | None = field(default=None, repr=False)
    _cancelled: bool = field(default=False, repr=False)
    _subscribers: list[asyncio.Queue] = field(default_factory=list, repr=False)

    @property
    def progress(self) -> float:
        if self.state in ("done",):
            return 1.0
        if self.total <= 0:
            return 0.0
        return min(self.current / self.total, 1.0)

    @property
    def terminal(self) -> bool:
        return self.state in ("done", "cancelled", "error")

    def payload(self) -> dict:
        return {
            "job_id": self.job_id,
            "kind": self.kind,
            "object_id": self.object_id,
            "video_id": self.video_id,
            "state": self.state,
            "current": self.current,
            "total": self.total,
            "progress": round(self.progress, 4),
            "message": self.message,
            "error": self.error,
            "result": self.result,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "client_id": self.client_id,
            "user": self.user,
            "queue_pos": self.queue_pos,
        }


class JobManager:
    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def for_video(self, object_id: str, video_id: str) -> list[Job]:
        # Chaveado por (objeto, vídeo): video_id é sha1 do caminho RELATIVO e não
        # é salgado por objeto, então dois objetos com o mesmo relpath geram o
        # mesmo id. Sem o objeto na chave, um cancelaria os jobs do outro.
        return [
            job
            for job in self._jobs.values()
            if job.object_id == object_id and job.video_id == video_id
        ]

    def active_for_video(self, object_id: str, video_id: str) -> list[Job]:
        return [job for job in self.for_video(object_id, video_id) if not job.terminal]

    def active_for_object(self, object_id: str) -> list[Job]:
        return [
            job for job in self._jobs.values()
            if job.object_id == object_id and not job.terminal
        ]

    def create(
        self,
        kind: JobKind,
        object_id: str,
        video_id: str,
        total: int,
        message: str = "",
        *,
        client_id: str | None = None,
        user: str | None = None,
    ) -> Job:
        job = Job(
            job_id=f"j{next(_counter):05d}",
            kind=kind,
            object_id=object_id,
            video_id=video_id,
            total=total,
            message=message,
            client_id=client_id,
            user=user,
        )
        self._jobs[job.job_id] = job
        self._renumber_queue()
        return job

    def _renumber_queue(self) -> None:
        """Posição na fila do semáforo pesado, para a UI explicar a espera."""
        waiting = sorted(
            (job for job in self._jobs.values() if job.state == "queued"),
            key=lambda job: job.job_id,
        )
        for position, job in enumerate(waiting):
            job.queue_pos = position

    # -- pub/sub -----------------------------------------------------------

    def subscribe(self, job: Job) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue()
        job._subscribers.append(queue)
        queue.put_nowait(job.payload())
        return queue

    def unsubscribe(self, job: Job, queue: asyncio.Queue) -> None:
        if queue in job._subscribers:
            job._subscribers.remove(queue)

    def _publish(self, job: Job) -> None:
        payload = job.payload()
        for queue in list(job._subscribers):
            queue.put_nowait(payload)

    # -- execução ----------------------------------------------------------

    async def run(
        self,
        job: Job,
        argv: list[str],
        *,
        on_success: Callable[[Job], dict] | None = None,
        on_cleanup: Callable[[Job], None] | None = None,
        parse: Callable[[str], int | None] | None = ffmpeg_progress,
        env: dict[str, str] | None = None,
        failure_hint: str = "ffmpeg",
    ) -> Job:
        """Roda um subprocesso até o fim, publicando progresso.

        `on_success` roda em thread depois de um exit 0 e devolve o `result`.
        `on_cleanup` roda em thread após cancelamento ou erro.
        `parse` extrai o progresso de cada linha do stdout; `None` desliga a
        leitura de progresso (o chamador atualiza `job.current` por conta, como
        o download do GCS, que conta por bloco de arquivos).
        """
        loop = asyncio.get_running_loop()

        async with _heavy:
            if job._cancelled:
                job.state = "cancelled"
                job.finished_at = iso()
                self._publish(job)
                return job

            job.state = "running"
            job.queue_pos = 0
            self._renumber_queue()
            self._publish(job)

            def bump(current: int) -> None:
                job.current = current
                self._publish(job)

            def worker() -> tuple[int, str]:
                process = subprocess.Popen(
                    argv,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    bufsize=1,
                    env=env,
                    **_NO_WINDOW,
                )
                with self._lock:
                    job._process = process

                assert process.stdout is not None
                for raw in process.stdout:
                    if parse is None:
                        continue
                    value = parse(raw.strip())
                    if value is not None:
                        loop.call_soon_threadsafe(bump, value)

                stderr = process.stderr.read() if process.stderr else ""
                return process.wait(), stderr

            try:
                code, stderr = await asyncio.to_thread(worker)
            except Exception as exc:  # noqa: BLE001
                job.state = "error"
                job.error = str(exc)
                job.finished_at = iso()
                self._publish(job)
                return job

            if job._cancelled:
                job.state = "cancelled"
                job.message = "cancelado"
                if on_cleanup:
                    await asyncio.to_thread(on_cleanup, job)
            elif code != 0:
                job.state = "error"
                job.error = (
                    (stderr or "").strip()[-2000:] or f"{failure_hint} saiu com {code}"
                )
                log.error("job %s falhou: %s", job.job_id, job.error)
                if on_cleanup:
                    await asyncio.to_thread(on_cleanup, job)
            else:
                job.state = "done"
                if on_success:
                    try:
                        job.result = await asyncio.to_thread(on_success, job)
                    except Exception as exc:  # noqa: BLE001
                        job.state = "error"
                        job.error = f"pós-processamento falhou: {exc}"

            job.finished_at = iso()
            self._publish(job)
            return job

    def cancel(self, job_id: str) -> bool:
        job = self._jobs.get(job_id)
        if job is None or job.terminal:
            return False
        job._cancelled = True
        with self._lock:
            process = job._process
        if process and process.poll() is None:
            try:
                process.terminate()
            except OSError:
                return False
        else:
            # Ainda na fila do semáforo: `run` verifica a flag antes de começar.
            job.state = "cancelled"
            job.finished_at = iso()
            self._publish(job)
        return True

    def cancel_others(
        self,
        object_id: str,
        keep_video_id: str | None,
        kinds: tuple[JobKind, ...],
        *,
        client_id: str | None = None,
    ) -> int:
        """Cancela extrações de outros vídeos ao trocar de vídeo.

        Escopado por objeto E por aba (`client_id`). A versão anterior cancelava
        TODA extração ativa do processo: com mais de um usuário, abrir um vídeo
        matava a extração 4K de quem estivesse trabalhando ao lado, sempre. Sem
        `client_id` (chamada interna) mantém o comportamento amplo dentro do
        objeto.
        """
        count = 0
        for job in list(self._jobs.values()):
            if job.terminal or job.kind not in kinds:
                continue
            if job.object_id != object_id:
                continue
            if keep_video_id is not None and job.video_id == keep_video_id:
                continue
            if client_id is not None and job.client_id != client_id:
                continue
            if self.cancel(job.job_id):
                count += 1
        return count

    def prune(self, keep: int = 200) -> None:
        terminal = [j for j in self._jobs.values() if j.terminal]
        if len(terminal) <= keep:
            return
        terminal.sort(key=lambda j: j.finished_at or "")
        for job in terminal[: len(terminal) - keep]:
            self._jobs.pop(job.job_id, None)


jobs = JobManager()


async def sse_stream(job: Job, request: Any):
    """Gerador de eventos SSE. Fecha ao terminar o job ou ao cliente sumir."""
    queue = jobs.subscribe(job)
    try:
        while True:
            try:
                payload = await asyncio.wait_for(queue.get(), timeout=15)
            except asyncio.TimeoutError:
                yield ": keepalive\n\n"
                if await request.is_disconnected():
                    return
                continue

            import json

            yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
            if payload["state"] in ("done", "cancelled", "error"):
                return
            if await request.is_disconnected():
                return
    finally:
        jobs.unsubscribe(job, queue)
