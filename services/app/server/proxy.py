"""Extração de frames de proxy: modo completo e modo janela sob demanda.

Vídeos curtos ganham o proxy inteiro numa passada (navegação frame-a-frame
instantânea e contagem de frames exata de graça). Vídeos longos usam janelas em
torno do ponto marcado, com evicção LRU.
"""

from __future__ import annotations

import asyncio
import heapq
import json
import logging
import math
import os
import re
import shutil
import stat
import threading
import time
import uuid
from bisect import bisect_right
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Callable, Iterator

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
RETIRE_MARKER = ".retired"
CURRENT_POINTER = "CURRENT"
GENERATIONS_DIR = "generations"
WINDOWS_INDEX = "INDEX.json"
WINDOW_SEQUENCE = ".sequence"
SWEEP_CURSOR = ".proxy-sweep-cursor"
MARKER_SCHEMA_VERSION = 2
WINDOWS_INDEX_SCHEMA_VERSION = 3
_MAX_POINTER_BYTES = 128
_MAX_MARKER_BYTES = 16 * 1024
_MAX_WINDOWS_INDEX_BYTES = 4 * 1024 * 1024
_MAX_SWEEP_CURSOR_BYTES = 4 * 1024
_DEFAULT_GC_DELETE_LIMIT = 8
_DEFAULT_SWEEP_ENTRY_LIMIT = 1024
_DEFAULT_SWEEP_TIME_BUDGET_SECONDS = 0.25
_MAX_SWEEP_STREAMS = 64
GENERATION_RETIRE_GRACE_SECONDS = 120.0
STAGING_ORPHAN_GRACE_SECONDS = 24 * 60 * 60.0
_GENERATION_TOKEN = re.compile(r"^[0-9a-f]{32}$")
_GC_TRASH = re.compile(r"^\.gc-[0-9a-f]{32}$")
_WINDOW_ROOT = re.compile(r"^([0-9]{8,12})_([0-9]{8,12})$")

_SweepRoot = tuple[str, Path, tuple[int, int] | None]
_SWEEP_STREAMS: dict[str, Iterator[_SweepRoot | None]] = {}
_SWEEP_STREAMS_LOCK = threading.Lock()

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
    sequence: int | None = None


@dataclass(frozen=True)
class WindowIndexSnapshot:
    entries: tuple[tuple[int, int, str, int, str | None], ...]
    spans: tuple[
        tuple[int, int, tuple[tuple[int, int, str, str | None], ...]], ...
    ]
    span_starts: tuple[int, ...]


def proxy_dir(ctx, video_id: str, tier: Tier | None = None) -> Path:
    base = ctx.cache_dir / "proxy" / video_id
    return base / tier if tier else base


def window_dir(ctx, video_id: str, start: int, end: int, tier: Tier | None = None) -> Path:
    base = ctx.cache_dir / "windows" / video_id / f"{start:08d}_{end:08d}"
    return base / tier if tier else base


def frame_file(directory: Path, local_index: int) -> Path:
    return directory / f"{local_index:06d}.jpg"


def new_staging_generation(root: Path) -> Path:
    """Reserva um staging único sob a mesma trava usada por publish/eviction."""
    if root.is_symlink():
        raise ValueError("raiz do proxy não pode ser symlink")
    staging = root / GENERATIONS_DIR / f".{uuid.uuid4().hex}.part"
    with _exclusive_file_lock(root / ".publish.lock"):
        staging.mkdir(parents=True)
    return staging


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


def _regular_directory(path: Path) -> bool:
    try:
        info = path.lstat()
    except OSError:
        return False
    return stat.S_ISDIR(info.st_mode) and not path.is_symlink()


def _resolved_child(path: Path, *roots: Path) -> bool:
    try:
        resolved = path.resolve(strict=True)
        for root in roots:
            parent = root.resolve(strict=True)
            if resolved == parent or not resolved.is_relative_to(parent):
                return False
        return True
    except OSError:
        return False


def _safe_frame(path: Path, generation_root: Path, cache_root: Path | None = None) -> bool:
    roots = (generation_root,) if cache_root is None else (generation_root, cache_root)
    return _resolved_child(path, *roots) and _regular_nonempty(path)


def _require_cache_child(path: Path, cache_root: Path) -> tuple[Path, Path]:
    resolved = path.resolve()
    cache_resolved = cache_root.resolve()
    if resolved == cache_resolved or not resolved.is_relative_to(cache_resolved):
        raise ValueError("raiz do proxy fora do cache")
    return resolved, cache_resolved


@contextmanager
def _exclusive_file_lock(path: Path, *, blocking: bool = True) -> Iterator[bool]:
    """Trava de processo para commits curtos; o SO a libera após crash."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise ValueError("arquivo de lock nao pode ser symlink")
    with path.open("a+b") as raw:
        handle = raw
        if os.name == "nt":
            import msvcrt

            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            mode = msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK
            try:
                msvcrt.locking(handle.fileno(), mode, 1)
            except OSError:
                if blocking:
                    raise
                yield False
                return
            try:
                yield True
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            flags = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
            try:
                fcntl.flock(handle.fileno(), flags)
            except OSError:
                if blocking:
                    raise
                yield False
                return
            try:
                yield True
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


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
    """Promove a geração e troca atomicamente apenas o ponteiro CURRENT.

    ``publish_guard`` roda depois do fsync do ponteiro temporário e imediatamente
    antes do syscall que torna a geração visível. Essa é a menor janela prática
    de fencing: a lease ainda pode mudar depois da guarda, mas nesse ponto
    ``os.replace`` é a única operação linearizável restante.
    """
    if root.is_symlink():
        raise ValueError("raiz do proxy não pode ser symlink")
    _require_cache_child(root, cache_root)
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

    marker: dict[str, object] = {
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

    final = generations / generation.token
    generations.mkdir(parents=True, exist_ok=True)
    pointer = root / CURRENT_POINTER
    pointer_part = root / f".{generation.token}.CURRENT.part"
    previous_token: str | None = None
    sequence: int | None = None

    def commit_current() -> None:
        nonlocal previous_token
        _fsync_file(staging / COMPLETE_MARKER, json.dumps(marker, sort_keys=True))
        staging.replace(final)
        _fsync_directory(generations)
        try:
            _fsync_file(pointer_part, generation.token + "\n")
            raw_previous = _bounded_text(pointer, _MAX_POINTER_BYTES)
            if raw_previous is not None and _GENERATION_TOKEN.fullmatch(raw_previous):
                previous_token = raw_previous
            if publish_guard is not None and not publish_guard():
                raise RuntimeError("publicação cancelada")
            os.replace(pointer_part, pointer)
            _fsync_directory(root)
            if previous_token is not None and previous_token != generation.token:
                previous = generations / previous_token
                try:
                    if _regular_directory(previous):
                        _fsync_file(previous / RETIRE_MARKER, str(time.time()))
                except OSError:
                    log.warning(
                        "não foi possível aposentar geração %s", previous_token
                    )
        finally:
            pointer_part.unlink(missing_ok=True)

    with _exclusive_file_lock(root / ".publish.lock"):
        if final.exists():
            raise FileExistsError(f"geração já existe: {generation.token}")
        if kind == "window":
            assert start is not None and end is not None
            base = root.parent
            _validate_window_root(root, start, end, cache_root)
            with _exclusive_file_lock(base / ".index.lock"):
                sequence = _allocate_window_sequence_locked(base)
                marker["sequence"] = sequence
                commit_current()
                _publish_window_index_locked(
                    root,
                    start,
                    end,
                    generation.token,
                    sequence,
                    cache_root,
                )
        else:
            commit_current()

    if previous_token is not None and previous_token != generation.token:
        try:
            _schedule_stale_generation_gc(root, cache_root)
        except Exception:
            log.warning("não foi possível agendar GC de %s", root, exc_info=True)

    return ProxyGeneration(
        final,
        generation.token,
        generation.frames,
        generation.tier_counts,
        kind,
        start,
        end,
        sequence,
    )


def _bounded_text(path: Path, limit: int) -> str | None:
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or path.is_symlink() or info.st_size > limit:
            return None
        return path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return None


def _empty_window_index() -> WindowIndexSnapshot:
    return WindowIndexSnapshot((), (), ())


def _compile_window_spans(
    entries: tuple[tuple[int, int, str, int, str | None], ...],
) -> tuple[
    tuple[int, int, tuple[tuple[int, int, str, str | None], ...]], ...
]:
    events: dict[
        int, list[tuple[bool, tuple[int, int, str, int, str | None]]]
    ] = {}
    for entry in entries:
        start, end, _, _, _ = entry
        events.setdefault(start, []).append((True, entry))
        events.setdefault(end + 1, []).append((False, entry))

    active: set[int] = set()
    winners: list[tuple[int, int, int, str, str | None]] = []
    spans: list[
        tuple[int, int, tuple[tuple[int, int, str, str | None], ...]]
    ] = []
    points = sorted(events)
    for offset, point in enumerate(points[:-1]):
        for add, entry in events[point]:
            start, end, name, sequence, generation = entry
            if add:
                active.add(sequence)
                heapq.heappush(
                    winners, (-sequence, start, end, name, generation)
                )
            else:
                active.discard(sequence)
        while winners and -winners[0][0] not in active:
            heapq.heappop(winners)
        next_point = points[offset + 1]
        if not winners or next_point <= point:
            continue
        _, start, end, name, generation = winners[0]
        span = (point, next_point - 1, ((start, end, name, generation),))
        if spans and spans[-1][1] + 1 == span[0] and spans[-1][2] == span[2]:
            previous = spans[-1]
            spans[-1] = (previous[0], span[1], previous[2])
        else:
            spans.append(span)
    return tuple(spans)


@lru_cache(maxsize=256)
def _parse_windows_index_snapshot(
    path_text: str,
    identity: tuple[int, int, int, int, int],
) -> WindowIndexSnapshot:
    del identity  # compõe a chave; o conteúdo vem do path atômico.
    raw = _bounded_text(Path(path_text), _MAX_WINDOWS_INDEX_BYTES)
    if raw is None:
        return _empty_window_index()
    try:
        data = json.loads(raw)
    except (TypeError, ValueError, RecursionError, OverflowError):
        return _empty_window_index()
    if not isinstance(data, dict) or data.get("schema_version") not in {1, 2, 3}:
        return _empty_window_index()
    windows = data.get("windows")
    if type(windows) is not list:
        return _empty_window_index()

    parsed: dict[tuple[int, int], tuple[str, int, str | None]] = {}
    used_sequences: set[int] = set()
    schema_version = data["schema_version"]
    for position, item in enumerate(windows, start=1):
        if not isinstance(item, dict):
            return _empty_window_index()
        start, end, name = item.get("start"), item.get("end"), item.get("root")
        if type(start) is not int or type(end) is not int or start < 0 or end < start:
            return _empty_window_index()
        if type(name) is not str or name != f"{start:08d}_{end:08d}":
            return _empty_window_index()
        sequence = item.get("seq", position) if schema_version == 2 else position
        if schema_version == 3:
            sequence = item.get("seq")
        if type(sequence) is not int or sequence <= 0 or sequence in used_sequences:
            return _empty_window_index()
        generation = item.get("generation") if schema_version == 3 else None
        if generation is not None and (
            type(generation) is not str or not _GENERATION_TOKEN.fullmatch(generation)
        ):
            return _empty_window_index()
        if schema_version == 3 and generation is None:
            return _empty_window_index()
        if (start, end) in parsed:
            return _empty_window_index()
        used_sequences.add(sequence)
        parsed[(start, end)] = (name, sequence, generation)
    entries = tuple(
        (
            start,
            end,
            parsed[(start, end)][0],
            parsed[(start, end)][1],
            parsed[(start, end)][2],
        )
        for start, end in sorted(parsed)
    )
    spans = _compile_window_spans(entries)
    return WindowIndexSnapshot(entries, spans, tuple(span[0] for span in spans))


def _windows_index(base: Path) -> WindowIndexSnapshot:
    path = base / WINDOWS_INDEX
    try:
        info = path.lstat()
    except OSError:
        return _empty_window_index()
    if not stat.S_ISREG(info.st_mode) or path.is_symlink() or info.st_size > _MAX_WINDOWS_INDEX_BYTES:
        return _empty_window_index()
    identity = (
        info.st_dev,
        info.st_ino,
        info.st_mtime_ns,
        info.st_ctime_ns,
        info.st_size,
    )
    return _parse_windows_index_snapshot(str(path), identity)


def _window_candidates(
    base: Path, frame: int
) -> Iterator[tuple[int, int, str, str | None]]:
    snapshot = _windows_index(base)
    position = bisect_right(snapshot.span_starts, frame) - 1
    if position < 0:
        return
    cover_start, cover_end, candidates = snapshot.spans[position]
    if cover_start <= frame <= cover_end:
        yield from candidates


def _validate_window_root(
    root: Path, start: int, end: int, cache_root: Path
) -> Path:
    base = root.parent
    _require_cache_child(base, cache_root)
    expected_name = f"{start:08d}_{end:08d}"
    if root.name != expected_name or root.parent.is_symlink():
        raise ValueError("raiz de janela inválida")
    return base


def _allocate_window_sequence_locked(base: Path) -> int:
    raw = _bounded_text(base / WINDOW_SEQUENCE, 64)
    try:
        persisted = int(raw) if raw is not None else 0
    except (TypeError, ValueError, OverflowError):
        persisted = 0
    indexed = max((entry[3] for entry in _windows_index(base).entries), default=0)
    sequence = max(persisted, indexed) + 1
    part = base / f".{uuid.uuid4().hex}.{WINDOW_SEQUENCE}.part"
    try:
        _fsync_file(part, str(sequence))
        os.replace(part, base / WINDOW_SEQUENCE)
        _fsync_directory(base)
    finally:
        part.unlink(missing_ok=True)
    return sequence


def _entries_for_index_write(
    base: Path, cache_root: Path
) -> dict[tuple[int, int], tuple[str, int, str]]:
    entries: dict[tuple[int, int], tuple[str, int, str]] = {}
    for start, end, name, sequence, generation in _windows_index(base).entries:
        if generation is None:
            current = current_generation(
                base / name, expected_kind="window", cache_root=cache_root
            )
            if current is None:
                continue
            generation = current.token
        entries[(start, end)] = (name, sequence, generation)
    return entries


def _publish_window_index_locked(
    root: Path,
    start: int,
    end: int,
    generation: str,
    sequence: int,
    cache_root: Path,
) -> None:
    base = _validate_window_root(root, start, end, cache_root)
    entries = _entries_for_index_write(base, cache_root)
    entries[(start, end)] = (root.name, sequence, generation)
    _replace_windows_index(base, base / WINDOWS_INDEX, entries)


def _replace_windows_index(
    base: Path,
    index_path: Path,
    entries: dict[tuple[int, int], tuple[str, int, str]],
) -> None:
    payload = {
        "schema_version": WINDOWS_INDEX_SCHEMA_VERSION,
        "windows": [
            {
                "start": item_start,
                "end": item_end,
                "root": entries[(item_start, item_end)][0],
                "seq": entries[(item_start, item_end)][1],
                "generation": entries[(item_start, item_end)][2],
            }
            for item_start, item_end in sorted(entries)
        ],
    }
    part = base / f".{uuid.uuid4().hex}.{WINDOWS_INDEX}.part"
    try:
        _fsync_file(part, json.dumps(payload, sort_keys=True))
        os.replace(part, index_path)
        _fsync_directory(base)
        _parse_windows_index_snapshot.cache_clear()
    finally:
        part.unlink(missing_ok=True)


def _remove_window_index(root: Path, start: int, end: int, cache_root: Path) -> bool:
    base = root.parent
    _require_cache_child(base, cache_root)
    index_path = base / WINDOWS_INDEX
    with _exclusive_file_lock(base / ".index.lock"):
        entries = _entries_for_index_write(base, cache_root)
        current = entries.get((start, end))
        if current is None:
            return True
        if current[0] != root.name:
            return False
        del entries[(start, end)]
        _replace_windows_index(base, index_path, entries)
    return True


def collect_stale_generations(
    root: Path,
    cache_root: Path,
    *,
    max_delete: int = _DEFAULT_GC_DELETE_LIMIT,
    retire_grace_seconds: float = GENERATION_RETIRE_GRACE_SECONDS,
    staging_grace_seconds: float = STAGING_ORPHAN_GRACE_SECONDS,
    blocking: bool = True,
) -> int:
    """Remove gerações imutáveis antigas sem tocar CURRENT ou stagings ativos."""
    _require_cache_child(root, cache_root)
    if type(max_delete) is not int or max_delete < 0:
        raise ValueError("limite de coleta inválido")
    if not isinstance(retire_grace_seconds, (int, float)) or retire_grace_seconds < 0:
        raise ValueError("grace period inválido")
    if not isinstance(staging_grace_seconds, (int, float)) or staging_grace_seconds < 0:
        raise ValueError("grace period de staging inválido")
    if max_delete == 0:
        return 0
    generations = root / GENERATIONS_DIR
    if not _regular_directory(generations) or not _resolved_child(generations, root):
        return 0

    victims: list[Path] = []
    trash: Path | None = None
    with _exclusive_file_lock(root / ".publish.lock", blocking=blocking) as acquired:
        if not acquired:
            return 0
        now = time.time()
        current = _bounded_text(root / CURRENT_POINTER, _MAX_POINTER_BYTES)
        protected = current if current is not None and _GENERATION_TOKEN.fullmatch(current) else None
        candidates: list[tuple[float, int, str, Path]] = []
        abandoned_stagings: list[tuple[float, int, str, Path]] = []
        for entry in generations.iterdir():
            if entry.name.endswith(".part"):
                if not _regular_directory(entry) or not _resolved_child(
                    entry, generations, root
                ):
                    continue
                last_activity = _tree_last_activity(entry)
                if (
                    last_activity is not None
                    and last_activity <= now
                    and now - last_activity >= staging_grace_seconds
                ):
                    abandoned_stagings.append(
                        (last_activity, 0, entry.name, entry)
                    )
                continue
            if entry.name == protected or not _GENERATION_TOKEN.fullmatch(entry.name):
                continue
            try:
                info = entry.lstat()
            except OSError:
                continue
            if not stat.S_ISDIR(info.st_mode) or entry.is_symlink():
                continue
            if not _resolved_child(entry, generations, root):
                continue
            raw_retired = _bounded_text(entry / RETIRE_MARKER, 128)
            try:
                retired_at = float(raw_retired) if raw_retired is not None else math.nan
            except (TypeError, ValueError, OverflowError):
                retired_at = math.nan
            if not math.isfinite(retired_at) or retired_at > now:
                retired_at = now
                _fsync_file(entry / RETIRE_MARKER, str(retired_at))
            candidates.append((retired_at, info.st_mtime_ns, entry.name, entry))

        previous = max(candidates, default=None)
        previous_token = previous[2] if protected is not None and previous is not None else None
        eligible = [
            item
            for item in candidates
            if item[2] != previous_token and now - item[0] >= retire_grace_seconds
        ]
        selected = [
            entry
            for _, _, _, entry in sorted(eligible + abandoned_stagings)[:max_delete]
        ]
        if selected:
            trash = root / f".gc-{uuid.uuid4().hex}"
            trash.mkdir()
            for entry in selected:
                destination = trash / entry.name
                entry.replace(destination)
                victims.append(destination)
            _fsync_directory(generations)
            _fsync_directory(root)

    # A movimentação curta acima retira as vítimas do namespace publicado. A
    # deleção potencialmente lenta acontece sem segurar a trava de publicação.
    if trash is not None:
        shutil.rmtree(trash)
    return len(victims)


def _collect_stale_generations_safely(root: Path, cache_root: Path) -> None:
    try:
        collect_stale_generations(root, cache_root)
    except Exception:
        log.warning("falha ao coletar gerações antigas de %s", root, exc_info=True)


def _schedule_stale_generation_gc(
    root: Path,
    cache_root: Path,
    *,
    delay: float = GENERATION_RETIRE_GRACE_SECONDS,
) -> None:
    """Dispara manutenção daemon; conclusão do job não espera rmtree."""
    worker = threading.Timer(
        max(float(delay), 0.0),
        _collect_stale_generations_safely,
        args=(root, cache_root),
    )
    worker.name = f"proxy-gc-{root.name}"
    worker.daemon = True
    try:
        worker.start()
    except Exception:
        log.warning("não foi possível iniciar GC de %s", root, exc_info=True)


def current_generation(
    root: Path,
    *,
    expected_kind: str | None = None,
    cache_root: Path | None = None,
) -> ProxyGeneration | None:
    """Resolve a geração publicada usando somente metadados e frames-limite."""
    generations = root / GENERATIONS_DIR
    if not _regular_directory(root) or not _regular_directory(generations):
        return None
    if cache_root is not None:
        try:
            _require_cache_child(root, cache_root)
        except ValueError:
            return None
    if not _resolved_child(generations, root):
        return None
    token = _bounded_text(root / CURRENT_POINTER, _MAX_POINTER_BYTES)
    if token is None or not _GENERATION_TOKEN.fullmatch(token):
        return None
    path = generations / token
    if not _regular_directory(path) or not _resolved_child(path, generations, root):
        return None
    raw = _bounded_text(path / COMPLETE_MARKER, _MAX_MARKER_BYTES)
    if raw is None:
        return None
    try:
        data = json.loads(raw)
    except (TypeError, ValueError, RecursionError, OverflowError):
        return None
    if not isinstance(data, dict) or data.get("schema_version") != MARKER_SCHEMA_VERSION:
        return None
    kind = data.get("kind")
    if data.get("generation") != token or type(kind) is not str:
        return None
    if kind not in {"full", "window"}:
        return None
    if expected_kind is not None and kind != expected_kind:
        return None
    if data.get("width") != PROXY_WIDTH or data.get("stage_width") != STAGE_WIDTH:
        return None
    tiers = data.get("tiers")
    if data.get("qscale") != PROXY_QSCALE or type(tiers) is not list:
        return None
    if tiers != list(TIERS):
        return None
    frames = data.get("frames")
    counts = data.get("tier_counts")
    if type(frames) is not int or frames <= 0 or not isinstance(counts, dict):
        return None
    if any(type(counts.get(tier)) is not int or counts.get(tier) != frames for tier in TIERS):
        return None
    for tier in TIERS:
        tier_dir = path / tier
        if not _regular_directory(tier_dir) or not _resolved_child(tier_dir, path):
            return None
        if not _safe_frame(frame_file(tier_dir, 0), path, cache_root):
            return None
        if not _safe_frame(frame_file(tier_dir, frames - 1), path, cache_root):
            return None
    start, end = data.get("start"), data.get("end")
    sequence = data.get("sequence")
    if kind == "window":
        if type(start) is not int or type(end) is not int or end < start:
            return None
        if frames != end - start + 1:
            return None
        if sequence is not None and (type(sequence) is not int or sequence <= 0):
            return None
    elif sequence is not None:
        return None
    return ProxyGeneration(
        path,
        token,
        frames,
        {tier: frames for tier in TIERS},
        kind,
        start,
        end,
        sequence,
    )


def _scan_sweep_events(cache_root: Path) -> Iterator[_SweepRoot | None]:
    """Um evento por dir entry; nenhum nivel e materializado ou ordenado."""
    proxy_base = cache_root / "proxy"
    if _regular_directory(proxy_base) and _resolved_child(proxy_base, cache_root):
        try:
            with os.scandir(proxy_base) as entries:
                for raw in entries:
                    root = Path(raw.path)
                    candidate: _SweepRoot | None = None
                    try:
                        if raw.is_dir(follow_symlinks=False) and _resolved_child(
                            root, proxy_base, cache_root
                        ):
                            candidate = (
                                root.relative_to(cache_root).as_posix(),
                                root,
                                None,
                            )
                    except OSError:
                        pass
                    yield candidate
        except OSError:
            log.warning(
                "falha ao enumerar roots de proxy em %s", proxy_base, exc_info=True
            )

    windows_base = cache_root / "windows"
    if not _regular_directory(windows_base) or not _resolved_child(
        windows_base, cache_root
    ):
        return
    try:
        with os.scandir(windows_base) as videos:
            for raw_video in videos:
                video = Path(raw_video.path)
                try:
                    valid_video = raw_video.is_dir(
                        follow_symlinks=False
                    ) and _resolved_child(video, windows_base, cache_root)
                except OSError:
                    valid_video = False
                # A propria entrada do video tambem consome o budget.
                yield None
                if not valid_video:
                    continue
                try:
                    with os.scandir(video) as windows:
                        for raw_root in windows:
                            root = Path(raw_root.path)
                            candidate = None
                            match = _WINDOW_ROOT.fullmatch(root.name)
                            try:
                                valid_root = raw_root.is_dir(
                                    follow_symlinks=False
                                ) and _resolved_child(
                                    root, video, windows_base, cache_root
                                )
                            except OSError:
                                valid_root = False
                            if match is not None and valid_root:
                                start, end = int(match.group(1)), int(match.group(2))
                                if end >= start and root.name == f"{start:08d}_{end:08d}":
                                    candidate = (
                                        root.relative_to(cache_root).as_posix(),
                                        root,
                                        (start, end),
                                    )
                            yield candidate
                except OSError:
                    log.warning(
                        "falha ao enumerar janelas de %s", video, exc_info=True
                    )
    except OSError:
        log.warning(
            "falha ao enumerar videos com janelas em %s",
            windows_base,
            exc_info=True,
        )


def _rotating_sweep_events(
    cache_root: Path, cursor: str | None
) -> Iterator[_SweepRoot | None]:
    """Percorre ciclos fisicos completos, iniciando depois do cursor salvo."""
    anchor = cursor
    while True:
        saw_entry = False
        found_anchor = anchor is None
        if anchor is not None:
            for candidate in _scan_sweep_events(cache_root):
                saw_entry = True
                if not found_anchor:
                    yield None
                    if candidate is not None and candidate[0] == anchor:
                        found_anchor = True
                else:
                    yield candidate

            # Completa a volta do inicio ate o anchor. Se o root sumiu, esta
            # segunda passagem vira um ciclo completo e recupera o progresso.
            for candidate in _scan_sweep_events(cache_root):
                saw_entry = True
                yield candidate
                if found_anchor and candidate is not None and candidate[0] == anchor:
                    break
        else:
            for candidate in _scan_sweep_events(cache_root):
                saw_entry = True
                yield candidate
        if not saw_entry:
            return


def _discovery_stream(cache_root: Path) -> Iterator[_SweepRoot | None]:
    key = str(cache_root.resolve())
    stream = _SWEEP_STREAMS.get(key)
    if stream is not None:
        return stream
    if len(_SWEEP_STREAMS) >= _MAX_SWEEP_STREAMS:
        stale_key = next(iter(_SWEEP_STREAMS))
        stale = _SWEEP_STREAMS.pop(stale_key)
        stale.close()
    cursor = _bounded_text(cache_root / SWEEP_CURSOR, _MAX_SWEEP_CURSOR_BYTES)
    stream = _rotating_sweep_events(cache_root, cursor)
    _SWEEP_STREAMS[key] = stream
    return stream


def _take_sweep_roots(
    cache_root: Path,
    *,
    max_roots: int,
    entry_budget: int,
    time_budget_seconds: float,
) -> tuple[list[_SweepRoot], int]:
    selected: list[_SweepRoot] = []
    scanned = 0
    deadline = time.monotonic() + time_budget_seconds
    if max_roots == 0 or entry_budget == 0 or time_budget_seconds == 0:
        return selected, scanned
    key = str(cache_root.resolve())
    with _SWEEP_STREAMS_LOCK:
        stream = _discovery_stream(cache_root)
        while scanned < entry_budget and len(selected) < max_roots:
            if scanned and time.monotonic() >= deadline:
                break
            try:
                candidate = next(stream)
            except StopIteration:
                _SWEEP_STREAMS.pop(key, None)
                break
            scanned += 1
            if candidate is not None:
                selected.append(candidate)
    return selected, scanned


def _store_sweep_cursor(cache_root: Path, key: str) -> None:
    if len(key.encode("utf-8")) > _MAX_SWEEP_CURSOR_BYTES:
        raise ValueError("cursor de sweep excede o limite")
    part = cache_root / f".{uuid.uuid4().hex}.{SWEEP_CURSOR}.part"
    try:
        _fsync_file(part, key)
        os.replace(part, cache_root / SWEEP_CURSOR)
        _fsync_directory(cache_root)
    finally:
        part.unlink(missing_ok=True)


def _cleanup_gc_trash(
    root: Path, cache_root: Path, *, max_delete: int
) -> tuple[int, int]:
    """Retenta tombstones de GC; rmtree potencialmente lento fica fora da trava."""
    if max_delete <= 0:
        return 0, 0
    victims: list[Path] = []
    with _exclusive_file_lock(root / ".publish.lock", blocking=False) as acquired:
        if not acquired:
            return 0, 0
        try:
            for entry in root.iterdir():
                if len(victims) >= max_delete:
                    break
                if (
                    _GC_TRASH.fullmatch(entry.name)
                    and _regular_directory(entry)
                    and _resolved_child(entry, root, cache_root)
                ):
                    victims.append(entry)
        except OSError:
            return 0, 1

    removed = 0
    errors = 0
    for victim in victims:
        try:
            shutil.rmtree(victim)
            removed += 1
        except FileNotFoundError:
            # Outro coletor concluiu o mesmo tombstone: estado desejado atingido.
            removed += 1
        except OSError:
            errors += 1
            log.warning(
                "falha ao remover tombstone de proxy %s", victim, exc_info=True
            )
    return removed, errors


def _reconcile_window_index(
    root: Path,
    cache_root: Path,
    start: int,
    end: int,
) -> bool:
    """Reconcilia CURRENT -> INDEX apos crash/publicacao ou eviccao parcial."""
    base = _validate_window_root(root, start, end, cache_root)
    with _exclusive_file_lock(root / ".publish.lock", blocking=False) as acquired:
        if not acquired:
            return False
        generation = current_generation(
            root, expected_kind="window", cache_root=cache_root
        )
        if generation is not None and (
            generation.start != start or generation.end != end
        ):
            generation = None

        with _exclusive_file_lock(base / ".index.lock"):
            snapshot = _windows_index(base)
            had_legacy_entries = any(entry[4] is None for entry in snapshot.entries)
            entries = _entries_for_index_write(base, cache_root)
            key = (start, end)
            current = entries.get(key)
            if generation is None:
                if current is None and not had_legacy_entries:
                    return False
                entries.pop(key, None)
                _replace_windows_index(base, base / WINDOWS_INDEX, entries)
                return True

            sequence = generation.sequence
            if sequence is None:
                sequence = current[1] if current is not None else None
            if sequence is None:
                sequence = _allocate_window_sequence_locked(base)
            wanted = (root.name, sequence, generation.token)
            if current == wanted and not had_legacy_entries:
                return False
            entries[key] = wanted
            _replace_windows_index(base, base / WINDOWS_INDEX, entries)
            return True


def sweep_cache(
    ctx,
    *,
    max_roots: int = 16,
    max_delete_per_root: int = _DEFAULT_GC_DELETE_LIMIT,
    retire_grace_seconds: float = GENERATION_RETIRE_GRACE_SECONDS,
    staging_grace_seconds: float = STAGING_ORPHAN_GRACE_SECONDS,
    discovery_entry_budget: int = _DEFAULT_SWEEP_ENTRY_LIMIT,
    time_budget_seconds: float = _DEFAULT_SWEEP_TIME_BUDGET_SECONDS,
) -> dict[str, int]:
    """Manutencao limitada e retomavel de artefatos deixados por crashes.

    O cursor e apenas de progresso; toda operacao e idempotente e revalida o
    root sob a trava de publicacao. Falhas sao contabilizadas e nunca propagadas
    para o loop de jobs.
    """
    if type(max_roots) is not int or max_roots < 0:
        raise ValueError("limite de roots do sweep invalido")
    if type(max_delete_per_root) is not int or max_delete_per_root < 0:
        raise ValueError("limite de remocao do sweep invalido")
    if type(discovery_entry_budget) is not int or discovery_entry_budget < 0:
        raise ValueError("budget de entradas do sweep invalido")
    if (
        not isinstance(time_budget_seconds, (int, float))
        or not math.isfinite(time_budget_seconds)
        or time_budget_seconds < 0
    ):
        raise ValueError("budget de tempo do sweep invalido")
    cache_root = Path(ctx.cache_dir)
    result = {
        "roots_scanned": 0,
        "artifacts_removed": 0,
        "trash_removed": 0,
        "index_repairs": 0,
        "entries_scanned": 0,
        "errors": 0,
    }
    if not _regular_directory(cache_root) or cache_root.is_symlink():
        return result

    roots, entries_scanned = _take_sweep_roots(
        cache_root,
        max_roots=max_roots,
        entry_budget=discovery_entry_budget,
        time_budget_seconds=float(time_budget_seconds),
    )
    result["entries_scanned"] = entries_scanned
    for key, root, window_range in roots:
        result["roots_scanned"] += 1
        try:
            removed, errors = _cleanup_gc_trash(
                root, cache_root, max_delete=max_delete_per_root
            )
        except Exception:
            removed, errors = 0, 1
            log.warning("falha ao recuperar tombstones de %s", root, exc_info=True)
        result["trash_removed"] += removed
        result["errors"] += errors
        remaining = max(max_delete_per_root - removed, 0)
        if remaining:
            try:
                result["artifacts_removed"] += collect_stale_generations(
                    root,
                    cache_root,
                    max_delete=remaining,
                    retire_grace_seconds=retire_grace_seconds,
                    staging_grace_seconds=staging_grace_seconds,
                    blocking=False,
                )
            except Exception:
                result["errors"] += 1
                log.warning("falha ao coletar artefatos de %s", root, exc_info=True)
        if window_range is not None:
            try:
                if _reconcile_window_index(
                    root, cache_root, window_range[0], window_range[1]
                ):
                    result["index_repairs"] += 1
            except Exception:
                result["errors"] += 1
                log.warning("falha ao reconciliar indice de %s", root, exc_info=True)
        try:
            _store_sweep_cursor(cache_root, key)
        except Exception:
            result["errors"] += 1
            log.warning("falha ao persistir cursor do sweep", exc_info=True)
    return result


def is_complete(ctx, video_id: str) -> int | None:
    """Contagem real de frames se o proxy completo existe e ainda é válido.

    O marcador guarda a largura usada: subir PROXY_WIDTH precisa invalidar o cache
    sozinho, senão vídeos já visitados continuariam mostrando o proxy antigo e de
    baixa resolução para sempre, sem nenhum sinal do porquê.
    """
    generation = current_generation(
        proxy_dir(ctx, video_id), expected_kind="full", cache_root=ctx.cache_dir
    )
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
    if not _regular_directory(base):
        return []
    try:
        _require_cache_child(base, ctx.cache_dir)
    except ValueError:
        return []
    ranges: list[tuple[int, int]] = []
    for cover_start, cover_end, winners in _windows_index(base).spans:
        if not winners:
            continue
        start, end, name, indexed_generation = winners[0]
        entry = base / name
        generation = current_generation(
            entry, expected_kind="window", cache_root=ctx.cache_dir
        )
        if generation is None or generation.start != start or generation.end != end:
            continue
        if indexed_generation is not None and generation.token != indexed_generation:
            continue
        ranges.append((cover_start, cover_end))
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

    full = current_generation(
        proxy_dir(ctx, video_id), expected_kind="full", cache_root=ctx.cache_dir
    )
    if full is not None and 0 <= frame < full.frames:
        candidate = frame_file(full.path / tier, frame)
        if _safe_frame(candidate, full.path, ctx.cache_dir):
            _touch(proxy_dir(ctx, video_id))
            return candidate

    base = ctx.cache_dir / "windows" / video_id
    if not _regular_directory(base):
        return None
    try:
        _require_cache_child(base, ctx.cache_dir)
    except ValueError:
        return None
    for start, end, name, indexed_generation in _window_candidates(base, frame):
        root = base / name
        generation = current_generation(
            root, expected_kind="window", cache_root=ctx.cache_dir
        )
        if generation is None or generation.start != start or generation.end != end:
            continue
        if indexed_generation is not None and generation.token != indexed_generation:
            continue
        candidate = frame_file(generation.path / tier, frame - start)
        if _safe_frame(candidate, generation.path, ctx.cache_dir):
            _touch(root)
            return candidate

    return None


def status(ctx, video_id: str, frame_count: int | None) -> dict:
    generation = current_generation(
        proxy_dir(ctx, video_id), expected_kind="full", cache_root=ctx.cache_dir
    )
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
        try:
            _evict_if_needed(ctx)
        except Exception:
            log.warning("falha na manutenção LRU após publicar janela", exc_info=True)
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
    total = 0
    try:
        for item in path.rglob("*"):
            try:
                if item.is_file() and not item.is_symlink():
                    total += item.stat().st_size
            except OSError:
                continue
    except OSError:
        return total
    return total


def _tree_last_activity(path: Path) -> float | None:
    """Best-effort activity clock; uncertainty always protects the staging."""
    try:
        latest = path.stat().st_mtime
        for entry in path.rglob("*"):
            latest = max(latest, entry.stat(follow_symlinks=False).st_mtime)
        return latest
    except (OSError, TypeError):
        return None


def cache_size_bytes(ctx) -> int:
    cache = ctx.cache_dir
    return _dir_size(cache) if cache.exists() else 0


def _has_active_staging(
    generations: Path,
    *,
    now: float | None = None,
    grace_seconds: float = STAGING_ORPHAN_GRACE_SECONDS,
) -> bool:
    observed_at = time.time() if now is None else now
    try:
        for entry in generations.iterdir():
            if not entry.name.endswith(".part") or not _regular_directory(entry):
                continue
            last_activity = _tree_last_activity(entry)
            if (
                last_activity is None
                or last_activity > observed_at
                or observed_at - last_activity < grace_seconds
            ):
                return True
    except OSError:
        return True
    return False


def _retire_proxy_root(root: Path, cache_root: Path) -> bool:
    """Despublica um root sem bloquear publisher nem apagar frames já resolvidos."""
    _require_cache_child(root, cache_root)
    with _exclusive_file_lock(root / ".publish.lock", blocking=False) as acquired:
        if not acquired:
            return False
        generations = root / GENERATIONS_DIR
        if not _regular_directory(generations) or _has_active_staging(generations):
            return False
        generation = current_generation(root, cache_root=cache_root)
        if generation is None:
            return False
        if generation.kind == "window" and (
            generation.start is None or generation.end is None
        ):
            return False

        # Falhar antes de despublicar mantém CURRENT e INDEX coerentes. Depois
        # do unlink, uma entrada stale no índice é fail-safe: leitores sempre
        # revalidam CURRENT antes de servir qualquer frame.
        _fsync_file(generation.path / RETIRE_MARKER, str(time.time()))
        (root / CURRENT_POINTER).unlink(missing_ok=True)
        _fsync_directory(root)
        if generation.kind == "window":
            assert generation.start is not None and generation.end is not None
            try:
                _remove_window_index(
                    root, generation.start, generation.end, cache_root
                )
            except Exception:
                log.warning(
                    "janela aposentada ficou com entrada stale no índice: %s",
                    root,
                    exc_info=True,
                )

    _schedule_stale_generation_gc(root, cache_root)
    return True


def _evict_if_needed_impl(ctx) -> None:
    limit = int(CACHE_LIMIT_GB * 1024**3)
    total = cache_size_bytes(ctx)
    if total <= limit:
        return

    candidates: list[tuple[float, Path, int]] = []
    proxy_base = ctx.cache_dir / "proxy"
    if _regular_directory(proxy_base):
        for entry in proxy_base.iterdir():
            if _regular_directory(entry):
                candidates.append((_last_used(entry), entry, _dir_size(entry)))

    windows_base = ctx.cache_dir / "windows"
    if _regular_directory(windows_base):
        for video_entry in windows_base.iterdir():
            if not _regular_directory(video_entry):
                continue
            for entry in video_entry.iterdir():
                if _regular_directory(entry):
                    candidates.append((_last_used(entry), entry, _dir_size(entry)))

    for _, path, size in sorted(candidates, key=lambda item: item[0]):
        if total <= limit:
            break
        if _retire_proxy_root(path, ctx.cache_dir):
            total -= size
            log.info("cache: aposentado %s (%.1f MB)", path.name, size / 1024**2)


def _evict_if_needed(ctx) -> None:
    # O teto é POR OBJETO: com N objetos o disco pode chegar a N x CACHE_LIMIT_GB.
    # Evicção global exigiria conhecer todos os objetos aqui, e o cache é
    # regenerável — o teto é conforto, não limite rígido.
    try:
        _evict_if_needed_impl(ctx)
    except Exception:
        log.warning("falha na manutenção LRU do cache", exc_info=True)


def clear_cache(ctx, video_id: str | None = None) -> None:
    if video_id:
        shutil.rmtree(proxy_dir(ctx, video_id), ignore_errors=True)
        shutil.rmtree(ctx.cache_dir / "windows" / video_id, ignore_errors=True)
        return
    for sub in ("proxy", "windows"):
        shutil.rmtree(ctx.cache_dir / sub, ignore_errors=True)
        (ctx.cache_dir / sub).mkdir(parents=True, exist_ok=True)
