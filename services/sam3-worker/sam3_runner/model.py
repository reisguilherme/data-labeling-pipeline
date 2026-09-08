"""Carga do modelo SAM3 e sondagem da API de inferência.

`torch` e `sam3` só são importados aqui, e só quando este módulo é usado. É o
que permite `validate` e os testes de `boxes`/`classes`/`marker` rodarem numa
máquina sem GPU e sem o pacote gated instalado.
"""

from __future__ import annotations

import inspect
import logging
import os
from pathlib import Path

from pipeline_core.model_registry import ModelSpec

log = logging.getLogger("sam3_runner.model")

DETECTOR_OVERLAY_BASE_KEYS = (
    "backbone.vision_backbone.sam2_convs.",
    "segmentation_head.cross_attend_prompt.",
    "segmentation_head.cross_attn_norm.",
    "segmentation_head.instance_seg_head.",
    "segmentation_head.mask_predictor.mask_embed.",
    "segmentation_head.pixel_decoder.conv_layers.",
    "segmentation_head.pixel_decoder.norms.",
    "segmentation_head.semantic_seg_head.",
)


def validate_detector_overlay(missing_keys, unexpected_keys) -> None:
    """Permit only video-specific detector layers to come from the base model."""
    invalid_missing = sorted(
        key
        for key in missing_keys
        if not any(key.startswith(prefix) for prefix in DETECTOR_OVERLAY_BASE_KEYS)
    )
    unexpected = sorted(unexpected_keys)
    if invalid_missing or unexpected:
        raise RuntimeError(
            "checkpoint detector incompativel; "
            f"missing={invalid_missing}, unexpected={unexpected}"
        )


def prepare_inference_checkpoint(source: Path, cache_dir: Path, checksum: str) -> Path:
    """Strip optimizer/training state without materializing the whole checkpoint.

    Fine-tuning checkpoints commonly contain roughly three copies of the model
    (weights plus optimizer moments). The official builder only consumes the
    ``model`` state dict, so retaining the rest causes a needless RAM spike.
    """
    import torch

    payload = torch.load(source, map_location="cpu", weights_only=True, mmap=True)
    if not isinstance(payload, dict) or not isinstance(payload.get("model"), dict):
        return source
    extra_keys = set(payload) - {"model"}
    if not extra_keys:
        return source

    target_dir = cache_dir / "inference-checkpoints"
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{checksum}.pt"
    if target.is_file():
        return target

    temporary = target.with_suffix(".pt.tmp")
    log.info(
        "checkpoint de treino detectado (%s); extraindo somente pesos para %s",
        ", ".join(sorted(extra_keys)),
        target,
    )
    try:
        torch.save(payload["model"], temporary)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return target


def require_token() -> None:
    """O checkpoint vem de um repo GATED do HuggingFace.

    Falhar aqui, no start, é muito melhor que falhar no primeiro job — nesse
    momento o worker já pegou um lease e o vídeo ficaria preso até expirar.
    """
    if not (os.environ.get("HF_TOKEN") or os.environ.get("HUGGING_FACE_HUB_TOKEN")):
        raise RuntimeError(
            "HF_TOKEN não definido. O checkpoint do SAM3 está num repositório "
            "restrito (https://huggingface.co/facebook/sam3): solicite acesso e "
            "exporte o token no ambiente do container."
        )


def checkpoint_builder_kwargs(spec: ModelSpec, checkpoint_path: Path) -> dict:
    """Arguments required for a fine-tuned checkpoint to fail closed."""
    return {
        "checkpoint_path": str(checkpoint_path),
        "load_from_HF": False,
        "strict_state_dict_loading": True,
    }


def build_predictor(
    spec: ModelSpec | None = None,
    checkpoint_path: Path | None = None,
):
    """Constrói o preditor de vídeo, com o mesmo arranjo do notebook.

    A troca de backbone (`tracker` com o backbone do `detector`) não é
    cosmética: é o que faz o caminho de segmentação promptável por caixa
    funcionar. Mantida idêntica ao notebook de propósito — divergir aqui mudaria
    silenciosamente as máscaras em relação ao que foi validado à mão.
    """
    if spec is None:
        require_token()
    elif checkpoint_path is None:
        raise ValueError("checkpoint_path obrigatorio para modelo registrado")

    from sam3.model_builder import build_sam3_video_model

    if spec is None:
        model = build_sam3_video_model()
    else:
        import torch

        weights = torch.load(
            checkpoint_path,
            map_location="cpu",
            weights_only=True,
            mmap=True,
        )
        is_video_checkpoint = any(
            key.startswith(("detector.", "tracker.")) for key in weights
        )
        if is_video_checkpoint:
            del weights
            model = build_sam3_video_model(**checkpoint_builder_kwargs(spec, checkpoint_path))
        else:
            require_token()
            log.info(
                "checkpoint de detector detectado; carregando tracker base e aplicando overlay finetunado"
            )
            model = build_sam3_video_model()
            missing, unexpected = model.detector.load_state_dict(weights, strict=False)
            validate_detector_overlay(missing, unexpected)
            del weights
    predictor = model.tracker
    predictor.backbone = model.detector.backbone
    return predictor


def init_state_kwargs(predictor, cfg) -> dict:
    """Argumentos de offload que ESTE build do SAM3 realmente aceita.

    Sondados em vez de assumidos: o pacote é gated e a assinatura varia entre
    versões. Passar um kwarg inexistente derruba o job inteiro; deixar de passar
    um que existe só custa memória.
    """
    try:
        params = inspect.signature(predictor.init_state).parameters
    except (TypeError, ValueError):
        return {}

    wanted = {
        "offload_video_to_cpu": cfg.offload_video,
        "offload_state_to_cpu": cfg.offload_state,
    }
    accepted = {name: True for name, on in wanted.items() if on and name in params}
    ignored = [name for name, on in wanted.items() if on and name not in params]
    if ignored:
        log.info("init_state não aceita %s neste build do SAM3", ", ".join(ignored))
    return accepted


def autocast_context():
    """bf16 na GPU, como o notebook. Context manager de verdade: o notebook faz
    `.__enter__()` e nunca sai, o que é tolerável numa sessão interativa e
    péssimo num processo que roda por dias."""
    import contextlib

    import torch

    if not torch.cuda.is_available():
        return contextlib.nullcontext()

    if torch.cuda.get_device_properties(0).major >= 8:
        torch.backends.cuda.matmul.allow_tf32 = True
        torch.backends.cudnn.allow_tf32 = True
    return torch.autocast("cuda", dtype=torch.bfloat16)


def device_info() -> dict:
    try:
        import torch
    except ImportError:
        return {"torch": None, "cuda": False}
    info = {
        "torch": torch.__version__,
        "cuda": bool(torch.cuda.is_available()),
        "cuda_version": torch.version.cuda,
    }
    if torch.cuda.is_available():
        props = torch.cuda.get_device_properties(0)
        info["device"] = props.name
        info["capability"] = f"{props.major}.{props.minor}"
        info["vram_gb"] = round(props.total_memory / 1024**3, 1)
    return info


def free_vram() -> None:
    import gc

    gc.collect()
    try:
        import torch

        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except ImportError:
        pass
