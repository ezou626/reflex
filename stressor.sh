#!/usr/bin/env bash
set -euo pipefail

CGDIR="/sys/fs/cgroup/reflex_stressor_$$"
sudo mkdir -p "$CGDIR"
CGID=$(stat -c %i "$CGDIR")
bash -c "$*" &
PID=$!
echo "$PID" | sudo tee "$CGDIR/cgroup.procs" > /dev/null
echo "$CGID" >> /tmp/reflex_cgroups
