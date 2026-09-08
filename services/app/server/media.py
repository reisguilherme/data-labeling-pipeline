"""Respostas HTTP Range e geração de thumbnails."""

from __future__ import annotations

import asyncio
import re
import subprocess
from collections.abc import Iterator
from pathlib import Path

from fastapi import HTTPException
from starlette.requests import Request
from starlette.responses import FileResponse, Response, StreamingResponse

from . import ffmpeg

CHUNK = 1024 * 1024

_RANGE_RE = re.compile(r"^bytes=(\d*)-(\d*)$")


def _parse_range(header: str, size: int) -> tuple[int, int] | None:
    """Retorna (start, end) inclusivos, ou None se malformado/insatisfazível.

    Aceita `bytes=START-`, `bytes=START-END` e o sufixo `bytes=-N` (últimos N
    bytes) — este último é o que os browsers usam para pegar o átomo `moov` de um
    mp4 sem faststart, então ignorá-lo quebra exatamente os arquivos brutos que
    esta ferramenta consome.
    """
    match = _RANGE_RE.match(header.strip())
    if not match:
        return None
    raw_start, raw_end = match.group(1), match.group(2)

    if not raw_start and not raw_end:
        return None

    if not raw_start:  # sufixo
        length = int(raw_end)
        if length <= 0:
            return None
        start = max(size - length, 0)
        return start, size - 1

    start = int(raw_start)
    if start >= size:
        return None
    end = int(raw_end) if raw_end else size - 1
    end = min(end, size - 1)
    if end < start:
        return None
    return start, end


def _iter_file(path: Path, start: int, end: int) -> Iterator[bytes]:
    """Gerador SÍNCRONO de propósito: o Starlette roda iteradores sync no
    threadpool, então o I/O de disco não bloqueia o event loop."""
    remaining = end - start + 1
    with open(path, "rb") as handle:
        handle.seek(start)
        while remaining > 0:
            data = handle.read(min(CHUNK, remaining))
            if not data:
                break
            remaining -= len(data)
            yield data


def range_response(request: Request, path: Path, media_type: str) -> Response:
    """Serve um arquivo honrando Range.

    Não usamos FileResponse aqui: o tratamento de Range do Starlette mudou entre
    versões, e devolver silenciosamente `200 + corpo inteiro` quebra o seek de um
    jeito que PARECE só lentidão do player — um dos bugs mais caros de
    diagnosticar nesse tipo de app.
    """
    if not path.exists():
        raise HTTPException(status_code=404, detail="arquivo não encontrado")

    size = path.stat().st_size
    header = request.headers.get("range")

    if header is None:
        return FileResponse(
            path,
            media_type=media_type,
            headers={"Accept-Ranges": "bytes", "Cache-Control": "no-cache"},
        )

    parsed = _parse_range(header, size)
    if parsed is None:
        return Response(
            status_code=416,
            headers={"Content-Range": f"bytes */{size}", "Accept-Ranges": "bytes"},
        )

    start, end = parsed
    return StreamingResponse(
        _iter_file(path, start, end),
        status_code=206,
        media_type=media_type,
        headers={
            "Content-Range": f"bytes {start}-{end}/{size}",
            "Content-Length": str(end - start + 1),
            "Accept-Ranges": "bytes",
            "Cache-Control": "no-cache",
        },
    )


# --------------------------------------------------------------------------
# thumbnails
# --------------------------------------------------------------------------

_thumb_gate = asyncio.Semaphore(3)


def thumb_path(ctx, video_id: str) -> Path:
    return ctx.cache_dir / "thumbs" / f"{video_id}.jpg"


def _is_near_black(path: Path) -> bool:
    try:
        from PIL import Image, ImageStat

        with Image.open(path) as image:
            return ImageStat.Stat(image.convert("L")).mean[0] < 8
    except Exception:  # noqa: BLE001 — heurística cosmética, nunca deve derrubar nada
        return False


def _run_thumb(src: Path, out: Path, seek: float) -> bool:
    out.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        ffmpeg.thumb_argv(src, out, seek), capture_output=True, text=True, timeout=120
    )
    return result.returncode == 0 and out.exists() and out.stat().st_size > 0


def _generate_thumb_sync(src: Path, out: Path, duration: float | None) -> bool:
    seek = (duration * 0.10) if duration else 3.0
    if not _run_thumb(src, out, seek):
        # Fallback: alguns arquivos não têm keyframe utilizável tão cedo.
        if not _run_thumb(src, out, 0.0):
            return False
    if _is_near_black(out) and duration:
        # Abertura em fade-in / tampa de lente: tenta o meio do vídeo.
        _run_thumb(src, out, duration * 0.5)
    return out.exists()


async def ensure_thumb(ctx, video_id: str, src: Path, duration: float | None) -> Path | None:
    out = thumb_path(ctx, video_id)
    if out.exists() and out.stat().st_size > 0:
        return out
    async with _thumb_gate:
        if out.exists() and out.stat().st_size > 0:
            return out
        ok = await asyncio.to_thread(_generate_thumb_sync, src, out, duration)
    return out if ok else None
