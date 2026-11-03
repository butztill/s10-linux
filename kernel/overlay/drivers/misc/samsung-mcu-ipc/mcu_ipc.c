// SPDX-License-Identifier: GPL-2.0-or-later
/*
 * Copyright (C) 2014 Samsung Electronics Co., Ltd.
 *
 * Samsung Exynos 9820 MCU_IPC mailbox provider.
 *
 * This preserves the legacy API expected by Samsung's S5000AP/SIPC modem
 * stack. It is deliberately separate from the generic Exynos mailbox
 * controller: the S10 CP mailbox has a different register block and wire
 * protocol.
 */

#include <linux/bitops.h>
#include <linux/delay.h>
#include <linux/device.h>
#include <linux/interrupt.h>
#include <linux/io.h>
#include <linux/module.h>
#include <linux/mutex.h>
#include <linux/of.h>
#include <linux/platform_device.h>
#include <linux/spinlock.h>

#include <linux/mcu_ipc.h>

#include "regs-mcu-ipc.h"

#define MCU_IPC_IRQS		16
#define MCU_IPC_RX_IRQ_MASK	GENMASK(31, 16)

struct mcu_ipc_handler {
	void *data;
	void (*handler)(void *arg);
};

struct mcu_ipc_instance {
	struct device *dev;
	void __iomem *base;
	int irq;
	enum mcu_ipc_region id;
	unsigned long unmasked_irq;
	u32 registered_irq;
	struct mcu_ipc_handler handlers[MCU_IPC_IRQS];
	/* Protects atomic updates to the mailbox value registers. */
	spinlock_t value_lock;
	/* Protects interrupt state and handler registration. */
	spinlock_t irq_lock;
};

static struct mcu_ipc_instance mcu_ipc[MCU_MAX];
static DEFINE_MUTEX(mcu_ipc_probe_lock);

static bool mcu_ipc_valid_id(enum mcu_ipc_region id)
{
	return id >= MCU_CP && id < MCU_MAX;
}

static struct mcu_ipc_instance *mcu_ipc_get(enum mcu_ipc_region id)
{
	struct mcu_ipc_instance *ipc;

	if (!mcu_ipc_valid_id(id))
		return NULL;

	ipc = &mcu_ipc[id];
	if (!READ_ONCE(ipc->dev) || !READ_ONCE(ipc->base))
		return NULL;

	return ipc;
}

static inline u32 mcu_ipc_readl(struct mcu_ipc_instance *ipc, u32 reg)
{
	return readl(ipc->base + reg);
}

static inline void mcu_ipc_writel(struct mcu_ipc_instance *ipc,
				  u32 value, u32 reg)
{
	writel(value, ipc->base + reg);
}

void mcu_ipc_reg_dump(enum mcu_ipc_region id)
{
	struct mcu_ipc_instance *ipc = mcu_ipc_get(id);
	unsigned long flags;
	u32 values[4];
	unsigned int i;

	if (!ipc)
		return;

	spin_lock_irqsave(&ipc->irq_lock, flags);
	for (i = 0; i < 4; i++)
		values[i] = mcu_ipc_readl(ipc, MCU_IPC_ISSR0 + 4 * i);
	spin_unlock_irqrestore(&ipc->irq_lock, flags);

	for (i = 0; i < 4; i++)
		dev_info(ipc->dev, "ISSR%u: %#08x\n", i, values[i]);
}
EXPORT_SYMBOL_GPL(mcu_ipc_reg_dump);

static irqreturn_t mcu_ipc_irq_handler(int irq, void *data)
{
	struct mcu_ipc_instance *ipc = data;
	void (*callbacks[MCU_IPC_IRQS])(void *);
	void *callback_data[MCU_IPC_IRQS];
	unsigned long flags;
	u32 pending;
	unsigned int i;

	spin_lock_irqsave(&ipc->irq_lock, flags);
	pending = mcu_ipc_readl(ipc, MCU_IPC_INTSR0) & MCU_IPC_RX_IRQ_MASK;
	pending &= (u32)ipc->unmasked_irq << 16;
	if (!pending) {
		spin_unlock_irqrestore(&ipc->irq_lock, flags);
		return IRQ_NONE;
	}

	/* INTCR0 is write-one-to-clear for AP receive interrupts. */
	mcu_ipc_writel(ipc, pending, MCU_IPC_INTCR0);
	for (i = 0; i < MCU_IPC_IRQS; i++) {
		if (!(pending & BIT(i + 16)))
			continue;
		callbacks[i] = ipc->handlers[i].handler;
		callback_data[i] = ipc->handlers[i].data;
	}
	spin_unlock_irqrestore(&ipc->irq_lock, flags);

	for (i = 0; i < MCU_IPC_IRQS; i++) {
		if (pending & BIT(i + 16)) {
			if (callbacks[i])
				callbacks[i](callback_data[i]);
			else
				dev_warn_ratelimited(ipc->dev, "unregistered mailbox IRQ %u\n", i);
		}
	}

	return IRQ_HANDLED;
}

int mbox_request_irq(enum mcu_ipc_region id, u32 int_num,
		     void (*handler)(void *), void *data)
{
	struct mcu_ipc_instance *ipc = mcu_ipc_get(id);
	unsigned long flags;

	if (!ipc || !handler || int_num >= MCU_IPC_IRQS)
		return -EINVAL;

	spin_lock_irqsave(&ipc->irq_lock, flags);
	if (ipc->handlers[int_num].handler) {
		spin_unlock_irqrestore(&ipc->irq_lock, flags);
		return -EBUSY;
	}
	ipc->handlers[int_num].data = data;
	ipc->handlers[int_num].handler = handler;
	ipc->registered_irq |= BIT(int_num + 16);
	set_bit(int_num, &ipc->unmasked_irq);
	spin_unlock_irqrestore(&ipc->irq_lock, flags);

	return 0;
}
EXPORT_SYMBOL_GPL(mbox_request_irq);

int mbox_enable_irq(enum mcu_ipc_region id, u32 int_num)
{
	struct mcu_ipc_instance *ipc = mcu_ipc_get(id);
	unsigned long flags;
	u32 mask;
	u32 bit;

	if (!ipc || int_num >= MCU_IPC_IRQS)
		return -EINVAL;
	bit = BIT(int_num + 16);

	spin_lock_irqsave(&ipc->irq_lock, flags);
	if (!(ipc->registered_irq & bit)) {
		spin_unlock_irqrestore(&ipc->irq_lock, flags);
		return -EINVAL;
	}

	mask = mcu_ipc_readl(ipc, MCU_IPC_INTMR0);
	if (mask & bit)
		mcu_ipc_writel(ipc, mask & ~bit, MCU_IPC_INTMR0);
	set_bit(int_num, &ipc->unmasked_irq);
	spin_unlock_irqrestore(&ipc->irq_lock, flags);

	return 0;
}
EXPORT_SYMBOL_GPL(mbox_enable_irq);

int mbox_check_irq(enum mcu_ipc_region id, u32 int_num)
{
	struct mcu_ipc_instance *ipc = mcu_ipc_get(id);
	unsigned long flags;
	u32 bit;
	bool pending;

	if (!ipc || int_num >= MCU_IPC_IRQS)
		return -EINVAL;
	bit = BIT(int_num + 16);

	spin_lock_irqsave(&ipc->irq_lock, flags);
	if (!(ipc->registered_irq & bit) ||
	    test_bit(int_num, &ipc->unmasked_irq)) {
		spin_unlock_irqrestore(&ipc->irq_lock, flags);
		return -EINVAL;
	}

	pending = !!(mcu_ipc_readl(ipc, MCU_IPC_INTSR0) & bit);
	if (pending)
		mcu_ipc_writel(ipc, bit, MCU_IPC_INTCR0);
	spin_unlock_irqrestore(&ipc->irq_lock, flags);

	return pending;
}
EXPORT_SYMBOL_GPL(mbox_check_irq);

int mbox_disable_irq(enum mcu_ipc_region id, u32 int_num)
{
	struct mcu_ipc_instance *ipc = mcu_ipc_get(id);
	unsigned long flags;
	u32 bit;
	u32 mask;

	if (!ipc || int_num >= MCU_IPC_IRQS)
		return -EINVAL;
	bit = BIT(int_num + 16);

	spin_lock_irqsave(&ipc->irq_lock, flags);
	if (!(ipc->registered_irq & bit)) {
		spin_unlock_irqrestore(&ipc->irq_lock, flags);
		return -EINVAL;
	}

	mask = mcu_ipc_readl(ipc, MCU_IPC_INTMR0);
	if (!(mask & bit)) {
		mcu_ipc_writel(ipc, mask | bit, MCU_IPC_INTMR0);
		udelay(5);
		mcu_ipc_writel(ipc, BIT(int_num + 16), MCU_IPC_INTGR0);
		udelay(5);
	}
	clear_bit(int_num, &ipc->unmasked_irq);
	spin_unlock_irqrestore(&ipc->irq_lock, flags);

	return 0;
}
EXPORT_SYMBOL_GPL(mbox_disable_irq);

int mcu_ipc_unregister_handler(enum mcu_ipc_region id, u32 int_num,
			       void (*handler)(void *))
{
	struct mcu_ipc_instance *ipc = mcu_ipc_get(id);
	unsigned long flags;

	if (!ipc || !handler || int_num >= MCU_IPC_IRQS)
		return -EINVAL;

	spin_lock_irqsave(&ipc->irq_lock, flags);
	if (ipc->handlers[int_num].handler != handler) {
		spin_unlock_irqrestore(&ipc->irq_lock, flags);
		return -EINVAL;
	}
	ipc->handlers[int_num].data = NULL;
	ipc->handlers[int_num].handler = NULL;
	ipc->registered_irq &= ~BIT(int_num + 16);
	clear_bit(int_num, &ipc->unmasked_irq);
	spin_unlock_irqrestore(&ipc->irq_lock, flags);

	return 0;
}
EXPORT_SYMBOL_GPL(mcu_ipc_unregister_handler);

void mbox_set_interrupt(enum mcu_ipc_region id, u32 int_num)
{
	struct mcu_ipc_instance *ipc = mcu_ipc_get(id);

	if (ipc && int_num < MCU_IPC_IRQS)
		mcu_ipc_writel(ipc, BIT(int_num), MCU_IPC_INTGR1);
}
EXPORT_SYMBOL_GPL(mbox_set_interrupt);

void mcu_ipc_send_command(enum mcu_ipc_region id, u32 int_num, u16 cmd)
{
	struct mcu_ipc_instance *ipc = mcu_ipc_get(id);

	if (!ipc || int_num >= MCU_IPC_IRQS)
		return;

	mcu_ipc_writel(ipc, cmd, MCU_IPC_ISSR0 + 8 * int_num);
	mbox_set_interrupt(id, int_num);
}
EXPORT_SYMBOL_GPL(mcu_ipc_send_command);

u32 mbox_get_value(enum mcu_ipc_region id, u32 mbx_num)
{
	struct mcu_ipc_instance *ipc = mcu_ipc_get(id);

	if (!ipc || mbx_num >= MAX_MBOX_NUM)
		return 0;

	return mcu_ipc_readl(ipc, MCU_IPC_ISSR0 + 4 * mbx_num);
}
EXPORT_SYMBOL_GPL(mbox_get_value);

void mbox_set_value(enum mcu_ipc_region id, u32 mbx_num, u32 msg)
{
	struct mcu_ipc_instance *ipc = mcu_ipc_get(id);

	if (ipc && mbx_num < MAX_MBOX_NUM)
		mcu_ipc_writel(ipc, msg, MCU_IPC_ISSR0 + 4 * mbx_num);
}
EXPORT_SYMBOL_GPL(mbox_set_value);

void mbox_update_value(enum mcu_ipc_region id, u32 mbx_num,
		       u32 msg, u32 mask, u32 pos)
{
	struct mcu_ipc_instance *ipc = mcu_ipc_get(id);
	unsigned long flags;
	u32 value;

	if (!ipc || mbx_num >= MAX_MBOX_NUM || pos >= 32)
		return;

	spin_lock_irqsave(&ipc->value_lock, flags);
	value = mbox_get_value(id, mbx_num);
	value &= ~(mask << pos);
	value |= (msg & mask) << pos;
	mbox_set_value(id, mbx_num, value);
	spin_unlock_irqrestore(&ipc->value_lock, flags);
}
EXPORT_SYMBOL_GPL(mbox_update_value);

u32 mbox_extract_value(enum mcu_ipc_region id, u32 mbx_num,
		       u32 mask, u32 pos)
{
	if (pos >= 32)
		return 0;

	return (mbox_get_value(id, mbx_num) >> pos) & mask;
}
EXPORT_SYMBOL_GPL(mbox_extract_value);

void mbox_sw_reset(enum mcu_ipc_region id)
{
	struct mcu_ipc_instance *ipc = mcu_ipc_get(id);
	u32 value;

	if (!ipc)
		return;

	dev_warn(ipc->dev, "mailbox software reset requested\n");
	value = mcu_ipc_readl(ipc, MCU_IPC_MCUCTLR);
	value |= BIT(MCU_IPC_MCUCTLR_MSWRST);
	mcu_ipc_writel(ipc, value, MCU_IPC_MCUCTLR);
	udelay(5);
}
EXPORT_SYMBOL_GPL(mbox_sw_reset);

static int mcu_ipc_probe(struct platform_device *pdev)
{
	struct device *dev = &pdev->dev;
	struct mcu_ipc_instance *ipc;
	struct resource *res;
	const char *name = "mcu_ipc";
	u32 id;
	int ret;

	ret = of_property_read_u32(dev->of_node, "mcu,id", &id);
	if (ret)
		return dev_err_probe(dev, ret, "missing mcu,id\n");
	if (id >= MCU_MAX)
		return dev_err_probe(dev, -EINVAL, "invalid mcu,id %u\n", id);

	if (!of_property_read_string(dev->of_node, "mcu,name", &name))
		dev_dbg(dev, "mailbox instance %s\n", name);

	mutex_lock(&mcu_ipc_probe_lock);
	ipc = &mcu_ipc[id];
	if (ipc->dev) {
		ret = -EBUSY;
		goto unlock;
	}

	res = platform_get_resource(pdev, IORESOURCE_MEM, 0);
	if (!res) {
		ret = -ENODEV;
		goto unlock;
	}
	ipc->base = devm_ioremap_resource(dev, res);
	if (IS_ERR(ipc->base)) {
		ret = PTR_ERR(ipc->base);
		ipc->base = NULL;
		goto unlock;
	}

	ipc->dev = dev;
	ipc->id = id;
	spin_lock_init(&ipc->value_lock);
	spin_lock_init(&ipc->irq_lock);
	ipc->registered_irq = 0;
	ipc->unmasked_irq = 0;
	memset(ipc->handlers, 0, sizeof(ipc->handlers));

	ipc->irq = platform_get_irq(pdev, 0);
	if (ipc->irq < 0) {
		ret = ipc->irq;
		goto clear_state;
	}

	ret = devm_request_irq(dev, ipc->irq, mcu_ipc_irq_handler, 0,
			       dev_name(dev), ipc);
	if (ret)
		goto clear_state;

	/*
	 * Match the Samsung driver: clear AP-generated pending interrupts
	 * after the shared IRQ has been acquired. This does not reset the CP
	 * or modify CP-to-AP status or shared-memory registers.
	 */
	mcu_ipc_writel(ipc, GENMASK(15, 0), MCU_IPC_INTCR1);

	platform_set_drvdata(pdev, ipc);
	dev_info(dev, "Samsung MCU_IPC region %u registered on IRQ %d\n",
		 id, ipc->irq);
	mutex_unlock(&mcu_ipc_probe_lock);
	return 0;

clear_state:
	ipc->dev = NULL;
	ipc->base = NULL;
unlock:
	mutex_unlock(&mcu_ipc_probe_lock);
	return dev_err_probe(dev, ret, "failed to initialize MCU_IPC\n");
}

static void mcu_ipc_remove(struct platform_device *pdev)
{
	struct mcu_ipc_instance *ipc = platform_get_drvdata(pdev);
	unsigned long flags;
	u32 mask;

	if (!ipc)
		return;

	/* Stop IRQ delivery before devres releases the mapping and IRQ. */
	spin_lock_irqsave(&ipc->irq_lock, flags);
	mask = mcu_ipc_readl(ipc, MCU_IPC_INTMR0);
	mcu_ipc_writel(ipc, mask | MCU_IPC_RX_IRQ_MASK, MCU_IPC_INTMR0);
	ipc->registered_irq = 0;
	ipc->unmasked_irq = 0;
	memset(ipc->handlers, 0, sizeof(ipc->handlers));
	spin_unlock_irqrestore(&ipc->irq_lock, flags);
	devm_free_irq(&pdev->dev, ipc->irq, ipc);

	mutex_lock(&mcu_ipc_probe_lock);
	WRITE_ONCE(ipc->base, NULL);
	WRITE_ONCE(ipc->dev, NULL);
	mutex_unlock(&mcu_ipc_probe_lock);
}

static const struct of_device_id mcu_ipc_of_match[] = {
	{ .compatible = "samsung,exynos-shd-ipc-mailbox" },
	{ }
};
MODULE_DEVICE_TABLE(of, mcu_ipc_of_match);

static struct platform_driver mcu_ipc_driver = {
	.probe = mcu_ipc_probe,
	.remove = mcu_ipc_remove,
	.driver = {
		.name = "samsung-exynos9820-mcu-ipc",
		.of_match_table = mcu_ipc_of_match,
		.suppress_bind_attrs = true,
	},
};
module_platform_driver(mcu_ipc_driver);

MODULE_DESCRIPTION("Samsung Exynos 9820 CP MCU_IPC mailbox provider");
MODULE_AUTHOR("Samsung Electronics; ported for the Exynos 9820 S10");
MODULE_LICENSE("GPL");
