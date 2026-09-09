# Triage and Proxy Reliability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development and superpowers:test-driven-development. Implement one task at a time, with a fresh implementer and a fresh reviewer for each task.

**Goal:** Make triage open reliably, never strand the operator on a black frame, acknowledge annotation actions quickly, navigate immediately after durable mutations, and finish background frame exports even if the browser leaves the page.

**Architecture:** Treat proxy frames as immutable published generations. FFmpeg writes into a staging generation, the worker validates both tiers, and only then swaps it into the public path and writes a versioned completion marker. The frontend has an explicit bootstrap/recovery state machine with full-to-small fallback and one in-flight repair per video/frame. Annotation mutations use cached/indexed media only and enqueue heavy cleanup/export work. Export completion is committed by the app through an authenticated worker callback, not by a browser lifecycle callback.

**Tech Stack:** Python 3.12, FastAPI/Starlette, PostgreSQL durable jobs, React 19, Zustand, TypeScript 5.9, JSDOM smoke harness, unittest.

**Spec:** `docs/superpowers/specs/2026-09-08-reliability-redesign-design.md`

## Global constraints

- Do not modify, delete, move, rename, re-encode, or reprocess anything in `data/`, `models/`, `backups/`, `secrets/`, or existing annotation/export artifacts during tests.
- A successful save/no-object response means the annotation JSON is durably persisted.
- Navigation may happen only after the mutation and export enqueue ACKs, but must not wait for library refresh, FFmpeg, filesystem cleanup, or SAM3.
- Never serve a JPEG that FFmpeg may still be writing.
- Never publish a complete proxy unless `small` and `full` contain the same non-zero frame count.
- Preserve the previous complete proxy until its replacement is fully validated.
- Keep browser compatibility: existing `/export/finish` remains as an idempotent compatibility endpoint.
- Keep one SAM3 worker; these changes affect only app/CPU worker concurrency.
- Add no frontend framework or state-management dependency.

---

### Task 1: Recoverable frontend proxy bootstrap and fast navigation

**Files:**
- Modify: `services/app/web/src/store/annotator.ts`
- Modify: `services/app/web/src/components/FramePreview.tsx`
- Modify: `services/app/web/src/views/AnnotatorView.tsx`
- Modify: `services/app/web/scripts/smoke.mjs`
- Modify: `services/app/web/package.json`

**Interfaces:**
- `open(videoId)` must publish state only while that `videoId` is still active.
- `ensureFrameAvailable(frame)` must coalesce an identical in-flight request and recover both window-mode misses and invalid complete caches.
- `FramePreview` may request `full`, fall back to `small`, and show an actionable loading/error state; it must not leave an empty black stage.
- Save/no-object handlers derive the next target from the current library snapshot before mutation and refresh the board in the background after navigation.

- [ ] Add RED smoke scenario `triage-window-bootstrap`: proxy status becomes `window` only after the first image error; assert exactly one window POST occurs and the image is reloaded after the job completes.
- [ ] Add RED smoke scenario `triage-full-frame-fallback`: dispatch an error on the `tier=full` stage image and assert the active stage switches to `tier=small`; dispatching a second error must initiate one forced repair, not an unbounded request loop.
- [ ] Add RED scenarios `triage-submit-next-slow-refresh` and `triage-no-object-next-slow-refresh`: hold the post-mutation `/videos` response, assert navigation has already occurred, and assert mutation ACK precedes navigation. For submit, assert PUT annotation precedes POST export.
- [ ] Add a mutation-failure scenario asserting navigation does not occur, export is not enqueued after a failed PUT, and the visible error is retained.
- [ ] Run focused smoke scenarios and capture RED evidence.
- [ ] Add an open-generation guard around every async `open()` state publication and watcher callback so a slow previous video cannot overwrite the new video.
- [ ] Bootstrap proxy status before declaring the frame stage ready, then explicitly ensure the current frame when mode is `window`.
- [ ] Coalesce `ensureFrameAvailable` by `videoId:frame`; re-check active video before every state update. When a supposedly complete cache misses both tiers, force a full proxy repair and observe its job.
- [ ] In `FramePreview`, keep `requestedTier` state: reset to preferred tier on video/frame/play-state changes, fall back `full -> small` on first error, and call recovery only after `small` also fails. Retry only when availability/generation changes.
- [ ] Capture `nextPending` before the mutation. After save+export enqueue or no-object persistence succeeds, navigate synchronously and fire `refresh()` without awaiting it. Preserve the current screen on failure.
- [ ] Run all smoke scenarios and `npm run build`; verify GREEN.
- [ ] Commit as `fix: make triage navigation and frame recovery immediate`.

---

### Task 2: Atomic, validated proxy publication

**Files:**
- Modify: `services/app/server/proxy.py`
- Modify: `services/app/worker.py`
- Modify: `services/app/server/routers/video.py`
- Create: `services/app/server/tests/test_proxy_reliability.py`
- Modify: `tests/test_cpu_worker_safety.py`

**Interfaces:**
- `proxy.validate_generation(path) -> ProxyGeneration` validates marker-independent staged output with metadata only.
- `proxy.publish_generation(staging, root, cache_root) -> ProxyGeneration` renames a validated immutable generation and atomically replaces only a small `CURRENT` pointer.
- Layout is `proxy/<video>/CURRENT` plus `generations/<uuid>/`; window roots use the same layout. A `.part` generation is never addressable through `CURRENT`.
- Completion marker schema v2 lives inside the immutable generation and contains `schema_version`, generation/job identity, kind/range, frames, per-tier counts, dimensions/configuration, and a generation token.
- `is_complete()` performs bounded O(1) checks and invalidates legacy/contradictory markers.

- [ ] Write RED tests proving a complete marker with a missing tier, unequal counts, zero-byte boundary JPEG, or old schema is not complete.
- [ ] Write a RED worker test where FFmpeg fails after an already-complete proxy exists; assert the old generation and marker are byte-for-byte preserved and staging is removed.
- [ ] Write a RED success test asserting no staged output is addressable during FFmpeg, both tiers are validated, and `CURRENT` is the final/only publication write.
- [ ] Write a RED test proving `available_ranges()` never advertises an in-progress full generation and `locate_frame()` never returns a `.part` artifact.
- [ ] Write RED crash tests around every publish boundary: before generation rename, after generation rename/before `CURRENT`, and while replacing `CURRENT`; the old pointer must remain valid and a new orphan must never become active.
- [ ] Run focused tests and capture RED evidence.
- [ ] Introduce versioned marker parsing/writing with bounded boundary-file checks; never enumerate all frames in request handlers.
- [ ] Make durable and in-process `proxy_full` extraction use a unique `generations/.<uuid>.part` directory. Validate exact equal tier counts and sequential non-empty regular JPEGs after FFmpeg; write the marker, rename to immutable `generations/<uuid>`, fsync a temporary pointer, then `os.replace()` it over `CURRENT`.
- [ ] Resolve all reads (`is_complete`, `available_ranges`, `locate_frame`) through a validated `CURRENT` pointer. Request handlers may inspect marker plus O(1) boundary files but must never enumerate a generation.
- [ ] Apply the same immutable-generation/pointer model to windows; never delete/replace a non-empty public directory.
- [ ] Use a distinct staging UUID per attempt so a stale worker cannot clean another worker's staging. Leave post-publish orphan collection outside the critical path.
- [ ] Remove the frontend-derived progress range contract from the API: job progress remains progress only; availability comes from published server state.
- [ ] Fence publication on the current lease/cancellation state. Ensure cancellation/failure removes only its own staging and never the prior current generation.
- [ ] Run proxy, worker-safety, video-router, and full server/root tests; verify GREEN.
- [ ] Commit as `fix: publish proxy frames atomically`.

---

### Task 3: Fast annotation ACK and durable cleanup queue

**Files:**
- Modify: `services/app/server/routers/annotations.py`
- Modify: `services/app/worker.py`
- Modify: `services/app/server/durable_jobs.py` only if a query/helper is needed
- Create: `services/app/server/tests/test_triage_mutation_latency.py`
- Modify: `tests/test_cpu_worker_safety.py`

**Interfaces:**
- `_media_for_write(ctx, video_id)` may use the exact proxy marker or cached probe and may issue only a bounded non-packet-count probe as fallback.
- `PUT /annotations/{video_id}` must not call `count_packets`, enumerate proxy frames, or clean filesystem trees.
- `POST /annotations/{video_id}/no-object` persists first and returns a `cleanup_job_id` when old exported segments need removal.
- Every annotation entry has monotonic `annotation_revision` (legacy default `0`); export and cleanup jobs carry that revision and may not commit against a newer state.
- New CPU job `video_export_cleanup` accepts object/video/revision plus a validated direct-child basename, reconstructs the root from the current workspace context, and deletes only direct `seg_*` children after strict containment checks.

- [ ] Add RED tests that patch packet counting, `proxy.status`, and `clean_segments` to fail if called in either mutation endpoint.
- [ ] Add a RED event-loop test with cleanup blocked by a thread event and prove the no-object endpoint still returns after persistence/enqueue.
- [ ] Add RED tests for `video_export_cleanup` containment, idempotency, missing roots, cancellation boundary, and successful cleanup without touching sibling exports.
- [ ] Add RED concurrency/fencing tests: two same-video mutations preserve history and increment revisions; a stale export cannot promote a later no-object entry; a stale cleanup cannot delete a later re-export.
- [ ] Add a RED database test for two simultaneous identical idempotency keys; both callers must receive the same job without a unique-key failure.
- [ ] Run focused tests and capture RED evidence.
- [ ] Build PUT and no-object entries with `ctx.store.mutate()` so previous state, history, and revision are read/updated under the writer lock. Replace `_media_for` with exact-marker/cached/previous metadata and `probe(count_packets=False)` fallback only for PUT; no-object never probes media. Derive proxy mode without `available_ranges()`.
- [ ] Persist no-object before enqueueing cleanup. Validate the previous root as one direct child of `output_root`, then enqueue an idempotent job keyed by object/video/revision. If enqueue fails, return persisted state with a retryable cleanup marker instead of rolling back the user decision.
- [ ] Add the CPU worker handler; reconstruct the root from its basename, reject root/sibling/symlink escapes, delete only direct `seg_*` children via `_remove_tree`, and preserve other files.
- [ ] Fence every video export/cleanup with annotation revision; cancel older active exports after a new mutation. Use a per-object/video advisory lock around worker media mutation and re-check revision immediately before destructive work/commit.
- [ ] Make durable-job idempotent creation atomic under concurrent absent-row callers (`INSERT ... ON CONFLICT` or equivalent transaction-safe approach).
- [ ] Keep previous export metadata in the cleanup job payload/audit history while clearing it from the effective annotation immediately.
- [ ] Run focused, server, and root tests; verify GREEN.
- [ ] Commit as `perf: remove media cleanup from triage mutations`.

---

### Task 4: Server-owned export completion

**Files:**
- Create: `services/app/server/video_export_completion.py`
- Modify: `services/app/server/routers/annotations.py`
- Modify: `services/app/worker.py`
- Modify: `compose.yml`
- Create: `services/app/server/tests/test_triage_export_completion.py`
- Modify: `services/app/web/src/views/AnnotatorView.tsx`
- Modify: `services/app/web/scripts/smoke.mjs`

**Interfaces:**
- `finalize_video_export(ctx, video_id, result, user, job_id)` is idempotent and is the only place that promotes annotation status to `done` and enqueues SAM3.
- Authenticated internal endpoint accepts a running CPU worker's job ID/result and worker token; it rejects wrong kind/object/video/result roots.
- The CPU worker calls the internal endpoint after output validation and before marking the job done.
- Existing browser `/export/finish` delegates to the same idempotent helper; the frontend no longer owns correctness.

- [ ] Add RED tests: export completion with no browser request still writes export metadata, marks `done`, and enqueues SAM3 once; repeated callback/legacy finish is idempotent; wrong token/job/root is rejected.
- [ ] Add a RED worker test proving failure to obtain completion ACK prevents the job from being marked done/retries safely.
- [ ] Add a RED smoke scenario that unmounts the annotator immediately after enqueue and never calls `/export/finish`; frontend still exposes the background job but correctness is not coupled to the view.
- [ ] Run focused tests and capture RED evidence.
- [ ] Extract finalization logic, including same-object containment validation and SAM3 enqueue deduplication.
- [ ] Add the worker-only endpoint using the existing shared worker secret and validate the currently leased job identity.
- [ ] Configure CPU worker `MST_API=http://app:8000`; send a finite-timeout callback with sanitized logging and retryable failure semantics.
- [ ] Delegate the compatibility endpoint to the shared finalizer and remove the browser callback from `AnnotatorView`; retain board refresh on terminal job status.
- [ ] Run focused, server, root, frontend, and compose security tests; verify GREEN.
- [ ] Commit as `fix: finalize frame exports independently of browser`.

---

### Task 5: Subproject verification and operational handoff

**Files:**
- Modify: `README.md`
- Modify: `docs/REMOTE-MOVE.md` only if runtime update commands are stale
- Create or modify: `.superpowers/sdd/2026-09-08-triage-proxy-reliability/progress.md` (ignored working evidence)

- [ ] Run `python -m unittest discover -s services/app/server/tests -p "test_*.py" -v`.
- [ ] Run `python -m unittest discover -s tests -p "test_*.py" -v`.
- [ ] Run `npm --prefix services/app/web run build`.
- [ ] Run `docker compose config` and the compose security tests without starting or mutating user services.
- [ ] Inspect the full diff for data paths, secret exposure, destructive operations, blocking I/O, stale frontend state, and browser-dependent completion.
- [ ] Request a fresh whole-subproject code review and resolve every Critical/Important finding.
- [ ] Document server update commands that rebuild/recreate only `app` and `worker`; explicitly warn against `docker compose down -v` and confirm the bind-mounted workspace is untouched.
- [ ] Commit as `docs: add triage reliability rollout` if documentation changes.

## Completion gate

- [ ] A window-mode video cannot remain black after the proxy status arrives.
- [ ] A missing full frame falls back to small; a missing complete generation triggers one coalesced repair.
- [ ] A slow response from video A cannot overwrite state for video B.
- [ ] Save/no-object never waits for a refreshed library, FFmpeg, packet counting, proxy directory enumeration, or recursive deletion.
- [ ] Save+export+next mutates, enqueues, and navigates in that order.
- [ ] Failed mutations never navigate or enqueue downstream work.
- [ ] No public proxy JPEG is visible before both tiers are complete and validated.
- [ ] Failed/cancelled proxy replacement preserves the prior generation.
- [ ] Export completion and SAM3 enqueue survive browser navigation/reload.
- [ ] All tests/builds have fresh passing evidence.
- [ ] No test or implementation command modified user media, masks, annotations, models, backups, or secrets.
