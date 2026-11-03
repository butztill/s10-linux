#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Health-aware feeder for the Exynos hardware watchdog (10050000.watchdog).

One worker per CPU, pinned with sched_setaffinity, stamps a shared timestamp every 0.5 s. The main loop feeds the
watchdog only while *every* CPU's worker made progress in the last 4 s, so a core that never wakes up from C2 lets
the watchdog expire (reset after the DT timeout, default 20 s; lowered to 10 s here). Never magic-closes on exit
unless started with --release."""
import fcntl, glob, multiprocessing as mp, os, signal, struct, sys, time

WDIOC_SETTIMEOUT = 0xC0045706


def find():
    for d in glob.glob('/sys/class/watchdog/watchdog*'):
        if '10050000' in os.path.realpath(d + '/device'):
            return '/dev/' + os.path.basename(d)
    raise SystemExit('Exynos watchdog not found')


def dump(stale):
    import subprocess
    out = '/home/user/s10-work/hang-%d' % int(time.time())
    os.makedirs(out, exist_ok=True)
    def sh(name, cmd, t=5):
        try:
            r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=t)
            open(os.path.join(out, name), 'w').write(r.stdout + r.stderr)
        except Exception as e:
            open(os.path.join(out, name), 'w').write('ERR %r' % e)
    open(os.path.join(out, 'stale'), 'w').write(repr(stale))
    # Deliberately no PMU regmap dumps: such reads can raise an external abort and turn a stall into a hard reset.
    sh('interrupts', 'cat /proc/interrupts')
    sh('cpuidle', 'for c in 0 1 2 3; do echo cpu$c $(cat /sys/devices/system/cpu/cpu$c/cpuidle/state1/usage) $(cat /sys/devices/system/cpu/cpu$c/cpuidle/state1/rejected) $(cat /sys/devices/system/cpu/cpu$c/cpuidle/state1/disable); done')
    sh('stat', 'cat /proc/stat | head -10')
    sh('dmesg', 'dmesg | tail -60')
    sh('sched', 'cat /proc/sched_debug 2>&1 | head -80')
    os.sync()


def worker(cpu, ts):
    os.sched_setaffinity(0, {cpu})
    try:
        os.sched_setscheduler(0, os.SCHED_FIFO, os.sched_param(1))
    except OSError:
        pass
    while True:
        ts[cpu] = time.monotonic()
        time.sleep(0.5)


if __name__ == '__main__':
    cpus = sorted(os.sched_getaffinity(0))   # CPUs online at start; later offline CPUs are ignored in the stall check
    ts = mp.Array('d', max(cpus) + 1, lock=False)
    for c in cpus:
        ts[c] = time.monotonic()
        mp.Process(target=worker, args=(c, ts), daemon=True).start()
    try:
        os.sched_setscheduler(0, os.SCHED_FIFO, os.sched_param(2))
    except OSError:
        pass
    dev = find()
    fd = os.open(dev, os.O_WRONLY)
    try:
        fcntl.ioctl(fd, WDIOC_SETTIMEOUT, struct.pack('i', 10))
    except OSError:
        pass
    stop = []
    dumped = []
    signal.signal(signal.SIGTERM, lambda *a: stop.append(1))
    print('feeding', dev, 'cpus', cpus, flush=True)
    last = time.monotonic()
    while not stop:
        now = time.monotonic()
        if now - last > 3:
            # The loop slept far longer than 1 s: the system was suspended. Give the per-CPU workers a moment to
            # stamp again instead of reporting a hang (and dumping state) on resume.
            time.sleep(2)
            now = last = time.monotonic()
        last = now
        try:
            online = set(os.sched_getaffinity(0))
        except OSError:
            online = set(cpus)
        stale = [c for c in cpus if c in online and now - ts[c] > 4]
        if stale:
            print('stale cpus', stale, 'not feeding', flush=True)
            if not dumped:
                dumped.append(1)
                dump(stale)
        else:
            os.write(fd, b'k')
        time.sleep(1)
    if '--release' in sys.argv:
        os.write(fd, b'V')
    os.close(fd)
