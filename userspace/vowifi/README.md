<!-- SPDX-License-Identifier: GPL-2.0-only -->

# Experimental IMS/ePDG integration

`swu_emulator.py` derives from [SWu-IKEv2 by fasferraz](https://github.com/fasferraz/SWu-IKEv2).
The pinned upstream revision is `bf5d258b7298db65e64b43065b153325eba47a9e`.
Its original GPLv3 text is retained unchanged in `LICENSE`; this modified
distribution uses GPL-3.0-only. The root GPLv2 text does not replace that license.
The complete local file is reproducible from the pinned source plus
`patches/vowifi/s10-swu-integration.patch`.

The recorded adaptation guards optional smartcard/serial imports and delegates
SIM authentication operations to `usim_cp.py` through the Shannon modem. The shared original adapter is GPL-2.0-or-later, compatible with the selected
GPLv3 distribution. The tunnel is separate userspace software, not part of the kernel.
See [the source review](../../docs/public/PROVENANCE.md) for all local changes.

The tunnel runs in an `ims` network namespace. Carrier-specific ePDG, SIM and
APN settings are installation inputs and must not be committed as credentials
or examples containing real subscriber identifiers. Diagnostic output can
contain authentication keys and identifiers; retain necessary local logs with
restricted permissions and exclude them from public reports.

IMS calls and SMS have worked in specific configurations. Network changes,
restart behavior, direct LTE IMS and emergency calling are not qualified.
Consult the public [hardware status](../../docs/public/STATUS.md) and
[licensing overview](../../docs/public/LICENSING.md).
