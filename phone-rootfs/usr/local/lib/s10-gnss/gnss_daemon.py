#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""gnss_daemon: Android-GPS-HAL stand-in that keeps Samsung's gpsd running a navigation session and republishes its NMEA on a
Linux unix socket (/var/run/gps-share.sock, the format geoclue's [network-nmea] nmea-socket and gps-share clients read).
Needs the stack from run-stack.sh (bbd_bridge, lhd, gpsd). Message framing: see gnss_hal.py."""
import os, re, select, signal, socket, struct, sys, time

D = '/opt/android-root/data/vendor/gps/'
SOCK = '/var/run/gps-share.sock'
TAG = 0x5245ff02
NMEA = re.compile(rb'\$[A-Z]{5}[ -~]{3,120}?\*[0-9A-F]{2}')


def msg(a, b, payload=b''):
    body = struct.pack('<IiIi', TAG, a, TAG, b) + payload
    return struct.pack('<I', 4 + len(body)) + body


def wb(data):
    return struct.pack('<Q', len(data)) + data + bytes(-len(data) % 4)


def log(*a):
    print(time.strftime('%H:%M:%S'), *a, flush=True)


MODE = msg(0, 8, wb(struct.pack('<I', 0)) + wb(struct.pack('<I', 0)) + wb(struct.pack('<I', 1000)) + wb(struct.pack('<I', 0)) + wb(struct.pack('<I', 0)) + wb(b'\x00'))
running = True


def wi(v):
    """Int item (IpcOutgoingMessage::WriteInt): type marker + i32."""
    return struct.pack('<Ii', TAG, v)


def inject_time():
    # Stock GpsiClient gps_inject_time(): WriteBytes(time, 8), WriteBytes(timeReference, 8), WriteInt(uncertainty).
    # timeReference is Android's elapsedRealtime (CLOCK_BOOTTIME) in ms.
    now = int(time.time() * 1000)
    ref = int(time.clock_gettime(time.CLOCK_BOOTTIME) * 1000)
    return msg(0, 4, wb(struct.pack('<q', now)) + wb(struct.pack('<q', ref)) + wi(50))



def stop(*_):
    global running
    running = False


signal.signal(signal.SIGTERM, stop)
signal.signal(signal.SIGINT, stop)
# Socket activation (s10-gnss.socket): the listening socket is fd 3 and belongs to systemd. The service
# runs only while a client (geoclue's [network-nmea] source) is connected; IDLE_EXIT seconds after the
# last client left the daemon exits, the stack stops and s10-sensord gets the hub back.
ACTIVATED = int(os.environ.get('LISTEN_FDS', '0')) >= 1
IDLE_EXIT = float(os.environ.get('IDLE_EXIT', '0'))
if ACTIVATED:
    srv = socket.socket(fileno=3)
else:
    if os.path.exists(SOCK):
        os.unlink(SOCK)
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(SOCK)
    os.chmod(SOCK, 0o666)
    srv.listen(4)
srv.setblocking(False)
clients = []
last_client = time.time()
fix = {'last': None}


def publish(line):
    data = line + b'\r\n'
    for c in list(clients):
        try:
            c.send(data, socket.MSG_DONTWAIT)
        except OSError:
            clients.remove(c)
            c.close()


def wait_for(path, secs=60):
    t = time.time()
    while running and time.time() - t < secs:
        if os.path.exists(path):
            return True
        time.sleep(0.5)
    return False


log('gnss_daemon: waiting for gpsd pipe')
if not wait_for(D + '.gps.interface.pipe.to_gpsd', 120):
    sys.exit('gpsd HAL pipe not found')
time.sleep(1)
w = os.open(D + '.gps.interface.pipe.to_gpsd', os.O_WRONLY | os.O_NONBLOCK)
wait_for(D + '.gps.interface.pipe.to_jni', 20)
rfd = os.open(D + '.gps.interface.pipe.to_jni', os.O_RDONLY | os.O_NONBLOCK)
for m in (msg(0, 0), msg(0, 10), msg(0, 0x10)):
    os.write(w, m); time.sleep(0.3)
buf = b''
last_inject = time.time()
started = False
t_start = 0
last_nmea = 0
exit_code = 0
NMEA_TIMEOUT = float(os.environ.get('NMEA_TIMEOUT', '45'))
try:
    while running:
        r, _, _ = select.select([rfd, srv] + clients, [], [], 1)
        if srv in r:
            try:
                c, _ = srv.accept(); c.setblocking(False); clients.append(c); log('client connected')
            except OSError:
                pass
        for c in [c for c in clients if c in r]:
            try:
                gone = c.recv(4096) == b''   # clients only read; EOF means they left
            except BlockingIOError:
                gone = False
            except OSError:
                gone = True
            if gone:
                clients.remove(c); c.close(); log('client disconnected')
        if clients:
            last_client = time.time()
        elif IDLE_EXIT and time.time() - last_client > IDLE_EXIT:
            log('no clients for %d s, stopping' % IDLE_EXIT)
            break
        if rfd in r:
            d = os.read(rfd, 65536)
            if not d:
                time.sleep(0.2)
                continue
            buf += d
            while len(buf) >= 4:
                n = struct.unpack_from('<I', buf)[0]
                if n < 12 or n > 200000 or len(buf) < n:
                    break
                m, buf = buf[:n], buf[n:]
                cid = struct.unpack_from('<i', m, 16)[0]
                if cid == 260 and not started:      # capabilities: engine ready, now start the session
                    time.sleep(3)
                    (os.write(w, inject_time()) if os.environ.get('INJECT_TIME') else None); time.sleep(0.2); os.write(w, MODE); time.sleep(0.3); os.write(w, msg(0, 1)); started = True; t_start = time.time()
                    log('navigation started')
                elif cid == 259:
                    last_nmea = time.time()
                    for s in NMEA.findall(m):
                        publish(s)
                        if s.startswith(b'$GPGGA'):
                            f = s.split(b',')
                            if len(f) > 7 and f[6] not in (b'', b'0'):
                                if fix['last'] is None:
                                    log('FIX', s.decode())
                                fix['last'] = time.time()
        # The reference chain sometimes starts without ever delivering NMEA; let systemd restart it.
        if started and time.time() - max(t_start, last_nmea) > NMEA_TIMEOUT:
            log('no NMEA for %d s, restarting the stack' % NMEA_TIMEOUT)
            exit_code = 1
            break
        if started and os.environ.get('INJECT_TIME') and time.time() - last_inject > 600:
            last_inject = time.time(); os.write(w, inject_time())
        if started and time.time() - t_start > 30 and fix['last'] is None and int(time.time()) % 60 == 0:
            log('no fix yet')
except BrokenPipeError:
    log('gpsd closed the pipe')
    exit_code = 1
finally:
    try:
        os.write(w, msg(0, 2)); os.write(w, msg(0, 3))
    except OSError:
        pass
    for c in clients:
        c.close()
    if not ACTIVATED and os.path.exists(SOCK):
        os.unlink(SOCK)
    log('gnss_daemon: stopped')
sys.exit(exit_code)
