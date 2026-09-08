"""Contrato HTTP independente de framework para revisão de máscaras."""

from __future__ import annotations

import base64
import binascii
import io
from collections.abc import Callable

from PIL import Image
from pipeline_core.masks import encode_binary_png
from pipeline_core.review_store import FrameMaskState, MaskEdit


def _canonical_browser_png(png: bytes) -> bytes:
    try:
        with Image.open(io.BytesIO(png)) as source:
            source.load()
            if source.mode in ("1", "L"):
                return png
            if source.mode != "RGBA":
                raise ValueError(f"modo PNG nao suportado: {source.mode}")
            rgba = source.copy()
    except (OSError, ValueError) as exc:
        raise ValueError(f"PNG invalido: {exc}") from exc
    binary = Image.new("1", rgba.size, 0)
    source_pixels = rgba.load()
    target_pixels = binary.load()
    for y in range(rgba.height):
        for x in range(rgba.width):
            red, green, blue, alpha = source_pixels[x, y]
            if alpha not in (0, 255):
                raise ValueError("PNG RGBA deve ter alpha binario (0 ou 255)")
            if alpha == 255:
                if (red, green, blue) != (255, 255, 255):
                    raise ValueError("pixels opacos da mascara devem ser brancos")
                target_pixels[x, y] = 1
    return encode_binary_png(binary)


def decode_mask_edits(items: list[dict]) -> list[MaskEdit]:
    edits = []
    for position, item in enumerate(items, start=1):
        try:
            png = base64.b64decode(item.get("png_base64") or "", validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError(f"instância {position}: PNG base64 inválido") from exc
        if not png:
            raise ValueError(f"instância {position}: PNG base64 vazio")
        png = _canonical_browser_png(png)
        edits.append(
            MaskEdit(
                obj_id=int(item.get("obj_id") or position),
                label=item.get("label") or "",
                png=png,
            )
        )
    return edits


def serialize_frame_state(
    state: FrameMaskState, *, mask_url: Callable[[int], str]
) -> dict:
    return {
        "frame": state.frame,
        "revision": state.revision,
        "status": state.status,
        "reviewed_by": state.reviewed_by,
        "instances": [
            {
                "obj_id": instance.obj_id,
                "label": instance.label,
                "mask_url": f"{mask_url(instance.obj_id)}?revision={state.revision}",
                "bbox": list(instance.info.bbox_normalized)
                if instance.info.bbox_normalized is not None
                else None,
                "area_pixels": instance.info.area_pixels,
                "sha256": instance.info.sha256,
            }
            for instance in state.instances
        ],
    }
