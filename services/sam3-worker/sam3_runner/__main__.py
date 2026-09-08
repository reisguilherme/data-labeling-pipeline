"""CLI do runner.

    python -m sam3_runner validate --workspace /workspace [--object boom]
    python -m sam3_runner scan     --workspace /workspace
    python -m sam3_runner run      --workspace /workspace [--object boom] [--video X] [--force]
    python -m sam3_runner serve    [--api http://screening:8000]

`validate` e `scan` NÃO importam torch: rodam em qualquer máquina, sem GPU e sem
o pacote gated do SAM3. É de propósito — violação de contrato entre a triagem e
o runner é o tipo de coisa que se quer descobrir antes de a GPU esquentar.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from .config import VERSION, RunnerConfig
from .prompt import PromptError, check_against_disk, find_segments, load_prompt

log = logging.getLogger("sam3_runner")


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(levelname)-7s %(name)s: %(message)s",
    )


def _segments(cfg: RunnerConfig, object_id: str | None, video: str | None) -> list[Path]:
    found = find_segments(cfg.workspace, object_id)
    if video:
        needle = video.lower()
        found = [p for p in found if needle in p.parent.name.lower()]
    return found


# --------------------------------------------------------------------------
# validate
# --------------------------------------------------------------------------


def cmd_validate(args) -> int:
    cfg = RunnerConfig.from_env(args.workspace)
    segments = _segments(cfg, args.object, args.video)
    if not segments:
        print(f"nenhum segmento exportado em {cfg.workspace}")
        return 0

    print(f"validando {len(segments)} segmento(s) em {cfg.workspace}\n")
    bad = 0
    labels: set[str] = set()
    total_frames = 0

    for segment_dir in segments:
        rel = segment_dir.relative_to(cfg.workspace)
        try:
            prompt = load_prompt(segment_dir)
        except PromptError as exc:
            print(f"[ FALHA] {rel}\n         {exc}")
            bad += 1
            continue

        problems = check_against_disk(prompt)
        if problems:
            print(f"[ FALHA] {rel}")
            for problem in problems:
                print(f"         {problem}")
            bad += 1
            continue

        labels.update(prompt.labels())
        total_frames += prompt.frame_count
        if args.verbose:
            print(
                f"[  ok  ] {rel} — {prompt.frame_count} frames, "
                f"{len(prompt.objects)} objeto(s), prompt no frame {prompt.prompt_frame_idx}"
            )

    print(
        f"\n{len(segments) - bad}/{len(segments)} segmentos válidos, "
        f"{total_frames} frames, rótulos: {', '.join(sorted(labels)) or '(nenhum)'}"
    )
    return 1 if bad else 0


# --------------------------------------------------------------------------
# scan
# --------------------------------------------------------------------------


def cmd_scan(args) -> int:
    from .segment import should_skip

    cfg = RunnerConfig.from_env(args.workspace)
    segments = _segments(cfg, args.object, args.video)

    pending, done, broken = [], [], []
    for segment_dir in segments:
        try:
            prompt = load_prompt(segment_dir)
        except PromptError:
            broken.append(segment_dir)
            continue
        (done if should_skip(prompt, cfg) else pending).append(segment_dir)

    print(f"workspace : {cfg.workspace}")
    print(f"segmentos : {len(segments)}")
    print(f"  prontos : {len(done)}")
    print(f"  pendentes: {len(pending)}")
    print(f"  inválidos: {len(broken)}")
    if pending and args.verbose:
        print("\npendentes:")
        for segment_dir in pending[:50]:
            print(f"  {segment_dir.relative_to(cfg.workspace)}")
    return 0


# --------------------------------------------------------------------------
# run
# --------------------------------------------------------------------------


def cmd_run(args) -> int:
    from .classes import ClassMap
    from .model import build_predictor, device_info
    from .segment import run_segment, should_skip

    cfg = RunnerConfig.from_env(args.workspace)
    if args.max_side is not None:
        cfg.max_side = args.max_side

    segments = _segments(cfg, args.object, args.video)
    if not segments:
        print(f"nenhum segmento exportado em {cfg.workspace}")
        return 0

    prompts = []
    for segment_dir in segments:
        try:
            prompt = load_prompt(segment_dir)
        except PromptError as exc:
            log.warning("pulando %s: %s", segment_dir, exc)
            continue
        if not args.force and should_skip(prompt, cfg):
            continue
        prompts.append(prompt)

    if not prompts:
        print("tudo já processado (use --force para refazer)")
        return 0

    info = device_info()
    log.info("torch %s, cuda=%s %s", info.get("torch"), info.get("cuda"), info.get("device", ""))
    if not info.get("cuda"):
        log.warning("sem CUDA: a inferência vai rodar em CPU e será MUITO lenta")

    classes = ClassMap(cfg.classes_path)
    predictor = build_predictor()

    from .model import autocast_context

    ok = failed = 0
    for position, prompt in enumerate(prompts, start=1):
        rel = prompt.segment_dir.relative_to(cfg.workspace)
        log.info("[%d/%d] %s (%d frames)", position, len(prompts), rel, prompt.frame_count)
        with autocast_context():
            report = run_segment(predictor, prompt, cfg, classes)
        if report.status == "done":
            ok += 1
            log.info(
                "      %d/%d frames com objeto em %.1fs",
                report.frames_with_objects, report.frame_count, report.duration_sec or 0,
            )
        else:
            failed += 1
            log.error("      %s", report.error)

    print(f"\n{ok} segmento(s) processados, {failed} com erro")
    print(f"classes: {classes.names}")
    return 1 if failed else 0


# --------------------------------------------------------------------------
# serve
# --------------------------------------------------------------------------


def cmd_serve(args) -> int:
    from .queue_client import serve

    cfg = RunnerConfig.from_env(args.workspace)
    if args.api:
        cfg.api = args.api.rstrip("/")
    return serve(cfg, once=args.once)


# --------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="sam3_runner", description=__doc__)
    parser.add_argument("--version", action="version", version=VERSION)
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p, *, workspace_required: bool = True) -> None:
        p.add_argument("--workspace", type=Path,
                       default=None if workspace_required else Path("/workspace"))
        p.add_argument("--object", help="processa só este objeto")
        p.add_argument("--video", help="filtra por trecho do nome do vídeo")
        p.add_argument("-v", "--verbose", action="store_true")

    p_validate = sub.add_parser("validate", help="confere o contrato sem rodar o modelo")
    common(p_validate)
    p_validate.set_defaults(func=cmd_validate)

    p_scan = sub.add_parser("scan", help="lista o que falta processar")
    common(p_scan)
    p_scan.set_defaults(func=cmd_scan)

    p_run = sub.add_parser("run", help="processa segmentos agora")
    common(p_run)
    p_run.add_argument("--force", action="store_true", help="refaz o que já está pronto")
    p_run.add_argument("--max-side", type=int, default=None,
                       help="reescala o lado maior (contra falta de memória)")
    p_run.set_defaults(func=cmd_run)

    p_serve = sub.add_parser("serve", help="consome a fila da triagem continuamente")
    p_serve.add_argument("--workspace", type=Path, default=None)
    p_serve.add_argument("--api", help="URL da triagem")
    p_serve.add_argument("--once", action="store_true", help="processa um job e sai")
    p_serve.add_argument("-v", "--verbose", action="store_true")
    p_serve.set_defaults(func=cmd_serve)

    args = parser.parse_args(argv)
    _setup_logging(getattr(args, "verbose", False))
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
