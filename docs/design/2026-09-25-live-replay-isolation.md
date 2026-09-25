# Live capture and replay preparation isolation

## Intent

Race Lens must expose an active Formula 1 race, sprint, qualifying, or sprint
qualifying session without waiting for an older session's replay preparation.
Live availability depends only on the live timing transport and object storage,
not on FastF1 canonical data becoming available.

Success means:

- `R`, `Sprint`, `Q`, and `SQ` publish Live snapshots from the captured raw feed;
- a slow or failed replay preparation job cannot delay the next scheduled capture;
- at most one replay preparation job runs at a time;
- recorder restarts do not lose captured data or create two preparation jobs;
- the existing atomic archive validation and publication guarantees remain intact.

## Terms

- **Capture** acquires the upstream SignalR feed and appends it to the durable raw
  recording.
- **Live publication** derives an expiring snapshot from the current raw recording
  and writes it to object storage while capture is active.
- **Replay preparation** builds and validates the durable canonical archive after
  capture. It may legitimately wait for delayed FastF1 data and retry later.
- **Coordinator** is the only owner of recorder state transitions. It schedules
  capture and supervises replay preparation.
- **Preparation runner** is a bounded module that executes one replay preparation
  child process and reports its result to the coordinator.

Live publication is not replay preparation. A missing canonical replay must never
make a valid raw Live feed unavailable.

## Current failure

Two independent defects were visible during Azerbaijan 2026 qualifying:

1. `Q` was included in `RECORDER_PUBLISH_SESSIONS`, but omitted from
   `LIVE_SESSION_KINDS`. The raw feed was recorded, but no Live snapshot could be
   published regardless of server speed.
2. Replay preparation runs synchronously inside the recorder loop. A long FastF1,
   transcription, track, or resampling step can therefore prevent the coordinator
   from starting another capture until that step returns.

The first SignalR connection also timed out, but the existing retry recovered and
recorded the session. After capture, canonical FastF1 data was not ready; archive
validation correctly rejected a one-event fixture. That failure must remain a
retryable replay-preparation failure rather than affect Live.

## Design

### Live session contract

Use one explicit live-session policy containing `R`, `Sprint`, `Q`, and `SQ`.
Practice sessions remain recordable and replayable but do not become public Live
sessions in this change.

The policy is tested independently from the archive publication policy so a future
intentional difference is visible rather than accidental.

### Coordinator and preparation runner

The coordinator keeps the existing priority order for capture. Replay preparation
moves behind a small `PreparationRunner` interface:

```text
start(session) -> started | already_running
poll()          -> idle | running | succeeded | failed
close()         -> stop owned child
```

The production adapter uses a child process. The child performs the existing
`process(session)` pipeline without changing its archive-building semantics. The
coordinator starts at most one child and returns to its normal scheduling loop
immediately. Tests use a controllable fake runner through the same seam.

Only the coordinator writes `recorder.json`:

```text
CAPTURED -> PROCESSING -> COMPLETE
                      -> FAILED(retry_phase=PROCESSING)
```

The child returns a small result record or exit status; it never changes the state
store. This prevents concurrent JSON writers and keeps state transitions ordered.

### Scheduling and ownership

On every coordinator tick:

1. Reconcile the owned preparation child and persist a completed result.
2. Resume an interrupted `RECORDING` session or start a due capture.
3. If no capture is due, start one eligible preparation job when no child is active
   and the existing capture guard permits it.
4. Continue polling without waiting for preparation completion.

Capture remains synchronous in the coordinator because it owns one upstream feed
for the duration of a session. Live snapshot publication remains inside that loop
and is attempted at the existing cadence.

The capture guard prevents starting new heavy preparation shortly before a session.
If a preparation child is already running when a capture becomes due, capture still
starts. Preparation is best-effort background work; Live has priority.

### Restart recovery

The child process is owned by the recorder container and receives termination when
the coordinator exits. On startup, a persisted `PROCESSING` session with no owned
child is eligible to start again. Existing preparation stages are atomic and
idempotent, so partial temporary artifacts are replaced or revalidated rather than
published.

No process identifier is persisted. A PID can be reused and is not sufficient proof
of ownership after restart.

### Failure handling

- SignalR failure follows the existing bounded capture retry policy.
- A Live snapshot failure is logged and retried on the next capture poll; raw capture
  continues.
- A preparation child failure records the existing processing retry timestamp.
- A canonical FastF1 fixture with insufficient laps remains rejected and retryable.
- An unhandled coordinator failure must terminate or reap its owned preparation
  child during shutdown.

### Observability

Recorder health and logs distinguish `capture`, `live_publish`, and
`replay_preparation`. The status output exposes whether a preparation child is
running and which session it owns. Live health continues to derive freshness from
raw-file growth and snapshot expiry, not preparation status.

## Verification

Automated tests must prove:

- `Q` and `SQ` publish Live snapshots, while practice sessions do not;
- a running fake preparation job does not prevent a due capture from starting;
- no second preparation job starts while one is running;
- success and failure are persisted by the coordinator with the existing phases;
- restart from `PROCESSING` relaunches preparation exactly once;
- a delayed FastF1 response cannot change or remove an active Live pointer;
- current recorder, Live lifecycle, object-storage, and archive validation tests
  remain green.

A production UAT should record timestamps for preparation start, scheduled capture,
actual capture, first raw event, and first Live snapshot. The valuable result is the
measured capture-start delay while preparation is running; no zero-delay claim is
made before that measurement exists.

## Non-goals

- No FastF1 rewrite, message broker, database, or additional deployment service.
- No concurrent preparation jobs.
- No public Live mode for practice sessions.
- No change to replay archive contents, validation thresholds, or retention.
- No claim that asynchronous preparation makes upstream canonical data available
  sooner.
