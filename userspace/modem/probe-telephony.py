#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Read-only IPC queries. Run ONLY with s10-modem and ModemManager stopped.

No dial, SMS transmission, SIM reads, authentication or firmware/NV writes.
Output only structural counts and registration state, never raw replies.
"""
import json
import struct
import subprocess
from s10modemd import Modem

for service in ('s10-modem', 'ModemManager'):
    if subprocess.run(['systemctl', 'is-active', '--quiet', service]).returncode == 0:
        raise SystemExit('Refusing concurrent CP reader: ' + service)

m = Modem()
result = {}
for name, main, sub, payload in (
        ('sim_pin_status', 5, 1, b''),
        ('voice_registration', 8, 5, bytes([255, 2])),
        ('data_registration', 8, 5, bytes([255, 3])),
        ('service_domain', 8, 8, b''),
        ('call_list', 2, 6, b''),
        ('smsc', 4, 10, b''),
        ('sim_sms_count', 4, 9, bytes([1]))):
    reply = m.request(main, sub, 2, payload, timeout=2)
    summary = {'answered': reply is not None, 'bytes': len(reply) if reply is not None else 0}
    if name.endswith('_registration') and reply and len(reply) >= 3:
        summary.update(rat=reply[0], domain=reply[1], registration=reply[2])
        # G973FXXSGHWC2 IpcRxNetRegState @0043cc78, non-NR layout.
        # Skip location/cell fields entirely; publish only protocol enums.
        if reply[0] == 0x21 and len(reply) >= 15:
            summary.update(reject_cause=reply[10], volte_support=reply[13],
                           ims_emergency_support=reply[14])
    elif name == 'sim_pin_status' and reply:
        summary['status'] = reply[0]  # enum only, no PIN or subscriber data
    elif name == 'service_domain' and reply:
        summary['value'] = reply[0]
    elif name == 'call_list' and reply:
        summary['count'] = reply[0]
    elif name == 'smsc' and reply:
        summary['declared_bytes'] = reply[0]
        summary['framing_valid'] = 1 <= reply[0] <= 12 and reply[0] + 1 <= len(reply)
    elif name == 'sim_sms_count' and reply and len(reply) >= 5:
        summary['memory'], summary['total'], summary['used'] = struct.unpack_from('<BHH', reply)
    result[name] = summary
print(json.dumps(result, indent=2))
