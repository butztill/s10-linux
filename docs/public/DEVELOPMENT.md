<!-- SPDX-License-Identifier: GPL-2.0-only -->

# Development and diagnostics

Write reports in English using an impersonal, factual style: configuration,
trigger, observation, interpretation and limits. Record successful and failed
outcomes. A console marker proves execution of its logging point, not every
hardware action before it.

## Suspend findings

The normal baseline is s2idle. Host tests exercise phase-marker ordering and
the finisher control. Two boot-only, default-off controls distinguish the ARM
context-save path (`finisher_control`) and the normal PMU exit path without a
firmware sleep call (`dry_normal_exit`). Neither is a production sleep mode.

A real firmware attempt reached `before_psci`, then
`after_cpu_suspend ret=0` without `after_psci`; it returned through the ARM
resume branch. APM early-wake/SoC-down counters increased, MIF-down did not,
and CPU7/UFS recovery failed. No complete, stable deep-sleep cycle has been
established. The normal exit table alone did not reproduce those two failures.
Bluetooth and audio warnings also occur in controls and are separate findings.

The current production UFS system and runtime PM levels are both 3. Earlier
notes used level 5. A controlled comparison must record and restore these
values and distinguish Hibern8 retention from a full link restart.

## Device-test boundaries

Use a sufficiently charged device and a verified recovery path. Disable C2 for
suspend experiments. Use reviewed one-shot test images and explicit run IDs;
never replace a working default entry to run an experiment. After a test,
check the production kernel, all CPUs, s2idle, watchdog and regular services.
Stop and evaluate after at most one required forced recovery in a test series.

Do not issue blanket PMU register dumps, enable `trace_suspend`, write values
from another SoC, or assume APM SRAM survives a reset. The old raw marker area
overlapped a ramoops record and is disabled by default. New early-resume writes
need a separately validated path. Missing markers after battery loss are not
proof that a suspend phase was never reached.

Keep raw dumps and detailed logs local. Public bug reports should contain only
reviewed, minimal excerpts without device identifiers, SIM data, credentials
or proprietary material.
