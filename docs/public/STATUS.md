<!-- SPDX-License-Identifier: GPL-2.0-only -->

# Hardware status

Scope: SM-G973F / beyond1lte, Exynos 9820. Updated 2026-10-31. Results refer to
the development installation and are not a qualification of every operating
state, peripheral, mobile network or S10 variant.

| Component | Evidence and limitations |
| --- | --- |
| Boot | U-Boot/UEFI development installation boots; no general installer. |
| Storage | Internal storage works during normal operation. UFS resume failed in a firmware-sleep experiment. microSD remains unvalidated. |
| Display and touch | Native display, brightness and touch have worked, including display power cycling. |
| GPU | Panfrost acceleration and clock scaling have worked; long-term stability remains unqualified. |
| Desktop | Plasma Mobile is the current development desktop. |
| Cameras | Four sensors have produced still images. Focus and preview work in tested configurations. Video, OIS and variable aperture are not qualified. The source-preview driver now uses external runtime tables/programs. Its new loader is host-tested only; earlier capture results do not qualify this migration. Private color/firmware data is not distributed. |
| Audio | Playback, microphone capture and call audio have worked. Recovery after suspend is unreliable; regulator warnings remain open. |
| Wi-Fi | BCM4375 connectivity works; the sleep hook removes and reattaches the PCIe path. |
| Bluetooth | Controller initialization and discovery have worked, but initialization and suspend-related timeouts remain. Bluetooth audio is not qualified. |
| Sensors | Sensor-hub transport and orientation have worked with external firmware. |
| GNSS | Fixes have been obtained with external proprietary vendor programs; this is not a fully open GNSS implementation. |
| NFC | Basic NCI/RF and tag detection tested; general NDEF read/write not qualified. |
| LTE data | Tested with ModemManager/NetworkManager and a specific network configuration. This device is LTE, not 5G. Daemon/boot sources have been recovered; target installation and reliability remain unqualified. |
| SMS and calls | IMS/ePDG configurations have sent/received SMS and carried two-way audio. Direct LTE IMS, restart/network-change reliability and emergency calls are not qualified. |
| Battery and charging | Battery reporting and ordinary charging work. Battery and charger status can disagree. Automatic fast charging is not enabled; experimental 9-V PD work is not a release feature. |
| Suspend | s2idle is the default. Stable SYS_SLEEP is unresolved. CPU7 and UFS recovery require further investigation. |
| CPU C2 idle | A tested development configuration uses group exclusion and a hardware watchdog. This workaround is not a general reliability guarantee. |
| Other features | USB OTG has limited confirmation; fingerprint and full-device encryption are not qualified. |

No release image is currently provided. Installing these sources does not
reproduce external proprietary dependencies or locally built application trees
that have not yet been packaged and pinned.
