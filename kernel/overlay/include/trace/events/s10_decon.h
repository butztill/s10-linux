/* SPDX-License-Identifier: GPL-2.0-only */
#undef TRACE_SYSTEM
#define TRACE_SYSTEM s10_decon

#if !defined(_TRACE_S10_DECON_H) || defined(TRACE_HEADER_MULTI_READ)
#define _TRACE_S10_DECON_H

#include <linux/tracepoint.h>

TRACE_EVENT(s10_decon_frame,
	TP_PROTO(u32 phase, u64 commit, u32 fb, u32 pending, u32 shadow, u64 age_ns),
	TP_ARGS(phase, commit, fb, pending, shadow, age_ns),
	TP_STRUCT__entry(
		__field(u32, phase)
		__field(u64, commit)
		__field(u32, fb)
		__field(u32, pending)
		__field(u32, shadow)
		__field(u64, age_ns)
	),
	TP_fast_assign(
		__entry->phase = phase;
		__entry->commit = commit;
		__entry->fb = fb;
		__entry->pending = pending;
		__entry->shadow = shadow;
		__entry->age_ns = age_ns;
	),
	TP_printk("phase=%s commit=%llu fb=%u pending=0x%x shadow=0x%x age_ns=%llu",
		__print_symbolic(__entry->phase, {0, "update"}, {1, "start"},
				 {2, "done"}, {3, "flip"}),
		__entry->commit, __entry->fb, __entry->pending,
		__entry->shadow, __entry->age_ns)
);
#endif
#include <trace/define_trace.h>
