#!/bin/sh
# SPDX-License-Identifier: GPL-2.0-only
# Install the S10 sensor daemon on the phone (run as root on the phone from this directory).
# Phosh rotates without s10-tabletmode; that GNOME-only workaround is not installed here.
set -eu
: "${S10_SHUB_FIRMWARE:?Set S10_SHUB_FIRMWARE to an externally supplied sensor-hub firmware file}"
case "$S10_SHUB_FIRMWARE" in
    /*) ;;
    *) echo "Use an absolute S10_SHUB_FIRMWARE path." >&2; exit 1 ;;
esac
test -f "$S10_SHUB_FIRMWARE" && test -s "$S10_SHUB_FIRMWARE" || {
    echo "Sensor-hub firmware is missing or empty; nothing installed." >&2
    exit 1
}
test "$(id -u)" = 0 || { echo "Run this installer as root on the target device." >&2; exit 1; }
cd "$(dirname "$0")"
D=/usr/local/lib/s10-sensors
mkdir -p $D
cp s10shub.py s10-sensord "$D/"
install -m 0644 "$S10_SHUB_FIRMWARE" "$D/shub_bbd_patch.bin"
chmod 755 $D/s10-sensord
cp s10-sensord.service /etc/systemd/system/
systemctl disable iio-sensor-proxy 2>/dev/null || true
rm -f /etc/systemd/system/iio-sensor-proxy.service
ln -s /etc/systemd/system/s10-sensord.service /etc/systemd/system/iio-sensor-proxy.service
systemctl daemon-reload
systemctl enable s10-sensord.service 2>&1 | tail -1
systemctl restart s10-sensord.service
echo installed
