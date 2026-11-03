#!/bin/sh
# SPDX-License-Identifier: GPL-2.0-only
# Enable / disable the deferred CPU C2 idle state on all CPUs (kernel has the group-exclusion workaround).
set -eu
v=${1:?enable|disable}
case "$v" in enable) val=0;; disable) val=1;; *) exit 2;; esac
for f in /sys/devices/system/cpu/cpu[0-9]*/cpuidle/state1/disable; do
    [ -w "$f" ] && echo $val > "$f" || true
done
logger -t s10-c2 "C2 $v"
