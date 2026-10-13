# Automated Radio Archive Maintenance

## Intent

Race Lens must apply a new durable radio-transcription profile to existing replay
archives without an operator downloading, editing, or republishing each archive.
The recorder should discover stale archives during idle time, resume bounded work
after a restart, preserve human-authored text, and expose either the complete old
archive or the complete new revision.

This change automates archive lifecycle and safe abstention. It does not claim
that confidence scores prove a transcript is semantically correct. Without a
labelled reference set, word error rate remains unknown.

## Current Gap

The current durable path already rechecks old machine metadata and preserves
transcripts without metadata as manual. Its quality gate records accepted or
rejected decisions, but it only mutates a local JSONL fixture when explicitly
invoked.

Three existing guarantees prevent unsafe automation but also block upgrades:

- published replay IDs are immutable in `publish_session()`;
- ready preparation jobs cannot be enqueued again;
- a recorder session in `COMPLETE` is terminal.

Consequently, a profile change cannot migrate an already-published replay. The
Singapore archive required a one-off replacement. That maintenance procedure
must not become the product workflow.

## Scope

The first release will:

- discover published archives whose radio profile or policy is obsolete;
- process at most one archive and three clips per recorder pass;
- preserve manual transcripts byte-for-byte;
- distinguish a quality rejection from a temporary processing failure;
- checkpoint terminal clip decisions and resume them after restart;
- validate the complete revised archive before publication;
- publish content-addressed immutable revisions and switch the manifest last;
- retain the previous revision for rollback;
- keep Live transcription and capture scheduling unchanged.

The first release will not:

- use an LLM to rewrite or guess radio messages;
- automatically promote a new model from unlabelled audio;
- delete old archive revisions;
- support multiple concurrent archive publishers;
- rebuild timing, track, or position data solely for a radio migration.

## Deep Module and Interface

The new deep module is `RadioArchiveMaintainer`. Its seam belongs in
`racelens.radio.archive_maintenance`; recorder code only supplies dependencies
and a budget.

```python
@dataclass(frozen=True, slots=True)
class MaintenanceBudget:
    max_archives: int = 1
    max_clips: int = 3
    max_seconds: float = 60.0


@dataclass(frozen=True, slots=True)
class MaintenanceResult:
    status: Literal["idle", "progressed", "published", "deferred", "quarantined"]
    replay_session_id: str | None
    clips_scanned: int
    clips_processed: int
    clips_reused: int
    accepted: int
    rejected: int
    retry_at: datetime | None


class RadioArchiveMaintainer:
    def run_once(self, budget: MaintenanceBudget) -> MaintenanceResult: ...
```

Profile and policy identifiers are constructor configuration, not per-call
parameters. The caller does not coordinate manifest scanning, archive downloads,
clip classification, checkpoints, retries, validation, or publication.

The implementation may use private internal seams for deterministic tests:

- the existing object-store seam, with `S3Store` and an in-memory adapter;
- a final-transcript adapter, with faster-whisper and a scripted test adapter;
- the persistent filesystem, with the recorder data directory and `tmp_path`.

No filesystem protocol or generic workflow engine is introduced.

## Transcript Ownership and Decisions

Every radio row is classified before processing:

- non-empty transcript without metadata: legacy manual text, never changed;
- metadata with the current profile and policy: terminal, reused;
- missing transcript and metadata: unprocessed, eligible;
- known older machine metadata: eligible for reprocessing;
- malformed or ambiguous metadata: quarantine the archive and publish nothing.

The final-transcript adapter returns one of three explicit outcomes:

- `accepted`: text plus bounded quality evidence;
- `rejected`: no public text, a stable reason, and bounded quality evidence;
- `deferred`: network, codec, model, or resource failure that may succeed later.

`deferred` never becomes a quality rejection. A rejected result retains the
audio URL and removes only the machine-generated transcript. Manual text is
never passed to the recognizer.

Machine provenance stores separate profile and policy identifiers. Changing
model options or changing confidence thresholds therefore creates an explicit
new target instead of overloading a single integer version.

## Checkpoint and Idempotence

The deterministic job identity is derived from:

```text
replay ID + source manifest digest + target profile + target policy
```

The workspace is stored below the recorder's persistent data directory. A
bounded JSON checkpoint maps stable clip identities to accepted or rejected
decisions. The clip identity uses the event time, driver, audio URL, and source
event ID. Each checkpoint update uses write, flush, fsync, and atomic rename.

The fixture is rewritten once after all eligible clips have terminal decisions;
it is not rewritten after every clip. A restart reloads the same checkpoint and
does not fetch or transcribe completed clips again. A changed source manifest
creates a different job identity and invalidates the old workspace.

At most 100 radio clips are accepted per archive. Audio remains limited to 10
MiB per clip, and only one audio file is held at a time.

## Archive Manifest and Revisions

Archive manifests receive a dedicated schema version independent from the
shared live and preparation record version.

Schema version 2 adds:

- `revision`: deterministic digest of archive identities, file sizes and hashes,
  radio profile, and radio policy;
- `parent_revision`: the source revision or the digest of a legacy manifest;
- `radio_profile` and `radio_policy`;
- a bounded radio summary containing total, accepted, rejected, and manual counts.

Schema version 1 remains readable. Its fixed object keys remain valid. A legacy
manifest has unknown radio provenance and is inspected once by the maintainer.

New and revised archives use content-addressed keys:

```text
sessions/{replay}/revisions/{revision}/events.jsonl
sessions/{replay}/revisions/{revision}/track.json
sessions/{replay}/revisions/{revision}/positions.json
```

The revision digest is calculated from content hashes and manifest metadata
before keys are constructed, avoiding a circular hash definition. Each revision
is self-contained. This duplicates unchanged track and position objects but
makes rollback and integrity reasoning local to one revision.

The stable key remains:

```text
sessions/{replay}/manifest.json
```

Readers continue to trust only this manifest.

## Reconciliation Flow

One `run_once()` call performs bounded progress in this order:

1. Read a bounded page of replay manifests and the local scan cursor.
2. Select one legacy or stale radio profile outside retry backoff.
3. Load and validate its manifest and verify every referenced object by size and
   SHA-256.
4. Materialize one verified archive into its deterministic workspace.
5. Validate the source fixture and build the radio inventory in deterministic
   event order.
6. Load a matching checkpoint or create an empty one.
7. Process eligible clips until `max_clips` is reached. Check `max_seconds`
   between clips; it is a soft deadline, not an unsafe mid-inference kill.
8. Return `progressed` when eligible clips remain, or `deferred` with bounded
   exponential backoff when an external dependency failed.
9. Once every eligible clip is accepted or rejected, apply decisions to a copy,
   recompute changed event IDs, deterministically sort, and validate the complete
   fixture and archive.
10. Require the event count to remain unchanged and require non-radio events to
    be identical. Track and position content must match the source revision.
11. Re-read the source manifest. If it changed, publish nothing and start a new
    job on a later pass.
12. Upload the new immutable revision, verify each stored object by size and
    SHA-256, then write the stable manifest last.
13. Read back and validate the manifest and all referenced objects before
    recording a completed receipt and removing the workspace.

If transformed bytes are unchanged, the maintainer records a current receipt
without creating a duplicate revision.

## Failure and Concurrency Guarantees

- A crash before the manifest write leaves the previous archive visible and may
  leave an unreferenced immutable revision.
- A crash after the manifest write but before the receipt is repaired by an
  idempotent rescan; current clip decisions cause no new inference or revision.
- Network, model, codec, and temporary storage failures preserve the published
  archive and schedule retry with bounded backoff.
- Corrupt manifests, mismatched hashes, malformed provenance, invalid fixtures,
  and invariant violations quarantine the job and publish nothing.
- The source manifest is compared again immediately before commit. The recorder
  remains the single archive writer. Strict multi-writer support requires a
  provider-tested conditional `If-Match` write and is outside this release.
- No transcript text, audio URL, provider response, or credential is written to
  operational logs.

Manifest-last publication no longer requires the temporary invalid manifest
used during the Singapore repair. Public readers see a complete old revision
until the complete new revision is ready.

## Recorder Scheduling

Maintenance runs only when:

- no capture is due;
- no replay preparation subprocess is active;
- no remote preparation request is runnable;
- the next capture is outside the existing two-hour capture guard.

Capture and normal archive preparation always take priority. The Live
`TranscriptWorker` remains a separate fast, non-blocking path without word-level
inspection. Maintenance uses only the strict durable profile after the race.

The first release processes one archive and at most three clips per pass with a
single recognizer. Existing measurements of approximately 12--17 seconds per
strict clip imply roughly 36--51 seconds of inference for a full three-clip
pass after model load; this is measured locally, not a production latency SLA.

## Observability

Structured maintenance results and health output expose only identifiers,
counts, durations, and reasons:

- archives scanned, current, progressed, published, deferred, or quarantined;
- clips reused, accepted, rejected by reason, or deferred;
- checkpoint resumes and duplicate work avoided;
- source and target revisions;
- bytes read and written;
- transcription and publication duration;
- attempt count and next retry time.

No metric claims a word-error improvement until a labelled evaluation set has
been run. The existing private evaluation command remains the promotion gate
for model changes, but maintenance itself does not require manual labels.

## Verification Strategy

Tests exercise the same `run_once()` interface used by the recorder, with the
in-memory object store, temporary filesystem, and scripted recognizer.

Required behavioural tests:

- a legacy v1 archive migrates to a readable v2 immutable revision;
- a current archive performs zero audio downloads, inference calls, and uploads;
- manual text remains byte-for-byte identical;
- accepted, rejected, and deferred outcomes remain distinct;
- a deferred outcome preserves the currently published machine text;
- restart after every clip resumes without retranscribing completed clips;
- duplicate runs create zero duplicate revisions;
- changing only the policy makes the archive eligible again;
- a source-manifest conflict publishes nothing;
- injected failure at every upload, verification, and manifest step exposes no
  mixed archive revision;
- archive event count and every non-radio event remain unchanged;
- a due capture or preparation prevents maintenance from starting;
- `RemoteSessionCache` rematerializes after the manifest revision changes;
- old v1 manifests remain readable by the API.

One real archive is first run in dry-run mode, then migrated as a canary. Its old
and new hashes, radio decision counts, restart behaviour, and rollback path are
recorded before the remaining stale archives are eligible.

## Delivery Sequence

1. Separate final transcript decisions from transient processing failures.
2. Implement the maintainer against local archives with checkpoint and invariant
   tests.
3. Add backward-compatible archive manifest v2 and revision publication with
   publication fault injection.
4. Integrate bounded scanning and idle scheduling into the recorder.
5. Deploy with migration disabled and run one production canary dry-run.
6. Enable one-archive maintenance, verify the canary revision, restart recovery,
   and Render cache refresh.
7. Leave old revision garbage collection disabled until a separate retention
   design is reviewed.

## Acceptance Criteria

The feature is accepted when all of the following are demonstrated:

- a stale archive reaches the current profile without an operator editing or
  republishing files;
- killing the worker after a processed clip resumes without repeating that clip;
- a repeated completed run performs zero inference and creates no revision;
- injected publication failures expose zero mixed revisions;
- manual transcripts and non-radio events are unchanged;
- Live and capture continue while maintenance remains idle or preempted;
- the public API rematerializes and serves the new revision after the manifest
  switch;
- the previous revision remains independently verifiable for rollback.
