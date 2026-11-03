#!/bin/sh
# SPDX-License-Identifier: GPL-2.0-only
# root, on FRESH phone boot: boot CP, bring SBD link up to INIT_END, then run the RFS server (rfsd.py).
cd /home/user/s10-modem
[ -e /home/user/s10-modem/DISABLE ] && { echo 'DISABLE file present, not starting the modem'; exit 0; }
# restart-safe: if the CP is already up (module loaded, CP status set) only restart the daemon
if grep -q '^s10_cp_boot' /proc/modules && [ "$(cut -d' ' -f3 /sys/kernel/debug/s10cp/mbox 2>/dev/null)" != "00000000" ]; then
  echo 'CP already running, restarting daemon only'
  echo irq_on > /sys/kernel/debug/s10cp/ctl 2>/dev/null
  grep -q '^s10_modem_tty' /proc/modules || insmod tty/s10-modem-tty.ko
  exec python3 -I s10modemd.py
fi
./boot2.sh > boot2.out 2>&1
R=./run.sh
for i in 1 2 3 4 5 6 7 8 9 10; do
  $R mbrx 3 >/dev/null 2>&1
  grep -q "ISSR1=000000c1" last.log && break
  sleep 1
done
$R initsbd 4 >/dev/null 2>&1
$R "irq 0xcd" 2 > /dev/null 2>&1
sleep 3
dd if=/dev/zero of=/sys/kernel/debug/s10cp/shm bs=1 seek=$((0xb200008)) count=$((0x3f8)) conv=notrunc 2>/dev/null
$R magic 2 >/dev/null 2>&1
$R "irq 0xc2" 2 >/dev/null 2>&1
echo link up
echo irq_on > /sys/kernel/debug/s10cp/ctl 2>/dev/null
insmod tty/s10-modem-tty.ko
udevadm trigger --action=add --subsystem-match=tty --sysname-match='ttyCP*' 2>/dev/null
exec python3 -I s10modemd.py
