// SPDX-License-Identifier: GPL-2.0
/*
 * Copyright (c) 2026 chiffathefox <chiffathefoxx@gmail.com>
 *
 * EXYNOS9820 PMU (Power Management Unit) support
 */

#include <linux/s10-c2-diag.h>
#include <linux/bits.h>
#include <linux/cpumask.h>
#include <linux/irqflags.h>
#include <linux/moduleparam.h>
#include <linux/regmap.h>
#include <linux/soc/samsung/exynos-pmu.h>
#include <linux/soc/samsung/exynos-regs-pmu.h>
#include <linux/topology.h>

#include <asm/smp_plat.h>

#include "exynos-pmu.h"

#define EXYNOS9820_CPU_INFORM_CPD 2

struct exynos9820_cpu_pmu_regs {
	u32 inform;
	u32 mask;
	u32 int_en;
	unsigned int group;
};

/*
 * PMUCAL core00..03, core10..11, core20..21 use hardware slots 0..7.
 * Aff0 0..5 share Aff1=0; the two Cheetah CPUs use Aff1=1/Aff0=0..1.
 * These slots are not Linux scheduler cluster IDs or logical CPU numbers.
 */
static const u32 exynos9820_cpu_int_en[] = {
	0x1044, 0x10c4, 0x1144, 0x11c4,
	0x1344, 0x13c4, 0x1544, 0x15c4,
};

/*
 * Hardware limitation found by bisecting on the S10: two cores of the same group in C2 at the same time lock the
 * whole system up (deterministic per pair, every pair tested; cores in different groups are fine). Observed groups:
 * {0,2,4}, {1,3,5,6} and {7}. Allow only one sleeping core per group; the veto makes cpuidle fall back to WFI.
 * Protected by ctx->cpupm_lock.
 */
static const u8 exynos9820_c2_group[] = { 0, 1, 0, 1, 0, 1, 1, 2 };
static int exynos9820_c2_group_owner[3] = { -1, -1, -1 };

/*
 * Boot-only experiment for the observed CPU6/7 cluster. A hardware attempt
 * reset before trace results could be retrieved; do not repeat without a
 * diagnostic channel that survives or streams the failing transition.
 */
static bool cpuhp_cluster_hint;
module_param(cpuhp_cluster_hint, bool, 0400);
MODULE_PARM_DESC(cpuhp_cluster_hint, "Experiment: CPU7 last-in-cluster CPD hint");

/* Separate boot-only opt-in: bounded markers for a CPU7 hotplug experiment. */
static bool cpuhp_trace_markers;
module_param(cpuhp_trace_markers, bool, 0400);
MODULE_PARM_DESC(cpuhp_trace_markers, "Trace CPU7 hotplug PMU checkpoints");

static void exynos9820_cpuhp_mark(struct exynos_pmu_context *ctx,
				  unsigned int cpu, const char *phase,
				  unsigned int value, int ret)
{
	if (cpuhp_trace_markers && cpu == 7 && test_bit(cpu, ctx->in_cpuhp))
		pr_notice("s10-cpuhp-mark cpu=%u phase=%s value=%u ret=%d irq_off=%d\n",
			  cpu, phase, value, ret, irqs_disabled());
}

static bool exynos9820_cpuhp_needs_cluster_hint(struct exynos_pmu_context *ctx,
						unsigned int cpu, u32 slot_mask)
{
	unsigned int sibling;
	int cluster_id, package_id;
	u64 sibling_mpidr;

	if (!cpuhp_cluster_hint || cpu != 7 || slot_mask != BIT(7) ||
	    !test_bit(cpu, ctx->in_cpuhp))
		return false;
	if (!cpu_possible(6) || cpu_online(6))
		return false;
	/* The scheduler sibling mask shrinks before this callback runs. */
	cluster_id = topology_cluster_id(cpu);
	package_id = topology_physical_package_id(cpu);
	if (cluster_id < 0 || package_id < 0 ||
	    topology_cluster_id(6) != cluster_id ||
	    topology_physical_package_id(6) != package_id)
		return false;
	sibling_mpidr = cpu_logical_map(6);
	if (MPIDR_AFFINITY_LEVEL(sibling_mpidr, 0) != 0 ||
	    MPIDR_AFFINITY_LEVEL(sibling_mpidr, 1) != 1 ||
	    MPIDR_AFFINITY_LEVEL(sibling_mpidr, 2) ||
	    MPIDR_AFFINITY_LEVEL(sibling_mpidr, 3))
		return false;
	for_each_possible_cpu(sibling)
		if (sibling != 6 && sibling != 7 &&
		    topology_cluster_id(sibling) == cluster_id &&
		    topology_physical_package_id(sibling) == package_id)
			return false;
	return true;
}

static int exynos9820_clear_pending(struct exynos_pmu_context *ctx,
				    unsigned int status_reg,
				    unsigned int clear_reg, u32 mask)
{
	u32 pending;
	int ret;

	/* Match PMUCAL_CLEAR_PEND: sample UPEND once, then W1C those bits once. */
	ret = regmap_read(ctx->pmuintrgen, status_reg, &pending);
	if (ret)
		return ret;

	return regmap_write(ctx->pmuintrgen, clear_reg, pending & mask);
}

static int exynos9820_cpu_pmu_regs(unsigned int cpu,
				   struct exynos9820_cpu_pmu_regs *regs)
{
	u64 mpidr = cpu_logical_map(cpu);
	unsigned int aff0 = MPIDR_AFFINITY_LEVEL(mpidr, 0);
	unsigned int aff1 = MPIDR_AFFINITY_LEVEL(mpidr, 1);
	unsigned int slot;

	if (cpu != smp_processor_id())
		return -EINVAL;
	if (MPIDR_AFFINITY_LEVEL(mpidr, 2) ||
	    MPIDR_AFFINITY_LEVEL(mpidr, 3))
		return -EINVAL;

	if (!aff1 && aff0 < 6)
		slot = aff0;
	else if (aff1 == 1 && aff0 < 2)
		slot = 6 + aff0;
	else
		return -EINVAL;

	regs->group = exynos9820_c2_group[slot];
	regs->inform = GS101_CPU_INFORM(slot);
	regs->mask = BIT(slot);
	regs->int_en = exynos9820_cpu_int_en[slot];
	return 0;
}

/* Also used to unwind partially completed entry; attempt every restore step. */
static int exynos9820_cpu_pmu_restore(struct exynos_pmu_context *ctx,
				      const struct exynos9820_cpu_pmu_regs *regs)
{
	int ret, err;

	if (exynos9820_c2_group_owner[regs->group] == (int)smp_processor_id())
		exynos9820_c2_group_owner[regs->group] = -1;

	ret = regmap_write(ctx->pmureg, regs->inform, CPU_INFORM_CLEAR);
	err = regmap_update_bits(ctx->pmuintrgen, EXYNOS_GRP2_INTR_BID_ENABLE,
				 regs->mask, 0);
	if (!ret)
		ret = err;

	err = exynos9820_clear_pending(ctx, EXYNOS_GRP2_INTR_BID_UPEND,
				       EXYNOS_GRP2_INTR_BID_CLEAR, regs->mask);
	if (!ret)
		ret = err;

	err = regmap_update_bits(ctx->pmureg, regs->int_en, BIT(3), 0);
	return ret ? ret : err;
}

static int exynos9820_cpu_pmu_offline(struct exynos_pmu_context *ctx,
				      unsigned int cpu)
	__must_hold(&ctx->cpupm_lock)
{
	struct exynos9820_cpu_pmu_regs regs;
	int ret, restore_ret;

	ret = exynos9820_cpu_pmu_regs(cpu, &regs);
	if (ret)
		return ret;

	/* Idle path only: CPU hotplug programs the PMU unconditionally. */
	if (!test_bit(cpu, ctx->in_cpuhp)) {
		if (exynos9820_c2_group_owner[regs.group] >= 0 &&
		    exynos9820_c2_group_owner[regs.group] != (int)cpu)
			return -EBUSY;
		exynos9820_c2_group_owner[regs.group] = cpu;
	}

	exynos9820_cpuhp_mark(ctx, cpu, "before-local", CPU_INFORM_C2, 0);
	ret = regmap_write(ctx->pmureg, regs.inform, CPU_INFORM_C2);
	exynos9820_cpuhp_mark(ctx, cpu, "after-local", CPU_INFORM_C2, ret);
	if (ret)
		goto restore;
	ret = regmap_update_bits(ctx->pmuintrgen, EXYNOS_GRP2_INTR_BID_ENABLE,
				 regs.mask, regs.mask);
	if (ret)
		goto restore;
	ret = exynos9820_clear_pending(ctx, EXYNOS_GRP1_INTR_BID_UPEND,
				       EXYNOS_GRP1_INTR_BID_CLEAR, regs.mask);
	if (ret)
		goto restore;
	ret = exynos9820_clear_pending(ctx, EXYNOS_GRP1_INTR_BID_UPEND,
				       EXYNOS_GRP1_INTR_BID_CLEAR,
				       regs.mask << 8);
	if (ret)
		goto restore;
	ret = regmap_update_bits(ctx->pmureg, regs.int_en, BIT(3), BIT(3));
	if (ret)
		goto restore;
	/* Stock PMUCAL sets CPD after the local core sequence on the last CPU. */
	if (exynos9820_cpuhp_needs_cluster_hint(ctx, cpu, regs.mask)) {
		exynos9820_cpuhp_mark(ctx, cpu, "before-cpd", EXYNOS9820_CPU_INFORM_CPD, 0);
		ret = regmap_write(ctx->pmureg, regs.inform, EXYNOS9820_CPU_INFORM_CPD);
		exynos9820_cpuhp_mark(ctx, cpu, "after-cpd", EXYNOS9820_CPU_INFORM_CPD, ret);
		if (ret)
			goto restore;
	}
	s10_c2_diag_call(s10_c2_diag_pmu, false, ctx->pmureg, ctx->pmuintrgen);
	return 0;

restore:
	/* A failing CPU_PM_ENTER callback is not sent ENTER_FAILED itself. */
	restore_ret = exynos9820_cpu_pmu_restore(ctx, &regs);
	pr_err_ratelimited("exynos9820-pmu: CPU%u entry failed: %d (restore: %d)\n",
			   cpu, ret, restore_ret);
	return ret;
}

static int exynos9820_cpu_pmu_online(struct exynos_pmu_context *ctx,
				     unsigned int cpu)
	__must_hold(&ctx->cpupm_lock)
{
	struct exynos9820_cpu_pmu_regs regs;
	int ret;

	ret = exynos9820_cpu_pmu_regs(cpu, &regs);
	if (ret)
		return ret;

	s10_c2_diag_call(s10_c2_diag_pmu, true, ctx->pmureg, ctx->pmuintrgen);
	return exynos9820_cpu_pmu_restore(ctx, &regs);
}

const struct exynos_pmu_data exynos9820_pmu_data = {
	.pmu_cpuhp = true,
	.cpu_pmu_offline = exynos9820_cpu_pmu_offline,
	.cpu_pmu_online = exynos9820_cpu_pmu_online,
};
