# Fast Library and Responsive API Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the video library metadata-only, keep filesystem work off the FastAPI event loop, coalesce identical concurrent listings, and expose trustworthy request timing without changing or reprocessing user data.

**Architecture:** Split pipeline inspection into a fast manifest validator and an explicit deep artifact auditor. The library route calls a synchronous payload builder through a keyed single-flight executor backed by `asyncio.to_thread`; request timing is an independent HTTP middleware. Canonical masks and manifests remain untouched, and a legacy run without sufficient manifest evidence is reported as `audit_required`, never promoted to completed.

**Tech Stack:** Python 3.12, FastAPI/Starlette, asyncio, unittest/pytest-compatible tests, PostgreSQL-independent filesystem fixtures.

**Spec:** `docs/superpowers/specs/2026-09-08-reliability-redesign-design.md`

## Global Constraints

- Never read or decode a PNG from `GET /api/objects/{object_id}/videos`.
- Never copy, move, rename, convert, delete, or reprocess existing media, masks, frames, annotations, or exports.
- Keep masks and immutable review manifests canonical; manifest metadata is only an operational index.
- A legacy or incomplete run must become `audit_required` and must not become `completed`.
- Run synchronous filesystem work outside the FastAPI event loop.
- Preserve the existing public fields and add only backward-compatible progress fields.
- Preserve Python `>=3.12,<3.13`; add no framework, broker, or runtime dependency.
- Use deterministic tests; no timing assertion based only on a fragile wall-clock threshold.

---

### Task 1: Metadata-only pipeline inspection

**Files:**
- Modify: `services/app/server/pipeline_state.py`
- Modify: `services/app/server/tests/test_pipeline_state.py`

**Interfaces:**
- Produces: `inspect_pipeline_entry(entry, sam3, output_root) -> PipelineSnapshot`, guaranteed not to read mask bytes.
- Produces: `audit_pipeline_entry(entry, sam3, output_root) -> PipelineSnapshot`, the explicit deep PNG validation path.
- Produces: `PipelineSnapshot.validation_status: str`, one of `not_applicable`, `manifest`, `audit_required`, or `invalid`.
- Consumes: existing `_sam3/run.json`, `prompt.json`, and `mask_review.json` schemas; no database or UI changes.

- [ ] **Step 1: Add a failing regression proving the fast path never reads masks**

Create a completed two-frame fixture whose `run.json.artifacts` contains `format: "png-1bit-v1"`, `files: 2`, and exactly two 64-character checksum entries, but do not create the PNG files. Patch `Path.read_bytes` to raise `AssertionError("PNG read on fast path")`, call `inspect_pipeline_entry`, and assert:

```python
self.assertEqual((snapshot.stage, snapshot.status), ("completed", "validated"))
self.assertEqual(snapshot.validation_status, "manifest")
self.assertTrue(snapshot.artifacts_valid)
```

- [ ] **Step 2: Add failing legacy and deep-audit tests**

Change the current missing-mask test to declare a syntactically complete artifacts manifest and call `audit_pipeline_entry`; assert it reports `("sam3", "invalid")`. Add a separate legacy test with no `artifacts` block and fully reviewed frames; assert:

```python
self.assertEqual((snapshot.stage, snapshot.status), ("review", "audit_required"))
self.assertEqual(snapshot.validation_status, "audit_required")
self.assertFalse(snapshot.artifacts_valid)
```

- [ ] **Step 3: Run the focused tests and verify RED**

Run:

```powershell
python -m unittest services.app.server.tests.test_pipeline_state -v
```

Expected: failures because `validation_status` and `audit_pipeline_entry` do not exist and the current inspector tries to read PNGs.

- [ ] **Step 4: Implement strict manifest validation without pixel I/O**

In `pipeline_state.py`:

- add `validation_status` to `PipelineSnapshot` and `public_progress()`;
- parse only JSON text and path metadata in `inspect_pipeline_entry`;
- require `status == "done"`, known schema, positive dimensions/frame count, `frames_written == frame_count`, unique prompt object IDs, matching run object IDs when present, `artifacts.format == "png-1bit-v1"`, exact artifact cardinality, exact expected relative keys, and lowercase SHA-256 strings;
- classify missing legacy evidence as `audit_required`, structural contradictions as `invalid`, and a complete manifest as `manifest`;
- preserve review counting from `mask_review.json`;
- never call `Path.read_bytes`, `inspect_binary_png`, or Pillow on the fast path.

- [ ] **Step 5: Move byte validation into the explicit auditor**

Implement `audit_pipeline_entry` by first validating metadata, then opening every expected mask and calling `inspect_binary_png(expected_size=...)`. Return `validation_status="invalid"` on missing/invalid bytes and `validation_status="manifest"` only when every expected artifact passes. Keep raw masks read-only.

- [ ] **Step 6: Run focused and adjacent tests and verify GREEN**

Run:

```powershell
python -m unittest services.app.server.tests.test_pipeline_state tests.test_masks tests.test_runner_masks -v
```

Expected: all tests pass; the fast-path regression would fail immediately if mask bytes were opened.

- [ ] **Step 7: Commit the inspector boundary**

```powershell
git add services/app/server/pipeline_state.py services/app/server/tests/test_pipeline_state.py
git commit -m "perf: make pipeline listing metadata only"
```

---

### Task 2: Non-blocking, coalesced library listing

**Files:**
- Create: `services/app/server/singleflight.py`
- Create: `services/app/server/tests/test_singleflight.py`
- Modify: `services/app/server/routers/library.py`
- Create: `services/app/server/tests/test_library_responsiveness.py`

**Interfaces:**
- Produces: `AsyncSingleFlight.run(key: Hashable, factory: Callable[[], Awaitable[T]]) -> T`.
- Produces: `_build_video_listing(ctx, search, status, sort) -> dict`, synchronous and side-effect free except metadata reads.
- Consumes: Task 1 metadata-only `inspect_pipeline_entry`.

- [ ] **Step 1: Write failing single-flight behavior tests**

Using `unittest.IsolatedAsyncioTestCase`, start two callers with the same key and a factory held by an `asyncio.Event`. Assert the factory is invoked once and both callers receive the same result. Add a second test where the shared factory raises; a later call with the same key must execute a fresh factory rather than reuse a poisoned task.

- [ ] **Step 2: Run the single-flight test and verify RED**

Run:

```powershell
python -m unittest services.app.server.tests.test_singleflight -v
```

Expected: import failure because `server.singleflight` does not exist.

- [ ] **Step 3: Implement `AsyncSingleFlight`**

Use an `asyncio.Lock` to guard a dictionary of in-flight tasks. Create one task per absent key, await it through `asyncio.shield`, and remove only the identical completed task in `finally`. Do not cache completed values and do not swallow cancellation or exceptions.

- [ ] **Step 4: Write failing library responsiveness tests**

Add one test that patches `_build_video_listing` with a blocking `threading.Event`, starts `list_videos`, and proves an independent event-loop coroutine runs before the event is released. Add a second test that launches two identical `list_videos` calls for one object and asserts `_build_video_listing` executes once. Use lightweight fake context/index/store objects; never scan real workspace data.

- [ ] **Step 5: Run the library tests and verify RED**

Run:

```powershell
python -m unittest services.app.server.tests.test_library_responsiveness -v
```

Expected: failure because listing still executes inline and has no coalescer boundary.

- [ ] **Step 6: Extract and offload the listing builder**

Move the current payload construction, counting, filtering, and sorting into `_build_video_listing`. Keep `list_videos` async and call the builder through module-level `AsyncSingleFlight`, with a key containing object ID, scan revision/timestamp, search, status, and sort. Run the builder with `asyncio.to_thread`. Make `rescan` await `ctx.rescan` in a thread and then invoke the same coalesced list path.

- [ ] **Step 7: Run focused and router tests and verify GREEN**

Run:

```powershell
python -m unittest services.app.server.tests.test_singleflight services.app.server.tests.test_library_responsiveness services.app.server.tests.test_pipeline_state -v
```

Expected: all tests pass, identical simultaneous listings build once, and the event loop remains responsive.

- [ ] **Step 8: Commit the concurrency boundary**

```powershell
git add services/app/server/singleflight.py services/app/server/routers/library.py services/app/server/tests/test_singleflight.py services/app/server/tests/test_library_responsiveness.py
git commit -m "perf: offload and coalesce video listings"
```

---

### Task 3: Request IDs and normalized server timing

**Files:**
- Create: `services/app/server/observability.py`
- Create: `services/app/server/tests/test_observability.py`
- Modify: `services/app/server/main.py`

**Interfaces:**
- Produces: `install_request_observability(app: FastAPI, slow_request_ms: float = 1000.0) -> None`.
- Produces response headers `X-Request-ID` and `Server-Timing: app;dur=<milliseconds>`.
- Consumes no user data; logs only request ID, method, normalized route template, status, and duration.

- [ ] **Step 1: Write failing middleware tests**

Build a minimal FastAPI app, install the middleware, and assert a request receives a UUID request ID and a parseable non-negative `Server-Timing` duration. Add a parameterized route `/items/{item_id}` and capture logs for a forced slow threshold of `0`; assert the log contains `/items/{item_id}` and does not contain the concrete item ID.

- [ ] **Step 2: Run the tests and verify RED**

Run:

```powershell
python -m unittest services.app.server.tests.test_observability -v
```

Expected: import failure because `server.observability` does not exist.

- [ ] **Step 3: Implement isolated observability middleware**

Use `time.perf_counter`, accept a valid incoming `X-Request-ID` containing only `[A-Za-z0-9._-]` up to 128 characters or generate `uuid.uuid4()`, and set both headers on success and handled errors. After routing, read `request.scope["route"].path` when available; fall back to `"unmatched"`, never the raw URL path. Log requests at warning level only when duration exceeds the threshold.

- [ ] **Step 4: Install middleware in the application**

Call `install_request_observability(app)` immediately after FastAPI construction and before router registration. Do not make health depend on workspace, PostgreSQL, MinIO, FFmpeg, or SAM3.

- [ ] **Step 5: Run focused and startup tests and verify GREEN**

Run:

```powershell
python -m unittest services.app.server.tests.test_observability services.app.server.tests.test_app_startup -v
```

If `test_app_startup` is absent, run the complete `services/app/server/tests` discovery instead:

```powershell
python -m unittest discover -s services/app/server/tests -p "test_*.py" -v
```

Expected: all available tests pass and headers are present.

- [ ] **Step 6: Commit observability**

```powershell
git add services/app/server/observability.py services/app/server/main.py services/app/server/tests/test_observability.py
git commit -m "feat: add normalized request timing"
```

---

### Task 4: Whole-subproject regression and lightweight benchmark

**Files:**
- Create: `scripts/benchmark-library.py`
- Modify: `README.md`

**Interfaces:**
- Produces: a read-only benchmark command that requests an existing object library repeatedly and reports warm p50/p95 without printing video names or credentials.
- Consumes: base URL and object ID through command-line arguments; default base URL `http://127.0.0.1:8000`.

- [ ] **Step 1: Add a benchmark parser test before implementation**

Keep percentile calculation in a pure function importable from the script. Add a unit test with known samples `[10, 20, 30, 40, 50]` and assert deterministic p50/p95 interpolation or nearest-rank behavior documented by the function.

- [ ] **Step 2: Verify RED, then implement the read-only benchmark**

The script performs one warm-up followed by a configurable number of sequential GETs, uses a finite timeout, fails non-zero on non-2xx, and prints only sample count, p50, p95, min, and max. It must not mutate, rescan, or enumerate files itself.

- [ ] **Step 3: Document safe usage and acceptance target**

Document:

```powershell
python scripts/benchmark-library.py --base-url http://127.0.0.1:8000 --object-id boom --samples 20
```

State that the reference acceptance target is warmed p95 `<= 500 ms` for 416 videos, measured on the deployed host after this branch is built. Do not claim the target from unit tests.

- [ ] **Step 4: Run the complete relevant verification**

Run:

```powershell
python -m unittest discover -s services/app/server/tests -p "test_*.py" -v
python -m unittest discover -s tests -p "test_*.py" -v
```

Then build the frontend to prove backend response additions remain compatible:

```powershell
npm --prefix services/app/web run build
```

Expected: all tests and the TypeScript production build pass. Record any pre-existing environment-only failure separately; do not hide it.

- [ ] **Step 5: Commit benchmark and documentation**

```powershell
git add scripts/benchmark-library.py README.md services/app/server/tests
git commit -m "test: add library latency regression coverage"
```

---

## Completion Gate

- [ ] `GET /videos` has no call path to `inspect_binary_png`, Pillow, or `Path.read_bytes`.
- [ ] Legacy manifests expose `validation_status=audit_required` and cannot become completed.
- [ ] Deep audit still detects missing, malformed, wrong-sized, and non-binary masks.
- [ ] Simultaneous identical listings share one filesystem build.
- [ ] A blocked listing does not block an independent event-loop task or `/api/health`.
- [ ] Request logs use normalized route templates and contain no filenames, video IDs, tokens, or mask payloads.
- [ ] Unit/integration tests and frontend build have fresh passing evidence.
- [ ] No command touched `data/`, `models/`, `backups/`, `secrets/`, or an existing annotation artifact.
