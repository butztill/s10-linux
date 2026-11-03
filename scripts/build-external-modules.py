#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Build reviewed external modules from explicit kernel inputs; never install."""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import tomllib

ROOT = Path(__file__).resolve().parents[1]


def compiler_declaration(path):
    for line in path.read_text().splitlines():
        fields = line.split(None, 2)
        if len(fields) == 3 and fields[:2] == ['#define','LINUX_COMPILER']:
            return fields[2]
    return None


def public_inputs(root):
    spec = importlib.util.spec_from_file_location('module_source_policy', root/'scripts/publication.py')
    publication = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(publication)
    policy = tomllib.loads((root/'release/publication.toml').read_text())
    return {name for name in publication.inventory(root, policy)
            if not publication.exclusion(name, policy)}


def stage_module(root, entry, target, selected):
    """Copy the public source boundary, including when the workspace has extras."""
    if entry+'/Makefile' not in selected:
        raise ValueError('Module Makefile is outside the public selection: '+entry)
    records = []
    for name in sorted(selected):
        if not name.startswith(entry+'/'):
            continue
        if Path(name).is_absolute() or '..' in Path(name).parts:
            raise ValueError('Invalid public source path: '+name)
        p = root/name
        if p.name.endswith('.mod.c') or not (p.suffix in {'.c','.h','.license'} or p.name in {'Makefile','Kconfig'}):
            continue
        if (p.is_symlink() or not p.resolve().is_relative_to(root.resolve())
                or not p.is_file()):
            raise ValueError('Invalid public source: '+name)
        data = p.read_bytes()
        dst = target/Path(name).relative_to(entry)
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(data)
        records.append({'path':name, 'sha256':hashlib.sha256(data).hexdigest()})
    return records


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--kernel-source', type=Path, required=True)
    parser.add_argument('--kernel-build', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--jobs', type=int, default=4)
    args = parser.parse_args()
    if args.jobs < 1:
        parser.error('jobs must be positive')
    if args.output.exists() or args.output.is_symlink():
        parser.error('Output exists; choose a fresh directory.')
    source, build = args.kernel_source.resolve(), args.kernel_build.resolve()
    input_names = ['.config','Module.symvers','include/config/kernel.release','include/generated/compile.h']
    if not (source/'Makefile').is_file() or not all((build/name).is_file() for name in input_names):
        parser.error('A complete matching kernel/module build with Module.symvers is required.')
    kernel_inputs = {name:hashlib.sha256((build/name).read_bytes()).hexdigest() for name in input_names}
    try:
        selected = public_inputs(ROOT)
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        parser.error('Public source inventory is unavailable: '+str(error))
    entries = [x.strip() for x in (ROOT/'release/modules.list').read_text().splitlines()
               if x.strip() and not x.lstrip().startswith('#')]
    for entry in entries:
        p = Path(entry)
        if (p.is_absolute() or '..' in p.parts or not (ROOT/p/'Makefile').is_file()
                or (ROOT/p).is_symlink()):
            parser.error('Invalid module entry: '+entry)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.s10-modules-', dir=args.output.parent) as tmp:
        staging = Path(tmp)
        source_records = []
        for entry in entries:
            target = staging/'sources'/entry
            source_records.extend(stage_module(ROOT, entry, target, selected))
            subprocess.run(['make','-C',str(source),'O='+str(build),'ARCH=arm64','LLVM=1',
                            'W=1','M='+str(target),'-j'+str(args.jobs),'modules'],check=True)
        products = staging/'products'
        checksums=[]
        for module in sorted((staging/'sources').rglob('*.ko')):
            relative=module.relative_to(staging/'sources')
            target=products/relative
            target.parent.mkdir(parents=True,exist_ok=True)
            shutil.copyfile(module,target)
            checksums.append(hashlib.sha256(target.read_bytes()).hexdigest()+'  '+relative.as_posix())
        if not checksums:
            parser.error('No module products were generated.')
        if any(hashlib.sha256((build/name).read_bytes()).hexdigest() != digest
               for name,digest in kernel_inputs.items()):
            parser.error('Kernel inputs changed during the build; use a completed matching kernel tree.')
        release = build/'include/config/kernel.release'
        compile_info = build/'include/generated/compile.h'
        metadata = {
            'format':1, 'architecture':'arm64',
            'kernel_release':release.read_text().strip(),
            'kernel_input_sha256':kernel_inputs,
            'kernel_compiler_declaration':compiler_declaration(compile_info),
            'compiler':subprocess.check_output(['clang','--version'],text=True).splitlines()[0],
            'linker':subprocess.check_output(['ld.lld','--version'],text=True).strip(),
            'sources':source_records,
            'scope':'Build provenance only; no installed-kernel ABI or device qualification.'}
        info = products/'BUILD-INFO.json'
        info.write_text(json.dumps(metadata,indent=2)+'\n')
        checksums.append(hashlib.sha256(info.read_bytes()).hexdigest()+'  BUILD-INFO.json')
        (products/'SHA256SUMS').write_text('\n'.join(checksums)+'\n')
        if args.output.exists() or args.output.is_symlink():
            parser.error('Output appeared during build; refusing replacement.')
        products.rename(args.output)
    print('Modules built for ARM64. Nothing installed, loaded or copied to the phone.')


if __name__ == '__main__':
    main()
