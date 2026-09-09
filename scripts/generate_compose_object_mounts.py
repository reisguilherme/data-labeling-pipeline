#!/usr/bin/env python3
"""Gera mounts bind explícitos por raiz de objeto, sem mover dados.

O arquivo padrão ``compose.override.yml`` é carregado automaticamente pelo
Docker Compose ao lado de ``compose.yml``. A montagem ampla de ``WORKSPACE_DIR``
continua disponível para o registro e para objetos criados depois; os mounts
aninhados tornam cada ``raw``/``dataset`` já registrado um alvo persistente e
impedem que o Compose crie silenciosamente uma raiz ausente.
"""

from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path, PurePosixPath
from typing import NamedTuple


SERVICES = ("app", "worker", "sam3-worker")
ROOT_FIELDS = (("videos_root", "raw"), ("output_root", "dataset"))
OBJECT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
WINDOWS_ABSOLUTE_RE = re.compile(r"^[A-Za-z]:[\\/]")


class GenerationResult(NamedTuple):
    object_count: int
    mount_count: int
    output: Path


def _relative_parts(raw: object, *, object_id: str, field: str) -> tuple[str, ...]:
    value = str(raw or "").strip().replace("\\", "/")
    if not value:
        raise ValueError(f"objeto {object_id}: {field} ausente")
    if value.startswith("/") or WINDOWS_ABSOLUTE_RE.match(value):
        raise ValueError(
            f"objeto {object_id}: {field} precisa ser relativo ao workspace; "
            "execute o rehome antes de gerar os mounts"
        )
    path = PurePosixPath(value)
    if any(part in ("", ".", "..") for part in path.parts):
        raise ValueError(f"objeto {object_id}: {field} aponta para fora do workspace")
    return path.parts


def _load_mounts(workspace: Path) -> tuple[int, list[tuple[Path, str]]]:
    workspace = workspace.resolve(strict=True)
    if not workspace.is_dir() or workspace.is_symlink():
        raise ValueError(f"workspace nao e um diretorio seguro: {workspace}")
    registry_path = workspace / "objects.json"
    if not registry_path.is_file() or registry_path.is_symlink():
        raise ValueError(f"objects.json nao encontrado ou inseguro: {registry_path}")
    try:
        document = json.loads(registry_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"objects.json invalido: {registry_path}") from exc
    objects = document.get("objects")
    if not isinstance(objects, list) or not objects:
        raise ValueError(f"nenhum objeto cadastrado em {registry_path}")

    mounts: list[tuple[Path, str]] = []
    seen_ids: set[str] = set()
    for item in objects:
        if not isinstance(item, dict):
            raise ValueError("objects.json contem uma entrada invalida")
        object_id = str(item.get("object_id") or "")
        if not OBJECT_ID_RE.fullmatch(object_id) or object_id in seen_ids:
            raise ValueError(f"object_id invalido ou duplicado: {object_id!r}")
        seen_ids.add(object_id)
        for field, _kind in ROOT_FIELDS:
            parts = _relative_parts(item.get(field), object_id=object_id, field=field)
            lexical_source = workspace.joinpath(*parts)
            if lexical_source.is_symlink():
                raise ValueError(f"objeto {object_id}: raiz registrada e um symlink")
            try:
                source = lexical_source.resolve(strict=True)
                source.relative_to(workspace)
            except FileNotFoundError as exc:
                raise ValueError(
                    f"objeto {object_id}: raiz registrada nao existe: {lexical_source}"
                ) from exc
            except ValueError as exc:
                raise ValueError(
                    f"objeto {object_id}: {field} aponta para fora do workspace"
                ) from exc
            if not source.is_dir():
                raise ValueError(
                    f"objeto {object_id}: raiz registrada nao e diretorio: {source}"
                )
            relative = source.relative_to(workspace).as_posix()
            mounts.append((source, f"/workspace/{relative}"))

    roots = [source for source, _target in mounts]
    for index, left in enumerate(roots):
        for right in roots[index + 1 :]:
            if left == right or left in right.parents or right in left.parents:
                raise ValueError(
                    f"raizes registradas sobrepostas: {left} e {right}"
                )
    return len(objects), mounts


def _quoted(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _render(workspace: Path, mounts: list[tuple[Path, str]]) -> str:
    lines = [
        "# Gerado por scripts/generate_compose_object_mounts.py.",
        "# Nao edite: regenere depois de cadastrar ou rehomear um objeto.",
        f"# Workspace: {workspace}",
        "services:",
    ]
    for service in SERVICES:
        lines.extend((f"  {service}:", "    volumes:"))
        for source, target in mounts:
            lines.extend(
                (
                    "      - type: bind",
                    f"        source: {_quoted(str(source))}",
                    f"        target: {_quoted(target)}",
                    "        bind:",
                    "          create_host_path: false",
                )
            )
    return "\n".join(lines) + "\n"


def generate(workspace: Path, output: Path) -> GenerationResult:
    workspace = Path(workspace).expanduser().resolve(strict=True)
    object_count, mounts = _load_mounts(workspace)
    content = _render(workspace, mounts)
    output = Path(output).expanduser().resolve(strict=False)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, output)
    finally:
        temporary.unlink(missing_ok=True)
    return GenerationResult(object_count, len(mounts), output)


def main() -> int:
    project_root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(
        description="Gera compose.override.yml com mounts persistentes por objeto."
    )
    parser.add_argument(
        "--workspace",
        type=Path,
        default=Path(os.environ.get("WORKSPACE_DIR", project_root / "data" / "workspace")),
    )
    parser.add_argument(
        "--output", type=Path, default=project_root / "compose.override.yml"
    )
    args = parser.parse_args()
    result = generate(args.workspace, args.output)
    print(
        f"Override gerado em {result.output}: {result.object_count} objetos, "
        f"{result.mount_count} raizes persistentes. Nenhum dado foi movido."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
