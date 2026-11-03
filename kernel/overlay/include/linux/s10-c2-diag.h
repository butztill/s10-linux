/* SPDX-License-Identifier: GPL-2.0-only */
#ifndef _LINUX_S10_C2_DIAG_H
#define _LINUX_S10_C2_DIAG_H
#include <linux/types.h>
#include <linux/compiler.h>
struct regmap;
struct arm_smccc_res;
#if defined(CONFIG_ARM64) && defined(CONFIG_ARM_PSCI_CPUIDLE) && defined(CONFIG_DEBUG_FS)
extern bool s10_c2_diag_enabled;
static inline bool s10_c2_diag_active(void)
{ return unlikely(READ_ONCE(s10_c2_diag_enabled)); }
void s10_c2_diag_begin(u32 state);
void s10_c2_diag_end(int pm_ret, int ret);
void s10_c2_diag_smc_enter(unsigned long fn, unsigned long state, unsigned long entry);
void s10_c2_diag_smc_return(unsigned long fn, unsigned long state,
			   const struct arm_smccc_res *res);
void s10_c2_diag_pmu(bool restore, struct regmap *pmu, struct regmap *intr);
#define s10_c2_diag_call(fn, ...) do { if (unlikely(READ_ONCE(s10_c2_diag_enabled))) \
	fn(__VA_ARGS__); } while (0)
#else
static inline bool s10_c2_diag_active(void) { return false; }
#define s10_c2_diag_call(fn, ...) do { } while (0)
#endif
#endif
