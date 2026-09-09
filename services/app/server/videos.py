"""Varredura da pasta de vídeos, identidade estável e cache de probe."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import ffmpeg
from .config import VIDEO_EXTS


def iso(ts: float | None = None) -> str:
    dt = datetime.fromtimestamp(ts, tz=timezone.utc) if ts else datetime.now(timezone.utc)
    return dt.astimezone().isoformat(timespec="milliseconds")


def make_video_id(relpath: str) -> str:
    """Identidade estável derivada do caminho relativo.

    Hash em vez do caminho cru para manter nomes de cache curtos e ASCII mesmo
    com acentos e espaços na origem — e para caber em URLs sem escaping.
    """
    return hashlib.sha1(relpath.encode("utf-8")).hexdigest()[:12]


@dataclass(frozen=True)
class VideoFile:
    video_id: str
    relpath: str  # sempre com "/", para ser portável entre Windows e a Spark
    abspath: Path
    name: str  # stem, sem extensão
    size_bytes: int
    file_mtime: str


class VideoIndex:
    """Índice em memória dos arquivos encontrados na varredura.

    Uma instância POR OBJETO de anotação: os caminhos chegam pelo construtor em
    vez de virem de um singleton global, porque as extrações de proxy rodam em
    thread, depois do semáforo, e leem o diretório de cache na hora de gravar —
    com um "objeto ativo" global, trocar de objeto no meio de uma extração
    escreveria os frames no cache do outro objeto.
    """

    def __init__(self, videos_root: Path, cache_dir: Path) -> None:
        self.videos_root = videos_root
        self.cache_dir = cache_dir
        self._by_id: dict[str, VideoFile] = {}
        self._order: list[str] = []
        self._probe_cache: dict[str, dict] = {}
        self._probe_lock = threading.Lock()
        self.scanned_at: str | None = None

    # -- varredura ---------------------------------------------------------

    def scan(self) -> list[VideoFile]:
        """os.scandir recursivo. Deliberadamente NÃO dá ffprobe em nada: com 300
        arquivos isso custaria ~45 s de tela morta. Os campos de mídia chegam
        depois, do cache ou do probe sob demanda."""
        videos_root = self.videos_root
        found: list[VideoFile] = []

        def walk(directory: Path) -> None:
            try:
                entries = list(os.scandir(directory))
            except (PermissionError, OSError):
                return
            for entry in sorted(entries, key=lambda e: e.name.lower()):
                # "_" marca pastas de serviço ao lado dos vídeos úteis:
                # _trash (excluídos) e _incoming (download em andamento). Indexar
                # um download parcial mostraria vídeo truncado na biblioteca.
                if entry.name.startswith((".", "_")):
                    continue
                if entry.is_dir(follow_symlinks=False):
                    walk(Path(entry.path))
                elif entry.is_file(follow_symlinks=False):
                    if Path(entry.name).suffix.lower() not in VIDEO_EXTS:
                        continue
                    abspath = Path(entry.path)
                    relpath = abspath.relative_to(videos_root).as_posix()
                    stat = entry.stat()
                    found.append(
                        VideoFile(
                            video_id=make_video_id(relpath),
                            relpath=relpath,
                            abspath=abspath,
                            name=abspath.stem,
                            size_bytes=stat.st_size,
                            file_mtime=iso(stat.st_mtime),
                        )
                    )

        walk(videos_root)

        self._by_id = {video.video_id: video for video in found}
        self._order = [video.video_id for video in found]
        self.scanned_at = iso()
        self._load_probe_cache()
        return found

    def all(self) -> list[VideoFile]:
        return [self._by_id[vid] for vid in self._order]

    def get(self, video_id: str) -> VideoFile | None:
        return self._by_id.get(video_id)

    def by_relpath(self, relpath: str) -> VideoFile | None:
        return self._by_id.get(make_video_id(relpath))

    def resolve_path(self, video_id: str) -> Path:
        """Única porta de entrada de video_id -> caminho no disco.

        Nunca aceitamos caminho vindo do cliente; ainda assim revalidamos o
        confinamento sob videos_root, porque o índice pode estar velho depois de
        um rename ou de um symlink trocado.
        """
        video = self._by_id.get(video_id)
        if video is None:
            raise KeyError(video_id)
        resolved = video.abspath.resolve()
        if not resolved.is_relative_to(self.videos_root.resolve()):
            raise PermissionError(f"caminho fora de videos_root: {resolved}")
        return resolved

    # -- cache de probe ----------------------------------------------------

    @property
    def _probe_path(self) -> Path:
        return self.cache_dir / "probe.json"

    def _load_probe_cache(self) -> None:
        path = self._probe_path
        if not path.exists():
            self._probe_cache = {}
            return
        try:
            self._probe_cache = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            self._probe_cache = {}

    def _save_probe_cache(self) -> None:
        path = self._probe_path
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(self._probe_cache, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        os.replace(tmp, path)

    def cached_probe(self, video_id: str) -> dict | None:
        return self._probe_cache.get(video_id)

    def store_probe(self, video_id: str, media: dict) -> None:
        with self._probe_lock:
            self._probe_cache[video_id] = media
            self._save_probe_cache()

    def update_frame_count(self, video_id: str, frame_count: int, source: str) -> None:
        """Promove a contagem de frames quando surge uma fonte mais confiável."""
        with self._probe_lock:
            media = self._probe_cache.get(video_id)
            if media is None:
                return
            media["frame_count"] = frame_count
            media["frame_count_source"] = source
            media["frame_count_exact"] = source == "proxy_extraction"
            self._save_probe_cache()

    # -- probe -------------------------------------------------------------

    def probe_sync(self, video_id: str, *, count_packets: bool = False) -> dict:
        path = self.resolve_path(video_id)
        import subprocess

        result = subprocess.run(
            ffmpeg.probe_argv(path), capture_output=True, text=True, timeout=120
        )
        if result.returncode != 0:
            raise ffmpeg.FFmpegError(result.stderr.strip() or "ffprobe falhou")
        media = ffmpeg.parse_probe(result.stdout)

        if count_packets:
            packets = subprocess.run(
                ffmpeg.count_packets_argv(path), capture_output=True, text=True, timeout=600
            )
            if packets.returncode == 0:
                try:
                    streams = json.loads(packets.stdout).get("streams") or [{}]
                    value = streams[0].get("nb_read_packets")
                    if value not in (None, "N/A"):
                        media["frame_count"] = int(value)
                        media["frame_count_source"] = "packet_count"
                except (json.JSONDecodeError, ValueError, IndexError):
                    pass

        media["probed_at"] = iso()
        self.store_probe(video_id, media)
        return media

    async def probe(self, video_id: str, *, count_packets: bool = False) -> dict:
        return await asyncio.to_thread(
            self.probe_sync, video_id, count_packets=count_packets
        )


# --------------------------------------------------------------------------
# nome da pasta de saída
# --------------------------------------------------------------------------

_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def export_folder_name(video: VideoFile, taken: set[str] | None = None) -> str:
    """Nome da pasta de saída a partir do stem do arquivo.

    O ``video_id`` faz parte do nome sempre. Consultar o conteúdo atual da pasta
    tornava a identidade dependente da ordem das requisições e permitia que dois
    ``clip.mp4`` em subpastas diferentes apontassem para o mesmo export.

    ``taken`` permanece no contrato apenas para compatibilidade com chamadas
    antigas; a identidade nova não depende dele.
    """
    base = _UNSAFE.sub("_", video.name).strip(" .") or video.video_id
    safe_id = _UNSAFE.sub("_", video.video_id).strip(" .")
    return f"{base}__{safe_id or hashlib.sha1(video.relpath.encode('utf-8')).hexdigest()[:12]}"
