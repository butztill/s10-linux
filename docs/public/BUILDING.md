<!-- SPDX-License-Identifier: GPL-2.0-only -->

# Building the kernel

## Inputs

- Base: Linux 7.2-rc2, commit `8cdeaa50eae8dad34885515f62559ee83e7e8dda`.
- Overlay: `kernel/overlay/`.
- Configuration: `kernel/config-7.2.0-rc2-s10-panfrost1`.
- Production device-tree source: `dts/s10-production-20261026.dts`.
- Build environment: `kernel/BUILD-ENV.txt`.

The source overlay contains upstream and port-specific files. Preserve their
individual notices. A complete corresponding-source distribution also needs
the pinned base source and the inputs for any separately distributed modules
and applications; this repository alone is not a complete system image.

## Build command

From the repository root:

```sh
bash scripts/build-kernel-reproducible.sh build-kernel
```

The script downloads and verifies the base archive, copies the overlay, uses
the active Clang/LLD toolchain and builds an ARM64 Image and device-tree blob.
It records the actual tool versions in `BUILD-INFO.txt` and does not install or flash the result. Inspect any checksum mismatch instead
of treating it as a successful reproduction.

The development workspace keeps an earlier baseline in
`kernel/EXPECTED.sha256`; that file is excluded from the public candidate. Subsequent
source changes, including removal of a device-specific serial property, need
newly verified release hashes. Matching an old kernel version string is not
sufficient evidence that an image or its modules are compatible.

## Module compatibility

Use the same source, configuration and compiler for the kernel and modules.
Compare module symbol-version requirements against the newly built exports
before deployment. A GCC rebuild previously changed `module_layout` and could
not load the existing Clang-built modules despite sharing the kernel version.

## Other components

`drivers/` contains external driver sources. `patches/` contains changes for
kernel and userspace projects, and `userspace/` contains port services.
`phone-rootfs/` records the development integration. Their build/install paths
and upstream revisions are not yet a complete portable release recipe.
The camera source now builds without embedding register/program tables. Its new
external-runtime-data loader is host-tested only. See the
[camera data boundary](CAMERA-FIRMWARE-BOUNDARY.md) before considering deployment.
The U-Boot reference is documented in `u-boot-port/UBOOT.md`.

Do not run workstation-specific deployment or experiment scripts as an
installation procedure. Do not distribute prebuilt images until corresponding
source, module versions, external dependencies and recovery have been checked.

## Sensor-hub runtime dependency

The transport CRC table is generated from polynomial 0x4d; no binary lookup
table is required. The hub itself still requires externally supplied firmware.
The target-side installer requires an explicit path before it installs anything:

```sh
sudo S10_SHUB_FIRMWARE=/path/to/external/firmware.bin sh userspace/sensors/install.sh
```

No firmware is bundled or downloaded by this installer. Review the source and
target integration before running it; this is not a general installation recipe.

## Checked public libcamera build

The public build uses the explicit series in `release/libcamera.series`, rather
than a glob of every development patch. It excludes proprietary-reference color
inputs and the exposure follow-up that depends on them. It does not install
packages, libraries or files on the phone.

Acquire the exact public archive and compare its hash before building:

```sh
curl -fL -o libcamera-191e202.tar.gz \
  https://codeload.github.com/libcamera-org/libcamera/tar.gz/191e202178f02430b5942397c70d215cdd2056fa
printf '%s\n' 'cb26396dfc8c5359ccdff8670ad6972a663f46d029f6aba0feb543039059dde7  libcamera-191e202.tar.gz' | sha256sum -c -
S10_LIBCAMERA_ARCHIVE="$PWD/libcamera-191e202.tar.gz" S10_JOBS=4 \
  sh scripts/build-libcamera-s10.sh /path/to/new/work-directory
```

Build dependencies include a C/C++ toolchain, Meson/Ninja, pkg-config,
YAML/Jinja2/PLY Python modules and the selected libcamera native development
libraries. Use one active Python environment for preparation and compilation.
The helper verifies the archive again and refuses an existing source output.
Use `python3 scripts/prepare-libcamera-source.py --archive ARCHIVE --output NEW_DIR`
for source preparation alone. This recipe builds for the host architecture;
an ARM64 cross-build requires a separately validated target sysroot/toolchain.

See [host build validation](BUILD-VALIDATION.md) for observed results and limits.

## External ARM64 modules

The checked source selection is listed in `release/modules.list`. It includes the external-data sensor driver and excludes obsolete private-input
prototypes. Prepare the complete matching kernel/module build first:

```sh
make -C /path/to/kernel/src O=/path/to/kernel/out ARCH=arm64 LLVM=1 -j4 modules
python3 scripts/build-external-modules.py \
  --kernel-source /path/to/kernel/src --kernel-build /path/to/kernel/out \
  --output /path/to/new/module-output --jobs 4
```

Use the same compiler and configuration as the kernel Image. The builder requires
`Module.symvers`, stages sources in a temporary directory, refuses existing output
and writes a module SHA-256 manifest. Staging uses only the selected public
source paths, even when private headers exist in the development tree.
`BUILD-INFO.json` records those sources, key kernel input hashes, kernel release
and actual tool versions; its checksum is part of `SHA256SUMS`. The builder
requires completed kernel inputs and refuses products if they change during
compilation. It never loads, installs or uploads modules.
The earlier 22-directory build produced 29 ARM64 modules; the new camera bundle
separately produced seven modules. The current list has 23 directories. These
checks do not qualify mixing binaries from different kernel/compiler builds.
Temporary source paths mean bit-identical module binaries are not promised.

## ModemManager plugin

Use the fixed package source in `release/component-sources.toml`, not the older
local development snapshot:

```sh
curl -fL -o ModemManager-7f0bf17.tar.gz \
  https://gitlab.freedesktop.org/mobile-broadband/ModemManager/-/archive/7f0bf17ab518cd78925cf7e53cc0d5c42f88b41a/ModemManager-7f0bf17ab518cd78925cf7e53cc0d5c42f88b41a.tar.gz
printf '%s\n' '018b1d62ca4e2a05a903b706bd63e34663622594caf5384490c4900503aefc8c  ModemManager-7f0bf17.tar.gz' | sha256sum -c -
mkdir mm-source
tar -xzf ModemManager-7f0bf17.tar.gz --strip-components=1 -C mm-source
meson setup mm-build mm-source -Dauto_features=disabled \
  -Dtests=false -Dexamples=false -Dman=false -Dintrospection=false \
  -Dpolkit=no -Dmbim=false -Dqmi=false -Dqrtr=false \
  -Dsystemd_suspend_resume=true -Dsystemd_journal=false \
  -Dsystemdsystemunitdir=no -Dbash_completion=false
ninja -C mm-build -j4
sh userspace/modem/build-mm-plugin.sh "$PWD/mm-source" "$PWD/mm-build" \
  "$PWD/libmm-plugin-s10.so"
```

This builds for the host and installs nothing. GLib/GIO/GModule, libgudev and
the selected ModemManager development prerequisites are required. The helper
prefers matching uninstalled `mm-glib` metadata instead of unrelated host headers.
Do not deploy a native x86-64 plugin to the ARM64 phone. Cross-compilation and
installation need a validated target sysroot and separate compatibility checks.

## Public tool selection

Only the reviewed build, source-preparation, generation and audit entry points
are selected for publication. One-use boot/flash/install/staging experiments and
their dependent tests remain in the local development workspace. The source-only
candidate does not contain a universal installer. Sensor generators require
explicit reference/output paths; they do not silently use ignored work trees.

## External sensor runtime files

The sensor driver requires four model-specific `.s5k` files in the target
firmware search path. They are neither embedded nor distributed with this source
preview. The open conversion tool accepts pinned reference headers and an explicit
new private output directory. The kernel firmware loader and SHA-256 helper must
be enabled; the recorded kernel configuration already enables them.
See [the format and validation report](CAMERA-FIRMWARE-BOUNDARY.md).
The generator does not install firmware, and its output is excluded from source
export. The new loader requires a matching kernel/module rebuild and a separate
device comparison before deployment; current successful camera results used the
previous installed driver.
