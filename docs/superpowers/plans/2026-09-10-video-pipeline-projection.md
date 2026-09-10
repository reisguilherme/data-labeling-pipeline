# Video Pipeline Projection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a recoverable PostgreSQL projection for each video's pipeline state so library reads remain fast without allowing stale derived data to mark work complete.

**Architecture:** Files, immutable SAM3 manifests, and mask-review manifests remain authoritative. Every source mutation first reserves a durable monotonic projection event, publishes its canonical file state, then conditionally applies a derived snapshot; failed applies stay pending and a metadata-only reconciler repairs them. Library reads may use a projection only when its source identity matches, and otherwise fall back to the existing metadata inspector with an explicit stale indicator.

**Tech Stack:** Python 3.12, FastAPI, psycopg 3, PostgreSQL 16, existing JSON annotation store, immutable SAM3 generations, unittest.

**Spec:** `docs/superpowers/specs/2026-09-08-reliability-redesign-design.md`

## Global Constraints

- Preservar integralmente vídeos, frames, máscaras, revisões, intervalos, decisões, exports e configurações existentes.
- Versionar código, testes, migrações, configuração de exemplo e documentação; excluir dados reais, modelos, backups e segredos do Git.
- Máscaras PNG binárias e revisões imutáveis continuam canônicas; a projeção é derivada e reconstruível.
- `GET /api/objects/{object_id}/videos` não abre nem decodifica PNGs, inclusive na primeira consulta de runs legados.
- Uma projeção ausente ou incompatível não vira evidência de conclusão.
- Usar upsert condicional por revisão monotônica para um evento antigo não sobrescrever um novo.
- Reconciliação lê metadados e não reprocessa mídia.
- PostgreSQL, MinIO, configuração e cache de modelos mantêm os volumes persistentes existentes.

---

### Task 1: Durable projection schema and conditional store

**Files:**
- Create: `migrations/005_video_pipeline_projection.sql`
- Create: `services/app/server/pipeline_projection.py`
- Create: `services/app/server/tests/test_pipeline_projection.py`
- Test: `services/app/server/tests/test_pipeline_projection_postgres.py`

**Interfaces:**
- Produces: `ProjectionIntent`, `ProjectionRecord`, `reserve_intent(...)`, `apply_intent(...)`, `fail_intent(...)`, `get_many(...)`, and `pending_intents(...)`.
- The database allocates `event_seq BIGSERIAL`; callers never manufacture ordering from timestamps or per-frame revisions.
- `apply_intent` updates `(object_id, video_id)` only when `event_seq` is greater than the currently applied sequence.

- [x] **Step 1: Write failing unit tests for serialization, disabled-database behavior, and sanitized errors.**
- [x] **Step 2: Run `python -m unittest server.tests.test_pipeline_projection -v` and verify RED for the absent module.**
- [x] **Step 3: Write a real-PostgreSQL test that reserves two events and applies them in reverse order; assert the newer event wins and the older becomes superseded.**
- [x] **Step 4: Add tests for idempotent reservation by `(object_id, video_id, event_kind, source_identity)` and retry of a pending event.**
- [x] **Step 5: Add migration 005 with `video_pipeline_projection_events` and `video_pipeline_projection`, JSONB source identity/snapshot, status checks, unique idempotency key, and lookup indexes.**
- [x] **Step 6: Implement the focused store with one transaction per public operation, finite connection timeout, and no global connection shared between worker processes.**
- [x] **Step 7: Run unit and PostgreSQL tests and verify GREEN.**
- [x] **Step 8: Commit as `feat: add durable video pipeline projection store`.**

### Task 2: Canonical metadata identity and reconciliation

**Files:**
- Modify: `services/app/server/pipeline_state.py`
- Modify: `services/app/server/review.py`
- Create: `services/app/server/pipeline_reconcile.py`
- Create: `services/app/server/tests/test_pipeline_reconcile.py`

**Interfaces:**
- Produces: `derive_pipeline_source(entry, sam3, output_root) -> PipelineSource` and `reconcile_video(ctx, video_id, *, intent=None) -> ProjectionRecord`.
- `PipelineSource.identity` includes annotation revision/status/export identity, active SAM3 generation ID plus manifest SHA-256, and canonical mask-review manifest identity for every segment.
- Derivation may read bounded JSON/control files, but never PNG bytes or enumerate mask directories.

- [ ] **Step 1: Write failing tests proving source identity changes after annotation revision, SAM3 pointer swap, or review-manifest revision, but ignores mtimes alone.**
- [ ] **Step 2: Add tests proving malformed/legacy metadata produces `validation_status=audit_required` and cannot create a completed projection.**
- [ ] **Step 3: Run the focused tests and verify RED.**
- [ ] **Step 4: Extract deterministic source identity and snapshot serialization around `PipelineSnapshot`; hash canonical JSON, never wall-clock fields.**
- [ ] **Step 5: Implement reconciliation that reloads the object context and canonical entry, derives the metadata-only snapshot, and applies/reserves one idempotent event.**
- [ ] **Step 6: Test crash recovery for a pending event, absent video, archived object, and repeated reconciliation.**
- [ ] **Step 7: Run focused and existing pipeline-state tests and verify GREEN.**
- [ ] **Step 8: Commit as `feat: reconcile pipeline projection from canonical metadata`.**

### Task 3: Fence every canonical mutation with projection intent

**Files:**
- Modify: `services/app/server/routers/annotations.py`
- Modify: `services/app/server/video_export_completion.py`
- Modify: `services/app/server/routers/sam3.py`
- Modify: `services/app/server/routers/review.py`
- Modify: `services/app/server/routers/objects.py`
- Modify: `services/app/server/object_lifecycle.py`
- Test: `services/app/server/tests/test_triage_optimistic_concurrency.py`
- Test: `services/app/server/tests/test_sam3_publication.py`
- Test: `services/app/server/tests/test_review_media_fence.py`
- Create: `services/app/server/tests/test_projection_mutation_hooks.py`

**Interfaces:**
- Consumes: Task 1 store and Task 2 reconciler.
- Produces: `projection_pending: bool` and `projection_event_seq: int | null` in successful mutation responses where applicable.
- Reservation occurs after ownership/revision validation but before the canonical manifest/file commit. Apply occurs only after canonical publication succeeds.

- [ ] **Step 1: Add RED tests for annotation save/no-object, video export completion, SAM3 current-pointer publication, mask-review batch, and archive/restore.**
- [ ] **Step 2: Assert a reservation failure prevents canonical mutation; assert an apply failure preserves the canonical success and returns `projection_pending=true`.**
- [ ] **Step 3: Assert retry uses the same source identity/event and an older delayed apply cannot replace a newer projection.**
- [ ] **Step 4: Run focused hook tests and verify RED.**
- [ ] **Step 5: Add a small mutation helper/context manager so endpoints do not duplicate reserve/apply/fail semantics.**
- [ ] **Step 6: Wire annotation and video export completion under their existing advisory/file locks.**
- [ ] **Step 7: Wire SAM3 after immutable-generation validation and before/after only the atomic `current.json` pointer swap; never index staging output as current.**
- [ ] **Step 8: Wire review batch around its all-frame validation/publication boundary and preserve HTTP 409 behavior.**
- [ ] **Step 9: Mark every video projection stale on archive/restore/rename without scanning media or deleting rows.**
- [ ] **Step 10: Run all focused mutation, SAM3, review, and lifecycle suites and verify GREEN.**
- [ ] **Step 11: Commit as `fix: keep pipeline projection aligned with canonical commits`.**

### Task 4: Safe library fast path and background repair

**Files:**
- Modify: `services/app/server/routers/library.py`
- Modify: `services/app/server/durable_jobs.py`
- Modify: `services/app/worker.py`
- Modify: `services/app/web/src/api/types.ts`
- Modify: `services/app/web/src/store/library.ts`
- Modify: `services/app/web/src/views/OperationsView.tsx`
- Create: `services/app/server/tests/test_library_projection.py`
- Modify: `services/app/web/scripts/smoke.mjs`

**Interfaces:**
- Library payload adds `projection_status: "current" | "stale" | "missing" | "pending"` and `projected_at: string | null` per video.
- A current projection is accepted only after exact `source_identity` comparison; completion additionally requires `artifacts_valid=true`, `validation_status=manifest`, and `reviewed_frames>=expected_frames`.
- Job kind `pipeline_projection_reconcile` carries only `object_id`, `video_id`, and source identity; enqueue is idempotent.

- [ ] **Step 1: Add RED router tests proving a valid matching projection is served, while missing/stale/pending/mismatched rows use canonical metadata and cannot promote completion.**
- [ ] **Step 2: Add a RED test proving the endpoint performs one bulk projection query, no per-video PostgreSQL query, and no PNG read.**
- [ ] **Step 3: Add RED job tests for idempotent reconciliation, fresh object context per worker job, and no media-processing calls.**
- [ ] **Step 4: Run focused backend tests and verify RED.**
- [ ] **Step 5: Implement minimal bulk lookup/fallback/enqueue behavior and bound reconciliation enqueue volume per request.**
- [ ] **Step 6: Surface stale/pending state as a non-blocking operational indicator; never replace the canonical stage label with optimistic projected completion.**
- [ ] **Step 7: Add a smoke test for stale indicator and current-projection counts.**
- [ ] **Step 8: Run focused backend/frontend tests and verify GREEN.**
- [ ] **Step 9: Commit as `perf: serve verified pipeline projections in library`.**

### Task 5: Verification and rollout

**Files:**
- Modify: `docs/REMOTE-MOVE.md`
- Modify: `README.md`
- Create: `scripts/reconcile-pipeline-projection.ps1`
- Create: `scripts/reconcile-pipeline-projection.sh`
- Test: `services/app/server/tests/test_pipeline_projection_postgres.py`

- [ ] **Step 1: Add a read-only dry-run command that reports missing/stale/current projection counts without changing files.**
- [ ] **Step 2: Add explicit `--apply` reconciliation in bounded batches with resume tokens; it may update only the new PostgreSQL tables.**
- [ ] **Step 3: Document additive migration, dry run, apply, rollback-by-code, and why projection rows need not be copied separately from the database backup.**
- [ ] **Step 4: Run the complete backend, root, SAM3-worker, frontend, real-PostgreSQL, Compose config, and repository hygiene gates.**
- [ ] **Step 5: Verify no command modified `data/`, `models/`, `backups/`, `secrets/`, or any existing annotation/mask artifact.**
- [ ] **Step 6: Commit as `docs: add pipeline projection rollout and repair`.**

## Completion Gate

- [ ] Missing, stale, pending, legacy, or identity-mismatched projection rows never mark a video completed.
- [ ] Old event application cannot overwrite a newer source state.
- [ ] A crash between canonical publication and projection apply leaves a durable, idempotently repairable intent.
- [ ] Library lookup uses one bulk database query and never reads mask pixels.
- [ ] Annotation, SAM3, review, export completion, and lifecycle paths update or invalidate the projection.
- [ ] Reconciliation is metadata-only, bounded, resumable, and safe to run repeatedly.
- [ ] Existing work and persistent mounts remain untouched.
- [ ] All affected test suites have fresh passing evidence, including real PostgreSQL ordering/retry coverage.
