// SPDX-License-Identifier: GPL-2.0
/*
 * Exynos9820 (Galaxy S10) suspend-to-RAM: the SYS_SLEEP path of Samsung's SM-G973F kernel.
 *
 * Stock sequence (drivers/soc/samsung/exynos-pm.c, cal-if/pmucal_system.c):
 *   1. program the PMU wakeup masks and the CPU_INFORM power-mode hint (= 4, SLEEP)
 *   2. snapshot the "save_sleep" registers, then run the PMUCAL "enter_sleep" sequence
 *   3. cpu_suspend() with a finisher issuing PSCI CPU_SUSPEND(affinity level 3 << 24, cpu_resume)
 *   4. run "exit_sleep" (or "earlywkup_sleep" if the firmware returned without sleeping), restore the saved registers
 * The firmware (ACPM/EL3) takes MIF and the SoC down once every master is idle.
 *
 * mem_sleep stays s2idle by default; select with "echo deep > /sys/power/mem_sleep".
 * Module parameter dry_run=1 runs the register sequences but skips the PSCI call (returns at once).
 */

#include <linux/bits.h>
#include <linux/atomic.h>
#include <linux/cpu_pm.h>
#include <linux/delay.h>
#include <linux/init.h>
#include <linux/io.h>
#include <linux/kernel.h>
#include <linux/ktime.h>
#include <linux/memblock.h>
#include <linux/module.h>
#include <linux/of.h>
#include <linux/psci.h>
#include <linux/smp.h>
#include <linux/suspend.h>
#include <linux/sched/debug.h>
#include <linux/nmi.h>
#include <linux/timer.h>
#include <linux/vmalloc.h>
#include <linux/mm.h>

#include <asm/cacheflush.h>
#include <asm/barrier.h>
#include <asm/suspend.h>
#include <asm/sections.h>

#define S10_SLEEP "exynos9820-sleep: "

enum pmucal_seq_acctype {
	PMUCAL_READ = 0,
	PMUCAL_WRITE,
	PMUCAL_COND_READ,
	PMUCAL_COND_WRITE,
	PMUCAL_SAVE_RESTORE,
	PMUCAL_COND_SAVE_RESTORE,
	PMUCAL_WAIT,
	PMUCAL_WAIT_TWO,
	PMUCAL_CHECK_SKIP,
	PMUCAL_COND_CHECK_SKIP,
	PMUCAL_WRITE_WAIT,
	PMUCAL_WRITE_RETRY,
	PMUCAL_WRITE_RETRY_INV,
	PMUCAL_WRITE_RETURN,
	PMUCAL_SET_BIT_ATOMIC,
	PMUCAL_CLR_BIT_ATOMIC,
	PMUCAL_DELAY,
	PMUCAL_CLEAR_PEND,
};

struct pmucal_seq {
	u32 access_type;
	const char *sfr_name;
	phys_addr_t base_pa;
	void __iomem *base_va;
	u32 offset;
	u32 mask;
	u32 value;
	phys_addr_t cond_base_pa;
	void __iomem *cond_base_va;
	u32 cond_offset;
	u32 cond_mask;
	u32 cond_value;
	bool need_restore;
	bool need_skip;
};

#define PMUCAL_SEQ_DESC(_access_type, _sfr_name, _base_pa, _offset,	\
			_mask, _value, _cond_base_pa, _cond_offset,	\
			_cond_mask, _cond_value) {			\
	.access_type = _access_type,					\
	.sfr_name = _sfr_name,						\
	.base_pa = _base_pa,						\
	.offset = _offset,						\
	.mask = _mask,							\
	.value = _value,						\
	.cond_base_pa = _cond_base_pa,					\
	.cond_offset = _cond_offset,					\
	.cond_mask = _cond_mask,					\
	.cond_value = _cond_value,					\
}

#include "exynos9820-sleep-seq.h"

#define PMU_PA			0x15860000
#define PMU_CPU_INFORM(cpu)	(0x0860 + 4 * (cpu))
#define CPU_INFORM_SLEEP	4
#define PMU_WAKEUP_INT_EN	0x3944
#define PMU_WAKEUP2_INT_EN	0x3964
#define PMU_WAKEUP_STAT		0x3950
#define PMU_WAKEUP2_STAT	0x3954
#define PMU_EINT_WAKEUP_MASK	0x060c
#define PMU_EINT_WAKEUP_MASK2	0x061c

/* SYS_SLEEP column of the stock DT exynos-powermode wakeup-masks (1 = wakeup source enabled) */
#define WAKEUP_EN_SLEEP		0x1f6f
#define WAKEUP2_EN_SLEEP	0xf

/* Stock arm_cpuidle_suspend(PSCI_SYSTEM_SLEEP) -> power_state: id 0, type 0, affinity level 3 */
#define PSCI_STATE_SYSTEM_SLEEP	(3u << 24)

static bool dry_run = true;
module_param(dry_run, bool, 0644);
MODULE_PARM_DESC(dry_run, "run the register sequences but skip the PSCI CPU_SUSPEND call");

static unsigned int dry_level = 3;
module_param(dry_level, uint, 0644);
MODULE_PARM_DESC(dry_level, "what the suspend path does: 0=nothing, 1=masks+hint, 2=+enter/exit sequences, 3=+save/restore");

static int dry_ret;
module_param(dry_ret, int, 0644);
MODULE_PARM_DESC(dry_ret, "value dry_level 0 returns from the suspend op (0 = pretend we slept, -11 = abort the suspend)");

static bool finisher_control;
module_param(finisher_control, bool, 0444);
MODULE_PARM_DESC(finisher_control, "boot-only: exercise cpu_suspend but return -EAGAIN from its finisher without calling firmware");

static bool dry_normal_exit;
module_param(dry_normal_exit, bool, 0444);
MODULE_PARM_DESC(dry_normal_exit, "boot-only: dry-run control using the normal exit sequence instead of early-wakeup");

static bool enable_at_boot;
module_param(enable_at_boot, bool, 0444);
MODULE_PARM_DESC(enable_at_boot, "kernel command line exynos_pmu.enable_at_boot=1: register the suspend ops during boot");

static bool seq_trace;
module_param(seq_trace, bool, 0644);
MODULE_PARM_DESC(seq_trace, "log every PMUCAL sequence entry before it runs and fence it with a DSB (finds async bus errors)");

bool s10_sleep_skip_clk_pm;
module_param_named(skip_clk_pm, s10_sleep_skip_clk_pm, bool, 0644);
MODULE_PARM_DESC(skip_clk_pm, "debug: skip the generic samsung_clk_suspend/resume register restore");

static bool flush_cache = true;
module_param(flush_cache, bool, 0644);
MODULE_PARM_DESC(flush_cache, "clean+invalidate the data cache over all RAM right before the PSCI sleep call");

static bool use_cpu_pm;
module_param(use_cpu_pm, bool, 0644);
MODULE_PARM_DESC(use_cpu_pm, "also run CPU_PM notifiers around the sleep (stock does not)");

unsigned int s10_sleep_keep_wdt;
module_param_named(keep_wdt, s10_sleep_keep_wdt, uint, 0644);
MODULE_PARM_DESC(keep_wdt, "debug: keep the watchdog armed across suspend with this timeout in seconds (0 = stop it as usual; only for dry_run)");

static bool trace_suspend;
module_param(trace_suspend, bool, 0644);
MODULE_PARM_DESC(trace_suspend, "keep sleep progress in PMU INFORM3 during suspend");

static bool skip_save;
module_param(skip_save, bool, 0644);
MODULE_PARM_DESC(skip_save, "debug: do not snapshot/restore the save_sleep registers");

/* EINT wakeup mask (1 = masked). Default: only GPA0 (volume/power/wink keys) wakes the system. */
static unsigned int eint_wake_mask = ~0xffu;
module_param(eint_wake_mask, uint, 0644);

static unsigned int eint_wake_mask2 = ~0u;
module_param(eint_wake_mask2, uint, 0644);

static unsigned int wakeup_en = WAKEUP_EN_SLEEP;
module_param(wakeup_en, uint, 0644);
static unsigned int wakeup2_en = WAKEUP2_EN_SLEEP;
module_param(wakeup2_en, uint, 0644);

static void __iomem *pmu;
static void __iomem *alive_gpio;

/* Legacy marker overlaps a ramoops dmesg record; keep it disabled by default. */
#define S10_MARK_PA	0xfc0ff000ULL
static u32 *s10_mark;
static bool legacy_ram_marker;
module_param(legacy_ram_marker, bool, 0400);
MODULE_PARM_DESC(legacy_ram_marker, "unsafe legacy marker in a ramoops record (default off)");

/* A nonzero boot-only identifier enables bounded, run-labelled console markers. */
static unsigned int phase_run_id;
module_param(phase_run_id, uint, 0400);
MODULE_PARM_DESC(phase_run_id, "nonzero run identifier for suspend phase markers");
static atomic_t phase_sequence = ATOMIC_INIT(0);

static void s10_phase_mark(const char *kind, const char *phase, int ret)
{
	if (!phase_run_id)
		return;
	pr_notice("s10-suspend-phase run=%u seq=%d kind=%s phase=%s ret=%d cpu=%u irq_off=%d\n",
		  phase_run_id, atomic_inc_return(&phase_sequence), kind, phase,
		  ret, raw_smp_processor_id(), irqs_disabled());
}

/* Emits synthetic checkpoints only: no suspend, CPU hotplug or PMU access. */
static int phase_control_run(const char *val, bool irqoff)
{
	unsigned long flags = 0;
	bool enabled;
	int ret = kstrtobool(val, &enabled);

	if (ret || !enabled)
		return ret ? ret : -EINVAL;
	if (!phase_run_id)
		return -EPERM;
	if (irqoff)
		local_irq_save(flags);
	s10_phase_mark("control", "after_cpu_offline", 0);
	s10_phase_mark("control", "enter_after_syscore", 0);
	s10_phase_mark("control", "before_system_pmu", 0);
	s10_phase_mark("control", "after_system_pmu", 0);
	s10_phase_mark("control", "before_cpu_suspend", 0);
	s10_phase_mark("control", "after_cpu_suspend", 0);
	if (irqoff) {
		local_irq_restore(flags);
		s10_phase_mark("control", "irq_state_restored", 0);
	}
	return 0;
}
static int phase_control_set(const char *val, const struct kernel_param *kp)
{
	return phase_control_run(val, false);
}
static int phase_control_irqoff_set(const char *val, const struct kernel_param *kp)
{
	return phase_control_run(val, true);
}
static const struct kernel_param_ops phase_control_ops = { .set = phase_control_set };
static const struct kernel_param_ops phase_control_irqoff_ops = { .set = phase_control_irqoff_set };
module_param_cb(phase_control, &phase_control_ops, NULL, 0200);
module_param_cb(phase_control_irqoff, &phase_control_irqoff_ops, NULL, 0200);
MODULE_PARM_DESC(phase_control, "emit synthetic phase markers without hardware accesses");
MODULE_PARM_DESC(phase_control_irqoff, "emit synthetic phase markers with local IRQs disabled");

int s10_acpm_dvfs_ap_state(bool running);	/* drivers/firmware/samsung/exynos-acpm.c */
static bool dvfs_notify = true;
module_param(dvfs_notify, bool, 0644);
MODULE_PARM_DESC(dvfs_notify, "tell the ACPM DVFS firmware that the AP sleeps (stock exynos_devfreq_suspend)");

/* APM flexpmu debug area (stock DT exynos_flexpmu_dbg data-base/size): 16-byte lines, data at +8/+12. */
#define S10_APMDBG_PA	0x0204bc00
#define APMDBG(line, idx)	(s10_apmdbg ? __raw_readl(s10_apmdbg + 16 * (line) + 8 + 4 * (idx)) : 0)
static void __iomem *s10_apmdbg;
/* stock exynos_pm_enter(): line 22 = AP_COUNT_SLEEP (early wakeups, SoC-down count), 23 = MIF_COUNT_SLEEP */
#define APM_EARLY()	APMDBG(22, 0)
#define APM_SOCDN()	APMDBG(22, 1)
#define APM_MIFDN()	APMDBG(23, 1)

/* Opt in only after confirming that this SRAM is readable on the test device.
 * A boot snapshot is not proof that the data survived the previous reset.
 */
static bool apm_snapshot_at_boot;
module_param(apm_snapshot_at_boot, bool, 0400);
MODULE_PARM_DESC(apm_snapshot_at_boot, "read FlexPMU diagnostic records at boot (off by default)");

static int apm_snapshot_format(char *buf, size_t size)
{
	static const u8 lines[] = { 1, 2, 3, 10, 11, 12, 21, 22, 23 };
	unsigned int i;
	int len = 0;

	if (!s10_apmdbg)
		return -ENODEV;

	/* Each record contains two data words at +8 and +12. These are
	 * sequential reads of live firmware data, not an atomic snapshot.
	 */
	for (i = 0; i < ARRAY_SIZE(lines); i++) {
		u32 first = APMDBG(lines[i], 0);
		u32 second = APMDBG(lines[i], 1);

		len += scnprintf(buf + len, size - len, "line%02u=%08x,%08x%s",
				 lines[i], first, second,
				 i + 1 == ARRAY_SIZE(lines) ? "\n" : " ");
	}
	return len;
}
static void mark(u32 v)
{
	if (!s10_mark)
		return;
	WRITE_ONCE(s10_mark[0], 0x4d4b0000 | v);
	WRITE_ONCE(s10_mark[1], (u32)read_sysreg(cntvct_el0));
	dsb(sy);
}

/* Progress marker for hang hunting: survives a watchdog reset (PMU INFORM3). */
#define PMU_PROGRESS		0x080c
static bool trace_progress;
static u32 cur_stage;

static inline void progress(u32 stage, u32 idx)
{
	if (trace_progress)
		__raw_writel(0xa5000000 | (stage << 16) | idx, pmu + PMU_PROGRESS);
}

struct sleep_map {
	phys_addr_t pa;
	void __iomem *va;
};
static struct sleep_map sleep_maps[64];
static unsigned int nr_sleep_maps;

static unsigned long wakeup_count, early_count, fail_count;

static void __iomem *sleep_map(phys_addr_t pa)
{
	unsigned int i;

	for (i = 0; i < nr_sleep_maps; i++)
		if (sleep_maps[i].pa == pa)
			return sleep_maps[i].va;
	if (nr_sleep_maps >= ARRAY_SIZE(sleep_maps))
		return NULL;
	sleep_maps[nr_sleep_maps].va = ioremap(pa, SZ_64K);
	if (!sleep_maps[nr_sleep_maps].va)
		return NULL;
	sleep_maps[nr_sleep_maps].pa = pa;
	return sleep_maps[nr_sleep_maps++].va;
}

static int sleep_phy2virt(struct pmucal_seq *seq, unsigned int n)
{
	unsigned int i;

	for (i = 0; i < n; i++) {
		if (seq[i].access_type == PMUCAL_DELAY)
			continue;
		if (seq[i].base_pa) {
			seq[i].base_va = sleep_map(seq[i].base_pa);
			if (!seq[i].base_va)
				return -ENOMEM;
		}
		if (seq[i].cond_base_pa) {
			seq[i].cond_base_va = sleep_map(seq[i].cond_base_pa);
			if (!seq[i].cond_base_va)
				return -ENOMEM;
		}
	}
	return 0;
}

/* --- PMUCAL register access engine (port of cal-if/pmucal_rae.c) --- */

static bool pcal_cond(struct pmucal_seq *s)
{
	return (__raw_readl(s->cond_base_va + s->cond_offset) & s->cond_mask) == s->cond_value;
}

static bool pcal_value_ok(struct pmucal_seq *s)
{
	u32 reg = __raw_readl(s->base_va + s->offset);

	return (reg & s->mask) == s->value;
}

static int pcal_wait(struct pmucal_seq *s)
{
	unsigned int timeout = 0;

	while (!pcal_value_ok(s)) {
		udelay(1);
		if (++timeout > 2000) {
			pr_err(S10_SLEEP "wait timed out: %s (0x%x)\n", s->sfr_name,
			       __raw_readl(s->base_va + s->offset));
			return -ETIMEDOUT;
		}
	}
	return 0;
}

static void pcal_read(struct pmucal_seq *s)
{
	s->value = __raw_readl(s->base_va + s->offset) & s->mask;
}

static void pcal_write(struct pmucal_seq *s)
{
	if (s->mask == U32_MAX) {
		__raw_writel(s->value, s->base_va + s->offset);
	} else {
		u32 reg = __raw_readl(s->base_va + s->offset);

		reg = (reg & ~s->mask) | (s->value & s->mask);
		__raw_writel(reg, s->base_va + s->offset);
	}
}

static void pcal_clr_pend(struct pmucal_seq *s)
{
	u32 reg = __raw_readl(s->cond_base_va + s->cond_offset) & s->cond_mask;

	__raw_writel(reg & s->mask, s->base_va + s->offset);
}

static int pcal_write_retry(struct pmucal_seq *s, bool inv)
{
	unsigned int timeout = 0, count = 0, i;

	for (;;) {
		bool retry = inv ? !pcal_cond(s) : pcal_cond(s);

		if (!retry)
			return 0;
		for (i = 0; i < 10; i++)
			pcal_write(s);
		count++;
		for (i = 0; i < count; i++)
			pcal_write(s);
		udelay(1);
		if (++timeout > 1000) {
			pr_err(S10_SLEEP "write-retry timed out: %s\n", s->sfr_name);
			return -ETIMEDOUT;
		}
	}
}

static int pcal_handle(struct pmucal_seq *seq, unsigned int n)
{
	unsigned int i;
	int ret;

	for (i = 0; i < n; i++) {
		struct pmucal_seq *s = &seq[i];

		if (seq_trace) {
			dsb(sy);
			pr_info(S10_SLEEP "seq st%u[%u] type=%u %s %#llx+%#x\n", cur_stage, i, s->access_type,
				s->sfr_name, (unsigned long long)s->base_pa, s->offset);
		}

		progress(cur_stage, i);

		switch (s->access_type) {
		case PMUCAL_READ:
			pcal_read(s);
			break;
		case PMUCAL_WRITE:
			pcal_write(s);
			break;
		case PMUCAL_COND_READ:
			if (pcal_cond(s))
				pcal_read(s);
			break;
		case PMUCAL_COND_WRITE:
			if (pcal_cond(s))
				pcal_write(s);
			break;
		case PMUCAL_WAIT:
		case PMUCAL_WAIT_TWO:
			ret = pcal_wait(s);
			if (ret)
				return ret;
			break;
		case PMUCAL_WRITE_WAIT:
			pcal_write(s);
			ret = pcal_wait(s);
			if (ret)
				return ret;
			break;
		case PMUCAL_WRITE_RETRY:
		case PMUCAL_WRITE_RETRY_INV:
			ret = pcal_write_retry(s, s->access_type == PMUCAL_WRITE_RETRY_INV);
			if (ret)
				return ret;
			break;
		case PMUCAL_WRITE_RETURN:
			pcal_write(s);
			return 0;
		case PMUCAL_DELAY:
			udelay(s->value);
			break;
		case PMUCAL_SET_BIT_ATOMIC:
			if (s->offset <= 0x3fff)
				__raw_writel(s->value, s->base_va + (s->offset | 0xc000));
			break;
		case PMUCAL_CLR_BIT_ATOMIC:
			if (s->offset <= 0x3fff)
				__raw_writel(s->value, s->base_va + (s->offset | 0x8000));
			break;
		case PMUCAL_CLEAR_PEND:
			pcal_clr_pend(s);
			break;
		default:
			pr_err(S10_SLEEP "invalid access type %u\n", s->access_type);
			return -EINVAL;
		}
	}
	return 0;
}


/*
 * save_sleep entries that live in switchable power domains: Samsung's table reads them when the PMU status says
 * "domain on", but Linux has gated the domain's bus clock by then (e.g. ISPPRE after its drivers suspended) and
 * the read raises a synchronous external abort. The Linux drivers re-initialise those blocks on power-up, so
 * skip them for save and restore.
 */
static bool pcal_base_skipped(phys_addr_t pa)
{
	switch (pa) {
	case 0x18c00000:	/* AUD */
	case 0x15500000:	/* VTS */
	case 0x17600000:	/* VRA2 */
	case 0x18000000:	/* IVA */
	case 0x18800000:	/* MFC */
	case 0x17000000:	/* ISPPRE */
	case 0x17400000:	/* ISPLP */
	case 0x17700000:	/* ISPHQ */
	case 0x17900000:
	case 0x17a00000:
	case 0x17c00000:
	case 0x17f00000:
	case 0x18400000:	/* G3D */
	case 0x18a00000:
	case 0x19000000:	/* DPU */
		return true;
	default:
		return false;
	}
}

static void pcal_save(struct pmucal_seq *seq, unsigned int n)
{
	unsigned int i;

	for (i = 0; i < n; i++) {
		struct pmucal_seq *s = &seq[i];

		if (pcal_base_skipped(s->base_pa)) {
			s->need_restore = false;
			continue;
		}

		progress(cur_stage, i);

		switch (s->access_type) {
		case PMUCAL_COND_SAVE_RESTORE:
			if (!pcal_cond(s))
				break;
			fallthrough;
		case PMUCAL_SAVE_RESTORE:
			pcal_read(s);
			s->need_restore = true;
			break;
		case PMUCAL_READ:
			pcal_read(s);
			s->need_restore = false;
			break;
		case PMUCAL_COND_READ:
			if (pcal_cond(s))
				pcal_read(s);
			break;
		case PMUCAL_CLEAR_PEND:
			pcal_clr_pend(s);
			break;
		default:
			break;
		}
	}
}

static int pcal_restore(struct pmucal_seq *seq, unsigned int n)
{
	unsigned int i;
	int ret;

	for (i = 0; i < n; i++) {
		struct pmucal_seq *s = &seq[i];

		progress(cur_stage, i);

		if (s->need_skip) {
			s->need_skip = false;
			continue;
		}
		if (pcal_base_skipped(s->base_pa))
			continue;

		switch (s->access_type) {
		case PMUCAL_COND_SAVE_RESTORE:
			if (!pcal_cond(s))
				break;
			fallthrough;
		case PMUCAL_SAVE_RESTORE:
			if (s->need_restore) {
				s->need_restore = false;
				if (seq_trace) {
					dsb(sy);
					pr_info(S10_SLEEP "restore[%u] %s %#llx+%#x\n", i, s->sfr_name,
						(unsigned long long)s->base_pa, s->offset);
				}
				pcal_write(s);
				if (seq_trace)
					dsb(sy);
			}
			break;
		case PMUCAL_CHECK_SKIP:
			if (i + 1 < n)
				seq[i + 1].need_skip = pcal_value_ok(s);
			break;
		case PMUCAL_COND_CHECK_SKIP:
			if (i + 1 < n)
				seq[i + 1].need_skip = pcal_cond(s) ? pcal_value_ok(s) : true;
			break;
		case PMUCAL_WAIT:
		case PMUCAL_WAIT_TWO:
			ret = pcal_wait(s);
			if (ret)
				return ret;
			break;
		case PMUCAL_WRITE:
			pcal_write(s);
			break;
		case PMUCAL_CLEAR_PEND:
			pcal_clr_pend(s);
			break;
		default:
			break;
		}
	}
	return 0;
}

/* --- suspend ops --- */

static int sleep_finisher(unsigned long state)
{
	int r;

	if (finisher_control) {
		s10_phase_mark("control", "finisher_abort", -EAGAIN);
		return -EAGAIN;
	}

	s10_phase_mark("suspend", "before_psci", 0);
	r = psci_ops.cpu_suspend((u32)state, __pa_symbol(cpu_resume));
	s10_phase_mark("suspend", "after_psci", r);

	pr_info(S10_SLEEP "PSCI CPU_SUSPEND returned %d (did not sleep)\n", r);
	return r;
}

static void sleep_wakeup_masks(void)
{
	__raw_writel(0, pmu + PMU_WAKEUP_STAT);
	__raw_writel(wakeup_en, pmu + PMU_WAKEUP_INT_EN);
	__raw_writel(0, pmu + PMU_WAKEUP2_STAT);
	__raw_writel(wakeup2_en, pmu + PMU_WAKEUP2_INT_EN);
	__raw_writel(eint_wake_mask, pmu + PMU_EINT_WAKEUP_MASK);
	__raw_writel(eint_wake_mask2, pmu + PMU_EINT_WAKEUP_MASK2);
}


/* Called after secondary CPUs are offline, immediately before syscore suspend. */
void arch_suspend_disable_irqs(void)
{
	local_irq_disable();
	s10_phase_mark("suspend", "after_cpu_offline", 0);
}

/*
 * Override the weak default: log around the irq enable that follows syscore_resume() in suspend_enter(), and show the
 * GIC distributor state (it may have been lost in the system sleep).
 */
#define S10_GICD_PA	0x10101000
static void __iomem *gicd;

static void s10_gic_state(const char *tag)
{
	if (!gicd)
		return;
	pr_info(S10_SLEEP "gic %s: GICD_CTLR=%#x TYPER=%#x ISENABLER0..2=%#x %#x %#x PIDR2=%#x\n", tag,
		__raw_readl(gicd + 0x0), __raw_readl(gicd + 0x4), __raw_readl(gicd + 0x100),
		__raw_readl(gicd + 0x104), __raw_readl(gicd + 0x108), __raw_readl(gicd + 0xfe8));
	pr_info(S10_SLEEP "gic %s: ISPENDR0..5=%#x %#x %#x %#x %#x %#x ISACTIVER0..2=%#x %#x %#x\n", tag,
		__raw_readl(gicd + 0x200), __raw_readl(gicd + 0x204), __raw_readl(gicd + 0x208),
		__raw_readl(gicd + 0x20c), __raw_readl(gicd + 0x210), __raw_readl(gicd + 0x214),
		__raw_readl(gicd + 0x300), __raw_readl(gicd + 0x304), __raw_readl(gicd + 0x308));
}

extern atomic_t s10_irq_trace;

void arch_suspend_enable_irqs(void)
{
	mark(0x50);
	pr_info(S10_SLEEP "arch_suspend_enable_irqs: before\n");
	s10_gic_state("before-irq-enable");
	atomic_set(&s10_irq_trace, 60);
	local_irq_enable();
	mark(0x60);
	pr_info(S10_SLEEP "arch_suspend_enable_irqs: irqs are on\n");
}


/*
 * Make sure no dirty line is left in the CPU caches when the system powers down: after the sleep the kernel found
 * inconsistent hrtimer queue entries, which looks like data that was never written back. All other CPUs are
 * offline here, so a by-address clean+invalidate to the PoC over the whole linear map is enough.
 */
static void s10_flush_all_ram(void)
{
	phys_addr_t start, end;
	u64 i;

	for_each_mem_range(i, &start, &end)
		dcache_clean_inval_poc((unsigned long)phys_to_virt(start), (unsigned long)phys_to_virt(end));
	dsb(sy);
	pr_info(S10_SLEEP "flushed all RAM from the caches\n");
}

static int s10_sleep_enter(suspend_state_t state)
{
	unsigned int cpu = smp_processor_id();
	u32 stat, stat2;
	int ret, early = 0;

	if (num_online_cpus() != 1 || cpu != 0) {
		s10_phase_mark("suspend", "enter_after_syscore", -EBUSY);
		pr_err(S10_SLEEP "refusing: online cpus %u, running on cpu%u\n", num_online_cpus(), cpu);
		return -EBUSY;
	}
	s10_phase_mark("suspend", "enter_after_syscore", 0);

	if (use_cpu_pm) {
		ret = cpu_pm_enter();
		if (ret)
			return ret;
	}

	if (dry_run && !dry_level)
		return dry_ret;

	s10_phase_mark("suspend", "before_system_pmu", 0);
	sleep_wakeup_masks();
	__raw_writel(CPU_INFORM_SLEEP, pmu + PMU_CPU_INFORM(cpu));

	if (dry_run && dry_level < 2) {
		__raw_writel(0, pmu + PMU_CPU_INFORM(cpu));
		pr_info(S10_SLEEP "dry_level 1: masks+hint only\n");
		return 0;
	}

	trace_progress = trace_suspend;
	/* Stock pmucal_system_enter(): save the clock/PLL state first, then run the enter sequence. */
	cur_stage = 2;
	if (!skip_save && !(dry_run && dry_level < 3))
		pcal_save(s10_save_sleep, ARRAY_SIZE(s10_save_sleep));
	cur_stage = 1;
	ret = pcal_handle(s10_enter_sleep, ARRAY_SIZE(s10_enter_sleep));
	s10_phase_mark("suspend", "after_system_pmu", ret);
	if (ret) {
		pr_err(S10_SLEEP "enter sequence failed: %d\n", ret);
		fail_count++;
		goto out;
	}

	if (trace_progress)
		__raw_writel(0xa5ee0000, pmu + PMU_PROGRESS);	/* about to enter the PSCI call */
	if (dry_run && !finisher_control) {
		early = !dry_normal_exit;
	} else {
		int cret;
		u32 apm_before[3];

		if (flush_cache)
			s10_flush_all_ram();
		apm_before[0] = APM_EARLY(); apm_before[1] = APM_SOCDN(); apm_before[2] = APM_MIFDN();
		pr_info(S10_SLEEP "calling cpu_suspend(%#x), finisher_control=%d\n",
			PSCI_STATE_SYSTEM_SLEEP, finisher_control);
		s10_phase_mark("suspend", "before_cpu_suspend", 0);
		mark(0x10);
		cret = cpu_suspend(PSCI_STATE_SYSTEM_SLEEP, sleep_finisher);
		mark(0x20 | (cret ? 1 : 0));
		s10_phase_mark("suspend", "after_cpu_suspend", cret);
		pr_info(S10_SLEEP "back from cpu_suspend: ret=%d (0 = resumed from sleep)\n", cret);
		pr_info(S10_SLEEP "APM counters early/socdn/mifdn: %u/%u/%u -> %u/%u/%u\n",
			apm_before[0], apm_before[1], apm_before[2], APM_EARLY(), APM_SOCDN(), APM_MIFDN());
		early = cret != 0;
	}

	if (trace_progress)
		__raw_writel(0xa5ee0001, pmu + PMU_PROGRESS);	/* back from the PSCI call */
	if (early) {
		early_count++;
		cur_stage = 3;
		ret = pcal_handle(s10_earlywkup_sleep, ARRAY_SIZE(s10_earlywkup_sleep));
	} else {
		cur_stage = 4;
		ret = pcal_handle(s10_exit_sleep, ARRAY_SIZE(s10_exit_sleep));
	}
	mark(0x30);
	cur_stage = 5;
	if (!ret && !skip_save && !(dry_run && dry_level < 3))
		ret = pcal_restore(s10_save_sleep, ARRAY_SIZE(s10_save_sleep));
	if (ret) {
		pr_err(S10_SLEEP "exit sequence failed: %d\n", ret);
		fail_count++;
	}
	wakeup_count++;
	mark(0x40);

out:
	trace_progress = false;
	__raw_writel(0, pmu + PMU_CPU_INFORM(cpu));
	stat = __raw_readl(pmu + PMU_WAKEUP_STAT);
	stat2 = __raw_readl(pmu + PMU_WAKEUP2_STAT);
	if (use_cpu_pm)
		cpu_pm_exit();
	pr_info(S10_SLEEP "resumed (%s), WAKEUP_STAT=%#x WAKEUP2_STAT=%#x\n",
		dry_run ? (early ? "early/dry" : "normal-exit/dry") :
		(early ? "early" : "slept"), stat, stat2);
	if (alive_gpio)
		pr_info(S10_SLEEP "EINT_PEND %08x %08x %08x %08x mask %08x %08x\n",
			__raw_readl(alive_gpio + 0xa00), __raw_readl(alive_gpio + 0xa04),
			__raw_readl(alive_gpio + 0xa08), __raw_readl(alive_gpio + 0xa0c),
			__raw_readl(pmu + PMU_EINT_WAKEUP_MASK), __raw_readl(pmu + PMU_EINT_WAKEUP_MASK2));
	s10_gic_state("right-after-wake");
	return ret;
}

/*
 * Run the register sequences from process context (no suspend, watchdog still armed, so a bus hang resets the phone
 * instead of freezing it): write a bitmask to 'selftest': 1=enter_sleep 2=save 4=earlywkup 8=exit 16=restore 32=wakeup masks 64=CPU_INFORM SLEEP hint.
 * With trace_progress=1 the stage/index being processed is kept in PMU INFORM3 (0xa5SSIIII) for post-mortem reads.
 */
static int selftest_set(const char *val, const struct kernel_param *kp)
{
	unsigned int mask;
	unsigned long flags;
	int ret = kstrtouint(val, 0, &mask);

	if (ret)
		return ret;
	local_irq_save(flags);
	trace_progress = true;
	if (mask & 0x20)
		sleep_wakeup_masks();
	if (mask & 0x40)
		__raw_writel(CPU_INFORM_SLEEP, pmu + PMU_CPU_INFORM(smp_processor_id()));
	if (mask & 1) {
		cur_stage = 1;
		ret = pcal_handle(s10_enter_sleep, ARRAY_SIZE(s10_enter_sleep));
	}
	if (!ret && (mask & 2)) {
		cur_stage = 2;
		pcal_save(s10_save_sleep, ARRAY_SIZE(s10_save_sleep));
	}
	if (!ret && (mask & 4)) {
		cur_stage = 3;
		ret = pcal_handle(s10_earlywkup_sleep, ARRAY_SIZE(s10_earlywkup_sleep));
	}
	if (!ret && (mask & 8)) {
		cur_stage = 4;
		ret = pcal_handle(s10_exit_sleep, ARRAY_SIZE(s10_exit_sleep));
	}
	if (!ret && (mask & 16)) {
		cur_stage = 5;
		ret = pcal_restore(s10_save_sleep, ARRAY_SIZE(s10_save_sleep));
	}
	if (mask & 0x40)
		__raw_writel(0, pmu + PMU_CPU_INFORM(smp_processor_id()));
	__raw_writel(0xa5ff0000 | (u32)mask, pmu + PMU_PROGRESS);	/* finished */
	trace_progress = false;
	local_irq_restore(flags);
	pr_info(S10_SLEEP "selftest %#x -> %d\n", mask, ret);
	return ret;
}

static int progress_get(char *buf, const struct kernel_param *kp)
{
	return sysfs_emit(buf, "%#x\n", __raw_readl(pmu + PMU_PROGRESS));
}

/* Find PMU registers that survive a watchdog reset: write patterns, reset, read back with 'peek'. */
static unsigned int peek_off;
static int peek_set(const char *val, const struct kernel_param *kp)
{
	int ret = kstrtouint(val, 0, &peek_off);

	return ret ? ret : (peek_off > 0xfffc || (peek_off & 3)) ? -EINVAL : 0;
}
static int peek_get(char *buf, const struct kernel_param *kp)
{
	return sysfs_emit(buf, "%#x: %#x\n", peek_off, __raw_readl(pmu + peek_off));
}
static int eintdump_get(char *buf, const struct kernel_param *kp)
{
	if (!alive_gpio)
		return sysfs_emit(buf, "n/a\n");
	return sysfs_emit(buf, "pend %08x %08x %08x %08x mask %08x %08x %08x %08x\n",
			  __raw_readl(alive_gpio + 0xa00), __raw_readl(alive_gpio + 0xa04),
			  __raw_readl(alive_gpio + 0xa08), __raw_readl(alive_gpio + 0xa0c),
			  __raw_readl(alive_gpio + 0x900), __raw_readl(alive_gpio + 0x904),
			  __raw_readl(alive_gpio + 0x908), __raw_readl(alive_gpio + 0x90c));
}
static int apmdbg_get(char *buf, const struct kernel_param *kp)
{
	if (!s10_apmdbg)
		return -ENODEV;
	return sysfs_emit(buf, "early=%u socdn=%u mifdn=%u\n", APM_EARLY(), APM_SOCDN(), APM_MIFDN());
}

static int apm_snapshot_get(char *buf, const struct kernel_param *kp)
{
	return apm_snapshot_format(buf, PAGE_SIZE);
}
/* stock flexpmu-dbg "mif_always_on": APM keeps MIF/DRAM up during SYS_SLEEP when set */
static int mif_on_set(const char *val, const struct kernel_param *kp)
{
	bool on;
	int ret = kstrtobool(val, &on);

	if (ret)
		return ret;
	if (!s10_apmdbg)
		return -ENODEV;
	__raw_writel(on, s10_apmdbg + 16 * 21 + 0xc);
	return 0;
}
static int mif_on_get(char *buf, const struct kernel_param *kp)
{
	return sysfs_emit(buf, "%u\n", s10_apmdbg ? __raw_readl(s10_apmdbg + 16 * 21 + 0xc) : 0);
}
static const struct kernel_param_ops mif_on_ops = { .set = mif_on_set, .get = mif_on_get };
module_param_cb(mif_always_on, &mif_on_ops, NULL, 0644);
static const struct kernel_param_ops apmdbg_ops = { .get = apmdbg_get };
module_param_cb(apmdbg, &apmdbg_ops, NULL, 0444);
static const struct kernel_param_ops apm_snapshot_ops = { .get = apm_snapshot_get };
module_param_cb(apm_snapshot, &apm_snapshot_ops, NULL, 0400);
MODULE_PARM_DESC(apm_snapshot, "read selected FlexPMU records as non-atomic raw word pairs");
static const struct kernel_param_ops eintdump_ops = { .get = eintdump_get };
module_param_cb(eintdump, &eintdump_ops, NULL, 0400);
static const struct kernel_param_ops peek_ops = { .set = peek_set, .get = peek_get };
module_param_cb(peek, &peek_ops, NULL, 0600);

static int patterns_set(const char *val, const struct kernel_param *kp)
{
	unsigned int i;

	/* INFORM scratch registers only (0x800/0x80c hold the reboot reason and stay untouched) */
	for (i = 0; i < 16; i++) {
		if (i == 0 || i == 3)
			continue;
		__raw_writel(0xd0d00000 + i, pmu + 0x800 + 4 * i);
	}
	return 0;
}
static const struct kernel_param_ops patterns_ops = { .set = patterns_set };
module_param_cb(patterns, &patterns_ops, NULL, 0200);

/* Does DRAM survive a watchdog reset? 'rammark' writes a marker into the old ramoops area, 'ramread' shows it. */
#define S10_RAM_PA	0xfc000000ULL
#define S10_RAM_SZ	0x200000
static void __iomem *s10_ram;

static int rammark_set(const char *val, const struct kernel_param *kp)
{
	u32 i;

	if (!legacy_ram_marker)
		return -EPERM;
	if (!s10_ram)
		s10_ram = memremap(S10_RAM_PA, S10_RAM_SZ, MEMREMAP_WB);
	if (!s10_ram)
		return -ENOMEM;
	for (i = 0; i < 16; i++)
		((u32 *)s10_ram)[i] = 0x53313000 + i;	/* "S10\0"+i */
	((u64 *)s10_ram)[8] = ktime_get_ns();
	return 0;
}
static int ramread_get(char *buf, const struct kernel_param *kp)
{
	u32 *p;

	if (!s10_ram)
		s10_ram = memremap(S10_RAM_PA, S10_RAM_SZ, MEMREMAP_WB);
	if (!s10_ram)
		return -ENOMEM;
	p = s10_ram;
	return sysfs_emit(buf, "%08x %08x %08x %08x ts=%llu\n", p[0], p[1], p[2], p[15], ((u64 *)s10_ram)[8]);
}
static const struct kernel_param_ops rammark_ops = { .set = rammark_set };
static const struct kernel_param_ops ramread_ops = { .get = ramread_get };
module_param_cb(rammark, &rammark_ops, NULL, 0200);
module_param_cb(ramread, &ramread_ops, NULL, 0400);

static const struct kernel_param_ops progress_ops = { .get = progress_get };
module_param_cb(progress, &progress_ops, NULL, 0400);

static const struct kernel_param_ops selftest_ops = { .set = selftest_set };
module_param_cb(selftest, &selftest_ops, NULL, 0200);


/*
 * Diagnostics for the deep-suspend hang: at the platform_suspend_ops hooks, read the registers of Samsung's own
 * save_sleep table (known to be readable: the selftest reads all of them) and log, by name, the ones that changed
 * between the stages begin -> after-devices -> after-noirq -> before-noirq-resume. Enable with exynos_pmu.diag=1.
 * Reading any other register is not safe: the PMU 0x28c0/0x3900 range and some CMU ranges raise a synchronous
 * external abort that cannot be fixed up and kills the caller while it holds the parameter lock.
 */
static bool diag;
module_param(diag, bool, 0644);
MODULE_PARM_DESC(diag, "log changes of the save_sleep registers between the suspend stages (debug)");

#define DIAG_STAGES	4
static u32 *diag_buf[DIAG_STAGES];
static const char *const diag_stage_name[DIAG_STAGES] = { "begin", "after-devices", "after-noirq", "before-noirq-resume" };

static bool diag_wanted(struct pmucal_seq *q)
{
	if (pcal_base_skipped(q->base_pa))
		return false;
	switch (q->access_type) {
	case PMUCAL_COND_SAVE_RESTORE:
	case PMUCAL_COND_READ:
		return pcal_cond(q);
	case PMUCAL_SAVE_RESTORE:
	case PMUCAL_READ:
		return true;
	default:
		return false;
	}
}

static void diag_snap(unsigned int stage)
{
	unsigned int i, shown = 0;
	const unsigned int n = ARRAY_SIZE(s10_save_sleep);

	if (!diag)
		return;
	if (!diag_buf[stage]) {
		diag_buf[stage] = kvmalloc_array(n, sizeof(u32), GFP_KERNEL);
		if (!diag_buf[stage])
			return;
	}
	for (i = 0; i < n; i++) {
		struct pmucal_seq *q = &s10_save_sleep[i];

		diag_buf[stage][i] = diag_wanted(q) ? (__raw_readl(q->base_va + q->offset) & q->mask) : 0xdead0000;
	}
	if (!stage || !diag_buf[stage - 1])
		return;
	pr_info(S10_SLEEP "diag: changes %s -> %s\n", diag_stage_name[stage - 1], diag_stage_name[stage]);
	for (i = 0; i < n; i++) {
		u32 x = diag_buf[stage - 1][i], y = diag_buf[stage][i];

		if (x != y && shown++ < 120)
			pr_info(S10_SLEEP "diag: %s: %#x -> %#x\n", s10_save_sleep[i].sfr_name, x, y);
	}
	pr_info(S10_SLEEP "diag: %u registers changed\n", shown);
}


/*
 * Timer-based state dump: kernel threads (khungtaskd, workqueues) are frozen during suspend, but timers still fire.
 * With exynos_pmu.dump_secs=N the .begin hook arms timers at N and N+4 s that print every blocked (D-state) task
 * and a backtrace of all CPUs. Goes to the console, i.e. into the reset-proof ramoops log.
 */
static unsigned int dump_secs;
module_param(dump_secs, uint, 0644);
MODULE_PARM_DESC(dump_secs, "seconds after suspend begin to dump blocked tasks (0 = off)");

static struct timer_list s10_dump_timer[2];

static void s10_dump_fn(struct timer_list *t)
{
	pr_info(S10_SLEEP "=== state dump %ld on cpu%d (jiffies %lu) ===\n", (long)(t - s10_dump_timer), raw_smp_processor_id(), jiffies);
	show_state_filter(TASK_UNINTERRUPTIBLE);
	trigger_all_cpu_backtrace();
}

static void s10_dump_arm(void)
{
	unsigned int i;

	if (!dump_secs)
		return;
	for (i = 0; i < 2; i++) {
		/* arm on CPUs other than the one running the suspend: that CPU may be the one that gets stuck */
		unsigned int cpu = (raw_smp_processor_id() + 3 + 2 * i) % num_possible_cpus();

		timer_setup(&s10_dump_timer[i], s10_dump_fn, TIMER_PINNED);
		s10_dump_timer[i].expires = jiffies + (dump_secs + 4 * i) * HZ;
		add_timer_on(&s10_dump_timer[i], cpu);
	}
}

static void s10_dump_disarm(void)
{
	unsigned int i;

	for (i = 0; i < 2; i++)
		if (s10_dump_timer[i].function)
			timer_delete_sync(&s10_dump_timer[i]);
}

static int s10_diag_begin(suspend_state_t state)
{
	s10_dump_arm();
	diag_snap(0);
	return 0;
}

static bool dvfs_released;

static int s10_diag_prepare(void)
{
	diag_snap(1);
	if (dvfs_notify) {
		int ret = s10_acpm_dvfs_ap_state(false);

		dvfs_released = !ret;
		pr_info(S10_SLEEP "ACPM DVFS: AP sleeping -> %d\n", ret);
	}
	return 0;
}

static int s10_diag_prepare_late(void)
{
	diag_snap(2);
	return 0;
}

static void s10_diag_end(void)
{
	mark(0x80);
	if (dvfs_released) {
		pr_info(S10_SLEEP "ACPM DVFS: AP running -> %d\n", s10_acpm_dvfs_ap_state(true));
		dvfs_released = false;
	}
	s10_dump_disarm();
}

static void s10_diag_wake(void)
{
	mark(0x70);
	diag_snap(3);
}

static const struct platform_suspend_ops s10_sleep_ops = {
	.begin = s10_diag_begin,
	.prepare = s10_diag_prepare,
	.prepare_late = s10_diag_prepare_late,
	.wake = s10_diag_wake,
	.end = s10_diag_end,
	.valid = suspend_valid_only_mem,
	.enter = s10_sleep_enter,
};

static int __init s10_sleep_init(void)
{
	int ret;

	if (!of_machine_is_compatible("samsung,exynos9820"))
		return 0;
	if (!psci_ops.cpu_suspend) {
		pr_err(S10_SLEEP "no PSCI CPU_SUSPEND\n");
		return -ENODEV;
	}

	{
		void *r = memremap(S10_RAM_PA, 64, MEMREMAP_WB);

		if (r) {
			pr_info(S10_SLEEP "ram marker at boot: %08x %08x %08x %08x\n", ((u32 *)r)[0], ((u32 *)r)[1],
				((u32 *)r)[2], ((u32 *)r)[15]);
			memunmap(r);
		}
	}
	pmu = ioremap(PMU_PA, SZ_64K);
	if (!pmu)
		return -ENOMEM;
	/* Resume diagnostics run with IRQs off; ioremap may sleep. */
	gicd = ioremap(S10_GICD_PA, SZ_4K);
	if (!gicd)
		pr_warn(S10_SLEEP "GIC diagnostics unavailable\n");
	alive_gpio = ioremap(0x15850000, SZ_4K);
	s10_apmdbg = ioremap(S10_APMDBG_PA, 0x400);
	if (s10_apmdbg)
		pr_info(S10_SLEEP "APM counters at boot early/socdn/mifdn: %u/%u/%u\n",
			APM_EARLY(), APM_SOCDN(), APM_MIFDN());
	if (apm_snapshot_at_boot) {
		char snapshot[256];

		ret = apm_snapshot_format(snapshot, sizeof(snapshot));
		if (ret < 0)
			pr_warn(S10_SLEEP "APM boot snapshot unavailable: %d\n", ret);
		else
			pr_info(S10_SLEEP "APM boot snapshot (non-atomic): %s", snapshot);
	}
	if (legacy_ram_marker)
		s10_mark = memremap(S10_MARK_PA, SZ_4K, MEMREMAP_WC);
	if (s10_mark) {
		pr_info(S10_SLEEP "stage mark from previous boot: %08x t=%08x\n", s10_mark[0], s10_mark[1]);
		mark(0x01);
	}

	ret = sleep_phy2virt(s10_enter_sleep, ARRAY_SIZE(s10_enter_sleep));
	if (!ret)
		ret = sleep_phy2virt(s10_save_sleep, ARRAY_SIZE(s10_save_sleep));
	if (!ret)
		ret = sleep_phy2virt(s10_exit_sleep, ARRAY_SIZE(s10_exit_sleep));
	if (!ret)
		ret = sleep_phy2virt(s10_earlywkup_sleep, ARRAY_SIZE(s10_earlywkup_sleep));
	if (ret) {
		pr_err(S10_SLEEP "mapping failed: %d\n", ret);
		return ret;
	}

	if (enable_at_boot) {
		suspend_set_ops(&s10_sleep_ops);
		pr_info(S10_SLEEP "suspend ops registered at boot (dry_run=%d)\n", dry_run);
		return 0;
	}
	pr_info(S10_SLEEP "ready (dry_run=%d); not active: write 1 to the 'activate' parameter, then 'echo deep > /sys/power/mem_sleep'\n",
		dry_run);
	return 0;
}

/*
 * Registering the ops makes 'deep' the default mem_sleep state, so it stays off until asked for: the deep
 * path currently hangs the phone in the noirq phase (see docs/NOTE-sleep-port-20261027.md).
 */
static int activate_set(const char *val, const struct kernel_param *kp)
{
	bool on;
	int ret = kstrtobool(val, &on);

	if (ret || !on || !pmu)
		return ret ? ret : -EINVAL;
	suspend_set_ops(&s10_sleep_ops);
	pr_info(S10_SLEEP "suspend ops registered; select with 'echo deep > /sys/power/mem_sleep'\n");
	return 0;
}

static const struct kernel_param_ops activate_ops = { .set = activate_set };
module_param_cb(activate, &activate_ops, NULL, 0200);
late_initcall(s10_sleep_init);
