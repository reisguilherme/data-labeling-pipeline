"""Sessão interativa: segmenta UM frame por vez, com o estado vivo na GPU.

O `init_state` de um segmento 4K é o custo dominante — decodificar os frames e
montar o estado leva bem mais tempo que a inferência de um frame. Recarregá-lo a
cada ajuste da caixa tornaria a iteração inviável. Aqui ele é pago uma vez e o
estado fica de pé até a sessão fechar ou expirar.

Em troca, a sessão segura VRAM. Por isso o servidor a derruba por inatividade e
o runner libera no `finally`, mesmo se o loop morrer no meio.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from pathlib import Path

from .config import RunnerConfig

log = logging.getLogger("sam3_runner.session")

# Nome do arquivo de prévia. A sequência entra no nome para o browser nunca
# servir a máscara anterior de cache.
PREVIEW_DIR = "_preview"


class SessionClient:
    def __init__(self, cfg: RunnerConfig) -> None:
        self.cfg = cfg

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

    def next_session(self, wait: int) -> dict | None:
        return self._request(
            "GET",
            f"/api/sam3/session/next?worker={self.cfg.worker_id}&wait={wait}",
            timeout=wait + 30,
        )

    def ready(self, session_id: str) -> None:
        self._request("POST", f"/api/sam3/session/{session_id}/ready")

    def fail(self, session_id: str, error: str) -> None:
        self._request("POST", f"/api/sam3/session/{session_id}/fail", {"error": error})

    def next_command(self, session_id: str, wait: int) -> dict | None:
        return self._request(
            "GET",
            f"/api/sam3/session/{session_id}/command?wait={wait}",
            timeout=wait + 30,
        )

    def result(self, session_id: str, payload: dict) -> None:
        self._request("POST", f"/api/sam3/session/{session_id}/result", payload)


def _write_mask_png(mask, out_path: Path, color=(139, 92, 246)) -> None:
    """Máscara como PNG RGBA semitransparente, para sobrepor ao frame.

    Violeta com alfa parcial: deixa ver os pixels por baixo (essencial num
    reflexo de baixo contraste) e ainda torna óbvio o que vazou para o fundo.
    """
    import numpy as np
    from PIL import Image

    binary = mask
    while hasattr(binary, "dim") and binary.dim() > 2:
        binary = binary[0]
    array = binary.detach().cpu().numpy() if hasattr(binary, "detach") else np.asarray(binary)
    array = array > 0

    height, width = array.shape
    rgba = np.zeros((height, width, 4), dtype=np.uint8)
    rgba[..., 0] = color[0]
    rgba[..., 1] = color[1]
    rgba[..., 2] = color[2]
    rgba[..., 3] = np.where(array, 130, 0).astype(np.uint8)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(rgba, mode="RGBA").save(out_path, optimize=True)


def _cleanup_previews(directory: Path, keep: int = 3) -> None:
    """Só as últimas prévias interessam; o resto é lixo de 4K no disco."""
    if not directory.is_dir():
        return
    arquivos = sorted(directory.glob("*.png"), key=lambda p: p.stat().st_mtime, reverse=True)
    for velho in arquivos[keep:]:
        velho.unlink(missing_ok=True)


def run_session(client: SessionClient, job: dict, cfg: RunnerConfig, predictor) -> None:
    """Atende uma sessão até ela fechar. Bloqueia enquanto durar."""
    import torch

    from .boxes import bbox_from_mask
    from .model import autocast_context, free_vram, init_state_kwargs

    session_id = job["session_id"]
    segment_dir = Path(job["segment_dir"])
    frame_idx = int(job.get("frame_idx") or 0)
    preview_dir = segment_dir / "_sam3" / PREVIEW_DIR

    log.info("sessão %s: carregando %s", session_id[:8], segment_dir.name)
    state = None
    try:
        kwargs = init_state_kwargs(predictor, cfg)
        state = predictor.init_state(video_path=str(segment_dir), **kwargs)
        client.ready(session_id)
        log.info("sessão %s: pronta", session_id[:8])
    except Exception as exc:  # noqa: BLE001
        log.exception("sessão %s: falhou ao abrir", session_id[:8])
        try:
            client.fail(session_id, f"{type(exc).__name__}: {exc}")
        except Exception:  # noqa: BLE001
            pass
        if state is not None:
            del state
        free_vram()
        return

    try:
        while True:
            try:
                command = client.next_command(session_id, cfg.poll_wait)
            except urllib.error.HTTPError as exc:
                if exc.code == 404:
                    log.info("sessão %s: sumiu do servidor", session_id[:8])
                    return
                raise
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                log.warning("sessão %s: servidor indisponível (%s)", session_id[:8], exc)
                time.sleep(2)
                continue

            if command is None:
                continue
            if command.get("kind") == "close":
                log.info("sessão %s: encerrada", session_id[:8])
                return

            seq = int(command.get("seq") or 0)
            boxes = command.get("boxes") or []
            started = time.monotonic()

            try:
                # Cada teste parte do zero: sem limpar, as caixas anteriores
                # continuariam valendo e o resultado seria a união de tudo que
                # foi tentado — não o que está na tela.
                predictor.clear_all_points_in_video(state)
                with autocast_context():
                    saida = None
                    for position, box in enumerate(boxes, start=1):
                        normalized = box.get("normalized") or box.get("box_normalized")
                        _, obj_ids, _, masks = predictor.add_new_points_or_box(
                            inference_state=state,
                            frame_idx=frame_idx,
                            obj_id=int(box.get("obj_id") or position),
                            box=torch.tensor([list(normalized)], dtype=torch.float32),
                            clear_old_points=True,
                        )[:4]
                        saida = (obj_ids, masks)

                if saida is None:
                    raise ValueError("nenhuma caixa utilizável no comando")

                _, masks = saida
                mask = masks[0]
                caixa = bbox_from_mask(mask, cfg.mask_threshold)

                nome = f"{seq:04d}.png"
                if caixa is not None:
                    _write_mask_png(mask, preview_dir / nome)
                    _cleanup_previews(preview_dir)
                    largura = getattr(mask, "shape", [0, 0, 0])[-1]
                    altura = getattr(mask, "shape", [0, 0, 0])[-2]
                    payload = {
                        "seq": seq,
                        "mask": nome,
                        "bbox": [
                            caixa.x1 / largura,
                            caixa.y1 / altura,
                            (caixa.x2 + 1) / largura,
                            (caixa.y2 + 1) / altura,
                        ],
                        "area_frac": round(caixa.area() / (largura * altura), 5),
                        "error": None,
                    }
                else:
                    # Máscara vazia é informação útil: a caixa não pegou nada.
                    payload = {
                        "seq": seq,
                        "mask": None,
                        "bbox": None,
                        "area_frac": 0.0,
                        "error": "o SAM3 não encontrou objeto nesta caixa",
                    }

                log.info(
                    "sessão %s: teste %d em %.1fs (área %.3f%%)",
                    session_id[:8], seq, time.monotonic() - started,
                    (payload.get("area_frac") or 0) * 100,
                )
                client.result(session_id, payload)

            except Exception as exc:  # noqa: BLE001
                log.exception("sessão %s: teste %d falhou", session_id[:8], seq)
                try:
                    client.result(
                        session_id,
                        {"seq": seq, "mask": None, "bbox": None,
                         "error": f"{type(exc).__name__}: {exc}"},
                    )
                except Exception:  # noqa: BLE001
                    pass
    finally:
        # A VRAM tem de voltar mesmo se o loop morrer — é o recurso escasso e o
        # motivo de a sessão ter prazo de validade.
        if state is not None:
            del state
        free_vram()
        log.info("sessão %s: VRAM liberada", session_id[:8])
