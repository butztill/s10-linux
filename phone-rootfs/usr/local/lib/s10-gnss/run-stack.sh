#!/bin/sh
# SPDX-License-Identifier: GPL-2.0-only
# S10 GNSS stack (reference chain in /opt/android-root): hub bridge, lhd, gpsd, NMEA daemon. Foreground; stops on SIGTERM.
# Install to /usr/local/lib/s10-gnss/ (bbd_bridge.py run_android.py gnss_daemon.py run-stack.sh) - see s10-gnss.service.
D=/usr/local/lib/s10-gnss
export SPI_HZ=25000000
# CAPTURE=1 and VERBOSE=1 in the environment enable the SSI capture (/tmp/ssi.cap) and per-transfer logging in bbd_bridge.py
cleanup() {
  kill $DPID $GPID $LPID $BPID 2>/dev/null
  sleep 1
  pkill -9 -f 'linker64 /vendor/bin/hw/' ; pkill -9 -f 'run_android.py' ; pkill -9 -f 'bbd_bridge.py'
  umount /opt/android-root/dev/pts /opt/android-root/dev 2>/dev/null
}
trap 'cleanup; exit 0' TERM INT
umount /opt/android-root/dev/pts /opt/android-root/dev 2>/dev/null
pkill -9 -f 'linker64 /vendor/bin/hw/' ; pkill -9 -f 'run_android.py' ; pkill -9 -f 'bbd_bridge.py'
sleep 1
mkdir -p /run/gnss
python3 -u $D/bbd_bridge.py > /run/gnss/bridge.log 2>&1 &
BPID=$!
sleep 4
python3 -u $D/run_android.py /run/gnss/lhd.log /vendor/bin/hw/lhd /vendor/etc/sensor/lhd.conf &
LPID=$!
sleep 14
chroot /opt/android-root /system/bin/linker64 /system/bin/sh /run-gpsd.sh > /run/gnss/gpsd.log 2>&1 &
GPID=$!
sleep 8
python3 -u $D/gnss_daemon.py &
DPID=$!
wait $DPID
rc=$?
cleanup
exit $rc
