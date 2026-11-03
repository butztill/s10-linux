/* SPDX-License-Identifier: GPL-2.0-only */
/* Pure, fixed-storage state machine, also compiled by the host tests. */
#ifndef S10_C2_DIAG_CORE_H
#define S10_C2_DIAG_CORE_H
#define C2_RING 256
#define C2_HIST 64
enum c2_outcome { C2_OPEN, C2_PM_VETO, C2_PRE_SMC_ABORT,
		  C2_IMMEDIATE_RETURN, C2_RESUMED, C2_INCONSISTENT };
struct c2_event {
	u64 id, mpidr, entry, raw[4], isr, daif;
	u32 fn, state, pmu[2][6], pmu_valid[2];
	int pm_ret, ret;
	unsigned int outcome;
	bool issued, raw_valid, pending_valid;
};
struct c2_hist { u64 raw, count; };
struct c2_data {
	u64 attempts, issued, immediate, resumed, veto, pre_abort, inconsistent;
	u64 hist_overflow, nested, pmu_read_errors;
	unsigned int mode, hist_used;
	bool running, active;
	struct c2_hist hist[C2_HIST];
	struct c2_event events[C2_RING];
};
static __always_inline struct c2_event *c2_active(struct c2_data *d,
						unsigned int cpu)
{
	if (!d->running || !d->active || cpu)
		return NULL;
	return &d->events[(d->attempts - 1) % C2_RING];
}
static __always_inline void c2_begin(struct c2_data *d, unsigned int cpu, u32 state)
{
	struct c2_event *e;

	if (!d->running || cpu || state != 0x10000)
		return;
	if (d->active) {
		d->nested++;
		return;
	}
	e = &d->events[d->attempts++ % C2_RING];
	memset(e, 0, sizeof(*e));
	e->id = d->attempts;
	e->state = state;
	d->active = true;
}
static __always_inline void c2_smc_enter(struct c2_data *d, unsigned int cpu,
				       u64 fn, u64 state, u64 entry, u64 mpidr,
				       u64 isr, u64 daif)
{
	struct c2_event *e = c2_active(d, cpu);

	if (!e || fn != 0xc4000001 || state != e->state)
		return;
	if (e->issued) {
		d->nested++;
		return;
	}
	e->issued = true;
	e->fn = fn;
	e->entry = entry;
	e->mpidr = mpidr;
	if (d->mode == 2) {
		e->pending_valid = true;
		e->isr = isr;
		e->daif = daif;
	}
	d->issued++;
}
static __always_inline void c2_smc_return(struct c2_data *d, unsigned int cpu,
					u64 fn, u64 state, u64 a0, u64 a1,
					u64 a2, u64 a3)
{
	struct c2_event *e = c2_active(d, cpu);
	unsigned int i;

	if (!e || !e->issued || e->raw_valid || fn != e->fn || state != e->state)
		return;
	e->raw[0] = a0;
	e->raw[1] = a1;
	e->raw[2] = a2;
	e->raw[3] = a3;
	e->raw_valid = true;
	d->immediate++;
	for (i = 0; i < d->hist_used; i++) {
		if (d->hist[i].raw == a0) {
			d->hist[i].count++;
			return;
		}
	}
	if (d->hist_used == C2_HIST) {
		d->hist_overflow++;
		return;
	}
	d->hist[d->hist_used].raw = a0;
	d->hist[d->hist_used++].count = 1;
}
static __always_inline void c2_end(struct c2_data *d, unsigned int cpu,
				 int pm_ret, int ret)
{
	struct c2_event *e = c2_active(d, cpu);

	if (!e)
		return;
	e->pm_ret = pm_ret;
	e->ret = ret;
	if (pm_ret && !e->issued) {
		e->outcome = C2_PM_VETO;
		d->veto++;
	} else if (!e->issued) {
		if (ret) {
			e->outcome = C2_PRE_SMC_ABORT;
			d->pre_abort++;
		} else {
			e->outcome = C2_INCONSISTENT;
			d->inconsistent++;
		}
	} else if (!pm_ret && e->raw_valid) {
		e->outcome = C2_IMMEDIATE_RETURN;
	} else if (!pm_ret && !ret) {
		e->outcome = C2_RESUMED;
		d->resumed++;
	} else {
		e->outcome = C2_INCONSISTENT;
		d->inconsistent++;
	}
	d->active = false;
}
#endif
