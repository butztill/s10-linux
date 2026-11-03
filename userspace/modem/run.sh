#!/bin/sh
# SPDX-License-Identifier: GPL-2.0-only
# usage (root): run.sh <cmd>   -- sends cmd to s10cp and persists the kernel log before returning
echo "$1" > /sys/kernel/debug/s10cp/ctl
dmesg | tail -${2:-25} > /home/user/s10-modem/last.log
sync
cat /home/user/s10-modem/last.log
