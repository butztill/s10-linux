# SPDX-License-Identifier: GPL-2.0-only
import sys, time, struct
sys.path.insert(0, '.')
import s10shub as S
h = S.Hub(log=print)
h.boot()
for sid, ms in ((S.ACCEL, 100), (S.GYRO, 100), (S.LIGHT, 200), (S.PROX, 100), (S.MAG, 200), (S.PRESSURE, 500)):
    print('on', sid, h.sensor_on(sid, ms))
t0 = time.time(); last = {}; n = 0
while time.time() - t0 < float(sys.argv[1] if len(sys.argv) > 1 else 5):
    h.feed(0.05)
    for e in h.events():
        if e[0] == 'sens': last[e[1]] = e[2]; n += 1
for k, v in sorted(last.items()): print(k, v.hex())
print('events', n)
h.power_off()
