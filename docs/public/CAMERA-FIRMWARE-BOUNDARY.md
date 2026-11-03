<!-- SPDX-License-Identifier: GPL-2.0-only -->

# External camera runtime data

Updated on 2026-11-03. The sensor driver no longer compiles any of the four
payload-bearing table headers into its module. All global/mode/master tables
and burst data are now loaded from a separate, model-specific runtime file.
The public source contains the loader, bounded parser, generator, tests and
SHA-256 identities. **The new loader has not been tested on the phone.**

| Sensor | Required runtime filename | Bytes | Tables / operations / bursts |
| --- | --- | --- | --- |
| S5K3P9 | `samsung/s10/s5k3p9.s5k` | 1195 | 3 / 113 / 1 |
| S5K3M3 | `samsung/s10/s5k3m3.s5k` | 1658 | 3 / 152 / 1 |
| S5K3J1 | `samsung/s10/s5k3j1.s5k` | 4207 | 4 / 745 / 1 |
| S5K2L4 | `samsung/s10/s5k2l4.s5k` | 9057 | 3 / 997 / 2 |

The runtime files are **not distributed** in the source candidate. Reference
header provenance is verified, but preferred source form and redistribution of
the embedded sensor programs remain unresolved. Moving the files outside the
module does not create a new firmware grant. Existing proprietary-reference
color inputs and historical payload headers remain excluded, as does Git history.

## Loader behavior

The driver requests the exact model filename through the kernel firmware API,
with no userspace fallback. It checks the container model/revision against the binding, format, table and mode
counts, reserved fields, offsets, widths, delay/burst operands, total size and
SHA-256 identity before power/pinctrl acquisition or sensor access. Missing or
invalid data fails probe. The existing hardware ID/revision check after power-on
is retained. There is no empty-table or embedded-data fallback.

All register streams are external, including ordinary-looking scalar writes;
only model geometry, clocks, controls and the driver logic remain in source.
The generated identity header contains hashes rather than register/program bytes.
Validated entries are decoded into native per-device tables. Burst storage stays
owned by the firmware object until device-managed cleanup. Failed allocations
return an error; they do not start a partially initialized sensor.

## Container format, version 1

Every integer below is little-endian. Offsets are absolute file offsets.

- 24-byte header: magic `S10S5K1` followed by NUL; six u16 fields (version,
  model, revision, table count, burst count, zero reserved); u32 total size.
- One 12-byte descriptor per table: u16 ID, u16 zero reserved, u32 offset,
  u32 entry count. Order is global (ID 0), modes (IDs 0x100 onward), then
  optional master (ID 1).
- One 8-byte descriptor per burst: u32 offset and u32 byte count.
- Each table entry is five bytes: u16 register, u16 value, u8 width. Width is
  1 or 2 for writes; 0xffff is a microsecond delay and 0xfffe a burst index,
  both with width zero. Delays are bounded at 65000 microseconds.
- Table data followed by burst bytes must be contiguous, non-overlapping and
  consume the complete file. Bursts contain their original I2C register prefix.
  Empty tables, holes, aliases, trailing bytes and out-of-range operands fail.

The current parser caps files at 1 MiB, modes at two, bursts at sixteen and
individual bursts at 65535 bytes. Hash pinning restricts accepted content to the
specific reviewed conversion, not arbitrary syntactically valid register programs.

## Preparation and verification

For a local reference set whose use is authorized:

```sh
python3 scripts/prepare-s5k-firmware.py \
  --reference-dir /path/to/pinned/reference-headers \
  --output-dir /path/to/new/private-runtime-directory
```

The generator verifies all four source hashes and refuses existing output.
It does not download, install or publish data. Its identity header must match
`drivers/s10-camera-v4l2/s5k-fw-manifest.h`. Runtime files belong under the target
firmware search path only after a separate target-compatibility review; this
command is not an installation procedure.

The production C parser and actual driver decoder reproduced all 2007 original
table operations and five burst byte arrays exactly. Synthetic tests cover
truncation, identity/format errors, overlap, overflow, invalid operands and
allocation failure. A native ASan/UBSan mutation sweep passed; leak scanning was
disabled because the sandbox disallows ptrace. All seven camera modules built
for ARM64 without the legacy headers. Source hashes and per-model results are
recorded in [the review manifest](../../release/camera-firmware-review.json).

These are host/source checks. The build used compiler 23.1.4 against a development
kernel recorded as 23.1.3; it is not an installed-image ABI qualification. Before
deployment, rebuild the matching kernel/modules and compare sensor startup,
capture and power-off behavior against the existing working configuration.
No camera module, firmware file, boot setting or secure firmware was changed on
the S10 during this migration.
