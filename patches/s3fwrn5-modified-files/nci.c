// SPDX-License-Identifier: GPL-2.0-or-later
/*
 * NCI based driver for Samsung S3FWRN5 NFC chip
 *
 * Copyright (C) 2015 Samsung Electronics
 * Robert Baldyga <r.baldyga@samsung.com>
 */

#include <linux/completion.h>
#include <linux/module.h>
#include <linux/firmware.h>

#include "s3fwrn5.h"
#include "nci.h"

static int s3fwrn5_nci_prop_rsp(struct nci_dev *ndev, struct sk_buff *skb)
{
	__u8 status = skb->data[0];

	nci_req_complete(ndev, status);
	return 0;
}

const struct nci_driver_ops s3fwrn5_nci_prop_ops[4] = {
	{
		.opcode = nci_opcode_pack(NCI_GID_PROPRIETARY,
				NCI_PROP_SET_RFREG),
		.rsp = s3fwrn5_nci_prop_rsp,
	},
	{
		.opcode = nci_opcode_pack(NCI_GID_PROPRIETARY,
				NCI_PROP_START_RFREG),
		.rsp = s3fwrn5_nci_prop_rsp,
	},
	{
		.opcode = nci_opcode_pack(NCI_GID_PROPRIETARY,
				NCI_PROP_STOP_RFREG),
		.rsp = s3fwrn5_nci_prop_rsp,
	},
	{
		.opcode = nci_opcode_pack(NCI_GID_PROPRIETARY,
				NCI_PROP_FW_CFG),
		.rsp = s3fwrn5_nci_prop_rsp,
	},
};

#define S3FWRN5_RFREG_SECTION_SIZE 252

static unsigned int fwcfg_len = 1;
module_param(fwcfg_len, uint, 0644);
static unsigned int fwcfg[3] = { 0x01, 0x12, 0x01 };
module_param_array_named(fwcfg, fwcfg, uint, NULL, 0644);
MODULE_PARM_DESC(fwcfg, "FW_CFG clk type, speed, req");

int s3fwrn5_nci_rf_configure(struct s3fwrn5_info *info, const char *fw_name)
{
	struct device *dev = &info->ndev->nfc_dev->dev;
	const struct firmware *fw;
	struct nci_prop_fw_cfg_cmd fw_cfg;
	struct nci_prop_set_rfreg_cmd set_rfreg;
	struct nci_prop_stop_rfreg_cmd stop_rfreg;
	u32 checksum;
	int i, len;
	int ret;

	ret = request_firmware_direct(&fw, fw_name, dev);
	if (ret < 0)
		return ret;

	/* Compute rfreg checksum */

	checksum = 0;
	for (i = 0; i < fw->size; i += 4)
		checksum += *((u32 *)(fw->data+i));

	/* Set default clock configuration for external crystal */

	fw_cfg.clk_type = fwcfg[0];
	fw_cfg.clk_speed = fwcfg[1];
	fw_cfg.clk_req = fwcfg[2];
	fw_cfg.clk_type = fwcfg[0];
	ret = nci_prop_cmd(info->ndev, NCI_PROP_FW_CFG,
		fwcfg_len, (fwcfg_len == 1) ? (__u8 *)&fw_cfg.clk_speed : (__u8 *)&fw_cfg);
	if (ret < 0)
		dev_warn(dev, "clock configuration rejected (%d)\n", ret);

	/* Start rfreg configuration */

	dev_info(dev, "rfreg configuration update: %s\n", fw_name);

	ret = nci_prop_cmd(info->ndev, NCI_PROP_START_RFREG, 0, NULL);
	if (ret < 0) {
		dev_err(dev, "Unable to start rfreg update\n");
		goto out;
	}

	/* Update rfreg */

	set_rfreg.index = 0;
	for (i = 0; i < fw->size; i += S3FWRN5_RFREG_SECTION_SIZE) {
		len = (fw->size - i < S3FWRN5_RFREG_SECTION_SIZE) ?
			(fw->size - i) : S3FWRN5_RFREG_SECTION_SIZE;
		memcpy(set_rfreg.data, fw->data+i, len);
		ret = nci_prop_cmd(info->ndev, NCI_PROP_SET_RFREG,
			len+1, (__u8 *)&set_rfreg);
		if (ret < 0) {
			dev_err(dev, "rfreg update error (code=%d)\n", ret);
			goto out;
		}
		set_rfreg.index++;
	}

	/* Finish rfreg configuration */

	stop_rfreg.checksum = checksum & 0xffff;
	ret = nci_prop_cmd(info->ndev, NCI_PROP_STOP_RFREG,
		sizeof(stop_rfreg), (__u8 *)&stop_rfreg);
	if (ret < 0) {
		dev_err(dev, "Unable to stop rfreg update\n");
		goto out;
	}

	dev_info(dev, "rfreg configuration update: success\n");
out:
	release_firmware(fw);
	return ret;
}
