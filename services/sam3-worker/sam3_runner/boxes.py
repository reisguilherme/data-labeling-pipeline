"""Máscara -> bounding box -> linha YOLO.

Sem dependência de torch no nível do módulo: `bbox_from_mask` funciona tanto com
tensor quanto com array numpy, o que deixa a lógica testável sem GPU e sem CUDA.
"""

from __future__ import annotations

from dataclasses import dataclass

# Uma caixa de poucos pixels não é sinal, é resíduo: quando o objeto sai de
# quadro o tracker costuma deixar um punhado de pixels soltos. Uma caixa de 1 px
# no dataset é veneno — o detector aprende a prever ruído.
DEFAULT_MIN_BOX_PX = 3

# Caixa ocupando mais que isto do frame é o tracker tendo explodido (vazou para
# o fundo inteiro). Também é lixo, e do tipo que estraga o treino em silêncio.
DEFAULT_MAX_BOX_FRAC = 0.5


@dataclass(frozen=True)
class Box:
    """Cantos INCLUSIVOS, em pixels."""

    x1: int
    y1: int
    x2: int
    y2: int

    @property
    def width(self) -> int:
        return self.x2 - self.x1 + 1

    @property
    def height(self) -> int:
        return self.y2 - self.y1 + 1

    def area(self) -> int:
        return self.width * self.height


def bbox_from_mask(mask, threshold: float = 0.0) -> Box | None:
    """Caixa envolvente dos pixels acima do limiar. None se a máscara é vazia.

    Recebe LOGITS, não probabilidades — o limiar 0.0 é o mesmo que o notebook
    usa (`mask > 0.0`), e trocá-lo muda o resultado silenciosamente.

    Aceita tensor do torch ou array do numpy. Com tensor, a redução acontece
    onde o dado já está (na GPU) e só quatro inteiros cruzam o barramento; a
    alternativa ingênua — `.cpu().numpy()` antes de medir — transfere 8 MB por
    frame por objeto num vídeo 4K.
    """
    binary = mask > threshold

    # SAM3 devolve (1, 1, H, W) ou (1, H, W) conforme o caminho; reduz até 2D.
    while hasattr(binary, "dim") and binary.dim() > 2:
        binary = binary[0]
    while hasattr(binary, "ndim") and not hasattr(binary, "dim") and binary.ndim > 2:
        binary = binary[0]

    if hasattr(binary, "dim"):  # torch
        rows = binary.any(dim=1)
        cols = binary.any(dim=0)
        if not bool(rows.any()):
            return None
        ys = rows.nonzero().flatten()
        xs = cols.nonzero().flatten()
        return Box(int(xs[0]), int(ys[0]), int(xs[-1]), int(ys[-1]))

    import numpy as np  # só no caminho numpy, para o módulo não exigir numpy

    rows = np.any(binary, axis=1)
    cols = np.any(binary, axis=0)
    if not rows.any():
        return None
    ys = np.flatnonzero(rows)
    xs = np.flatnonzero(cols)
    return Box(int(xs[0]), int(ys[0]), int(xs[-1]), int(ys[-1]))


def to_yolo(box: Box, width: int, height: int) -> tuple[float, float, float, float]:
    """(cx, cy, w, h) normalizado, como o Ultralytics espera.

    O `+1` não é detalhe: os índices de `nonzero` são INCLUSIVOS, então uma
    caixa de x1=x2=10 tem largura 1 pixel, não 0. Sem isso, todo objeto fica
    meio pixel deslocado e os objetos de 1 px viram largura zero.
    """
    box_w = box.width / width
    box_h = box.height / height
    cx = (box.x1 + box.x2 + 1) / 2 / width
    cy = (box.y1 + box.y2 + 1) / 2 / height
    clamp = lambda v: max(0.0, min(1.0, v))  # noqa: E731
    return clamp(cx), clamp(cy), clamp(box_w), clamp(box_h)


def yolo_line(class_index: int, box: Box, width: int, height: int) -> str:
    cx, cy, box_w, box_h = to_yolo(box, width, height)
    return f"{class_index} {cx:.6f} {cy:.6f} {box_w:.6f} {box_h:.6f}"


def reject_reason(
    box: Box,
    width: int,
    height: int,
    *,
    min_box_px: int = DEFAULT_MIN_BOX_PX,
    max_box_frac: float = DEFAULT_MAX_BOX_FRAC,
) -> str | None:
    """Motivo para descartar a caixa, ou None se ela serve."""
    if box.width < min_box_px or box.height < min_box_px:
        return "small"
    if box.area() > max_box_frac * width * height:
        return "huge"
    return None
