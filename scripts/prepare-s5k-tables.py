#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Turn Samsung's GPL register tables into C tables for drivers/s10-camera-v4l2/s5k-cis.c.

Usage: scripts/prepare-s5k-tables.py --reference-dir INPUT --output-dir OUTPUT

The register values are copied unchanged from the Samsung kernel sources in
work-camera/reference/ (LineageOS/android_kernel_samsung_exynos9820, commit
4bcfb0263b21b553760a3bdce542452db3184820). The source files are pinned by SHA-256.

Encoding of struct s5k_reg { u16 reg; u16 val; u8 len; }:
  len 1 / 2      register write of that many bytes (value in the low byte(s))
  reg 0xffff     delay of val microseconds
  reg 0xfffe     burst write: val is an index into the model's burst[] array
"""
from pathlib import Path
import argparse
import hashlib
import re
import sys

root = Path(__file__).resolve().parents[1]

SHA = {
    'fimc-is-cis-3p9-setA.h': '59e9e8cb37226cf3b25400e3180f6a0ab8d96ceada3164a0b3fa5a89156afac5',
    'fimc-is-cis-3m3-setA.h': 'f1f2c003e0dee60525a6b5bfe35a17a06969d56aa090102f5479fb0ab6130c34',
    'fimc-is-cis-3j1-setA.h': '400fb51211eea00dc9d99c93657d750e72d4f9a156ba7f134a9770374badffe2',
    'fimc-is-cis-2l4-setA.h': '858bcedcff68dcd4671a09234a959a36a82ded6b5903ca6f07fa448c95362c7b',
}

# model -> (source file, [(C name, Samsung array name or list of names to concatenate)])
# A list item may also be a literal (reg, value, width) triple of strings.
MODELS = {
    's5k3p9': ('fimc-is-cis-3p9-setA.h', [
        ('global', 'sensor_3p9_setfile_A_Global'),
        ('mode_2304x1728', 'sensor_3p9_setfile_A_2304x1728_60fps'),
        ('master', 'sensor_3p9_setfile_A_dualsync_Master'),
    ]),
    's5k3m3': ('fimc-is-cis-3m3-setA.h', [
        ('global', 'sensor_3m3_setfile_A_Global'),
        ('mode_4032x3024', 'sensor_3m3_setfile_A_4032x3024_30fps'),
        ('mode_2016x1512', 'sensor_3m3_setfile_A_2016x1512_30fps'),
    ]),
    's5k3j1': ('fimc-is-cis-3j1-setA.h', [
        ('global', 'sensor_3j1_setfile_A_Global'),
        ('mode_3648x2736', 'sensor_3j1_setfile_A_3648x2736_30fps_nonPD'),
        ('mode_1824x1368', 'sensor_3j1_setfile_A_1824x1368_30fps_nonPD'),
    ]),
    # Samsung's sensor_2l4_cis_set_global_setting_internal() writes the TnP
    # firmware patch, then the global table. Retention tables are not used.
    's5k2l4': ('fimc-is-cis-2l4-setA.h', [
        ('global', ['sensor_2l4_setfile_A_Reset_TnP', 'sensor_2l4_setfile_A_Global']),
        # Samsung's RF-variation table lowers the MIPI clock to 923 MHz (1846 Mbit/s):
        # at the default 2054 Mbit/s the 4032-pixel lines overflow CSIS0 at the
        # 400 MHz ISPPRE clock (dual pixel mode, 821.6 Mpixel/s > 800).
        # The full mode table enables the interleaved WDR readout (0x021E); Samsung's
        # set_exposure_time() clears it again whenever WDR is not requested.
        ('mode_4032x3024', ['sensor_2l4_setfile_A_4032x3024_30fps',
                            'sensor_2l4_setfile_A_mipi_FULL_923_mhz',
                            ('0xFCFC', '0x4000', '0x02'), ('0x021E', '0x0000', '0x02')]),
        ('mode_2016x1512', 'sensor_2l4_setfile_A_2016x1512_30fps'),
    ]),
}


def strip_comments(text):
    text = re.sub(r'/\*.*?\*/', '', text, flags=re.S)
    return re.sub(r'//[^\n]*', '', text)


def parse(text, name):
    m = re.search(r'const u32 ' + name + r'\[\] = \{(.*?)\n\};', text, re.S)
    if not m:
        sys.exit(f'array {name} not found')
    tokens = [t.strip() for t in strip_comments(m[1]).split(',') if t.strip()]
    if len(tokens) % 3:
        sys.exit(f'{name}: token count {len(tokens)} is not a multiple of 3')
    return list(zip(tokens[::3], tokens[1::3], tokens[2::3]))


def convert(name, triples, bursts):
    ops = []
    cur = None
    for reg, val, width in triples:
        val, width = int(val, 0), int(width, 0)
        if reg == 'I2C_MODE_DELAY':
            assert width == 0 and 0 <= val <= 65000, (name, reg, val)
            ops.append((0xffff, val, 0))
        elif reg == 'I2C_MODE_BURST_ADDR':
            assert width == 2 and cur is None, name
            cur = [val >> 8, val & 0xff]      # the register address leads the payload
            bursts.append(cur)
            ops.append((0xfffe, len(bursts) - 1, 0))
        elif reg == 'I2C_MODE_BURST_DATA':
            assert width == 2 and cur is not None, name
            cur += [val >> 8, val & 0xff]
        else:
            cur = None
            reg = int(reg, 0)
            assert width in (1, 2) and reg < 0xfffe and val < (1 << (8 * width)), (name, reg, val, width)
            ops.append((reg, val, width))
    return ops


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--reference-dir', type=Path, required=True)
    parser.add_argument('--output-dir', type=Path, required=True)
    args = parser.parse_args(argv)
    ref, out_dir = args.reference_dir, args.output_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    for model, (src, arrays) in MODELS.items():
        data = (ref / src).read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        if digest != SHA[src]:
            sys.exit(f'{src}: unexpected SHA-256 {digest}')
        text = data.decode()
        bursts = []
        body = []
        for cname, anames in arrays:
            if isinstance(anames, str):
                anames = [anames]
            ops = []
            for aname in anames:
                if isinstance(aname, tuple):
                    ops += convert(cname, [aname], bursts)
                else:
                    ops += convert(aname, parse(text, aname), bursts)
            body.append(f'static const struct s5k_reg {model}_{cname}[] = {{\n' + '\n'.join(
                f'\t{{ 0x{r:04x}, 0x{v:04x}, {w} }},' for r, v, w in ops) + '\n};\n')
        head = [f'''/* SPDX-License-Identifier: GPL-2.0-only
 * Register values Copyright (c) Samsung Electronics Co., Ltd.
 * Generated by scripts/prepare-s5k-tables.py from {src}
 * (LineageOS/android_kernel_samsung_exynos9820 commit
 * 4bcfb0263b21b553760a3bdce542452db3184820, SHA-256 {digest}).
 * Do not edit; see the generator for the encoding of struct s5k_reg.
 */
''']
        for i, b in enumerate(bursts):
            head.append(f'static const u8 {model}_burst{i}[] = {{\n' + '\n'.join(
                '\t' + ', '.join(f'0x{x:02x}' for x in b[j:j + 16]) + ','
                for j in range(0, len(b), 16)) + '\n};\n')
        if bursts:
            head.append(f'static const struct s5k_burst {model}_bursts[] = {{\n' + '\n'.join(
                f'\t{{ {model}_burst{i}, sizeof({model}_burst{i}) }},' for i in range(len(bursts))) + '\n};\n')
        (out_dir / f'{model}-tables.h').write_text('\n'.join(head + body))
        print(f'{model}: {len(bursts)} burst(s), {sum(b.count(chr(10)) for b in body)} lines')


if __name__ == '__main__':
    main()
