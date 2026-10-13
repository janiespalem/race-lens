# Automated Radio Archive Maintenance Implementation Plan

> Execute task-by-task with a fresh review gate after every commit. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Automatically migrate stale machine-generated radio transcripts to the current quality profile and atomically publish a verified replay revision without operator file edits.

**Architecture:** A deep `RadioArchiveMaintainer.run_once()` module owns bounded discovery, checkpointed transcription, archive invariants, and revision publication. Object storage gains a backward-compatible archive-manifest v2 with content-addressed immutable revisions; the recorder invokes maintenance only during idle time outside its capture guard.

**Tech Stack:** Python 3.11+, faster-whisper, Pydantic event models, boto3-compatible object storage, pytest, Ruff.

**Spec:** `docs/design/2026-10-13-radio-archive-maintenance.md`

## Global Constraints

- Preserve the fast non-blocking Live `TranscriptWorker`; strict word-confidence work is replay-only.
- Treat non-empty transcript text without metadata as manual and preserve it byte-for-byte.
- Keep accepted, rejected, and deferred processing outcomes distinct; deferred work must never remove published text.
- Process at most one archive, three clips, and 60 soft-deadline seconds per recorder pass.
- Accept at most 100 radio clips per archive and at most 10 MiB per audio clip.
- Keep the existing 64 MiB per-object and 128 MiB per-session limits.
- Publish immutable content-addressed objects first and the stable manifest last; retain the previous revision.
- Keep archive schema versioning separate from live/preparation `SCHEMA_VERSION`.
- Assume one recorder archive writer; fail on source-manifest change instead of claiming multi-writer compare-and-swap.
- Do not log transcript text, audio URLs, provider responses, or credentials.
- Do not add dependencies, an LLM correction path, revision garbage collection, or a generic workflow engine.

## Review Focus

- Malformed legacy transcript metadata must quarantine the archive rather than guess ownership; Task 3 pins this with `test_malformed_provenance_is_quarantined`.
- A source manifest changed during reconciliation must cause a conflict with no manifest write; Task 4 pins this with `test_source_revision_change_aborts_publish`.
- A corrupt or oversized checkpoint must quarantine the workspace without replacing the published archive; Task 3 pins this with `test_corrupt_checkpoint_is_quarantined`.
- More than 100 radio clips must be rejected before audio download or inference; Task 3 pins this with `test_radio_clip_limit_fails_before_transcription`.
- Event-ID changes must be confined to changed radio events while every non-radio event remains byte-equivalent; Task 4 pins this with `test_reconciliation_changes_only_machine_radio_events`.

---

### Task 1: Explicit Final-Transcript Outcomes and Provenance

**Files:**
- Create: `backend/racelens/radio/models.py`
- Modify: `backend/racelens/radio/transcribe.py`
- Modify: `backend/racelens/cli.py`
- Test: `backend/tests/test_radio_transcribe.py`

**Interfaces:**
- Consumes: existing faster-whisper `_model()`, quality thresholds, atomic fixture writes, and Live `transcribe(url) -> str | None`.
- Produces: `CURRENT_RADIO_PROFILE`, `CURRENT_RADIO_POLICY`, `FinalTranscriptDecision`, `RadioSummary.to_manifest() -> dict[str, object]`, `transcribe_final(url) -> FinalTranscriptDecision`, and `summarize_fixture(path) -> RadioSummary`.

- [ ] **Step 1: Add failing tests for accepted, rejected, and deferred final decisions**

  Add `test_transcribe_final_returns_accepted_decision`, `test_transcribe_final_returns_rejected_decision`, and `test_transcribe_final_returns_deferred_without_sensitive_error`. Assert that processing exceptions produce `status == "deferred"`, a stable reason such as `processing_failed`, no text, and no exception/audio URL in metadata.

- [ ] **Step 2: Run the decision tests and confirm they fail**

  Run: `PYTHONPATH=backend ../../.venv/bin/python -m pytest backend/tests/test_radio_transcribe.py -k 'transcribe_final' -q`

  Expected: FAIL because the decision types and `transcribe_final` do not exist.

- [ ] **Step 3: Implement the radio model types and final adapter**

  In `models.py`, define exact string constants for profile `medium-int8-word-timestamps-v1` and policy `f1-confidence-gate-v1`; define frozen slot dataclasses:

  ```python
  FinalTranscriptDecision(status: Literal["accepted", "rejected", "deferred"], text: str | None, reason: str | None, metadata: dict[str, Any])
  RadioSummary(profile: str, policy: str, total: int, accepted: int, rejected: int, manual: int)
  ```

  `RadioSummary.to_manifest()` returns exactly those six fields. Refactor only the strict durable path to return the decision type. Keep Live `transcribe()` behaviour and options unchanged.

- [ ] **Step 4: Add failing tests for ownership, incomplete processing, and summary counts**

  Add tests asserting: manual text is unchanged; current accepted/rejected results are reused; older machine metadata is reprocessed; a deferred decision preserves old machine text and makes the CLI operation fail; `summarize_fixture()` reports exact total/accepted/rejected/manual counts and rejects malformed metadata.

- [ ] **Step 5: Implement provenance-aware enrichment and summary**

  Make `enrich_fixture()` persist accepted/rejected decisions, retain resumable progress, and raise `IncompleteTranscriptionError` after the pass if any decision is deferred. Update `_cmd_radio_transcribe()` to return a non-zero process exit through that exception. Do not change manual text or Live behaviour.

- [ ] **Step 6: Run focused tests and lint**

  Run: `PYTHONPATH=backend ../../.venv/bin/python -m pytest backend/tests/test_radio_transcribe.py -q`

  Run: `../../.venv/bin/python -m ruff check backend/racelens/radio backend/racelens/cli.py backend/tests/test_radio_transcribe.py`

  Expected: all tests pass and Ruff prints no errors.

- [ ] **Step 7: Commit Task 1**

  ```bash
  git add backend/racelens/radio/models.py backend/racelens/radio/transcribe.py backend/racelens/cli.py backend/tests/test_radio_transcribe.py
  git commit -m "refactor(radio): distinguish final transcript outcomes"
  ```

### Task 2: Backward-Compatible Immutable Archive Revisions

**Files:**
- Modify: `backend/racelens/object_storage.py`
- Modify: `backend/tests/test_object_storage.py`
- Modify: `backend/tests/test_diagnostics.py`

**Interfaces:**
- Consumes: the exact dictionary returned by `RadioSummary.to_manifest()`.
- Produces: `ARCHIVE_SCHEMA_V1`, `ARCHIVE_SCHEMA_V2`, `manifest_digest(manifest) -> str`, `archive_revision(files, radio_summary) -> str`, and `publish_archive_revision(store, current_manifest, fixture_path, track_path, positions_path, *, event_count, radio_summary) -> dict`.

- [ ] **Step 1: Add failing v1/v2 manifest validation tests**

  Extend `test_object_storage.py` with tests proving that existing v1 fixed-key manifests remain valid; v2 requires `revision`, `parent_revision`, `radio_profile`, `radio_policy`, and the bounded radio summary; v2 object keys must match `sessions/{replay}/revisions/{revision}/{name}`; unsafe keys, inconsistent counts, and unknown fields fail closed.

- [ ] **Step 2: Run the manifest tests and confirm they fail**

  Run: `PYTHONPATH=backend ../../.venv/bin/python -m pytest backend/tests/test_object_storage.py -k 'manifest' -q`

  Expected: FAIL because archive schema v2 is not accepted.

- [ ] **Step 3: Split archive schema validation from shared record schema**

  Keep `SCHEMA_VERSION` for live/preparation records. Add archive-specific constants and branch `validate_manifest()` by archive schema. Make `manifest_digest()` SHA-256 the canonical sorted compact JSON bytes of the validated manifest.

- [ ] **Step 4: Add failing revision-publication ordering and idempotence tests**

  Test that revision keys are deterministic from content sizes/hashes plus radio profile/policy; all three revision objects are uploaded and verified before the stable manifest write; identical input returns the current manifest with zero uploads; a different parent manifest raises a conflict; an injected upload/verify failure leaves the old manifest unchanged.

- [ ] **Step 5: Implement content-addressed revision publication**

  Implement `publish_archive_revision()` without invalidating the current manifest. Re-read the expected current manifest immediately before the final write, write the v2 manifest last, read it back, and verify every referenced object. Do not delete old revisions. Keep `publish_session()` compatible for existing callers; when passed a `radio_summary`, it may create an initial v2 archive through the same revision machinery.

- [ ] **Step 6: Prove cache rematerialization for a manifest revision**

  Extend `test_diagnostics.py` or the existing `RemoteSessionCache` test: materialize v1, publish v2, acquire a new lease, and assert the new events bytes are downloaded exactly once while concurrent readers share that materialization.

- [ ] **Step 7: Run object-storage tests and lint**

  Run: `PYTHONPATH=backend ../../.venv/bin/python -m pytest backend/tests/test_object_storage.py backend/tests/test_diagnostics.py -q`

  Run: `../../.venv/bin/python -m ruff check backend/racelens/object_storage.py backend/tests/test_object_storage.py backend/tests/test_diagnostics.py`

  Expected: all tests pass and Ruff prints no errors.

- [ ] **Step 8: Commit Task 2**

  ```bash
  git add backend/racelens/object_storage.py backend/tests/test_object_storage.py backend/tests/test_diagnostics.py
  git commit -m "feat(storage): publish immutable archive revisions"
  ```

### Task 3: Bounded Discovery, Checkpoints, and Resume

**Files:**
- Create: `backend/racelens/radio/archive_maintenance.py`
- Create: `backend/tests/test_radio_archive_maintenance.py`

**Interfaces:**
- Consumes: `FinalTranscriptDecision`, current profile/policy constants, object-store `list_keys/get_json/download_verified`, and manifest validation/digests.
- Produces: `MaintenanceBudget`, `MaintenanceResult`, and `RadioArchiveMaintainer.run_once(budget) -> MaintenanceResult`.

- [ ] **Step 1: Write failing selection and no-op tests**

  Add `test_current_archive_performs_zero_downloads_or_inference`, `test_legacy_archive_is_selected_once`, and `test_only_one_archive_is_selected_per_run`. Use a scripted transcription callable and an in-memory object store with operation counters.

- [ ] **Step 2: Run the selection tests and confirm they fail**

  Run: `PYTHONPATH=backend ../../.venv/bin/python -m pytest backend/tests/test_radio_archive_maintenance.py -k 'current_archive or selected' -q`

  Expected: FAIL because the maintainer module does not exist.

- [ ] **Step 3: Implement the module interface and bounded manifest cursor**

  Constructor signature:

  ```python
  RadioArchiveMaintainer(store: Any, workspace: Path, transcribe: Callable[[str], FinalTranscriptDecision], *, profile: str = CURRENT_RADIO_PROFILE, policy: str = CURRENT_RADIO_POLICY, publish: bool = True, now: Callable[[], datetime] | None = None)
  ```

  Use `datetime.now(UTC)` when `now` is absent. `run_once()` validates positive bounds capped at one archive, three clips, 60 seconds, and scans at most 1,000 sorted `sessions/*/manifest.json` keys. Persist an atomic cursor and per-replay retry receipt below `workspace`.

- [ ] **Step 4: Write failing ownership, clip-bound, and malformed-input tests**

  Add `test_manual_transcript_is_never_sent_to_transcriber`, `test_radio_clip_limit_fails_before_transcription`, `test_malformed_provenance_is_quarantined`, and `test_corrupt_checkpoint_is_quarantined`. Assert the published manifest remains unchanged in every quarantine case.

- [ ] **Step 5: Implement deterministic inventory and checkpoint persistence**

  Inventory radio events in `(session_time_ms, event_id)` order. Use job identity `replay ID + source manifest digest + target profile + target policy`; key clip decisions by source event ID, event time, driver, and audio URL. Store no transcript or URL in logs. Persist each terminal decision using flush, fsync, and atomic rename. Treat deferred decisions as retryable with existing 5-minute/15-minute backoff and preserve the old public archive.

- [ ] **Step 6: Write failing resume and budget tests**

  Add `test_restart_reuses_terminal_clip_checkpoints`, `test_three_clip_budget_stops_between_clips`, `test_soft_deadline_is_checked_between_clips`, and `test_policy_change_creates_a_new_job_identity`. Assert a restarted maintainer performs zero duplicate inference for checkpointed clips.

- [ ] **Step 7: Implement resume and bounded progress results**

  Return only `idle`, `progressed`, `deferred`, or `quarantined` until publication exists. A soft deadline is checked before starting the next clip and never kills active inference. Cap checkpoint records at 100.

- [ ] **Step 8: Run maintainer core tests and lint**

  Run: `PYTHONPATH=backend ../../.venv/bin/python -m pytest backend/tests/test_radio_archive_maintenance.py -q`

  Run: `../../.venv/bin/python -m ruff check backend/racelens/radio/archive_maintenance.py backend/tests/test_radio_archive_maintenance.py`

  Expected: all tests pass and Ruff prints no errors.

- [ ] **Step 9: Commit Task 3**

  ```bash
  git add backend/racelens/radio/archive_maintenance.py backend/tests/test_radio_archive_maintenance.py
  git commit -m "feat(radio): checkpoint archive maintenance"
  ```

### Task 4: Archive Invariants and Manifest-Last Commit

**Files:**
- Modify: `backend/racelens/radio/archive_maintenance.py`
- Modify: `backend/tests/test_radio_archive_maintenance.py`
- Modify: `backend/tests/test_object_storage.py`

**Interfaces:**
- Consumes: `publish_archive_revision()` from Task 2 and completed checkpoints from Task 3.
- Produces: terminal `MaintenanceResult(status="published", ...)` and idempotent completion receipts.

- [ ] **Step 1: Add failing archive-transformation invariant tests**

  Add `test_reconciliation_changes_only_machine_radio_events`: compare parsed source and target by stable source event identity; assert event count, every non-radio event, manual radio payloads, track bytes, and positions bytes are unchanged. Assert changed machine radio event IDs equal `make_event_id()` and final ordering is deterministic.

- [ ] **Step 2: Implement final fixture construction and full validation**

  Apply checkpoint decisions to a copy only after all eligible clips are terminal. Run `validate_fixture()` and `validate_archive()`. Refuse publication on event-count change, non-radio mutation, manual mutation, or track/positions hash change.

- [ ] **Step 3: Add failing publication conflict and fault-injection tests**

  Add `test_source_revision_change_aborts_publish`, `test_failure_at_each_revision_upload_keeps_old_manifest`, `test_manifest_failure_keeps_old_revision_visible`, `test_crash_after_manifest_is_repaired_without_inference`, and `test_duplicate_completed_run_creates_zero_uploads`.

- [ ] **Step 4: Implement manifest-last publication and receipts**

  Re-read and compare the source manifest immediately before `publish_archive_revision()`. On success, read-verify the new manifest and objects, then atomically persist a completion receipt and clean only that deterministic workspace. In `publish=False` mode, retain the completed checkpoint and return `progressed` without writing storage.

- [ ] **Step 5: Run all maintenance and storage fault tests**

  Run: `PYTHONPATH=backend ../../.venv/bin/python -m pytest backend/tests/test_radio_archive_maintenance.py backend/tests/test_object_storage.py -q`

  Expected: all tests pass, including every injected publication step.

- [ ] **Step 6: Commit Task 4**

  ```bash
  git add backend/racelens/radio/archive_maintenance.py backend/tests/test_radio_archive_maintenance.py backend/tests/test_object_storage.py
  git commit -m "feat(radio): publish reconciled archive revisions"
  ```

### Task 5: Recorder Idle Scheduling and Initial v2 Publication

**Files:**
- Modify: `backend/racelens/recorder/worker.py`
- Modify: `backend/tests/test_recorder_worker.py`
- Modify: `backend/tests/test_recorder_preparation.py`

**Interfaces:**
- Consumes: `RadioArchiveMaintainer.run_once()`, `MaintenanceBudget`, and `summarize_fixture()`.
- Produces: recorder modes `off`, `dry-run`, and `enabled` through `RECORDER_RADIO_MAINTENANCE` and automatic idle invocation.

- [ ] **Step 1: Add failing configuration tests**

  Assert the default is `off`, exact accepted values are `off|dry-run|enabled`, unknown values fail startup, and the mode is independent from `RECORDER_TRANSCRIBE_RADIO`.

- [ ] **Step 2: Implement recorder configuration and dependency injection**

  Add `radio_maintenance_mode: Literal["off", "dry-run", "enabled"]` to `Config`. Allow `Recorder.__init__()` to accept an optional maintainer for tests; construct the production maintainer only when object storage exists and mode is not `off`. Map `dry-run` to `publish=False`.

- [ ] **Step 3: Add failing scheduling-priority tests**

  Add tests proving maintenance is not called when capture is due, within the two-hour capture guard, while a preparation subprocess is active, while a due processing retry exists, or while a remote request is runnable. Add a test proving one idle pass calls exactly `run_once(MaintenanceBudget(1, 3, 60.0))`.

- [ ] **Step 4: Integrate maintenance after all higher-priority work**

  Invoke maintenance only after capture recovery, due capture, preparation reconciliation/start, and remote preparation return idle. Return a sanitized one-line status containing replay ID and counts, never transcript text or URLs. Keep award synchronization lower priority than maintenance.

- [ ] **Step 5: Add failing initial-publication provenance test**

  In `_build_archive()`/`_publish()` tests, assert a newly transcribed archive passes `summarize_fixture()` to `publish_session()` and produces a v2 manifest already marked with the current radio profile/policy. A current new archive must therefore need zero maintenance inference.

- [ ] **Step 6: Implement initial v2 radio summary publication**

  Compute the summary after durable transcription and final validation. Pass it to `publish_session()` without altering sessions whose radio transcription is disabled.

- [ ] **Step 7: Run recorder tests and lint**

  Run: `PYTHONPATH=backend ../../.venv/bin/python -m pytest backend/tests/test_recorder_worker.py backend/tests/test_recorder_preparation.py -q`

  Run: `../../.venv/bin/python -m ruff check backend/racelens/recorder/worker.py backend/tests/test_recorder_worker.py backend/tests/test_recorder_preparation.py`

  Expected: all tests pass and Ruff prints no errors.

- [ ] **Step 8: Commit Task 5**

  ```bash
  git add backend/racelens/recorder/worker.py backend/tests/test_recorder_worker.py backend/tests/test_recorder_preparation.py
  git commit -m "feat(recorder): maintain radio archives while idle"
  ```

### Task 6: Operator Status, Deployment Controls, and End-to-End Verification

**Files:**
- Modify: `backend/racelens/recorder/status.py`
- Modify: `backend/tests/test_recorder_status.py`
- Modify: `deploy/recorder/install.sh`
- Modify: `deploy/recorder/README.md`

**Interfaces:**
- Consumes: maintainer's bounded status receipt and recorder mode from Task 5.
- Produces: sanitized `radio_maintenance` operator status and documented rollout/rollback procedure.

- [ ] **Step 1: Add failing sanitized-status tests**

  Test current/progressed/deferred/quarantined/published status parsing; reject symlinks, files over 4096 bytes, stale/invalid timestamps, malformed replay IDs, and unexpected keys. Existing status output without a maintenance file remains backward-compatible with `radio_maintenance: null`.

- [ ] **Step 2: Implement bounded operator status**

  Have the maintainer atomically write a fixed-schema `state/radio-maintenance.json`. Extend `recorder_status()` to expose mode, replay ID, status, counts, age, attempt, and retry age only.

- [ ] **Step 3: Update deployment defaults and documentation**

  Add `RECORDER_RADIO_MAINTENANCE=off` only when creating a new env file; never overwrite an existing operator value. Document v1/v2 keys, revision retention, IAM permissions under `sessions/`, the `off → dry-run → enabled` rollout, verification commands, and rollback by restoring the prior verified manifest. Keep session deletion permissions absent.

- [ ] **Step 4: Run the complete local verification suite**

  Run: `PYTHONPATH=backend ../../.venv/bin/python -m pytest backend/tests -q`

  Run: `../../.venv/bin/python -m ruff check backend/racelens backend/tests`

  Run: `git diff --check origin/main...HEAD`

  Expected: all backend tests pass, Ruff is clean, and diff check prints nothing.

- [ ] **Step 5: Run a local deterministic canary**

  Use an in-memory or temporary-directory archive with at least one manual, one accepted, one rejected, and one deferred clip. Run dry-run, restart, enabled publication, and duplicate enabled publication. Record exact inference calls, uploads, source/target SHA-256, and exposed manifest sequence; expected duplicate counts are zero.

- [ ] **Step 6: Commit Task 6**

  ```bash
  git add backend/racelens/recorder/status.py backend/tests/test_recorder_status.py deploy/recorder/install.sh deploy/recorder/README.md
  git commit -m "docs(recorder): expose radio maintenance rollout"
  ```

- [ ] **Step 7: Review, push, and open a PR without enabling production migration**

  Push the feature branch, run the existing GitHub CI matrix, and request whole-branch review focused on manifest compatibility, failure ordering, retry bounds, and Live isolation. Merge only with every required job green.

- [ ] **Step 8: Deploy in dry-run mode and verify one production canary**

  Deploy the merged recorder revision with `RECORDER_RADIO_MAINTENANCE=dry-run`. Confirm healthy container, zero restarts/OOM, unchanged public manifest, bounded checkpoint progress, and no overlap with capture/preparation. Record canary source hash and predicted target summary.

- [ ] **Step 9: Enable one-archive maintenance and verify rollback evidence**

  Set `RECORDER_RADIO_MAINTENANCE=enabled`, allow one stale archive to publish, then verify the v2 manifest and every object hash from the recorder and public API. Confirm Render rematerializes the target revision, duplicate maintenance performs zero inference/uploads, and the prior revision remains verifiable. Stop rollout and restore `off` on any mismatch.
