// SPDX-License-Identifier: GPL-2.0
/* Test the real CPU_PM_ENTER_FAILED unwind, without ever calling CPU_SUSPEND. */
#include <linux/cpu.h>
#include <linux/cpu_pm.h>
#include <linux/io.h>
#include <linux/limits.h>
#include <linux/module.h>
#include <linux/of.h>
#include <linux/psci.h>
#include <linux/workqueue.h>
#include <uapi/linux/psci.h>
#include <asm/cputype.h>

static unsigned int cpu;
module_param(cpu, uint, 0400);
MODULE_PARM_DESC(cpu, "Online CPU for the single aborted-entry test (default 0)");
static bool query_features;
module_param(query_features, bool, 0400);
MODULE_PARM_DESC(query_features, "Also query PSCI_VERSION/FEATURES (never change suspend mode)");
static DEFINE_PER_CPU(bool, veto_armed);
static void __iomem *pmu, *intr;
static bool veto_seen, prepared;
static u32 inform, mask, int_en;

static void query_firmware(void)
{
	struct arm_smccc_res res;

	/* Legacy DT initialization reports 0.1 without asking the firmware. */
	arm_smccc_smc(PSCI_0_2_FN_PSCI_VERSION, 0, 0, 0, 0, 0, 0, 0, &res);
	pr_info("s10-c2-abort-test: direct PSCI_VERSION=%#lx\n", res.a0);
	if ((s32)res.a0 < 0 || PSCI_VERSION_MAJOR((u32)res.a0) < 1)
		return;
	arm_smccc_smc(PSCI_1_0_FN_PSCI_FEATURES, PSCI_0_2_FN64_CPU_SUSPEND,
		      0, 0, 0, 0, 0, 0, &res);
	pr_info("s10-c2-abort-test: CPU_SUSPEND features=%#lx (OSI bit0, extended-state bit1)\n",
		res.a0);
}

static int veto(struct notifier_block *nb, unsigned long action, void *unused)
{
	if (action != CPU_PM_ENTER || !__this_cpu_read(veto_armed))
		return NOTIFY_OK;

	veto_seen = true;
	prepared = readl(pmu + inform) == 1 &&
		   (readl(intr + 0x200) & mask) &&
		   (readl(pmu + int_en) & BIT(3));
	return notifier_from_errno(-ECANCELED);
}

/* Last: the PMU and context notifiers must have prepared before the veto. */
static struct notifier_block veto_nb = {
	.notifier_call = veto,
	.priority = INT_MIN,
};

static long test_abort(void *unused)
{
	static const u32 offsets[] = {
		0x1044, 0x10c4, 0x1144, 0x11c4,
		0x1344, 0x13c4, 0x1544, 0x15c4,
	};
	u64 mpidr = read_cpuid_mpidr();
	u32 aff0 = MPIDR_AFFINITY_LEVEL(mpidr, 0);
	u32 aff1 = MPIDR_AFFINITY_LEVEL(mpidr, 1);
	unsigned long flags;
	unsigned int slot;
	int ret;
	bool restored;

	if (MPIDR_AFFINITY_LEVEL(mpidr, 2) ||
	    MPIDR_AFFINITY_LEVEL(mpidr, 3))
		return -EINVAL;
	if (!aff1 && aff0 < 6)
		slot = aff0;
	else if (aff1 == 1 && aff0 < 2)
		slot = aff0 + 6;
	else
		return -EINVAL;

	inform = 0x860 + slot * 4;
	mask = BIT(slot);
	int_en = offsets[slot];

	preempt_disable();
	local_irq_save(flags);
	/* Refuse an entry that is already prepared or otherwise unexplained. */
	if (readl(pmu + inform) || (readl(intr + 0x200) & mask) ||
	    (readl(pmu + int_en) & BIT(3))) {
		local_irq_restore(flags);
		preempt_enable();
		return -EBUSY;
	}

	__this_cpu_write(veto_armed, true);
	ret = cpu_pm_enter();
	__this_cpu_write(veto_armed, false);
	if (!ret)
		cpu_pm_exit();
	restored = !readl(pmu + inform) && !(readl(intr + 0x200) & mask) &&
		   !(readl(pmu + int_en) & BIT(3));
	local_irq_restore(flags);
	preempt_enable();

	pr_info("s10-c2-abort-test: CPU%u slot%u ret=%d veto=%d prepared=%d restored=%d\n",
		cpu, slot, ret, veto_seen, prepared, restored);
	return ret == -ECANCELED && veto_seen && prepared && restored ? 0 : -EIO;
}

static int __init test_init(void)
{
	const char *marker;
	int ret;

	if (!of_machine_is_compatible("samsung,exynos9820") ||
	    of_property_read_string(of_chosen, "samsung,c2-pmu-transaction-test", &marker) ||
	    strcmp(marker, "pmu-transaction-v1"))
		return -ENODEV;
	if (query_features)
		query_firmware();

	pmu = ioremap(0x15860000, SZ_8K);
	intr = ioremap(0x15870000, SZ_4K);
	if (!pmu || !intr) {
		ret = -ENOMEM;
		goto unmap;
	}
	ret = cpu_pm_register_notifier(&veto_nb);
	if (ret)
		goto unmap;

	cpus_read_lock();
	if (cpu >= nr_cpu_ids || !cpu_online(cpu))
		ret = -ENODEV;
	else
		ret = work_on_cpu(cpu, test_abort, NULL);
	cpus_read_unlock();
	cpu_pm_unregister_notifier(&veto_nb);
unmap:
	if (intr)
		iounmap(intr);
	if (pmu)
		iounmap(pmu);
	if (!ret)
		pr_info("s10-c2-abort-test: PASS; no CPU_SUSPEND was called\n");
	return ret;
}

static void __exit test_exit(void) { }
module_init(test_init);
module_exit(test_exit);
MODULE_LICENSE("GPL");
MODULE_DESCRIPTION("S10 PMU aborted-idle-entry hardware regression test");
