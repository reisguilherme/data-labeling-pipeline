"""Conversões lossless COCO e polygonização YOLO da máscara canônica."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from math import dist

from PIL import Image


def _counts(image: Image.Image) -> list[int]:
    binary = image.convert("1")
    pixels = binary.load()
    values = [1 if pixels[x, y] else 0 for x in range(binary.width) for y in range(binary.height)]
    counts: list[int] = []
    current = 0
    run = 0
    for value in values:
        if value == current:
            run += 1
        else:
            counts.append(run)
            run = 1
            current = value
    counts.append(run)
    return counts


def _compress_counts(counts: list[int]) -> str:
    encoded = []
    for index, original in enumerate(counts):
        value = original - counts[index - 2] if index > 2 else original
        more = True
        while more:
            char = value & 0x1F
            value >>= 5
            more = value != (-1 if char & 0x10 else 0)
            if more:
                char |= 0x20
            char += 48
            if char >= 92:
                char += 1
            encoded.append(chr(char))
    return "".join(encoded)


def _decompress_counts(encoded: str) -> list[int]:
    counts = []
    position = 0
    while position < len(encoded):
        value = 0
        shift = 0
        more = True
        char = 0
        while more:
            char = ord(encoded[position]) - 48
            if char > 40:
                char -= 1
            value |= (char & 0x1F) << (5 * shift)
            more = bool(char & 0x20)
            position += 1
            shift += 1
        if char & 0x10:
            value |= -1 << (5 * shift)
        if len(counts) > 2:
            value += counts[-2]
        counts.append(value)
    return counts


def coco_rle(image: Image.Image) -> dict:
    """COCO compressed RLE, em ordem Fortran, serializável em JSON."""
    return {"size": [image.height, image.width], "counts": _compress_counts(_counts(image))}


def decode_coco_rle(rle: dict) -> Image.Image:
    height, width = (int(value) for value in rle["size"])
    counts = _decompress_counts(str(rle["counts"]))
    column_major = []
    value = 0
    for count in counts:
        column_major.extend([value] * count)
        value = 1 - value
    if len(column_major) != width * height:
        raise ValueError("RLE COCO não cobre exatamente as dimensões declaradas")
    row_major = bytearray(width * height)
    for x in range(width):
        for y in range(height):
            row_major[y * width + x] = 255 if column_major[x * height + y] else 0
    return Image.frombytes("L", (width, height), bytes(row_major)).convert("1")


Point = tuple[int, int]


@dataclass(frozen=True)
class Polygonization:
    polygons: tuple[tuple[tuple[float, float], ...], ...]
    warnings: tuple[str, ...]


def _direction(start: Point, end: Point) -> int:
    dx, dy = end[0] - start[0], end[1] - start[1]
    return {(1, 0): 0, (0, 1): 1, (-1, 0): 2, (0, -1): 3}[(dx, dy)]


def _choose_next(previous: Point, current: Point, candidates: list[Point]) -> Point:
    incoming = _direction(previous, current)
    priority = {1: 0, 0: 1, 3: 2, 2: 3}  # direita, reto, esquerda, retorno
    return min(candidates, key=lambda point: priority[(_direction(current, point) - incoming) % 4])


def _simplify_collinear(loop: list[Point]) -> list[Point]:
    changed = True
    points = loop[:]
    while changed and len(points) > 3:
        changed = False
        reduced = []
        for index, current in enumerate(points):
            previous = points[index - 1]
            following = points[(index + 1) % len(points)]
            if (current[0] - previous[0]) * (following[1] - current[1]) == (
                current[1] - previous[1]
            ) * (following[0] - current[0]):
                changed = True
                continue
            reduced.append(current)
        points = reduced
    return points


def _signed_area(points: list[Point]) -> float:
    return sum(
        points[index][0] * points[(index + 1) % len(points)][1]
        - points[(index + 1) % len(points)][0] * points[index][1]
        for index in range(len(points))
    ) / 2


def _loops(image: Image.Image) -> list[list[Point]]:
    binary = image.convert("1")
    pixels = binary.load()
    edges: set[tuple[Point, Point]] = set()
    for y in range(binary.height):
        for x in range(binary.width):
            if not pixels[x, y]:
                continue
            if y == 0 or not pixels[x, y - 1]:
                edges.add(((x, y), (x + 1, y)))
            if x == binary.width - 1 or not pixels[x + 1, y]:
                edges.add(((x + 1, y), (x + 1, y + 1)))
            if y == binary.height - 1 or not pixels[x, y + 1]:
                edges.add(((x + 1, y + 1), (x, y + 1)))
            if x == 0 or not pixels[x - 1, y]:
                edges.add(((x, y + 1), (x, y)))

    outgoing: dict[Point, set[Point]] = defaultdict(set)
    for start, end in edges:
        outgoing[start].add(end)
    loops = []
    while edges:
        start, current = next(iter(edges))
        edges.remove((start, current))
        outgoing[start].discard(current)
        path = [start]
        previous = start
        while current != start:
            path.append(current)
            candidates = [end for end in outgoing[current] if (current, end) in edges]
            if not candidates:
                raise ValueError("contorno de máscara aberto")
            following = _choose_next(previous, current, candidates)
            edges.remove((current, following))
            outgoing[current].discard(following)
            previous, current = current, following
        loops.append(_simplify_collinear(path))
    return loops


def _merge_components(components: list[list[Point]]) -> list[Point]:
    largest = max(components, key=lambda points: abs(_signed_area(points)))
    merged = largest[:]
    remaining = components[:]
    remaining.remove(largest)
    for component in remaining:
        left, right = min(
            ((i, j) for i in range(len(merged)) for j in range(len(component))),
            key=lambda pair: dist(merged[pair[0]], component[pair[1]]),
        )
        cycle = component[right:] + component[: right + 1]
        merged = merged[: left + 1] + cycle + [merged[left]] + merged[left + 1 :]
    return merged


def yolo_polygons(image: Image.Image) -> Polygonization:
    """Extrai contorno(s) externo(s) e normaliza coordenadas para YOLO-seg."""
    loops = _loops(image)
    external = [loop for loop in loops if len(loop) >= 3 and _signed_area(loop) > 0]
    holes = [loop for loop in loops if len(loop) >= 3 and _signed_area(loop) < 0]
    warnings = []
    if holes:
        warnings.append(f"holes_dropped:{len(holes)}")
    if not external:
        return Polygonization((), tuple(warnings))
    if len(external) > 1:
        warnings.append(f"components_merged:{len(external)}")
        external = [_merge_components(external)]
    normalized = tuple(
        tuple((x / image.width, y / image.height) for x, y in polygon)
        for polygon in external
    )
    return Polygonization(normalized, tuple(warnings))
