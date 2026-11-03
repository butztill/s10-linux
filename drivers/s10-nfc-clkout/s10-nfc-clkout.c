// SPDX-License-Identifier: GPL-2.0
/* Enables the SoC CLKOUT1 pad (stock "OSC_NFC") that feeds the NFC chip XI clock.
 * Stock cal: PMU 0xa0c, mux sel in bits [13:8], bit 0 = disable (0 = enabled). Test helper. */
#include <linux/module.h>
#include <linux/mfd/syscon.h>
#include <linux/regmap.h>

static unsigned int offset = 0xa0c;
static unsigned int sel;
static bool disable;
module_param(offset, uint, 0644);
module_param(sel, uint, 0644);
module_param(disable, bool, 0644);

static int __init s10_nfc_clkout_init(void)
{
	struct regmap *rm = syscon_regmap_lookup_by_compatible("samsung,exynos9820-pmu");
	unsigned int old = 0, now = 0, val;

	if (IS_ERR(rm))
		return PTR_ERR(rm);
	regmap_read(rm, offset, &old);
	val = (old & ~(0x3fu << 8) & ~1u) | (sel << 8) | (disable ? 1 : 0);
	regmap_write(rm, offset, val);
	regmap_read(rm, offset, &now);
	pr_info("s10-nfc-clkout: PMU[0x%x] 0x%x -> 0x%x (now 0x%x)\n", offset, old, val, now);
	return 0;
}
module_init(s10_nfc_clkout_init);
MODULE_DESCRIPTION("S10 NFC CLKOUT1 enable (test)");
MODULE_LICENSE("GPL");
