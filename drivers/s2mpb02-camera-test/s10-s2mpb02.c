// SPDX-License-Identifier: GPL-2.0-or-later
/* Galaxy S10 camera supplies. Register layout and voltage ranges from
 * Samsung Electronics' GPL s2mpb02-regulator.c / s2mpb02-regulator.h (2014).
 *
 * The default probe is read-only. With regulators=1 it exposes the LDOs and
 * the buck that the camera sensors use (Samsung board file names in the DT):
 *
 *   buck1   S2MPB02_BUCK1         1.3-V input of the S2MPB03 S5K2L4 (main)
 *                                 core rails; always-on in Samsung's DT
 *   buck2   VDD_EXT_1P3_PB02      all rear sensors and the front sensor
 *   ldo3    VDDD_1.05V_SUB        S5K3M3 (tele) digital core
 *   ldo4    VDDD_1.05V_WIDESUB    S5K3P9 (ultra-wide) digital core
 *   ldo5    VDDD_1.05V_VT         S5K3J1 (front) digital core
 *   ldo6    VDDIO_1.8V_VT         S5K3J1 / S5K3P9 I/O
 *   ldo7    VDDIO_1.8V_SUB        S5K3M3 I/O
 *   ldo8    VDDIO_1.8V_WIDESUB    S5K3P9 I/O
 *   ldo11   VDDA_2.8V_SUB         S5K3M3 analog
 *   ldo12   VDDAF_2.8V_SUB        S5K3M3 AF actuator (AK7374)
 *   ldo13   VDDA_2.95V_VT         S5K3J1 analog
 *   ldo15   VDDA_2.8V_WIDESUB     S5K3P9 analog
 *
 * Boot state: Samsung's bootloader leaves these registers at the values in
 * pb02_boot[]. The regulator core rewrites the voltage codes and enable bits;
 * they are put back on unload and on shutdown/reboot, so the next boot (or the
 * next insmod) starts from the same state. Probe refuses to touch a supply
 * whose enable bit is already set (somebody else is using it). BUCK1 is the
 * exception: it is always-on in Samsung's DT, stays on across a warm reboot
 * after a stock kernel ran, and has no other user in pmOS, so it is taken over.
 */
#include <linux/i2c.h>
#include <linux/leds.h>
#include <linux/module.h>
#include <linux/of.h>
#include <linux/regmap.h>
#include <linux/regulator/driver.h>

static bool regulators;
module_param(regulators, bool, 0400);
MODULE_PARM_DESC(regulators, "Register the S10 camera supplies (default: read-only)");
static bool af_common_only;
module_param(af_common_only, bool, 0400);
MODULE_PARM_DESC(af_common_only,
		 "Expose only LDO14 (AF-common); snapshot and restore its exact initial byte");

#define PB02_ID 0x00
#define PB02_B1CTRL1 0x0b
#define PB02_B1CTRL2 0x0c
#define PB02_B2CTRL1 0x0d
#define PB02_B2CTRL2 0x0e
#define PB02_BST_CTRL2 0x1d
#define PB02_LCTRL(n) (0x1d + (n))	/* LDOn control: enable in bit 7, code in 6:0 */

/* Flash LED (Samsung GPL leds-s2mpb02.c / s2mpb02-regulator.h) */
#define PB02_FLED_CTRL1	0x12	/* 7: enable, 6: 1 = torch / 0 = flash, 3: low-voltage mode */
#define PB02_FLED_CUR1	0x14	/* 7:4 flash current, 3:0 torch current */
#define PB02_FLED_TIME1	0x15	/* 7:4 flash timeout, 3:0 torch timeout */
#define PB02_FLED_MODE	0xc0
#define PB02_FLED_TORCH	0xc0
#define PB02_FLED_FLASH	0x80
#define PB02_FLED_LV	0x08

#define PB02_BUCK_EN	0xc0
#define PB02_LDO_EN	0x80

struct pb02_boot_reg {
	u8 reg;
	u8 value;
	u8 enable_mask;
};

/* Order matters for restore: LDOs first, the shared buck last. */
static const struct pb02_boot_reg pb02_boot[] = {
	{ PB02_LCTRL(3),  0x20, PB02_LDO_EN },
	{ PB02_LCTRL(4),  0x30, PB02_LDO_EN },
	{ PB02_LCTRL(5),  0x30, PB02_LDO_EN },
	{ PB02_LCTRL(6),  0x30, PB02_LDO_EN },
	{ PB02_LCTRL(7),  0x30, PB02_LDO_EN },
	{ PB02_LCTRL(8),  0x30, PB02_LDO_EN },
	{ PB02_LCTRL(10), 0x30, PB02_LDO_EN },
	{ PB02_LCTRL(11), 0x58, PB02_LDO_EN },
	{ PB02_LCTRL(12), 0x58, PB02_LDO_EN },
	{ PB02_LCTRL(13), 0x5c, PB02_LDO_EN },
	{ PB02_LCTRL(14), 0x58, PB02_LDO_EN },
	{ PB02_LCTRL(15), 0x58, PB02_LDO_EN },
	{ PB02_LCTRL(16), 0x58, PB02_LDO_EN },
	{ PB02_B2CTRL2,   0x40, 0 },
	{ PB02_B2CTRL1,   0x58, PB02_BUCK_EN },
	/* BUCK1 last: it feeds the S2MPB03 core rails of the main sensor.
	 * Disable before restoring the boot selector (0xc0 = 1.6 V > 1.3 V).
	 */
	{ PB02_B1CTRL1,   0x58, 0 },
	{ PB02_B1CTRL2,   0xc0, 0 },
};

static bool pb02_writable(struct device *dev, unsigned int reg)
{
	int i;

	if (reg == PB02_FLED_CTRL1 || reg == PB02_FLED_CUR1 || reg == PB02_FLED_TIME1)
		return true;
	if (!regulators)
		return false;
	if (af_common_only)
		return reg == PB02_LCTRL(14);
	for (i = 0; i < ARRAY_SIZE(pb02_boot); i++)
		if (pb02_boot[i].reg == reg)
			return true;
	return false;
}

static const struct regmap_config pb02_regmap_config = {
	.reg_bits = 8,
	.val_bits = 8,
	.max_register = 0x32,
	.cache_type = REGCACHE_NONE,
	.writeable_reg = pb02_writable,
};

struct pb02_state {
	struct regmap *map;
	struct led_classdev torch;
	struct led_classdev strobe;
	u8 af_common_initial;
	bool af_common_only;
};

static const struct regulator_ops pb02_ops = {
	.list_voltage = regulator_list_voltage_linear,
	.map_voltage = regulator_map_voltage_linear,
	.get_voltage_sel = regulator_get_voltage_sel_regmap,
	.set_voltage_sel = regulator_set_voltage_sel_regmap,
	.is_enabled = regulator_is_enabled_regmap,
	.enable = regulator_enable_regmap,
	.disable = regulator_disable_regmap,
};

/* LDO1..5 step 12.5 mV, LDO6..18 step 25 mV, both from 0.6 V */
#define PB02_LDO(_n, _step) { \
	.name = "s2mpb02-ldo" #_n, .of_match = "s2mpb02-ldo" #_n, \
	.regulators_node = "regulators", \
	.id = _n, .ops = &pb02_ops, .type = REGULATOR_VOLTAGE, \
	.owner = THIS_MODULE, .min_uV = 600000, .uV_step = _step, \
	.n_voltages = 128, .vsel_reg = PB02_LCTRL(_n), .vsel_mask = 0x7f, \
	.enable_reg = PB02_LCTRL(_n), .enable_mask = 0x80, .enable_time = 180, \
}

static const struct regulator_desc pb02_desc[] = {
	{
		.name = "s2mpb02-buck2", .of_match = "s2mpb02-buck2",
		.regulators_node = "regulators", .id = 0, .ops = &pb02_ops,
		.type = REGULATOR_VOLTAGE, .owner = THIS_MODULE,
		.min_uV = 400000, .uV_step = 6250, .n_voltages = 256,
		.vsel_reg = PB02_B2CTRL2, .vsel_mask = 0xff,
		.enable_reg = PB02_B2CTRL1, .enable_mask = 0xc0,
		.enable_val = 0xc0, .enable_time = 100,
	},
	{
		.name = "s2mpb02-buck1", .of_match = "s2mpb02-buck1",
		.regulators_node = "regulators", .id = 1, .ops = &pb02_ops,
		.type = REGULATOR_VOLTAGE, .owner = THIS_MODULE,
		.min_uV = 400000, .uV_step = 6250, .n_voltages = 256,
		.vsel_reg = PB02_B1CTRL2, .vsel_mask = 0xff,
		.enable_reg = PB02_B1CTRL1, .enable_mask = 0xc0,
		.enable_val = 0xc0, .enable_time = 100,
	},
	PB02_LDO(3, 12500),
	PB02_LDO(4, 12500),
	PB02_LDO(5, 12500),
	PB02_LDO(6, 25000),
	PB02_LDO(7, 25000),
	PB02_LDO(8, 25000),
	PB02_LDO(10, 25000),
	PB02_LDO(11, 25000),
	PB02_LDO(12, 25000),
	PB02_LDO(13, 25000),
	PB02_LDO(14, 25000),
	PB02_LDO(15, 25000),
	PB02_LDO(16, 25000),
};

static ssize_t status_show(struct device *dev, struct device_attribute *attr, char *buf)
{
	struct pb02_state *state = dev_get_drvdata(dev);
	struct regmap *map = state->map;
	unsigned int val;
	ssize_t len = 0;
	int i, ret;

	ret = regmap_read(map, PB02_ID, &val);
	if (ret)
		return ret;
	len += sysfs_emit_at(buf, len, "%02x=%02x\n", PB02_ID, val);
	ret = regmap_read(map, PB02_BST_CTRL2, &val);
	if (ret)
		return ret;
	len += sysfs_emit_at(buf, len, "%02x=%02x\n", PB02_BST_CTRL2, val);
	for (i = 0; i < ARRAY_SIZE(pb02_boot); i++) {
		ret = regmap_read(map, pb02_boot[i].reg, &val);
		if (ret)
			return ret;
		len += sysfs_emit_at(buf, len, "%02x=%02x\n", pb02_boot[i].reg, val);
	}
	return len;
}
static DEVICE_ATTR_RO(status);
static struct attribute *pb02_attrs[] = { &dev_attr_status.attr, NULL };
ATTRIBUTE_GROUPS(pb02);

static void pb02_restore(void *data)
{
	struct pb02_state *state = data;
	struct regmap *map = state->map;
	int i, ret;

	/* Consumers must be removed first. */
	if (state->af_common_only) {
		ret = regmap_write(map, PB02_LCTRL(14), state->af_common_initial);
		if (ret)
			pr_err("s10-s2mpb02: restore AF-common register failed: %d\n", ret);
		return;
	}
	for (i = 0; i < ARRAY_SIZE(pb02_boot); i++) {
		ret = regmap_write(map, pb02_boot[i].reg, pb02_boot[i].value);
		if (ret)
			pr_err("s10-s2mpb02: restore register %#x failed: %d\n",
			       pb02_boot[i].reg, ret);
	}
}

/* Put the PMIC back to its boot state on reboot/poweroff. */
static void pb02_led_off(void *data);

static void pb02_shutdown(struct i2c_client *client)
{
	struct pb02_state *state = dev_get_drvdata(&client->dev);

	pb02_led_off(state);
	if (regulators)
		pb02_restore(state);
}

/*
 * Rear flash LED. Samsung's DT (s2mpb02_pmic@59/torch) uses torch_table <2 4 4 5 6 8 9 10 10 12> for
 * ten torch levels and brightness 0xc / timeout 0x7 for the flash. GPIO control (torch gpg2-4, flash
 * gpg3-6) is not used: the LED is switched over I2C.
 */
static const u8 pb02_torch_table[] = { 2, 4, 4, 5, 6, 8, 9, 10, 10, 12 };

static int pb02_torch_set(struct led_classdev *led, enum led_brightness value)
{
	struct pb02_state *state = container_of(led, struct pb02_state, torch);
	int ret;

	if (value == LED_OFF)
		return regmap_update_bits(state->map, PB02_FLED_CTRL1, PB02_FLED_MODE, 0);
	ret = regmap_update_bits(state->map, PB02_FLED_CUR1, 0x0f, pb02_torch_table[value - 1]);
	if (ret)
		return ret;
	return regmap_update_bits(state->map, PB02_FLED_CTRL1, PB02_FLED_MODE | PB02_FLED_LV,
				  PB02_FLED_TORCH);
}

/* brightness 1 fires one flash pulse; the PMIC's flash timer ends it. */
static int pb02_strobe_set(struct led_classdev *led, enum led_brightness value)
{
	struct pb02_state *state = container_of(led, struct pb02_state, strobe);
	int ret;

	ret = regmap_update_bits(state->map, PB02_FLED_CTRL1, PB02_FLED_MODE, 0);
	if (ret || value == LED_OFF)
		return ret;
	ret = regmap_update_bits(state->map, PB02_FLED_CUR1, 0xf0, 0xc << 4);
	if (!ret)
		ret = regmap_update_bits(state->map, PB02_FLED_TIME1, 0xf0, 0x7 << 4);
	if (!ret)
		ret = regmap_update_bits(state->map, PB02_FLED_CTRL1, PB02_FLED_MODE | PB02_FLED_LV,
					 PB02_FLED_FLASH);
	return ret;
}

static void pb02_led_off(void *data)
{
	struct pb02_state *state = data;

	regmap_update_bits(state->map, PB02_FLED_CTRL1, PB02_FLED_MODE, 0);
}

static int pb02_leds_init(struct device *dev, struct pb02_state *state)
{
	int ret;

	state->torch.name = "white:torch";
	state->torch.max_brightness = ARRAY_SIZE(pb02_torch_table);
	state->torch.brightness_set_blocking = pb02_torch_set;
	state->strobe.name = "s2mpb02:strobe";
	state->strobe.max_brightness = 1;
	state->strobe.brightness_set_blocking = pb02_strobe_set;
	ret = devm_add_action_or_reset(dev, pb02_led_off, state);
	if (ret)
		return ret;
	ret = devm_led_classdev_register(dev, &state->torch);
	if (ret)
		return ret;
	return devm_led_classdev_register(dev, &state->strobe);
}

static int pb02_probe(struct i2c_client *client)
{
	struct regulator_config config = { .dev = &client->dev };
	struct regulator_dev *rdev;
	struct pb02_state *state;
	struct regmap *map;
	unsigned int id, revision, af_common_initial;
	int ret, i;

	if (client->addr != 0x59 || !of_machine_is_compatible("samsung,beyond1lte"))
		return -ENODEV;
	map = devm_regmap_init_i2c(client, &pb02_regmap_config);
	if (IS_ERR(map))
		return PTR_ERR(map);
	state = devm_kzalloc(&client->dev, sizeof(*state), GFP_KERNEL);
	if (!state)
		return -ENOMEM;
	state->map = map;
	state->af_common_only = af_common_only;
	dev_set_drvdata(&client->dev, state);
	ret = regmap_read(map, PB02_ID, &id);
	if (ret)
		return dev_err_probe(&client->dev, ret, "ID read failed\n");
	ret = regmap_read(map, PB02_BST_CTRL2, &revision);
	if (ret)
		return ret;
	dev_info(&client->dev, "S2MPB02 ID=%#x BST_CTRL2=%#x; %s\n",
		id, revision, regulators ? "camera regulators" : "read-only identification");
	ret = pb02_leds_init(&client->dev, state);
	if (ret)
		return dev_err_probe(&client->dev, ret, "flash LED registration failed\n");
	if (!regulators)
		return 0;
	if (af_common_only) {
		ret = regmap_read(map, PB02_LCTRL(14), &af_common_initial);
		if (ret)
			return ret;
		state->af_common_initial = af_common_initial;
		if (af_common_initial & PB02_LDO_EN)
			return dev_err_probe(&client->dev, -EBUSY,
					     "AF-common LDO14 is active (%#x); leaving shared rail untouched\n",
					     state->af_common_initial);
		ret = devm_add_action_or_reset(&client->dev, pb02_restore, state);
		if (ret)
			return ret;
		config.regmap = map;
		for (i = 0; i < ARRAY_SIZE(pb02_desc); i++) {
			if (pb02_desc[i].id != 14)
				continue;
			rdev = devm_regulator_register(&client->dev, &pb02_desc[i], &config);
			if (IS_ERR(rdev))
				return dev_err_probe(&client->dev, PTR_ERR(rdev),
						     "AF-common regulator failed\n");
			return 0;
		}
		return -EINVAL;
	}
	/* Values measured in the read-only test on this phone. */
	if (id != 0 || revision != 0x90)
		return -ENODEV;
	for (i = 0; i < ARRAY_SIZE(pb02_boot); i++) {
		unsigned int value;

		ret = regmap_read(map, pb02_boot[i].reg, &value);
		if (ret)
			return ret;
		if (pb02_boot[i].enable_mask == PB02_LDO_EN && (value & PB02_LDO_EN))
			return dev_err_probe(&client->dev, -EBUSY,
				"LDO register %#x is already enabled (%#x); not taking it over\n",
				pb02_boot[i].reg, value);
		if (pb02_boot[i].enable_mask == PB02_BUCK_EN &&
		    (value & PB02_BUCK_EN) == PB02_BUCK_EN)
			return dev_err_probe(&client->dev, -EBUSY,
				"buck2 is already enabled (%#x)\n", value);
	}
	ret = devm_add_action_or_reset(&client->dev, pb02_restore, state);
	if (ret)
		return ret;
	config.regmap = map;
	for (i = 0; i < ARRAY_SIZE(pb02_desc); i++) {
		rdev = devm_regulator_register(&client->dev, &pb02_desc[i], &config);
		if (IS_ERR(rdev))
			return dev_err_probe(&client->dev, PTR_ERR(rdev), "regulator %s failed\n",
					     pb02_desc[i].name);
	}
	return 0;
}

static const struct of_device_id pb02_match[] = {
	{ .compatible = "samsung,s2mpb02-camera-test" }, { }
};
MODULE_DEVICE_TABLE(of, pb02_match);
static struct i2c_driver pb02_driver = {
	.driver = { .name = "s10-s2mpb02", .of_match_table = pb02_match,
		.dev_groups = pb02_groups },
	.probe = pb02_probe,
	.shutdown = pb02_shutdown,
};
module_i2c_driver(pb02_driver);
MODULE_LICENSE("GPL");
MODULE_DESCRIPTION("S10 S2MPB02 camera supplies and rear flash LED");
