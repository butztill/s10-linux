#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Exclusive, read-only DeviceCapa/registration probe. No SMS or SET commands.

G973FXXSGHWC2 CP getter at 0x4103e71e returns [EUTRAN pref, 0].
Run in the installed modem directory, with ModemManager and s10-modem stopped.
Only protocol enums are printed; never subscriber, location or SMS data.
"""
import json
import subprocess
from s10modemd import Modem

for service in ('ModemManager', 's10-modem'):
    if subprocess.run(['systemctl', 'is-active', '--quiet', service]).returncode == 0:
        raise SystemExit('Refusing concurrent CP reader: ' + service)

modem = Modem()
result = {}
reply = modem.request(0x0a, 0x2d, 2, b'', timeout=3)
result['device_capa'] = {'answered': reply is not None,
                         'bytes': len(reply) if reply is not None else 0}
if reply is not None and len(reply) == 2:
    result['device_capa'].update(eutran=reply[0], utran=reply[1],
                                  restorable=reply[0] < 4 and reply[1] == 0)
for domain, name in ((2, 'voice'), (3, 'packet')):
    reply = modem.request(8, 5, 2, bytes([255, domain]), timeout=3)
    item = {'answered': reply is not None,
            'bytes': len(reply) if reply is not None else 0}
    if reply is not None and len(reply) >= 3:
        item.update(rat=reply[0], domain=reply[1], registration=reply[2])
        if reply[0] == 0x21 and len(reply) >= 15:
            item.update(reject_cause=reply[10], volte_support=reply[13],
                        ims_emergency_support=reply[14])
    result[name] = item
print(json.dumps(result, indent=2))
