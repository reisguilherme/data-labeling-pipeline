# Operations UX Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reorganizar a aplicação em áreas operacionais de Triagem, SAM3, Revisão, Concluídos, Objetos e Exportação global multiclasse, sem regressão dos editores atuais.

**Architecture:** O FastAPI calcula o estado autoritativo do pipeline a partir dos metadados existentes, runs SAM3 e `mask_review.json`; o React consome esse contrato em um shell persistente com rotas por URL. Gestão de objetos reutiliza o registro atual e adiciona operações seguras; exportação global refatora o exportador atual para operar sobre candidatos com namespace de objeto e mapa de classes determinístico.

**Tech Stack:** Python 3.12, FastAPI, PostgreSQL 16, MinIO, React 19, TypeScript, Zustand, Tailwind CSS 4, Vite e Docker Compose.

**Spec:** `docs/superpowers/specs/2026-09-01-operations-ux-redesign-design.md`

## Global Constraints

- Máscaras continuam sendo a fonte canônica e bboxes continuam derivadas.
- `completed` exige todos os frames do run SAM3 efetivo revisados como `ok` ou `edited` e artefatos válidos.
- Vídeos `no_boom` são `discarded`, nunca `completed`.
- `object_id`, raízes e chaves históricas permanecem imutáveis.
- Apenas `127.0.0.1:8000` permanece publicado.
- Endpoints e editores existentes permanecem funcionais durante a migração.
- Splits continuam determinísticos e agrupados por `{object_id, video_id}`.
- Esta pasta não possui repositório Git; cada tarefa termina com testes e registro no plano, sem commits.

---

### Task 1: Classificação autoritativa do pipeline

**Files:**
- Create: `services/app/server/pipeline_state.py`
- Create: `services/app/server/tests/test_pipeline_state.py`
- Modify: `services/app/server/routers/library.py`
- Modify: `services/app/web/src/api/types.ts`

**Interfaces:**
- Consumes: `AnnotationStore`, `Sam3Queue`, `_sam3/run.json`, `_sam3/masks/*` e `_sam3/mask_review.json`.
- Produces: `classify_pipeline(annotation_status: str, sam3_state: str | None, expected_frames: int, reviewed_frames: int, artifacts_valid: bool) -> PipelineSnapshot` e campos `pipeline_stage`, `stage_status`, `stage_progress` em cada `VideoListItem`.

- [ ] **Step 1: Write the failing domain tests**

```python
def test_only_fully_reviewed_sam3_video_is_completed():
    assert classify_pipeline("done", "done", 7, 7, True).stage == "completed"
    assert classify_pipeline("done", "done", 7, 6, True).stage == "review"

def test_triaged_and_discarded_are_not_completed():
    assert classify_pipeline("done", None, 0, 0, False).stage == "sam3"
    assert classify_pipeline("no_boom", None, 0, 0, False).stage == "discarded"
```

- [ ] **Step 2: Run RED**

Run: `python -m unittest services.app.server.tests.test_pipeline_state -v`  
Expected: FAIL because `server.pipeline_state` does not exist.

- [ ] **Step 3: Implement the pure classifier and filesystem inspector**

```python
@dataclass(frozen=True)
class PipelineSnapshot:
    stage: Literal["triage", "sam3", "review", "completed", "discarded"]
    status: str
    expected_frames: int = 0
    reviewed_frames: int = 0
    edited_frames: int = 0
    artifacts_valid: bool = False

def classify_pipeline(annotation_status, sam3_state, expected, reviewed, valid):
    if annotation_status == "no_boom": return PipelineSnapshot("discarded", "discarded")
    if annotation_status in {"pending", "in_progress"}: return PipelineSnapshot("triage", annotation_status)
    if sam3_state != "done" or not valid: return PipelineSnapshot("sam3", sam3_state or "ready")
    if expected > 0 and reviewed >= expected: return PipelineSnapshot("completed", "validated", expected, reviewed, artifacts_valid=True)
    return PipelineSnapshot("review", "in_progress" if reviewed else "waiting", expected, reviewed, artifacts_valid=True)
```

`inspect_pipeline(ctx, entry, sam3)` soma `frame_count` dos marcadores válidos, valida a existência/dimensões das máscaras via `FileMaskReviewStore.get_frame()` e conta somente entradas `ok|edited` de `mask_review.json`.

- [ ] **Step 4: Attach snapshots to the existing video response**

`video_payload()` inclui:

```python
"pipeline_stage": snapshot.stage,
"stage_status": snapshot.status,
"stage_progress": snapshot.public_progress(),
```

`list_videos()` adiciona `pipeline_counts` por stage/status sem remover `counts` legado.

- [ ] **Step 5: Run GREEN and regression suite**

Run: `python -m unittest services.app.server.tests.test_pipeline_state -v`  
Run: `python -m unittest discover -s services/app/server/tests -p 'test_*.py' -q`  
Expected: all PASS.

### Task 2: Rotas locais e shell operacional React

**Files:**
- Create: `services/app/web/src/lib/routes.ts`
- Create: `services/app/web/src/components/AppShell.tsx`
- Create: `services/app/web/src/components/PipelineSidebar.tsx`
- Create: `services/app/web/src/components/PipelineVideoCard.tsx`
- Create: `services/app/web/src/views/OperationsView.tsx`
- Modify: `services/app/web/src/App.tsx`
- Modify: `services/app/web/src/api/types.ts`
- Modify: `services/app/web/src/index.css`
- Modify: `services/app/web/scripts/smoke.mjs`

**Interfaces:**
- Consumes: novos campos de `VideoListItem` e handlers existentes de Annotator/SAM3/Review.
- Produces: `parseRoute(pathname)`, `navigate(route)`, `AppShell` persistente e boards por etapa.

- [ ] **Step 1: Add failing route and operations smoke scenarios**

O cenário `operations` abre `/objects/boom/review` e exige os textos “Revisão de máscaras”, “Aguardando” e “Em andamento”. O cenário `completed-gate` fornece um vídeo triado sem revisão e um validado, e exige que somente o validado apareça em Concluídos.

- [ ] **Step 2: Run RED**

Run: `npm run build` in `services/app/web`  
Expected: smoke FAIL because the new routes and headings are absent.

- [ ] **Step 3: Implement URL parsing without adding a routing dependency**

```ts
export type Route =
  | { page: "operations"; objectId: string; stage: PipelineStage }
  | { page: "editor"; objectId: string; videoId: string; editor: "triage"|"sam3"|"review" }
  | { page: "export" }
  | { page: "objects" };

export function navigate(path: string): void {
  history.pushState({}, "", path);
  dispatchEvent(new PopStateEvent("popstate"));
}
```

- [ ] **Step 4: Implement shell, sidebar and stage boards**

`OperationsView` filtra `videos` por `pipeline_stage`, oferece filtros de `stage_status`, busca, ordenação e a ação primária da etapa. Cards têm botões sempre visíveis e mostram locks/progresso/erro. O shell recolhe a sidebar em editores.

- [ ] **Step 5: Keep legacy editor behaviors behind stable URLs**

`App.tsx` resolve a URL e monta `AnnotatorView`, `Sam3View` ou `ReviewView` dentro de `AppShell`, traduzindo `onBack` e `onReview` para `navigate()`.

- [ ] **Step 6: Run GREEN**

Run: `npm run build` in `services/app/web`  
Expected: TypeScript, Vite and all smoke scenarios PASS.

### Task 3: Visão geral e atualização incremental de estados

**Files:**
- Create: `services/app/web/src/views/OverviewView.tsx`
- Modify: `services/app/web/src/store/library.ts`
- Modify: `services/app/web/src/components/AppShell.tsx`
- Modify: `services/app/web/scripts/smoke.mjs`

**Interfaces:**
- Consumes: `pipeline_counts`, `refresh()` e `refreshSam3()`.
- Produces: cards de resumo e `nextOperationalVideo(userId)`.

- [ ] **Step 1: Add a failing overview smoke scenario**

O mock contém 2 triage, 3 sam3, 4 review, 5 completed e 9 discarded. O DOM deve exibir `2`, `3`, `4`, `5` nos cards e não somar `discarded` a Concluídos.

- [ ] **Step 2: Run RED**

Run: `npm run smoke -- overview`  
Expected: FAIL because `OverviewView` is absent.

- [ ] **Step 3: Implement overview and polling**

O polling atual atualiza SAM3 a cada cinco segundos enquanto houver job ativo; ao mudar um job para `done`, chama `refresh()` uma vez para recalcular etapa/revisão. Salvar revisão também chama `refresh()` antes de voltar ao board.

- [ ] **Step 4: Run GREEN**

Run: `npm run build`  
Expected: all smoke scenarios PASS.

### Task 4: Gestão editável, arquivamento e exclusão protegida de objetos

**Files:**
- Create: `services/app/server/object_lifecycle.py`
- Create: `services/app/server/tests/test_object_lifecycle.py`
- Modify: `services/app/server/workspace.py`
- Modify: `services/app/server/routers/objects.py`
- Modify: `services/app/worker.py`
- Modify: `services/app/web/src/api/client.ts`
- Modify: `services/app/web/src/store/session.ts`
- Rewrite: `services/app/web/src/views/ObjectsView.tsx`
- Modify: `services/app/web/scripts/smoke.mjs`

**Interfaces:**
- Consumes: `Workspace.update`, locks, durable jobs, MinIO prefixes and managed roots.
- Produces: archive/restore/purge APIs, `Workspace.remove_registration()` and object editor UI.

- [ ] **Step 1: Write failing lifecycle tests**

```python
def test_confirmation_must_equal_excluir_object_id(self):
    with self.assertRaisesRegex(ValueError, "confirmação"):
        validate_purge_confirmation("boom", "EXCLUIR microfone")

def test_external_roots_are_never_managed(self):
    managed, skipped = partition_managed_paths(
        Path("C:/workspace"),
        [Path("C:/workspace/boom/dataset"), Path("D:/acervo/boom")],
    )
    self.assertEqual(managed, [Path("C:/workspace/boom/dataset")])
    self.assertEqual(skipped, [Path("D:/acervo/boom")])

def test_duplicate_active_class_label_is_rejected(self):
    with self.assertRaisesRegex(ValueError, "classe já usada"):
        validate_unique_label(
            [("boom", "boom", False), ("microfone", "microphone", False)],
            object_id="microfone",
            label="BOOM",
        )
```

- [ ] **Step 2: Run RED**

Run: `python -m unittest services.app.server.tests.test_object_lifecycle -v`  
Expected: FAIL because lifecycle service/endpoints do not exist.

- [ ] **Step 3: Implement safe lifecycle service**

```python
def normalize_label(value: str) -> str: return value.strip().casefold()
def validate_purge_confirmation(object_id: str, confirmation: str) -> None:
    if confirmation != f"EXCLUIR {object_id}":
        raise ValueError("confirmação de exclusão inválida")

def validate_unique_label(items, *, object_id: str, label: str) -> None:
    wanted = normalize_label(label)
    if not wanted:
        raise ValueError("nome da classe é obrigatório")
    for current_id, current_label, archived in items:
        if current_id != object_id and not archived and normalize_label(current_label) == wanted:
            raise ValueError(f"classe já usada pelo objeto {current_id}")
```

Only resolved roots beneath `workspace.root` enter `managed_paths`; external paths are returned in `skipped_paths` and never deleted.

- [ ] **Step 4: Add API operations and worker job**

- `GET /api/objects?include_archived=true`
- `POST /api/objects/{id}/archive`
- `POST /api/objects/{id}/restore`
- `POST /api/objects/{id}/purge` returns a durable `object_purge` job

The worker writes the inventory/checksums, removes object-prefixed MinIO objects, deletes managed paths, removes DB rows transactionally, then removes the registry entry. Exported datasets remain.

- [ ] **Step 5: Add failing object-management smoke and implement UI**

The smoke asserts edit fields, active/archived filters, restore action, exact purge confirmation and disabled destructive action before confirmation.

- [ ] **Step 6: Run GREEN**

Run backend lifecycle and full backend tests, then `npm run build`.  
Expected: all PASS.

### Task 5: Núcleo de exportação global multiclasse

**Files:**
- Create: `services/app/server/multiclass_dataset.py`
- Create: `services/app/server/tests/test_multiclass_dataset.py`
- Modify: `services/app/server/dataset.py`
- Modify: `services/app/server/routers/dataset.py`
- Modify: `services/app/server/main.py`
- Modify: `services/app/worker.py`

**Interfaces:**
- Consumes: `dataset.collect`, mask exporters and contexts selected by object ID.
- Produces: `preview_multiclass(contexts: list[ObjectContext], selections: list[VideoSelection], filters: Filters, task: str) -> dict`, `export_multiclass(contexts: list[ObjectContext], selections: list[VideoSelection], filters: Filters, *, out_dir: Path, fmt: str, task: str, val_fraction: float, test_fraction: float, on_progress=None) -> dict` and global `/api/datasets/*` endpoints.

- [ ] **Step 1: Write failing deterministic-class and collision tests**

```python
def test_class_ids_sort_by_object_id():
    assert class_map([("microfone", "microphone"), ("boom", "boom")]) == {"boom": 0, "microphone": 1}

def test_namespaced_stem_prevents_same_video_name_collision():
    assert namespaced_stem("boom", "abc", "seg_00", 1) != namespaced_stem(
        "microfone", "abc", "seg_00", 1
    )

def test_split_key_contains_object_id_and_video_id():
    assert split_key("boom", "abc") == "boom:abc"
    assert split_key("microfone", "abc") == "microfone:abc"
```

- [ ] **Step 2: Run RED**

Run: `python -m unittest services.app.server.tests.test_multiclass_dataset -v`  
Expected: FAIL because the module is absent.

- [ ] **Step 3: Extract an object-agnostic export core**

Extend `Candidate` with `object_id` and `class_label`. Introduce:

```python
def export_candidates(candidates, class_names, *, out_dir, fmt, task,
                      val_fraction, test_fraction, filters_manifest,
                      workspace_root, on_progress=None) -> dict:
    if not candidates:
        raise ValueError("nenhum segmento casa com o filtro")
    class_index = {name: index for index, name in enumerate(class_names)}
    split_by_video = {
        split_key(item.object_id, item.video_id): split_for(
            split_key(item.object_id, item.video_id), val_fraction, test_fraction
        )
        for item in candidates
    }
    return write_dataset_artifacts(
        candidates,
        class_index=class_index,
        split_by_video=split_by_video,
        out_dir=out_dir,
        fmt=fmt,
        task=task,
        filters_manifest=filters_manifest,
        workspace_root=workspace_root,
        on_progress=on_progress,
    )
```

`write_dataset_artifacts` recebe toda a lógica de escrita hoje contida no corpo de `dataset.export`; a função pública existente `export` coleta um contexto e chama `export_candidates`, preservando a API legada.

- [ ] **Step 4: Implement global preview/export contracts**

Payload video selection uses `{object_id, video_id}`. All contexts force `reviewed_only=True` and are filtered by `pipeline_stage == "completed"`. Output lives at `/workspace/_datasets/<name>` and MinIO `datasets/global/<name>`.

- [ ] **Step 5: Add worker support**

`worker.py` handles `dataset_export_global`, reloads all requested contexts, runs `export_multiclass`, reports progress and mirrors output to the global prefix.

- [ ] **Step 6: Run GREEN and single-object regressions**

Run multiclass, dataset export, segmentation export and full backend tests.  
Expected: all PASS.

### Task 6: Construtor global de dataset no frontend

**Files:**
- Create: `services/app/web/src/views/GlobalDatasetView.tsx`
- Create: `services/app/web/src/components/ObjectClassSelector.tsx`
- Create: `services/app/web/src/components/VideoMultiSelector.tsx`
- Modify: `services/app/web/src/api/client.ts`
- Modify: `services/app/web/src/api/types.ts`
- Modify: `services/app/web/src/App.tsx`
- Modify: `services/app/web/scripts/smoke.mjs`

**Interfaces:**
- Consumes: `/api/datasets/preview`, `/api/datasets/export`, `/api/datasets` and all active objects.
- Produces: global export workflow with mandatory preview and job monitoring.

- [ ] **Step 1: Add failing global-export smoke**

Assert selection of two classes, deterministic IDs 0/1, filters by flags/videos, locked “somente concluídos”, preview totals and disabled export when `export_allowed=false`.

- [ ] **Step 2: Run RED**

Run: `npm run smoke -- global-export`  
Expected: FAIL because the new page is absent.

- [ ] **Step 3: Implement selectors and preview state**

```ts
type SelectedVideo = { object_id: string; video_id: string };
type GlobalFilters = { flags: Record<string,string[]>; videos: SelectedVideo[]; include_empty: boolean };
```

Every object/filter/task change aborts stale preview state and requests a new preview. Export is enabled only for nonzero frames and `export_allowed=true`.

- [ ] **Step 4: Implement job/history interactions**

Reuse `watchJob`; on done, refresh global datasets. Historical per-object datasets are shown with an `Escopo` column and are not mutated by class rename/deletion.

- [ ] **Step 5: Run GREEN**

Run: `npm run build`  
Expected: all TypeScript and smoke scenarios PASS.

### Task 7: Integração, acessibilidade, documentação e deploy

**Files:**
- Modify: `services/app/web/src/index.css`
- Modify: `services/app/web/scripts/smoke.mjs`
- Modify: `README.md`
- Modify: `tests/test_repository_layout.py` if the new route files require layout assertions

**Interfaces:**
- Consumes: all earlier tasks.
- Produces: verified production images and updated operator documentation.

- [ ] **Step 1: Add integrated E2E smoke fixtures**

The fixture transitions a video through triage → sam3 → review → completed and verifies the board changes; a second object enters global export with stable class mapping.

- [ ] **Step 2: Verify keyboard and semantic behavior**

All card actions are `<button>`, board headings follow hierarchy, focus is visible, dialogs trap/restore focus, colors are paired with text labels, and no primary action exists only on hover.

- [ ] **Step 3: Update README operator flow**

Document the new sidebar, completion rule, object lifecycle and global multiclasse export, including the permanent-delete safety boundary.

- [ ] **Step 4: Run full local verification**

Run all  backend tests with the existing `PYTHONPATH`; run `npm run build`; run `docker compose config --quiet`.  
Expected: zero failures.

- [ ] **Step 5: Build and recreate services**

Run: `docker compose build app worker`  
Run: `docker compose up -d --force-recreate app worker`  
Expected: app healthy, worker stable, PostgreSQL/MinIO/SAM3 worker remain available.

- [ ] **Step 6: Run live acceptance checks**

Verify `/api/health`, pipeline counts, the existing completed SAM3 run, UI routes, one mask review fetch and a non-writing global preview. Confirm only `127.0.0.1:8000` is published.

---

## Execution Notes

Tasks 1–3 form the first independently deployable checkpoint: correct semantics and redesigned production boards. Task 4 is a separate destructive-lifecycle checkpoint. Tasks 5–6 form the global export checkpoint. Task 7 is the deployment gate. Because this workspace has no `.git`, rollback relies on the existing files/backups and Docker images rather than commits; no destructive purge acceptance test targets real user data.
