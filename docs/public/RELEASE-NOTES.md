<!-- SPDX-License-Identifier: GPL-2.0-only -->

# Experimental source preview: 2026-11-03, revision 2

Status: local review candidate. This is source for the Exynos SM-G973F port,
with English documentation, component licenses and recorded upstream inputs.
It contains no Git history, firmware files, vendor executables or release image.
Use the [quick start](QUICKSTART.md) to verify and inspect the package.

## Changes in this revision

- External-module staging now copies only selected public source. Private legacy
  headers and stale generated module sources cannot silently enter the build.
- The builder records module source hashes, key kernel input hashes and actual
  tool versions in `BUILD-INFO.json`, covered by the output checksum manifest.
  It refuses to emit products if those kernel inputs change during the build.
- A complete local build of the current 23-directory selection produced 36 ARM64
  modules. The compiler mismatch with the existing kernel is recorded; deployment
  ABI compatibility is not claimed.
- The package includes a short verification guide and regression checks for the
  module staging boundary. All 128 Python host checks pass.

The preceding source preview introduced the external sensor-data loader,
recovered modem sources/startup helpers, fixed component source pins and portable
build entry points. Its camera parser/decoder reproduced the original table
operations and burst bytes; the private runtime data was not distributed.

## Known limits

The new camera loader has not been installed or tested on the phone. It requires
four exact external runtime files and fails probe if they are absent or invalid.
Their embedded program source form/redistribution remains unresolved. Existing
capture results describe the earlier installed setup, not this migration.

Stable SYS_SLEEP remains unresolved; s2idle is the normal baseline. General
installation/recovery, emergency calling, a release image and a fully free
runtime stack are not qualified. See [hardware status](STATUS.md),
[source-preview readiness](RELEASE-READINESS.md) and
[camera runtime data](CAMERA-FIRMWARE-BOUNDARY.md).

Nothing was published, flashed or installed by creating this candidate.
