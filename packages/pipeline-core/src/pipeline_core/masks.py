"""Codificação e validação da máscara canônica do pipeline."""

from __future__ import annotations

import hashlib
import io
from dataclasses import dataclass

from PIL import Image, UnidentifiedImageError


class MaskValidationError(ValueError):
    """O blob não representa uma máscara PNG binária válida."""


@dataclass(frozen=True)
class MaskInfo:
    width: int
    height: int
    area_pixels: int
    bbox_pixels: tuple[int, int, int, int] | None
    bbox_normalized: tuple[float, float, float, float] | None
    sha256: str


def _binary_image(data: bytes, expected_size: tuple[int, int] | None) -> Image.Image:
    try:
        image = Image.open(io.BytesIO(data))
        image.load()
    except (UnidentifiedImageError, OSError) as exc:
        raise MaskValidationError("máscara precisa ser um PNG válido") from exc
    if image.format != "PNG":
        raise MaskValidationError("máscara precisa ser um PNG")
    if expected_size is not None and image.size != expected_size:
        raise MaskValidationError(
            f"dimensões da máscara {image.size[0]}x{image.size[1]} diferem de "
            f"{expected_size[0]}x{expected_size[1]}"
        )
    if image.mode not in ("1", "L"):
        raise MaskValidationError("máscara precisa ser binária (modo 1 ou L)")
    if image.mode == "L" and any(image.histogram()[1:255]):
        raise MaskValidationError("máscara precisa ser binária, somente 0 e 255")
    return image.convert("1")


def encode_binary_png(image: Image.Image) -> bytes:
    """Normaliza uma imagem binária para PNG 1-bit lossless."""
    buffer = io.BytesIO()
    candidate = image.convert("L")
    if any(candidate.histogram()[1:255]):
        raise MaskValidationError("máscara precisa ser binária, somente 0 e 255")
    candidate.convert("1").save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


def inspect_binary_png(
    data: bytes, *, expected_size: tuple[int, int] | None = None
) -> MaskInfo:
    """Valida o PNG e deriva estatísticas diretamente dos pixels positivos."""
    image = _binary_image(data, expected_size)
    width, height = image.size
    bbox = image.getbbox()
    area = width * height - image.convert("L").histogram()[0]
    normalized = None
    if bbox is not None:
        x1, y1, x2, y2 = bbox
        normalized = (x1 / width, y1 / height, x2 / width, y2 / height)
    return MaskInfo(
        width=width,
        height=height,
        area_pixels=area,
        bbox_pixels=bbox,
        bbox_normalized=normalized,
        sha256=hashlib.sha256(data).hexdigest(),
    )
