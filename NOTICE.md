<!-- SPDX-License-Identifier: GPL-2.0-only -->

# Third-party notices and review status

This source collection incorporates modifications to other open-source
projects. Preserve the notices in their original files; the list below is an
inventory starting point, not a replacement for those notices or a completed
copyright review.

| Source | Recorded provenance | Licensing evidence |
| --- | --- | --- |
| Linux | Linux 7.2-rc2 and the kernel overlay | File-level GPL/LGPL/permissive SPDX expressions and original notices |
| Exynos9820 port work | Files credit chiffathefox and other original contributors | Preserve each file's notice; do not attribute their work to this repository maintainer |
| Samsung GPL source | SM-G973F_EUR_12_Opensource package, used for PMU and sensor reference work | PMU provenance verified; sensor embedded-program source form remains unresolved |
| U-Boot reference | chiffathefox/u-boot, exynos9820, dec4927b4ad5b789b0854050096fc80250627de7 | Existing dual-license and copyright notices |
| videobuf2 | Linux video-buffer framework copies in the external camera driver | Byte-identical to the pinned Linux base; existing GNU GPL notices retained, GPLv2 distribution recorded in sidecars |
| SWu-IKEv2 | fasferraz/SWu-IKEv2 at bf5d258b7298db65e64b43065b153325eba47a9e; reproducible local integration patch | Original GPLv3 text retained; this modified distribution uses GPL-3.0-only |
| CLAT | toreanderson/clatd v2.1.0 at 5e085f1b1730cb4133623d5738fd447199baf58e | Original Tore Anderson MIT notice retained in both copies |
| libcamera and desktop components | Component patches under patches/ | Reviewed component-specific licenses and known revisions recorded; private color data excluded and unqualified build inputs remain explicit |

No proprietary firmware or vendor executable is licensed by this notice.
Unresolved attribution and distribution questions block the public candidate.
See [licensing](docs/public/LICENSING.md) and the publication audit.

The original SIM bridge `userspace/vowifi/usim_cp.py` is GPL-2.0-or-later.
See the [verified source review](docs/public/PROVENANCE.md) for scope and limits.
