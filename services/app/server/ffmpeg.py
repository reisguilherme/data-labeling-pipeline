"""Resolução dos binários ffmpeg/ffprobe e TODOS os argv builders.

Este módulo é a única fonte dos invariantes de precisão de frame. Nenhum outro
lugar do projeto pode montar uma linha de comando de ffmpeg.

    Frame índice `n` = posição 0-based do frame na sequência decodificada do
    stream, sem duplicar nem descartar nenhum.

Isso é exatamente o que o filtergraph do ffmpeg conta sob `-fps_mode passthrough`.
Tempo nunca é a identidade de um frame — é só uma dica de exibição. Ver
`_FRAME_INVARIANTS` abaixo antes de tocar em qualquer coisa aqui.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

from .config import (
    BROWSER_CODECS,
    EXPORT_QSCALE,
    FFMPEG_THREADS,
    PROXY_QSCALE,
    PROXY_WIDTH,
    STAGE_QSCALE,
    STAGE_WIDTH,
    THUMB_QSCALE,
    THUMB_WIDTH,
)

# Documentação executável dos quatro flags que carregam toda a correção:
#
# 1. -fps_mode passthrough  Sem isso o modo `auto` pode normalizar para taxa
#    constante DUPLICANDO ou DESCARTANDO frames. Num vídeo VFR isso desloca
#    silenciosamente todos os índices após a primeira irregularidade: os frames
#    exportados ficariam *quase* certos, o que é pior que obviamente errados.
#
# 2. trim=start_frame=S:end_frame=E+1  em vez de select='between(n\,S\,E)'.
#    Contam idêntico, mas `trim` usa `:` e escapa do footgun das vírgulas dentro
#    de between(), que precisam de escape para o parser do FILTERGRAPH (não do
#    shell — morde mesmo passando lista argv). `end_frame` é EXCLUSIVO.
#
# 3. -start_number 0  O muxer image2 começa em 1 por padrão. Sem isso o frame
#    `n` vira o arquivo `n+1` e toda seta do teclado fica off-by-one.
#
# 4. -frames:v <count>  Sem ele o ffmpeg decodifica o resto do arquivo depois de
#    E. Num vídeo de 60 min é a diferença entre 4 s e 90 s.
_FRAME_INVARIANTS = ("-fps_mode", "passthrough", "-start_number", "0")

MIN_FFMPEG_VERSION = (5, 1)  # `-fps_mode` entrou aqui


class FFmpegError(RuntimeError):
    pass


@dataclass(frozen=True)
class FFmpegBinaries:
    ffmpeg: str
    ffprobe: str
    version: str
    version_tuple: tuple[int, ...]
    source: str


_binaries: FFmpegBinaries | None = None


def _probe_version(ffmpeg_path: str) -> tuple[str, tuple[int, ...]]:
    out = subprocess.run(
        [ffmpeg_path, "-hide_banner", "-version"],
        capture_output=True,
        text=True,
        timeout=30,
    ).stdout
    match = re.search(r"ffmpeg version (\S+)", out)
    if not match:
        raise FFmpegError(f"não consegui ler a versão de {ffmpeg_path}")
    raw = match.group(1)
    nums = re.match(r"(\d+)\.(\d+)", raw)
    if not nums:  # builds git-nightly não têm versão semântica; assume recente
        return raw, (99, 0)
    return raw, (int(nums.group(1)), int(nums.group(2)))


def _candidates() -> Iterator[tuple[str, str, str]]:
    """(fonte, ffmpeg, ffprobe) em ordem de precedência.

    GERADOR, não lista, e isso importa: `get_or_fetch_platform_executables_else_raise`
    do static_ffmpeg **baixa binários da internet** quando ainda não os tem. Numa
    lista, esse download acontecia mesmo quando MST_FFMPEG já apontava para um
    ffmpeg perfeitamente válido — bastava construir a lista. Num container sem
    rede isso é a primeira chamada travando por minutos. Sendo gerador, o
    fallback só é tocado se os anteriores realmente falharem.
    """
    env_ff, env_fp = os.environ.get("MST_FFMPEG"), os.environ.get("MST_FFPROBE")
    if env_ff and env_fp:
        yield "env", env_ff, env_fp

    which_ff, which_fp = shutil.which("ffmpeg"), shutil.which("ffprobe")
    if which_ff and which_fp:
        yield "path", which_ff, which_fp

    try:
        from static_ffmpeg import run as static_run

        yield ("static_ffmpeg", *static_run.get_or_fetch_platform_executables_else_raise())
    except Exception:  # noqa: BLE001 — fallback opcional, qualquer falha é ignorável
        return


def resolve() -> FFmpegBinaries:
    """Resolve os binários uma vez. Falha alto e claro se não achar nada."""
    global _binaries
    if _binaries is not None:
        return _binaries

    problems: list[str] = []
    for source, ff, fp in _candidates():
        if not (Path(ff).exists() and Path(fp).exists()):
            problems.append(f"{source}: caminho inexistente ({ff!r}, {fp!r})")
            continue
        try:
            version, version_tuple = _probe_version(ff)
        except Exception as exc:  # noqa: BLE001
            problems.append(f"{source}: {exc}")
            continue
        if version_tuple < MIN_FFMPEG_VERSION:
            problems.append(
                f"{source}: ffmpeg {version} é antigo demais "
                f"(mínimo {'.'.join(map(str, MIN_FFMPEG_VERSION))}, por causa de -fps_mode)"
            )
            continue
        _binaries = FFmpegBinaries(ff, fp, version, version_tuple, source)
        return _binaries

    detail = "\n  ".join(problems) if problems else "nenhum candidato encontrado"
    raise FFmpegError(
        "ffmpeg/ffprobe não encontrados ou inadequados.\n"
        f"  {detail}\n"
        "Resolva com uma das opções:\n"
        "  pip install static-ffmpeg   (isolado no venv, recomendado)\n"
        "  winget install Gyan.FFmpeg  (sistema)\n"
        "  set MST_FFMPEG / MST_FFPROBE apontando para os binários"
    )


# --------------------------------------------------------------------------
# ffprobe
# --------------------------------------------------------------------------

_PROBE_ENTRIES = (
    "stream=width,height,codec_name,pix_fmt,avg_frame_rate,r_frame_rate,"
    "nb_frames,duration,start_time"
    ":stream_side_data=rotation"
    ":format=duration"
)


def probe_argv(path: Path) -> list[str]:
    """Probe rápido de container — sem decodificar nada."""
    return [
        resolve().ffprobe,
        "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", _PROBE_ENTRIES,
        "-of", "json",
        str(path),
    ]


def count_packets_argv(path: Path) -> list[str]:
    """Contagem exata por demux, sem decode. ~1-3 s. Em mp4 H.264/265 1 pacote = 1 frame."""
    return [
        resolve().ffprobe,
        "-v", "error",
        "-select_streams", "v:0",
        "-count_packets",
        "-show_entries", "stream=nb_read_packets",
        "-of", "json",
        str(path),
    ]


def _fraction(value: str | None) -> float | None:
    if not value or value in ("0/0", "N/A"):
        return None
    try:
        result = float(Fraction(value))
    except (ZeroDivisionError, ValueError):
        return None
    return result or None


def parse_probe(raw: str) -> dict:
    """Normaliza a saída do ffprobe, tratando rotação e VFR.

    ARMADILHA: se o mp4 carrega matriz de rotação de ±90°, tanto o <video> quanto
    o ffmpeg autorotate exibem a imagem ROTACIONADA, mas `stream=width,height`
    reporta as dimensões CODIFICADAS. Normalizar bbox contra elas produz
    coordenadas transpostas e silenciosamente erradas — por isso trocamos aqui, e
    a verdade final ainda é conferida contra o JPEG extraído.
    """
    data = json.loads(raw)
    streams = data.get("streams") or []
    if not streams:
        raise FFmpegError("nenhum stream de vídeo encontrado")
    stream = streams[0]
    fmt = data.get("format") or {}

    coded_w = int(stream.get("width") or 0)
    coded_h = int(stream.get("height") or 0)

    rotation = 0
    for side in stream.get("side_data_list") or []:
        if "rotation" in side:
            rotation = int(side["rotation"])
            break
    rotation = ((rotation % 360) + 360) % 360

    width, height = coded_w, coded_h
    if rotation in (90, 270):
        width, height = coded_h, coded_w

    avg_rate = stream.get("avg_frame_rate")
    r_rate = stream.get("r_frame_rate")
    fps = _fraction(avg_rate) or _fraction(r_rate)

    duration = None
    for candidate in (stream.get("duration"), fmt.get("duration")):
        if candidate not in (None, "N/A"):
            try:
                duration = float(candidate)
                break
            except ValueError:
                continue

    nb_frames = stream.get("nb_frames")
    container_nb_frames = int(nb_frames) if nb_frames not in (None, "N/A") else None

    frame_count = container_nb_frames
    frame_count_source = "container"
    if frame_count is None and duration and fps:
        frame_count = int(round(duration * fps))
        frame_count_source = "duration_x_fps"

    codec = stream.get("codec_name") or "unknown"

    return {
        "duration_sec": duration,
        "width": width,
        "height": height,
        "coded_width": coded_w,
        "coded_height": coded_h,
        "rotation": rotation,
        "codec": codec,
        "pix_fmt": stream.get("pix_fmt"),
        "avg_frame_rate": avg_rate,
        "r_frame_rate": r_rate,
        "fps": fps,
        "start_time_sec": float(stream.get("start_time") or 0.0),
        "container_nb_frames": container_nb_frames,
        "frame_count": frame_count,
        "frame_count_source": frame_count_source,
        "frame_count_exact": False,
        # Rate médio != rate base é o sinal clássico de VFR. Só afeta a escala do
        # scrubber (o export é por número de frame), mas o usuário precisa saber
        # que o relógio do player não é confiável nesse arquivo.
        "is_vfr_suspect": bool(avg_rate and r_rate and avg_rate != r_rate),
        "browser_playable": codec in BROWSER_CODECS,
    }


# --------------------------------------------------------------------------
# extração
# --------------------------------------------------------------------------

_BASE = ("-hide_banner", "-loglevel", "error", "-progress", "pipe:1", "-nostats")
_NO_EXTRA_STREAMS = ("-an", "-sn", "-dn")


def _decoder_threads() -> list[str]:
    return ["-threads", str(FFMPEG_THREADS)]


def _encoder_threads() -> list[str]:
    return ["-threads", str(FFMPEG_THREADS)]


def _dual_output_args(small_dir: Path, full_dir: Path, count: int | None) -> tuple[str, list[str]]:
    """Filtergraph e saídas para produzir as duas resoluções num passe só.

    `split` duplica o vídeo já decodificado: paga-se o decode (que é o caro em 4K
    HEVC) uma vez e escreve-se os dois tamanhos. O ramo pequeno alimenta filmstrip
    e reprodução; o grande alimenta a imagem do palco.
    """
    graph = ["[0:v]split=2[a][b]", f"[a]scale={PROXY_WIDTH}:-2:flags=bilinear[small]"]
    if STAGE_WIDTH > 0:
        graph.append(f"[b]scale={STAGE_WIDTH}:-2:flags=bilinear[full]")
        full_label = "[full]"
    else:
        full_label = "[b]"

    limit = ["-frames:v", str(count)] if count is not None else []
    outputs = [
        "-map", "[small]", *limit,
        "-fps_mode", "passthrough", "-q:v", str(PROXY_QSCALE), "-start_number", "0",
        *_encoder_threads(),
        str(small_dir / "%06d.jpg"),
        "-map", full_label, *limit,
        "-fps_mode", "passthrough", "-q:v", str(STAGE_QSCALE), "-start_number", "0",
        *_encoder_threads(),
        str(full_dir / "%06d.jpg"),
    ]
    return ";".join(graph), outputs


def proxy_full_argv(src: Path, small_dir: Path, full_dir: Path) -> list[str]:
    """Extrai TODOS os frames, nas duas resoluções. A contagem produzida é a
    verdade absoluta sobre o número de frames do vídeo."""
    graph, outputs = _dual_output_args(small_dir, full_dir, None)
    return [
        resolve().ffmpeg, *_BASE, "-y",
        *_decoder_threads(),
        "-i", str(src),
        *_NO_EXTRA_STREAMS,
        "-filter_complex", graph,
        *outputs,
    ]


def proxy_window_argv(
    src: Path, small_dir: Path, full_dir: Path, start: int, end: int
) -> list[str]:
    """Janela nas duas resoluções, para vídeos longos demais para extração completa."""
    if start < 0 or end < start:
        raise ValueError(f"intervalo inválido: [{start}, {end}]")
    count = end - start + 1
    graph, outputs = _dual_output_args(small_dir, full_dir, count)
    # end_frame é EXCLUSIVO no filtro trim; o split vem depois para que o corte
    # seja feito uma vez só.
    graph = graph.replace(
        "[0:v]split=2", f"[0:v]trim=start_frame={start}:end_frame={end + 1},split=2", 1
    )
    return [
        resolve().ffmpeg, *_BASE, "-y",
        *_decoder_threads(),
        "-i", str(src),
        *_NO_EXTRA_STREAMS,
        "-filter_complex", graph,
        *outputs,
    ]


def export_segment_argv(src: Path, out_dir: Path, start: int, end: int) -> list[str]:
    """Frames finais que alimentam o SAM3: resolução original, alta qualidade.

    Usa exatamente o mesmo contador de frames do passe de proxy, então os dois
    concordam POR CONSTRUÇÃO — não por sorte.
    """
    if start < 0 or end < start:
        raise ValueError(f"intervalo inválido: [{start}, {end}]")
    count = end - start + 1
    return [
        resolve().ffmpeg, *_BASE, "-y",
        *_decoder_threads(),
        "-i", str(src),
        *_NO_EXTRA_STREAMS,
        # end_frame é EXCLUSIVO.
        "-vf", f"trim=start_frame={start}:end_frame={end + 1}",
        "-fps_mode", "passthrough",
        "-frames:v", str(count),
        "-q:v", str(EXPORT_QSCALE),
        "-start_number", "0",
        *_encoder_threads(),
        str(out_dir / "%06d.jpg"),
    ]


def thumb_argv(src: Path, out_path: Path, seek_sec: float) -> list[str]:
    """Thumbnail da biblioteca. `-ss` ANTES de `-i` = seek por keyframe (~150 ms).

    Aqui a imprecisão do seek é irrelevante — é só uma miniatura de identificação,
    nunca entra em nenhuma decisão de índice de frame.
    """
    return [
        resolve().ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
        "-ss", f"{max(seek_sec, 0):.3f}",
        *_decoder_threads(),
        "-i", str(src),
        "-frames:v", "1",
        "-vf", f"scale={THUMB_WIDTH}:-2:flags=bilinear",
        "-q:v", str(THUMB_QSCALE),
        *_encoder_threads(),
        str(out_path),
    ]
