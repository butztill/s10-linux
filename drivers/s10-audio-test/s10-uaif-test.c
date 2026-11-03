// SPDX-License-Identifier: GPL-2.0-only
/* S10 ABOX UAIF1 clock-output test (no audio data, no DMA, no firmware).
 * Drives UAIF1 as clock master, 48 kHz, 2 x 32 bit, DSP-A with inverted BCLK
 * (Samsung beyond1lte uaif1 link), samples the gpb1 pad levels and restores
 * every touched register afterwards. The amplifiers stay in standby.
 * Offsets: Samsung cmucal-sfr.c, abox_soc.h, abox_if.c, abox.c (LineageOS 4bcfb02).
 */
#include <linux/clk.h>
#include <linux/delay.h>
#include <linux/io.h>
#include <linux/iopoll.h>
#include <linux/mfd/syscon.h>
#include <linux/module.h>
#include <linux/of.h>
#include <linux/platform_device.h>
#include <linux/pm_runtime.h>
#include <linux/regmap.h>

#define CMU_DIV_AUDIF		0x1800
#define CMU_DIV_UAIF1		0x1830
#define CMU_DIV_BUSY		BIT(16)
#define ABOX_QCH_DISABLE	0x0038
#define ABOX_QCH_BCLK_UAIF1	BIT(3)
#define ABOX_ROUTE_CTRL0	0x0040
#define ABOX_UAIF1_CTRL0	0x0510
#define ABOX_UAIF1_CTRL1	0x0514
#define ABOX_UAIF1_STATUS	0x051c
#define GPB1_PHYS		0x18c60020
#define GPB1_CON		0x0
#define GPB1_DAT		0x4
#define PMU_PAD_NORMAL_AUD	0x1920
#define PMU_PAD_NORMAL_BIT	BIT(11)

static int s10_cmu_div_set(struct device *dev, void __iomem *reg, u32 ratio);
static unsigned int samples = 200000;
module_param(samples, uint, 0400);
static unsigned int uaif_div = 191;	/* 589.824 MHz / 192 = 3.072 MHz */
module_param(uaif_div, uint, 0400);
static bool pll = true;
module_param(pll, bool, 0400);
MODULE_PARM_DESC(pll, "program PLL_AUD0 to the bootloader's 1179.648 MHz setting");
static bool i2s = true;
module_param(i2s, bool, 0400);
MODULE_PARM_DESC(i2s, "I2S framing (50% WS, pad-visible) instead of the board's DSP-A");

/* CMU_AUD state left by the S-boot bootloader, read on the first AUD power-on
 * (logs/audio-aud-probe-20261020.log). After a genpd power cycle the block is
 * in reset (PLL off, all dividers 0), so Linux must restore it.
 */
static const struct { u16 off; u32 val; } s10_aud_boot[] = {
	{ 0x0800, 0xf0000000 },	/* CONTROLLER_OPTION */
	{ 0x1800, 0x1 },	/* DIV_AUDIF  /2  -> 589.824 MHz */
	{ 0x1804, 0x6 },	/* DIV_BUS    /7  -> 168.5 MHz */
	{ 0x1808, 0x2 },	/* DIV_BUSP   /3 */
	{ 0x180c, 0x5 },	/* DIV_CNT */
	{ 0x1810, 0x1 },	/* DIV_CPU_ACLK */
	{ 0x181c, 0x1 },	/* DIV_DMIC */
	{ 0x1824, 0x2 },	/* DIV_MCLK */
	{ 0x1828, 0x3 },	/* DIV_PLL    /4  -> 294.9 MHz */
};
#define CMU_PLL_LOCKTIME	0x0000
#define CMU_PLL_CON0		0x0120
#define CMU_PLL_CON3		0x012c
#define CMU_PLL_ENABLE		BIT(31)
#define CMU_PLL_STABLE		BIT(29)
#define CMU_PLL_MUX_SEL		BIT(4)

static int s10_aud_pll_on(struct device *dev, void __iomem *cmu, u32 *saved)
{
	u32 val;
	int i, ret;

	saved[0] = readl(cmu + CMU_PLL_LOCKTIME);
	saved[1] = readl(cmu + CMU_PLL_CON3);
	saved[2] = readl(cmu + CMU_PLL_CON0);
	for (i = 0; i < ARRAY_SIZE(s10_aud_boot); i++)
		saved[3 + i] = readl(cmu + s10_aud_boot[i].off);
	if (saved[2] & CMU_PLL_ENABLE) {
		dev_info(dev, "PLL_AUD0 already enabled (%#x); leaving it\n", saved[2]);
		return 0;
	}
	writel(0x96, cmu + CMU_PLL_LOCKTIME);
	writel(0x5eff, cmu + CMU_PLL_CON3);		/* K */
	writel(CMU_PLL_ENABLE | (45 << 16) | (1 << 8), cmu + CMU_PLL_CON0); /* M=45 P=1 S=0 */
	ret = readl_poll_timeout_atomic(cmu + CMU_PLL_CON0, val, val & CMU_PLL_STABLE, 1, 2000);
	if (ret) {
		dev_err(dev, "PLL_AUD0 did not lock (%#x)\n", val);
		writel(saved[2], cmu + CMU_PLL_CON0);
		return ret;
	}
	writel(val | CMU_PLL_MUX_SEL, cmu + CMU_PLL_CON0);
	for (i = 0; i < ARRAY_SIZE(s10_aud_boot); i++)
		if (s10_aud_boot[i].off == 0x0800)
			writel(s10_aud_boot[i].val, cmu + s10_aud_boot[i].off);
		else
			s10_cmu_div_set(dev, cmu + s10_aud_boot[i].off, s10_aud_boot[i].val);
	dev_info(dev, "PLL_AUD0 locked: CON0=%#x CON3=%#x (26 MHz * (45 + 0x5eff/65536) = 1179.648 MHz)\n",
		 readl(cmu + CMU_PLL_CON0), readl(cmu + CMU_PLL_CON3));
	return 0;
}

static void s10_aud_pll_restore(struct device *dev, void __iomem *cmu, const u32 *saved)
{
	int i;

	if (saved[2] & CMU_PLL_ENABLE)
		return;
	for (i = ARRAY_SIZE(s10_aud_boot) - 1; i >= 0; i--)
		if (s10_aud_boot[i].off == 0x0800)
			writel(saved[3 + i], cmu + s10_aud_boot[i].off);
		else
			s10_cmu_div_set(dev, cmu + s10_aud_boot[i].off, saved[3 + i] & 0x1ff);
	writel(readl(cmu + CMU_PLL_CON0) & ~CMU_PLL_MUX_SEL, cmu + CMU_PLL_CON0);
	writel(saved[2], cmu + CMU_PLL_CON0);
	writel(saved[1], cmu + CMU_PLL_CON3);
	writel(saved[0], cmu + CMU_PLL_LOCKTIME);
}

static int s10_cmu_div_set(struct device *dev, void __iomem *reg, u32 ratio)
{
	/* ratio field is 9 bits wide at most; narrower fields ignore upper bits */
	u32 val;

	writel((readl(reg) & ~0x1ff) | ratio, reg);
	return readl_poll_timeout_atomic(reg, val, !(val & CMU_DIV_BUSY), 1, 1000);
}

static int s10_uaif_probe(struct platform_device *pdev)
{
	struct device *dev = &pdev->dev;
	u32 s_div, s_qch, s_c0, s_c1, s_con, pad, c1, dat, prev, st;
	u32 s_pll[3 + ARRAY_SIZE(s10_aud_boot)];
	unsigned int edges[4] = { 0 }, i, b;
	void __iomem *cmu, *abox, *gpb1;
	struct regmap *pmu;
	struct resource *res;
	struct clk *top;
	int ret;

	res = platform_get_resource_byname(pdev, IORESOURCE_MEM, "cmu");
	if (!res || res->start != 0x18c00000 ||
	    !of_machine_is_compatible("samsung,beyond1lte"))
		return -ENODEV;
	cmu = devm_ioremap(dev, res->start, 0x8000);
	abox = devm_ioremap(dev, 0x18c50000, 0x1000);
	gpb1 = devm_ioremap(dev, GPB1_PHYS, 0x20);	/* shared with pinctrl: no request */
	if (!cmu || !abox || !gpb1 || uaif_div > 511)
		return -EINVAL;
	pmu = syscon_regmap_lookup_by_compatible("samsung,exynos9820-pmu");
	if (IS_ERR(pmu))
		return dev_err_probe(dev, PTR_ERR(pmu), "PMU syscon unavailable\n");
	top = devm_clk_get(dev, "top");
	if (IS_ERR(top))
		return PTR_ERR(top);
	ret = clk_prepare_enable(top);
	if (ret)
		return ret;
	pm_runtime_enable(dev);
	ret = pm_runtime_resume_and_get(dev);
	if (ret < 0) {
		pm_runtime_disable(dev);
		clk_disable_unprepare(top);
		return ret;
	}

	regmap_read(pmu, PMU_PAD_NORMAL_AUD, &pad);
	dev_info(dev, "AUD on; PMU PAD_NORMAL_AUD=%#x (bit11 %s)\n", pad,
		 (pad & PMU_PAD_NORMAL_BIT) ? "normal" : "retention");
	if (!(pad & PMU_PAD_NORMAL_BIT))
		regmap_update_bits(pmu, PMU_PAD_NORMAL_AUD, PMU_PAD_NORMAL_BIT,
				   PMU_PAD_NORMAL_BIT);

	if (pll) {
		ret = s10_aud_pll_on(dev, cmu, s_pll);
		if (ret)
			goto out_pm;
	}
	s_div = readl(cmu + CMU_DIV_UAIF1);
	s_qch = readl(abox + ABOX_QCH_DISABLE);
	s_c0 = readl(abox + ABOX_UAIF1_CTRL0);
	s_c1 = readl(abox + ABOX_UAIF1_CTRL1);
	s_con = readl(gpb1 + GPB1_CON);
	dev_info(dev, "saved: DIV_AUDIF=%#x DIV_UAIF1=%#x QCH_DIS=%#x ROUTE0=%#x UAIF1 C0=%#x C1=%#x GPB1_CON=%#x\n",
		 readl(cmu + CMU_DIV_AUDIF), s_div, s_qch, readl(abox + ABOX_ROUTE_CTRL0),
		 s_c0, s_c1, s_con);

	ret = s10_cmu_div_set(dev, cmu + CMU_DIV_UAIF1, uaif_div);
	if (ret) {
		dev_err(dev, "DIV_UAIF1 busy timeout\n");
		goto restore;
	}
	writel((s_con & ~0xffff) | 0x2222, gpb1 + GPB1_CON);
	writel(s_qch | ABOX_QCH_BCLK_UAIF1, abox + ABOX_QCH_DISABLE);
	/* FORMAT 0x19 (2ch, 32 bit), WS_MODE 1 (DSP-A), BCLK_POL 0 (IB), WS_POL 0,
	 * SLOT_MAX 1, SBIT_MAX 31, VALID_STR/END 31. */
	c1 = (0x19 << 24) | (1 << 18) | (31 << 12);
	if (!i2s)
		c1 |= BIT(22) | (31 << 6) | 31;	/* DSP-A: WS_MODE 1, VALID 31/31 */
	writel(c1, abox + ABOX_UAIF1_CTRL1);
	writel((s_c0 & ~0x7) | BIT(2), abox + ABOX_UAIF1_CTRL0);	/* master */
	writel((s_c0 & ~0x7) | BIT(2) | BIT(0), abox + ABOX_UAIF1_CTRL0); /* SPK on */
	dev_info(dev, "UAIF1 SPK enabled: C0=%#x C1=%#x DIV_UAIF1=%#x; sampling %u pad reads\n",
		 readl(abox + ABOX_UAIF1_CTRL0), readl(abox + ABOX_UAIF1_CTRL1),
		 readl(cmu + CMU_DIV_UAIF1), samples);
	udelay(100);
	prev = readl(gpb1 + GPB1_DAT);
	for (i = 0; i < samples; i++) {
		dat = readl(gpb1 + GPB1_DAT);
		for (b = 0; b < 4; b++)
			if ((dat ^ prev) & BIT(b))
				edges[b]++;
		prev = dat;
	}
	st = readl(abox + ABOX_UAIF1_STATUS);
	dev_info(dev, "pad edges: gpb1-0=%u gpb1-1=%u gpb1-2=%u gpb1-3=%u; UAIF1_STATUS=%#x\n",
		 edges[0], edges[1], edges[2], edges[3], st);

	writel((s_c0 & ~0x7) | BIT(2), abox + ABOX_UAIF1_CTRL0);
	usleep_range(600, 1200);
restore:
	writel(s_c0, abox + ABOX_UAIF1_CTRL0);
	writel(s_c1, abox + ABOX_UAIF1_CTRL1);
	writel(s_qch, abox + ABOX_QCH_DISABLE);
	writel(s_con, gpb1 + GPB1_CON);
	s10_cmu_div_set(dev, cmu + CMU_DIV_UAIF1, s_div & 0x1ff);
	dev_info(dev, "restored: DIV_UAIF1=%#x QCH_DIS=%#x C0=%#x C1=%#x GPB1_CON=%#x\n",
		 readl(cmu + CMU_DIV_UAIF1), readl(abox + ABOX_QCH_DISABLE),
		 readl(abox + ABOX_UAIF1_CTRL0), readl(abox + ABOX_UAIF1_CTRL1),
		 readl(gpb1 + GPB1_CON));
	if (pll)
		s10_aud_pll_restore(dev, cmu, s_pll);
	ret = 0;
out_pm:
	pm_runtime_put_sync_suspend(dev);
	pm_runtime_disable(dev);
	clk_disable_unprepare(top);
	return ret;
}

static const struct of_device_id s10_uaif_match[] = {
	{ .compatible = "samsung,exynos9820-aud-probe-test" }, { }
};
MODULE_DEVICE_TABLE(of, s10_uaif_match);
static struct platform_driver s10_uaif_driver = {
	.probe = s10_uaif_probe,
	.driver = { .name = "s10-uaif-test", .of_match_table = s10_uaif_match },
};
module_platform_driver(s10_uaif_driver);
MODULE_LICENSE("GPL");
MODULE_DESCRIPTION("S10 ABOX UAIF1 clock-output test");
