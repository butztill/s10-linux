#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""bbd_bridge: stand-in for the Samsung 'bbd' kernel driver for the Android lhd/gpsd running in /opt/android-root.

Provides inside the chroot (ROOT/dev, ROOT/sys): ttyBCM (raw stream <-> BCM4773 over SPI SSI), bbd_control, bbd_sensor (ptys,
traffic logged only), bbd_patch (the sensor-hub patch blob), and the sysfs files named in lhd.conf (GPS_PWR_EN/value drives the hub power pin).
Run as root with s10-sensord stopped:  python3 bbd_bridge.py   (then start lhd/gpsd in the chroot, see run-lhd.sh)"""
import os, sys, time, select, termios, tty, stat, shutil, subprocess
sys.path.insert(0, '/usr/local/lib/s10-sensors')
import s10shub

ROOT = '/opt/android-root'
PATCH = '/usr/local/lib/s10-sensors/shub_bbd_patch.bin'


def log(*a):
    print(time.strftime('%H:%M:%S'), *a, flush=True)


def sh(*a):
    subprocess.run(a, check=False, capture_output=True)


def setup_root():
    d = ROOT + '/dev'
    if os.path.ismount(d):
        sh('umount', d + '/pts'); sh('umount', d)
    sh('mount', '-t', 'tmpfs', '-o', 'mode=755', 'tmpfs', d)
    for name, (maj, mino) in {'null': (1, 3), 'zero': (1, 5), 'full': (1, 7), 'random': (1, 8), 'urandom': (1, 9), 'tty': (5, 0)}.items():
        os.mknod('%s/%s' % (d, name), 0o666 | stat.S_IFCHR, os.makedev(maj, mino))
    os.makedirs(d + '/pts', exist_ok=True)
    sh('mount', '--bind', '/dev/pts', d + '/pts')
    shutil.copy(PATCH, d + '/bbd_patch')
    os.chmod(d + '/bbd_patch', 0o666)
    for p in ('sys/class/sec/sensorhub', 'sys/class/sec/gps/GPS_PWR_EN', 'sys/devices/platform/bbd.0'):
        os.makedirs('%s/%s' % (ROOT, p), exist_ok=True)
    for f in ('sys/class/sec/sensorhub/mcu_power', 'sys/class/sec/gps/GPS_PWR_EN/value') + tuple(
            'sys/devices/platform/bbd.0/' + n for n in ('BBD', 'DEV', 'ESW', 'baud', 'debug', 'buf', 'pl', 'passthru', 'ssi_xfer', 'ssi_count', 'ssi_trace', 'shmd')):
        open('%s/%s' % (ROOT, f), 'w').write('0\n'); os.chmod('%s/%s' % (ROOT, f), 0o666)


def new_pty(name):
    m, s = os.openpty()
    tty.setraw(m); tty.setraw(s)
    path = os.ttyname(s)
    link = '%s/dev/%s' % (ROOT, name)
    if os.path.lexists(link): os.unlink(link)
    os.symlink(path, link)
    os.chmod(path, 0o666)
    return m, s


def main():
    setup_root()
    hub = s10shub.Hub(log=log, spi_speed=int(os.environ.get('SPI_HZ', '12000000')))
    tbcm, tbcm_s = new_pty('ttyBCM')
    ctl, ctl_s = new_pty('bbd_control')
    sens, sens_s = new_pty('bbd_sensor')
    pwrfile = ROOT + '/sys/class/sec/gps/GPS_PWR_EN/value'
    last = open(pwrfile).read().strip(); powered = False
    log('bridge ready; waiting for lhd')
    txq = b''
    last_poll = 0
    cap = open('/tmp/ssi.cap', 'wb') if os.environ.get('CAPTURE') else None
    def capture(direction, data):
        if cap is None: return
        cap.write(bytes([direction]) + int(time.time() * 1000).to_bytes(8, 'little') + len(data).to_bytes(4, 'little') + data); cap.flush()
    while True:
        # power control from lhd
        try: cur = open(pwrfile).read().strip()
        except OSError: cur = last
        if cur != last:
            last = cur
            log('GPS_PWR_EN ->', cur)
            if cur.startswith('1') and not powered:
                hub.req.set(0); hub.pwr.set(1); time.sleep(0.3); hub.req.set(1); time.sleep(0.01); hub.spi.xfer(bytes(8)); powered = True
                log('hub powered')
            elif cur.startswith('0'):
                hub.req.set(0); hub.pwr.set(0); powered = False; log('hub off')
        r, _, _ = select.select([tbcm, ctl, sens], [], [], 0.0005 if powered else 0.05)
        for fd in r:
            data = os.read(fd, 4096)
            if fd == tbcm:
                if powered:
                    try:
                        for off in range(0, len(data), 2000):
                            hub.tx_all(data[off:off + 2000])
                    except Exception as e: log('ssi tx error', e)
                capture(0, data)
                if os.environ.get('VERBOSE'): log('lhd->hub %d B' % len(data))
            elif fd == ctl:
                log('control:', data[:80])
            else:
                if os.environ.get('VERBOSE'): log('sensor:', data[:40].hex(' '))
        if powered:
            # like the kernel driver: read whenever HOST_REQ is high (drain), plus a slow safety poll
            n = 0
            while n < 64 and (hub.host.get() or time.time() - last_poll > 0.05):
                last_poll = time.time(); n += 1
                try:
                    d = hub.ssi_rx()
                except Exception as e:
                    d = b''; log('ssi rx error', e)
                if not d:
                    break
                os.write(tbcm, d)
                capture(1, d)
                if os.environ.get('VERBOSE'): log('hub->lhd %d B' % len(d))


if __name__ == '__main__':
    main()
