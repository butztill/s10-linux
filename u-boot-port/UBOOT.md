<!-- SPDX-License-Identifier: GPL-2.0-only -->

# U-Boot reference for Exynos 9820

Upstream: https://github.com/chiffathefox/u-boot, branch `exynos9820`, commit
`dec4927b4ad5b789b0854050096fc80250627de7`.

This directory records the S10-specific device-tree/configuration files and
`uncommitted-exynos9820-common.dtsi.patch`, a local VTS pinctrl change. Preserve
the original license and copyright notices. The complete upstream tree is
required to build U-Boot; these reference files are not a bootloader image.

The development setup uses `CONFIG_EFI_VARIABLE_FILE_STORE` and stores EFI
variables in `/boot/ubootefi.var`. The project one-shot helper handles this
setup; ordinary `bootctl set-oneshot` is not a validated substitute.

The kernel device tree began from this U-Boot tree and was extended during
bring-up. The current kernel input is `dts/s10-production-20261026.dts`.
Bootloader installation and recovery are not yet a validated public procedure.
