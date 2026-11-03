// SPDX-License-Identifier: GPL-2.0-only
/* Local firmware-backed MAX77705 PD experiment for the Exynos Galaxy S10.
 * Register/opcode protocol reference: Samsung/Maxim's GPL downstream
 * drivers/ccic/max77705{_usbc,_pd}.c and include/linux/ccic/max77705.h.
 * This module never changes charger current, firmware, interrupt masks or roles.
 * Root may request a verified fixed 5 V/9 V PDO through its test attribute.
 */
#include <linux/delay.h>
#include <linux/i2c.h>
#include <linux/jiffies.h>
#include <linux/module.h>
#include <linux/mutex.h>
#include <linux/power_supply.h>
#include <linux/slab.h>
#include <linux/unaligned.h>
#include <linux/usb/typec.h>
#include <linux/workqueue.h>

#define REG_STATUS 0x06
#define REG_UIC_INT 0x02
#define REG_WRITE 0x21
#define REG_END 0x41
#define REG_RESPONSE 0x51
#define CMD_CURRENT_CAPS 0x30
#define CMD_REQUEST_CAP 0x32

struct s10_pd {
	struct i2c_client *client;
	struct mutex lock;
	bool mailbox_failed;
	struct typec_port *port;
	struct typec_partner *partner;
	struct delayed_work status_work;
	bool partner_pd;
};

/* One combined transfer: register pointer followed by a register read.
 * No read of clear-on-read interrupt registers except in the command path.
 */
static int pd_read(struct s10_pd *pd, u8 reg, void *buf, u16 len)
{
	struct i2c_msg msgs[] = {
		{ .addr = pd->client->addr, .len = 1, .buf = &reg },
		{ .addr = pd->client->addr, .flags = I2C_M_RD,
		  .len = len, .buf = buf },
	};
	int ret = i2c_transfer(pd->client->adapter, msgs, ARRAY_SIZE(msgs));

	return ret == ARRAY_SIZE(msgs) ? 0 : ret < 0 ? ret : -EIO;
}

/* Lock covers both mailbox directions. After an ambiguous timeout, refuse
 * further commands until rebind: a late completion must not satisfy a new one.
 */
static int pd_command(struct s10_pd *pd, u8 command, const u8 *payload,
		      u8 length, u8 *response, u8 response_len)
{
	u8 packet[33], irq;
	unsigned long deadline;
	int ret;

	lockdep_assert_held(&pd->lock);
	if (pd->mailbox_failed)
		return -EIO;
	if (length > 31 || response_len > 32)
		return -EINVAL;
	ret = pd_read(pd, REG_UIC_INT, &irq, 1);
	if (ret)
		return ret;
	packet[0] = REG_WRITE;
	packet[1] = command;
	if (length)
		memcpy(packet + 2, payload, length);
	ret = i2c_master_send(pd->client, packet, length + 2);
	if (ret != length + 2)
		goto fail;
	packet[0] = REG_END;
	packet[1] = 0;
	ret = i2c_master_send(pd->client, packet, 2);
	if (ret != 2)
		goto fail;
	deadline = jiffies + msecs_to_jiffies(3000);
	do {
		usleep_range(10000, 15000);
		ret = pd_read(pd, REG_UIC_INT, &irq, 1);
		if (ret)
			goto fail;
		if (irq & BIT(7)) {
			ret = pd_read(pd, REG_RESPONSE, response, response_len);
			if (ret)
				goto fail;
			if (response[0] != command) {
				ret = -EPROTO;
				goto fail;
			}
			return 0;
		}
	} while (time_before(jiffies, deadline));
	ret = -ETIMEDOUT;
fail:
	pd->mailbox_failed = true;
	return ret < 0 ? ret : -EIO;
}

static ssize_t controller_status_show(struct device *dev,
				     struct device_attribute *attr, char *buf)
{
	struct s10_pd *pd = dev_get_drvdata(dev);
	u8 regs[8], hw, fw;
	int ret;

	mutex_lock(&pd->lock);
	ret = pd_read(pd, 0, &hw, 1);
	if (!ret)
		ret = pd_read(pd, 1, &fw, 1);
	if (!ret)
		ret = pd_read(pd, REG_STATUS, regs, sizeof(regs));
	mutex_unlock(&pd->lock);
	if (ret)
		return ret;
	return sysfs_emit(buf,
		"hw_rev=0x%02x fw_rev=0x%02x.%02x usbc_status1=0x%02x sysmsg=0x%02x bc_status=0x%02x cc_status0=0x%02x cc_status1=0x%02x pd_status0=0x%02x pd_status1=0x%02x vbus_present=%u vbadc_code=%u cc_state=%u rp_level=%u psrdy=%u mailbox_failed=%u\n",
		hw, fw, regs[3], regs[0], regs[1], regs[2], regs[4], regs[5],
		regs[6], regs[7], !!(regs[2] & BIT(7)), regs[0] >> 4,
		regs[4] & 7, (regs[4] >> 4) & 3, !!(regs[7] & BIT(4)),
		READ_ONCE(pd->mailbox_failed));
}
static DEVICE_ATTR_RO(controller_status);

static ssize_t source_capabilities_show(struct device *dev,
				       struct device_attribute *attr, char *buf)
{
	struct s10_pd *pd = dev_get_drvdata(dev);
	u8 status[8], response[32];
	unsigned int n, selected, i;
	ssize_t count = 0;
	int ret;

	mutex_lock(&pd->lock);
	ret = pd_read(pd, REG_STATUS, status, sizeof(status));
	/* Query only while acting as an attached sink with a ready PD contract.
	 * Reading this attribute never requests a different voltage or profile.
	 */
	if (!ret && (!(status[2] & BIT(7)) || (status[4] & 7) != 1 ||
		     !(status[7] & BIT(4))))
		ret = -ENODATA;
	if (!ret)
		ret = pd_command(pd, CMD_CURRENT_CAPS, NULL, 0,
				 response, sizeof(response));
	if (!ret) {
		n = response[1] & 7;
		selected = (response[1] >> 3) & 7;
		if (!n || selected > n)
			ret = -EPROTO;
		else {
			count = sysfs_emit(buf, "count=%u selected=%u\n", n, selected);
			for (i = 0; i < n; i++) {
				u32 pdo = get_unaligned_le32(response + 2 + i * 4);
				u32 type = pdo >> 30;
				count += sysfs_emit_at(buf, count,
					"pdo%u raw=0x%08x type=%u fixed_mv=%u fixed_ma=%u\n",
					i + 1, pdo, type,
					type ? 0 : ((pdo >> 10) & 0x3ff) * 50,
					type ? 0 : (pdo & 0x3ff) * 10);
			}
		}
	}
	mutex_unlock(&pd->lock);
	return ret ? ret : count;
}
static DEVICE_ATTR_RO(source_capabilities);

static int pd_check_battery(void)
{
	struct power_supply *battery;
	union power_supply_propval temp, health, capacity;
	int ret;

	battery = power_supply_get_by_name("max170xx_battery");
	if (!battery)
		return -ENODEV;
	ret = power_supply_get_property(battery, POWER_SUPPLY_PROP_TEMP, &temp);
	if (!ret)
		ret = power_supply_get_property(battery, POWER_SUPPLY_PROP_HEALTH, &health);
	if (!ret)
		ret = power_supply_get_property(battery, POWER_SUPPLY_PROP_CAPACITY, &capacity);
	if (!ret && (temp.intval < 100 || temp.intval >= 400 ||
		     health.intval != POWER_SUPPLY_HEALTH_GOOD || capacity.intval >= 85))
		ret = -EPERM;
	power_supply_put(battery);
	return ret;
}

static ssize_t request_fixed_voltage_mv_store(struct device *dev,
					     struct device_attribute *attr,
					     const char *buf, size_t count)
{
	struct s10_pd *pd = dev_get_drvdata(dev);
	u8 status[8], caps[32], result[2], position = 0;
	unsigned int mv, n, i, selected;
	int ret, attempt;

	ret = kstrtouint(buf, 10, &mv);
	if (ret)
		return ret;
	if (mv != 5000 && mv != 9000)
		return -EINVAL;
	/* These are conservative local test conditions, not a complete JEITA
	 * charging policy. Lowering back to 5 V remains possible when warm.
	 */
	if (mv == 9000) {
		ret = pd_check_battery();
		if (ret)
			return ret;
	}
	mutex_lock(&pd->lock);
	ret = pd_read(pd, REG_STATUS, status, sizeof(status));
	if (ret)
		goto out;
	if (!(status[2] & BIT(7)) || (status[4] & 7) != 1 ||
	    !(status[7] & BIT(4)) || (status[5] & 0xc0)) {
		ret = -ENOTCONN;
		goto out;
	}
	ret = pd_command(pd, CMD_CURRENT_CAPS, NULL, 0, caps, sizeof(caps));
	if (ret)
		goto out;
	n = caps[1] & 7;
	selected = (caps[1] >> 3) & 7;
	if (!n || !selected || selected > n) {
		ret = -EPROTO;
		goto out;
	}
	for (i = 0; i < n; i++) {
		u32 pdo = get_unaligned_le32(caps + 2 + i * 4);

		if (!(pdo >> 30) && ((pdo >> 10) & 0x3ff) * 50 == mv &&
		    (pdo & 0x3ff) * 10 >= 1000) {
			position = i + 1;
			break;
		}
	}
	if (!position) {
		ret = -EOPNOTSUPP;
		goto out;
	}
	if (selected != position) {
		ret = pd_command(pd, CMD_REQUEST_CAP, &position, 1, result, sizeof(result));
		if (ret)
			goto out;
		if (result[1]) {
			ret = -EREMOTEIO;
			goto out;
		}
	}
	ret = -ETIMEDOUT;
	for (attempt = 0; attempt < 100; attempt++) {
		msleep(100);
		ret = pd_read(pd, REG_STATUS, status, sizeof(status));
		if (ret)
			goto out;
		if (!(status[2] & BIT(7)) || (status[4] & 7) != 1) {
			ret = -ENOTCONN;
			goto out;
		}
		if (!(status[7] & BIT(4)) || status[0] >> 4 != (mv == 9000 ? 6 : 2))
			continue;
		ret = pd_command(pd, CMD_CURRENT_CAPS, NULL, 0, caps, sizeof(caps));
		if (ret)
			goto out;
		if (((caps[1] >> 3) & 7) == position) {
			dev_info(dev, "fixed PDO %u selected, %u mV confirmed by VBADC\n",
				 position, mv);
			ret = 0;
			goto out;
		}
	}
	ret = -ETIMEDOUT;
out:
	mutex_unlock(&pd->lock);
	return ret ? ret : count;
}
static DEVICE_ATTR_WO(request_fixed_voltage_mv);

/* Observe the firmware-maintained roles. No Linux role-switch callback is
 * offered: changing them needs additional policy and gadget integration.
 */
static void pd_status_work(struct work_struct *work)
{
	struct s10_pd *pd = container_of(to_delayed_work(work), struct s10_pd, status_work);
	struct typec_partner_desc desc = {};
	u8 status[8];
	bool attached, is_pd;
	unsigned int cc, rp, orientation;
	int ret;

	mutex_lock(&pd->lock);
	ret = pd_read(pd, REG_STATUS, status, sizeof(status));
	mutex_unlock(&pd->lock);
	if (ret)
		goto again;
	cc = status[4] & 7;
	rp = (status[4] >> 4) & 3;
	orientation = status[4] >> 6;
	attached = cc == 1 || cc == 2;
	is_pd = attached && (status[7] & BIT(4));
	if (pd->partner && (!attached || is_pd != pd->partner_pd)) {
		typec_unregister_partner(pd->partner);
		pd->partner = NULL;
	}
	if (attached) {
		typec_set_pwr_role(pd->port, cc == 1 ? TYPEC_SINK : TYPEC_SOURCE);
		typec_set_data_role(pd->port, status[7] & BIT(7) ? TYPEC_HOST : TYPEC_DEVICE);
		typec_set_orientation(pd->port, orientation == 1 ? TYPEC_ORIENTATION_NORMAL :
				      orientation == 2 ? TYPEC_ORIENTATION_REVERSE : TYPEC_ORIENTATION_NONE);
		typec_set_pwr_opmode(pd->port, is_pd ? TYPEC_PWR_MODE_PD :
				    rp == 3 ? TYPEC_PWR_MODE_3_0A :
				    rp == 2 ? TYPEC_PWR_MODE_1_5A : TYPEC_PWR_MODE_USB);
		if (!pd->partner) {
			desc.usb_pd = is_pd;
			desc.accessory = TYPEC_ACCESSORY_NONE;
			pd->partner = typec_register_partner(pd->port, &desc);
			if (IS_ERR(pd->partner))
				pd->partner = NULL;
			else
				pd->partner_pd = is_pd;
		}
	} else {
		typec_set_orientation(pd->port, TYPEC_ORIENTATION_NONE);
		typec_set_pwr_opmode(pd->port, TYPEC_PWR_MODE_USB);
	}
again:
	schedule_delayed_work(&pd->status_work, msecs_to_jiffies(2000));
}

static struct attribute *s10_pd_attrs[] = {
	&dev_attr_controller_status.attr,
	&dev_attr_source_capabilities.attr,
	&dev_attr_request_fixed_voltage_mv.attr,
	NULL,
};
ATTRIBUTE_GROUPS(s10_pd);

static int s10_pd_probe(struct i2c_client *client)
{
	struct s10_pd *pd;
	struct typec_capability cap = {
		.type = TYPEC_PORT_DRP,
		.data = TYPEC_PORT_DRD,
		.revision = USB_TYPEC_REV_1_2,
		.prefer_role = TYPEC_NO_PREFERRED_ROLE,
	};
	u8 hw, fw;
	int ret;

	if (client->addr != 0x25 ||
	    !i2c_check_functionality(client->adapter, I2C_FUNC_I2C))
		return -ENODEV;
	pd = devm_kzalloc(&client->dev, sizeof(*pd), GFP_KERNEL);
	if (!pd)
		return -ENOMEM;
	pd->client = client;
	mutex_init(&pd->lock);
	i2c_set_clientdata(client, pd);
	ret = pd_read(pd, 0, &hw, 1);
	if (!ret)
		ret = pd_read(pd, 1, &fw, 1);
	if (ret)
		return dev_err_probe(&client->dev, ret, "controller not readable\n");
	if (hw == 0xff || fw == 0xff)
		return -ENODEV;
	pd->port = typec_register_port(&client->dev, &cap);
	if (IS_ERR(pd->port))
		return PTR_ERR(pd->port);
	INIT_DELAYED_WORK(&pd->status_work, pd_status_work);
	schedule_delayed_work(&pd->status_work, 0);
	dev_info(&client->dev, "PD diagnostic transport ready, hw=%02x fw=%02x; no voltage/current changes\n", hw, fw);
	return 0;
}

static void s10_pd_remove(struct i2c_client *client)
{
	struct s10_pd *pd = i2c_get_clientdata(client);

	cancel_delayed_work_sync(&pd->status_work);
	if (pd->partner)
		typec_unregister_partner(pd->partner);
	typec_unregister_port(pd->port);
}

static const struct i2c_device_id s10_pd_ids[] = {
	{ "s10-max77705-pd", 0 }, { }
};
MODULE_DEVICE_TABLE(i2c, s10_pd_ids);

static struct i2c_driver s10_pd_driver = {
	.driver = {
		.name = "s10-max77705-pd",
		.dev_groups = s10_pd_groups,
	},
	.probe = s10_pd_probe,
	.remove = s10_pd_remove,
	.id_table = s10_pd_ids,
};
module_i2c_driver(s10_pd_driver);
MODULE_LICENSE("GPL");
MODULE_DESCRIPTION("Galaxy S10 MAX77705 firmware PD diagnostic transport");
