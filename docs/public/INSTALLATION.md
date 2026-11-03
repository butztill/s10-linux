<!-- SPDX-License-Identifier: GPL-2.0-only -->

# Installation and recovery

There is no supported general-purpose installer or validated public image yet.
The development setup uses an unlocked SM-G973F, U-Boot/UEFI, postmarketOS and
an existing boot partition. Instructions for an arbitrary stock phone cannot
be inferred from that setup.

Before installation documentation can be released, it must cover the exact
supported model, bootloader prerequisites, partition layout, recovery entry,
source and checksums of every image, firmware acquisition rights, and a tested
return to a known working system. Keep a backup appropriate to the installation
method. These sources are not an invitation to flash another S10 variant.

## Existing development installations

Keep a known working default boot entry. Test a checksum-verified kernel through
a one-shot entry and inspect the resulting selection before reboot. The
current U-Boot setup stores EFI variables in a file; ordinary `bootctl
set-oneshot` is not a validated replacement for the project helper.

Boot entries in `phone-rootfs/boot/` describe one existing installation. Their
partition identifiers and image paths must be replaced for another device.
They are not universal defaults and are excluded from the public candidate.

Do not flash or modify secure firmware or the bootloader to investigate an
unexplained suspend failure. Preserve the default s2idle path while deep sleep
is under development.

## Source-preview camera change

The source preview contains a new external-data sensor loader. It has compiled and
passed host stream checks, but it has not replaced the working installed driver.
Missing or invalid runtime files fail sensor probe. Do not interpret the source
audit or a native build as device compatibility. Review the
[camera runtime boundary](CAMERA-FIRMWARE-BOUNDARY.md), rebuild matching kernel/modules
and qualify target startup/capture/power-off before deployment.
