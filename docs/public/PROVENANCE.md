<!-- SPDX-License-Identifier: GPL-2.0-only -->

# Source provenance review

Reviewed 2026-10-31. This records verified source relationships, the precise
scope of license annotations and the remaining publication blockers. No
repository was published and no device configuration was changed.

## Existing project grants and integration files

Commit `723d22308ab7155d3c2458987eea99eefd198cfe` introduced the root GPLv2
license and explicitly scoped it to original project work while preserving
third-party notices. That grant was used for reviewed S10-specific service,
udev/modprobe/WirePlumber integration and external-module Kbuild files. Generic
distribution configuration was deliberately not covered by this annotation.
No contributor name or copyright ownership was invented.

Together with the two MIT CLAT copies, 63 files received identifiers matching
reviewed existing grants. The pre-annotation contents and operational lines
were preserved. Local before/after hashes are retained in the review artifacts.
Generic GeoClue configuration and blacklist copies are excluded; the project
default has not been assigned to those imported distribution files.

Historical boot-default restoration services and a one-off USB deployment
service are excluded from the public selection. They target old development
boot entries and are not portable production integration.

## Further file-level review

The subsequent review added existing-grant identifiers to 90 original runtime,
service, test and build inputs, and 134 original project diagnostic/build helpers.
Imported patches, upstream files and flattened device trees were handled
separately. Generated-output notices inside generators were preserved; their
presence was not treated as a grant for the surrounding script.

Four videobuf2 C files are byte-identical to the pinned Linux base, as well as
the existing development kernel copy. Their legacy GPL notices remain untouched;
sidecars record the GPLv2 distribution under Linux's COPYING. Sidecars also cover
kernel metadata/configuration and the U-Boot configuration without inserting
comments into imported source files. Linux-derived port patches use the existing
project GPLv2 grant and retain upstream context/notices.

Original libcamera, Plasma Camera, Megapixels, Phoc and wlroots changes are
explicitly authorized under the affected upstream file licenses. Their patch
metadata now records those choices, including mixed-file AND expressions where
necessary. The wlroots patches retain the complete upstream MIT notice. This
permission does not change foreign copyright ownership or license choices.
The standard CC0 and GPL-3.0-or-later texts and their hashes are included.

Generic distribution blacklist and GeoClue defaults are excluded. They are not
S10 source components and must not overwrite another installation's defaults.
For the existing GNSS integration, configure the target GeoClue network-NMEA
source to use the service's `/var/run/gps-share.sock` socket while preserving the
target distribution's own configuration. The location schema override remains
separate. This is not a general GNSS installer or a claim of an open vendor stack.
Stock extraction/initrd research helpers and color-extraction-dependent tests
are excluded with their private fixtures.

## CLAT

The two copies at `userspace/modem/vendor/clatd` and
`phone-rootfs/usr/local/sbin/s10-clatd` retain Tore Anderson's full MIT notice.
They were compared with [clatd v2.1.0](https://github.com/toreanderson/clatd/blob/5e085f1b1730cb4133623d5738fd447199baf58e/clatd).
The only pre-existing local difference is one diagnostic sentence. SPDX MIT
identifiers were added without removing or rewriting the original notice.
These files are not GPLv2-relicensed project code.

## SIM adapter and SWu

`userspace/vowifi/usim_cp.py` is confirmed original project code and is now
explicitly licensed **GPL-2.0-or-later**. It is imported directly by both the
GPLv2 IMS client and the GPLv3 SWu process. The later-version option permits
the appropriate version choice in each combination; the kernel remains under
its existing licenses. See the [GNU compatibility explanation](https://www.gnu.org/licenses/gpl-faq.en.html#v2v3Compatibility).
The publication policy checks this component-specific grant so it cannot
silently regress to a GPLv2-only adapter.

The modified SWu implementation derives from
[fasferraz/SWu-IKEv2](https://github.com/fasferraz/SWu-IKEv2/tree/bf5d258b7298db65e64b43065b153325eba47a9e)
at `bf5d258b7298db65e64b43065b153325eba47a9e`.
The complete upstream Python source has SHA-256 `bd564620ff1deaac1f19041c32223e707fb4e277998759d3aef6847bceef4872`.
The local `userspace/vowifi/LICENSE` is byte-identical to that revision's GPLv3
text. This modified distribution selects GPL version 3 and is identified as
GPL-3.0-only; it does not claim GPLv2 permission or additional later-version
permission for the upstream code.

Applying [the local integration patch](../../patches/vowifi/s10-swu-integration.patch)
to that pinned original reproduces the complete modified file byte-for-byte.
Patch SHA-256: `cf85d780e77425db875f38c32e23e84c5493d10cc7722fb11d73f54086b84235`. Modifications include optional dependency
imports, the CP authentication bridge, IPv6 address setup, empty INFORMATIONAL
liveness handling, NAT keepalives, non-interactive stdin handling and the
multiprocessing start method. They are more extensive than the old bring-up
note's two-change description.

The upstream example IMEI/IMEISV literals match the pinned public original.
The separate IMS-signalling test uses synthetic fixtures and mocked network/SIM
access. Hash-bound review records identify these examples; they do not waive
private-key checks or approve their use as real device identities. The minimal
IMS client no longer defaults to a fabricated device identity: it obtains one
from the modem or requires an explicit 15-digit value, otherwise failing before
SIM authentication or socket creation.

## Camera register data and embedded payloads

Four [Samsung/LineageOS source headers](https://github.com/LineageOS/android_kernel_samsung_exynos9820/tree/4bcfb0263b21b553760a3bdce542452db3184820/drivers/media/platform/exynos/fimc-is2/sensor/module_framework/cis)
were downloaded at `4bcfb0263b21b553760a3bdce542452db3184820` and matched the
locally pinned input hashes. All contain Samsung copyright and explicit GPLv2
notices. The table generator reproduces the four current headers byte-for-byte.

| Input | SHA-256 | Emitted byte-array sizes |
| --- | --- | --- |
| fimc-is-cis-3p9-setA.h | 59e9e8cb37226cf3b25400e3180f6a0ab8d96ceada3164a0b3fa5a89156afac5 | 562 |
| fimc-is-cis-3m3-setA.h | f1f2c003e0dee60525a6b5bfe35a17a06969d56aa090102f5479fb0ab6130c34 | 830 |
| fimc-is-cis-3j1-setA.h | 400fb51211eea00dc9d99c93657d750e72d4f9a156ba7f134a9770374badffe2 | 402 |
| fimc-is-cis-2l4-setA.h | 858bcedcff68dcd4671a09234a959a36a82ded6b5903ca6f07fa448c95362c7b | 42 and 3954 |

This verifies origin and generation, not the preferred source form of every
embedded program payload. The sources include a TnP sensor patch and opaque
burst data. An outer GPL header alone does not demonstrate that the embedded
firmware's editable source has been provided. The four generated headers and
the separate 3P9 diagnostic table are therefore excluded pending source-form
review or separation into externally supplied firmware. They are not labelled
proprietary solely because they contain byte arrays.

The original payload-bearing headers remain excluded. The driver has now been
adapted to explicitly external runtime data; its source compiles without those
headers, while the data files themselves are not distributed. Proprietary-library color matrices remain excluded separately, together with
the exposure follow-up patch that depends on their header. The checked public
libcamera series builds without those inputs.
The initial provenance review changed no driver behavior. The subsequent external
loader migration changes the local source/runtime interface, but no installed
phone file or device configuration was changed. The generator now supports explicit reference/output directories and
can be imported without writing into the driver tree.

```sh
python3 scripts/prepare-s5k-tables.py \
  --reference-dir /path/to/reviewed/gpl/headers \
  --output-dir /path/to/local/generated-headers
```

## PMU table

`kernel/overlay/drivers/soc/samsung/exynos9820-sleep-seq.h` reproduced exactly
from Samsung's `SM-G973F_EUR_12_Opensource.zip` package:

- Package SHA-256: `9ffe68b6789d5d0c2ff142faf70d770d2543664e4cacf6d7272d9a7d6628b354`.
- Input: `kernel/drivers/soc/samsung/cal-if/exynos9820/flexpmu_cal_system_exynos9820.h`.
- Input SHA-256: `187e78ce5724db88b9b6ad74f27a57d28eaa9ad1a01932ca60ba0910c0e7b517`.
- License evidence: the package's kernel `COPYING` supplies GPLv2.
- Generator: `scripts/gen-exynos9820-sleep-seq.py` (its hash is bound to the review record).

```sh
python3 scripts/gen-exynos9820-sleep-seq.py \
  /path/to/gpl/kernel/drivers/soc/samsung/cal-if/exynos9820/flexpmu_cal_system_exynos9820.h \
  > /path/to/local/pmu-regenerated.h
```

The PMU output is a register sequence. This review does not generalize its
conclusion to camera firmware payloads or arbitrary data from the same package.

## History and remaining work

A limited pattern review covered 30 reachable commits and 768 unique Git blobs.
No private-key marker matched. Device serial properties remain in seven old
DTS/DTB blob versions. This is not a full credential or copyright certification,
and the existing history remains unsuitable for direct publication. The public
export deliberately contains no Git history.

## Device-tree source chain

The source of the original live-tree capture is now established by a semantic
comparison. Exact device-tree files were extracted from chiffathefox/u-boot
commit `dec4927b4ad5b789b0854050096fc80250627de7`, preprocessed and compiled
again. All 311 source nodes and their properties match the original capture.
The only differences are a runtime `/memory` node and the `serial-number` and
`u-boot,version` boot properties. No source nodes were removed. Matched property
values are not copied into public diagnostics.

The repository's bring-up history records the subsequent local transformations
from this baseline. Both flattened working DTS inputs now retain the original
2025/2026 chiffathefox copyright notices. GPL version 2 is selected from the
upstream dual GPL/BSD grant, consistent with the included GPL-only TMU source
and local GPLv2 changes. Adding these notices leaves both compiled DTBs
byte-identical. Serial properties and stale boot pointers are absent from the
candidate. The raw capture and historical DTBs remain private and excluded.

## Remaining release boundaries

No file-level SPDX gaps remain in the current selected tree. That does not
certify complete copyright/secret review or complete corresponding source.
The modem dependencies and startup sources have now been recovered, with original
GPL grants preserved or explicitly confirmed. The source recovery manifest records
eleven files without copying firmware or mutable modem data. ModemManager's package
source commit and SHA-512 are identified; native application/plugin builds passed.
Phoc and wlroots source archives are pinned and their patches apply without fuzz.

The public tool selection now contains the checked build, preparation and audit
entry points, selected host tests and data generators with explicit input paths.
Historical boot/flash/staging scripts remain in the development workspace but are
not shipped. Two obsolete camera-control prototypes and the private-regdump-based
main-camera diagnostic are also deferred; ordinary camera clock/control modules
remain in the public module build. This is source-selection cleanup, not a phone
configuration change.

Those four source-build gaps are closed by the external-data loader. The
embedded-program source/redistribution question remains outside the source archive. They have not been relabelled as proprietary merely
because they contain byte arrays, and their outer GPL notices do not establish
the editable program source. See the
[camera source boundary](CAMERA-FIRMWARE-BOUNDARY.md),
[component manifest](../../release/component-sources.toml),
[build validation](BUILD-VALIDATION.md) and
[remaining-source worksheet](../../release/metadata-review.tsv).

`release/reviewed-files.json` stores narrow decisions with file/input hashes
and evidence. Changed contents invalidate a decision; these records cannot
waive a private key or an embedded-payload review. They are provenance records,
not a replacement for license grants or a legal clearance certificate.
