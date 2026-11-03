#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""callmedia: RTP/AMR media of one call. Run inside the tunnel namespace as root (spawned by imsd).
  callmedia.py LOCAL_ADDR LOCAL_PORT REMOTE_ADDR REMOTE_PORT CODEC(AMR-WB|AMR) PT
The RTP socket (the advertised port, so the network's media gateway can latch onto it) is owned here; GStreamer does codec and
audio I/O (PulseAudio of the user session) and talks to us over loopback UDP."""
import os, select, signal, socket, subprocess, sys, time

laddr, lport, raddr, rport, codec, pt = sys.argv[1], int(sys.argv[2]), sys.argv[3], int(sys.argv[4]), sys.argv[5], int(sys.argv[6])
wb = codec == 'AMR-WB'
rate = 16000 if wb else 8000
RX_PORT, TX_PORT = 39000 + lport % 500, 39500 + lport % 500          # loopback ports towards / from GStreamer
env = dict(os.environ, XDG_RUNTIME_DIR='/run/user/10000', HOME='/home/user', USER='user')


def as_user():
    os.setsid()
    os.initgroups('user', 10000)
    os.setgid(10000)
    os.setuid(10000)

caps = ('application/x-rtp,media=(string)audio,clock-rate=(int)%d,encoding-name=(string)%s,encoding-params=(string)1,'
        'octet-align=(string)1,payload=(int)%d' % (rate, 'AMR-WB' if wb else 'AMR', pt))
dec = 'amrwbdec' if wb else 'amrnbdec'
enc = 'voamrwbenc band-mode=6' if wb else 'amrnbenc band-mode=7'
rx = ('gst-launch-1.0 -q udpsrc address=127.0.0.1 port=%d caps="%s" ! rtpjitterbuffer latency=80 ! rtpamrdepay ! %s ! '
      'audioconvert ! audioresample ! pulsesink buffer-time=60000 latency-time=20000' % (RX_PORT, caps, dec))
tx = ('gst-launch-1.0 -q pulsesrc buffer-time=60000 latency-time=20000 ! audioconvert ! audioresample ! '
      'audio/x-raw,rate=%d,channels=1 ! %s ! rtpamrpay pt=%d ! udpsink host=127.0.0.1 port=%d sync=false' % (rate, enc, pt, TX_PORT))
procs = [subprocess.Popen(c, shell=True, env=env, preexec_fn=as_user) for c in (rx, tx)]

net = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
net.bind((laddr, lport))
rx_in = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
tx_out = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
tx_in = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
tx_in.bind(('127.0.0.1', TX_PORT))
running = True


def stop(*_):
    global running
    running = False


signal.signal(signal.SIGTERM, stop)
signal.signal(signal.SIGINT, stop)
n_in = n_out = 0
t0 = time.time()
while running:
    r, _, _ = select.select([net, tx_in], [], [], 0.5)
    for s in r:
        data, peer = s.recvfrom(2048)
        if s is net:
            if len(data) > 12 and (data[1] & 0x7f) == pt:
                tx_out.sendto(data, ('127.0.0.1', RX_PORT))
                n_in += 1
        else:
            try:
                net.sendto(data, (raddr, rport))
                n_out += 1
            except OSError as e:
                if n_out == 0 or n_out % 500 == 0:
                    print('send error', e, 'after', n_out, flush=True)
print('media stats: %d RTP in, %d out, %.0f s' % (n_in, n_out, time.time() - t0), flush=True)
for p in procs:
    try:
        os.killpg(p.pid, signal.SIGTERM)
    except OSError:
        pass
