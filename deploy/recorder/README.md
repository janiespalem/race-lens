# Automated recorder

The Debian worker records every Formula 1 session from FP1 through the race.
It starts races one hour before the published UTC time and every other session
ten minutes early, resumes an interrupted capture, rejects stale or mismatched
SignalR sessions, and keeps raw recordings for recovery. The extra race lead
time only affects local raw data; published archives still begin at the
isolated session boundary. Raw recordings are retained for 14 days by default.
After the archive becomes available it builds the canonical fixture, merges
captured team radio, transcribes it with Whisper, and generates track telemetry
for sessions selected for publication.

By default every recorded session is published to the demo. Change
`RECORDER_PUBLISH_SESSIONS` in the private env file to narrow that policy.

## Install or update

From the server checkout:

```sh
deploy/recorder/install.sh
```

No Compose or sudo is required. The script replaces only the container named
`race-lens-recorder` and never publishes a port. State lives outside Git:

- `~/.config/race-lens-recorder/recorder.env`
- `~/.local/share/race-lens-recorder/data/`
- `~/.local/share/race-lens-recorder/state/`
- `~/.local/share/race-lens-recorder/raw/`

The installer also adds a two-minute host watchdog. Health requires a recent
worker heartbeat and, during capture, a growing raw feed. An unhealthy
container is restarted and resumes the same recording by appending.

## Publication boundary

The container has no repository or SSH key. It can only write an exact fixture,
track, and positions triplet to the staging directory. `publish.py` runs on the
host, validates names, paths, sizes, and the exact file set, then pushes a
single `capture/*` branch. GitHub Actions validates the archive and runs the
full backend, Rust, and frontend checks before fast-forwarding `main`.

This separation means a parser or media dependency compromised inside the
container cannot use Git credentials. The host publisher must be installed by
the operator with its own write-enabled deploy key and cron entry.

Set `RECORDER_GIT_PUBLICATION=1` during storage migration. It keeps this
CI-gated path in parallel with object storage. Change it to `0` only after a
practice, historical qualifying, historical race, duplicate request, and
failure/retry have all passed through production storage.

## Private object storage

Use one private S3-compatible bucket. The browser never talks to it. Add these
values to both Render's secret environment and
`~/.config/race-lens-recorder/recorder.env`; use different credentials for the
two processes:

```sh
RACELENS_S3_ENDPOINT=https://s3-provider.example
RACELENS_S3_REGION=region
RACELENS_S3_BUCKET=race-lens
RACELENS_S3_ACCESS_KEY_ID=...
RACELENS_S3_SECRET_ACCESS_KEY=...
# RACELENS_S3_SESSION_TOKEN=...  # only for temporary credentials
```

Keep the env file at mode `0600`. The bucket stays private and needs no browser
CORS rule.

Render needs `ListBucket` restricted to `requests/`, `status/`, `sessions/`, `awards/`, and
`live/`; `GetObject` on those prefixes; and `PutObject` only on `requests/`. The worker
needs list/read on `requests/` and `status/`, write on `status/`, list/read/write on `live/`,
read/write on `awards/`, and list/read/write on `sessions/`. For `sessions/`, restrict
`s3:ListBucket` to the `sessions/` prefix and grant `s3:GetObject` and
`s3:PutObject` on `sessions/*`, including revision objects. Initial v1 publication
also needs multipart write/copy on `tmp/`; copying requires source read and
destination write. Grant deletion only on `tmp/*`, never on `sessions/*`.
Do not grant `s3:DeleteObject`, `s3:DeleteObjectVersion`, bucket administration,
or public access for session archives. Retained revisions also must not expire
through a bucket lifecycle rule.

Tigris Standard is the preferred free-tier provider for this deployment. Use
endpoint `https://t3.storage.dev` and region `auto`. Its
[free tier](https://www.tigrisdata.com/pricing/) includes 5 GB, 10,000 Class A
requests, 100,000 Class B requests, and free egress; existing Race Lens
triplets are roughly 3–17 MB each. Tigris access keys support custom
[AWS-style IAM policies](https://github.com/tigrisdata/storage/tree/main/packages/cli)
so the two permission sets above can be applied without changing the object
contract or adding a gateway.

Cloudflare R2 has a larger free tier, but its long-lived tokens scope
permissions to buckets rather than mixed per-prefix actions. Its
[prefix-scoped temporary credentials](https://developers.cloudflare.com/r2/api/s3/temporary-credentials/)
expire after at most seven days. Do not silently give Render a permanent
bucket-wide write token: add credential rotation or a gateway before choosing
R2 for the strict one-bucket deployment.

The stable pointer is `sessions/{replay_session_id}/manifest.json`. Both archive
manifest versions remain readable. Version 1 references the three flat objects;
version 2 references the same triplet under an immutable revision prefix:

```text
requests/{canonical_session_id}.json
status/{canonical_session_id}.json
sessions/{replay_session_id}/manifest.json
sessions/{replay_session_id}/events.jsonl
sessions/{replay_session_id}/track.json
sessions/{replay_session_id}/positions.json
sessions/{replay_session_id}/revisions/{revision}/events.jsonl
sessions/{replay_session_id}/revisions/{revision}/track.json
sessions/{replay_session_id}/revisions/{revision}/positions.json
awards/driver-of-the-day/{replay_session_id}.json
live/current.json
live/{canonical_session_id}/snapshot.json
```

Live keys are transient: the snapshot expires every 20 seconds and is
rewritten while `live/current.json` tracks the session lifecycle until the
archive publishes.

Initial v1 publication uploads to `tmp/`, reads each object back, copies verified
files to the flat `sessions/` keys, reads them back again, and writes the final
manifest last. Version 2 publication uploads directly to the revision keys,
verifies every object, checks that the source manifest is unchanged, and replaces
the stable manifest last. Its `revision`, `parent_revision`, `radio_profile`,
`radio_policy`, and `radio_summary` identify the reconciled archive. The first
v2 parent is the source v1 manifest digest; later parents are revision IDs.
Prior v1 files and v2 revision objects are retained, not overwritten or deleted
by radio maintenance. Save each verified manifest outside the bucket pointer:
retaining the objects alone is not a manifest backup.

A missing or invalid manifest is never exposed as `WATCH`. Preparation jobs
retry twice with backoff, then become a safe terminal failure that the user may
explicitly retry. Radio maintenance has separate five-minute then fifteen-minute
retry delays; deferred clips remain retryable and do not become rejections.

## Radio archive maintenance rollout

`RECORDER_RADIO_MAINTENANCE=off` is the default. The installer adds it only to a
new env file and preserves every existing operator value. A missing setting
also means `off`; valid alternatives are `dry-run` and `enabled`. Maintenance
requires configured private object storage and only runs when capture, upcoming
capture guards, local archive processing, and remote preparation leave the worker
idle. Each pass handles at most one archive and three inference calls within a
60-second scheduling budget; an already-running inference call is not forcibly
interrupted. Live transcript handling and Live storage keys are not migrated.

Roll out `off → dry-run → enabled` only after the branch review and all required
CI checks pass. Edit the private env file deliberately, then run the installer
to recreate the container with the new setting. Do not enable maintenance merely
by upgrading the image.

1. In `off`, record the container revision, restart/OOM counters, and a canary's
   verified public manifest and all three object SHA-256 values. Save the exact
   manifest as the rollback pointer and verify its referenced objects. Exclude
   independent writers to that canary during verification.
2. Set `dry-run`. Observe bounded progress, terminal checkpoint reuse across a
   restart, and unchanged public manifest/object hashes. Manual transcripts are
   preserved, accepted/rejected current-policy decisions are reused, and deferred
   clips block publication until they resolve. Check capture/preparation still
   take priority and the container has no new restart or OOM.
3. Set `enabled` for a supervised canary window. There is no replay allowlist or
   stop-after-one switch: each idle pass may select another stale archive. Return
   to `off` after the first verified publication, or pause before proceeding if
   the deployment cannot safely constrain that window. Verify the v2 manifest,
   every target SHA-256, unchanged non-radio/manual content, preserved prior
   objects, and API rematerialization of the new revision. A duplicate pass must
   perform zero inference calls and zero uploads. Any mismatch stops rollout.

Operator checks (no credentials or transcripts are printed by recorder status):

```sh
docker exec race-lens-recorder python -m racelens.cli recorder-status --json
docker inspect race-lens-recorder --format \
  'health={{.State.Health.Status}} restarts={{.RestartCount}} oom={{.State.OOMKilled}} revision={{index .Config.Labels "org.opencontainers.image.revision"}}'
docker logs --tail 100 race-lens-recorder
```

`radio_maintenance` is a bounded, sanitized last-pass receipt from
`state/radio-maintenance.json`: mode (`dry-run`/`enabled`), replay ID, status
(`current`, `progressed`, `deferred`, `quarantined`, `published`), scanned/processed/
reused/accepted/rejected counts, age, attempt, and retry age only. Negative retry
age means seconds until the next attempt. It is `null` on older installs or for
missing, invalid, or more-than-24-hour-old receipts. `off` creates no receipt;
switching back to `off` can leave a recent last-pass receipt, so confirm the actual
setting in the private env file. Checkpoints and retry receipts live under
`state/radio-maintenance/`; they are private and are not operator status output.

Verify a saved manifest and its objects using the deployed storage reader, with
the required credentials already loaded privately into its environment:

```sh
python - /private/path/prior-verified-manifest.json <<'PY'
import json
import sys
from pathlib import Path
from racelens.object_storage import S3Store, StorageConfig, manifest_digest, validate_manifest

manifest = validate_manifest(json.loads(Path(sys.argv[1]).read_text()))
store = S3Store(StorageConfig.from_env())
for row in manifest["files"].values():
    store.verify(row["key"], size=row["size"], sha256=row["sha256"])
print(manifest_digest(manifest))
PY
```

Use the same read-back verification for the current stable manifest. The public
API exposes replay data, not raw object manifests: request `/api/sessions`, then
the canary's `/api/sessions/{replay_session_id}/feed?until_ms={session_end_ms}&limit=100`,
`/api/sessions/{replay_session_id}/track`, and `/api/sessions/{replay_session_id}/positions`
on the configured API origin and compare with the verified target. Render's
local materialization must follow the changed manifest digest; a healthy response
alone does not prove it served the new revision. Local verification commands are:

```sh
PYTHONPATH=backend .venv/bin/python -m pytest backend/tests -q
.venv/bin/python -m ruff check backend/racelens backend/tests
git diff --check origin/main...HEAD
PYTHONPATH=backend .venv/bin/python -m pytest backend/tests/test_radio_archive_maintenance.py -k deterministic_mixed_radio_rollout_canary -q -s
```

For rollback, first set `RECORDER_RADIO_MAINTENANCE=off` and recreate the container.
Exclude other archive writers, validate the saved prior manifest and read-verify
all its objects, then restore that exact manifest to the stable pointer using
the worker's scoped `PutObject` permission. Read it back and compare its manifest
digest and object hashes with the saved verified version. Confirm Render and the
public API rematerialize the prior archive. Keep both revisions for investigation;
do not delete session objects or try to construct a rollback manifest from a
revision ID alone. An image rollback does not restore an archive pointer.

## Container security

The worker runs as the caller's non-root UID/GID with all capabilities dropped,
`no-new-privileges`, a read-only root filesystem, a no-exec `/tmp`, and CPU,
memory, PID, and descriptor limits. Only `data`, `state`, and `raw` are writable.
Docker's default seccomp and AppArmor profiles remain active. The bridge network
allows outbound F1/FastF1/model traffic; there are no inbound ports.

Docker daemon access remains root-equivalent on the host. This deployment does
not pretend otherwise.

## Checks

```sh
docker inspect race-lens-recorder --format \
  'image={{.Config.Image}} revision={{index .Config.Labels "org.opencontainers.image.revision"}} user={{.Config.User}} status={{.State.Status}} health={{.State.Health.Status}} readonly={{.HostConfig.ReadonlyRootfs}}'
docker port race-lens-recorder          # must print nothing
docker logs --tail 100 race-lens-recorder
python3 deploy/recorder/publish.py      # normally invoked by cron
```
