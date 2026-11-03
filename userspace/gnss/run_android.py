#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""run_android.py LOGFILE PROGRAM [ARGS...]: run an Android (bionic) program inside /opt/android-root with a pty as stdout/stderr
(so its stdio is line buffered and its console log is visible), copying its output to LOGFILE with timestamps."""
import os, pty, select, sys, time

ROOT = '/opt/android-root'
LP = ':'.join(['/system/lib64', '/vendor/lib64', '/apex/com.android.runtime/lib64/bionic', '/apex/com.android.runtime/lib64',
               '/apex/com.android.i18n/lib64', '/apex/com.android.conscrypt/lib64', '/apex/com.android.art/lib64',
               '/apex/com.android.resolv/lib64', '/apex/com.android.vndk.current/lib64'])
log, prog, args = sys.argv[1], sys.argv[2], sys.argv[3:]
pid, fd = pty.fork()
if pid == 0:
    os.chroot(ROOT); os.chdir('/')
    os.environ['LD_LIBRARY_PATH'] = LP
    os.execv('/system/bin/linker64', ['linker64', prog] + args)
out = open(log, 'a', buffering=1)
buf = b''
while True:
    try:
        d = os.read(fd, 4096)
    except OSError:
        break
    if not d:
        break
    buf += d
    while b'\n' in buf:
        ln, buf = buf.split(b'\n', 1)
        t = ln.decode('latin1').rstrip('\r')
        if 'linkerconfig' not in t:
            out.write('%s %s\n' % (time.strftime('%H:%M:%S'), t))
_, st = os.waitpid(pid, 0)
out.write('%s exited %s\n' % (time.strftime('%H:%M:%S'), st))
