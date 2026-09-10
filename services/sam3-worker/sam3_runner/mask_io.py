"""Persistência imediata das máscaras canônicas produzidas pelo SAM3."""

from __future__ import annotations

import os
import hashlib
from pathlib import Path

from PIL import Image

from pipeline_core.masks import encode_binary_png
from pipeline_core.masks import MaskValidationError, inspect_binary_png
from pipeline_core.sam3_runs import mask_object_key
from pipeline_core.storage import MinioBlobStore


class MaskSetValidationError(RuntimeError):
    """A run cannot be completed because its canonical mask set is incomplete."""


_STORE_FROM_ENV = object()


def _as_image(mask, threshold: float) -> Image.Image:
    if isinstance(mask, Image.Image):
        return mask
    binary = mask > threshold
    while hasattr(binary, "dim") and binary.dim() > 2:
        binary = binary[0]
    array = binary.detach().cpu().numpy() if hasattr(binary, "detach") else binary
    return Image.fromarray(array.astype("uint8") * 255, mode="L")


def save_mask_png(
    out_dir: Path,
    *,
    obj_id: int,
    frame_idx: int,
    mask,
    threshold: float = 0.0,
    blob_store=_STORE_FROM_ENV,
) -> Path:
    """Grava uma máscara 1-bit de forma atômica e devolve o caminho final."""
    target = out_dir / "masks" / str(obj_id) / f"{frame_idx:06d}.png"
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = encode_binary_png(_as_image(mask, threshold))
    temporary = target.with_suffix(".png.tmp")
    with open(temporary, "wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, target)
    store = (
        MinioBlobStore.from_env()
        if blob_store is _STORE_FROM_ENV
        else blob_store
    )
    if store is not None:
        checksum = hashlib.sha256(payload).hexdigest()
        store.put_file_if_absent("masks", mask_object_key(checksum), target)
    return target


def validate_mask_set(
    out_dir: Path,
    *,
    obj_ids: list[int],
    frame_count: int,
    image_size: tuple[int, int],
) -> dict:
    missing: list[str] = []
    invalid: list[str] = []
    checksums: dict[str, str] = {}
    empty = 0
    for obj_id in obj_ids:
        for frame_idx in range(frame_count):
            relative = Path("masks") / str(obj_id) / f"{frame_idx:06d}.png"
            path = out_dir / relative
            if not path.is_file():
                missing.append(relative.as_posix())
                continue
            try:
                info = inspect_binary_png(path.read_bytes(), expected_size=image_size)
            except (OSError, MaskValidationError) as exc:
                invalid.append(f"{relative.as_posix()}: {exc}")
                continue
            checksums[relative.as_posix()] = info.sha256
            if info.area_pixels == 0:
                empty += 1
    if missing or invalid:
        details = []
        if missing:
            details.append(f"mascaras ausentes ({len(missing)}): {', '.join(missing[:3])}")
        if invalid:
            details.append(f"mascaras invalidas ({len(invalid)}): {', '.join(invalid[:3])}")
        raise MaskSetValidationError("; ".join(details))
    return {
        "format": "png-1bit-v1",
        "files": len(checksums),
        "empty": empty,
        "checksums": checksums,
    }
