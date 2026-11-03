// SPDX-License-Identifier: GPL-2.0-only
/* Read the CSIS1 identification/control registers with runtime PM and CCF.
 * This does not start CSI, configure the PHY, or write DMA registers.
 */
#include <linux/clk.h>
#include <linux/delay.h>
#include <linux/io.h>
#include <linux/module.h>
#include <linux/of.h>
#include <linux/platform_device.h>
#include <linux/pm_runtime.h>

#define S10_CSIS_NUM_CLOCKS 1
struct s10_csis_id {
	u32 version, control, clock_control;
	/* devm_clk_bulk_get retains this array until device detach. */
	struct clk_bulk_data clocks[S10_CSIS_NUM_CLOCKS];
};
static unsigned int stage;
module_param(stage, uint, 0400);
MODULE_PARM_DESC(stage, "0=dependency check, 1=request CSIS Q-channel and read ID");

static ssize_t identification_show(struct device *dev,
				   struct device_attribute *attr, char *buf)
{
	struct s10_csis_id *id = dev_get_drvdata(dev);

	return sysfs_emit(buf, "stage=%u version=%#010x control=%#010x clock=%#010x; no receiver or DMA started\n",
			 stage, id->version, id->control, id->clock_control);
}
static DEVICE_ATTR_RO(identification);
static struct attribute *s10_csis_attrs[] = { &dev_attr_identification.attr, NULL };
ATTRIBUTE_GROUPS(s10_csis);

static int s10_csis_probe(struct platform_device *pdev)
{
	struct device *dev = &pdev->dev;
	static const char * const names[] = {
		"csis",
	};
	struct clk_bulk_data *clocks;
	struct resource *res;
	struct s10_csis_id *id;
	void __iomem *regs;
	int ret, enabled = 0, i;

	res = platform_get_resource(pdev, IORESOURCE_MEM, 0);
	if (!res || res->start != 0x17040000 || resource_size(res) != 0x1000 ||
	    !of_machine_is_compatible("samsung,beyond1lte"))
		return -ENODEV;
	if (stage > S10_CSIS_NUM_CLOCKS)
		return -EINVAL;
	dev_info(dev, "CSIS diagnostic probe stage %u: mapping resource\n", stage);
	id = devm_kzalloc(dev, sizeof(*id), GFP_KERNEL);
	if (!id)
		return -ENOMEM;
	clocks = id->clocks;
	for (i = 0; i < S10_CSIS_NUM_CLOCKS; i++)
		clocks[i].id = names[i];
	regs = devm_platform_ioremap_resource(pdev, 0);
	if (IS_ERR(regs))
		return PTR_ERR(regs);
	ret = devm_clk_bulk_get(dev, S10_CSIS_NUM_CLOCKS, clocks);
	if (ret)
		return dev_err_probe(dev, ret, "CSIS clocks unavailable\n");
	dev_info(dev, "CSIS clock references acquired; resuming power domain\n");
	pm_runtime_enable(dev);
	ret = pm_runtime_resume_and_get(dev);
	if (ret < 0)
		goto disable_pm;
	dev_info(dev, "CSIS power domain resumed\n");
	for (i = 0; i < stage; i++) {
		dev_info(dev, "enabling clock %d: %s\n", i, clocks[i].id);
		msleep(100);
		ret = clk_prepare_enable(clocks[i].clk);
		if (ret)
			goto disable_clocks;
		enabled++;
		dev_info(dev, "enabled clock %d: %s\n", i, clocks[i].id);
		msleep(100);
	}
	if (stage < S10_CSIS_NUM_CLOCKS) {
		ret = 0;
		goto disable_clocks;
	}
	dev_info(dev, "CSIS identification preflight: clock=%lu Hz; reading version\n",
		 clk_get_rate(clocks[0].clk));
	id->version = readl(regs);
	id->control = readl(regs + 4);
	id->clock_control = readl(regs + 8);
	if (!id->version || id->version == ~0U)
		ret = -ENODEV;
	dev_info(dev, "CSIS version=%#x control=%#x clock=%#x; no stream/DMA writes\n",
		 id->version, id->control, id->clock_control);
disable_clocks:
	while (enabled) {
		i = --enabled;
		dev_info(dev, "disabling clock %d: %s\n", i, clocks[i].id);
		clk_disable_unprepare(clocks[i].clk);
	}
	pm_runtime_put_sync_suspend(dev);
disable_pm:
	pm_runtime_disable(dev);
	if (!ret)
		platform_set_drvdata(pdev, id);
	return ret;
}
static const struct of_device_id s10_csis_match[] = {
	{ .compatible = "samsung,exynos9820-csis1-id-test" }, { }
};
MODULE_DEVICE_TABLE(of, s10_csis_match);
static struct platform_driver s10_csis_driver = {
	.probe = s10_csis_probe,
	.driver = {
		.name = "s10-csis-identify", .of_match_table = s10_csis_match,
		.dev_groups = s10_csis_groups,
	},
};
module_platform_driver(s10_csis_driver);
MODULE_LICENSE("GPL");
MODULE_DESCRIPTION("S10 CSIS1 read-only identification test");
