#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Exercise the actual phase marker/control functions with host mocks."""
from pathlib import Path
import subprocess
import tempfile

root = Path(__file__).resolve().parents[1]
source = (root / 'kernel/overlay/drivers/soc/samsung/exynos9820-sleep.c').read_text()
dt = (root / 'dts/s10-sleep-debug-20261027.dts').read_text()
ramoops = dt.split('ramoops@fc000000 {', 1)[1].split('};', 1)[0]
assert 'reg = <0x00 0xfc000000 0x200000>;' in ramoops
assert 'record-size = <0x10000>;' in ramoops
assert 'console-size = <0x100000>;' in ramoops
# ram.c assigns dmesg records before the console: the old 0xff000
# offset falls in the last of sixteen 64-KiB dmesg records.
assert 0xf0000 <= 0xff000 < 0x100000


def function(signature):
    start = source.index(signature)
    opening = source.index('{', start)
    depth = 1
    end = opening + 1
    while depth:
        depth += (source[end] == '{') - (source[end] == '}')
        end += 1
    return source[start:end]


harness = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdarg.h>
#include <stdio.h>
#include <string.h>
#include <errno.h>
struct kernel_param { int unused; };
static unsigned int phase_run_id;
static int phase_sequence;
static char lines[16][256];
static int n;
static bool fake_irq_disabled;
#define atomic_inc_return(p) (++*(p))
#define raw_smp_processor_id() 0
#define irqs_disabled() fake_irq_disabled
#define local_irq_save(flags) do { (flags) = fake_irq_disabled; fake_irq_disabled = true; } while (0)
#define local_irq_restore(flags) do { fake_irq_disabled = (flags); } while (0)
static void pr_notice(const char *fmt, ...) {
    va_list a; va_start(a, fmt); vsnprintf(lines[n++], 256, fmt, a); va_end(a);
}
static int kstrtobool(const char *s, bool *out) {
    if (!strcmp(s, "1")) { *out = true; return 0; }
    if (!strcmp(s, "0")) { *out = false; return 0; }
    return -EINVAL;
}
''' + '\n'.join(function(signature) for signature in (
    'static void s10_phase_mark(', 'static int phase_control_run(',
    'static int phase_control_set(', 'static int phase_control_irqoff_set(')) + r'''
int main(void) {
    struct kernel_param kp = {0};
    assert(phase_control_set("1", &kp) == -EPERM && n == 0);
    phase_run_id = 123;
    assert(phase_control_set("0", &kp) == -EINVAL && n == 0);
    assert(phase_control_set("bad", &kp) == -EINVAL && n == 0);
    assert(phase_control_set("1", &kp) == 0 && n == 6);
    assert(strstr(lines[0], "run=123 seq=1 kind=control phase=after_cpu_offline ret=0"));
    assert(strstr(lines[1], "seq=2 kind=control phase=enter_after_syscore"));
    assert(strstr(lines[2], "seq=3 kind=control phase=before_system_pmu"));
    assert(strstr(lines[3], "seq=4 kind=control phase=after_system_pmu"));
    assert(strstr(lines[4], "seq=5 kind=control phase=before_cpu_suspend"));
    assert(strstr(lines[5], "seq=6 kind=control phase=after_cpu_suspend"));
    s10_phase_mark("suspend", "after_cpu_suspend", -5);
    assert(strstr(lines[6], "seq=7 kind=suspend phase=after_cpu_suspend ret=-5"));
    assert(phase_control_irqoff_set("bad", &kp) == -EINVAL && !fake_irq_disabled);
    n = 0;
    assert(phase_control_irqoff_set("1", &kp) == 0 && n == 7 && !fake_irq_disabled);
    for (int i=0; i<6; i++) assert(strstr(lines[i], "irq_off=1"));
    assert(strstr(lines[6], "phase=irq_state_restored") && strstr(lines[6], "irq_off=0"));
    n = 0; fake_irq_disabled = true;
    assert(phase_control_irqoff_set("1", &kp) == 0 && fake_irq_disabled);
    assert(strstr(lines[6], "irq_off=1"));
    puts("Suspend phase marker opt-in, ordering and return values: PASS");
}
'''
with tempfile.TemporaryDirectory(prefix='s10-phase-test-') as tmp:
    src = Path(tmp) / 'test.c'
    exe = Path(tmp) / 'test'
    src.write_text(harness)
    subprocess.run(['cc', '-std=c11', '-Wall', '-Wextra', '-Werror',
                    '-Wno-unused-parameter', str(src), '-o', str(exe)], check=True)
    subprocess.run([str(exe)], check=True)
