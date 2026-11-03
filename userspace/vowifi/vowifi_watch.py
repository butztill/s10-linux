#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""vowifi_watch: keeps the cellular IMS path alive (mobile data, 464XLAT, ePDG tunnel, imsd registration).
Every 15 s: bring up mobile data if needed; (re)start CLAT if the bearer is up but s10clat is missing; restart the tunnel after the
IPv4 path was lost and came back, or when imsd stays unregistered for more than 120 s."""
import json, os, socket, subprocess, time


def sh(*a):
    return subprocess.run(a, capture_output=True, text=True).stdout


def log(*a):
    print(time.strftime('%H:%M:%S'), *a, flush=True)


def registered():
    try:
        s = socket.socket(socket.AF_UNIX)
        s.settimeout(3)
        s.connect('/run/s10modem/ims.sock')
        return bool(json.loads(s.recv(4096).split(b'\n')[0])['registered'])
    except (OSError, ValueError, KeyError, IndexError):
        return False


dirty = False
unreg_since = None
last_restart = 0
while True:
    try:
        v6 = 'scope global' in sh('ip', '-6', '-o', 'addr', 'show', 'dev', 'rmnet0')
        if not v6:
            dirty = True
            log('no mobile data, bringing the connection up')
            sh('nmcli', 'con', 'up', 'S10 Telekom LTE')
        else:
            clat = '192.0.0.1' in sh('ip', '-4', '-o', 'addr', 'show', 'dev', 's10clat')
            if not clat:
                dirty = True
                if sh('systemctl', 'is-active', 's10-clat').strip() != 'activating':
                    log('CLAT missing, restarting it')
                    sh('systemctl', 'restart', 's10-clat')
            elif dirty:
                log('IPv4 path is back, restarting the tunnel')
                dirty = False
                sh('systemctl', 'restart', 's10-vowifi-tun')
                last_restart = time.time()
        if registered():
            unreg_since = None
        else:
            unreg_since = unreg_since or time.time()
            if time.time() - unreg_since > 120 and time.time() - last_restart > 120 and v6:
                log('imsd unregistered for >120 s, restarting the tunnel')
                sh('systemctl', 'restart', 's10-vowifi-tun')
                last_restart = time.time()
                unreg_since = None
    except Exception as e:
        log('watch error', type(e).__name__, e)
    time.sleep(15)
