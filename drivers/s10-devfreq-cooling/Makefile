# SPDX-License-Identifier: GPL-2.0-only
# Exact kernel devfreq cooling source, built externally because the running
# development kernel omitted CONFIG_DEVFREQ_THERMAL.
obj-m += devfreq_cooling.o
ccflags-y += -DCONFIG_DEVFREQ_THERMAL=1
ccflags-y += -I$(srctree)/drivers/thermal
