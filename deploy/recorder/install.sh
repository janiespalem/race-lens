#!/bin/sh
set -eu

container=race-lens-recorder
script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
repo_dir=$(CDPATH= cd -- "$script_dir/../.." && pwd)
config_dir=${XDG_CONFIG_HOME:-"$HOME/.config"}/race-lens-recorder
storage_dir=${XDG_DATA_HOME:-"$HOME/.local/share"}/race-lens-recorder
env_file=$config_dir/recorder.env

command -v docker >/dev/null || { echo "docker is required" >&2; exit 1; }
[ "$(id -u)" -ne 0 ] || { echo "run as the unprivileged recorder user, not root" >&2; exit 1; }
docker info >/dev/null 2>&1 || { echo "current user cannot access Docker" >&2; exit 1; }
[ -f "$repo_dir/backend/pyproject.toml" ] || { echo "run from the Race Lens checkout" >&2; exit 1; }

install -d -m 0700 "$config_dir" "$storage_dir/data" "$storage_dir/state" "$storage_dir/raw"
if [ ! -e "$env_file" ]; then
    umask 077
    printf '%s\n' \
        'RECORDER_INTERVAL_SEC=120' \
        'RECORDER_CAPTURE_POLL_SEC=5' \
        'RECORDER_RAW_RETENTION_DAYS=14' \
        'RECORDER_PUBLISH_SESSIONS=FP1,FP2,FP3,Q,SQ,Sprint,R' \
        'RECORDER_TRANSCRIBE_RADIO=1' \
        'RECORDER_RADIO_MAINTENANCE=off' \
        'RECORDER_GIT_PUBLICATION=1' > "$env_file"
fi
chmod 0600 "$env_file"

tag=$(git -C "$repo_dir" rev-parse --short=12 HEAD 2>/dev/null || echo local)
revision=$(git -C "$repo_dir" rev-parse HEAD 2>/dev/null || echo unknown)
image=race-lens-recorder:$tag
docker build --build-arg "VCS_REF=$revision" -f "$script_dir/Dockerfile" -t "$image" "$repo_dir"
docker rm -f "$container" >/dev/null 2>&1 || true
docker run -d \
    --name "$container" \
    --hostname "$container" \
    --restart unless-stopped \
    --init \
    --user "$(id -u):$(id -g)" \
    --cap-drop ALL \
    --security-opt no-new-privileges:true \
    --read-only \
    --tmpfs /tmp:rw,noexec,nosuid,nodev,size=256m \
    --pids-limit 256 \
    --memory 6g \
    --cpus 2.0 \
    --ulimit nofile=2048:2048 \
    --stop-timeout 45 \
    --network bridge \
    --env-file "$env_file" \
    --mount "type=bind,src=$storage_dir/data,dst=/var/lib/race-lens-recorder/data" \
    --mount "type=bind,src=$storage_dir/state,dst=/var/lib/race-lens-recorder/state" \
    --mount "type=bind,src=$storage_dir/raw,dst=/var/lib/race-lens-recorder/raw" \
    --log-driver journald \
    --log-opt tag=race-lens-recorder \
    "$image"

docker ps --filter "name=^/${container}$" \
    --format 'name={{.Names}} image={{.Image}} status={{.Status}} ports={{.Ports}}'

watchdog="$script_dir/watchdog.sh"
watchdog_log="$storage_dir/watchdog.log"
watchdog_marker='# race-lens-recorder-watchdog'
watchdog_entry="*/2 * * * * $watchdog >> $watchdog_log 2>&1 $watchdog_marker"
(
    crontab -l 2>/dev/null | grep -Fv "$watchdog_marker" || true
    printf '%s\n' "$watchdog_entry"
) | crontab -
