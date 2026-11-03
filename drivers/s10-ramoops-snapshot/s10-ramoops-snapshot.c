// SPDX-License-Identifier: GPL-2.0-only
/* Read only the known ramoops area after recovery into the original stable DT. */
#include <linux/capability.h>
#include <linux/fs.h>
#include <linux/io.h>
#include <linux/miscdevice.h>
#include <linux/module.h>
#include <linux/of_address.h>
#include <linux/vmalloc.h>

#define SNAP_BASE 0xfc000000ULL
#define SNAP_SIZE 0x200000
static void *snapshot;

static int snapshot_open(struct inode *inode, struct file *file)
{
	return capable(CAP_SYS_RAWIO) ? 0 : -EPERM;
}

static ssize_t snapshot_read(struct file *file, char __user *buf,
			     size_t size, loff_t *off)
{
	return simple_read_from_buffer(buf, size, off, snapshot, SNAP_SIZE);
}

static const struct file_operations snapshot_fops = {
	.owner = THIS_MODULE,
	.open = snapshot_open,
	.read = snapshot_read,
	.llseek = default_llseek,
};
static struct miscdevice snapshot_dev = {
	.minor = MISC_DYNAMIC_MINOR,
	.name = "s10-ramoops-snapshot",
	.fops = &snapshot_fops,
	.mode = 0400,
};

static int __init snapshot_init(void)
{
	struct device_node *np;
	struct resource res;
	void __iomem *mem;
	int ret;

	if (!of_machine_is_compatible("samsung,beyond1lte"))
		return -ENODEV;
	np = of_find_node_by_path("/reserved-memory/ramoops@fc000000");
	if (np) {
		of_node_put(np);
		return -EBUSY; /* use pstore when its backend is already active */
	}
	np = of_find_node_by_path("/reserved-memory/cp_ram_logging@fb900000");
	if (!np)
		return -ENODEV;
	ret = of_address_to_resource(np, 0, &res);
	if (!ret && (!of_property_read_bool(np, "no-map") ||
		    res.start > SNAP_BASE || res.end < SNAP_BASE + SNAP_SIZE - 1))
		ret = -EINVAL;
	of_node_put(np);
	if (ret)
		return ret;
	snapshot = vzalloc(SNAP_SIZE);
	if (!snapshot)
		return -ENOMEM;
	mem = ioremap_wc(SNAP_BASE, SNAP_SIZE);
	if (!mem) {
		vfree(snapshot);
		return -ENOMEM;
	}
	memcpy_fromio(snapshot, mem, SNAP_SIZE);
	iounmap(mem);
	ret = misc_register(&snapshot_dev);
	if (ret)
		vfree(snapshot);
	return ret;
}

static void __exit snapshot_exit(void)
{
	misc_deregister(&snapshot_dev);
	vfree(snapshot);
}
module_init(snapshot_init);
module_exit(snapshot_exit);
MODULE_LICENSE("GPL");
MODULE_DESCRIPTION("Read-only S10 reserved ramoops snapshot after stable recovery");
