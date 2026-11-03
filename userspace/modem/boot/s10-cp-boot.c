// SPDX-License-Identifier: GPL-2.0-only
/*
 * Galaxy S10 (Exynos 9820) Shannon 5000 CP boot helper - stage 1.
 *
 * Drives the same sequence Samsung's cbd + cpif do on the stock kernel:
 *   load images into the modem_if shm, init mailbox, power the CP block,
 *   let EL3 authenticate BOOT+MAIN and release the CP, watch the mailbox.
 *
 * Control through debugfs: echo <cmd> > /sys/kernel/debug/s10cp/ctl
 *   sizes <bl> <main>  remember image sizes for the auth SMC
 * (images are written to debugfs s10cp/shm by userspace, see load-radio.py)
 *   mbox          reset the CP mailbox and set AP status bits
 *   init          power on the CP block (cal_cp_init sequence)
 *   auth          SMC: verify BOOT+MAIN and start CP
 *   status        dump PMU / mailbox / shm header words
 *   off           assert CP reset
 */
#include <linux/module.h>
#include <linux/io.h>
#include <linux/fs.h>
#include <linux/uaccess.h>
#include <linux/delay.h>
#include <linux/debugfs.h>
#include <linux/slab.h>
#include <linux/vmalloc.h>
#include <linux/interrupt.h>
#include <linux/irq.h>
#include <linux/irqdomain.h>
#include <linux/of.h>
#include <linux/of_irq.h>
#include <linux/miscdevice.h>
#include <linux/poll.h>
#include <linux/wait.h>
#include <linux/atomic.h>

#define SHM_PHYS	0xf0000000UL
#define SHM_SIZE	0x0b900000UL
#define CP_BASE_ADDR	0x40000000U	/* CP view of SHM_PHYS */

#define PMU_CP		0x15863000UL	/* CP_CONFIGURATION.. */
#define CP_CONFIGURATION	0x00
#define CP_STATUS		0x04
#define CP_OUT			0x20
#define CP_IN			0x24

#define MBOX_PHYS	0x15920000UL
#define MBOX_SIZE	0x180
#define MBOX_ISSR(n)	(0x80 + 4 * (n))
#define MBOX_INTGR1	0x1c
#define MBOX_INTSR0	0x14
#define MBOX_MCUCTLR	0x0

#define AP2CP_CFG_PHYS	0x15020000UL

#define SMC_ID		0x82000700U
#define SMC_ID_CLK	0x82001011U
#define READ_CTRL	3
#define WRITE_CTRL	4
#define CP_CTRL_S	0
#define CP_CTRL_NS	1
#define CP_START	BIT(3)
#define CP_PWRON	BIT(1)
#define CP_RESET_SET	BIT(2)

/* mailbox ISSR numbers and bit fields from the stock mif_pdata */
#define MBX_AP2CP_MSG		0
#define MBX_CP2AP_MSG		1
#define MBX_AP2CP_UNITED	2
#define MBX_CP2AP_UNITED	3

static void __iomem *shm, *pmu, *mbox, *cfg, *spare;
#define SPARE_PHYS	0xd0000000UL	/* modem_spare: pktproc descriptors/data (CP view 0x50000000) */
#define SPARE_SIZE	0x01600000UL

static u32 bl_size, main_size;
static unsigned int smc_mode;
static unsigned int ap_status = 1;
module_param(ap_status, uint, 0644);
static unsigned int sys_rev, ds_det, dev_type;	/* ISSR2 fields: board rev [13:6], dual-SIM detect [15:14], device type [22:18] */
module_param(sys_rev, uint, 0644);
module_param(ds_det, uint, 0644);
module_param(dev_type, uint, 0644);
module_param(smc_mode, uint, 0644);

static unsigned long smc4(unsigned long a0, unsigned long a1, unsigned long a2,
			  unsigned long a3, unsigned long *r1)
{
	register unsigned long x0 asm("x0") = a0;
	register unsigned long x1 asm("x1") = a1;
	register unsigned long x2 asm("x2") = a2;
	register unsigned long x3 asm("x3") = a3;

	asm volatile("smc #0" : "+r"(x0), "+r"(x1), "+r"(x2), "+r"(x3) :
		     : "x4", "x5", "x6", "x7", "x8", "x9", "x10", "x11", "x12",
		       "x13", "x14", "x15", "x16", "x17", "memory");
	if (r1)
		*r1 = x1;
	return x0;
}

static int cp_ctrl_read(int reg, u32 *val)
{
	unsigned long lo = smc4(SMC_ID, READ_CTRL, 0, reg, NULL);
	unsigned long hi = smc4(SMC_ID, READ_CTRL, 1, reg, NULL);

	if ((lo & 0xffff) || (hi & 0xffff))
		return -EIO;
	*val = ((hi >> 16) << 16) | (lo >> 16);
	return 0;
}

static int cp_ctrl_write(int reg, u32 val)
{
	unsigned long r = smc4(SMC_ID, WRITE_CTRL, val, reg, NULL);

	return r > 0 ? -EIO : 0;
}

static int cp_ctrl_update(int reg, u32 mask, u32 val)
{
	u32 v;
	int ret = cp_ctrl_read(reg, &v);

	if (ret)
		return ret;
	return cp_ctrl_write(reg, (v & ~mask) | (val & mask));
}

static int ensure_maps(void)
{
	if (!shm)
		shm = ioremap_wc(SHM_PHYS, SHM_SIZE);
	if (!pmu)
		pmu = ioremap(PMU_CP, 0x40);
	if (!mbox)
		mbox = ioremap(MBOX_PHYS, MBOX_SIZE);
	if (!spare)
		spare = ioremap_wc(SPARE_PHYS, SPARE_SIZE);
	/* AP2CP_CFG (0x15020000) aborts the bus from EL1 here: not mapped */
	return (shm && pmu && mbox && spare) ? 0 : -ENOMEM;
}

static ssize_t shm_write(struct file *f, const char __user *ubuf, size_t len,
			 loff_t *pos)
{
	size_t done = 0;
	u8 *tmp;
	int ret = ensure_maps();

	if (ret)
		return ret;
	if (*pos >= SHM_SIZE)
		return -ENOSPC;
	len = min_t(size_t, len, SHM_SIZE - *pos);
	tmp = kmalloc(SZ_64K, GFP_KERNEL);
	if (!tmp)
		return -ENOMEM;
	while (done < len) {
		size_t n = min_t(size_t, len - done, SZ_64K);

		if (copy_from_user(tmp, ubuf + done, n)) {
			kfree(tmp);
			return -EFAULT;
		}
		memcpy_toio(shm + *pos + done, tmp, n);
		done += n;
	}
	kfree(tmp);
	*pos += done;
	return done;
}

static ssize_t shm_read(struct file *f, char __user *ubuf, size_t len,
			loff_t *pos)
{
	size_t done = 0;
	u8 *tmp;
	int ret = ensure_maps();

	if (ret)
		return ret;
	if (*pos >= SHM_SIZE)
		return 0;
	len = min_t(size_t, len, SHM_SIZE - *pos);
	tmp = kmalloc(SZ_64K, GFP_KERNEL);
	if (!tmp)
		return -ENOMEM;
	while (done < len) {
		size_t n = min_t(size_t, len - done, SZ_64K);

		memcpy_fromio(tmp, shm + *pos + done, n);
		if (copy_to_user(ubuf + done, tmp, n)) {
			kfree(tmp);
			return -EFAULT;
		}
		done += n;
	}
	kfree(tmp);
	*pos += done;
	return done;
}

static ssize_t spare_write(struct file *f, const char __user *ubuf, size_t len,
			 loff_t *pos)
{
	size_t done = 0;
	u8 *tmp;
	int ret = ensure_maps();

	if (ret)
		return ret;
	if (*pos >= SPARE_SIZE)
		return -ENOSPC;
	len = min_t(size_t, len, SPARE_SIZE - *pos);
	tmp = kmalloc(SZ_64K, GFP_KERNEL);
	if (!tmp)
		return -ENOMEM;
	while (done < len) {
		size_t n = min_t(size_t, len - done, SZ_64K);

		if (copy_from_user(tmp, ubuf + done, n)) {
			kfree(tmp);
			return -EFAULT;
		}
		memcpy_toio(spare + *pos + done, tmp, n);
		done += n;
	}
	kfree(tmp);
	*pos += done;
	return done;
}

static ssize_t spare_read(struct file *f, char __user *ubuf, size_t len,
			loff_t *pos)
{
	size_t done = 0;
	u8 *tmp;
	int ret = ensure_maps();

	if (ret)
		return ret;
	if (*pos >= SPARE_SIZE)
		return 0;
	len = min_t(size_t, len, SPARE_SIZE - *pos);
	tmp = kmalloc(SZ_64K, GFP_KERNEL);
	if (!tmp)
		return -ENOMEM;
	while (done < len) {
		size_t n = min_t(size_t, len - done, SZ_64K);

		memcpy_fromio(tmp, spare + *pos + done, n);
		if (copy_to_user(ubuf + done, tmp, n)) {
			kfree(tmp);
			return -EFAULT;
		}
		done += n;
	}
	kfree(tmp);
	*pos += done;
	return done;
}

static ssize_t mbox_read(struct file *f, char __user *ubuf, size_t len, loff_t *pos)
{
	char buf[160];
	int n, ret = ensure_maps();

	if (ret)
		return ret;
	/* "<ISSR1 cp2ap msg> <INTSR0> <ISSR3 cp2ap status>" and clear the pending CP2AP irq bits */
	n = scnprintf(buf, sizeof(buf), "%08x %08x %08x\n", readl(mbox + MBOX_ISSR(1)),
		      readl(mbox + 0x14), readl(mbox + MBOX_ISSR(3)));
	return simple_read_from_buffer(ubuf, len, pos, buf, n);
}

static const struct file_operations mbox_fops = {
	.read = mbox_read,
	.owner = THIS_MODULE,
};

static const struct file_operations spare_fops = {
	.read = spare_read,
	.write = spare_write,
	.llseek = default_llseek,
	.owner = THIS_MODULE,
};

static const struct file_operations shm_fops = {
	.read = shm_read,
	.write = shm_write,
	.llseek = default_llseek,
	.owner = THIS_MODULE,
};

/* CP bootloader ring layout (CP view 0x4b200000 = IPC region), from BL disassembly */
#define RB_IPC_OFF	0xb200000UL
#define RB_A2C_HEAD	0x18	/* AP writes, queue at ipc+0x3000, size 0x1fd000 */
#define RB_A2C_TAIL	0x1c
#define RB_C2A_HEAD	0x20	/* CP writes, queue at ipc+0x200000, size 0x200000 */
#define RB_C2A_TAIL	0x24
#define RB_A2C_BASE	0x3000
#define RB_A2C_SIZE	0x1fd000
#define RB_C2A_BASE	0x200000
#define RB_C2A_SIZE	0x200000

static void do_zero(void)
{
	int i;

	for (i = 0; i < 0x500000; i += 4)
		writel(0, shm + RB_IPC_OFF + i);
	pr_info("s10cp: ipc region zeroed\n");
}

/* 8-byte SIPC5-style frame on the boot channel (0xd7): fc d7 <len16> <code32> */
static int do_send(u32 code)
{
	void __iomem *ipc = shm + RB_IPC_OFF;
	u32 head = readl(ipc + RB_A2C_HEAD), tail = readl(ipc + RB_A2C_TAIL);
	u8 f[8] = { 0xfc, 0xd7, 8, 0, code, code >> 8, code >> 16, code >> 24 };

	if (head > RB_A2C_SIZE - 8 || tail > RB_A2C_SIZE)
		return -EINVAL;
	memcpy_toio(ipc + RB_A2C_BASE + head, f, 8);
	wmb();
	writel((head + 8) % RB_A2C_SIZE, ipc + RB_A2C_HEAD);
	pr_info("s10cp: sent %08x (head %x tail %x)\n", code, head + 8, tail);
	return 0;
}

static void do_recv(void)
{
	void __iomem *ipc = shm + RB_IPC_OFF;
	u32 head = readl(ipc + RB_C2A_HEAD), tail = readl(ipc + RB_C2A_TAIL);

	pr_info("s10cp: c2a head=%x tail=%x a2c head=%x tail=%x\n", head, tail,
		readl(ipc + RB_A2C_HEAD), readl(ipc + RB_A2C_TAIL));
	while (tail != head && head <= RB_C2A_SIZE && tail < RB_C2A_SIZE) {
		u8 f[8];

		memcpy_fromio(f, ipc + RB_C2A_BASE + tail, 8);
		pr_info("s10cp: rx @%x: %8ph\n", tail, f);
		tail = (tail + 8) % RB_C2A_SIZE;
	}
	writel(tail, ipc + RB_C2A_TAIL);
}

/* pktproc (PS downlink) global descriptor, as cpif setup_pktproc(): CP view = 0x50000000 + AP offset in 'spare' */
#define PKTPROC_CP_BASE		0x50000000U
#define PKTPROC_DESC_OFF	0x100
#define PKTPROC_DATA_OFF	0x100000
#define PKTPROC_NDESC		8192
#define PKTPROC_DESC_SZ		24

static void do_pktproc(void)
{
	u32 i;

	for (i = 0; i < PKTPROC_DATA_OFF; i += 4)
		writel(0, spare + i);
	writel(PKTPROC_CP_BASE | PKTPROC_DESC_OFF, spare + 0);	/* desc_base_addr */
	writel(PKTPROC_NDESC, spare + 4);			/* num_of_desc */
	writel(PKTPROC_CP_BASE | PKTPROC_DATA_OFF, spare + 8);	/* data_base_addr */
	writel(0, spare + 12);					/* fore_ptr (CP) */
	writel(0, spare + 16);					/* rear_ptr (AP) */
	wmb();
	pr_info("s10cp: pktproc global descriptor initialised\n");
}


/* ---- CP->AP mailbox interrupt (GIC SPI 41), delivered to userspace through /dev/s10modem-evt ----
 * read() returns the pending CP2AP interrupt bits (bit n = mailbox interrupt n) and clears them;
 * poll() wakes on a new interrupt. Lets the daemon sleep instead of polling the rings. */
#define MBOX_SPI	41
#define MBOX_INTCR0	0x0c
#define MBOX_INTMSR0	0x18

static int evt_irq;
static atomic_t evt_pending = ATOMIC_INIT(0);
static DECLARE_WAIT_QUEUE_HEAD(evt_wq);
static unsigned long evt_count;

static irqreturn_t cp_mbox_irq(int irq, void *data)
{
	u32 sr = readl(mbox + MBOX_INTMSR0) & 0xffff0000;

	if (!sr)
		return IRQ_NONE;
	writel(sr, mbox + MBOX_INTCR0);
	atomic_or(sr >> 16, &evt_pending);
	evt_count++;
	wake_up_interruptible(&evt_wq);
	return IRQ_HANDLED;
}

static ssize_t evt_read(struct file *f, char __user *ubuf, size_t len, loff_t *pos)
{
	u32 v;
	int ret;

	if (len < 4)
		return -EINVAL;
	if (!atomic_read(&evt_pending)) {
		if (f->f_flags & O_NONBLOCK)
			return -EAGAIN;
		ret = wait_event_interruptible(evt_wq, atomic_read(&evt_pending));
		if (ret)
			return ret;
	}
	v = atomic_xchg(&evt_pending, 0);
	return copy_to_user(ubuf, &v, 4) ? -EFAULT : 4;
}

static __poll_t evt_poll(struct file *f, poll_table *wait)
{
	poll_wait(f, &evt_wq, wait);
	return atomic_read(&evt_pending) ? EPOLLIN | EPOLLRDNORM : 0;
}

static const struct file_operations evt_fops = {
	.owner = THIS_MODULE,
	.read = evt_read,
	.poll = evt_poll,
};

static struct miscdevice evt_misc = {
	.minor = MISC_DYNAMIC_MINOR,
	.name = "s10modem-evt",
	.fops = &evt_fops,
};
static bool evt_registered;

static int do_irq_on(void)
{
	struct device_node *gic;
	struct irq_fwspec fw = { };
	int ret;

	if (evt_irq)
		return 0;
	gic = of_find_compatible_node(NULL, NULL, "arm,cortex-a15-gic");
	if (!gic)
		return -ENODEV;
	fw.fwnode = of_fwnode_handle(gic);
	fw.param_count = 3;
	fw.param[0] = 0;		/* GIC_SPI */
	fw.param[1] = MBOX_SPI;
	fw.param[2] = IRQ_TYPE_LEVEL_HIGH;
	evt_irq = irq_create_fwspec_mapping(&fw);
	if (evt_irq <= 0) {
		evt_irq = 0;
		return -EINVAL;
	}
	if (!evt_registered) {
		ret = misc_register(&evt_misc);
		if (ret)
			return ret;
		evt_registered = true;
	}
	/* clear anything stale, then listen */
	writel(readl(mbox + MBOX_INTMSR0) & 0xffff0000, mbox + MBOX_INTCR0);
	ret = request_irq(evt_irq, cp_mbox_irq, 0, "s10-cp-mbox", &evt_irq);
	if (ret) {
		irq_dispose_mapping(evt_irq);
		evt_irq = 0;
		return ret;
	}
	pr_info("s10cp: CP mailbox irq %d enabled, /dev/s10modem-evt\n", evt_irq);
	return 0;
}

static void do_irq_off(void)
{
	if (evt_irq) {
		free_irq(evt_irq, &evt_irq);
		irq_dispose_mapping(evt_irq);
		evt_irq = 0;
	}
	if (evt_registered) {
		misc_deregister(&evt_misc);
		evt_registered = false;
	}
}

static void do_mbox(void)
{
	int i;

	for (i = 0; i < 16; i++)
		writel(0, mbox + MBOX_ISSR(i));
	/* ap2cp united status: pda_active at bit 5; sys_rev/ds_det left 0 */
	writel(BIT(5) | (ap_status << 1) | ((sys_rev & 0xff) << 6) | ((ds_det & 3) << 14) | ((dev_type & 0x1f) << 18),
	       mbox + MBOX_ISSR(MBX_AP2CP_UNITED));
	pr_info("s10cp: mailbox cleared, ap_status=%08x\n",
		readl(mbox + MBOX_ISSR(MBX_AP2CP_UNITED)));
}

static int wait_pmu(u32 reg, u32 mask, u32 val, int ms)
{
	while (ms--) {
		if ((readl(pmu + reg) & mask) == val)
			return 0;
		msleep(1);
	}
	return -ETIMEDOUT;
}

static int do_init(void)
{
	int ret;

	if (readl(pmu + CP_STATUS) & 1) {
		pr_info("s10cp: CP block already up\n");
		return 0;
	}
	ret = cp_ctrl_update(CP_CTRL_S, CP_START, CP_START);
	pr_info("s10cp: CP_CTRL_S set START -> %d\n", ret);
	/* cal_cp_init: CP_CTRL_S[3] then CP_CONFIGURATION[0] */
	writel(readl(pmu + CP_CONFIGURATION) | 1, pmu + CP_CONFIGURATION);
	ret = wait_pmu(CP_STATUS, 1, 1, 1000);
	pr_info("s10cp: CP_STATUS wait -> %d (%08x)\n", ret, readl(pmu + CP_STATUS));
	ret = wait_pmu(CP_IN, BIT(4), BIT(4), 1000);
	pr_info("s10cp: CP_IN[4] wait -> %d (%08x)\n", ret, readl(pmu + CP_IN));
	return ret;
}

static int do_auth(void)
{
	unsigned long r;
	int tries = 0;

	if (!bl_size || !main_size) {
		pr_err("s10cp: run 'load' first\n");
		return -EINVAL;
	}
	smc4(SMC_ID_CLK, 0, 0, 0, NULL);
	pr_info("s10cp: SMC mode %lx bl=%x main=%x\n", SHM_PHYS | smc_mode, bl_size, main_size);
	r = smc4(SMC_ID, SHM_PHYS | smc_mode, bl_size, main_size, NULL);
	while (r == 0x100 && tries++ < 0x1000)
		r = smc4(SMC_ID, 8, 0, 0, NULL);
	smc4(SMC_ID_CLK, 1, 0, 0, NULL);
	pr_info("s10cp: SMC result 0x%lx after %d continues\n", r, tries);
	return r ? -EIO : 0;
}

static void do_status(void)
{
	u32 s = 0, ns = 0;
	int i;

	pr_info("s10cp: CP_CONFIGURATION=%08x CP_STATUS=%08x CP_OUT=%08x CP_IN=%08x\n",
		readl(pmu + CP_CONFIGURATION), readl(pmu + CP_STATUS),
		readl(pmu + CP_OUT), readl(pmu + CP_IN));
	cp_ctrl_read(CP_CTRL_S, &s);
	cp_ctrl_read(CP_CTRL_NS, &ns);
	pr_info("s10cp: CP_CTRL_S=%08x CP_CTRL_NS=%08x\n", s, ns);
	for (i = 0; i < 16; i++)
		pr_info("s10cp: ISSR%d=%08x\n", i, readl(mbox + MBOX_ISSR(i)));
	pr_info("s10cp: INTSR0=%08x INTMR0=%08x INTSR1=%08x INTMR1=%08x\n", readl(mbox + MBOX_INTSR0),
		readl(mbox + 0x10), readl(mbox + 0x28), readl(mbox + 0x24));
	pr_info("s10cp: shm+0=%08x shm+ipc(0xb200000)=%08x %08x %08x %08x\n",
		readl(shm), readl(shm + 0xb200000), readl(shm + 0xb200004),
		readl(shm + 0xb200008), readl(shm + 0xb20000c));
}

static void do_off(void)
{
	writel(readl(pmu + CP_CONFIGURATION) & ~1, pmu + CP_CONFIGURATION);
	wait_pmu(CP_STATUS, 1, 0, 1000);
	pr_info("s10cp: reset asserted CP_STATUS=%08x\n", readl(pmu + CP_STATUS));
}


/* ---------- stage 2: SBD (shared buffer descriptor) link, as in cpif ---------- */
#define SBD_DESC_OFF	2048		/* CTRL 1K + CMD 1K */
#define SBD_BUFF_OFF	SZ_64K
#define SBD_MAX_CH	32
#define MASK_INT_VALID	0x80
#define MASK_CMD_VALID	0x40
#define MASK_SEND_DATA	0x01
#define CMD_INIT_START	0x01
#define CMD_INIT_END	0x02
#define CMD_PHONE_START	0x08
#define CMD_PIF_INIT_DONE 0x0d
#define MBX_AP2CP_MSG_REG 0
#define MBX_CP2AP_MSG_REG 1
#define INTGR1		0x1c
#define INTCR0		0x0c
#define INTSR0_	0x14
#define INTMSR0		0x18

struct sbd_ch {
	u16 ch;
	u16 ul_len, ul_bsz, dl_len, dl_bsz;
	u32 ul_buf, dl_buf;	/* buffer array offsets in ipc region */
	u32 ul_addr_v, ul_size_v, dl_addr_v, dl_size_v;
};

/* non-PS IPC channels of the stock EUR DT, minus PS data (pktproc) */
static struct sbd_ch chans[] = {
	{ .ch = 0xf4, .ul_len = 0x10, .ul_bsz = 0x800, .dl_len = 0x20, .dl_bsz = 0x800 },	/* ipc_loopback0 */
	{ .ch = 0x1c, .ul_len = 0x10, .ul_bsz = 0x800, .dl_len = 0x80, .dl_bsz = 0x800 },	/* umts_dm0 */
	{ .ch = 0x19, .ul_len = 0x10, .ul_bsz = 0x800, .dl_len = 0x10, .dl_bsz = 0x800 },	/* umts_router */
	{ .ch = 0x01, .ul_len = 0x20, .ul_bsz = 0x800, .dl_len = 0x40, .dl_bsz = 0x800 },	/* umts_csd */
	{ .ch = 0xf5, .ul_len = 0x10, .ul_bsz = 0x800, .dl_len = 0x200, .dl_bsz = 0x800 },	/* umts_rfs0 */
	{ .ch = 0xec, .ul_len = 0x10, .ul_bsz = 0x1000, .dl_len = 0x20, .dl_bsz = 0x1000 },	/* umts_ipc1 */
	{ .ch = 0xeb, .ul_len = 0x10, .ul_bsz = 0x1000, .dl_len = 0x20, .dl_bsz = 0x1000 },	/* umts_ipc0 */
	{ .ch = 0x23, .ul_len = 0x10, .ul_bsz = 0x800, .dl_len = 0x10, .dl_bsz = 0x800 },	/* umts_cass */
	{ .ch = 0x21, .ul_len = 0x10, .ul_bsz = 0x800, .dl_len = 0x80, .dl_bsz = 0x800 },	/* smd4 (AT) */
	{ .ch = 0x1a, .ul_len = 0x10, .ul_bsz = 0x800, .dl_len = 0x80, .dl_bsz = 0x800 },	/* umts_ciq0 */
	{ .ch = 0x0a, .ul_len = 0x200, .ul_bsz = 0x800, .dl_len = 0x400, .dl_bsz = 0x08 },	/* multipdp (QoS ring "10") */
	{ .ch = 0x0b, .ul_len = 0x100, .ul_bsz = 0x800, .dl_len = 0x100, .dl_bsz = 0x800 },	/* multipdp_hiprio (QoS ring "11") */
};
#define NCH ARRAY_SIZE(chans)
static u32 rbps_off = SBD_DESC_OFF + 12 + SBD_MAX_CH * 16 + SBD_MAX_CH * 2 * 20;	/* u16 rp[UL][n], wp[UL][n], rp[DL][n], wp[DL][n] */

struct __packed sbd_rbd {
	u16 ch, reserved, direction, signaling;
	u32 sig_mask;
	u16 length, id, buff_size, payload_offset;
};

static u16 rbp_rd(int dir, int wr, int id)
{
	return readw(shm + RB_IPC_OFF + rbps_off + 2 * (NCH * (dir * 2 + wr) + id));
}

static void rbp_wr(int dir, int wr, int id, u16 v)
{
	writew(v, shm + RB_IPC_OFF + rbps_off + 2 * (NCH * (dir * 2 + wr) + id));
}

static int do_initsbd(void)
{
	void __iomem *ipc = shm + RB_IPC_OFF;
	u32 desc = SBD_DESC_OFF, buff = SBD_BUFF_OFF;
	u32 g_off, rbd_off;
	size_t gsz = 12 + SBD_MAX_CH * 16 + SBD_MAX_CH * 2 * sizeof(struct sbd_rbd);
	u8 *g;
	int i, j;

	g = kzalloc(gsz, GFP_KERNEL);
	if (!g)
		return -ENOMEM;
	for (i = 0; i < SZ_64K - SBD_DESC_OFF; i += 4)
		writel(0, ipc + SBD_DESC_OFF + i);

	g_off = desc;
	desc += gsz;
	rbps_off = desc;
	desc += 2 * 2 * 2 * NCH;
	((__le32 *)g)[0] = cpu_to_le32(1);		/* version */
	((__le32 *)g)[1] = cpu_to_le32(NCH);
	((__le32 *)g)[2] = cpu_to_le32(rbps_off);
	rbd_off = 12 + SBD_MAX_CH * 16;

	for (i = 0; i < NCH; i++) {
		struct sbd_ch *c = &chans[i];
		__le32 *rbch = (__le32 *)(g + 12 + 16 * i);

		for (j = 0; j < 2; j++) {
			u16 len = j ? c->dl_len : c->ul_len;
			u16 bsz = j ? c->dl_bsz : c->ul_bsz;
			struct sbd_rbd *d = (void *)(g + rbd_off + (i * 2 + j) * sizeof(*d));
			u32 addr_v = desc, size_v = desc + len * 4, b = buff, k;

			desc += len * 8;
			buff += (u32)len * bsz;
			if (desc > SBD_BUFF_OFF || buff > 0x500000) {
				kfree(g);
				return -ENOSPC;
			}
			d->ch = c->ch;
			d->direction = j;
			d->signaling = 1;
			d->sig_mask = MASK_INT_VALID | MASK_SEND_DATA;
			d->length = len;
			d->id = i;
			d->buff_size = bsz;
			d->payload_offset = 0;
			for (k = 0; k < len; k++) {
				writel(b + k * bsz, ipc + addr_v + 4 * k);
				writel(0, ipc + size_v + 4 * k);
			}
			if (j) {
				c->dl_buf = b; c->dl_addr_v = addr_v; c->dl_size_v = size_v;
			} else {
				c->ul_buf = b; c->ul_addr_v = addr_v; c->ul_size_v = size_v;
			}
			rbch[j * 2] = cpu_to_le32(g_off + rbd_off + (i * 2 + j) * sizeof(*d));
			rbch[j * 2 + 1] = cpu_to_le32(addr_v);
		}
	}
	memcpy_toio(ipc + g_off, g, gsz);
	kfree(g);
	for (i = 0; i < 4 * NCH; i++)
		writew(0, ipc + rbps_off + 2 * i);
	wmb();
	pr_info("s10cp: SBD %zu channels, desc end %x, buff end %x\n", NCH, desc, buff);
	return 0;
}

static void send_cp_irq(u16 val)
{
	writel(val, mbox + MBOX_ISSR(MBX_AP2CP_MSG_REG));
	wmb();
	writel(BIT(0), mbox + INTGR1);
}

static void do_mbrx(void)
{
	u32 sr = readl(mbox + INTSR0_), msr = readl(mbox + INTMSR0);
	u32 msg = readl(mbox + MBOX_ISSR(MBX_CP2AP_MSG_REG));

	pr_info("s10cp: mbrx INTSR0=%08x INTMSR0=%08x ISSR1=%08x -> %s cmd=%u data=%x\n", sr, msr, msg,
		(msg & MASK_INT_VALID) ? "valid" : "novalid", msg & 0x3f, msg & 0x3f);
	writel(sr & 0xffff0000, mbox + INTCR0);
}

static void do_dl(void)
{
	void __iomem *ipc = shm + RB_IPC_OFF;
	int i;

	for (i = 0; i < NCH; i++) {
		struct sbd_ch *c = &chans[i];
		u16 rp = rbp_rd(1, 0, i), wp = rbp_rd(1, 1, i);

		while (rp != wp && rp < c->dl_len) {
			u32 len = readl(ipc + c->dl_size_v + 4 * rp) & 0xffff;
			u8 f[96];

			memcpy_fromio(f, ipc + c->dl_buf + (u32)rp * c->dl_bsz, min_t(u32, len, 96));
			pr_info("s10cp: DL ch%#x[%d] len %u: %*ph\n", c->ch, rp, len,
				(int)min_t(u32, len, 96), f);
			if (len > 4)
				pr_info("s10cp: DL ch%#x text: %.*s\n", c->ch,
					(int)min_t(u32, len - 4, 44), (char *)f + 4);
			rp = (rp + 1) % c->dl_len;
			rbp_wr(1, 0, i, rp);
		}
	}
}

/* write a SIPC5 frame (no padding, no ctl) on UL ring of channel @ch */
static int do_ul(u8 ch, const char *data, int n)
{
	void __iomem *ipc = shm + RB_IPC_OFF;
	int i;

	for (i = 0; i < NCH; i++) {
		struct sbd_ch *c = &chans[i];
		u16 wp, rp;
		u8 hdr[4];

		if (c->ch != ch)
			continue;
		wp = rbp_rd(0, 1, i);
		rp = rbp_rd(0, 0, i);
		if ((wp + 1) % c->ul_len == rp)
			return -ENOSPC;
		hdr[0] = 0xf8;
		hdr[1] = ch;
		hdr[2] = (n + 4) & 0xff;
		hdr[3] = (n + 4) >> 8;
		memcpy_toio(ipc + c->ul_buf + (u32)wp * c->ul_bsz, hdr, 4);
		memcpy_toio(ipc + c->ul_buf + (u32)wp * c->ul_bsz + 4, data, n);
		writel(n + 4, ipc + c->ul_size_v + 4 * wp);
		wmb();
		rbp_wr(0, 1, i, (wp + 1) % c->ul_len);
		send_cp_irq(MASK_INT_VALID | MASK_SEND_DATA);
		return 0;
	}
	return -ENODEV;
}

static ssize_t ctl_write(struct file *f, const char __user *ubuf, size_t len,
			 loff_t *pos)
{
	char cmd[64];
	u32 arg32;
	int ret = 0;
	unsigned int u2val;

	if (!len)
		return 0;
	if (len >= sizeof(cmd))
		return -EINVAL;
	if (copy_from_user(cmd, ubuf, len))
		return -EFAULT;
	cmd[len] = 0;
	while (len && (cmd[len - 1] == '\n' || cmd[len - 1] == ' '))
		cmd[--len] = 0;

	ret = ensure_maps();
	if (ret)
		return ret;
	if (sscanf(cmd, "sizes %i %i", &bl_size, &main_size) == 2)
		pr_info("s10cp: bl=%x main=%x\n", bl_size, main_size);
	else if (!strcmp(cmd, "mbox"))
		do_mbox();
	else if (sscanf(cmd, "issr2 %i", &u2val) == 1) {
		writel(u2val, mbox + MBOX_ISSR(MBX_AP2CP_UNITED));
		pr_info("s10cp: ISSR2=%08x\n", readl(mbox + MBOX_ISSR(MBX_AP2CP_UNITED)));
	}
	else if (!strcmp(cmd, "initsbd"))
		ret = do_initsbd();
	else if (!strcmp(cmd, "mbrx"))
		do_mbrx();
	else if (!strcmp(cmd, "dl"))
		do_dl();
	else if (sscanf(cmd, "irq %i", &arg32) == 1)
		send_cp_irq(arg32);
	else if (!strncmp(cmd, "ul ", 3)) {
		u32 ch;
		char txt[48];
		int n;

		n = sscanf(cmd + 3, "%i %47[^\n]", &ch, txt);
		if (n == 2) {
			strcat(txt, "\r");
			ret = do_ul(ch, txt, strlen(txt));
		} else {
			ret = -EINVAL;
		}
	} else if (!strcmp(cmd, "rings")) {
		int i;

		for (i = 0; i < NCH; i++)
			pr_info("s10cp: ch%#04x UL rp=%u wp=%u  DL rp=%u wp=%u\n", chans[i].ch,
				rbp_rd(0, 0, i), rbp_rd(0, 1, i), rbp_rd(1, 0, i), rbp_rd(1, 1, i));
	} else if (!strcmp(cmd, "irq_on")) {
		ret = do_irq_on();
	} else if (!strcmp(cmd, "pktproc")) {
		do_pktproc();
	} else if (!strcmp(cmd, "magic")) {
		writel(0xAA, shm + RB_IPC_OFF);
		writel(1, shm + RB_IPC_OFF + 4);
	} else if (!strcmp(cmd, "zero"))
		do_zero();
	else if (!strcmp(cmd, "recv"))
		do_recv();
	else if (sscanf(cmd, "send %i", &arg32) == 1)
		ret = do_send(arg32);
	else if (!strcmp(cmd, "reinit")) {
		unsigned long r;

		smc4(SMC_ID_CLK, 0, 0, 0, NULL);
		r = smc4(SMC_ID, SHM_PHYS | 2, 0, 0, NULL);
		smc4(SMC_ID_CLK, 1, 0, 0, NULL);
		pr_info("s10cp: SMC reinit -> 0x%lx\n", r);
	} else if (!strcmp(cmd, "cfgrd")) {
		if (!cfg)
			cfg = ioremap(AP2CP_CFG_PHYS, 4);
		pr_info("s10cp: AP2CP_CFG=%08x\n", readl(cfg));
	} else if (!strcmp(cmd, "cfg")) {
		if (!cfg)
			cfg = ioremap(AP2CP_CFG_PHYS, 4);
		writel(1, cfg);
		pr_info("s10cp: AP2CP_CFG=%08x\n", readl(cfg));
	} else if (!strcmp(cmd, "ctrl")) {
		ret = cp_ctrl_update(CP_CTRL_S, CP_START, CP_START);
	} else if (!strcmp(cmd, "pwr")) {
		writel(readl(pmu + CP_CONFIGURATION) | 1, pmu + CP_CONFIGURATION);
		ret = wait_pmu(CP_STATUS, 1, 1, 1000);
		pr_info("s10cp: CP_STATUS=%08x CP_IN=%08x\n",
			readl(pmu + CP_STATUS), readl(pmu + CP_IN));
	} else if (!strcmp(cmd, "init"))
		ret = do_init();
	else if (!strcmp(cmd, "auth"))
		ret = do_auth();
	else if (!strcmp(cmd, "status"))
		do_status();
	else if (!strcmp(cmd, "off"))
		do_off();
	else
		ret = -EINVAL;
	pr_info("s10cp: '%s' -> %d\n", cmd, ret);
	return ret ? ret : len;
}

static const struct file_operations ctl_fops = {
	.write = ctl_write,
	.owner = THIS_MODULE,
};

static struct dentry *dir;

static int __init s10cp_init(void)
{
	dir = debugfs_create_dir("s10cp", NULL);
	debugfs_create_file("shm", 0600, dir, NULL, &shm_fops);
	debugfs_create_file("spare", 0600, dir, NULL, &spare_fops);
	debugfs_create_file("ctl", 0200, dir, NULL, &ctl_fops);
	debugfs_create_file("mbox", 0400, dir, NULL, &mbox_fops);
	return 0;
}

static void __exit s10cp_exit(void)
{
	do_irq_off();
	debugfs_remove_recursive(dir);
	if (shm)
		iounmap(shm);
	if (pmu)
		iounmap(pmu);
	if (mbox)
		iounmap(mbox);
	if (cfg)
		iounmap(cfg);
}

module_init(s10cp_init);
module_exit(s10cp_exit);
MODULE_LICENSE("GPL");
MODULE_DESCRIPTION("S10 Shannon CP boot helper (stage 1)");
