// SPDX-License-Identifier: GPL-2.0-only
/*
 * Galaxy S10 Shannon modem: tty bridge for ModemManager.
 *
 * Creates a platform device "s10-modem" with real tty ports (/dev/ttyCP0, /dev/ttyCP1) so that
 * ModemManager (which only sees sysfs-registered ttys) can use them as AT ports. The userspace
 * modem daemon (s10modemd) attaches to the other end through /dev/s10modem-at0 and -at1, like the
 * master side of a pty: bytes written by the tty user appear on read(), bytes written by the
 * daemon are delivered to the tty reader.
 */
#include <linux/module.h>
#include <linux/platform_device.h>
#include <linux/tty.h>
#include <linux/tty_driver.h>
#include <linux/tty_flip.h>
#include <linux/miscdevice.h>
#include <linux/kfifo.h>
#include <linux/poll.h>
#include <linux/slab.h>
#include <linux/uaccess.h>
#include <linux/netdevice.h>
#include <linux/skbuff.h>
#include <linux/if_arp.h>
#include <linux/ip.h>

#define NPORTS		2
#define FIFO_SZ		8192

struct s10_port {
	struct tty_port port;
	struct miscdevice misc;
	char misc_name[24];
	DECLARE_KFIFO(to_daemon, u8, FIFO_SZ);
	spinlock_t lock;
	wait_queue_head_t wq;
	bool daemon_open;
};

static struct s10_port ports[NPORTS];
static struct tty_driver *drv;
static struct platform_device *pdev;

/* ---- tty side ---- */
static int s10_install(struct tty_driver *d, struct tty_struct *tty)
{
	tty->port = &ports[tty->index].port;
	return tty_standard_install(d, tty);
}

static int s10_open(struct tty_struct *tty, struct file *f)
{
	return tty_port_open(tty->port, tty, f);
}

static void s10_close(struct tty_struct *tty, struct file *f)
{
	tty_port_close(tty->port, tty, f);
}

static void s10_hangup(struct tty_struct *tty)
{
	tty_port_hangup(tty->port);
}

static ssize_t s10_write(struct tty_struct *tty, const u8 *buf, size_t count)
{
	struct s10_port *p = container_of(tty->port, struct s10_port, port);
	unsigned long flags;
	unsigned int n;

	spin_lock_irqsave(&p->lock, flags);
	n = kfifo_in(&p->to_daemon, buf, count);
	spin_unlock_irqrestore(&p->lock, flags);
	if (n)
		wake_up_interruptible(&p->wq);
	return n;
}

static unsigned int s10_write_room(struct tty_struct *tty)
{
	struct s10_port *p = container_of(tty->port, struct s10_port, port);

	return kfifo_avail(&p->to_daemon);
}

static unsigned int s10_chars_in_buffer(struct tty_struct *tty)
{
	struct s10_port *p = container_of(tty->port, struct s10_port, port);

	return kfifo_len(&p->to_daemon);
}

static const struct tty_operations s10_tty_ops = {
	.install = s10_install,
	.open = s10_open,
	.close = s10_close,
	.hangup = s10_hangup,
	.write = s10_write,
	.write_room = s10_write_room,
	.chars_in_buffer = s10_chars_in_buffer,
};

static const struct tty_port_operations s10_port_ops = { };

/* ---- daemon side (misc device) ---- */
static struct s10_port *misc_to_port(struct file *f)
{
	struct miscdevice *m = f->private_data;

	return container_of(m, struct s10_port, misc);
}

static int s10_misc_open(struct inode *i, struct file *f)
{
	struct s10_port *p = misc_to_port(f);

	if (p->daemon_open)
		return -EBUSY;
	p->daemon_open = true;
	return 0;
}

static int s10_misc_release(struct inode *i, struct file *f)
{
	misc_to_port(f)->daemon_open = false;
	return 0;
}

static ssize_t s10_misc_read(struct file *f, char __user *ubuf, size_t len, loff_t *pos)
{
	struct s10_port *p = misc_to_port(f);
	unsigned int copied;
	int ret;

	if (kfifo_is_empty(&p->to_daemon)) {
		if (f->f_flags & O_NONBLOCK)
			return -EAGAIN;
		ret = wait_event_interruptible(p->wq, !kfifo_is_empty(&p->to_daemon));
		if (ret)
			return ret;
	}
	ret = kfifo_to_user(&p->to_daemon, ubuf, len, &copied);
	if (ret)
		return ret;
	/* room again for the tty writer */
	tty_port_tty_wakeup(&p->port);
	return copied;
}

static ssize_t s10_misc_write(struct file *f, const char __user *ubuf, size_t len, loff_t *pos)
{
	struct s10_port *p = misc_to_port(f);
	u8 tmp[256];
	size_t done = 0;

	while (done < len) {
		size_t n = min(len - done, sizeof(tmp));

		if (copy_from_user(tmp, ubuf + done, n))
			return done ? done : -EFAULT;
		tty_insert_flip_string(&p->port, tmp, n);
		done += n;
	}
	tty_flip_buffer_push(&p->port);
	return done;
}

static __poll_t s10_misc_poll(struct file *f, poll_table *wait)
{
	struct s10_port *p = misc_to_port(f);
	__poll_t m = EPOLLOUT | EPOLLWRNORM;

	poll_wait(f, &p->wq, wait);
	if (!kfifo_is_empty(&p->to_daemon))
		m |= EPOLLIN | EPOLLRDNORM;
	return m;
}

static const struct file_operations s10_misc_fops = {
	.owner = THIS_MODULE,
	.open = s10_misc_open,
	.release = s10_misc_release,
	.read = s10_misc_read,
	.write = s10_misc_write,
	.poll = s10_misc_poll,
};


/* ---- data: raw-IP netdev "rmnet0" <-> /dev/s10modem-net0 (daemon moves packets to/from the CP rings) ---- */
#define NET_QLEN	256

struct s10_net {
	struct net_device *ndev;
	struct miscdevice misc;
	struct sk_buff_head txq;	/* AP -> daemon */
	wait_queue_head_t wq;
	bool daemon_open;
};

static struct s10_net snet;

static netdev_tx_t s10_ndo_xmit(struct sk_buff *skb, struct net_device *dev)
{
	if (!snet.daemon_open || skb_queue_len(&snet.txq) >= NET_QLEN) {
		dev->stats.tx_dropped++;
		dev_kfree_skb_any(skb);
		return NETDEV_TX_OK;
	}
	dev->stats.tx_packets++;
	dev->stats.tx_bytes += skb->len;
	skb_queue_tail(&snet.txq, skb);
	wake_up_interruptible(&snet.wq);
	return NETDEV_TX_OK;
}

static int s10_ndo_open(struct net_device *dev)
{
	netif_start_queue(dev);
	return 0;
}

static int s10_ndo_stop(struct net_device *dev)
{
	netif_stop_queue(dev);
	return 0;
}

static const struct net_device_ops s10_net_ops = {
	.ndo_open = s10_ndo_open,
	.ndo_stop = s10_ndo_stop,
	.ndo_start_xmit = s10_ndo_xmit,
};

static void s10_net_setup(struct net_device *dev)
{
	dev->netdev_ops = &s10_net_ops;
	dev->type = ARPHRD_RAWIP;
	dev->flags = IFF_POINTOPOINT | IFF_NOARP | IFF_MULTICAST;
	dev->hard_header_len = 0;
	dev->addr_len = 0;
	dev->mtu = 1500;
	dev->tx_queue_len = 1000;
	dev->needs_free_netdev = false;
}

static int s10_net_open_misc(struct inode *i, struct file *f)
{
	if (snet.daemon_open)
		return -EBUSY;
	snet.daemon_open = true;
	return 0;
}

static int s10_net_release_misc(struct inode *i, struct file *f)
{
	snet.daemon_open = false;
	skb_queue_purge(&snet.txq);
	return 0;
}

static ssize_t s10_net_read(struct file *f, char __user *ubuf, size_t len, loff_t *pos)
{
	struct sk_buff *skb;
	int ret;

	skb = skb_dequeue(&snet.txq);
	if (!skb) {
		if (f->f_flags & O_NONBLOCK)
			return -EAGAIN;
		ret = wait_event_interruptible(snet.wq, !skb_queue_empty(&snet.txq));
		if (ret)
			return ret;
		skb = skb_dequeue(&snet.txq);
		if (!skb)
			return -EAGAIN;
	}
	if (skb->len > len) {
		dev_kfree_skb_any(skb);
		return -EMSGSIZE;
	}
	len = skb->len;
	ret = copy_to_user(ubuf, skb->data, len) ? -EFAULT : len;
	dev_kfree_skb_any(skb);
	return ret;
}

static ssize_t s10_net_write(struct file *f, const char __user *ubuf, size_t len, loff_t *pos)
{
	struct sk_buff *skb;

	if (!len || len > 65535)
		return -EINVAL;
	skb = netdev_alloc_skb(snet.ndev, len);
	if (!skb)
		return -ENOMEM;
	if (copy_from_user(skb_put(skb, len), ubuf, len)) {
		kfree_skb(skb);
		return -EFAULT;
	}
	switch (skb->data[0] >> 4) {
	case 4:
		skb->protocol = htons(ETH_P_IP);
		break;
	case 6:
		skb->protocol = htons(ETH_P_IPV6);
		break;
	default:
		kfree_skb(skb);
		return -EINVAL;
	}
	skb->dev = snet.ndev;
	snet.ndev->stats.rx_packets++;
	snet.ndev->stats.rx_bytes += len;
	netif_rx(skb);
	return len;
}

static __poll_t s10_net_poll(struct file *f, poll_table *wait)
{
	__poll_t m = EPOLLOUT | EPOLLWRNORM;

	poll_wait(f, &snet.wq, wait);
	if (!skb_queue_empty(&snet.txq))
		m |= EPOLLIN | EPOLLRDNORM;
	return m;
}

static const struct file_operations s10_net_fops = {
	.owner = THIS_MODULE,
	.open = s10_net_open_misc,
	.release = s10_net_release_misc,
	.read = s10_net_read,
	.write = s10_net_write,
	.poll = s10_net_poll,
};

static int s10_net_init(void)
{
	int ret;

	skb_queue_head_init(&snet.txq);
	init_waitqueue_head(&snet.wq);
	snet.ndev = alloc_netdev(0, "rmnet%d", NET_NAME_ENUM, s10_net_setup);
	if (!snet.ndev)
		return -ENOMEM;
	SET_NETDEV_DEV(snet.ndev, &pdev->dev);
	ret = register_netdev(snet.ndev);
	if (ret) {
		free_netdev(snet.ndev);
		return ret;
	}
	snet.misc.minor = MISC_DYNAMIC_MINOR;
	snet.misc.name = "s10modem-net0";
	snet.misc.fops = &s10_net_fops;
	ret = misc_register(&snet.misc);
	if (ret) {
		unregister_netdev(snet.ndev);
		free_netdev(snet.ndev);
	}
	return ret;
}

static void s10_net_exit(void)
{
	misc_deregister(&snet.misc);
	unregister_netdev(snet.ndev);
	free_netdev(snet.ndev);
	skb_queue_purge(&snet.txq);
}

static int s10_probe(struct platform_device *p)
{
	return 0;
}

static struct platform_driver s10_pdrv = {
	.probe = s10_probe,
	.driver = { .name = "s10-modem" },
};

static int __init s10_init(void)
{
	int i, ret;

	ret = platform_driver_register(&s10_pdrv);
	if (ret)
		return ret;
	pdev = platform_device_register_simple("s10-modem", -1, NULL, 0);
	if (IS_ERR(pdev)) {
		ret = PTR_ERR(pdev);
		platform_driver_unregister(&s10_pdrv);
		return ret;
	}

	drv = tty_alloc_driver(NPORTS, TTY_DRIVER_REAL_RAW | TTY_DRIVER_DYNAMIC_DEV);
	if (IS_ERR(drv)) {
		ret = PTR_ERR(drv);
		goto err_pdev;
	}
	drv->driver_name = "s10-modem";
	drv->name = "ttyCP";
	drv->type = TTY_DRIVER_TYPE_SERIAL;
	drv->subtype = SERIAL_TYPE_NORMAL;
	drv->init_termios = tty_std_termios;
	drv->init_termios.c_cflag = B115200 | CS8 | CREAD | HUPCL | CLOCAL;
	drv->init_termios.c_iflag = 0;
	drv->init_termios.c_oflag = 0;
	drv->init_termios.c_lflag = 0;
	tty_set_operations(drv, &s10_tty_ops);
	ret = tty_register_driver(drv);
	if (ret)
		goto err_drv;

	for (i = 0; i < NPORTS; i++) {
		struct s10_port *p = &ports[i];
		struct device *d;

		INIT_KFIFO(p->to_daemon);
		spin_lock_init(&p->lock);
		init_waitqueue_head(&p->wq);
		tty_port_init(&p->port);
		p->port.ops = &s10_port_ops;
		d = tty_port_register_device(&p->port, drv, i, &pdev->dev);
		if (IS_ERR(d)) {
			ret = PTR_ERR(d);
			goto err_ports;
		}
		snprintf(p->misc_name, sizeof(p->misc_name), "s10modem-at%d", i);
		p->misc.minor = MISC_DYNAMIC_MINOR;
		p->misc.name = p->misc_name;
		p->misc.fops = &s10_misc_fops;
		ret = misc_register(&p->misc);
		if (ret) {
			tty_unregister_device(drv, i);
			goto err_ports;
		}
	}
	ret = s10_net_init();
	if (ret)
		goto err_ports_all;
	return 0;

err_ports_all:
	i = NPORTS;
err_ports:
	while (--i >= 0) {
		misc_deregister(&ports[i].misc);
		tty_unregister_device(drv, i);
		tty_port_destroy(&ports[i].port);
	}
	tty_unregister_driver(drv);
err_drv:
	tty_driver_kref_put(drv);
err_pdev:
	platform_device_unregister(pdev);
	platform_driver_unregister(&s10_pdrv);
	return ret;
}

static void __exit s10_exit(void)
{
	int i;

	s10_net_exit();
	for (i = 0; i < NPORTS; i++) {
		misc_deregister(&ports[i].misc);
		tty_unregister_device(drv, i);
		tty_port_destroy(&ports[i].port);
	}
	tty_unregister_driver(drv);
	tty_driver_kref_put(drv);
	platform_device_unregister(pdev);
	platform_driver_unregister(&s10_pdrv);
}

module_init(s10_init);
module_exit(s10_exit);
MODULE_LICENSE("GPL");
MODULE_DESCRIPTION("S10 Shannon modem tty bridge for ModemManager");
