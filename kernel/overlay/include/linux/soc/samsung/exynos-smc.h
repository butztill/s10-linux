/* SPDX-License-Identifier: GPL-2.0-or-later */
/* Copyright (C) 2014 Samsung Electronics Co., Ltd. */
#ifndef _LINUX_SOC_SAMSUNG_EXYNOS_SMC_H
#define _LINUX_SOC_SAMSUNG_EXYNOS_SMC_H

int exynos_smc(unsigned long command, unsigned long arg1,
	       unsigned long arg2, unsigned long arg3);

#endif /* _LINUX_SOC_SAMSUNG_EXYNOS_SMC_H */
