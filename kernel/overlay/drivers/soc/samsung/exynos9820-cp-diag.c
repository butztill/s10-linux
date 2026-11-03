// SPDX-License-Identifier: GPL-2.0-or-later
/*
 * Read-only Exynos 9820 cellular processor diagnostics.
 *
 * The register access protocol and bit definitions are taken from Samsung's
 * GPL kernel sources for the Galaxy S10 (Exynos 9820). This module deliberately
 * does not implement CP power-on, reset, firmware loading, or mailbox setup.
 */

#include <linux/kernel.h>
#include <linux/kobject.h>
#include <linux/module.h>
#include <linux/soc/samsung/exynos-smc.h>
#include <linux/sysfs.h>

#define EXYNOS9820_CP_SMC_ID		0x82000700
#define EXYNOS9820_CP_SMC_READ_CTRL	0x3

#define EXYNOS9820_CP_PWRON		BIT(1)
#define EXYNOS9820_CP_RESET_SET		BIT(2)
#define EXYNOS9820_CP_START		BIT(3)

enum exynos9820_cp_control {
	EXYNOS9820_CP_CTRL_S,
	EXYNOS9820_CP_CTRL_NS,
};

static struct kobject *exynos9820_cp_kobj;

/*
 * Samsung's READ_CTRL service returns a status in a0[15:0] and one 16-bit
 * register half in a0[31:16]. The two calls are required for one 32-bit word.
 */
static int exynos9820_cp_read_ctrl(enum exynos9820_cp_control reg, u32 *value)
{
	u32 low_result, high_result;
	int ret;

	ret = exynos_smc(EXYNOS9820_CP_SMC_ID, EXYNOS9820_CP_SMC_READ_CTRL,
			 0, reg);
	low_result = (u32)ret;
	if (low_result & 0xffff)
		return -EREMOTEIO;

	ret = exynos_smc(EXYNOS9820_CP_SMC_ID, EXYNOS9820_CP_SMC_READ_CTRL,
			 1, reg);
	high_result = (u32)ret;
	if (high_result & 0xffff)
		return -EREMOTEIO;

	*value = ((high_result >> 16) << 16) | (low_result >> 16);
	return 0;
}

static ssize_t cp_ctrl_ns_show(struct kobject *kobj,
			       struct kobj_attribute *attr, char *buf)
{
	u32 value;
	int ret;

	ret = exynos9820_cp_read_ctrl(EXYNOS9820_CP_CTRL_NS, &value);
	if (ret)
		return ret;

	return sysfs_emit(buf, "0x%08x\n", value);
}

static struct kobj_attribute cp_ctrl_ns_attr = __ATTR_RO(cp_ctrl_ns);

static ssize_t cp_ctrl_s_show(struct kobject *kobj,
			      struct kobj_attribute *attr, char *buf)
{
	u32 value;
	int ret;

	ret = exynos9820_cp_read_ctrl(EXYNOS9820_CP_CTRL_S, &value);
	if (ret)
		return ret;

	return sysfs_emit(buf, "0x%08x\n", value);
}

static struct kobj_attribute cp_ctrl_s_attr = __ATTR_RO(cp_ctrl_s);

static ssize_t status_show(struct kobject *kobj,
			   struct kobj_attribute *attr, char *buf)
{
	u32 cp_ctrl_ns, cp_ctrl_s;
	int ret;

	ret = exynos9820_cp_read_ctrl(EXYNOS9820_CP_CTRL_NS, &cp_ctrl_ns);
	if (ret)
		return ret;

	ret = exynos9820_cp_read_ctrl(EXYNOS9820_CP_CTRL_S, &cp_ctrl_s);
	if (ret)
		return ret;

	return sysfs_emit(buf,
			  "CP_CTRL_NS=0x%08x CP_CTRL_S=0x%08x\n"
			  "power_on=%u reset_asserted=%u start_requested=%u\n",
			  cp_ctrl_ns, cp_ctrl_s,
			  !!(cp_ctrl_ns & EXYNOS9820_CP_PWRON),
			  !!(cp_ctrl_ns & EXYNOS9820_CP_RESET_SET),
			  !!(cp_ctrl_s & EXYNOS9820_CP_START));
}

static struct kobj_attribute status_attr = __ATTR_RO(status);

static struct attribute *exynos9820_cp_attrs[] = {
	&cp_ctrl_ns_attr.attr,
	&cp_ctrl_s_attr.attr,
	&status_attr.attr,
	NULL,
};

static const struct attribute_group exynos9820_cp_attr_group = {
	.attrs = exynos9820_cp_attrs,
};

static int __init exynos9820_cp_diag_init(void)
{
	int ret;

	exynos9820_cp_kobj = kobject_create_and_add("exynos9820-cp", kernel_kobj);
	if (!exynos9820_cp_kobj)
		return -ENOMEM;

	ret = sysfs_create_group(exynos9820_cp_kobj, &exynos9820_cp_attr_group);
	if (ret) {
		kobject_put(exynos9820_cp_kobj);
		exynos9820_cp_kobj = NULL;
		return ret;
	}

	pr_info("Exynos 9820 CP read-only diagnostics ready; no SMC issued\n");
	return 0;
}

static void __exit exynos9820_cp_diag_exit(void)
{
	sysfs_remove_group(exynos9820_cp_kobj, &exynos9820_cp_attr_group);
	kobject_put(exynos9820_cp_kobj);
}

module_init(exynos9820_cp_diag_init);
module_exit(exynos9820_cp_diag_exit);

MODULE_DESCRIPTION("Read-only Exynos 9820 cellular processor diagnostics");
MODULE_AUTHOR("Codex; based on Samsung Exynos 9820 GPL sources");
MODULE_LICENSE("GPL");
