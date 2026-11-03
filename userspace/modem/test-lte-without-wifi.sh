#!/bin/sh
# SPDX-License-Identifier: GPL-2.0-only
# Detached root test. Arm an independent Wi-Fi reconnect timer before invoking.
set -u
out=/home/user/s10-modem/lte-data-test-20261025.log
exec >"$out" 2>&1
chmod 644 "$out"
test "$(id -u)" = 0 || exit 1
restore_timer=${1:-s10-lte-wifi-restore-20261025.timer}
case "$restore_timer" in s10-lte-wifi-restore-*.timer) ;; *) exit 1 ;; esac
systemctl is-active --quiet "$restore_timer" || exit 1
cleanup() { nmcli --wait 20 device connect wlan0 || true; }
trap cleanup EXIT HUP INT TERM
run() {
    name=$1
    shift
    echo "TEST=$name"
    if nmcli -g GENERAL.STATE dev show wlan0 | grep -q '^100'; then
        echo "RESULT=$name:INVALID_WIFI_CONNECTED"
        return 0
    fi
    "$@"
    rc=$?
    if nmcli -g GENERAL.STATE dev show wlan0 | grep -q '^100'; then
        echo "RESULT=$name:INVALID_WIFI_RESTORED_DURING_TEST"
        return 0
    fi
    echo "RESULT=$name:$rc"
    return 0
}
echo "START=$(date -u +%FT%TZ)"
nmcli --wait 10 device disconnect wlan0
sleep 5
ip -4 route show
ip -6 route show default
ip -o link show rmnet0
run ipv6 ping -6 -n -c 3 -W 3 2001:4860:4860::8888
run ipv4 ping -4 -n -c 3 -W 3 1.1.1.1
run dns timeout 8 python3 -c 'import socket; r=socket.getaddrinfo("www.kernel.org",443,type=socket.SOCK_STREAM); print("DNS families:",sorted(set(x[0] for x in r))); assert r'
run https timeout 15 wget -T 12 -q -O /dev/null https://example.com/
run ipv4_http timeout 15 wget -T 12 -q -O /dev/null http://1.1.1.1/cdn-cgi/trace
ip -s link show rmnet0
journalctl -u NetworkManager -b --no-pager --since '-2min' | grep -i clat | tail -15
echo "END=$(date -u +%FT%TZ)"
