# SPDX-License-Identifier: GPL-2.0-only
import sys, time, struct
sys.path.insert(0, '.')
import s10shub as S
h = S.Hub(log=print); h.boot()
print('on light', h.sensor_on(S.LIGHT, 200), 'prox', h.sensor_on(S.PROX, 200))
t0 = time.time(); seen=set()
while time.time() - t0 < 12:
    h.feed(0.05)
    for e in h.events():
        if e[0]=='sens' and e[1] in (9,7):
            k=(e[1],e[2].hex())
            if k not in seen: seen.add(k); print('%5.1f'%(time.time()-t0), e[1], e[2].hex())
        elif e[0]=='log' and (b'Sensor_Light' in e[1] or b'Light' in e[1]): print('LOG', e[1][:200])
h.power_off()
