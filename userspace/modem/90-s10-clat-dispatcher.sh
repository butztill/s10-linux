#!/bin/sh
# SPDX-License-Identifier: GPL-2.0-only
# The normal NM connection owns addresses/DNS; only translation lives here.
set -eu
case "${DEVICE_IP_IFACE:-$1}" in rmnet0|ttyCP0) ;; *) exit 0 ;; esac
case "$2" in
    up) systemctl --no-block restart s10-clat.service ;;
    pre-down|down) systemctl --no-block stop s10-clat.service ;;
esac
