#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Restore journalled DeviceCapa; both modem services must be stopped.

Disable telephony-policy.json before running. Does not authenticate/boot CP,
send SMS, select a RAT or change firmware/NV files directly.
"""
import json
from pathlib import Path
import subprocess
from s10modemd import Modem
from telephony_policy import BUILD, TelephonyPolicy, pair

directory = Path(__file__).resolve().parent
for name in ('ModemManager', 's10-modem'):
    if subprocess.run(['systemctl', 'is-active', '--quiet', name]).returncode == 0:
        raise SystemExit('Refusing concurrent CP reader: ' + name)
policy = TelephonyPolicy(directory, lambda *args: print(*args))
if policy.config is not None or policy.state != 'disabled':
    raise SystemExit('Disable policy before restoring')
saved = json.loads(policy.journal.read_text())
if (saved.get('version') != 1 or saved.get('firmware') != BUILD
        or not pair(saved.get('original')) or not pair(saved.get('target'))):
    raise SystemExit('Invalid rollback journal')
policy.original, policy.config = bytes(saved['original']), bytes(saved['target'])
modem = Modem()
version = modem.request(10, 1, 2, b'\xff')
if version is None or BUILD.encode() not in version:
    raise SystemExit('Firmware mismatch: refusing SET')
if not policy.restore(modem):
    raise SystemExit('Rollback not confirmed; keep journal')
# Retain journal as evidence. A later DIFFERENT policy requires explicit review.
print('Rollback confirmed; no SMS transmitted')
