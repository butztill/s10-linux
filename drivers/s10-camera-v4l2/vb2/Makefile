# SPDX-License-Identifier: GPL-2.0
# Out-of-tree copy of the kernel's videobuf2 (unchanged sources) because the
# running kernel was built without CONFIG_VIDEOBUF2_*. Once the kernel config
# has CONFIG_VIDEOBUF2_V4L2/DMA_CONTIG=m|y, drop this directory.
videobuf2-common-objs := videobuf2-core.o frame_vector.o vb2-trace.o
obj-m += videobuf2-common.o videobuf2-memops.o videobuf2-v4l2.o videobuf2-dma-contig.o
