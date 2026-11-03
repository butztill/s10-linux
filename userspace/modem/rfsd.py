#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Userspace RFS server for the Galaxy S10 Shannon modem (SIPC channel 0xf5).
Protocol reverse engineered from vendor libsec-ril.so (Nv::ProcessRfsMessage).
Message: u32 total_len, u8 cmd, u8 seq, ... ; reply: u32 len, u8 cmd, u8 seq, ...
"""
import os, struct, sys, time, errno
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from s10cp import Link

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'rfs-root')
LOG = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'rfsd.log'), 'a', buffering=1)
fds = {}


def log(*a):
    LOG.write('%.3f ' % time.time() + ' '.join(str(x) for x in a) + '\n')


def vpath(p):
    p = p.split(b'\0')[0].decode('latin1')
    parts = [x for x in p.split('/') if x not in ('', '.', '..')]
    return os.path.join(ROOT, *parts)


def hdr(cmd, seq, body=b''):
    return struct.pack('<IBB', 6 + len(body), cmd, seq) + body


def h_delete(m):
    plen, = struct.unpack_from('<I', m, 6)
    path = vpath(m[10:10 + plen])
    try:
        os.unlink(path); res, err = 0, 0
    except OSError as e:
        res, err = -1, e.errno
    log('DELETE', path, res, err)
    return hdr(0x0b, m[5], struct.pack('<iI', res, err))


def h_open(m):
    flags, plen = struct.unpack_from('<II', m, 6)
    path = vpath(m[14:14 + plen])
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        fd = os.open(path, flags | os.O_CLOEXEC, 0o775); err = 0
    except OSError as e:
        fd, err = -1, e.errno
    log('OPEN', path, hex(flags), '->', fd, err)
    return hdr(0x11, m[5], struct.pack('<iI', fd, err))


def h_nvwrite(m):
    off, size = struct.unpack_from('<II', m, 6)
    data = m[14:14 + size]
    path = os.path.join(ROOT, 'nv_data.bin')
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o660)
    n = os.pwrite(fd, data, off)
    os.fsync(fd); os.close(fd)
    log('NVWRITE off=%#x size=%#x -> %d' % (off, size, n))
    return hdr(0x02, m[5], struct.pack('<BIi', 1, off, n))


def res14(cmd, seq, res, err=0):
    return hdr(cmd, seq, struct.pack('<iI', res, err))


def h_close(m):
    fd, = struct.unpack_from('<i', m, 6)
    try:
        os.close(fd); r = (0, 0)
    except OSError as e:
        r = (-1, e.errno)
    log('CLOSE', fd, r)
    return res14(6, m[5], *r)


def h_write(m):
    fd, size = struct.unpack_from('<iI', m, 6)
    try:
        n = os.write(fd, m[14:14 + size]); r = (n, 0)
    except OSError as e:
        r = (-1, e.errno)
    log('WRITE', fd, size, r)
    return res14(4, m[5], *r)


def h_read(m):
    fd, size = struct.unpack_from('<iI', m, 6)
    size = min(size, 0x40000)
    try:
        data = os.read(fd, size); r = (len(data), 0)
    except OSError as e:
        data, r = b'', (-1, e.errno)
    log('READ', fd, size, r[0])
    return hdr(3, m[5], struct.pack('<iI', *r) + data)


def h_lseek(m):
    fd, off, whence = struct.unpack_from('<iII', m, 6)
    off, = struct.unpack('<i', struct.pack('<I', off))
    try:
        res, err = os.lseek(fd, off, whence), 0
    except OSError as e:
        res, err = -1, e.errno
    log('LSEEK', fd, off, whence, res, err)
    return struct.pack('<IBBIiI', 18, 5, m[5], 0, res, err)


def finfo(cmd, seq, st, err):
    # 32 byte reply: len, cmd, seq, res(s32), u16 flag, u32 size/mode..., errno at 0x1c  (layout partly guessed)
    b = bytearray(32)
    struct.pack_into('<IBB', b, 0, 32, cmd, seq)
    if st is None:
        struct.pack_into('<i', b, 6, -1)
        struct.pack_into('<I', b, 0x1c, err)
    else:
        struct.pack_into('<iHI', b, 6, 0, 1, st.st_size)
    return bytes(b)


def h_getfileinfo(m):
    plen, = struct.unpack_from('<I', m, 6)
    path = vpath(m[10:10 + plen])
    try:
        st, err = os.stat(path), 0
    except OSError as e:
        st, err = None, e.errno
    log('GETFILEINFO', path, 'ok' if st else err)
    return finfo(10, m[5], st, err)


def h_getfileinfo_fd(m):
    fd, = struct.unpack_from('<i', m, 6)
    try:
        st, err = os.fstat(fd), 0
    except OSError as e:
        st, err = None, e.errno
    log('GETFILEINFOBYHANDLE', fd, 'ok' if st else err)
    return finfo(19, m[5], st, err)


def h_mkdir(m):
    plen, = struct.unpack_from('<I', m, 6)
    path = vpath(m[10:10 + plen])
    try:
        os.makedirs(path, 0o775, exist_ok=True); r = (0, 0)
    except OSError as e:
        r = (-1, e.errno)
    log('MKDIR', path, r)
    return res14(12, m[5], *r)


def h_create(m):
    plen, = struct.unpack_from('<I', m, 6)
    path = vpath(m[10:10 + plen])
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_CLOEXEC, 0o775); r = (fd, 0)
    except OSError as e:
        r = (-1, e.errno)
    log('CREATE', path, r)
    return res14(20, m[5], *r)


def h_rename(m):
    l1, = struct.unpack_from('<I', m, 6)
    l2, = struct.unpack_from('<I', m, 0x40a)
    a, b = vpath(m[10:10 + l1]), vpath(m[0x40e:0x40e + l2])
    try:
        os.rename(a, b); r = (0, 0)
    except OSError as e:
        r = (-1, e.errno)
    log('RENAME', a, b, r)
    return res14(9, m[5], *r)


HANDLERS = {0x0b: h_delete, 0x11: h_open, 0x02: h_nvwrite, 0x06: h_close, 0x04: h_write, 0x03: h_read,
            0x05: h_lseek, 0x0a: h_getfileinfo, 0x13: h_getfileinfo_fd, 0x0c: h_mkdir, 0x14: h_create,
            0x09: h_rename}


def handle(link, m):
    cmd = m[4]
    h = HANDLERS.get(cmd)
    if not h:
        log('UNHANDLED cmd=%#x seq=%d len=%d: %s' % (cmd, m[5], len(m), m[:64].hex(' ')))
        return
    try:
        reply = h(m)
    except Exception as e:  # keep serving
        log('ERR', cmd, repr(e)); return
    link.send(0xf5, reply)


def main():
    link = Link()
    acc = b''
    last_msg = None
    log('rfsd start')
    while True:
        for p in link.poll_dl(0xf5):
            acc += p
            while len(acc) >= 4:
                total, = struct.unpack_from('<I', acc)
                if total < 6 or total > 0x80000:
                    log('BAD total', total, acc[:16].hex()); acc = b''; break
                if len(acc) < total:
                    break
                handle(link, acc[:total]); acc = acc[total:]
        isr1, intsr, st = link.mbox()
        if intsr & 0x10000:
            if isr1 != last_msg or True:
                log('mbox msg %#x status %#x' % (isr1, st))
            if isr1 == 0x83:
                link.irq(0xc4)
            link.cmd('mbrx')
        time.sleep(0.002)


if __name__ == '__main__':
    main()
