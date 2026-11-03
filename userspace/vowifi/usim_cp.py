# SPDX-License-Identifier: GPL-2.0-or-later
"""USIM access through the Shannon CP for the SWu/IKEv2 client and imsd, via the s10modemd control socket
(/run/s10modem/ctl.sock; the AT pty is shared with ModemManager and must not be used concurrently).

imsi()                    -> 15-digit IMSI string
imei()                    -> IMEI string
res_ck_ik(rand, autn)     -> (res, ck, ik) as hex strings, or (auts_hex, None, None) on SQN sync failure"""
import json
import socket
import time

CTL = '/run/s10modem/ctl.sock'


def _call(req, timeout=12):
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(CTL)
        s.sendall(json.dumps(req).encode())
        return json.loads(s.recv(65536))
    finally:
        s.close()


def _ipc(main, sub, typ, payload=b'', tries=4):
    for _ in range(tries):
        try:
            r = _call({'op': 'ipc', 'main': main, 'sub': sub, 'typ': typ, 'payload': payload.hex()})
        except (OSError, ValueError):
            r = {'ok': False}
        if r.get('ok'):
            return bytes.fromhex(r['payload'])
        time.sleep(1)
    raise RuntimeError('no IPC answer')


def imsi():
    for _ in range(8):
        try:
            digits = ''.join(chr(c) for c in _ipc(0x0a, 0x02, 2) if 0x30 <= c <= 0x39)
        except RuntimeError:
            digits = ''
        if len(digits) >= 15:
            return digits[-15:]
        time.sleep(2)
    raise RuntimeError('IMSI not available')


def imei():
    for _ in range(5):
        try:
            r = _call({'op': 'imei'})
            if r.get('ok'):
                return r['imei']
        except (OSError, ValueError):
            pass
        time.sleep(1)
    return ''


def res_ck_ik(rand_hex, autn_hex):
    rand, autn = bytes.fromhex(rand_hex), bytes.fromhex(autn_hex)
    raw = _ipc(0x05, 0x09, 2, bytes([2, 16]) + rand + bytes([16]) + autn, tries=1)
    if len(raw) >= 16 and raw[0] == 2 and raw[1] == 5:
        return raw[3:3 + raw[2]].hex(), None, None
    if len(raw) < 6 or raw[0] != 2 or raw[1] != 0:
        raise RuntimeError('AKA failed: %s' % raw[:2].hex())
    out, i = [], 2
    while i < len(raw):
        out.append(raw[i + 1:i + 1 + raw[i]].hex())
        i += 1 + raw[i]
    return out[0], out[1], out[2]
