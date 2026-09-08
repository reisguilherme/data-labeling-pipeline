"""Consome a fila da triagem por HTTP e processa os segmentos.

Usa só a stdlib: o container do runner já carrega torch e CUDA, e acrescentar
`requests` para três chamadas seria dependência por conforto.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from .config import RunnerConfig

log = logging.getLogger("sam3_runner.queue")

# Backoff quando a triagem não responde. Um restart do outro container não pode
# matar o worker — ele espera e volta.
BACKOFF_START = 2
BACKOFF_MAX = 60


class QueueError(RuntimeError):
    pass


class LeaseLost(RuntimeError):
    """O lease expirou ou foi para outro worker: PARE de processar.

    Continuar significaria dois workers escrevendo os mesmos arquivos no mesmo
    segmento ao mesmo tempo.
    """


class StatusHeartbeat:
    """Renova o status visível enquanto uma operação longa bloqueia o loop.

    O download/carregamento inicial do SAM3 e a inferência de um trecho podem
    ultrapassar o TTL da interface. O pulso é deliberadamente independente da
    inferência: uma falha de telemetria não cancela o trabalho.
    """

    def __init__(
        self,
        client: "Client",
        state: str,
        message,
        *,
        interval: float = 25,
    ) -> None:
        self._client = client
        self._state = state
        self._message = message
        self._interval = interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _current_message(self) -> str | None:
        return self._message() if callable(self._message) else self._message

    def _report(self) -> None:
        self._client.status(self._state, self._current_message())

    def start(self) -> None:
        self._report()
        self._thread = threading.Thread(target=self._run, name="sam3-status", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            self._report()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self._interval + 2)

    def __enter__(self) -> "StatusHeartbeat":
        self.start()
        return self

    def __exit__(self, *_exc) -> None:
        self.stop()


class JobHeartbeat(StatusHeartbeat):
    """Mantém lease e progresso do job vivos durante a propagação."""

    def __init__(self, client: "Client", lease_id: str, progress, *, interval: float = 25) -> None:
        self._lease_id = lease_id
        self._progress = progress
        self._cancel_requested = threading.Event()
        self._lease_lost: LeaseLost | None = None
        super().__init__(client, "busy", self._busy_message, interval=interval)

    @property
    def cancel_requested(self) -> bool:
        return self._cancel_requested.is_set()

    def raise_if_lease_lost(self) -> None:
        if self._lease_lost is not None:
            raise self._lease_lost

    def _busy_message(self) -> str:
        progress = self._progress()
        current = progress.get("current") or "trecho"
        frames_done = progress.get("frames_done", 0)
        return f"processando {current} · {frames_done} frames concluídos"

    def _report(self) -> None:
        super()._report()
        try:
            if not self._client.heartbeat(self._lease_id, dict(self._progress())):
                self._cancel_requested.set()
        except LeaseLost as exc:
            self._lease_lost = exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            # Uma falha transitória não deve derrubar o processo que ainda pode
            # renovar o lease no próximo pulso.
            log.warning("nao foi possivel renovar lease do SAM3: %s", exc)


def hold_startup_error(client, exc: Exception, *, once: bool = False) -> int:
    """Mantem um erro de configuracao visivel sem reiniciar e reler 10 GB.

    A configuracao/secrets e montada no startup; depois de corrigi-la o operador
    reinicia o servico. Enquanto isso, o processo permanece barato e renova a
    mensagem para a interface.
    """
    message = f"{type(exc).__name__}: {exc}"
    client.status("error", message)
    if once:
        return 2
    log.error("worker SAM3 parado por erro de startup: %s", message)
    while True:
        time.sleep(60)
        client.status("error", message)


class Client:
    def __init__(self, cfg: RunnerConfig) -> None:
        self.cfg = cfg
        if not cfg.worker_token:
            raise QueueError(
                "MST_WORKER_TOKEN não definido — sem ele a triagem recusa as "
                "rotas de worker (e é o que impede que qualquer um roube um lease)"
            )

    def _request(self, method: str, path: str, body: dict | None = None, timeout: int = 90):
        url = f"{self.cfg.api}{path}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(url, data=data, method=method)
        request.add_header("X-MST-Worker", self.cfg.worker_token)
        request.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(request, timeout=timeout) as response:
            if response.status == 204:
                return None
            raw = response.read()
            return json.loads(raw) if raw else None

    def next_job(self) -> dict | None:
        path = (
            f"/api/sam3/next?worker={self.cfg.worker_id}"
            f"&lease={self.cfg.lease_seconds}&wait={self.cfg.poll_wait}"
        )
        return self._request("GET", path, timeout=self.cfg.poll_wait + 30)

    def status(self, state: str, message: str | None = None) -> None:
        try:
            self._request(
                "POST",
                "/api/sam3/status",
                {"state": state, "message": message, "worker_id": self.cfg.worker_id},
                timeout=15,
            )
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            # Status e telemetria nunca podem derrubar a inferencia. A fila e os
            # heartbeats continuam sendo autoritativos para o processamento.
            log.warning("nao foi possivel publicar status do worker: %s", exc)

    def heartbeat(self, lease_id: str, progress: dict) -> bool:
        """Devolve True se deve continuar, False se pediram cancelamento."""
        try:
            reply = self._request(
                "POST",
                f"/api/sam3/lease/{lease_id}/heartbeat?lease={self.cfg.lease_seconds}",
                {"progress": progress},
            )
        except urllib.error.HTTPError as exc:
            if exc.code == 409:
                raise LeaseLost("lease expirou ou foi reatribuído") from exc
            raise
        return not (reply or {}).get("cancel_requested", False)

    def finish(self, lease_id: str, state: str, result: dict | None, error: str | None) -> None:
        self._request(
            "POST",
            f"/api/sam3/lease/{lease_id}/result",
            {"state": state, "result": result, "error": error},
        )


def process_job(client: Client, job: dict, cfg: RunnerConfig, predictor, classes) -> None:
    """Processa um job (um vídeo = vários segmentos)."""
    from .model import autocast_context
    from .prompt import PromptError, load_prompt
    from .marker import read as read_marker
    from .segment import run_segment, should_skip

    lease_id = job["lease_id"]
    segments = job.get("segments") or []
    total_segments = len(segments)

    done = failed = skipped = 0
    frames_done = 0
    errors: list[str] = []
    segment_runs: list[dict] = []
    progress = {
        "segments_done": 0,
        "segments_total": total_segments,
        "frames_done": 0,
        "current": None,
    }
    heartbeat = JobHeartbeat(
        client,
        lease_id,
        lambda: progress,
        interval=max(10, min(30, cfg.lease_seconds // 3)),
    )

    def remember(segment: dict, run: dict) -> None:
        artifacts = run.get("artifacts") or {}
        segment_runs.append(
            {
                "segment": segment["segment"],
                "dir": segment["dir"],
                "status": run.get("status"),
                "prompt_digest": run.get("prompt_digest"),
                "frame_count": run.get("frame_count"),
                "frames_written": run.get("frames_written"),
                "objects": run.get("objects") or [],
                "params": run.get("params") or {},
                "model": run.get("model") or {},
                "artifacts": {
                    "format": artifacts.get("format"),
                    "files": artifacts.get("files"),
                    "empty": artifacts.get("empty"),
                },
                "error": run.get("error"),
            }
        )

    heartbeat.start()
    try:
        for index, segment in enumerate(segments, start=1):
            progress.update(
                {
                    "segments_done": index - 1,
                    "frames_done": frames_done,
                    "current": segment["segment"],
                }
            )
            heartbeat.raise_if_lease_lost()
            if heartbeat.cancel_requested:
                log.info("cancelamento pedido — parando em %s", segment["segment"])
                client.finish(lease_id, "cancelled", {"segments_done": done}, None)
                return

            segment_dir = Path(segment["dir"])
            if not segment_dir.is_dir():
                errors.append(f"{segment['segment']}: pasta não existe ({segment_dir})")
                failed += 1
                continue

            try:
                prompt = load_prompt(segment_dir)
            except PromptError as exc:
                errors.append(str(exc))
                failed += 1
                continue

        # O marcador é POR SEGMENTO, e é isso que faz o retry ser barato: um job
        # reentregue depois de uma queda retoma no segmento que faltou, em vez de
        # refazer os onze anteriores.
            if not job.get("force") and should_skip(prompt, cfg):
                cached = read_marker(prompt.marker_path)
                if cached:
                    remember(segment, cached)
                skipped += 1
                done += 1
                frames_done += prompt.frame_count
                progress.update({"segments_done": index, "frames_done": frames_done})
                continue

            seen_frames: set[int] = set()

            def note_frame(frame_idx: int) -> None:
                seen_frames.add(frame_idx)
                progress["frames_done"] = frames_done + len(seen_frames)

            log.info("  %s (%d frames)", segment["segment"], prompt.frame_count)
            with autocast_context():
                report = run_segment(predictor, prompt, cfg, classes, on_frame=note_frame)
            remember(segment, report.to_json())

            if report.status == "done":
                done += 1
                frames_done += report.frames_written
            else:
                failed += 1
                errors.append(f"{segment['segment']}: {report.error}")
            progress.update({"segments_done": index, "frames_done": frames_done})

        result = {
            "runner_version": cfg.params()["runner_version"],
            "segments_total": total_segments,
            "segments_done": done,
            "segments_skipped": skipped,
            "segments_failed": failed,
            "frames": frames_done,
            "classes": {name: index for index, name in enumerate(classes.names)},
            "segment_runs": segment_runs,
        }
        if failed:
            client.finish(lease_id, "error", result, "; ".join(errors[:5]))
        else:
            client.finish(lease_id, "done", result, None)
    finally:
        heartbeat.stop()


def serve(cfg: RunnerConfig, *, once: bool = False) -> int:
    """Loop principal do worker."""
    from pipeline_core.model_registry import ModelRegistry
    from pipeline_core.storage import MinioBlobStore

    from .classes import ClassMap
    from .model import build_predictor, device_info, free_vram, prepare_inference_checkpoint

    client = Client(cfg)

    info = device_info()
    log.info("torch %s cuda=%s %s", info.get("torch"), info.get("cuda"), info.get("device", ""))
    if not info.get("cuda"):
        log.warning("sem CUDA disponível: a inferência será impraticavelmente lenta")

    # Construir o modelo ANTES de pegar qualquer lease. Falhar aqui é um erro de
    # configuração com mensagem clara; falhar depois de já ter um lease deixa um
    # vídeo preso até o TTL expirar, três vezes, até virar erro permanente.
    log.info("carregando o modelo (pode baixar os pesos na primeira vez)…")
    if not cfg.models_config.is_file():
        raise RuntimeError(
            f"registro SAM3 ausente: {cfg.models_config}. "
            "Crie config/models.local.yaml com o checkpoint finetunado."
        )
    registry = ModelRegistry.load(cfg.models_config)
    predictor = None
    loaded_model_id: str | None = None

    def predictor_for(object_id: str):
        nonlocal predictor, loaded_model_id
        spec = registry.select(object_id)
        image_commit = os.environ.get("SAM3_COMMIT")
        if image_commit and spec.sam3_commit != image_commit:
            raise RuntimeError(
                f"modelo {spec.model_id} exige SAM3 {spec.sam3_commit}, imagem usa {image_commit}"
            )
        if predictor is not None and loaded_model_id == spec.model_id:
            return predictor
        checkpoint = registry.resolve_checkpoint(spec.model_id, cache_dir=cfg.model_cache)
        blob_store = MinioBlobStore.from_env()
        if blob_store is not None:
            blob_store.put_file_if_absent(
                "models",
                f"{spec.model_id}/{spec.sha256}/{checkpoint.name}",
                checkpoint,
            )
        checkpoint = prepare_inference_checkpoint(checkpoint, cfg.model_cache, spec.sha256)
        if predictor is not None:
            del predictor
            predictor = None
            free_vram()
        log.info("carregando modelo registrado %s", spec.model_id)
        predictor = build_predictor(spec, checkpoint)
        loaded_model_id = spec.model_id
        cfg.model_id = spec.model_id
        cfg.model_sha256 = spec.sha256
        cfg.sam3_commit = spec.sam3_commit
        return predictor

    try:
        with StatusHeartbeat(client, "loading", "validando e carregando o checkpoint"):
            predictor_for("")
    except Exception as exc:
        return hold_startup_error(client, exc, once=once)
    classes = ClassMap(cfg.classes_path)
    log.info("pronto. consumindo a fila em %s", cfg.api)
    client.status("ready")

    from .session import SessionClient, run_session

    session_client = SessionClient(cfg)

    backoff = BACKOFF_START
    while True:
        client.status("ready")
        # SESSÃO INTERATIVA TEM PRIORIDADE. Alguém está esperando na tela para
        # ajustar a caixa; uma propagação de 800 frames pode esperar o minuto
        # que a iteração leva. Sem esta ordem, abrir uma sessão ficaria atrás de
        # toda a fila de trabalho pesado.
        try:
            pedido = session_client.next_session(wait=0)
            if pedido is not None:
                with StatusHeartbeat(client, "busy", "atendendo prévia interativa"):
                    run_session(
                        session_client,
                        pedido,
                        cfg,
                        predictor_for(str(pedido.get("object_id") or "")),
                    )
                continue
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 503):
                log.error("a triagem recusou o worker: %s", exc)
                return 2
            log.warning("erro ao buscar sessão: %s", exc)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            log.warning("triagem indisponível (%s)", exc)

        try:
            job = client.next_job()
            backoff = BACKOFF_START
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            log.warning("triagem indisponível (%s); nova tentativa em %ds", exc, backoff)
            time.sleep(backoff)
            backoff = min(backoff * 2, BACKOFF_MAX)
            continue
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 503):
                log.error("a triagem recusou o worker: %s", exc)
                return 2
            log.warning("erro HTTP %s; nova tentativa em %ds", exc.code, backoff)
            time.sleep(backoff)
            backoff = min(backoff * 2, BACKOFF_MAX)
            continue

        if job is None:
            if once:
                log.info("fila vazia")
                return 0
            continue

        log.info(
            "job %s/%s (%d segmentos, tentativa %s)",
            job["object_id"], job["name"], len(job.get("segments") or []), job.get("attempt"),
        )
        try:
            object_id = str(job.get("object_id") or "")
            with StatusHeartbeat(client, "loading", "preparando modelo para propagação"):
                predictor = predictor_for(object_id)
            process_job(client, job, cfg, predictor, classes)
        except LeaseLost as exc:
            log.warning("%s — abandonando o job", exc)
        except Exception as exc:  # noqa: BLE001
            log.exception("falha inesperada no job")
            try:
                client.finish(job["lease_id"], "error", None, f"{type(exc).__name__}: {exc}")
            except Exception:  # noqa: BLE001
                pass

        if once:
            return 0
