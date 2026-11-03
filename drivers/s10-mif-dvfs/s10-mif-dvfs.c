// SPDX-License-Identifier: GPL-2.0-only
/*
 * S10 test knob for the ACPM DVFS domains. The kernel sets MIF/INT to the boot
 * frequency via assigned-clock-rates and nothing lowers them again (the stock
 * kernel idles MIF at 421 MHz). Writes to /sys/module/s10_mif_dvfs/parameters/
 * {mif_hz,int_hz} go through clk_set_rate() on the exynos9820 ACPM provider
 * (index 0 = mif, 1 = int). On unload both are restored to the load-time rate.
 */
#include <linux/clk.h>
#include <linux/clk-provider.h>
#include <linux/module.h>
#include <linux/of.h>

static struct clk *mif, *intc;
static unsigned long mif_orig, int_orig;
static unsigned long mif_hz, int_hz;

static int set_hz(struct clk *c, unsigned long hz)
{
	int ret;

	if (!c)
		return -ENODEV;
	ret = clk_set_rate(c, hz);
	pr_info("s10-mif-dvfs: set %lu Hz -> %d (now %lu)\n", hz, ret, clk_get_rate(c));
	return ret;
}

static int mif_set(const char *val, const struct kernel_param *kp)
{
	unsigned long v;
	int ret = kstrtoul(val, 0, &v);

	if (ret)
		return ret;
	ret = set_hz(mif, v);
	if (!ret)
		mif_hz = v;
	return ret;
}

static int int_set(const char *val, const struct kernel_param *kp)
{
	unsigned long v;
	int ret = kstrtoul(val, 0, &v);

	if (ret)
		return ret;
	ret = set_hz(intc, v);
	if (!ret)
		int_hz = v;
	return ret;
}

static const struct kernel_param_ops mif_ops = { .set = mif_set, .get = param_get_ulong };
static const struct kernel_param_ops int_ops = { .set = int_set, .get = param_get_ulong };
module_param_cb(mif_hz, &mif_ops, &mif_hz, 0644);
module_param_cb(int_hz, &int_ops, &int_hz, 0644);

static struct clk *get_idx(struct device_node *np, u32 idx)
{
	struct of_phandle_args a = { .np = np, .args_count = 1, .args = { idx } };

	return of_clk_get_from_provider(&a);
}

static int __init s10_mif_init(void)
{
	struct device_node *np = of_find_compatible_node(NULL, NULL, "samsung,exynos9820-acpm-ipc");

	if (!np)
		return -ENODEV;
	mif = get_idx(np, 0);
	intc = get_idx(np, 1);
	of_node_put(np);
	if (IS_ERR(mif) || IS_ERR(intc))
		return -EPROBE_DEFER;
	mif_orig = mif_hz = clk_get_rate(mif);
	int_orig = int_hz = clk_get_rate(intc);
	pr_info("s10-mif-dvfs: mif %lu Hz, int %lu Hz\n", mif_hz, int_hz);
	return 0;
}

static void __exit s10_mif_exit(void)
{
	set_hz(mif, mif_orig);
	set_hz(intc, int_orig);
	clk_put(mif);
	clk_put(intc);
}

module_init(s10_mif_init);
module_exit(s10_mif_exit);
MODULE_DESCRIPTION("S10 MIF/INT ACPM DVFS test knob");
MODULE_LICENSE("GPL");
