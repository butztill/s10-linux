// SPDX-License-Identifier: GPL-2.0
/* Debug aid: read (and thereby clear) the S2MPS19 interrupt status registers. */
#include <linux/module.h>
#include <linux/platform_device.h>
#include <linux/regmap.h>

static struct device *pmic_c, *pmic_p;

static int dump_get(char *buf, const struct kernel_param *kp)
{
	struct regmap *c, *p;
	unsigned int v, i;
	int n = 0;

	if (!pmic_c || !pmic_p)
		return sysfs_emit(buf, "no pmic\n");
	c = dev_get_regmap(pmic_c, "common");
	p = dev_get_regmap(pmic_p, "pmic");
	if (!c || !p)
		return sysfs_emit(buf, "no regmap c=%p p=%p\n", c, p);
	if (!regmap_read(c, 1, &v))
		n += sysfs_emit_at(buf, n, "common_int=%02x ", v);
	for (i = 0; i < 14; i++)
		if (!regmap_read(p, i, &v))
			n += sysfs_emit_at(buf, n, "int%u=%02x ", i + 1, v);
	n += sysfs_emit_at(buf, n, "\n");
	return n;
}
static const struct kernel_param_ops dump_ops = { .get = dump_get };
module_param_cb(dump, &dump_ops, NULL, 0400);

/* Unmask PWRONF/PWRONR (INT1M bits 0/1) so a power-key press drives the PMIC IRQ line. */
static int pwron_set(const char *val, const struct kernel_param *kp)
{
	struct regmap *p;
	bool en;
	int ret = kstrtobool(val, &en);

	if (ret)
		return ret;
	p = pmic_p ? dev_get_regmap(pmic_p, "pmic") : NULL;
	if (!p)
		return -ENODEV;
	ret = regmap_update_bits(p, 7, 0x3, en ? 0 : 0x3);
	pr_info("s10-pmic-st: INT1M pwron %s -> %d\n", en ? "unmasked" : "masked", ret);
	return ret;
}
static const struct kernel_param_ops pwron_ops = { .set = pwron_set };
module_param_cb(pwron_wake, &pwron_ops, NULL, 0200);

/* "reg=val" (hex) writes one register of the "pmic" regmap, e.g. a4=be */
static int poke_set(const char *val, const struct kernel_param *kp)
{
	struct regmap *p = pmic_p ? dev_get_regmap(pmic_p, "pmic") : NULL;
	unsigned int reg, v, rb = 0;
	int ret;

	if (!p)
		return -ENODEV;
	if (sscanf(val, "%x=%x", &reg, &v) != 2 || reg > 0xff || v > 0xff)
		return -EINVAL;
	ret = regmap_write(p, reg, v);
	regmap_read(p, reg, &rb);
	pr_info("s10-pmic-st: %02x=%02x -> %d (read back %02x)\n", reg, v, ret, rb);
	return ret;
}
static const struct kernel_param_ops poke_ops = { .set = poke_set };
module_param_cb(poke, &poke_ops, NULL, 0200);

static int __init s10_pmic_int_init(void)
{
	pmic_c = bus_find_device_by_name(&platform_bus_type, NULL, "firmware:power-management:pmic-1");
	pmic_p = bus_find_device_by_name(&platform_bus_type, NULL, "firmware:power-management:pmic-1");
	return pmic_c && pmic_p ? 0 : -ENODEV;
}
module_init(s10_pmic_int_init);
MODULE_LICENSE("GPL");
MODULE_DESCRIPTION("S10 S2MPS19 interrupt/register debug aid");
