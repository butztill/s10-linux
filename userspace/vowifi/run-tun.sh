#!/bin/sh
# SPDX-License-Identifier: GPL-2.0-only
# Starts the SWu/IKEv2 tunnel to the Telekom ePDG inside network namespace "ims" (VoWiFi/VoLTE-independent IMS access).
# Preferred path: mobile data (internet bearer + 464XLAT, IPv4 192.0.0.1 on s10clat); fallback: Wi-Fi (wlan0 IPv4).
cd /home/user/s10-modem/vowifi || exit 1
umask 077
ip netns del ims 2>/dev/null
if ! ip -6 -o addr show dev rmnet0 scope global 2>/dev/null | grep -q .; then
  nmcli con up 'S10 Telekom LTE' >/dev/null 2>&1
fi
SRC=
for i in $(seq 1 30); do
  [ -e /home/user/s10-modem/vowifi/force-wifi ] && break
  if ip -4 -o addr show dev s10clat 2>/dev/null | grep -q 192.0.0.1; then SRC=192.0.0.1; break; fi
  sleep 2
done
if [ -e /home/user/s10-modem/vowifi/force-wifi ]; then
  SRC=$(ip -4 -o addr show dev wlan0 | sed -n 's/.* inet \([0-9.]*\)\/.*/\1/p' | head -1)
elif [ "$SRC" = 192.0.0.1 ]; then
  ip rule show | grep -q 'from 192.0.0.1 lookup 1433' || ip rule add from 192.0.0.1 lookup 1433 priority 1433
  ip route replace default dev s10clat table 1433
else
  SRC=$(ip -4 -o addr show dev wlan0 | sed -n 's/.* inet \([0-9.]*\)\/.*/\1/p' | head -1)
fi
[ -n "$SRC" ] || { echo "no IPv4 path to the ePDG"; exit 1; }
echo "tunnel source $SRC" >&2
exec python3 -u swu_emulator.py -m cp -s "$SRC" -d epdg.epc.mnc001.mcc262.pub.3gppnetwork.org -a ims -M 262 -N 001 -n ims
