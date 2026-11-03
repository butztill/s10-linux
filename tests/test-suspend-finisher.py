#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Check that the opt-in finisher control cannot call PSCI firmware."""
from pathlib import Path
import subprocess
import tempfile

root = Path(__file__).resolve().parents[1]
src = (root / 'kernel/overlay/drivers/soc/samsung/exynos9820-sleep.c').read_text()
a = src.index('static int sleep_finisher(')
b = src.index('\nstatic void sleep_wakeup_masks(', a)
finisher = src[a:b]
harness = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <string.h>
#include <errno.h>
typedef uint32_t u32;
static bool finisher_control;
static int calls, marks, firmware_ret;
static unsigned long last_state, last_resume;
static char phases[4][40];
static int results[4];
static void cpu_resume(void) {}
#define __pa_symbol(x) 0x80001234UL
#define S10_SLEEP "test: "
#define pr_info(...) ((void)0)
static void s10_phase_mark(const char *kind, const char *phase, int ret) {
    assert(!strcmp(kind, finisher_control ? "control" : "suspend"));
    strcpy(phases[marks], phase); results[marks++] = ret;
}
static int fake_psci(u32 state, unsigned long resume) {
    assert(marks == 1 && !strcmp(phases[0], "before_psci"));
    calls++; last_state = state; last_resume = resume;
    return firmware_ret;
}
static struct { int (*cpu_suspend)(u32, unsigned long); } psci_ops;
''' + finisher + r'''
int main(void) {
    (void)cpu_resume;
    finisher_control = true;
    psci_ops.cpu_suspend = 0;
    assert(sleep_finisher(0x03000000) == -EAGAIN);
    assert(calls == 0 && marks == 1 && results[0] == -EAGAIN);
    assert(!strcmp(phases[0], "finisher_abort"));
    finisher_control = false; marks = 0;
    psci_ops.cpu_suspend = fake_psci; firmware_ret = -7;
    assert(sleep_finisher(0x03000000) == -7);
    assert(calls == 1 && marks == 2);
    assert(last_state == 0x03000000 && last_resume == 0x80001234UL);
    assert(!strcmp(phases[1], "after_psci") && results[1] == -7);
    marks = 0; firmware_ret = 0;
    assert(sleep_finisher(0x03000000) == 0 && calls == 2);
    assert(results[1] == 0);
}
'''
with tempfile.TemporaryDirectory(prefix='s10-finisher-') as tmp:
    c = Path(tmp) / 'test.c'
    exe = Path(tmp) / 'test'
    c.write_text(harness)
    subprocess.run(['cc', '-std=c11', '-Wall', '-Wextra', '-Werror', str(c),
                    '-o', str(exe)], check=True)
    subprocess.run([str(exe)], check=True)
print('Finisher control skips firmware; real call preserves state/target/result: PASS')
