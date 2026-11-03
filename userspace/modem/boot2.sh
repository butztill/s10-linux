#!/bin/sh
# SPDX-License-Identifier: GPL-2.0-only
# root, on FRESH phone boot: full CP boot + BL handshake. Output -> boot2.out
cd /home/user/s10-modem
R=./run.sh
insmod boot/s10-cp-boot.ko $(cat /home/user/s10-modem/boot.params 2>/dev/null)
python3 -I load-radio.py >/dev/null 2>&1
$R "sizes 0x1ff0 0x262d250" 2 >/dev/null 2>&1
$R mbox 3 >/dev/null 2>&1
$R zero 3 >/dev/null 2>&1
$R pktproc 3 >/dev/null 2>&1
printf 'TOOB' | dd of=/sys/kernel/debug/s10cp/shm bs=1 seek=$((0xb200000)) conv=notrunc 2>/dev/null
$R ctrl 3 >/dev/null 2>&1
$R pwr 3 >/dev/null 2>&1
$R auth 4 >/dev/null 2>&1
$R cfg 3 >/dev/null 2>&1
sleep 3
$R recv 6 | sed 's/^\[ *[0-9.]*\] s10cp: //'
$R "send 0x900d" 4 | sed 's/^\[ *[0-9.]*\] s10cp: //'
sleep 1
$R recv 6 | sed 's/^\[ *[0-9.]*\] s10cp: //'
$R "send 0x9f00" 4 | sed 's/^\[ *[0-9.]*\] s10cp: //'
sleep 2
$R recv 6 | sed 's/^\[ *[0-9.]*\] s10cp: //'
sleep 5
$R status 34 | sed 's/^\[ *[0-9.]*\] s10cp: //' | grep -E "CP_|ISSR[0-9]+=[0-9a-f]*[1-9a-f]|INT"
$R recv 6 | sed 's/^\[ *[0-9.]*\] s10cp: //'
