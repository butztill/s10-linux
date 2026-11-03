// SPDX-License-Identifier: GPL-2.0
/* Sets the USI SW_CONF of UART_BT (sysreg_peric1 0x1000) to UART mode. Test helper. */
#include <linux/module.h>
#include <linux/mfd/syscon.h>
#include <linux/regmap.h>

static unsigned int offset = 0x1000;
static unsigned int value = 1;
module_param(offset, uint, 0644);
module_param(value, uint, 0644);

static int __init s10_uart_bt_mode_init(void)
{
	struct regmap *rm = syscon_regmap_lookup_by_compatible("samsung,exynos9820-peric1-sysreg");
	unsigned int old = 0;
	int ret;

	if (IS_ERR(rm))
		return PTR_ERR(rm);
	ret = regmap_read(rm, offset, &old);
	pr_info("s10-uart-bt-mode: sw_conf[0x%x] was 0x%x (ret %d)\n", offset, old, ret);
	ret = regmap_write(rm, offset, value);
	regmap_read(rm, offset, &old);
	pr_info("s10-uart-bt-mode: wrote 0x%x -> now 0x%x (ret %d)\n", value, old, ret);
	return 0;
}
module_init(s10_uart_bt_mode_init);
MODULE_LICENSE("GPL");
