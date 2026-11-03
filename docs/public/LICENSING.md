<!-- SPDX-License-Identifier: GPL-2.0-only -->

# Licensing and source provenance

This project is a multi-license collection. The root `LICENSE` contains GNU
GPL version 2; it does not relicense independently licensed dependencies.
Existing file-level notices and upstream copyright statements take precedence.
Public documentation added for release preparation is GPL-2.0-only.

## Components

| Component | Current evidence / release requirement |
| --- | --- |
| Linux overlay and external drivers | Preserve file notices and identify base/upstream revisions. Linux-derived material is not automatically original port code. Missing or ambiguous notices need review. |
| U-Boot device-tree reference | Preserve the original dual-license expressions and contributor attribution. Record the upstream revision and local patch. |
| libcamera, desktop and compositor patches | Record the exact upstream source and component license for each patch; copied context and generated data retain their own provenance. |
| SWu implementation | `userspace/vowifi/swu_emulator.py` is pinned to fasferraz/SWu-IKEv2 with its original GPLv3 text retained and a reproducible integration patch. This modified distribution uses GPL-3.0-only; the shared original SIM adapter is GPL-2.0-or-later. These are not GPLv2-relicensed kernel components. |
| Port scripts, services and configuration | Reviewed original integration now has explicit identifiers under the existing project grant. Imported source and component patches retain their own notices; an identifier does not by itself establish complete source availability. |
| Camera sensor/register tables | Four Samsung/LineageOS inputs and the generated outputs are verified. Opaque program/table payloads now use separate external runtime files; legacy headers remain excluded. Only the loader and identity metadata are in the source candidate. Preferred program source and runtime-data redistribution remain unresolved. |
| Camera color/tuning data | Some data was extracted from proprietary camera libraries. Redistribution has not been established. It is blocked from the public candidate pending review or independently sourced replacement. |
| Firmware and vendor executables | Not part of the public source candidate. Do not bundle firmware, Android GNSS daemons, vendor shared libraries, secure-firmware images or decompiled proprietary code. |

GPLv2, GPLv3 and LGPL components must not be described as one uniformly
GPLv2-only combined work. Their separation, linking and distribution conditions
need component-specific review. Retain the original copyright notices, include
the applicable license texts and provide corresponding source when distributing
binaries under licenses that require it.

`LICENSES/` contains standard license texts from SPDX license-list-data
v3.29.0 used by identified components. `release/license-texts.sha256` records
their checksums. The
publication audit inventories SPDX expressions and checks for missing texts;
it is not a legal opinion, a complete SBOM or a claim of REUSE compliance.
Manual review must establish ownership, the accuracy of each grant and the
rights to distribute derived material.

## External runtime requirements

The phone still uses proprietary device firmware. GNSS experiments also use
external proprietary Android programs. Those dependencies are not relicensed by
this project. A public source release must describe them accurately and must not
promise a completely free runtime stack. No proprietary packages are downloaded
or included by the publication tool.

## Review references

- [SPDX license identifiers](https://spdx.org/licenses/)
- [REUSE per-file licensing specification](https://reuse.software/spec-3.3/)
- [SWu-IKEv2 upstream](https://github.com/fasferraz/SWu-IKEv2)

The source-preview audit passes after externalizing camera runtime data. Unresolved
embedded-program source form does not authorize distributing those external files. Do not replace them with a blanket
license declaration.

The [source review](PROVENANCE.md) records verified revisions, retained notices,
component-specific grants and the remaining embedded-payload questions.
