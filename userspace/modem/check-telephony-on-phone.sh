#!/bin/sh
# SPDX-License-Identifier: GPL-2.0-only
# Run as root in /home/user/s10-modem. Wi-Fi SSH remains available.
set -eu
cd /home/user/s10-modem
test "$(id -u)" = 0
test "$(sha256sum s10modemd.py | cut -d ' ' -f1)" = 1599cbd767fa373fa76574cb3f00f3457e054088f5445aa3edc6fcc1320a4172
test ! -e s10modemd.py.before-sms-20261025
cp -p s10modemd.py s10modemd.py.before-sms-20261025
python3 -m py_compile s10sms.py s10modemd.sms-candidate.py probe-telephony.py
systemd-run --unit=s10-telephony-recover-20261025 --on-active=50s \
    /bin/sh -c 'systemctl start s10-modem; systemctl start ModemManager'
recover() {
    systemctl start s10-modem
    systemctl start ModemManager
}
trap recover EXIT
systemctl stop ModemManager
systemctl stop s10-modem
cp s10modemd.sms-candidate.py s10modemd.py
timeout 25 python3 probe-telephony.py
