#!/bin/sh
# SPDX-License-Identifier: GPL-2.0-only
# One full GNSS reference run on the phone (root). Usage: gnss-test.sh [SPI_HZ]   Summary printed at the end.
SPI_HZ=${1:-12000000}
cd /tmp
for u in $(systemctl list-units --all --no-legend 'android-*' 'bbd-*' | awk '{print $1}'); do systemctl stop $u 2>/dev/null; done
ps aux | grep '[v]endor/bin/hw' | awk '{print $2}' | xargs -r kill -9
umount /opt/android-root/dev/pts /opt/android-root/dev 2>/dev/null; systemctl reset-failed
systemctl stop s10-sensord; rm -f /tmp/ssi.cap /tmp/lhd2.log
systemd-run -q --unit=bbd-bridge -E SPI_HZ=$SPI_HZ -E CAPTURE=1 -E VERBOSE=1 -p StandardOutput=file:/tmp/bridge.out -p StandardError=file:/tmp/bridge.out python3 -u /tmp/bbd_bridge.py
sleep 4
systemd-run -q --unit=android-lhd -p StandardOutput=null python3 /tmp/run_android.py /tmp/lhd2.log /vendor/bin/hw/lhd /vendor/etc/sensor/lhd.conf
sleep 14
rm -f /opt/android-root/data/vendor/gps/.gpslogd.pipe; mkfifo -m 666 /opt/android-root/data/vendor/gps/.gpslogd.pipe
systemd-run -q --unit=gpslogd-reader -p StandardOutput=file:/tmp/gpslogd.out sh -c 'cat /opt/android-root/data/vendor/gps/.gpslogd.pipe'
sleep 1
systemd-run -q --unit=android-gpsd -p StandardOutput=file:/tmp/gpsd.out -p StandardError=file:/tmp/gpsd.out chroot /opt/android-root /system/bin/linker64 /system/bin/sh /run-gpsd.sh
sleep 9
systemctl is-active android-gpsd android-lhd bbd-bridge | tr '\n' ' '; echo
( sleep $((${MAPS_AT:-3})); GP=$(ps aux | grep '[g]psd -c /vendor' | awk '{print $2}' | head -1); G2=$(pgrep -P $GP 2>/dev/null | head -1); echo "gpsd pid $GP child $G2" > /tmp/maps.info; for p in $GP $G2; do [ -n "$p" ] && cat /proc/$p/maps > /tmp/maps.$p 2>/dev/null; done ) &
timeout $((${HAL_SECS:-45}+40)) python3 /tmp/gnss_hal.py ${HAL_SECS:-45} ${HAL_MODE} > /tmp/hal.out 2>&1; grep -i 'GL_STOP\|NMEA\|class 1 id 25[6-9]\|class 1 id 27[0-9]\|Error' /tmp/hal.out | cut -c1-330 | head -8; grep -c ' rx ' /tmp/hal.out
echo "gpsd after run: $(systemctl is-active android-gpsd) lhd: $(systemctl is-active android-lhd)"; tail -3 /tmp/gpsd.out | cut -c1-200; systemctl status android-gpsd --no-pager 2>/dev/null | grep -i "Main PID\|Active" | head -2
echo "--- timeline"
python3 /tmp/decode_cap.py /tmp/ssi.cap > /tmp/gnss-seq.txt
awk '/rpc 0x1c/{print "start/stop:", $0}' /tmp/gnss-seq.txt | cut -c1-90
echo "rows: $(wc -l < /tmp/gnss-seq.txt)  last 0x1e at: $(grep 'host->hub rpc 0x1e' /tmp/gnss-seq.txt | tail -1 | awk '{print $1}') ms; last 0x1f at: $(grep 'hub->host rpc 0x1f' /tmp/gnss-seq.txt | tail -1 | awk '{print $1}') ms"
grep -c 'fd 43 6c 00 00 c0' /tmp/gnss-seq.txt | sed 's/^/run-cmd writes: /'
for u in $(systemctl list-units --all --no-legend 'android-*' 'bbd-*' | awk '{print $1}'); do systemctl stop $u 2>/dev/null; done
ps aux | grep '[v]endor/bin/hw' | awk '{print $2}' | xargs -r kill -9
umount /opt/android-root/dev/pts /opt/android-root/dev 2>/dev/null; systemctl reset-failed
systemctl start s10-sensord
