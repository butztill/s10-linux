<!-- SPDX-License-Identifier: GPL-2.0-only -->

# Samsung Galaxy S10 Linux port

An experimental postmarketOS port for the **Samsung Galaxy S10 SM-G973F
(beyond1lte), Exynos 9820**, using a modified Linux kernel and Plasma Mobile.

This is a development project, not a supported phone release. There is no
validated general-purpose installer or release image yet. Other S10 models,
Snapdragon devices and other mobile networks have not been validated.

## Project status

Device testing has confirmed boot, internal storage, accelerated graphics,
touch, Wi-Fi, camera capture and LTE data. SMS and two-way call audio have
worked in specific test configurations. These results are not a guarantee of
reliable telephony, suspend recovery or daily use. Emergency calling is not
validated.

**Stable deep sleep is not implemented.** The default remains s2idle. An
instrumented SYS_SLEEP attempt returned through the ARM resume path but left
CPU7 offline and produced a UFS Hibern8-exit error. Control runs without the
firmware sleep call did not reproduce those two failures. The cause remains
open; no hypervisor or secure-firmware explanation has been established.

See the [hardware status](docs/public/STATUS.md) for feature limits.

## Documentation

Start with the [source-preview quick start](docs/public/QUICKSTART.md) and
[revision notes](docs/public/RELEASE-NOTES.md).

Public documentation is written in English and describes reproducible behavior
rather than individual test conversations.

- [Hardware status and known limitations](docs/public/STATUS.md)
- [Kernel build and source layout](docs/public/BUILDING.md)
- [Host build validation and remaining source gaps](docs/public/BUILD-VALIDATION.md)
- [Installation and recovery requirements](docs/public/INSTALLATION.md)
- [Development and suspend diagnostics](docs/public/DEVELOPMENT.md)
- [Licensing, provenance and external firmware](docs/public/LICENSING.md)
- [Verified source review and remaining questions](docs/public/PROVENANCE.md)
- [Publication preparation and release checks](docs/public/RELEASING.md)
- [Current publication readiness](docs/public/RELEASE-READINESS.md)
- [Contributing](CONTRIBUTING.md)

The existing dated bring-up notes are internal research records. They are
retained in this development workspace and excluded from the public source
candidate. They are not substitutes for the public documentation.

## Source and firmware boundary

The public candidate is intended to contain open-source code, build inputs,
configuration and documentation only. It must not bundle Samsung firmware,
Android vendor executables, SIM material, credentials, device dumps or
unreviewed data extracted from proprietary libraries.

Some working device configurations still depend on externally supplied
proprietary firmware. GNSS experiments additionally use external Android vendor
programs. Those programs are not open-source components of this port and must
not be redistributed as part of it. A source-only publication does not make
the device's complete runtime software stack free software.

## Licensing and publication status

The root [LICENSE](LICENSE) contains GNU GPL version 2. Individual file notices
remain authoritative: this is a multi-license source collection, including
GPL, LGPL, permissively licensed device trees and a separately licensed
GPLv3-derived SWu implementation. Do not apply the root license to all
third-party files or combine GPLv3 components into the kernel.

**The source-only preview passes its publication audit.**
The camera register/program streams are now explicitly external runtime data;
none is embedded in the driver source candidate. The new loader builds and passes
host checks but has not been qualified on the phone. See the
[current readiness report](docs/public/RELEASE-READINESS.md) before considering deployment.

```sh
python3 scripts/publication.py audit --report artifacts/publication-audit.json
python3 -m unittest discover -s tests -p 'test_publication.py'
```

The audit must pass before exporting a source snapshot. Publishing this workspace
or its existing Git history is a separate action and has not been performed.
