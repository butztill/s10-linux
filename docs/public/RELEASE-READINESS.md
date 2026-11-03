<!-- SPDX-License-Identifier: GPL-2.0-only -->

# Source-preview readiness

Reviewed on 2026-11-03. **The source-only preview passes its publication audit.**
This is a local review candidate, not a published repository, release image or
supported phone installation. No new driver or runtime file has been deployed.

## Candidate boundary

The selection contains **458 source/documentation/license files** and excludes
**322 indexed/preparation files**. Ignored private work trees and Git history are
outside this inventory. No automated license/source-selection findings remain.
This is not a complete copyright/security certification or a fully free runtime.

The former four sensor source dependencies are closed by an explicit external-data
loader. All global/mode/master tables and burst bytes now come from model-specific
runtime files. Those files are not distributed; the preferred source form and
redistribution of their embedded programs remain unresolved. The public driver
contains the parser, loader and SHA-256 identities instead of program/table arrays.
It fails probe on missing or invalid data, before powering or accessing the sensor.
The new path remains **unqualified on the phone**. See the
[camera runtime boundary](CAMERA-FIRMWARE-BOUNDARY.md).

## Completed preparation

- Recovered eleven original modem source/build files and confirmed/preserved their
  GPL grants, without copying firmware, NV/calibration data or subscriber state.
- Identified ModemManager's exact package commit and archive hash. Native source
  and S10 plugin builds passed. The older divergent snapshot is not the input.
- Pinned the Phoc/wlroots source archives and checked their patches without fuzz.
  The [component manifest](../../release/component-sources.toml) records inputs
  and separates builds from patch-only/reference checks.
- Replaced broad manual gates with explicit required-source checks, component pin
  validation and a reviewed public build/test selection. One-use boot/flash/staging
  experiments and private-input prototypes remain in the development workspace.
- Added a source-only firmware conversion tool and bounded production C parser.
  All 2007 original table operations and five burst arrays match after decoding.
  All seven camera modules build without the excluded historical table headers.
- Preserved foreign copyright/license notices and recorded original/component
  grants. Restored DTS attribution with byte-identical DTB results. Standard
  license texts and their hashes are included.
- Kept firmware, vendor programs, dumps, private color extractions, raw captures,
  compiled outputs, credentials and old Git history outside the source snapshot.
- Public documentation is neutral English. Detailed origins are recorded in the
  [source review](PROVENANCE.md) and camera
  [review manifest](../../release/camera-firmware-review.json).

## Validation and limits

The recorded 2026-10-31 builds passed for the ARM64 kernel/DTB, mainline modules,
29 external ARM64 modules, native libcamera, native ModemManager and its S10 plugin.
Plasma Camera source reconstruction, Megapixels patch application and native
photo-processing checks also passed. The camera bundle subsequently built seven
modules on 2026-11-03; its compiler differs from the development kernel compiler.
This does not qualify a combined binary image or deployment ABI.

**128 Python host tests pass**: 51 publication/CRC/generator/identity/transport/
firmware checks, 28 IMS signalling, 9 daemon, 20 SMS and 20 telephony-policy checks.
Two native suspend-control harnesses and the native IP parser pass. The camera
parser's ASan/UBSan sweep covered 1459200 mutated/truncated inputs; leak scanning
was disabled because sandbox ptrace is unavailable. Full data equivalence was
checked using the production C decoder, not a Python-only reconstruction.

License checksums, shell syntax and whitespace checks pass. A clean source fixture
round-trips through export and audit without Git; changed source, missing inputs,
unsafe paths and indexed runtime firmware are rejected. The source preview is
prepared for local review only. No watchdog, boot selection, secure firmware,
phone service or installed camera driver was changed.

Before target deployment, rebuild matching kernel/modules and compare startup,
capture and power-off against the known working camera setup. A universal
installer, general recovery procedure, release image and fully free firmware
stack remain outside this source-preview qualification.

## Recheck

```sh
python3 scripts/publication.py audit --report artifacts/publication-audit.json
python3 -m unittest discover -s tests -p 'test_*.py'
python3 userspace/vowifi/test-ims-signalling.py
python3 userspace/modem/test-daemon.py
python3 userspace/modem/test-sms.py
python3 userspace/modem/test-telephony-policy.py
python3 tests/test-suspend-phase-markers.py
python3 tests/test-suspend-finisher.py
```

See [build validation](BUILD-VALIDATION.md) for the observed failures, fixes,
tool versions and practical limits. Publication or deployment is a separate action.

## Revision 2 refinements

The [quick start](QUICKSTART.md) and [release notes](RELEASE-NOTES.md) provide the
short verification path and distinguish source-preview checks from device/image
qualification. The module builder now stages only public-selected source, excludes
stale generated files, and records exact module sources plus key kernel input
hashes. It rejects changes to those kernel inputs during compilation.
A complete current 23-directory build produced 36 ARM64 modules. The
[build review](../../release/module-build-review.json) records the compile context;
it does not assert compatibility with the installed kernel. Kernel compiler
metadata handles the tab-separated generated header without copying build-owner
or hostname fields into the output record.
