# API Performance and Multiworker Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reduzir a latência das APIs e da navegação da triagem, permitir dois jobs CPU simultâneos com orçamento de recursos e manter uma única instância SAM3.

**Architecture:** A biblioteca passa a consultar manifestos SAM3 já validados em vez de decodificar todas as máscaras a cada request; compatibilidade legada usa auditoria cacheada fora do event loop. A UI navega usando seu snapshot local e atualiza a biblioteca em segundo plano. O Compose replica somente o worker CPU e limita threads do FFmpeg.

**Tech Stack:** Python 3.12, FastAPI/Starlette, PostgreSQL/psycopg, React 19, TypeScript, Zustand, Docker Compose, FFmpeg, unittest e smoke tests JSDOM.

**Spec:** `docs/superpowers/specs/2026-09-03-api-performance-multiworker-design.md`

## Global Constraints

- Não modificar, mover ou reprocessar vídeos, frames, máscaras ou revisões existentes.
- Máscaras continuam canônicas; bbox/área continuam derivadas da máscara efetiva.
- Não remover a validação pixel a pixel de `sam3_runner.mask_io.validate_mask_set()`.
- Exportação/auditoria explícita continua abrindo e validando os PNGs selecionados.
- `GET /videos` não pode abrir PNGs de runs `png-1bit-v1` com manifesto válido.
- Manter um único processo Uvicorn enquanto locks e workspace tiverem estado em memória.
- Manter exatamente um `sam3-worker`; a fila GPU continua serial.
- Usar duas réplicas do worker CPU por padrão, com 4 CPUs, 8 GiB e 4 threads FFmpeg por réplica.
- PostgreSQL, leases, idempotência e `FOR UPDATE SKIP LOCKED` continuam sendo a coordenação autoritativa.
- Navegação só pode ocorrer após a mutação ou criação do job ter sido confirmada pelo backend; ela não deve aguardar a listagem global.
- Não adicionar Redis, Celery ou outro serviço.
- Não registrar tokens, cookies, secrets ou query strings sensíveis nos logs.
- Este diretório não possui metadados Git. Ao final de cada tarefa, criar um checkpoint verificável; executar os comandos de commit indicados somente se o projeto for inicializado como repositório Git antes da implementação.

---

### Task 1: Instrumentar duração de requests lentos

**Files:**
- Modify: `services/app/server/config.py`
- Modify: `services/app/server/main.py`
- Modify: `compose.yml`
- Modify: `.env.example`
- Create: `services/app/server/tests/test_request_timing.py`

**Interfaces:**
- Consumes: `MST_SLOW_REQUEST_SECONDS`, default `1.0`.
- Produces: header `Server-Timing: app;dur=<milliseconds>` em toda resposta normal.
- Produces: log `slow_request method=<...> path=<...> status=<...> duration_ms=<...>` somente quando a duração atingir o limiar.

- [ ] Adicionar primeiro o teste abaixo em `services/app/server/tests/test_request_timing.py`:

```python
from __future__ import annotations

import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from server.main import app


class RequestTimingTests(unittest.TestCase):
    def test_health_exposes_server_timing(self) -> None:
        with TestClient(app) as client:
            response = client.get("/api/health")

        self.assertEqual(response.status_code, 200)
        self.assertRegex(response.headers["server-timing"], r"^app;dur=\d+\.\d{2}$")

    def test_slow_request_is_logged_without_query_string(self) -> None:
        with (
            patch("server.main.SLOW_REQUEST_SECONDS", 0.0),
            self.assertLogs("movies-screening-tool", level="WARNING") as captured,
            TestClient(app) as client,
        ):
            response = client.get("/api/health?token=must-not-appear")

        self.assertEqual(response.status_code, 200)
        message = "\n".join(captured.output)
        self.assertIn("path=/api/health", message)
        self.assertNotIn("must-not-appear", message)


if __name__ == "__main__":
    unittest.main()
```

- [ ] Executar e confirmar RED:

```powershell
docker compose run --rm --no-deps -w /app `
  -v "${PWD}\services\app\server:/app/server:ro" `
  --entrypoint python app -m unittest server.tests.test_request_timing -v
```

Expected: falha por ausência do header `server-timing` e/ou símbolo `SLOW_REQUEST_SECONDS`.

- [ ] Em `config.py`, adicionar configuração validada:

```python
SLOW_REQUEST_SECONDS = max(
    0.0,
    float(os.environ.get("MST_SLOW_REQUEST_SECONDS", "1.0")),
)
```

- [ ] Em `main.py`, importar `perf_counter`, `Request` e `SLOW_REQUEST_SECONDS`, e registrar o middleware antes dos routers:

```python
from time import perf_counter

from fastapi import FastAPI, Request

from .config import APP_NAME, APP_VERSION, SLOW_REQUEST_SECONDS, settings


@app.middleware("http")
async def request_timing(request: Request, call_next):
    started = perf_counter()
    response = await call_next(request)
    duration_ms = (perf_counter() - started) * 1000.0
    response.headers["Server-Timing"] = f"app;dur={duration_ms:.2f}"
    if duration_ms >= SLOW_REQUEST_SECONDS * 1000.0:
        log.warning(
            "slow_request method=%s path=%s status=%d duration_ms=%.2f",
            request.method,
            request.url.path,
            response.status_code,
            duration_ms,
        )
    return response
```

- [ ] Adicionar `MST_SLOW_REQUEST_SECONDS: "${MST_SLOW_REQUEST_SECONDS:-1.0}"` ao anchor `x-app-base.environment` em `compose.yml` e `MST_SLOW_REQUEST_SECONDS=1.0` em `.env.example`.
- [ ] Executar o teste novamente e confirmar GREEN.
- [ ] Executar `python -m unittest discover -s services/app/server/tests` no ambiente de testes do projeto e confirmar ausência de regressões.
- [ ] Checkpoint: registrar arquivos alterados e saída dos testes. Se Git estiver inicializado: `git add services/app/server/config.py services/app/server/main.py services/app/server/tests/test_request_timing.py compose.yml .env.example && git commit -m "perf: instrument slow API requests"`.

---

### Task 2: Substituir auditoria de PNG por validação do manifesto na listagem

**Files:**
- Modify: `services/app/server/pipeline_state.py`
- Modify: `services/app/server/tests/test_pipeline_state.py`
- Verify: `services/sam3-worker/sam3_runner/mask_io.py`
- Verify: `services/sam3-worker/sam3_runner/marker.py`
- Verify: `services/app/server/dataset.py`
- Verify: `services/app/server/review.py`
- Test: `services/app/server/tests/test_dataset_tasks.py`

**Interfaces:**
- Add: `ArtifactAudit(valid: bool, inconsistencies: tuple[str, ...])`.
- Add: `_manifest_audit(run: dict, obj_ids: tuple[int, ...], frame_count: int, segment_name: str) -> ArtifactAudit | None`.
- Add: `_legacy_mask_audit_cached(segment_text: str, width: int, height: int, obj_ids: tuple[int, ...], frame_count: int, run_signature: tuple[int, int], prompt_signature: tuple[int, int]) -> ArtifactAudit`, limitado por `@lru_cache(maxsize=2048)`.
- Preserve: `inspect_pipeline_entry(...) -> PipelineSnapshot`.

- [ ] Acrescentar a `test_pipeline_state.py` um teste que constrói duas máscaras reais e um manifesto com SHA-256 reais, depois proíbe qualquer `Path.read_bytes()` de PNG durante `inspect_pipeline_entry()`:

```python
def test_valid_manifest_does_not_reopen_masks(self) -> None:
    from hashlib import sha256
    from unittest.mock import patch

    from server.pipeline_state import inspect_pipeline_entry

    root = Path(tempfile.mkdtemp())
    segment = root / "video" / "seg_00"
    out = segment / "_sam3"
    masks = out / "masks" / "1"
    masks.mkdir(parents=True)
    (segment / "prompt.json").write_text(
        json.dumps(
            {
                "image_width": 8,
                "image_height": 6,
                "objects": [{"obj_id": 1, "label": "boom", "box_normalized": [0, 0, 1, 1]}],
            }
        ),
        encoding="utf-8",
    )
    checksums = {}
    for frame in range(2):
        path = masks / f"{frame:06d}.png"
        image = Image.new("1", (8, 6), 0)
        image.putpixel((frame + 1, 2), 1)
        image.save(path, format="PNG")
        checksums[f"masks/1/{frame:06d}.png"] = sha256(path.read_bytes()).hexdigest()
    run = {
        "status": "done",
        "frame_count": 2,
        "frames_written": 2,
        "artifacts": {"format": "png-1bit-v1", "files": 2, "empty": 0, "checksums": checksums},
    }
    (out / "run.json").write_text(json.dumps(run), encoding="utf-8")

    original = Path.read_bytes
    def reject_png(path: Path) -> bytes:
        if path.suffix.lower() == ".png":
            raise AssertionError(f"library reopened mask: {path}")
        return original(path)

    entry = {"status": "done", "export": {"root": str(root / "video"), "segments": [segment.name]}}
    with patch.object(Path, "read_bytes", reject_png):
        snapshot = inspect_pipeline_entry(entry, {"state": "done"}, root.parent)

    self.assertTrue(snapshot.artifacts_valid)
    self.assertEqual((snapshot.stage, snapshot.status), ("review", "waiting"))
```

- [ ] Adicionar testes separados que rejeitam: `files` diferente do esperado; checksums ausentes; nome inesperado; digest que não seja SHA-256 hexadecimal; formato desconhecido.
- [ ] Adicionar teste legada que omite `artifacts`, executa a inspeção duas vezes e verifica via mock que a segunda não relê os PNGs.
- [ ] Executar somente `server.tests.test_pipeline_state` e confirmar RED: o teste de fast path deve detectar a leitura atual em `pipeline_state.py`.
- [ ] Implementar a estrutura de resultado e a validação do manifesto:

```python
@dataclass(frozen=True)
class ArtifactAudit:
    valid: bool
    inconsistencies: tuple[str, ...] = ()


_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _manifest_audit(
    run: dict,
    obj_ids: tuple[int, ...],
    frame_count: int,
    segment_name: str,
) -> ArtifactAudit | None:
    artifacts = run.get("artifacts")
    if not isinstance(artifacts, dict) or not artifacts:
        return None
    if artifacts.get("format") != "png-1bit-v1":
        return ArtifactAudit(False, (f"{segment_name}: formato de mascara desconhecido",))
    checksums = artifacts.get("checksums")
    expected = frame_count * len(obj_ids)
    if artifacts.get("files") != expected or not isinstance(checksums, dict) or len(checksums) != expected:
        return ArtifactAudit(False, (f"{segment_name}: manifesto de mascaras incompleto",))
    for obj_id in obj_ids:
        for frame in range(frame_count):
            key = f"masks/{obj_id}/{frame:06d}.png"
            if not _SHA256.fullmatch(str(checksums.get(key, ""))):
                return ArtifactAudit(False, (f"{segment_name}: checksum ausente ou invalido em {key}",))
    empty = artifacts.get("empty", 0)
    if not isinstance(empty, int) or not 0 <= empty <= expected:
        return ArtifactAudit(False, (f"{segment_name}: contagem de mascaras vazias invalida",))
    return ArtifactAudit(True)
```

- [ ] Extrair o loop existente de leitura de PNGs para `_legacy_mask_audit_cached`. A chave do cache deve incluir `segment.resolve()`, `width`, `height`, `obj_ids`, `frame_count` e assinaturas de `run.json` e `prompt.json`:

```python
def _signature(path: Path) -> tuple[int, int]:
    stat = path.stat()
    return stat.st_mtime_ns, stat.st_size


@lru_cache(maxsize=2048)
def _legacy_mask_audit_cached(
    segment_text: str,
    width: int,
    height: int,
    obj_ids: tuple[int, ...],
    frame_count: int,
    run_signature: tuple[int, int],
    prompt_signature: tuple[int, int],
) -> ArtifactAudit:
    segment = Path(segment_text)
    out = segment / "_sam3"
    errors: list[str] = []
    for obj_id in obj_ids:
        for frame in range(frame_count):
            path = out / "masks" / str(obj_id) / f"{frame:06d}.png"
            if not path.is_file():
                errors.append(f"{segment.name}: mascara ausente obj {obj_id}, frame {frame}")
                continue
            try:
                inspect_binary_png(path.read_bytes(), expected_size=(width, height))
            except (OSError, MaskValidationError) as exc:
                errors.append(f"{segment.name}: mascara invalida obj {obj_id}, frame {frame}: {exc}")
    return ArtifactAudit(not errors, tuple(errors))
```

- [ ] Em `inspect_pipeline_entry`, validar todos os `obj_id` primeiro; chamar `_manifest_audit`; usar o fallback cacheado somente quando o retorno for `None`; acumular `audit.inconsistencies` sem reabrir máscaras no fast path.
- [ ] Não cachear `PipelineSnapshot` completo: `mask_review.json` muda durante a revisão e `reviewed_frames` precisa refletir a gravação imediatamente.
- [ ] Confirmar que `validate_mask_set()` continua produzindo `format`, `files`, `empty` e `checksums` somente depois de validar todas as dimensões/pixels.
- [ ] Em `services/app/server/tests/test_dataset_tasks.py`, adicionar este teste para provar que a otimização da listagem não enfraqueceu a exportação:

```python
def test_export_still_rejects_corrupt_png(self) -> None:
    mask = self.output / "video-a" / "seg_000" / "_sam3" / "masks" / "1" / "000000.png"
    mask.write_bytes(b"not-a-png")

    with self.assertRaises((OSError, ValueError)):
        self._export("coco", "segmentation")
```
- [ ] Executar:

```powershell
docker compose run --rm --no-deps -w /app `
  -v "${PWD}\services\app\server:/app/server:ro" `
  -v "${PWD}\packages\pipeline-core\src:/packages/pipeline-core/src:ro" `
  --entrypoint python app -m unittest server.tests.test_pipeline_state -v
```

Expected: todos os casos de manifesto e fallback passam; o teste não observa leitura de PNG no fast path.

- [ ] Executar os testes de exportação e confirmar que corrupção ainda bloqueia a geração.
- [ ] Checkpoint: registrar o número de PNGs lidos na primeira e segunda inspeção legada. Se Git estiver inicializado: `git add services/app/server/pipeline_state.py services/app/server/tests/test_pipeline_state.py services/app/server/tests/test_dataset_tasks.py && git commit -m "perf: trust validated SAM3 manifests in library"`.

---

### Task 3: Tirar toda a montagem da biblioteca do event loop

**Files:**
- Modify: `services/app/server/routers/library.py`
- Create: `services/app/server/tests/test_library_responsiveness.py`

**Interfaces:**
- Add: `_build_video_list(ctx: ObjectContext, search: str | None, status: str | None, sort: str) -> dict`.
- Preserve: `GET /api/objects/{object_id}/videos` response schema.

- [ ] Mover o corpo síncrono atual de `list_videos()` para `_build_video_list()` sem alterar filtros, ordenação, counts ou payload.
- [ ] Antes de alterar a rota, criar um teste de responsividade:

```python
from __future__ import annotations

import asyncio
import time
import unittest
from unittest.mock import patch

from server.routers.library import list_videos


class LibraryResponsivenessTests(unittest.IsolatedAsyncioTestCase):
    async def test_slow_build_does_not_block_event_loop(self) -> None:
        def slow_build(*_args, **_kwargs):
            time.sleep(0.15)
            return {"videos": []}

        with patch("server.routers.library._build_video_list", slow_build):
            request = asyncio.create_task(list_videos(ctx=object()))
            started = time.perf_counter()
            await asyncio.sleep(0.01)
            elapsed = time.perf_counter() - started
            result = await request

        self.assertLess(elapsed, 0.08)
        self.assertEqual(result, {"videos": []})


if __name__ == "__main__":
    unittest.main()
```

- [ ] Executar e confirmar RED: com chamada síncrona, o sleep de 10 ms só retorna após aproximadamente 150 ms.
- [ ] Implementar a rota mínima:

```python
@router.get("/videos")
async def list_videos(
    search: str | None = None,
    status: str | None = None,
    sort: str = "name",
    ctx: ObjectContext = Depends(get_object),
) -> dict:
    return await asyncio.to_thread(_build_video_list, ctx, search, status, sort)
```

- [ ] Manter `locks.map_for`, `sam3_queue.map_for`, `ctx.index.all`, `cached_probe`, `ctx.store.counts` e `inspect_pipeline_entry` dentro de `_build_video_list`, pois todos podem acessar filesystem ou psycopg síncrono.
- [ ] Garantir que `rescan()` continue chamando `ctx.rescan` em thread e depois `list_videos`; não envolver `asyncio.to_thread` dentro de outro thread.
- [ ] Executar o novo teste e todos os testes de `library`/`pipeline_state`.
- [ ] Fazer um teste manual de cold path: iniciar `GET /videos` e, durante a auditoria legada, chamar `/api/health`; a segunda chamada precisa responder em menos de 200 ms.
- [ ] Checkpoint. Se Git estiver inicializado: `git add services/app/server/routers/library.py services/app/server/tests/test_library_responsiveness.py && git commit -m "perf: move library inspection off event loop"`.

---

### Task 4: Coalescer refreshes e navegar antes da atualização global

**Files:**
- Modify: `services/app/web/src/store/library.ts`
- Modify: `services/app/web/src/views/AnnotatorView.tsx`
- Modify: `services/app/web/scripts/smoke.mjs`

**Interfaces:**
- Preserve: `refresh(): Promise<void>`.
- Change behavior: chamadas concorrentes de `refresh()` compartilham uma única requisição.
- Change behavior: `handleSaveExportNext` e `handleNoBoom` navegam usando `useLibrary.getState().nextPending(video.video_id)` antes de disparar `void refresh()`.

- [ ] Em `smoke.mjs`, adicionar `blockRefreshAfterAction: true` aos cenários `triage-submit-next` e `triage-no-object-next`.
- [ ] Criar estado de teste `blockVideoRefresh`; depois do clique, qualquer fetch de `/api/objects/{id}/videos` deve devolver uma Promise pendente. Aguardar no máximo 250 ms e manter as asserções de `expectPath` já existentes.
- [ ] Rodar os dois cenários e confirmar RED: a URL não muda porque o componente aguarda `refresh()`.
- [ ] Em `library.ts`, declarar fora da store:

```typescript
let refreshInFlight: Promise<void> | null = null;
```

- [ ] Substituir `refresh` por uma implementação single-flight. O estado `loading` deve ser ativado uma vez e limpo no `finally`; o campo deve ser zerado somente se ainda apontar para a mesma Promise:

```typescript
refresh: () => {
  if (refreshInFlight) return refreshInFlight;
  set({ loading: true, error: null });
  const operation = api
    .videos({ sort: get().sort })
    .then((data) => {
      set({
        videos: data.videos,
        counts: data.counts,
        pipelineCounts: data.pipeline_counts,
        pipelineStatusCounts: data.pipeline_status_counts,
        label: data.label,
      });
    })
    .catch((error: Error) => {
      set({ error: error.message });
    })
    .finally(() => {
      if (refreshInFlight === operation) refreshInFlight = null;
      set({ loading: false });
    });
  refreshInFlight = operation;
  return operation;
},
```

- [ ] Acrescentar ao smoke um contador e afirmar que dois mounts/refreshes simultâneos geram exatamente um request de listagem.
- [ ] Em `AnnotatorView.tsx`, não usar `await refresh()` para escolher o próximo. O fluxo de exportação deve ser:

```typescript
const next = useLibrary.getState().nextPending(video.video_id);
if (next) onNavigate(next);
else onBack();
void refresh();
```

- [ ] Manter `watchJob()` antes da navegação, para a conclusão do job sobreviver ao unmount e chamar `finishExport()`; seu refresh final também deve ser fire-and-forget.
- [ ] Em “Sem objeto”, aguardar apenas `markNoBoom()`; calcular o próximo a partir do snapshot local; navegar; então executar `void refresh()`.
- [ ] Não remover `await handleSave()` nem `await state.queueExport()`: esses dois passos garantem persistência e job durável antes da navegação.
- [ ] Executar:

```powershell
Set-Location services/app/web
npm run build
node scripts/smoke.mjs triage-submit-next
node scripts/smoke.mjs triage-no-object-next
node scripts/smoke.mjs triage-submit-last
```

Expected: TypeScript compila; os cenários navegam mesmo com refresh pendente; o caso sem próximo volta ao painel.

- [ ] Executar o smoke completo definido em `package.json`.
- [ ] Checkpoint. Se Git estiver inicializado: `git add services/app/web/src/store/library.ts services/app/web/src/views/AnnotatorView.tsx services/app/web/scripts/smoke.mjs && git commit -m "perf: decouple triage navigation from library refresh"`.

---

### Task 5: Limitar o paralelismo interno do FFmpeg

**Files:**
- Modify: `services/app/server/config.py`
- Modify: `services/app/server/ffmpeg.py`
- Create: `services/app/server/tests/test_ffmpeg_threads.py`
- Modify: `compose.yml`
- Modify: `.env.example`

**Interfaces:**
- Consumes: `MST_FFMPEG_THREADS`, default `4`, mínimo `1`.
- Produces: argv com orçamento explícito de threads para decoder e encoder.

- [ ] Escrever testes que façam mock de `server.ffmpeg.resolve()` e inspecionem `proxy_full_argv`, `proxy_window_argv`, `export_segment_argv` e `thumb_argv`.
- [ ] Cada comando deve conter `-threads 4` imediatamente antes de `-i` para limitar o decoder. Cada saída JPEG deve conter seu próprio `-threads 4` antes do caminho de saída.
- [ ] Executar o teste e confirmar RED: os builders atuais não possuem `-threads`.
- [ ] Adicionar a `config.py`:

```python
FFMPEG_THREADS = max(1, int(os.environ.get("MST_FFMPEG_THREADS", "4")))
```

- [ ] Em `ffmpeg.py`, importar `FFMPEG_THREADS` e adicionar helpers sem estado:

```python
def _decoder_threads() -> list[str]:
    return ["-threads", str(FFMPEG_THREADS)]


def _encoder_threads() -> list[str]:
    return ["-threads", str(FFMPEG_THREADS)]
```

- [ ] Inserir `*_decoder_threads()` antes de cada `-i`. Em `_dual_output_args`, inserir `*_encoder_threads()` antes de cada destino. Em export e thumbnail, inserir o encoder budget antes do destino.
- [ ] Não alterar `trim`, `-fps_mode passthrough`, `-frames:v`, `-start_number`, resolução ou qualidade; esses argumentos preservam a correspondência exata dos frames.
- [ ] Adicionar `MST_FFMPEG_THREADS: "${MST_FFMPEG_THREADS:-4}"` ao ambiente compartilhado do app/worker no Compose e `MST_FFMPEG_THREADS=4` a `.env.example`.
- [ ] Executar `test_ffmpeg_threads.py` e os testes existentes de proxy/exportação.
- [ ] Checkpoint. Se Git estiver inicializado: `git add services/app/server/config.py services/app/server/ffmpeg.py services/app/server/tests/test_ffmpeg_threads.py compose.yml .env.example && git commit -m "perf: bound ffmpeg thread usage"`.

---

### Task 6: Escalar somente o worker CPU

**Files:**
- Modify: `compose.yml`
- Modify: `.env.example`
- Modify: `tests/test_compose_security.py`
- Modify: `docs/REMOTE-MOVE.md`

**Interfaces:**
- Consumes: `CPU_WORKER_REPLICAS=2`, `CPU_WORKER_CPUS=4.0`, `CPU_WORKER_MEMORY=8G`.
- Guarantees: `sam3-worker.deploy.replicas == 1`.

- [ ] Em `tests/test_compose_security.py`, criar teste do YAML fonte:

```python
def test_cpu_workers_are_bounded_and_gpu_worker_stays_single(self) -> None:
    worker = self.services["worker"]
    self.assertEqual(worker["deploy"]["replicas"], "${CPU_WORKER_REPLICAS:-2}")
    self.assertEqual(
        worker["deploy"]["resources"]["limits"]["cpus"],
        "${CPU_WORKER_CPUS:-4.0}",
    )
    self.assertEqual(
        worker["deploy"]["resources"]["limits"]["memory"],
        "${CPU_WORKER_MEMORY:-8G}",
    )
    self.assertEqual(self.services["sam3-worker"]["deploy"]["replicas"], 1)
```

- [ ] Executar e confirmar RED: `worker.deploy` e `sam3-worker.deploy.replicas` ainda não existem.
- [ ] Em `compose.yml`, configurar:

```yaml
  worker:
    <<: *app-base
    command: [worker]
    deploy:
      replicas: ${CPU_WORKER_REPLICAS:-2}
      resources:
        limits:
          cpus: "${CPU_WORKER_CPUS:-4.0}"
          memory: "${CPU_WORKER_MEMORY:-8G}"
```

- [ ] Preservar os `depends_on` atuais do worker e acrescentar `replicas: 1` ao `deploy` já existente do `sam3-worker`, sem alterar a reserva NVIDIA.
- [ ] Adicionar os três valores a `.env.example` e documentar em `REMOTE-MOVE.md` que `docker compose up -d` sobe duas réplicas CPU automaticamente.
- [ ] Documentar que `docker compose up --scale worker=N` não deve ser combinado com `deploy.replicas`; para ajuste permanente deve-se editar `CPU_WORKER_REPLICAS`.
- [ ] Executar:

```powershell
docker compose config --quiet
docker compose config --format json
python -m unittest tests.test_compose_security -v
```

Expected no JSON resolvido: `worker.deploy.replicas` é `2`, limite CPU `4.0`, memória `8G`; `sam3-worker.deploy.replicas` é `1`.

- [ ] Confirmar que não foi adicionado `ports` a worker, Postgres, MinIO ou SAM3 e que secrets continuam por arquivos.
- [ ] Checkpoint. Se Git estiver inicializado: `git add compose.yml .env.example tests/test_compose_security.py docs/REMOTE-MOVE.md && git commit -m "ops: run two bounded CPU workers"`.

---

### Task 7: Provar concorrência da fila e preservar exclusão mútua da GPU

**Files:**
- Create: `tests/test_job_queue_postgres.py`
- Verify: `tests/test_job_queue_contract.py`
- Verify: `packages/pipeline-core/src/pipeline_core/jobs.py`
- Verify: `services/app/server/sam3_postgres.py`
- Verify: `services/app/worker.py`
- Verify: `services/sam3-worker/sam3_runner/queue_client.py`

**Interfaces:**
- CPU workers claimam jobs com locks de linha distintos via `FOR UPDATE SKIP LOCKED`.
- GPU continua com uma única réplica e uma fila ordenada por prioridade.

- [ ] Manter os testes estáticos existentes para `FOR UPDATE SKIP LOCKED`, lease expirado e prioridade.
- [ ] Criar `tests/test_job_queue_postgres.py`. O setup pode inserir fixtures diretamente, mas os claims obrigatoriamente usam `PostgresJobQueue.claim()`:

```python
from __future__ import annotations

import json
import os
import unittest
from pathlib import Path
from urllib.parse import quote

import psycopg

from pipeline_core.jobs import PostgresJobQueue


def database_url() -> str:
    password = Path(os.environ["POSTGRES_PASSWORD_FILE"]).read_text(encoding="utf-8").strip()
    user = os.environ.get("POSTGRES_USER", "pipeline")
    database = os.environ.get("POSTGRES_DB", "pipeline")
    host = os.environ.get("POSTGRES_HOST", "postgres")
    return f"postgresql://{quote(user)}:{quote(password)}@{host}:5432/{quote(database)}"


class PostgresJobConcurrencyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.url = database_url()
        self.ids: list[str] = []
        with psycopg.connect(self.url) as connection:
            for suffix in ("a", "b"):
                row = connection.execute(
                    """
                    INSERT INTO jobs(kind, worker_kind, state, priority, payload, progress)
                    VALUES ('video_export', 'cpu-test', 'queued', 50, %s::jsonb, '{}'::jsonb)
                    RETURNING id::text
                    """,
                    (json.dumps({"test": f"cpu-concurrency-{suffix}"}),),
                ).fetchone()
                self.ids.append(row[0])

    def tearDown(self) -> None:
        with psycopg.connect(self.url) as connection:
            connection.execute("DELETE FROM jobs WHERE id::text = ANY(%s)", (self.ids,))

    def test_two_workers_claim_distinct_jobs(self) -> None:
        queue = PostgresJobQueue(lambda: psycopg.connect(self.url))

        first = queue.claim(worker_id="cpu-test-1", worker_kind="cpu-test", lease_seconds=180)
        second = queue.claim(worker_id="cpu-test-2", worker_kind="cpu-test", lease_seconds=180)
        third = queue.claim(worker_id="cpu-test-3", worker_kind="cpu-test", lease_seconds=180)

        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        self.assertNotEqual(first["id"], second["id"])
        self.assertNotEqual(first["lease_token"], second["lease_token"])
        self.assertEqual({first["id"], second["id"]}, set(self.ids))
        self.assertIsNone(third)


if __name__ == "__main__":
    unittest.main()
```

- [ ] Usar `worker_kind='cpu-test'` exclusivamente nas fixtures para não disputar com jobs reais nem exigir a parada dos workers em execução.
- [ ] Não adicionar paralelismo ao loop `sam3_runner.queue_client`; o teste de Compose da Task 6 é a garantia operacional de instância única.
- [ ] Executar com Postgres saudável:

```powershell
docker compose up -d postgres
docker compose run --rm --no-deps `
  -v "${PWD}\tests:/app/tests:ro" `
  --entrypoint python app -m unittest discover -s /app/tests -p test_job_queue_postgres.py -v
```

Expected: os dois workers recebem jobs diferentes sem duplicação.

- [ ] Inspecionar os payloads e confirmar que `object_id`, `video_id` e contexto fresco são resolvidos por job; preservar o teste `services/app/server/tests/test_worker_context.py`.
- [ ] Executar também `python -m unittest tests.test_job_queue_contract -v` pelo `scripts/verify.ps1` para preservar as garantias estáticas.
- [ ] Checkpoint. Se Git estiver inicializado: `git add tests/test_job_queue_postgres.py && git commit -m "test: prove concurrent CPU job claims"`.

---

### Task 8: Verificação integrada, benchmark e instruções de rollout

**Files:**
- Modify: `docs/REMOTE-MOVE.md`
- Create: `docs/PERFORMANCE.md`
- Verify: all files changed above

**Interfaces and acceptance thresholds:**
- Warm library p95: `<= 500 ms` no acervo atual de 416 vídeos.
- Health during cold legacy audit: `< 200 ms`.
- Navigation after successful mutation/job enqueue: `< 500 ms`, independent of `/videos` refresh.
- CPU worker replicas running: `2`.
- SAM3 worker replicas running: `1`.

- [ ] Criar `docs/PERFORMANCE.md` com os comandos abaixo, tabela de resultados antes/depois e procedimento de rollback. Não preencher resultados sem medi-los.
- [ ] Executar a suíte Python do app usando os sources locais montados:

```powershell
docker compose run --rm --no-deps -w /app `
  -v "${PWD}\services\app\server:/app/server:ro" `
  -v "${PWD}\services\app\worker.py:/app/worker.py:ro" `
  -v "${PWD}\services\app\entrypoint.py:/app/entrypoint.py:ro" `
  -v "${PWD}\packages\pipeline-core\src:/packages/pipeline-core/src:ro" `
  --entrypoint python app -m unittest discover -s server/tests -v
```

- [ ] Executar a verificação de repositório:

```powershell
.\scripts\verify.ps1
```

- [ ] Executar frontend:

```powershell
Set-Location services/app/web
npm ci
npm run build
npm run smoke
Set-Location ../../..
```

- [ ] Validar o Compose e construir apenas as imagens alteradas:

```powershell
docker compose config --quiet
docker compose build app worker
```

- [ ] Antes de recriar serviços, fazer backup conforme o procedimento existente. A mudança não exige migração de banco ou dados.
- [ ] Subir a versão:

```powershell
docker compose up -d --force-recreate app worker sam3-worker
docker compose ps
```

- [ ] Confirmar em `docker compose ps` dois containers `worker`, um `sam3-worker`, um `app` healthy, Postgres e MinIO preservados.
- [ ] Acompanhar logs sem expor secrets:

```powershell
docker compose logs --tail 200 app worker sam3-worker
```

- [ ] Medir 30 chamadas quentes de `/api/objects/boom/videos` e registrar p50/p95. Usar `Server-Timing`, não apenas tempo do Cloudflare, para separar backend de rede/túnel.
- [ ] Durante a primeira auditoria de um run legado, medir `/api/health`; confirmar `< 200 ms`.
- [ ] No browser, bloquear artificialmente `/videos` após o POST e confirmar que os dois botões avançam em `< 500 ms`.
- [ ] Enfileirar dois exports CPU de vídeos diferentes; confirmar dois jobs `leased/running` com worker IDs diferentes e ausência de job duplicado.
- [ ] Enfileirar dois jobs SAM3; confirmar um executando e o outro em fila, com heartbeat/progresso visível.
- [ ] Exportar uma amostra YOLO/COCO de detecção e segmentação; confirmar que a exportação ainda valida máscaras e checksums.
- [ ] Procedimento de rollback operacional: definir `CPU_WORKER_REPLICAS=1`, executar `docker compose up -d --force-recreate worker` e investigar contenção. Não reverter o fast path de manifesto sem evidência de inconsistência; executar auditoria/export explícito para verificar dados.
- [ ] Fazer scan por placeholders e alterações acidentais:

```powershell
rg -n "TODO|FIXME|pass$|NotImplementedError|must-not-appear" `
  services/app/server services/app/web/src tests compose.yml .env.example
```

Expected: nenhum placeholder novo; `must-not-appear` só pode existir no teste de sanitização de log.

- [ ] Revisar tipos/contratos: `PipelineSnapshot` inalterado; resposta `/videos` inalterada; `refresh(): Promise<void>` inalterado; envs documentadas; manifesto compatível com `validate_mask_set()`.
- [ ] Registrar no relatório final comandos, resultados, p50/p95, número de réplicas, riscos restantes e caminhos dos arquivos. Se Git estiver inicializado: `git add docs/PERFORMANCE.md docs/REMOTE-MOVE.md && git commit -m "docs: add performance rollout runbook"`.

---

## Agentic Execution Order

1. Executar Tasks 1–3 em sequência, porque a instrumentação mede a otimização e a Task 3 depende do helper da Task 2.
2. Após a Task 3 verde, Tasks 4 e 5 podem ser executadas em paralelo por agentes diferentes: os arquivos compartilhados `config.py`, `compose.yml` e `.env.example` pertencem à Task 5; a Task 4 deve tocar somente frontend.
3. Executar Task 6 depois da Task 5 para evitar conflito em `compose.yml` e `.env.example`.
4. Task 7 pode rodar em paralelo com Task 4, desde que o PostgreSQL de testes seja isolado.
5. Task 8 é obrigatoriamente a última e só começa quando todos os testes focados estiverem verdes.

## Agent Handoff Contract

Cada agente deve devolver:

- arquivos alterados;
- teste escrito antes da implementação e motivo da falha RED;
- comando exato e saída resumida do GREEN;
- qualquer desvio do plano, com justificativa técnica;
- riscos ou trabalho restante;
- confirmação explícita de que não alterou dados do workspace.

O agente integrador deve reler todos os diffs, resolver conflitos preservando os invariantes globais e executar a Task 8 completa. Nenhum agente pode declarar conclusão usando apenas testes isolados ou análise estática.
