"""Extração de frames de proxy: modo completo e modo janela sob demanda.

Vídeos curtos ganham o proxy inteiro numa passada (navegação frame-a-frame
instantânea e contagem de frames exata de graça). Vídeos longos usam janelas em
torno do ponto marcado, com evicção LRU.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import stat
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable

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
CURRENT_POINTER = "CURRENT"
GENERATIONS_DIR = "generations"
MARKER_SCHEMA_VERSION = 2
_MAX_POINTER_BYTES = 128
_MAX_MARKER_BYTES = 16 * 1024
_GENERATION_TOKEN = re.compile(r"^[0-9a-f]{32}$")

# "small" alimenta filmstrip e reprodução; "full" é a imagem grande do palco.
Tier = str
SMALL = "small"
FULL = "full"
TIERS = (SMALL, FULL)


@dataclass(frozen=True)
class ProxyPlan:
    mode: str  # "full" | "window"
    frame_count: int


@dataclass(frozen=True)
class ProxyGeneration:
    path: Path
    token: str
    frames: int
    tier_counts: dict[str, int]
    kind: str | None = None
    start: int | None = None
    end: int | None = None


def proxy_dir(ctx, video_id: str, tier: Tier | None = None) -> Path:
    base = ctx.cache_dir / "proxy" / video_id
    return base / tier if tier else base


def window_dir(ctx, video_id: str, start: int, end: int, tier: Tier | None = None) -> Path:
    base = ctx.cache_dir / "windows" / video_id / f"{start:08d}_{end:08d}"
    return base / tier if tier else base


def frame_file(directory: Path, local_index: int) -> Path:
    return directory / f"{local_index:06d}.jpg"


def new_staging_generation(root: Path) -> Path:
    return root / GENERATIONS_DIR / f".{uuid.uuid4().hex}.part"


def _token_from_staging(staging: Path) -> str:
    name = staging.name
    if not (name.startswith(".") and name.endswith(".part")):
        raise ValueError("staging de proxy precisa usar .<uuid>.part")
    token = name[1:-5]
    if not _GENERATION_TOKEN.fullmatch(token):
        raise ValueError("token de geração inválido")
    return token


def _regular_nonempty(path: Path) -> bool:
    try:
        info = path.lstat()
    except OSError:
        return False
    return stat.S_ISREG(info.st_mode) and not path.is_symlink() and info.st_size > 0


def validate_generation(path: Path, *, expected_frames: int | None = None) -> ProxyGeneration:
    """Valida enumerando frames. Esta função roda no worker, nunca em request."""
    if not path.is_dir() or path.is_symlink():
        raise ValueError("geração de proxy ausente ou insegura")
    token = _token_from_staging(path) if path.name.endswith(".part") else path.name
    if not _GENERATION_TOKEN.fullmatch(token):
        raise ValueError("token de geração inválido")

    counts: dict[str, int] = {}
    for tier in TIERS:
        tier_dir = path / tier
        if not tier_dir.is_dir() or tier_dir.is_symlink():
            raise ValueError(f"tier {tier} ausente ou inseguro")
        files = sorted(tier_dir.glob("*.jpg"))
        if not files:
            raise ValueError(f"tier {tier} vazio")
        if [item.name for item in files] != [
            f"{index:06d}.jpg" for index in range(len(files))
        ]:
            raise ValueError(f"tier {tier} não é sequencial")
        if any(not _regular_nonempty(item) for item in files):
            raise ValueError(f"tier {tier} contém frame vazio ou inseguro")
        counts[tier] = len(files)
    if len(set(counts.values())) != 1:
        raise ValueError("tiers do proxy têm contagens diferentes")
    frames = counts[SMALL]
    if expected_frames is not None and frames != expected_frames:
        raise ValueError(f"proxy produziu {frames} frames; esperado {expected_frames}")
    return ProxyGeneration(path, token, frames, counts)


def _fsync_file(path: Path, data: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def publish_generation(
    staging: Path,
    root: Path,
    cache_root: Path,
    *,
    kind: str,
    job_id: str,
    start: int | None = None,
    end: int | None = None,
    expected_frames: int | None = None,
    publish_guard: Callable[[], bool] | None = None,
) -> ProxyGeneration:
    """Promove a geração e troca atomicamente apenas o ponteiro CURRENT."""
    if root.is_symlink():
        raise ValueError("raiz do proxy não pode ser symlink")
    root_resolved = root.resolve()
    cache_resolved = cache_root.resolve()
    if root_resolved == cache_resolved or not root_resolved.is_relative_to(cache_resolved):
        raise ValueError("raiz do proxy fora do cache")
    generations = root / GENERATIONS_DIR
    if generations.is_symlink():
        raise ValueError("pasta de gerações não pode ser symlink")
    if staging.parent.resolve() != generations.resolve():
        raise ValueError("staging fora da pasta de gerações")
    generation = validate_generation(staging, expected_frames=expected_frames)
    if kind not in {"full", "window"}:
        raise ValueError("tipo de geração inválido")
    if kind == "window" and (start is None or end is None or end < start):
        raise ValueError("intervalo da janela inválido")
    if kind == "window" and generation.frames != end - start + 1:
        raise ValueError("janela não contém todos os frames esperados")
    if publish_guard is not None and not publish_guard():
        raise RuntimeError("publicação cancelada")

    marker = {
        "schema_version": MARKER_SCHEMA_VERSION,
        "generation": generation.token,
        "job_id": str(job_id),
        "kind": kind,
        "start": start,
        "end": end,
        "frames": generation.frames,
        "tier_counts": generation.tier_counts,
        "width": PROXY_WIDTH,
        "qscale": PROXY_QSCALE,
        "stage_width": STAGE_WIDTH,
        "tiers": list(TIERS),
        "created_at": datetime.now(UTC).isoformat(),
    }
    _fsync_file(staging / COMPLETE_MARKER, json.dumps(marker, sort_keys=True))

    final = generations / generation.token
    generations.mkdir(parents=True, exist_ok=True)
    if final.exists():
        raise FileExistsError(f"geração já existe: {generation.token}")
    staging.replace(final)
    _fsync_directory(generations)

    if publish_guard is not None and not publish_guard():
        raise RuntimeError("publicação cancelada")

    pointer = root / CURRENT_POINTER
    pointer_part = root / f".{generation.token}.CURRENT.part"
    try:
        _fsync_file(pointer_part, generation.token + "\n")
        os.replace(pointer_part, pointer)
        _fsync_directory(root)
    finally:
        pointer_part.unlink(missing_ok=True)

    return ProxyGeneration(
        final, generation.token, generation.frames, generation.tier_counts, kind, start, end
    )


def _bounded_text(path: Path, limit: int) -> str | None:
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or path.is_symlink() or info.st_size > limit:
            return None
        return path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return None


def current_generation(root: Path, *, expected_kind: str | None = None) -> ProxyGeneration | None:
    """Resolve a geração publicada usando somente metadados e frames-limite."""
    if root.is_symlink() or (root / GENERATIONS_DIR).is_symlink():
        return None
    token = _bounded_text(root / CURRENT_POINTER, _MAX_POINTER_BYTES)
    if token is None or not _GENERATION_TOKEN.fullmatch(token):
        return None
    path = root / GENERATIONS_DIR / token
    if not path.is_dir() or path.is_symlink():
        return None
    raw = _bounded_text(path / COMPLETE_MARKER, _MAX_MARKER_BYTES)
    if raw is None:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict) or data.get("schema_version") != MARKER_SCHEMA_VERSION:
        return None
    if data.get("generation") != token or data.get("kind") not in {"full", "window"}:
        return None
    if expected_kind is not None and data.get("kind") != expected_kind:
        return None
    if data.get("width") != PROXY_WIDTH or data.get("stage_width") != STAGE_WIDTH:
        return None
    if data.get("qscale") != PROXY_QSCALE or list(data.get("tiers") or []) != list(TIERS):
        return None
    frames = data.get("frames")
    counts = data.get("tier_counts")
    if type(frames) is not int or frames <= 0 or not isinstance(counts, dict):
        return None
    if any(type(counts.get(tier)) is not int or counts.get(tier) != frames for tier in TIERS):
        return None
    for tier in TIERS:
        if not _regular_nonempty(frame_file(path / tier, 0)):
            return None
        if not _regular_nonempty(frame_file(path / tier, frames - 1)):
            return None
    start, end = data.get("start"), data.get("end")
    if data.get("kind") == "window":
        if type(start) is not int or type(end) is not int or end < start:
            return None
        if frames != end - start + 1:
            return None
    return ProxyGeneration(
        path, token, frames, {tier: frames for tier in TIERS}, data["kind"], start, end
    )


def is_complete(ctx, video_id: str) -> int | None:
    """Contagem real de frames se o proxy completo existe e ainda é válido.

    O marcador guarda a largura usada: subir PROXY_WIDTH precisa invalidar o cache
    sozinho, senão vídeos já visitados continuariam mostrando o proxy antigo e de
    baixa resolução para sempre, sem nenhum sinal do porquê.
    """
    generation = current_generation(proxy_dir(ctx, video_id), expected_kind="full")
    return generation.frames if generation is not None else None

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
            start, end = int(raw_start), int(raw_end)
        except ValueError:
            continue
        generation = current_generation(entry, expected_kind="window")
        if generation is None or generation.start != start or generation.end != end:
            continue
        ranges.append((start, end))
    return sorted(ranges)


def available_ranges(ctx, video_id: str) -> list[list[int]]:
    complete = is_complete(ctx, video_id)
    if complete is not None:
        return [[0, max(complete - 1, 0)]]

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

    full = current_generation(proxy_dir(ctx, video_id), expected_kind="full")
    if full is not None and 0 <= frame < full.frames:
        candidate = frame_file(full.path / tier, frame)
        if _regular_nonempty(candidate):
            return candidate

    for start, end in _windows_for(ctx, video_id):
        if start <= frame <= end:
            root = window_dir(ctx, video_id, start, end)
            generation = current_generation(root, expected_kind="window")
            if generation is None:
                continue
            candidate = frame_file(generation.path / tier, frame - start)
            if _regular_nonempty(candidate):
                _touch(root)
                return candidate

    return None


def status(ctx, video_id: str, frame_count: int | None) -> dict:
    generation = current_generation(proxy_dir(ctx, video_id), expected_kind="full")
    complete = generation.frames if generation is not None else None
    plan = plan_for(video_id, frame_count)
    active = [job.payload() for job in jobs.active_for_video(ctx.object_id, video_id)]
    return {
        "mode": "full" if complete is not None else plan.mode,
        "complete": complete is not None,
        "frame_count": complete if complete is not None else frame_count,
        "available_ranges": available_ranges(ctx, video_id),
        "generation": generation.token if generation is not None else None,
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

    # Cada tentativa recebe um staging próprio e invisível para as rotas.
    staging = new_staging_generation(out)
    for tier in TIERS:
        (staging / tier).mkdir(parents=True, exist_ok=True)

    src = ctx.index.resolve_path(video_id)
    job = jobs.create(
        "proxy_full", ctx.object_id, video_id, frame_count, "extraindo frames…",
        client_id=client_id,
    )

    def on_success(done_job: Job) -> dict:
        if done_job._cancelled:
            raise RuntimeError("publicação cancelada")
        generation = publish_generation(
            staging,
            out,
            ctx.cache_dir,
            kind="full",
            job_id=done_job.job_id,
            publish_guard=lambda: not done_job._cancelled,
        )
        produced = generation.frames
        # A contagem produzida É a verdade sobre o vídeo — promove sobre a
        # estimativa do container/packet.
        ctx.index.update_frame_count(video_id, produced, "proxy_extraction")
        if frame_count and abs(produced - frame_count) > 1:
            log.warning(
                "proxy de %s produziu %d frames, estimativa era %d",
                video_id, produced, frame_count,
            )
        return {"frames": produced, "dir": str(generation.path), "generation": generation.token}

    def on_cleanup(_: Job) -> None:
        # Remove apenas o staging desta tentativa; CURRENT nunca é tocado.
        shutil.rmtree(staging, ignore_errors=True)

    asyncio.create_task(
        jobs.run(
            job,
            ffmpeg.proxy_full_argv(
                src, staging / SMALL, staging / FULL
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
    staging = new_staging_generation(final)
    for tier in TIERS:
        (staging / tier).mkdir(parents=True, exist_ok=True)

    src = ctx.index.resolve_path(video_id)
    job = jobs.create(
        "proxy_window", ctx.object_id, video_id, end - start + 1, "extraindo janela…",
        client_id=client_id,
    )
    job.result = {"start": start, "end": end}

    def on_success(done_job: Job) -> dict:
        # A janela só passa a existir depois de validar os dois tiers.
        if done_job._cancelled:
            raise RuntimeError("publicação cancelada")
        generation = publish_generation(
            staging,
            final,
            ctx.cache_dir,
            kind="window",
            job_id=done_job.job_id,
            start=start,
            end=end,
            expected_frames=end - start + 1,
            publish_guard=lambda: not done_job._cancelled,
        )
        _touch(final)
        _evict_if_needed(ctx)
        return {
            "start": start,
            "end": end,
            "frames": generation.frames,
            "generation": generation.token,
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
