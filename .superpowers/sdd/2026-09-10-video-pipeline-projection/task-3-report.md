# Task 3 — canonical mutation projection hooks

Status: implemented and verified. Selective commit requested as
`fix: keep pipeline projection aligned with canonical commits`.

## Implementation

- Added explicit `reserve_mutation(...)` / `ProjectionMutation.complete()` phases.
  Reservation errors escape before canonical publication. Reconcile failures
  retain canonical success, persist a bounded failure when possible, and return
  `projection_pending=true`. Responses preserve the sequence reserved by the
  mutation even when reconciliation selects a newer exact-source repair event.
- Annotation save and no-object reserve after revision/candidate validation,
  immediately before assigning the canonical entry. Reconciliation follows the
  store flush, video fence release and cleanup/queue work. No-object replay keeps
  the existing annotation revision and stable intent identity. Response flags are
  never inserted into annotation documents.
- Video export completion reserves after ownership/revision/artifact/replay
  validation. The internal completion API receives a deferred handle while it
  holds the video/job fences, then reconciles after both contexts exit. Direct
  finalizers reconcile after their own fence exits. Replays still heal queueing.
- SAM3 `publish_generation` has an additive `before_publish` callback invoked
  after generation validation and immediately before `_publish_pointer` in both
  existing/new generation branches, including recursive publication retry.
  The route applies only after pointer publication, annotation mutation, queue
  finish, owned-lease transaction exit and video-fence exit. Staging is never
  passed to the projection as current output.
- Individual and batch mask review reserve at the existing all-input validation
  boundary; batch reserves once. Legacy set/confirm/reset writers now expose a
  prepublication callback after candidate construction. Review identities retain
  effective content/revisions and exclude audit timestamps/history/user fields.
  HTTP 409 revision conflicts preserve the prior manifest.
- Mask review indexing uses an `ExitStack` publication guard: the SQL revision
  transaction stays open through canonical manifest replacement. A failed
  `os.replace` rolls the SQL index transaction back, preventing a committed index
  from advancing ahead of a rejected manifest publication.
- Lifecycle changes allocate an object barrier from the same global sequence as
  video intents. Apply rejects events at/below the barrier; repair reservation
  cannot reuse pre-barrier current/pending state; the existing bulk `get_many`
  query reports stale before considering a pre-barrier pending event. No media
  enumeration or video-row deletion is involved.
- A separate object advisory fence spans authoritative object-config reload,
  source derivation, repair reservation and apply. Lifecycle holds that fence
  across barrier reservation and registry publication. `apply_intent` uses the
  barrier row lock, never reacquires the outer advisory lock on another
  connection. Local mode uses a corresponding object lock.
- Archive/restore/rename routes return projection outcome fields. A no-op restore
  does not rewrite the registry or advance its barrier.

## Scope expansions and placement

- Lifecycle is centralized in `Workspace.update`, because all three routes and
  non-route metadata callers publish `objects.json` there. This is the location
  that holds the existing registry lock and can reserve after authoritative
  reload yet before field mutation. `object_lifecycle.py` contains validation,
  purge and ownership utilities; it does not publish these three mutations and
  therefore needed no cosmetic hook. Route-level tests prove archive, restore
  and rename reach the central barrier, with flags excluded from registry JSON.
- `pipeline_projection.py` and `pipeline_reconcile.py` changed only to implement
  the required object barrier and shared protection. Query cardinality remains
  one bulk SELECT for `get_many` (plus existing transaction configuration).
- New migration `006_video_pipeline_projection_barriers.sql` upgrades databases
  that already applied 005. Migration 005 is unchanged. 006 is idempotent and
  preserves existing barrier rows on replay. Projection PostgreSQL fixtures load
  005 followed by 006.
- `pipeline_core/sam3_runs.py` changed only for the validated pointer callback.
- `server/review.py` changed only for legacy prepublication callbacks.
- `sam3_run_index.py` changed to extend the transaction lifetime through file
  publication. Existing non-guarded callers retain immediate transaction scope.
- Existing export/SAM3 test doubles were updated to reflect the additive deferred
  handle and publication callback contracts, preserving their original checks.

## RED evidence

All behavior changes were preceded by executed failing tests in the application
Docker image; the local Windows Python alias is not executable.

1. Initial mutation-hook suite: 5 tests, 3 failure subcases and 2 missing-response
   API errors. Annotation/no-object/export accepted mutation despite a simulated
   failed reservation; deferred export handle and pending fields were absent.
2. SAM3 pointer/review batch tests: 3 tests, 2 failures and one missing-callback
   API error. Review committed without reservation and SAM3 lacked the validated
   pointer hook.
3. SAM3 route/all review writers: 2 tests, 5 failure subcases and one missing-hook
   error. Every individual/legacy writer published without the reservation.
4. Lifecycle barrier tests: 3 tests, 5 failure subcases. Archive/rename proceeded
   despite barrier failure, shared protection was absent and the store had no
   monotonic object invalidation operation.
5. Lifecycle response/no-op test: missing response fields reproduced before route
   integration. A real PostgreSQL paused-read/archive/restore race simultaneously
   verified the already-added object fence.
6. Audit regressions: 3 tests, 3 failures: response event sequence changed from 41
   to 99; retry identity changed with `at`/`updated_at`; the index connection had
   committed before a subsequent manifest replacement failure.
7. Upgrade regression: 1 test failed because 005 contained the new barrier DDL;
   moving it into independent migration 006 made the existing-005 upgrade pass.

## GREEN verification

All containers mounted this worktree read-only. Tests write exclusively to their
temporary directories and unique PostgreSQL test schemas. No user datasets,
models, secrets, backups or production database were touched.

Common invocation:

```powershell
docker run --rm -w /src/services/app --entrypoint python `
  -e PYTHONPATH=/src/services/app:/src/packages/pipeline-core/src `
  -v "${PWD}:/src:ro" boom/pipeline-app:0.2.0 -m unittest <modules> -q
```

PostgreSQL runs additionally used network `projection-task4-tests` and the
controller-provided `TEST_DATABASE_URL` for the isolated `projection-task4-pg`
database. Application `DATABASE_URL` remained unset outside specific mocked or
isolated connection factories.

Final focused command modules:

```text
server.tests.test_projection_mutation_hooks
server.tests.test_triage_optimistic_concurrency
server.tests.test_triage_export_completion
server.tests.test_triage_export_completion_api
server.tests.test_sam3_publication
server.tests.test_sam3_revision_queue
server.tests.test_review_media_fence
server.tests.test_revision_index_batch
server.tests.test_object_lifecycle
server.tests.test_pipeline_projection_postgres
server.tests.test_pipeline_reconcile
```

- Focused suite: **154/154 passed**, including real PostgreSQL.
- Final backend: `-m unittest discover -s server/tests -q`:
  **386/386 passed**, with real PostgreSQL tests enabled.
- Root/core/worker: workdir `/src`, `-m unittest discover -s tests -q`:
  **116 run, 111 passed, 5 expected skips**.
- Expected failure-path logging occurred in existing proxy tombstone, cleanup
  queue and worker completion tests; all corresponding tests passed.
- `git diff --check`: passed. Migration 005 diff is empty.

## Self-review

- Reserve-before-mutation, reconcile-after-unlocking verified for annotation,
  export, SAM3 and review; review last-frame conflict stays HTTP 409 and leaves
  the prior manifest byte-identical without reserving an intent.
- Order checks capture observations outside the helper's exception-catching
  boundary, so a failed ordering assertion cannot be swallowed as apply failure.
- Old delayed applies cannot replace a newer row or bypass a lifecycle barrier.
  A PostgreSQL thread paused after reading active config blocks concurrent
  archive until its reservation/apply finishes; archive then makes that row stale.
  Restore of the same A identity allocates a fresh event above both barriers.
- Pre-barrier pending events cannot mask lifecycle staleness in bulk reads.
- Barrier/repair lock order uses different advisory and row-lock resources;
  repeated and concurrent PostgreSQL tests finish without self-deadlock.
- No projection flags enter canonical annotation or object registry JSON.
- Only requested source/test/migration/report paths are staged. Pre-existing
  untracked `__pycache__` directories are preserved and excluded.

## Concerns / operational limits

- Apply/reconcile failures are recoverable through the durable pending intent.
  Without PostgreSQL, projection operations remain the established no-op mode
  (`projection_pending=false`, sequence null).
- JSON publication and the normalized review SQL index are not a distributed
  transaction. The new transaction guard prevents the SQL index leading a failed
  file publication, but a process/database failure after the successful file
  replace and before SQL commit can still leave that normalized index behind.
  The durable projection intent survives; projection reconciliation rebuilds its
  own snapshot, not the separate normalized review index. That broader repair
  concern is outside this projection task.
- Object reconciliation is serialized per object while holding the lifecycle
  protection. This is a deliberate correctness boundary; media is not scanned
  for lifecycle invalidation and library reads remain bulk.
- Deploy migration 006 before running the new store code.
