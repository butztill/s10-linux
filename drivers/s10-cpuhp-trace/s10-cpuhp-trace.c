// SPDX-License-Identifier: GPL-2.0
/* Bounded, in-memory observation of CPU7 hotplug and its INFORM writes. */
#include <linux/debugfs.h>
#include <linux/err.h>
#include <linux/kprobes.h>
#include <linux/ktime.h>
#include <linux/module.h>
#include <linux/of.h>
#include <linux/seq_file.h>
#include <linux/spinlock.h>

#define TRACE_CPU 7
#define INFORM_REG 0x87c
#define MAX_EVENTS 64

enum event_type { DOWN, UP, INFORM_WRITE, OFFLINE_RET, ONLINE_RET };
struct event_record {
	u64 ns;
	u32 event;
	u32 cpu;
	s64 value;
};

static DEFINE_RAW_SPINLOCK(event_lock);
static struct event_record records[MAX_EVENTS];
static unsigned int num_records, dropped;
static struct dentry *trace_dir;

static void record_event(enum event_type event, s64 value)
{
	unsigned long flags;
	unsigned int cpu = raw_smp_processor_id();

	if (cpu != TRACE_CPU)
		return;
	raw_spin_lock_irqsave(&event_lock, flags);
	if (num_records < MAX_EVENTS) {
		struct event_record *r = &records[num_records++];

		r->ns = ktime_get_mono_fast_ns();
		r->event = event;
		r->cpu = cpu;
		r->value = value;
	} else {
		dropped++;
	}
	raw_spin_unlock_irqrestore(&event_lock, flags);
}

static int down_pre(struct kprobe *p, struct pt_regs *regs)
{
	record_event(DOWN, regs->regs[0]);
	return 0;
}

static int up_pre(struct kprobe *p, struct pt_regs *regs)
{
	record_event(UP, regs->regs[0]);
	return 0;
}

static int write_pre(struct kprobe *p, struct pt_regs *regs)
{
	if ((u32)regs->regs[1] == INFORM_REG)
		record_event(INFORM_WRITE, (u32)regs->regs[2]);
	return 0;
}

static int offline_ret(struct kretprobe_instance *ri, struct pt_regs *regs)
{
	record_event(OFFLINE_RET, (int)regs_return_value(regs));
	return 0;
}

static int online_ret(struct kretprobe_instance *ri, struct pt_regs *regs)
{
	record_event(ONLINE_RET, (int)regs_return_value(regs));
	return 0;
}

static struct kprobe probes[] = {
	{ .symbol_name = "exynos_cpuhp_pmu_power_down", .pre_handler = down_pre },
	{ .symbol_name = "exynos_cpuhp_pmu_power_up", .pre_handler = up_pre },
	{ .symbol_name = "regmap_write", .pre_handler = write_pre },
};
static struct kretprobe returns[] = {
	{ .kp.symbol_name = "exynos9820_cpu_pmu_offline", .handler = offline_ret, .maxactive = 16 },
	{ .kp.symbol_name = "exynos9820_cpu_pmu_online", .handler = online_ret, .maxactive = 16 },
};

static int events_show(struct seq_file *m, void *v)
{
	static const char * const names[] = { "down", "up", "inform_write", "offline_ret", "online_ret" };
	struct event_record snapshot[MAX_EVENTS];
	unsigned int count, lost, i;
	unsigned long flags;

	raw_spin_lock_irqsave(&event_lock, flags);
	count = num_records;
	lost = dropped;
	memcpy(snapshot, records, count * sizeof(*snapshot));
	raw_spin_unlock_irqrestore(&event_lock, flags);
	seq_printf(m, "count=%u dropped=%u offline_missed=%d online_missed=%d\n",
		   count, lost, returns[0].nmissed, returns[1].nmissed);
	for (i = 0; i < count; i++)
		seq_printf(m, "ns=%llu cpu=%u event=%s value=%lld\n",
			   snapshot[i].ns, snapshot[i].cpu,
			   names[snapshot[i].event], snapshot[i].value);
	return 0;
}
DEFINE_SHOW_ATTRIBUTE(events);

static int __init s10_trace_init(void)
{
	struct dentry *events;
	int i, ret;

	if (!of_machine_is_compatible("samsung,exynos9820"))
		return -ENODEV;
	for (i = 0; i < ARRAY_SIZE(probes); i++) {
		ret = register_kprobe(&probes[i]);
		if (ret)
			goto unregister_probes;
	}
	for (i = 0; i < ARRAY_SIZE(returns); i++) {
		ret = register_kretprobe(&returns[i]);
		if (ret)
			goto unregister_returns;
	}
	trace_dir = debugfs_create_dir("s10-cpuhp-trace", NULL);
	if (IS_ERR_OR_NULL(trace_dir)) {
		ret = trace_dir ? PTR_ERR(trace_dir) : -ENODEV;
		goto unregister_returns;
	}
	events = debugfs_create_file("events", 0400, trace_dir, NULL, &events_fops);
	if (IS_ERR_OR_NULL(events)) {
		ret = events ? PTR_ERR(events) : -ENODEV;
		debugfs_remove_recursive(trace_dir);
		goto unregister_returns;
	}
	return 0;

unregister_returns:
	while (--i >= 0)
		unregister_kretprobe(&returns[i]);
	i = ARRAY_SIZE(probes);
unregister_probes:
	while (--i >= 0)
		unregister_kprobe(&probes[i]);
	return ret;
}

static void __exit s10_trace_exit(void)
{
	int i;

	debugfs_remove_recursive(trace_dir);
	for (i = 0; i < ARRAY_SIZE(returns); i++)
		unregister_kretprobe(&returns[i]);
	for (i = 0; i < ARRAY_SIZE(probes); i++)
		unregister_kprobe(&probes[i]);
}
module_init(s10_trace_init);
module_exit(s10_trace_exit);
MODULE_LICENSE("GPL");
MODULE_DESCRIPTION("Bounded in-memory CPU7 hotplug trace without PMU reads");
