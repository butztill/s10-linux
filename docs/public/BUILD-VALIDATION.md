<!-- SPDX-License-Identifier: GPL-2.0-only -->

# Host build validation

Builds recorded on 2026-10-31; audit and host checks refreshed on 2026-11-03.
These checks do not qualify a device or a release image. Nothing was installed
on the phone.

## Kernel

A fresh extraction of Linux commit `8cdeaa50eae8dad34885515f62559ee83e7e8dda`
passed the recorded uncompressed-tar SHA-256 and archive-commit checks. The
current overlay/configuration built an ARM64 Image and production DTB with
Clang/LLD 23.1.4, GNU Make 4.4.1 and dtc 1.8.1.

| Output | SHA-256 |
| --- | --- |
| Image | e75aad274a8f9ac47e4b58ef630b92bf3539645d419fdf2a723bb9042749f485 |
| Production DTB | 0d8bd0d2a8912753c8cb604f0a7b8fc9258e8a371598efa71876f01434731ecb |

The historical baseline used Clang/LLD 23.1.3 and different source inputs.
These results do not claim bit-identical reproduction of that older Image.
One existing `-Wmissing-prototypes` warning remains for
`s10_acpm_dvfs_ap_state` in `drivers/firmware/samsung/exynos-acpm.c`.
The DTS attribution headers were subsequently restored and both compiled
DTBs checked byte-for-byte against their pre-notice versions; behavior is unchanged.

The build helper accepts a checked cached archive through `S10_LINUX_ARCHIVE`,
limits parallelism with `S10_JOBS`, records actual tools and rejects an existing
unverified source directory. Use a new directory for a clean-source check.

## libcamera

The v0.7.2 tag resolves to commit
`191e202178f02430b5942397c70d215cdd2056fa`. The pinned archive hash and public
series are recorded in [component inputs](../../release/component-sources.toml)
and [libcamera.series](../../release/libcamera.series).
All selected patches applied without fuzz. A native x86-64 build completed for
the simple pipeline/IPA, software ISP, camera tools and V4L2 compatibility code.
This is not an ARM64 deployment or a new camera image-quality validation.

The host needed Python `ply` 3.11 in addition to its existing build dependencies.
It was installed only in a temporary virtual environment. Use the same active
Python environment for Meson and Ninja. A first run without `ply` failed; no
source workaround was used. The sandboxed host's ordinary ccache directory was
unwritable, so the successful build used `CCACHE_DISABLE=1`.

No proprietary-reference color tables were used. Their dependent exposure patch
is also absent. Sensor driver headers with opaque embedded program payloads
remain excluded, so this does not establish complete camera-driver source.

## Application patches

The old four-part Plasma Camera sequence contained duplicate header changes
and did not apply cleanly as a series. The reviewed public consolidated patch
now applies without fuzz to commit `29d46b59e2159f90b0eeb9bca94906e667cfaecf`
and reproduces all 16 current modified source files byte-for-byte. Earlier
patches remain local historical material and are excluded from the candidate.
The photo-processing host check builds directly from this reconstructed source;
it checks noise reduction, edges, color, image ownership and output dimensions.
The complete GUI and phone camera lifecycle have not been requalified here.

The unpacked RAW10 Megapixels patch applies without fuzz to commit
`578b1474e1c58829874c1a2c06844d34c9c82c56`. No complete Megapixels build or
hardware test is claimed. Unverified component checks remain explicit in the
[component manifest](../../release/component-sources.toml).

## Recovered modem and component inputs

The read-only USB source recovery on 2026-10-31 restored eleven modem source/build
files, including the three imported Python modules, the boot and TTY/network
modules and four startup helpers. New original-source GPL-2.0-only grants are
recorded; existing GPL notices were preserved. No firmware, NV files or subscriber
state was recovered. Startup helpers were not executed.

ModemManager package `1.25.95_git20261005` maps to commit
`7f0bf17ab518cd78925cf7e53cc0d5c42f88b41a`. The public archive matches the Alpine
recipe SHA-512. The old development snapshot has 875 identical files and twelve
different files; the pinned package source is the release input. A native build
of ModemManager and the S10 plugin passed against that public source. The plugin
helper now uses explicit source/build/output arguments and handles an already
enabled `WITH_SUSPEND_RESUME` without macro-redefinition errors.

Phoc 0.57.0 was downloaded and matched the recorded archive hash. Its S10 patch
applies without fuzz. wlroots 0.20.2 is now pinned by archive SHA-256 and both S10
patches apply without fuzz. Full compositor/device requalification is separate.

## ARM64 external modules

The pinned kernel's mainline modules completed successfully. The public module
builder staged sources from 22 reviewed module directories and built **29 ARM64
modules**, including the recovered modem boot and TTY/network modules. It copies
no results to the phone and does not load modules. Builds retain warnings,
including the recovered CP module's unused `evt_count`; warning-free compilation
is not claimed. Module binary reproducibility is not claimed for temporary stage
paths. The output SHA-256 manifest belongs to the specific host build.

`release/modules.list` is the explicit build selection. The sensor V4L2 driver was outside the original 22-directory list. It has now
been added after the separate external-data migration build described below.
Historical camera-capture/identification prototypes requiring private inputs are
also excluded; the ordinary clock/control modules remain buildable.
The portable CSIS-settle generator reproduces its existing 643-byte timing table
exactly when supplied the pinned reference header and an explicit output path.

## Camera external-data migration, 2026-11-03

The four source dependencies are closed: the driver no longer includes the legacy
payload-bearing headers. All table/program streams use private/external runtime
containers. Only loader/parser code and content identities are selected for source
export. The original embedded-program source/redistribution question is not waived.

The production C parser and actual driver decoder reproduce 2007 table operations
and all five burst arrays across the four models exactly. Ten new host cases check
valid layouts, truncation, identity/version errors, malformed offsets/counts,
invalid operands, allocation failure and native register decoding. ASan/UBSan
exercised 1459200 mutated/truncated inputs without errors; leak scanning was disabled
because this sandbox cannot use ptrace. No sandbox/kernel setting was changed.

All seven camera modules built for ARM64 from a staging tree without the old
headers. The first compile identified a missing `linux/hex.h` include; adding the
proper declaration fixed it. This build used the existing 7.2.0-rc2 development tree,
recorded as Clang 23.1.3, with compiler 23.1.4. It is not an installed-kernel ABI
qualification. No new module or runtime file was loaded or installed on the phone.
A matching full rebuild and controlled capture/power comparison remain required
before target deployment. Source hashes and per-model checks are in the
[camera review manifest](../../release/camera-firmware-review.json).

The current source-only audit passes. Exported source can be re-audited without
Git using its checksum manifest; altered/missing files and path escapes fail.
Firmware `.s5k` files are explicitly excluded even if accidentally indexed.
The public tests use synthetic runtime data and require no private firmware files.
The original sensor-data question is now an external runtime boundary rather than
missing C build inputs. See [the detailed boundary](CAMERA-FIRMWARE-BOUNDARY.md).

## Public-staging refinement, revision 2

The external-module builder previously staged every C/header file in a selected
development directory, including unselected historical/private headers. It now
uses the public source inventory. Tests cover exclusion of private/stale generated
sources, source hashes, symlinks and traversal paths. A generated-kernel-header
whitespace regression also checks compiler metadata without copying build-owner
or hostname declarations.

The refined builder built all 36 modules from 23 directories in one local run.
The four recorded kernel input files remained unchanged throughout compilation,
and no legacy payload table headers were staged. The output includes a
checksum-bound `BUILD-INFO.json` with 69 selected source identities, kernel release,
key kernel input hashes and compiler/linker versions. The native compiler remains
23.1.4 while the kernel records 23.1.3; this is compile evidence, not an installed
ABI or hardware qualification. See the
[module build record](../../release/module-build-review.json).
