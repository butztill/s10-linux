#!/bin/sh
# SPDX-License-Identifier: GPL-2.0-only
# Start Samsung's lhd in the Android chroot (needs bbd_bridge.py running). Output: /tmp/lhd.out
export LD_LIBRARY_PATH=/system/lib64:/vendor/lib64:/apex/com.android.runtime/lib64/bionic:/apex/com.android.runtime/lib64:/apex/com.android.i18n/lib64:/apex/com.android.conscrypt/lib64:/apex/com.android.art/lib64:/apex/com.android.resolv/lib64:/apex/com.android.vndk.current/lib64
exec /system/bin/linker64 /vendor/bin/hw/lhd /vendor/etc/sensor/lhd.conf
