# SPDX-License-Identifier: GPL-2.0-only
# Capture raw accel / prox / light for orientation + threshold calibration (stop the daemon first).
import sys, time, struct
sys.path.insert(0, '.')
import s10shub as S
h = S.Hub(log=print); h.boot()
for sid, ms in ((S.ACCEL, 200), (S.LIGHT, 200), (S.PROX, 100), (S.MAG, 200)): h.sensor_on(sid, ms)
T = float(sys.argv[1]); t0 = time.time(); last = {}; nxt = 0
while time.time() - t0 < T:
    h.feed(0.05)
    for e in h.events():
        if e[0] == 'sens': last[e[1]] = e[2]
    if time.time() >= nxt and 0 in last and 7 in last and 9 in last:
        nxt = time.time() + 0.5
        a = struct.unpack('<hhh', last[0]); p = last[7]; l = last[9]
        m = struct.unpack_from('<hhh', last[4]) if 4 in last else (0, 0, 0)
        print('%5.1f acc %6d %6d %6d | prox det=%3d adc=%5d | lux=%d r%d g%d b%d c%d | mag %d %d %d' % (
            time.time() - t0, *a, p[0], struct.unpack_from('<H', p, 1)[0], struct.unpack_from('<I', l, 0)[0],
            *struct.unpack_from('<HHHH', l, 8), *m), flush=True)
h.power_off()
