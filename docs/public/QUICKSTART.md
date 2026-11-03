<!-- SPDX-License-Identifier: GPL-2.0-only -->

# Inspecting the source preview

This guide runs on a Linux development workstation. It audits and tests source without
connecting to the phone. The package is an experimental source preview, not a
supported installation image. See [release notes](RELEASE-NOTES.md) for scope.

## Verify and extract

Place the archive and its companion `.sha256` file in the same directory. For
the current local revision:

```sh
set -eu
archive=s10-public-source-preview-20261103-r2.tar.gz
sha256sum -c "$archive.sha256"
mkdir s10-source-preview-r2
tar -xzf "$archive" -C s10-source-preview-r2
cd s10-source-preview-r2
python3 scripts/publication.py audit
```

The companion checksum detects changes relative to that checksum file; it is
not a publisher signature. The audit also verifies every source file against
the archive's `SOURCE-SHA256SUMS`. It needs no Git history in an extracted archive.

## Run the hardware-free checks

Python 3.12 or newer and a native C compiler are required. The commands below
use synthetic transports and temporary files; they neither contact a SIM/network
nor execute the modem startup helpers.

```sh
python3 -m unittest discover -s tests -p 'test_*.py'
python3 userspace/vowifi/test-ims-signalling.py
python3 userspace/modem/test-daemon.py
python3 userspace/modem/test-sms.py
python3 userspace/modem/test-telephony-policy.py
python3 tests/test-suspend-phase-markers.py
python3 tests/test-suspend-finisher.py
```

The expected result is 128 Python tests plus the two native suspend-control
harnesses passing. Further native parser/photo checks have separate development
dependencies; see [build validation](BUILD-VALIDATION.md).

## Build or modify

Follow [building](BUILDING.md) for pinned upstream archives, native libraries,
the ARM64 kernel and external modules. No build helper installs or loads its
outputs. External-module staging uses the public source selection, not every
header in a development directory. Its output includes source and key kernel
input hashes, compiler/linker versions and a checksum-bound build record.

For intentional source changes, use a new Git worktree/repository with the
changed paths in its index. The source-selection tools use indexed paths plus
declared preparation files in Git, and the checked manifest in an unchanged
archive. Editing an extracted archive invalidates its original integrity record;
that is expected, not a reason to disable checksum checks.

The sensor runtime files and other proprietary dependencies are absent. The new
camera loader has host/build evidence but no device qualification. Preserve the
existing installation until a matching kernel/module build and controlled target
comparison are available. See [installation and recovery](INSTALLATION.md).
