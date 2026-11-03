#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""imsctl: talk to imsd.   imsctl.py listen | dial NUMBER | answer | hangup   (events are printed with numbers masked)"""
import json, re, socket, sys
s = socket.socket(socket.AF_UNIX)
s.connect('/run/s10modem/ims.sock')
if sys.argv[1] in ('dial', 'answer', 'hangup'):
    req = {'op': sys.argv[1]}
    if sys.argv[1] == 'dial':
        req['number'] = sys.argv[2]
    s.sendall((json.dumps(req) + '\n').encode())
buf = b''
while True:
    d = s.recv(4096)
    if not d:
        break
    buf += d
    while b'\n' in buf:
        line, buf = buf.split(b'\n', 1)
        if json.loads(line).get('op') not in ('call', 'status'):
            continue
        print(re.sub(r'(\+?\d{3})\d{5,}', r'\1*****', line.decode()), flush=True)
