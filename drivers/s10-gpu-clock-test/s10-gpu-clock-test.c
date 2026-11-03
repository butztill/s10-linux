// SPDX-License-Identifier: GPL-2.0-only
/* Bounded stock-frequency test through the existing ACPM clock provider. */
#include <linux/clk.h>
#include <linux/module.h>
#include <linux/of.h>
#include <linux/of_platform.h>
#include <linux/platform_device.h>
#include <linux/pm_runtime.h>
#include <linux/workqueue.h>
static struct platform_device *gpu;
static struct clk *clock;
static unsigned long original;
static void restore(struct work_struct *work);
static DECLARE_DELAYED_WORK(reset_work, restore);
static void restore(struct work_struct *work)
{
 int ret = clk_set_rate(clock, original);
 pr_info("s10-gpu-clock-test: restore %lu Hz: %d\n", original, ret);
}
static int __init start(void)
{
 struct device_node *np;
 int ret;
 if (!of_machine_is_compatible("samsung,beyond1lte")) return -ENODEV;
 np = of_find_node_by_path("/soc@0/gpu@18500000");
 if (!np) return -ENODEV;
 gpu = of_find_device_by_node(np); of_node_put(np);
 if (!gpu) return -ENODEV;
 ret = pm_runtime_get_if_active(&gpu->dev);
 if (ret <= 0) { ret = -EBUSY; goto put; }
 clock = clk_get(&gpu->dev, "gpu");
 if (IS_ERR(clock)) { ret = PTR_ERR(clock); goto pm; }
 original = clk_get_rate(clock);
 if (original != 156000000) { ret = -EINVAL; goto clk; }
 ret = clk_set_rate(clock, 260000000);
 if (ret) { clk_set_rate(clock, original); goto clk; }
 pr_info("s10-gpu-clock-test: firmware requested 260 MHz, automatic restore in 60 seconds\n");
 schedule_delayed_work(&reset_work, 60 * HZ);
 return 0;
clk: clk_put(clock);
pm: pm_runtime_put(&gpu->dev);
put: put_device(&gpu->dev);
 return ret;
}
static void __exit stop(void)
{
 cancel_delayed_work_sync(&reset_work);
 restore(NULL); clk_put(clock);
 pm_runtime_put(&gpu->dev); put_device(&gpu->dev);
}
module_init(start);
module_exit(stop);
MODULE_LICENSE("GPL");
MODULE_DESCRIPTION("S10 bounded stock GPU frequency comparison with automatic restore");
