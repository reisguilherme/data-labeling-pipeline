"""Extração de frames de proxy: modo completo e modo janela sob demanda.

Vídeos curtos ganham o proxy inteiro numa passada (navegação frame-a-frame
instantânea e contagem de frames exata de graça). Vídeos longos usam janelas em
torno do ponto marcado, com evicção LRU.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

from . import ffmpeg
from .config import (
    CACHE_LIMIT_GB,
    PROXY_MAX_FRAMES,
    PROXY_QSCALE,
    PROXY_WIDTH,
    STAGE_WIDTH,
    WINDOW_RADIUS,
)
from .jobs import Job, jobs

log = logging.getLogger("movies-screening-tool.proxy")

COMPLETE_MARKER = ".complete"

# "small" alimenta filmstrip e reprodução; "full" é a imagem grande do palco.
Tier = str
SMALL = "small"
FULL = "full"
TIERS = (SMALL, FULL)


@dataclass(frozen=True)
class ProxyPlan:
    mode: str  # "full" | "window"
    frame_count: int


def proxy_dir(ctx, video_id: str, tier: Tier | None = None) -> Path:
    base = ctx.cache_dir / "proxy" / video_id
    return base / tier if tier else base


def window_dir(ctx, video_id: str, start: int, end: int, tier: Tier | None = None) -> Path:
    base = ctx.cache_dir / "windows" / video_id / f"{start:08d}_{end:08d}"
    return base / tier if tier else base


def frame_file(directory: Path, local_index: int) -> Path:
    return directory / f"{local_index:06d}.jpg"


def _write_marker(directory: Path, frames: int) -> None:
    directory.joinpath(COMPLETE_MARKER).write_text(
        json.dumps(
            {
                "frames": frames,
                "width": PROXY_WIDTH,
                "qscale": PROXY_QSCALE,
                "stage_width": STAGE_WIDTH,
                "tiers": list(TIERS),
            }
        ),
        encoding="utf-8",
    )


def is_complete(ctx, video_id: str) -> int | None:
    """Contagem real de frames se o proxy completo existe e ainda é válido.

    O marcador guarda a largura usada: subir PROXY_WIDTH precisa invalidar o cache
    sozinho, senão vídeos já visitados continuariam mostrando o proxy antigo e de
    baixa resolução para sempre, sem nenhum sinal do porquê.
    """
    marker = proxy_dir(ctx, video_id) / COMPLETE_MARKER
    if not marker.exists():
        return None
    try:
        raw = marker.read_text(encoding="utf-8").strip()
    except OSError:
        return None

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None

    # ATENÇÃO: o formato antigo era a contagem crua ("2083"), que é JSON válido e
    # desserializa como int — sem esta checagem de tipo, o `.get` abaixo estoura e
    # derruba a rota. Marcador antigo = largura desconhecida = obsoleto.
    if not isinstance(data, dict):
        return None

    # Qualquer mudança de resolução invalida o cache: senão vídeos já visitados
    # seguiriam mostrando o proxy antigo para sempre, sem sinal do porquê.
    if data.get("width") != PROXY_WIDTH or data.get("stage_width") != STAGE_WIDTH:
        return None
    if list(data.get("tiers") or []) != list(TIERS):
        return None
    frames = data.get("frames")
    return int(frames) if isinstance(frames, int) else None


def plan_for(video_id: str, frame_count: int | None) -> ProxyPlan:
    if frame_count is None:
        # Sem contagem confiável, assume longo: janela é sempre segura.
        return ProxyPlan("window", 0)
    mode = "full" if frame_count <= PROXY_MAX_FRAMES else "window"
    return ProxyPlan(mode, frame_count)


# --------------------------------------------------------------------------
# ranges disponíveis
# --------------------------------------------------------------------------


def _windows_for(ctx, video_id: str) -> list[tuple[int, int]]:
    base = ctx.cache_dir / "windows" / video_id
    if not base.is_dir():
        return []
    ranges: list[tuple[int, int]] = []
    for entry in base.iterdir():
        if not entry.is_dir() or entry.name.endswith(".part"):
            continue
        try:
            raw_start, raw_end = entry.name.split("_")
            ranges.append((int(raw_start), int(raw_end)))
        except ValueError:
            continue
    return sorted(ranges)


def available_ranges(ctx, video_id: str) -> list[list[int]]:
    complete = is_complete(ctx, video_id)
    if complete is not None:
        return [[0, max(complete - 1, 0)]]

    # Extração completa em andamento: o muxer image2 escreve sequencialmente, então
    # o que já está no disco é o prefixo [0, n-1]. Reportar isso deixa o filmstrip
    # preencher ao vivo em vez de ficar cinza até o job acabar. Usa o MENOR dos
    # dois tiers, para nunca anunciar um frame que ainda não existe em ambos.
    partial = proxy_dir(ctx, video_id)
    if partial.is_dir():
        counts = [
            sum(1 for _ in partial.joinpath(tier).glob("*.jpg"))
            for tier in TIERS
            if partial.joinpath(tier).is_dir()
        ]
        if counts and min(counts):
            return [[0, min(counts) - 1]]

    merged: list[list[int]] = []
    for start, end in _windows_for(ctx, video_id):
        if merged and start <= merged[-1][1] + 1:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return merged


def locate_frame(ctx, video_id: str, frame: int, tier: Tier = SMALL) -> Path | None:
    """Caminho do JPEG do frame, procurando no proxy completo e nas janelas."""
    if tier not in TIERS:
        tier = SMALL

    candidate = frame_file(proxy_dir(ctx, video_id, tier), frame)
    if candidate.exists():
        return candidate

    for start, end in _windows_for(ctx, video_id):
        if start <= frame <= end:
            candidate = frame_file(
                window_dir(ctx, video_id, start, end, tier), frame - start
            )
            if candidate.exists():
                _touch(window_dir(ctx, video_id, start, end))
                return candidate

    return None


def status(ctx, video_id: str, frame_count: int | None) -> dict:
    complete = is_complete(ctx, video_id)
    plan = plan_for(video_id, frame_count)
    active = [job.payload() for job in jobs.active_for_video(ctx.object_id, video_id)]
    return {
        "mode": "full" if complete is not None else plan.mode,
        "complete": complete is not None,
        "frame_count": complete if complete is not None else frame_count,
        "available_ranges": available_ranges(ctx, video_id),
        "jobs": active,
    }


# --------------------------------------------------------------------------
# extração
# --------------------------------------------------------------------------


async def start_full(
    ctx, video_id: str, frame_count: int, *, force: bool = False, client_id: str | None = None
) -> Job | None:
    """Dispara a extração completa. Devolve None se já estiver pronta."""
    out = proxy_dir(ctx, video_id)

    if not force and is_complete(ctx, video_id) is not None:
        return None

    existing = [
        job
        for job in jobs.active_for_video(ctx.object_id, video_id)
        if job.kind == "proxy_full"
    ]
    if existing:
        return existing[0]

    # Sempre parte do zero: o ffmpeg reextrai desde o frame 0 de qualquer jeito, e
    # sobras de uma execução anterior (cancelada, ou feita com outra largura) se
    # misturariam com as novas e seriam contadas como disponíveis.
    if out.exists():
        await asyncio.to_thread(shutil.rmtree, out, True)
    for tier in TIERS:
        proxy_dir(ctx, video_id, tier).mkdir(parents=True, exist_ok=True)

    src = ctx.index.resolve_path(video_id)
    job = jobs.create(
        "proxy_full", ctx.object_id, video_id, frame_count, "extraindo frames…",
        client_id=client_id,
    )

    def on_success(_: Job) -> dict:
        produced = len(list(proxy_dir(ctx, video_id, SMALL).glob("*.jpg")))
        _write_marker(out, produced)
        # A contagem produzida É a verdade sobre o vídeo — promove sobre a
        # estimativa do container/packet.
        ctx.index.update_frame_count(video_id, produced, "proxy_extraction")
        if frame_count and abs(produced - frame_count) > 1:
            log.warning(
                "proxy de %s produziu %d frames, estimativa era %d",
                video_id, produced, frame_count,
            )
        return {"frames": produced, "dir": str(out)}

    def on_cleanup(_: Job) -> None:
        # O muxer image2 escreve sequencialmente: só o arquivo de maior número
        # pode estar truncado. Apaga esse em cada tier; o resto segue válido e
        # visível no filmstrip até a próxima extração recomeçar do zero.
        for tier in TIERS:
            files = sorted(proxy_dir(ctx, video_id, tier).glob("*.jpg"))
            if files:
                files[-1].unlink(missing_ok=True)

    asyncio.create_task(
        jobs.run(
            job,
            ffmpeg.proxy_full_argv(
                src, proxy_dir(ctx, video_id, SMALL), proxy_dir(ctx, video_id, FULL)
            ),
            on_success=on_success,
            on_cleanup=on_cleanup,
        )
    )
    return job


async def start_window(
    ctx,
    video_id: str,
    center: int,
    radius: int = WINDOW_RADIUS,
    frame_count: int | None = None,
    client_id: str | None = None,
    force: bool = False,
) -> tuple[Job | None, int, int]:
    """Extrai uma janela em torno de `center`. Devolve (job|None, start, end)."""
    start = max(center - radius, 0)
    end = center + radius
    if frame_count:
        end = min(end, frame_count - 1)
    if end < start:
        end = start

    if not force:
        for existing_start, existing_end in _windows_for(ctx, video_id):
            if existing_start <= start and end <= existing_end:
                return None, existing_start, existing_end

    for job in jobs.active_for_video(ctx.object_id, video_id):
        if job.kind == "proxy_window" and job.result.get("start") == start:
            return job, start, end

    final = window_dir(ctx, video_id, start, end)
    staging = final.with_name(final.name + ".part")
    if staging.exists():
        await asyncio.to_thread(shutil.rmtree, staging, True)
    for tier in TIERS:
        (staging / tier).mkdir(parents=True, exist_ok=True)

    src = ctx.index.resolve_path(video_id)
    job = jobs.create(
        "proxy_window", ctx.object_id, video_id, end - start + 1, "extraindo janela…",
        client_id=client_id,
    )
    job.result = {"start": start, "end": end}

    def on_success(_: Job) -> dict:
        # Publicação atômica: a janela só passa a existir completa.
        if final.exists():
            shutil.rmtree(final, ignore_errors=True)
        staging.replace(final)
        _touch(final)
        _evict_if_needed(ctx)
        return {
            "start": start,
            "end": end,
            "frames": len(list((final / SMALL).glob("*.jpg"))),
        }

    def on_cleanup(_: Job) -> None:
        shutil.rmtree(staging, ignore_errors=True)

    asyncio.create_task(
        jobs.run(
            job,
            ffmpeg.proxy_window_argv(src, staging / SMALL, staging / FULL, start, end),
            on_success=on_success,
            on_cleanup=on_cleanup,
        )
    )
    return job, start, end


# --------------------------------------------------------------------------
# cache LRU
# --------------------------------------------------------------------------


def _touch(path: Path) -> None:
    try:
        (path / ".used").write_text(str(time.time()), encoding="utf-8")
    except OSError:
        pass


def _last_used(path: Path) -> float:
    marker = path / ".used"
    try:
        return float(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        try:
            return path.stat().st_mtime
        except OSError:
            return 0.0


def _dir_size(path: Path) -> int:
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


def cache_size_bytes(ctx) -> int:
    cache = ctx.cache_dir
    return _dir_size(cache) if cache.exists() else 0


def _evict_if_needed(ctx) -> None:
    # O teto é POR OBJETO: com N objetos o disco pode chegar a N x CACHE_LIMIT_GB.
    # Evicção global exigiria conhecer todos os objetos aqui, e o cache é
    # regenerável — o teto é conforto, não limite rígido.
    limit = int(CACHE_LIMIT_GB * 1024**3)
    total = cache_size_bytes(ctx)
    if total <= limit:
        return

    # A unidade de evicção é o conjunto completo de um vídeo (proxy) ou de uma
    # janela — nunca um tier isolado, o que deixaria "small" sem o "full"
    # correspondente e frames do palco faltando sem explicação.
    candidates: list[tuple[float, Path, int]] = []

    proxy_base = ctx.cache_dir / "proxy"
    if proxy_base.is_dir():
        for entry in proxy_base.iterdir():
            if entry.is_dir():
                candidates.append((_last_used(entry), entry, _dir_size(entry)))

    windows_base = ctx.cache_dir / "windows"
    if windows_base.is_dir():
        for video_entry in windows_base.iterdir():
            if not video_entry.is_dir():
                continue
            for entry in video_entry.iterdir():
                if entry.is_dir():
                    candidates.append((_last_used(entry), entry, _dir_size(entry)))

    candidates.sort(key=lambda item: item[0])
    for _, path, size in candidates:
        if total <= limit:
            break
        shutil.rmtree(path, ignore_errors=True)
        total -= size
        log.info("cache: removido %s (%.1f MB)", path.name, size / 1024**2)


def clear_cache(ctx, video_id: str | None = None) -> None:
    if video_id:
        shutil.rmtree(proxy_dir(ctx, video_id), ignore_errors=True)
        shutil.rmtree(ctx.cache_dir / "windows" / video_id, ignore_errors=True)
        return
    for sub in ("proxy", "windows"):
        shutil.rmtree(ctx.cache_dir / sub, ignore_errors=True)
        (ctx.cache_dir / sub).mkdir(parents=True, exist_ok=True)
