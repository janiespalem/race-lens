# Live capture and replay preparation isolation implementation plan

**Goal:** Publish qualifying Live snapshots and ensure replay preparation can never make the recorder wait before starting a scheduled capture.

**Architecture:** Keep capture and Live publication in the coordinator. Move the existing replay preparation call into one supervised child-process module; the coordinator alone persists recorder state and may preempt the child when capture becomes due. The existing archive builder, validators, object-storage publisher, and deployment topology remain unchanged.

**Tech stack:** Python 3.11/3.12, `subprocess`, pytest, existing recorder state store and S3-compatible object storage.

**Spec:** `docs/design/2026-09-25-live-replay-isolation.md`

## Global constraints

- Public Live session kinds are exactly `R`, `Sprint`, `Q`, and `SQ`.
- Practice sessions remain recordable and replayable but not public Live sessions.
- At most one replay preparation child may run.
- Only the coordinator writes `recorder.json`.
- Existing archive validation, atomic publication, retry, and retention behavior stays in force.
- No database, message broker, second container, or new dependency.
- Capture preempts background replay preparation when both need the recorder host.

## Review focus

- A preparation child exits between `poll()` and a due capture: the outcome is reconciled once, not converted into a false preemption failure.
- `Popen` fails after state selection: the session becomes retryable processing work and is not left permanently in `PROCESSING`.
- A coordinator restart sees persisted `PROCESSING` with no child: exactly one replacement child starts.
- A child spawns FastF1 or Rust descendants: preemption terminates the complete process group.
- An old active-marker file survives an unclean stop: startup removes it before status can report a phantom child.

---

### Task 1: Make qualifying an explicit Live session

**Files:**

- Modify: `backend/racelens/recorder/worker.py`
- Modify: `backend/tests/test_live_bridge.py`

**Interfaces:**

- Produces: `LIVE_SESSION_KINDS == frozenset({"R", "Sprint", "Q", "SQ"})`.
- Preserves: `Config.publish_sessions` remains the independent durable-archive policy.

- [ ] **Step 1: Add a failing policy test**

Add a parametrized test that uses the existing `_config`, `Recorder`, and `MemoryStore` helpers:

```python
@pytest.mark.parametrize("kind", ["R", "Sprint", "Q", "SQ"])
def test_public_live_policy_includes_competitive_sessions(tmp_path, kind):
    session = replace(SESSION, kind=kind)
    recorder = Recorder(_config(tmp_path), object_store=MemoryStore())
    assert recorder._is_live_session(session)


@pytest.mark.parametrize("kind", ["FP1", "FP2", "FP3"])
def test_public_live_policy_excludes_practice(tmp_path, kind):
    session = replace(SESSION, kind=kind)
    recorder = Recorder(_config(tmp_path), object_store=MemoryStore())
    assert not recorder._is_live_session(session)
```

- [ ] **Step 2: Run the focused test and observe the missing interface**

Run:

```bash
cd backend
../.venv/bin/pytest -q tests/test_live_bridge.py -k public_live_policy
```

Expected: failure because `Recorder._is_live_session` does not exist.

- [ ] **Step 3: Centralize the policy and replace direct set checks**

In `worker.py`:

```python
LIVE_SESSION_KINDS = frozenset({"R", "Sprint", "Q", "SQ"})


@staticmethod
def _is_live_session(session: ScheduledSession) -> bool:
    return session.kind in LIVE_SESSION_KINDS
```

Use `_is_live_session(session)` in `_publish_live_snapshot`, `_set_live_status`,
`_finish_live`, and `capture` instead of repeating membership checks.

- [ ] **Step 4: Prove the policy and existing Live lifecycle pass**

Run:

```bash
cd backend
../.venv/bin/pytest -q tests/test_live_bridge.py
```

Expected: all tests pass, including Q/SQ and practice policy cases.

- [ ] **Step 5: Commit the isolated contract change**

```bash
git add backend/racelens/recorder/worker.py backend/tests/test_live_bridge.py
git commit -m "fix(recorder): publish qualifying live sessions"
```

### Task 2: Add the single-flight preparation subprocess module

**Files:**

- Create: `backend/racelens/recorder/preparation.py`
- Create: `backend/racelens/recorder/preparation_job.py`
- Create: `backend/tests/test_recorder_preparation.py`

**Interfaces:**

- Produces: `PreparationOutcome(session_id: str, error: str | None)`.
- Produces: `SubprocessPreparationRunner.start(session) -> bool`.
- Produces: `SubprocessPreparationRunner.poll() -> PreparationOutcome | None`.
- Produces: `SubprocessPreparationRunner.stop(reason) -> PreparationOutcome | None`.
- Produces: `SubprocessPreparationRunner.active_session_id -> str | None`.
- The child command calls the unchanged `Recorder.process(session)` and never writes recorder state.

- [ ] **Step 1: Write runner tests with a fake `Popen` process**

Cover all five review-focus cases in `test_recorder_preparation.py`. The core expectations are:

```python
def test_runner_is_single_flight(tmp_path):
    runner, processes = make_runner(tmp_path)
    assert runner.start(SESSION)
    assert not runner.start(OTHER_SESSION)
    assert len(processes) == 1
    assert runner.active_session_id == SESSION.session_id


def test_poll_reports_one_terminal_outcome_and_clears_marker(tmp_path):
    runner, processes = make_runner(tmp_path)
    runner.start(SESSION)
    processes[0].returncode = 0
    assert runner.poll() == PreparationOutcome(SESSION.session_id, None)
    assert runner.poll() is None
    assert runner.active_session_id is None
    assert not (tmp_path / "preparation-active.json").exists()


def test_stop_terminates_process_group_and_reports_preemption(tmp_path, monkeypatch):
    runner, processes = make_runner(tmp_path)
    runner.start(SESSION)
    killed = []
    monkeypatch.setattr(os, "killpg", lambda pid, sig: killed.append((pid, sig)))
    outcome = runner.stop("preempted by scheduled capture")
    assert outcome == PreparationOutcome(
        SESSION.session_id, "preempted by scheduled capture"
    )
    assert killed == [(processes[0].pid, signal.SIGTERM)]
```

Also assert that a constructor removes a stale marker and that a `Popen` exception leaves no marker or active session.

- [ ] **Step 2: Run the new tests and confirm imports fail**

Run:

```bash
cd backend
../.venv/bin/pytest -q tests/test_recorder_preparation.py
```

Expected: collection failure because `racelens.recorder.preparation` does not exist.

- [ ] **Step 3: Implement the deep runner module**

Implement `PreparationOutcome` exactly as below, followed by
`SubprocessPreparationRunner` with constructor
`(state_dir: Path, *, popen: Callable = subprocess.Popen)`, read-only property
`active_session_id`, and methods `start`, `poll`, `stop`, and `close` matching the
signatures listed in the Interfaces block above:

```python
@dataclass(frozen=True, slots=True)
class PreparationOutcome:
    session_id: str
    error: str | None
```

`start()` uses `start_new_session=True` and this argument order:

```python
[
    sys.executable,
    "-m",
    "racelens.recorder.preparation_job",
    str(session.year),
    str(session.round_number),
    session.event_name,
    session.kind,
    session.starts_at.isoformat(),
]
```

Write `preparation-active.json` atomically only after `Popen` succeeds. `poll()` maps exit code `0` to `error=None` and every other code to `"preparation exited with status N"`. `stop()` sends SIGTERM to the child's process group, waits up to 30 seconds, then SIGKILLs the group and reaps it. Every terminal path removes the marker and clears ownership before returning.

- [ ] **Step 4: Implement the child entry point**

In `preparation_job.py`, parse the five arguments, require a timezone-aware ISO timestamp, reconstruct `ScheduledSession`, and call:

```python
Recorder(Config.from_env()).process(session)
```

Do not load or transition `StateStore` in this module. Let an exception produce a non-zero child exit and a traceback in container logs.

- [ ] **Step 5: Run runner tests and static checks**

```bash
cd backend
../.venv/bin/pytest -q tests/test_recorder_preparation.py
../.venv/bin/ruff check racelens/recorder/preparation.py \
  racelens/recorder/preparation_job.py tests/test_recorder_preparation.py
```

Expected: all tests and Ruff pass.

- [ ] **Step 6: Commit the module**

```bash
git add backend/racelens/recorder/preparation.py \
  backend/racelens/recorder/preparation_job.py \
  backend/tests/test_recorder_preparation.py
git commit -m "feat(recorder): supervise replay preparation process"
```

### Task 3: Make the coordinator non-blocking and capture-first

**Files:**

- Modify: `backend/racelens/recorder/worker.py`
- Modify: `backend/tests/test_recorder_worker.py`

**Interfaces:**

- Consumes: the `PreparationOutcome` and runner interface from Task 2.
- Produces: `Recorder(preparation_runner=runner)` dependency injection.
- Produces: `_reconcile_preparation() -> str | None` and `_preempt_preparation() -> None`.
- Changes: processing work is started, polled, and persisted by the coordinator without calling `process()` inline.

- [ ] **Step 1: Replace synchronous expectations with coordinator lifecycle tests**

Add a test fake with explicit control:

```python
class FakePreparationRunner:
    def __init__(self):
        self.active_session_id = None
        self.starts = []
        self.outcome = None

    def start(self, session):
        if self.active_session_id is not None:
            return False
        self.active_session_id = session.session_id
        self.starts.append(session.session_id)
        return True

    def poll(self):
        outcome, self.outcome = self.outcome, None
        if outcome is not None:
            self.active_session_id = None
        return outcome

    def stop(self, reason):
        if self.active_session_id is None:
            return None
        outcome = PreparationOutcome(self.active_session_id, reason)
        self.active_session_id = None
        return outcome

    def close(self):
        self.stop("coordinator stopped")
```

Tests must demonstrate:

```python
assert recorder.run_once() == f"processing started: {older.session_id}"
assert runner.active_session_id == older.session_id

# Advance to a due capture without completing preparation.
clock[0] = later.capture_from
assert recorder.run_once() == f"captured: {later.session_id}"
assert captures == [later.session_id]
assert recorder.store.load().sessions[older.session_id].retry_phase is Phase.PROCESSING
```

Also cover success, non-zero failure, no second start, `Popen`/runner-start failure, and restart from persisted `PROCESSING`.
Add a race regression in which `poll()` returns a successful outcome immediately
before a due capture: assert the old session becomes `COMPLETE`, `stop()` is not
called for it, and the new capture still starts. Add an object-store regression in
which preparation fails while a different session owns `live/current.json`: assert
the pointer remains byte-for-byte unchanged.

- [ ] **Step 2: Run the focused worker tests and observe synchronous behavior**

```bash
cd backend
../.venv/bin/pytest -q tests/test_recorder_worker.py -k 'processing or preparation or capture_preempts'
```

Expected: failures because `Recorder` still calls `process()` inline and has no runner injection.

- [ ] **Step 3: Inject the runner and reconcile it at the start of each tick**

Extend the constructor without changing existing callers:

```python
def __init__(
    self,
    config: Config,
    *,
    now: Callable[[], datetime] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    object_store: object | None = None,
    preparation_runner: PreparationRunner | None = None,
) -> None:
```

Create `SubprocessPreparationRunner(config.state_dir)` only when no runner is supplied. `_reconcile_preparation()` polls once; success transitions `PROCESSING -> COMPLETE`, failure transitions `PROCESSING -> FAILED` with `ARCHIVE_RETRY`, and either returns the existing human-readable result string.

- [ ] **Step 4: Preempt active preparation before a due capture**

Immediately before either recording-resume or selected due-capture execution, poll once more. If the child is still active, stop it with `"preempted by scheduled capture"` and persist a processing retry before entering `capture(session)`. If `poll()` returned a completed result, reconcile that result and do not write a preemption failure.

- [ ] **Step 5: Replace inline processing with single-flight start**

For an eligible `CAPTURED`, `PROCESSING`, or due processing-retry session:

```python
self.store.transition(session_id, Phase.PROCESSING, now)
try:
    started = self.preparation_runner.start(session)
except Exception as exc:
    self.store.transition(
        session_id,
        Phase.FAILED,
        self.now(),
        error=str(exc),
        retry_at=self.now() + ARCHIVE_RETRY,
    )
    return f"processing failed: {session_id}: {exc}"
if not started:
    return "processing busy"
return f"processing started: {session_id}"
```

Do not start another session while `active_session_id` is non-null. Keep the capture guard, retention protection, remote queue, and archive implementation unchanged.

- [ ] **Step 6: Close child ownership on worker shutdown**

Wrap `run_forever()` in `try/finally` and call `preparation_runner.close()` in the `finally` block. Do not mark the stopped session complete; persisted `PROCESSING` is the restart signal.

- [ ] **Step 7: Run the full recorder suite**

```bash
cd backend
../.venv/bin/pytest -q \
  tests/test_recorder_worker.py \
  tests/test_recorder_state.py \
  tests/test_recorder_feed.py \
  tests/test_recorder_health.py \
  tests/test_recorder_preparation.py \
  tests/test_live_bridge.py
```

Expected: all tests pass; no test waits for a real subprocess or FastF1 network call.

- [ ] **Step 8: Commit the coordinator change**

```bash
git add backend/racelens/recorder/worker.py backend/tests/test_recorder_worker.py
git commit -m "refactor(recorder): isolate replay preparation from capture"
```

### Task 4: Expose preparation ownership without weakening health

**Files:**

- Modify: `backend/racelens/recorder/status.py`
- Modify: `backend/tests/test_recorder_status.py`
- Modify: `backend/racelens/recorder/health.py`
- Modify: `backend/tests/test_recorder_health.py`

**Interfaces:**

- Consumes: `state/preparation-active.json` created by Task 2.
- Produces: sanitized status field `preparation` with `session_id` and marker age.
- Preserves: health still requires a fresh coordinator heartbeat and fresh raw data during `RECORDING`.

- [ ] **Step 1: Add failing sanitized-status tests**

Write an active marker and assert:

```python
status = recorder_status(tmp_path, now)
assert status["preparation"] == {
    "session_id": SESSION.session_id,
    "age_seconds": 0.0,
}
```

Add malformed JSON, symlink, unknown session ID, and stale-marker cases; all must return `preparation: None` without exposing paths or raw error text.

- [ ] **Step 2: Implement bounded marker parsing**

Read at most 4096 bytes, require a regular non-symlink file, validate the existing session-id pattern, parse `started_at` as a timezone-aware timestamp, and return only `session_id` plus non-negative age. Add `preparation: None` to empty and state-unavailable status shapes for a stable interface.

- [ ] **Step 3: Keep health tied to coordinator liveness**

Add a regression test where `preparation-active.json` is fresh but `heartbeat` is older than `MAX_PROCESSING_AGE`; health must still fail. Preserve the existing extended processing heartbeat allowance and raw freshness check.

- [ ] **Step 4: Run status and health tests**

```bash
cd backend
../.venv/bin/pytest -q tests/test_recorder_status.py tests/test_recorder_health.py
../.venv/bin/ruff check racelens/recorder/status.py racelens/recorder/health.py \
  tests/test_recorder_status.py tests/test_recorder_health.py
```

Expected: all tests and Ruff pass.

- [ ] **Step 5: Commit observability**

```bash
git add backend/racelens/recorder/status.py backend/racelens/recorder/health.py \
  backend/tests/test_recorder_status.py backend/tests/test_recorder_health.py
git commit -m "feat(recorder): report background preparation ownership"
```

### Task 5: Regression verification and production UAT preparation

**Files:**

- Modify only if verification exposes a defect in Tasks 1–4.

**Interfaces:**

- Verifies: backend, recorder image, shell syntax, and clean repository state.
- Produces: measured local scheduling evidence; production capture timing remains a later UAT result, not an invented metric.

- [ ] **Step 1: Run all backend tests and Ruff**

```bash
cd backend
../.venv/bin/pytest -q tests/
../.venv/bin/ruff check racelens tests
```

Expected: all tests and Ruff pass.

- [ ] **Step 2: Run repository integrity checks**

```bash
git diff --check
sh -n deploy/recorder/run.sh deploy/recorder/install.sh deploy/recorder/watchdog.sh
docker build -f deploy/recorder/Dockerfile --build-arg VCS_REF="$(git rev-parse HEAD)" .
```

Expected: clean diff, valid shell, successful recorder image build.

- [ ] **Step 3: Run the deterministic concurrency regression repeatedly**

```bash
cd backend
../.venv/bin/pytest -q tests/test_recorder_worker.py \
  -k 'capture_preempts or preparation' --count=20
```

If `pytest-repeat` is not installed, use a shell loop around the same pytest command without modifying dependencies. Record the number of runs and failures; the honest CV evidence is only the observed result.

- [ ] **Step 4: Inspect the complete branch diff**

```bash
git status --short
git diff origin/main...HEAD --stat
git diff origin/main...HEAD
```

Confirm there are no credentials, generated files, temporary local artifacts, unnecessary comments, or unrelated changes.

- [ ] **Step 5: Final review gate**

Review specifically for state-transition races, process-group leaks, object-storage pointer regressions, and health false positives. Fix findings in focused commits and rerun the affected tests plus the full backend suite.

- [ ] **Step 6: Release only after review**

Push the branch, run CI, merge only when all required jobs pass, deploy Render and the recorder at the same merge SHA, then verify health, revision, restart count, OOM state, user, read-only rootfs, ports, and memory limit. During the next real session, record scheduled capture, actual capture, first raw event, and first Live snapshot timestamps.
