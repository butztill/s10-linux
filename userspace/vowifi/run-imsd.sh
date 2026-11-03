#!/bin/sh
# SPDX-License-Identifier: GPL-2.0-only
# Waits for the tunnel (tun1 in netns ims, address and P-CSCF list in the swu log), then runs imsd there.
cd /home/user/s10-modem/vowifi || exit 1
for i in $(seq 1 60); do
  ip netns exec ims ip -6 -br addr show dev tun1 2>/dev/null | grep -q '::2/64' && grep -q 'P-CSCF IPV6 ADDRESS' swu.log && break
  sleep 2
done
ip netns exec ims ip link set lo up
exec ip netns exec ims python3 -u imsd.py swu.log
