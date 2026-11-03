#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Pack pinned reference tables as externally supplied sensor data; never install."""
import argparse
import hashlib
import importlib.util
from pathlib import Path
import struct

ROOT = Path(__file__).resolve().parents[1]
IDENTITY = {'s5k3p9': (0x3109, 0xa101), 's5k3m3': (0x30d3, 0xa301),
            's5k3j1': (0x30a1, 0xa101), 's5k2l4': (0x20c4, 0xa102)}


def pack(model, revision, tables, bursts):
    cursor = 24 + 12 * len(tables) + 8 * len(bursts)
    descriptors, payload = bytearray(), bytearray()
    for table_id, ops in tables:
        descriptors += struct.pack('<HHII', table_id, 0, cursor, len(ops))
        data = b''.join(struct.pack('<HHB', *op) for op in ops)
        payload += data
        cursor += len(data)
    for burst in bursts:
        descriptors += struct.pack('<II', cursor, len(burst))
        payload += bytes(burst)
        cursor += len(burst)
    return struct.pack('<8s6HI', b'S10S5K1\0', 1, model, revision,
                       len(tables), len(bursts), 0, cursor) + descriptors + payload


def load_generator():
    spec = importlib.util.spec_from_file_location('s5k_reference_generator', ROOT/'scripts/prepare-s5k-tables.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def generate(reference):
    module = load_generator()
    results = {}
    # Verify all reference inputs before generating any output files.
    for src in module.SHA:
        if hashlib.sha256((reference/src).read_bytes()).hexdigest() != module.SHA[src]:
            raise ValueError('Reference SHA-256 mismatch: '+src)
    for name, (src, arrays) in module.MODELS.items():
        text = (reference/src).read_text()
        bursts, tables = [], []
        mode = 0
        for cname, sources in arrays:
            sources = [sources] if isinstance(sources, str) else sources
            ops = []
            for source in sources:
                triples = [source] if isinstance(source, tuple) else module.parse(text, source)
                ops += module.convert(cname, triples, bursts)
            if cname == 'global': table_id = 0
            elif cname == 'master': table_id = 1
            else:
                table_id = 0x100 + mode
                mode += 1
            tables.append((table_id, ops))
        if name == 's5k3j1':
            tables.append((1, [(0x0a70, 1, 2), (0x0a72, 0x0100, 2)]))
        results[name] = pack(*IDENTITY[name], tables, bursts)
    return results


def manifest(results):
    lines = ['/* SPDX-License-Identifier: GPL-2.0-only */',
             '/* External sensor-data identities only; no register/program payload. */',
             '#ifndef S10_S5K_FW_MANIFEST_H', '#define S10_S5K_FW_MANIFEST_H']
    for name, data in sorted(results.items()):
        lines.append('#define '+name.upper()+'_FW_SHA256 "'+hashlib.sha256(data).hexdigest()+'"')
    return '\n'.join(lines+['#endif', ''])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--reference-dir', type=Path, required=True)
    parser.add_argument('--output-dir', type=Path, required=True)
    args = parser.parse_args()
    if args.output_dir.exists() or args.output_dir.is_symlink():
        parser.error('Output already exists; choose a new directory.')
    results = generate(args.reference_dir)
    args.output_dir.mkdir(parents=True)
    for name, data in results.items():
        (args.output_dir/(name+'.s5k')).write_bytes(data)
    (args.output_dir/'s5k-fw-manifest.h').write_text(manifest(results))
    print('Prepared external runtime data and identity manifest. Nothing installed; data files must not be published without separate rights review.')


if __name__ == '__main__':
    main()
