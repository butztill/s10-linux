#!/bin/sh
# SPDX-License-Identifier: GPL-2.0-only
# Lower MIF (memory) and INT bus clocks while the panel is off, restore them when it is on.
# Measured screen-off: MIF 2093/INT 534 MHz ~336 mA, MIF 421/INT 200 MHz ~273 mA (docs/NOTE-idle-power-ufs-20261027.md).
# The GPU needs >= 676 MHz MIF, so the low rates are only used with the panel off.
set -u
P=/sys/module/s10_mif_dvfs/parameters
DPMS=/sys/class/drm/card1-DSI-1/dpms
LOW_MIF=421000000 LOW_INT=200000000
HIGH_MIF=2093000000 HIGH_INT=534000000

set_rates() {
    echo "$1" > $P/mif_hz && echo "$2" > $P/int_hz
}

state=
while :; do
    if [ "$(cat $DPMS 2>/dev/null)" = Off ]; then want=low; else want=high; fi
    if [ "$want" != "$state" ]; then
        if [ $want = low ]; then
            set_rates $LOW_MIF $LOW_INT
        else
            set_rates $HIGH_MIF $HIGH_INT
        fi && state=$want && logger -t s10-mif-idle "MIF/INT $want"
    fi
    sleep 2
done
