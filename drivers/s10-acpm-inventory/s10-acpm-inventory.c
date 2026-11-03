// SPDX-License-Identifier: GPL-2.0
/* Read-only inventory of the Exynos9820 ACPM firmware plugin table. */
#include <linux/ctype.h>
#include <linux/errno.h>
#include <linux/init.h>
#include <linux/io.h>
#include <linux/module.h>
#include <linux/of.h>
#include <linux/string.h>
#include <linux/types.h>

#define APM_SRAM_PA 0x02039000
#define APM_SRAM_SIZE 0x30000
#define APM_INITDATA_OFFSET 0x7000
#define MAX_PLUGINS 32
#define MAX_NAME 64

/* CONFIG_EXYNOS_ACPM layout of Samsung's fw_header/common.h: all firmware
 * pointers are 32-bit SRAM offsets. No firmware pointers are dereferenced.
 */
struct acpm_plugin_record {
	u32 id, base_addr, acpm_ops, plugin_ops;
	u32 secure_func_mask, extern_func_mask;
	u32 timer_period, timer_multiplier;
	u8 attached;
	u32 size;
	u8 stay_attached;
	u32 fw_name;
};

static_assert(sizeof(struct acpm_plugin_record) == 48);
static_assert(offsetof(struct acpm_plugin_record, attached) == 32);
static_assert(offsetof(struct acpm_plugin_record, stay_attached) == 40);
static_assert(offsetof(struct acpm_plugin_record, fw_name) == 44);

static bool range_ok(u32 offset, u32 length)
{
	return offset <= APM_SRAM_SIZE && length <= APM_SRAM_SIZE - offset;
}

static void plugin_name(void __iomem *sram, u32 offset, char *name)
{
	unsigned int i;

	if (!offset) {
		strscpy(name, "<none>", MAX_NAME);
		return;
	}
	if (!range_ok(offset, MAX_NAME)) {
		strscpy(name, "<invalid-offset>", MAX_NAME);
		return;
	}
	for (i = 0; i < MAX_NAME - 1; i++) {
		unsigned char c = readb(sram + offset + i);

		if (!c) {
			name[i] = 0;
			return;
		}
		if (!isalnum(c) && c != '_' && c != '-' && c != '.' && c != '/') {
			strscpy(name, "<invalid-name>", MAX_NAME);
			return;
		}
		name[i] = c;
	}
	strscpy(name, "<unterminated>", MAX_NAME);
}

static int __init s10_acpm_inventory_init(void)
{
	void __iomem *sram;
	u32 offset, count, i;

	if (!of_machine_is_compatible("samsung,exynos9820"))
		return -ENODEV;
	sram = ioremap(APM_SRAM_PA, APM_SRAM_SIZE);
	if (!sram)
		return -ENOMEM;
	offset = readl(sram + APM_INITDATA_OFFSET);
	count = readl(sram + APM_INITDATA_OFFSET + 4);
	if (count > MAX_PLUGINS || (count && (!offset || (offset & 3))) ||
	    !range_ok(offset, count * sizeof(struct acpm_plugin_record))) {
		pr_err("s10-acpm-inventory: invalid table offset=%#x count=%u\n", offset, count);
		iounmap(sram);
		return -EINVAL;
	}

	pr_info("s10-acpm-inventory: table=%#x count=%u (live, non-atomic)\n", offset, count);
	for (i = 0; i < count; i++) {
		void __iomem *record = sram + offset + i * sizeof(struct acpm_plugin_record);
		u32 id = readl(record + offsetof(struct acpm_plugin_record, id));
		u32 fw_name = readl(record + offsetof(struct acpm_plugin_record, fw_name));
		u8 attached = readb(record + offsetof(struct acpm_plugin_record, attached));
		u8 stay = readb(record + offsetof(struct acpm_plugin_record, stay_attached));
		char name[MAX_NAME];

		plugin_name(sram, fw_name, name);
		pr_info("s10-acpm-inventory: index=%u id=%u attached=%u stay=%u name=%s\n",
			i, id, attached, stay, name);
	}
	if (offset != readl(sram + APM_INITDATA_OFFSET) ||
	    count != readl(sram + APM_INITDATA_OFFSET + 4))
		pr_warn("s10-acpm-inventory: table header changed during read; discard inventory\n");
	iounmap(sram);
	return 0;
}

static void __exit s10_acpm_inventory_exit(void) { }
module_init(s10_acpm_inventory_init);
module_exit(s10_acpm_inventory_exit);
MODULE_LICENSE("GPL");
MODULE_DESCRIPTION("Read-only Exynos9820 ACPM plugin inventory");
