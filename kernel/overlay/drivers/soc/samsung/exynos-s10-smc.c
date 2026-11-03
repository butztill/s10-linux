// SPDX-License-Identifier: GPL-2.0-or-later
/* Copyright (C) 2014 Samsung Electronics Co., Ltd. */

#include <linux/arm-smccc.h>
#include <linux/module.h>
#include <linux/soc/samsung/exynos-smc.h>

#include <asm/barrier.h>

int exynos_smc(unsigned long command, unsigned long arg1,
	       unsigned long arg2, unsigned long arg3)
{
	struct arm_smccc_res result;

	/* Preserve the ordering used by Samsung's original SMC entry point. */
	dsb(sy);
	arm_smccc_smc(command, arg1, arg2, arg3, 0, 0, 0, 0, &result);

	return (int)result.a0;
}
EXPORT_SYMBOL_GPL(exynos_smc);

MODULE_DESCRIPTION("Samsung Exynos ARM64 Secure Monitor Call wrapper");
MODULE_AUTHOR("Samsung Electronics; ported for the Exynos 9820 S10");
MODULE_LICENSE("GPL");
