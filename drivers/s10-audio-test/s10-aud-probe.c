// SPDX-License-Identifier: GPL-2.0-only
/* S10 audio block diagnostic: power the AUD domain through genpd, enable the
 * CMU_TOP parent and dump CMU_AUD. Read-only: no CMU or ABOX register writes.
 * stage=0: CMU_AUD only. stage=1: also ABOX_IP_INDEX/ABOX_VERSION, but only if
 * the dump shows the ABOX bus clock is running (see s10_aud_aclk_running()).
 * Register offsets: Samsung Exynos9820 cmucal-sfr.c / abox_soc.h (LineageOS 4bcfb02).
 */
#include <linux/clk.h>
#include <linux/io.h>
#include <linux/module.h>
#include <linux/of.h>
#include <linux/platform_device.h>
#include <linux/pm_runtime.h>

static unsigned int stage;
module_param(stage, uint, 0400);
MODULE_PARM_DESC(stage, "0=dump CMU_AUD, 1=also read ABOX version if clocked");

static const struct { u16 off; const char *name; } s10_aud_regs[] = {
	{ 0x0000, "PLL_LOCKTIME_AUD0" }, { 0x0004, "PLL_LOCKTIME_AUD1" },
	{ 0x0100, "MUX_AUD_CPU_USER" }, { 0x0108, "MUX_AUD_CPU_USER_CON2" },
	{ 0x0120, "PLL_AUD0_CON0" }, { 0x012c, "PLL_AUD0_CON3" },
	{ 0x0140, "PLL_AUD1_CON0" }, { 0x014c, "PLL_AUD1_CON3" },
	{ 0x0800, "CONTROLLER_OPTION" },
	{ 0x1000, "MUX_AUD_CPU" }, { 0x1004, "MUX_AUD_DSIF" },
	{ 0x100c, "MUX_AUD_UAIF0" }, { 0x1010, "MUX_AUD_UAIF1" },
	{ 0x1014, "MUX_AUD_UAIF2" }, { 0x1018, "MUX_AUD_UAIF3" },
	{ 0x101c, "MUX_HCHGEN_AUD_CPU" },
	{ 0x1800, "DIV_AUDIF" }, { 0x1804, "DIV_BUS" }, { 0x1808, "DIV_BUSP" },
	{ 0x180c, "DIV_CNT" }, { 0x1810, "DIV_CPU_ACLK" }, { 0x181c, "DIV_DMIC" },
	{ 0x1820, "DIV_DSIF" }, { 0x1824, "DIV_MCLK" }, { 0x1828, "DIV_PLL" },
	{ 0x182c, "DIV_UAIF0" }, { 0x1830, "DIV_UAIF1" },
	{ 0x1834, "DIV_UAIF2" }, { 0x1838, "DIV_UAIF3" },
	{ 0x2000, "GAT_CMU_PCLK" }, { 0x2004, "GAT_CODEC_MCLK" },
	{ 0x2010, "GAT_ABOX_ACLK" }, { 0x2014, "GAT_ABOX_ACLK_IRQ" },
	{ 0x2020, "GAT_BCLK_UAIF0" }, { 0x2024, "GAT_BCLK_UAIF1" },
	{ 0x204c, "GAT_AXI2APB" }, { 0x2060, "GAT_LHM_AXI_P" },
	{ 0x206c, "GAT_LHS_AXI_D" }, { 0x2084, "GAT_RSTN_BUSP" },
	{ 0x2088, "GAT_RSTN_BUS" }, { 0x20a4, "GAT_RSTN_UAIF0" },
	{ 0x20a8, "GAT_RSTN_UAIF1" }, { 0x20b4, "GAT_SMMU" },
	{ 0x20b8, "GAT_SYSREG" }, { 0x20cc, "GAT_XIU_P" },
	{ 0x3020, "QCH_ABOX_ACLK" }, { 0x3024, "QCH_ABOX_BCLK0" },
	{ 0x3028, "QCH_ABOX_BCLK1" }, { 0x3044, "QCH_CMU_AUD" },
	{ 0x3050, "QCH_LHM_AXI_P" }, { 0x3064, "QCH_SMMU" },
	{ 0x3068, "QCH_SYSREG" },
};

/* Gate: bit 20 MANUAL, bit 21 CG_VAL, bit 28 HWACG. Q-channel: bit 0 ENABLE,
 * bit 1 CLOCK_REQ. Consider ACLK running only if the gate passes the clock
 * (manual+on or HWACG) and the Q-channel is either forced on or under HW control.
 */
static bool s10_aud_aclk_running(u32 gate, u32 qch)
{
	bool gate_ok = (gate & BIT(28)) || !(gate & BIT(20)) || (gate & BIT(21));
	bool qch_ok = (qch & BIT(0)) || (qch & BIT(1));

	return gate_ok && qch_ok;
}

static int s10_aud_probe(struct platform_device *pdev)
{
	struct device *dev = &pdev->dev;
	void __iomem *cmu, *abox = NULL;
	struct resource *res;
	struct clk *top;
	u32 gate, qch, val;
	int ret, i;

	res = platform_get_resource_byname(pdev, IORESOURCE_MEM, "cmu");
	if (!res || res->start != 0x18c00000 ||
	    !of_machine_is_compatible("samsung,beyond1lte"))
		return -ENODEV;
	cmu = devm_ioremap_resource(dev, res);
	if (IS_ERR(cmu))
		return PTR_ERR(cmu);
	if (stage >= 1) {
		res = platform_get_resource_byname(pdev, IORESOURCE_MEM, "abox");
		if (!res || res->start != 0x18c50000)
			return -ENODEV;
		abox = devm_ioremap_resource(dev, res);
		if (IS_ERR(abox))
			return PTR_ERR(abox);
	}
	top = devm_clk_get(dev, "top");
	if (IS_ERR(top))
		return dev_err_probe(dev, PTR_ERR(top), "TOP AUD clock unavailable\n");
	ret = clk_prepare_enable(top);
	if (ret)
		return ret;
	dev_info(dev, "stage %u: TOP AUD CPU clock %lu Hz enabled; resuming AUD domain\n",
		 stage, clk_get_rate(top));
	pm_runtime_enable(dev);
	ret = pm_runtime_resume_and_get(dev);
	if (ret < 0)
		goto out_clk;
	dev_info(dev, "AUD domain active; dumping CMU_AUD (read-only)\n");
	for (i = 0; i < ARRAY_SIZE(s10_aud_regs); i++) {
		val = readl(cmu + s10_aud_regs[i].off);
		dev_info(dev, "CMU_AUD+%#06x %-22s = %#010x\n",
			 s10_aud_regs[i].off, s10_aud_regs[i].name, val);
	}
	gate = readl(cmu + 0x2010);
	qch = readl(cmu + 0x3020);
	if (stage >= 1) {
		if (!s10_aud_aclk_running(gate, qch)) {
			dev_warn(dev, "ABOX ACLK not proven running (gate %#x qch %#x); skipping ABOX read\n",
				 gate, qch);
		} else {
			dev_info(dev, "ABOX ACLK gate %#x qch %#x; reading ABOX identification\n",
				 gate, qch);
			dev_info(dev, "ABOX_IP_INDEX=%#010x ABOX_VERSION=%#010x\n",
				 readl(abox + 0x0), readl(abox + 0x4));
		}
	}
	dev_info(dev, "done; releasing AUD domain and TOP clock\n");
	pm_runtime_put_sync_suspend(dev);
	ret = 0;
out_clk:
	pm_runtime_disable(dev);
	clk_disable_unprepare(top);
	return ret;
}

static const struct of_device_id s10_aud_match[] = {
	{ .compatible = "samsung,exynos9820-aud-probe-test" }, { }
};
MODULE_DEVICE_TABLE(of, s10_aud_match);
static struct platform_driver s10_aud_driver = {
	.probe = s10_aud_probe,
	.driver = { .name = "s10-aud-probe", .of_match_table = s10_aud_match },
};
module_platform_driver(s10_aud_driver);
MODULE_LICENSE("GPL");
MODULE_DESCRIPTION("S10 CMU_AUD/ABOX read-only diagnostic");
