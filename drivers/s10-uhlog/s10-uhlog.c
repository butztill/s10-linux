// SPDX-License-Identifier: GPL-2.0
/* Debug aid: expose Samsung's EL2 hypervisor (uH) log buffer (stock include/linux/uh.h UH_LOG_START). */
#include <linux/debugfs.h>
#include <linux/io.h>
#include <linux/module.h>

#define UH_LOG_START	0x87100000
#define UH_LOG_SIZE	0x40000

static struct debugfs_blob_wrapper blob;
static struct dentry *file;

static int __init s10_uhlog_init(void)
{
	blob.data = memremap(UH_LOG_START, UH_LOG_SIZE, MEMREMAP_WB);
	if (!blob.data)
		return -ENOMEM;
	blob.size = UH_LOG_SIZE;
	file = debugfs_create_blob("s10_uhlog", 0400, NULL, &blob);
	return 0;
}

static void __exit s10_uhlog_exit(void)
{
	debugfs_remove(file);
	memunmap(blob.data);
}
module_init(s10_uhlog_init);
module_exit(s10_uhlog_exit);
MODULE_LICENSE("GPL");
MODULE_DESCRIPTION("S10 uH log reader");
