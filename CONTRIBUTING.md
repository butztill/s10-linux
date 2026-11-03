<!-- SPDX-License-Identifier: GPL-2.0-only -->

# Contributing

Use English for maintained documentation, change descriptions and review notes.
Describe the hardware, software revision, observed behavior and remaining
uncertainty. Avoid references to individual conversations, operators or agents.
Do not describe an untested feature as working.

Preserve upstream copyright, attribution and license notices. Identify the
upstream project and revision for copied or modified code. New contributions
need an explicit compatible license and accurate copyright attribution. Do not
add another person's name, remove an existing notice or guess a license.

Keep firmware, private keys, SIM identifiers, credentials, binary dumps,
proprietary decompilation and unreviewed vendor-derived tables out of public
changes. Generated outputs should have documented source and build commands;
build them during release preparation instead of checking them in as unexplained
binaries. See [licensing](docs/public/LICENSING.md).

Include a focused validation summary. Hardware changes must identify the exact
device variant and recovery method. Run relevant host tests before a device
experiment. Preserve unrelated work and keep experimental behavior disabled by
default. Changes to boot defaults or firmware are not ordinary test setup.

Before preparing a public candidate, run the publication audit and inspect its
report. A clean automated report does not replace provenance review or hardware
validation. Do not publish the existing development Git history as a shortcut.

The external-module builder follows the public source inventory. New source paths
must be indexed or declared as preparation inputs; private workspace files are not
staged implicitly. Extracted archives are immutable checksum references. Use a
new Git checkout/repository for intentional edits rather than weakening archive
integrity checks.
