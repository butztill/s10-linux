#!/bin/bash
# SPDX-License-Identifier: GPL-2.0-only
# Reproducible S10 kernel build: pinned base source + overlay + config + fixed build metadata.
#   scripts/build-kernel-reproducible.sh [WORKDIR]        (default: ./build-kernel)
# Result: WORKDIR/Image, WORKDIR/device-s10-production.dtb and their SHA256.
# S10_LINUX_ARCHIVE supplies a cached source archive; S10_JOBS limits build parallelism.
# Needs the toolchain in kernel/BUILD-ENV.txt (clang/lld 23.1.3, GNU make 4.4.1, dtc 1.8.1, ...). Bit-identical output
# is only expected with that exact toolchain; the result does not depend on the work directory path.
set -euo pipefail
REPO=$(cd "$(dirname "$0")/.." && pwd)
WORK=$(mkdir -p "${1:-$PWD/build-kernel}" && cd "${1:-$PWD/build-kernel}" && pwd)

BASE_URL=https://git.kernel.org/pub/scm/linux/kernel/git/torvalds/linux.git/snapshot/linux-7.2-rc2.tar.gz
BASE_COMMIT=8cdeaa50eae8dad34885515f62559ee83e7e8dda               # git commit recorded in the tarball
BASE_TAR_SHA256=2fd05c84450e572f66479fd737c309c480dc3684d2ab7c475791240087f7103b   # sha256 of the uncompressed tar stream

# Fixed build metadata (kernel version string, compile.h, utsversion.h)
export KBUILD_BUILD_TIMESTAMP='Mon Oct 26 00:00:00 UTC 2026'
export KBUILD_BUILD_USER=s10 KBUILD_BUILD_HOST=s10 KBUILD_BUILD_VERSION=1
export SOURCE_DATE_EPOCH=$(date -u -d "$KBUILD_BUILD_TIMESTAMP" +%s)
export LC_ALL=C TZ=UTC

mkdir -p "$WORK/dl"
TAR=${S10_LINUX_ARCHIVE:-$WORK/dl/linux-7.2-rc2.tar.gz}
if [ -n "${S10_LINUX_ARCHIVE:-}" ]; then
    [ -f "$TAR" ] || { echo "S10_LINUX_ARCHIVE does not name a file" >&2; exit 1; }
else
    [ -f "$TAR" ] || curl -fL -o "$TAR" "$BASE_URL"
fi
[ "$(gzip -dc "$TAR" | sha256sum | cut -d' ' -f1)" = "$BASE_TAR_SHA256" ] || { echo "base tarball hash mismatch"; exit 1; }
[ "$(gzip -dc "$TAR" | git get-tar-commit-id)" = "$BASE_COMMIT" ] || { echo "base commit mismatch"; exit 1; }

SRC=$WORK/src
if [ ! -e "$SRC" ]; then
    mkdir -p "$SRC"
    tar -xzf "$TAR" -C "$SRC" --strip-components=1
    printf '%s\n' "$BASE_COMMIT" > "$SRC/.s10-base-extracted"
elif [ ! -f "$SRC/.s10-base-extracted" ] || [ "$(cat "$SRC/.s10-base-extracted")" != "$BASE_COMMIT" ]; then
    echo "Refusing an existing unverified source directory; choose a new WORKDIR." >&2
    exit 1
fi
# Always (re)apply the overlay so overlay edits are picked up; --checksum avoids touching unchanged files (no needless rebuilds).
rsync -a --checksum "$REPO/kernel/overlay/" "$SRC/"

OUT=$WORK/out
mkdir -p "$OUT"
cp "$REPO/kernel/config-7.2.0-rc2-s10-panfrost1" "$OUT/.config"
MK=(make -C "$SRC" O="$OUT" ARCH=arm64 LLVM=1)
"${MK[@]}" olddefconfig
MAP="-ffile-prefix-map=$SRC=. -ffile-prefix-map=$OUT=."
# The compat (32-bit) vDSO builds with its own flags; its Makefile does CC_COMPAT ?= $(CC) and then appends --target, so it must come from the environment.
export CC_COMPAT="clang $MAP"
"${MK[@]}" -j"${S10_JOBS:-$(nproc)}" KCFLAGS="$MAP" KAFLAGS="$MAP" Image
cp "$OUT/arch/arm64/boot/Image" "$WORK/Image"
dtc -q -I dts -O dtb -o "$WORK/device-s10-production.dtb" "$REPO/dts/s10-production-20261026.dts"

( cd "$WORK" && sha256sum Image device-s10-production.dtb | tee SHA256SUMS )
# Record the actual tools rather than implying that a newer compiler reproduces
# the historical binary. A fresh directory is required for a clean-source check.
{
    printf 'base_commit=%s\nbase_tar_sha256=%s\n' "$BASE_COMMIT" "$BASE_TAR_SHA256"
    clang --version | sed -n '1p'
    ld.lld --version
    make --version | sed -n '1p'
    dtc --version
    ( cd "$REPO" && sha256sum kernel/config-7.2.0-rc2-s10-panfrost1 dts/s10-production-20261026.dts )
} > "$WORK/BUILD-INFO.txt"
if [ -f "$REPO/kernel/EXPECTED.sha256" ]; then
    ( cd "$WORK" && diff <(cat SHA256SUMS) "$REPO/kernel/EXPECTED.sha256" >/dev/null ) \
        && echo "REPRODUCED: matches kernel/EXPECTED.sha256" \
        || echo "DIFFERENT from historical kernel/EXPECTED.sha256; inspect recorded inputs and tools"
fi
