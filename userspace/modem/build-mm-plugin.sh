#!/bin/sh
# SPDX-License-Identifier: GPL-2.0-only
# Build only the S10 plugin from explicit matching source/build inputs. No install.
set -eu
if [ "$#" -ne 3 ]; then
    echo 'Usage: build-mm-plugin.sh MM_SOURCE MM_BUILD NEW_OUTPUT.so' >&2
    exit 2
fi
project=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
source=$(CDPATH= cd -- "$1" && pwd)
build=$(CDPATH= cd -- "$2" && pwd)
output=$3
if [ -e "$output" ] || [ -L "$output" ]; then
    echo 'Output already exists; refusing replacement.' >&2
    exit 1
fi
test -f "$source/src/mm-broadband-modem.h"
test -f "$build/config.h"
# Prefer the matching uninstalled pkg-config files to an unrelated host version.
export PKG_CONFIG_PATH="$build/meson-uninstalled${PKG_CONFIG_PATH:+:$PKG_CONFIG_PATH}"
test "$(pkg-config --modversion mm-glib)" = 1.25.95 || {
    echo 'Matching mm-glib 1.25.95 headers/libraries are required.' >&2; exit 1;
}
ninja -C "$build" src/mm-daemon-enums-types.h
# WITH_SUSPEND_RESUME changes the internal class ABI. Apply it after config.h.
mkdir -p "$build/s10-config"
cat > "$build/s10-config/config.h" <<'EOF'
#ifndef S10_PLUGIN_CONFIG_H
#define S10_PLUGIN_CONFIG_H
#include "../config.h"
#ifndef WITH_SUSPEND_RESUME
#define WITH_SUSPEND_RESUME 1
#endif
#endif
EOF
cc -std=gnu11 -shared -fPIC -O2 -Wall -Wextra -Werror -Wno-unused-parameter \
    '-DMM_MODULE_NAME="s10"' \
    -I"$build/s10-config" -I"$build" -I"$build/src" -I"$source/src" \
    -I"$source/src/kerneldevice" -I"$source/src/plugins" -I"$source/libmm-glib" \
    $(pkg-config --cflags mm-glib gio-unix-2.0 gmodule-2.0 gudev-1.0) \
    "$project/mm-plugin-s10.c" -o "$output" \
    $(pkg-config --libs mm-glib gio-unix-2.0 gmodule-2.0 gudev-1.0)
printf '%s\n' 'Plugin built for the host architecture. Nothing installed or loaded.'
