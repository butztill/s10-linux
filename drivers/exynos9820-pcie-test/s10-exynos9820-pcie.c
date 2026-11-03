// SPDX-License-Identifier: GPL-2.0-only
/* Exynos9820 PCIe0 bring-up, based on Samsung's GPL host-v0/calibration
 * driver and the mainline Exynos DesignWare glue. Initial use is enumeration
 * only: no modem, no S2MPU/HVC grants, no raw /dev/mem access.
 */
#include <linux/clk.h>
#include <linux/delay.h>
#include <linux/gpio/consumer.h>
#include <linux/interrupt.h>
#include <linux/mfd/syscon.h>
#include <linux/module.h>
#include <linux/of.h>
#include <linux/pci.h>
#include <linux/platform_device.h>
#include <linux/regmap.h>
#include "pcie-designware.h"

struct s10_pcie {
	struct dw_pcie pci;
	void __iomem *phy, *pcs;
	struct regmap *pmu, *sysreg;
	struct clk_bulk_data *clks;
	struct gpio_desc *reset, *enable;
	bool irq_requested;
};

static struct s10_pcie *to_s10(struct dw_pcie *pci)
{
	return dev_get_drvdata(pci->dev);
}

/* Samsung Exynos9820 PCIe channel-0 calibration tables are inserted below. */
static const u32 s10_cmn[] = {
	0x01, 0xE1, 0x05, 0x00, 0x88, 0x88, 0x88, 0x0C,
	0x61, 0x45, 0x65, 0x24, 0x33, 0x18, 0xE3, 0xFC,
	0xD8, 0x05, 0xE6, 0x80, 0x00, 0x00, 0x00, 0x00,
	0x60, 0x11, 0x00, 0xA0, 0x05, 0x04, 0x18, 0x88,
	0xC0, 0xFF, 0x9B, 0x52, 0x22, 0x30, 0x4F, 0xDC,
	0x40, 0x00, 0x00, 0x00, 0x00, 0xFF, 0x00, 0x80,
};

static const u32 s10_trsv[] = {
	0x31, 0x40, 0x37, 0x99, 0x85, 0x00, 0xC0, 0xFF,
	0xFF, 0x3F, 0x8C, 0xC8, 0x02, 0x01, 0x88, 0x80,
	0x06, 0x90, 0x6C, 0x66, 0x09, 0x61, 0x42, 0x44,
	0xC6, 0x50, 0x0A, 0x33, 0x58, 0xE7, 0x20, 0x22,
	0x80, 0x38, 0x05, 0x85, 0x00, 0x00, 0x00, 0x7E,
	0x00, 0x00, 0x55, 0x15, 0xAC, 0xAA, 0x3E, 0x00,
	0x00, 0x00, 0x20, 0x3F, 0x00, 0x03, 0x01, 0x00,
	0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF,
	0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x40,
	0x05, 0x85, 0xFF, 0x00, 0x00, 0x00, 0x00, 0x00,
};


static void s10_toggle(void __iomem *base, unsigned int reg)
{
	writel(1, base + reg);
	udelay(10);
	writel(0, base + reg);
	udelay(10);
	writel(1, base + reg);
	udelay(10);
}

static int s10_phy_init(struct s10_pcie *ep)
{
	void __iomem *elbi = ep->pci.elbi_base;
	u32 val;
	int ret, i;

	ret = regmap_update_bits(ep->pmu, 0x71c, BIT(0), BIT(0));
	if (ret)
		return ret;
	/* Samsung's sysreg window starts at 0x13c11044, inside this syscon. */
	ret = regmap_update_bits(ep->sysreg, 0x1044, BIT(1), BIT(1));
	if (ret)
		return ret;
	ret = regmap_update_bits(ep->sysreg, 0x1050,
				 GENMASK(7, 1) | BIT(12), (3 << 2) | BIT(12));
	if (ret)
		return ret;
	s10_toggle(elbi, 0x288);
	for (i = 0; i < ARRAY_SIZE(s10_cmn); i++)
		writel(s10_cmn[i], ep->phy + i * 4);
	for (i = 0; i < ARRAY_SIZE(s10_trsv); i++)
		writel(s10_trsv[i], ep->phy + (0x30 + i) * 4);
	writel(0x70, ep->pcs + 0xf8);
	writel(0x87, ep->pcs + 0x100);
	writel(0x50, ep->pcs + 0x104);
	val = readl(ep->pcs + 0x0c);
	writel((val & ~BIT(1)) | BIT(4), ep->pcs + 0x0c);
	s10_toggle(elbi, 0x290);
	s10_toggle(elbi, 0x28c);
	udelay(100);
	val = readl(ep->pcs + 0xd0);
	writel(val | (3 << 6), ep->pcs + 0xd0);
	udelay(20);
	val = (val & ~(3 << 6)) | (2 << 6);
	writel(val, ep->pcs + 0xd0);
	writel(val & ~(3 << 6), ep->pcs + 0xd0);
	return 0;
}

static u32 s10_read_dbi(struct dw_pcie *pci, void __iomem *base, u32 reg, size_t size)
{
	u32 mode, val;

	mode = readl(pci->elbi_base + 0x120);
	writel(mode | BIT(21), pci->elbi_base + 0x120);
	dw_pcie_read(base + reg, size, &val);
	writel(mode & ~BIT(21), pci->elbi_base + 0x120);
	return val;
}

static void s10_write_dbi(struct dw_pcie *pci, void __iomem *base,
			  u32 reg, size_t size, u32 val)
{
	u32 mode = readl(pci->elbi_base + 0x11c);

	writel(mode | BIT(21), pci->elbi_base + 0x11c);
	dw_pcie_write(base + reg, size, val);
	writel(mode & ~BIT(21), pci->elbi_base + 0x11c);
}

static bool s10_link_up(struct dw_pcie *pci)
{
	u32 state = readl(pci->elbi_base + 0x74) & 0x1f;

	return state >= 0x0d && state <= 0x14;
}

static int s10_start_link(struct dw_pcie *pci)
{
	struct s10_pcie *ep = to_s10(pci);
	u32 val;

	gpiod_set_value_cansleep(ep->reset, 0);
	usleep_range(18000, 20000);
	/* Stop ignoring receiver electrical idle once DBI setup is complete. */
	val = readl(ep->pcs + 0xec);
	writel(val & ~BIT(3), ep->pcs + 0xec);
	writel(1, pci->elbi_base + 0x2c);
	return 0;
}

static void s10_stop_link(struct dw_pcie *pci)
{
	struct s10_pcie *ep = to_s10(pci);

	writel(0, pci->elbi_base + 0x2c);
	gpiod_set_value_cansleep(ep->reset, 1);
}

static const struct dw_pcie_ops s10_dw_ops = {
	.read_dbi = s10_read_dbi,
	.write_dbi = s10_write_dbi,
	.link_up = s10_link_up,
	.start_link = s10_start_link,
	.stop_link = s10_stop_link,
};

static int s10_own_read(struct pci_bus *bus, unsigned int devfn,
			int where, int size, u32 *val)
{
	struct dw_pcie *pci = to_dw_pcie_from_pp(bus->sysdata);

	if (PCI_SLOT(devfn))
		return PCIBIOS_DEVICE_NOT_FOUND;
	*val = dw_pcie_read_dbi(pci, where, size);
	return PCIBIOS_SUCCESSFUL;
}

static int s10_own_write(struct pci_bus *bus, unsigned int devfn,
			 int where, int size, u32 val)
{
	struct dw_pcie *pci = to_dw_pcie_from_pp(bus->sysdata);

	if (PCI_SLOT(devfn))
		return PCIBIOS_DEVICE_NOT_FOUND;
	dw_pcie_write_dbi(pci, where, size, val);
	return PCIBIOS_SUCCESSFUL;
}

static struct pci_ops s10_pci_ops = {
	.read = s10_own_read, .write = s10_own_write,
};

static irqreturn_t s10_irq(int irq, void *data)
{
	struct s10_pcie *ep = data;
	u32 pending = readl(ep->pci.elbi_base);

	if (!pending)
		return IRQ_NONE;
	writel(pending, ep->pci.elbi_base);
	return IRQ_HANDLED;
}

static int s10_host_init(struct dw_pcie_rp *pp)
{
	struct dw_pcie *pci = to_dw_pcie_from_pp(pp);
	struct s10_pcie *ep = to_s10(pci);
	int ret;

	pp->bridge->ops = &s10_pci_ops;
	writel(0, pci->elbi_base + 0x0c);
	writel(0, pci->elbi_base + 0x10);
	writel(0, pci->elbi_base + 0x14);
	writel(readl(pci->elbi_base + 0x2c8) & ~GENMASK(11, 0), pci->elbi_base + 0x2c8);
	writel(readl(pci->elbi_base + 0xf4) | BIT(0) | BIT(4), pci->elbi_base + 0xf4);
	writel(BIT(1), pci->elbi_base + 0x1b8);
	ret = s10_phy_init(ep);
	if (ret)
		return ret;
	writel(0, pci->elbi_base + 0x1d0);
	udelay(20);
	writel(1, pci->elbi_base + 0x1d0);
	writel(readl(ep->pcs + 0xec) | BIT(3), ep->pcs + 0xec);
	writel(readl(pci->elbi_base), pci->elbi_base);
	writel(0x55, pci->elbi_base + 0x0c); /* Legacy INTx pulses only. */
	return 0;
}

static const struct dw_pcie_host_ops s10_host_ops = {
	.init = s10_host_init,
};

static void s10_power_off(struct s10_pcie *ep)
{
	writel(0, ep->pci.elbi_base + 0x0c);
	writel(0, ep->pci.elbi_base + 0x10);
	writel(0, ep->pci.elbi_base + 0x14);
	if (ep->irq_requested)
		synchronize_irq(ep->pci.pp.irq);
	s10_stop_link(&ep->pci);
	gpiod_set_value_cansleep(ep->enable, 0);
	regmap_update_bits(ep->pmu, 0x71c, BIT(0), 0);
}

static int s10_probe(struct platform_device *pdev)
{
	struct device *dev = &pdev->dev;
	struct s10_pcie *ep;
	int ret;

	ep = devm_kzalloc(dev, sizeof(*ep), GFP_KERNEL);
	if (!ep)
		return -ENOMEM;
	ep->pci.dev = dev;
	ep->pci.ops = &s10_dw_ops;
	ep->pci.pp.ops = &s10_host_ops;
	platform_set_drvdata(pdev, ep);
	ep->pci.elbi_base = devm_platform_ioremap_resource_byname(pdev, "elbi");
	if (IS_ERR(ep->pci.elbi_base))
		return PTR_ERR(ep->pci.elbi_base);
	ep->phy = devm_platform_ioremap_resource_byname(pdev, "phy");
	if (IS_ERR(ep->phy))
		return PTR_ERR(ep->phy);
	ep->pcs = devm_platform_ioremap_resource_byname(pdev, "pcs");
	if (IS_ERR(ep->pcs))
		return PTR_ERR(ep->pcs);
	ep->sysreg = syscon_regmap_lookup_by_phandle(dev->of_node, "samsung,sysreg");
	if (IS_ERR(ep->sysreg))
		return PTR_ERR(ep->sysreg);
	ep->pmu = syscon_regmap_lookup_by_phandle(dev->of_node, "samsung,pmu");
	if (IS_ERR(ep->pmu))
		return PTR_ERR(ep->pmu);
	ep->reset = devm_gpiod_get(dev, "reset", GPIOD_OUT_HIGH);
	if (IS_ERR(ep->reset))
		return PTR_ERR(ep->reset);
	ep->enable = devm_gpiod_get(dev, "wlan-enable", GPIOD_OUT_LOW);
	if (IS_ERR(ep->enable))
		return PTR_ERR(ep->enable);
	ret = devm_clk_bulk_get_all_enabled(dev, &ep->clks);
	if (ret < 0)
		return ret;
	dev_info(dev, "PCIe0 test: clocks ready, enabling WLAN supply\n");
	gpiod_set_value_cansleep(ep->enable, 1);
	msleep(150);
	ep->pci.pp.irq = platform_get_irq(pdev, 0);
	if (ep->pci.pp.irq < 0) {
		ret = ep->pci.pp.irq;
		goto fail;
	}
	ret = devm_request_irq(dev, ep->pci.pp.irq, s10_irq, IRQF_SHARED, "s10-pcie0", ep);
	if (ret)
		goto fail;
	ep->irq_requested = true;
	ret = dw_pcie_host_init(&ep->pci.pp);
	if (ret)
		goto fail;
	dev_info(dev, "PCIe0 enumeration complete, link=%d\n", s10_link_up(&ep->pci));
	return 0;
fail:
	s10_power_off(ep);
	return dev_err_probe(dev, ret, "PCIe0 test probe failed\n");
}

static void s10_remove(struct platform_device *pdev)
{
	struct s10_pcie *ep = platform_get_drvdata(pdev);

	dw_pcie_host_deinit(&ep->pci.pp);
	s10_power_off(ep);
}

static const struct of_device_id s10_match[] = {
	{ .compatible = "samsung,exynos9820-pcie-test" }, { }
};
MODULE_DEVICE_TABLE(of, s10_match);
static struct platform_driver s10_driver = {
	.probe = s10_probe, .remove = s10_remove,
	.driver = { .name = "s10-pcie0-test", .of_match_table = s10_match },
};
module_platform_driver(s10_driver);
MODULE_LICENSE("GPL");
MODULE_DESCRIPTION("Galaxy S10 Exynos9820 PCIe0 enumeration test");
