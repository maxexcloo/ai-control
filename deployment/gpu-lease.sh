#!/bin/sh
# Keep the lease through model caches and graceful shutdown, not just generation.
set -u
devices=${AI_GPU_DEVICES:-${HIP_VISIBLE_DEVICES:-}}
case "$devices" in
    0|1|0,1) ;;
    *) echo "Select physical GPU 0, 1, or 0,1." >&2; exit 64 ;;
esac
if [ "${HIP_VISIBLE_DEVICES:-$devices}" != "$devices" ]; then
    echo "GPU reservation and HIP visibility disagree." >&2
    exit 64
fi
if [ "$#" -eq 0 ]; then
    echo "A runtime command is required." >&2
    exit 64
fi
lease_dir=${AI_GPU_LEASE_DIR:-/run/ai-gpu}
acquire() {
    if command -v flock >/dev/null 2>&1; then
        flock -n -x "$1"
    else
        # The stock Radiance image is minimal; use the pinned static utility.
        /opt/ai/busybox flock -n -x "$1"
    fi
}
case "$devices" in
    0|0,1)
        exec 8>>"$lease_dir/gpu0.lock"
        acquire 8 || {
            echo "GPU 0 is reserved. Explicitly stop its runtime before switching." >&2
            exit 75
        }
        ;;
esac
case "$devices" in
    1|0,1)
        exec 9>>"$lease_dir/gpu1.lock"
        acquire 9 || {
            echo "GPU 1 is reserved. Explicitly stop its runtime before switching." >&2
            exit 75
        }
        ;;
esac
"$@" &
runtime_pid=$!
# shellcheck disable=SC2329 # Invoked by the INT/TERM trap below.
forward_stop() { kill -TERM "$runtime_pid" 2>/dev/null || :; }
trap forward_stop INT TERM
# wait can be interrupted by the signal trap; retain locks until the child exits.
while :; do
    wait "$runtime_pid"
    status=$?
    kill -0 "$runtime_pid" 2>/dev/null || break
done
exit "$status"
