"""Processa UM segmento: prompt.json -> rótulos YOLO.

Substitui as células 18, 26, 28 e 42 do notebook. Quatro diferenças deliberadas
em relação a ele, todas documentadas no ponto onde acontecem.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from pathlib import Path

from . import marker, model
from .boxes import bbox_from_mask, reject_reason, yolo_line
from .classes import ClassMap
from .config import VERSION, RunnerConfig
from .marker import ObjectReport, RunReport
from .mask_io import save_mask_png, validate_mask_set
from .prompt import Prompt

log = logging.getLogger("sam3_runner.segment")


class PropagationCancelled(RuntimeError):
    """Cooperative stop requested at a completed frame boundary."""


def _write_labels(prompt: Prompt, rows: dict[int, list[str]]) -> int:
    """Um .txt por frame do segmento — inclusive os vazios.

    O Ultralytics trata arquivo vazio e arquivo ausente igual (fundo), mas para
    QUEM LÊ O DATASET os dois casos são indistinguíveis de "ainda não
    processado". O arquivo vazio é a afirmação explícita: processado, nada aqui.
    """
    prompt.labels_dir.mkdir(parents=True, exist_ok=True)
    written = 0
    for index in range(prompt.frame_count):
        path = prompt.labels_dir / f"{index:06d}.txt"
        lines = rows.get(index, [])
        path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
        written += 1
    return written


def run_segment(
    predictor,
    prompt: Prompt,
    cfg: RunnerConfig,
    classes: ClassMap,
    *,
    on_frame: Callable[[int], bool | None] | None = None,
) -> RunReport:
    """Roda o SAM3 num segmento e grava `_sam3/labels/*.txt` + `_sam3/run.json`."""
    import torch

    started = time.monotonic()
    class_index = classes.ensure(prompt.labels())
    reports = {
        obj.obj_id: ObjectReport(
            obj_id=obj.obj_id, label=obj.label, class_index=class_index[obj.label]
        )
        for obj in prompt.objects
    }
    report = RunReport(
        runner_version=VERSION,
        prompt_digest=prompt.digest,
        frame_count=prompt.frame_count,
        objects=list(reports.values()),
        params=cfg.params(),
        model={
            "builder": "build_sam3_video_model",
            "model_id": cfg.model_id,
            "checkpoint_sha256": cfg.model_sha256,
            "sam3_commit": cfg.sam3_commit,
            **model.device_info(),
        },
    )

    rows: dict[int, list[str]] = {}
    observed_masks: set[tuple[int, int]] = set()

    kwargs = model.init_state_kwargs(predictor, cfg)
    report.params = {**report.params, "init_state_kwargs": sorted(kwargs)}
    state = predictor.init_state(video_path=str(prompt.segment_dir), **kwargs)

    try:
        predictor.clear_all_points_in_video(state)
        for obj in prompt.objects:
            predictor.add_new_points_or_box(
                inference_state=state,
                frame_idx=prompt.prompt_frame_idx,
                obj_id=obj.obj_id,
                box=torch.tensor([obj.box_list], dtype=torch.float32),
                clear_old_points=True,
            )

        # DOIS passes quando o prompt não está no primeiro frame. A triagem
        # deixa escolher qualquer frame do intervalo como frame do prompt
        # (`prompt_frame_idx = prompt_frame - start_frame`); com um passe só,
        # tudo que vem ANTES dele sairia sem rótulo — e sairia como .txt vazio,
        # ou seja, como "fundo", envenenando o dataset em silêncio.
        directions = [False] if prompt.prompt_frame_idx == 0 else [False, True]

        for reverse in directions:
            for frame_idx, obj_ids, _, masks, _ in predictor.propagate_in_video(
                inference_state=state,
                start_frame_idx=prompt.prompt_frame_idx,
                max_frame_num_to_track=prompt.frame_count,
                reverse=reverse,
                propagate_preflight=True,
            ):
                # Nao acumula tensores 4K: cada mascara e persistida nesta
                # iteracao e so entao sua bbox derivada e indexada.
                lines: list[str] = []
                for position, obj_id in enumerate(obj_ids):
                    obj_id = int(obj_id)
                    entry = reports.get(obj_id)
                    if entry is None:
                        continue
                    # Mascara canonica vem antes dos filtros de bbox: vazia,
                    # pequena ou degenerada continuam sendo resultados validos.
                    save_mask_png(
                        prompt.out_dir,
                        obj_id=obj_id,
                        frame_idx=frame_idx,
                        mask=masks[position],
                        threshold=cfg.mask_threshold,
                    )
                    observed_masks.add((obj_id, frame_idx))
                    box = bbox_from_mask(masks[position], cfg.mask_threshold)
                    if box is None:
                        continue
                    reason = reject_reason(
                        box,
                        prompt.image_width,
                        prompt.image_height,
                        min_box_px=cfg.min_box_px,
                        max_box_frac=cfg.max_box_frac,
                    )
                    if reason == "small":
                        entry.dropped_small += 1
                        continue
                    if reason == "huge":
                        entry.dropped_huge += 1
                        continue
                    # Uma linha por obj_id. O notebook funde as máscaras da
                    # mesma classe com OR lógico, o que destrói instâncias
                    # separadas — inofensivo para máscara semântica, fatal para
                    # detecção: dois booms virariam uma caixa cobrindo os dois.
                    lines.append(
                        yolo_line(
                            entry.class_index, box, prompt.image_width, prompt.image_height
                        )
                    )
                    entry.frames += 1

                if lines:
                    rows.setdefault(frame_idx, []).extend(lines)
                # A fila usa este sinal somente como telemetria: a máscara e os
                # artefatos continuam sendo gravados acima, frame a frame. Isso
                # deixa a interface mostrar avanço sem reter imagens ou tensores
                # 4K na RAM.
                if on_frame is not None:
                    if on_frame(frame_idx) is False:
                        raise PropagationCancelled(
                            "cancelamento solicitado durante a propagacao"
                        )

        # Alguns builds omitem obj_ids nos frames sem sinal. Ausencia de arquivo
        # nao pode ser confundida com mascara vazia, entao materializa o vazio.
        from PIL import Image

        empty = Image.new("1", (prompt.image_width, prompt.image_height), 0)
        for obj_id in reports:
            for frame_idx in range(prompt.frame_count):
                if (obj_id, frame_idx) not in observed_masks:
                    save_mask_png(
                        prompt.out_dir,
                        obj_id=obj_id,
                        frame_idx=frame_idx,
                        mask=empty,
                        threshold=cfg.mask_threshold,
                    )

        report.frames_written = _write_labels(prompt, rows)
        report.frames_with_objects = sum(1 for value in rows.values() if value)
        report.artifacts = validate_mask_set(
            prompt.out_dir,
            obj_ids=list(reports),
            frame_count=prompt.frame_count,
            image_size=(prompt.image_width, prompt.image_height),
        )
        report.status = "done"

    except PropagationCancelled:
        # Nao publique run.json para um segmento interrompido. As mascaras ja
        # persistidas permanecem parciais e, sem o marcador, nunca sao tratadas
        # como uma execucao concluida nem usadas por should_skip().
        raise
    except Exception as exc:  # noqa: BLE001
        # Capturar tudo aqui é o que garante que um segmento ruim não derrube o
        # worker: em 4K um vídeo longo pode simplesmente não caber, e a resposta
        # certa é marcar ESTE segmento como erro e seguir para o próximo — não
        # morrer e deixar o lease expirar sobre os outros onze.
        report.status = "error"
        report.error = _explain(exc, prompt, cfg)
        log.exception("falha em %s", prompt.segment_dir)
    finally:
        del state
        model.free_vram()

    report.finished_at = marker.iso()
    report.duration_sec = round(time.monotonic() - started, 2)
    report.objects = list(reports.values())
    marker.write(prompt.marker_path, report)
    return report


def _explain(exc: Exception, prompt: Prompt, cfg: RunnerConfig) -> str:
    """Mensagem de erro acionável.

    O OOM de CUDA chega como RuntimeError, não como MemoryError — checar pelo
    texto é feio, mas é o que distingue "não coube" de um bug de verdade, e a
    diferença muda completamente o que a pessoa deve fazer.
    """
    text = str(exc)
    if "out of memory" in text.lower() or type(exc).__name__ == "OutOfMemoryError":
        return (
            f"memória de GPU insuficiente para {prompt.frame_count} frames de "
            f"{prompt.image_width}x{prompt.image_height}. "
            "Tente SAM3_MAX_SIDE=1920 (reescala a entrada; o contrato é todo "
            "normalizado, então os rótulos continuam válidos) ou reduza o "
            "intervalo na triagem."
        )
    return f"{type(exc).__name__}: {text}"


def should_skip(prompt: Prompt, cfg: RunnerConfig) -> bool:
    return marker.is_complete(
        prompt.marker_path,
        prompt_digest=prompt.digest,
        frame_count=prompt.frame_count,
        params=cfg.params(),
    )


def find_prompt_dirs(workspace: Path, object_id: str | None = None) -> list[Path]:
    from .prompt import find_segments

    return find_segments(workspace, object_id)
