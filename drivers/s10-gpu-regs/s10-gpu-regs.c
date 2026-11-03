// SPDX-License-Identifier: GPL-2.0-only
/* Read-only dump of Mali-G76 configuration registers on the S10. Only runs when
 * the GPU is runtime-active (Panfrost loaded with power/control = on).
 */
#include <linux/io.h>
#include <linux/module.h>
#include <linux/of.h>
#include <linux/of_platform.h>
#include <linux/platform_device.h>
#include <linux/pm_runtime.h>

static const struct { u16 off; const char *name; } regs[] = {
	{ 0x000, "GPU_ID" }, { 0x004, "L2_FEATURES" }, { 0x008, "CORE_FEATURES" },
	{ 0x00c, "TILER_FEATURES" }, { 0x010, "MEM_FEATURES" }, { 0x014, "MMU_FEATURES" },
	{ 0x034, "GPU_STATUS" }, { 0x300, "COHERENCY_FEATURES" }, { 0x304, "COHERENCY_ENABLE" },
	{ 0x140, "SHADER_READY_LO" }, { 0x150, "TILER_READY_LO" }, { 0x160, "L2_READY_LO" },
	{ 0xf00, "JM_CONFIG" }, { 0xf04, "SHADER_CONFIG" }, { 0xf08, "TILER_CONFIG" },
	{ 0xf0c, "L2_MMU_CONFIG" },
};

static int __init s10_gpu_regs_init(void)
{
	struct device_node *np = of_find_node_by_path("/soc@0/gpu@18500000");
	struct platform_device *pdev;
	void __iomem *b;
	int i;

	if (!np)
		return -ENODEV;
	if (!of_machine_is_compatible("samsung,beyond1lte")) {
		of_node_put(np);
		return -ENODEV;
	}
	pdev = of_find_device_by_node(np);
	of_node_put(np);
	if (!pdev)
		return -ENODEV;
	if (pm_runtime_get_if_active(&pdev->dev) <= 0) {
		put_device(&pdev->dev);
		return -EBUSY;
	}
	b = ioremap(0x18500000, 0x3000);
	if (!b) {
		pm_runtime_put(&pdev->dev);
		put_device(&pdev->dev);
		return -ENOMEM;
	}
	for (i = 0; i < ARRAY_SIZE(regs); i++)
		pr_info("s10-gpu-regs: %#05x %-20s = %#010x\n", regs[i].off, regs[i].name,
			readl(b + regs[i].off));
	for (i = 0; i < 8; i++)
		pr_info("s10-gpu-regs: AS%d TRANSCFG=%#010x:%#010x TTBR=%#010x:%#010x\n",
			i, readl(b + 0x2434 + 64 * i), readl(b + 0x2430 + 64 * i),
			readl(b + 0x2404 + 64 * i), readl(b + 0x2400 + 64 * i));
	iounmap(b);
	pm_runtime_put(&pdev->dev);
	put_device(&pdev->dev);
	return -EAGAIN;	/* dump only, do not stay loaded */
}
module_init(s10_gpu_regs_init);
MODULE_LICENSE("GPL");

MODULE_DESCRIPTION("Read-only runtime-active S10 GPU register diagnostic");
