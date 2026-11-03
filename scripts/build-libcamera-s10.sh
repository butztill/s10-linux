#!/bin/sh
# SPDX-License-Identifier: GPL-2.0-only
# Prepare and build the reviewed public libcamera series. No installation.
set -eu
project=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
work=${1:?Usage: S10_LIBCAMERA_ARCHIVE=/path/to/pinned.tar.gz sh scripts/build-libcamera-s10.sh NEW_WORKDIR}
: "${S10_LIBCAMERA_ARCHIVE:?Set S10_LIBCAMERA_ARCHIVE to the pinned public archive; see docs/public/BUILDING.md}"
python3 "$project/scripts/prepare-libcamera-source.py" --archive "$S10_LIBCAMERA_ARCHIVE" --output "$work/src"
meson setup "$work/build" "$work/src" --prefix=/usr/local --libdir=lib \
    -Dpipelines=simple -Dipas=simple -Dcam=enabled -Dqcam=disabled \
    -Dgstreamer=disabled -Dtest=false -Ddocumentation=disabled -Dpycamera=disabled \
    -Dlc-compliance=disabled -Dsoftisp-gpu=disabled -Dbuildtype=release
ninja -C "$work/build" -j"${S10_JOBS:-4}"
printf '%s\n' 'Build complete. Nothing installed; target ABI and dependencies require separate validation.'
