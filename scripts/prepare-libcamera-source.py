#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Prepare pinned public libcamera source without private camera color inputs."""
import argparse
import hashlib
from pathlib import Path
import subprocess
import tarfile
import tempfile

ROOT = Path(__file__).resolve().parents[1]
COMMIT = '191e202178f02430b5942397c70d215cdd2056fa'
ARCHIVE_SHA256 = 'cb26396dfc8c5359ccdff8670ad6972a663f46d029f6aba0feb543039059dde7'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists() or args.output.is_symlink():
        parser.error('Output already exists; use a new directory.')
    with args.archive.open('rb') as stream:
        if hashlib.file_digest(stream, 'sha256').hexdigest() != ARCHIVE_SHA256:
            parser.error('Source archive SHA-256 mismatch.')
    series = ROOT / 'release/libcamera.series'
    patches = [line.strip() for line in series.read_text().splitlines()
               if line.strip() and not line.lstrip().startswith('#')]
    for name in patches:
        p = Path(name)
        if p.is_absolute() or '..' in p.parts or not (ROOT / p).is_file():
            parser.error('Invalid patch in release series.')
    prefix = 'libcamera-' + COMMIT
    # Extraction is isolated, rejects escaping links/paths, and never overwrites
    # an existing source tree. The pinned upstream archive contains two links.
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.s10-libcamera-', dir=args.output.parent) as temp:
        temp = Path(temp)
        with tarfile.open(args.archive) as archive:
            if any(not (m.name == prefix or m.name.startswith(prefix + '/'))
                   for m in archive.getmembers()):
                parser.error('Unexpected source archive root.')
            archive.extractall(temp, filter='data')
        source = temp / prefix
        for name in patches:
            subprocess.run(['patch', '--batch', '--forward', '--fuzz=0', '-p1',
                            '-i', str(ROOT / name)], cwd=source, check=True)
        records = ['# Pinned upstream commit: ' + COMMIT,
                   ARCHIVE_SHA256 + '  upstream.tar.gz']
        for name in patches:
            records.append(hashlib.sha256((ROOT / name).read_bytes()).hexdigest() + '  ' + name)
        (source / 'S10-SOURCE-INPUTS.sha256').write_text('\n'.join(records) + '\n')
        # Check again immediately before moving; no target should be replaced.
        if args.output.exists() or args.output.is_symlink():
            parser.error('Output appeared during preparation; refusing replacement.')
        source.rename(args.output)
    print('Prepared public patch series. This does not build or install libcamera.')


if __name__ == '__main__':
    main()
