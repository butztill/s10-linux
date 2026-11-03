// SPDX-License-Identifier: GPL-2.0-only
/* Opt-in CPU0 local-C2 recorder. Never changes a firmware or PMU result. */
#include <linux/s10-c2-diag.h>
#if defined(CONFIG_ARM64) && defined(CONFIG_ARM_PSCI_CPUIDLE) && defined(CONFIG_DEBUG_FS)
#include <linux/arm-smccc.h>
#include <linux/cpu.h>
#include <linux/cpuidle.h>
#include <linux/debugfs.h>
#include <linux/init.h>
#include <linux/mutex.h>
#include <linux/of.h>
#include <linux/regmap.h>
#include <linux/seq_file.h>
#include <linux/slab.h>
#include <linux/uaccess.h>
#include <asm/sysreg.h>
#include "s10-c2-diag-core.h"

bool s10_c2_diag_enabled;
static bool boot_enabled;
static struct c2_data data;
static DEFINE_MUTEX(diag_lock);

static int __init diag_setup(char *str)
{
	boot_enabled = !strcmp(str, "1");
	return 1;
}
__setup("s10.c2_diag=", diag_setup);

void s10_c2_diag_begin(u32 state)
{
	c2_begin(&data, raw_smp_processor_id(), state);
}
void s10_c2_diag_end(int pm_ret, int ret)
{
	c2_end(&data, raw_smp_processor_id(), pm_ret, ret);
}
noinstr void s10_c2_diag_smc_enter(unsigned long fn, unsigned long state,
				 unsigned long entry)
{
	u64 isr = 0, daif = 0;

	if (data.mode == 2 && !raw_smp_processor_id() && data.active &&
	    fn == 0xc4000001 && state == 0x10000) {
		isr = read_sysreg(isr_el1);
		daif = read_sysreg(daif);
	}
	c2_smc_enter(&data, raw_smp_processor_id(), fn, state, entry,
		    read_sysreg(mpidr_el1), isr, daif);
}
noinstr void s10_c2_diag_smc_return(unsigned long fn, unsigned long state,
				  const struct arm_smccc_res *res)
{
	c2_smc_return(&data, raw_smp_processor_id(), fn, state,
		      res->a0, res->a1, res->a2, res->a3);
}
void s10_c2_diag_pmu(bool restore, struct regmap *pmu, struct regmap *intr)
{
	static const unsigned int offsets[] = { 0x860, 0x1004, 0x1044, 0x108, 0x200, 0x208 };
	struct c2_event *e = c2_active(&data, raw_smp_processor_id());
	unsigned int i;

	if (!e || data.mode != 2)
		return;
	for (i = 0; i < ARRAY_SIZE(offsets); i++) {
		unsigned int value;
		int ret = regmap_read(i < 3 ? pmu : intr, offsets[i], &value);

		if (ret) {
			data.pmu_read_errors++;
			continue;
		}
		e->pmu[restore][i] = value;
		e->pmu_valid[restore] |= BIT(i);
	}
}

/* Called with cpu hotplug and cpuidle registration frozen. */
static bool all_c2_disabled(void)
{
	unsigned int cpu, i;
	bool cpu0_found = false;

	for_each_online_cpu(cpu) {
		struct cpuidle_device *dev = per_cpu(cpuidle_devices, cpu);
		struct cpuidle_driver *drv = cpuidle_get_cpu_driver(dev);

		if (!dev || !drv)
			continue;
		for (i = 1; i < drv->state_count; i++) {
			if (!cpu)
				cpu0_found = true;
			if (!READ_ONCE(dev->states_usage[i].disable) &&
			    !(drv->states[i].flags & CPUIDLE_FLAG_UNUSABLE))
				return false;
		}
	}
	return cpu0_found;
}
static ssize_t control_write(struct file *file, const char __user *buf,
			     size_t size, loff_t *off)
{
	char cmd[32];
	int ret = 0;

	if (!size || size >= sizeof(cmd))
		return -EINVAL;
	if (copy_from_user(cmd, buf, size))
		return -EFAULT;
	cmd[size] = 0;
	strim(cmd);
	if (strcmp(cmd, "reset") && strcmp(cmd, "start basic") &&
	    strcmp(cmd, "start pending") && strcmp(cmd, "stop"))
		return -EINVAL;
	mutex_lock(&diag_lock);
	cpus_read_lock();
	cpuidle_pause_and_lock();
	if (!all_c2_disabled() || data.active) {
		ret = -EBUSY;
		goto out;
	}
	if (!strcmp(cmd, "stop")) {
		WRITE_ONCE(s10_c2_diag_enabled, false);
		data.running = false;
	} else if (data.running || data.active) {
		ret = -EBUSY;
	} else if (!strcmp(cmd, "reset")) {
		memset(&data, 0, sizeof(data));
	} else if (data.attempts) {
		ret = -EBUSY; /* Explicit reset required between independent runs. */
	} else {
		data.mode = !strcmp(cmd, "start basic") ? 1 : 2;
		data.running = true;
		WRITE_ONCE(s10_c2_diag_enabled, true);
	}
 out:
	cpuidle_resume_and_unlock();
	cpus_read_unlock();
	mutex_unlock(&diag_lock);
	if (ret)
		return ret;
	return size;
}
static const struct file_operations control_fops = {
	.open = nonseekable_open, .write = control_write,
};
static int summary_show(struct seq_file *s, void *unused)
{
	struct c2_data *d = s->private;
	unsigned int i;

	seq_printf(s, "schema=2 cpu=0 mode=%u attempts=%llu smc_issued=%llu immediate=%llu resumed=%llu pm_veto=%llu pre_smc_abort=%llu inconsistent=%llu active=%u nested=%llu hist_overflow=%llu pmu_read_errors=%llu\n",
		   d->mode, d->attempts, d->issued, d->immediate, d->resumed,
		   d->veto, d->pre_abort, d->inconsistent, d->active, d->nested,
		   d->hist_overflow, d->pmu_read_errors);
	seq_puts(s, "raw_x0 count\n");
	for (i = 0; i < d->hist_used; i++)
		seq_printf(s, "0x%016llx %llu\n", d->hist[i].raw, d->hist[i].count);
	return 0;
}
static int events_show(struct seq_file *s, void *unused)
{
	struct c2_data *d = s->private;
	u64 first = d->attempts > C2_RING ? d->attempts - C2_RING : 0, n;

	seq_puts(s, "schema=2 outcomes=0:OPEN,1:PM_VETO,2:PRE_SMC_ABORT,3:IMMEDIATE_RETURN,4:RESUMED,5:INCONSISTENT\n");
	seq_puts(s, "id outcome pm_ret ret fn state entry mpidr issued raw_valid x0 x1 x2 x3 pending_valid isr daif prep_valid restore_valid prep_inform prep_status prep_int_en prep_grp1 prep_grp2_en prep_grp2 restore_inform restore_status restore_int_en restore_grp1 restore_grp2_en restore_grp2\n");
	for (n = first; n < d->attempts; n++) {
		struct c2_event *e = &d->events[n % C2_RING];
		unsigned int stage, i;

		seq_printf(s, "%llu %u %d %d %#x %#x %#llx %#llx %u %u %#llx %#llx %#llx %#llx %u %#llx %#llx %#x %#x",
			e->id, e->outcome, e->pm_ret, e->ret, e->fn, e->state,
			e->entry, e->mpidr, e->issued, e->raw_valid,
			e->raw[0], e->raw[1], e->raw[2], e->raw[3],
			e->pending_valid, e->isr, e->daif,
			e->pmu_valid[0], e->pmu_valid[1]);
		for (stage = 0; stage < 2; stage++)
			for (i = 0; i < 6; i++)
				seq_printf(s, " %#x", e->pmu[stage][i]);
		seq_putc(s, '\n');
	}
	return 0;
}
/* Each open owns an immutable snapshot, including across partial seq_reads. */
static int diag_open(struct inode *inode, struct file *file)
{
	struct c2_data *copy = kvzalloc(sizeof(*copy), GFP_KERNEL);
	int ret;

	if (!copy)
		return -ENOMEM;
	mutex_lock(&diag_lock);
	if (data.running || data.active) {
		ret = -EBUSY;
	} else {
		memcpy(copy, &data, sizeof(*copy));
		ret = single_open(file, inode->i_private ? events_show : summary_show, copy);
	}
	mutex_unlock(&diag_lock);
	if (ret)
		kvfree(copy);
	return ret;
}
static int diag_release(struct inode *inode, struct file *file)
{
	struct seq_file *s = file->private_data;

	kvfree(s->private);
	return single_release(inode, file);
}
static const struct file_operations diag_fops = {
	.open = diag_open, .read = seq_read, .llseek = seq_lseek, .release = diag_release,
};
static int __init diag_init(void)
{
	struct dentry *dir;

	if (!boot_enabled || !of_machine_is_compatible("samsung,exynos9820"))
		return 0;
	dir = debugfs_create_dir("s10_c2_diag", NULL);
	if (IS_ERR(dir))
		return PTR_ERR(dir);
	debugfs_create_file("control", 0200, dir, NULL, &control_fops);
	debugfs_create_file("summary", 0400, dir, NULL, &diag_fops);
	debugfs_create_file("events", 0400, dir, (void *)1, &diag_fops);
	return 0;
}
late_initcall(diag_init);
#endif
