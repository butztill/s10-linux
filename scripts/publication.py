#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Audit a source candidate and refuse export while review blockers remain."""
import argparse
import fnmatch
import gzip
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import tarfile
import tomllib

ROOT = Path(__file__).resolve().parents[1]
ALIASES = {'GPL-2.0': 'GPL-2.0-only', 'GPL-2.0+': 'GPL-2.0-or-later'}
LICENSES = {'GPL-2.0-only', 'GPL-2.0-or-later', 'GPL-3.0-only',
            'GPL-3.0-or-later', 'LGPL-2.1-only', 'LGPL-2.1-or-later',
            'BSD-2-Clause', 'BSD-3-Clause', 'MIT', 'CC0-1.0', 'Linux-syscall-note'}
BINARY_SUFFIXES = {'.bin', '.dtb', '.dtbo', '.ko', '.o', '.a', '.so', '.elf',
                   '.fw', '.img', '.raw', '.zip', '.pyc', '.s5k'}
PRIVATE_DIRS = {'.git', '.agents', '.codex', '.aws', 'keys', 'stock-fw',
                'artifacts', 'logs', 'toolchains', 'pmaports', 's10-kernel', 'u-boot'}
SPDX = re.compile(r'^\s*(?://|#|/\*|\*|<!--)?\s*SPDX-License-Identifier:\s*(.+)', re.M)
CHECKS = {
    'private-key': re.compile(r'-----BEGIN (?:OPENSSH |RSA |EC |DSA )?PRIVATE KEY-----'),
    'device-serial': re.compile(r'\bserial-number\s*='),
    'personal-home': re.compile(r'/home/(?!user(?:/|\b)|root(?:/|\b))[A-Za-z0-9_-]+'),
    'subscriber-identifier': re.compile(r'''(?i)\b(?:imsi|imei|imeisv|iccid|msisdn)\b\s*[:=]\s*['"][0-9]{10,22}['"]'''),
    'installation-identifier': re.compile(r'pmos_(?:boot|root)_uuid=[0-9A-Fa-f][0-9A-Fa-f-]{7,}'),
}
PROSE = re.compile(r'\b(?:der user|der nutzer|vom nutzer|nutzerbestätigt|user confirmed|the user confirmed)\b', re.I)
GERMAN = re.compile(r'\b(?:Dieses|Funktioniert|Einschränkungen|Veröffentlichung|Nutzer|Benutzer|Rückweg|Gerätezustand)\b')
PAYLOAD = re.compile(r'\bconst\s+(?:u8|uint8_t|unsigned\s+char)\s+\w*(?:burst|firmware|microcode|tnp)\w*\s*\[\s*\w*\s*\]\s*=', re.I)


def matches(name, patterns):
    return any(fnmatch.fnmatchcase(name, p) for p in patterns)


def inventory(root, policy):
    # Exported archives deliberately contain no Git history. Their generated
    # manifest supplies both the source inventory and a checked integrity boundary.
    manifest = root / 'SOURCE-SHA256SUMS'
    if not (root / '.git').exists() and manifest.is_file():
        if manifest.is_symlink():
            raise ValueError('Source manifest must not be a symlink.')
        names = set()
        for line in manifest.read_text().splitlines():
            if not line or line.startswith('#'):
                continue
            digest, name = line.split('  ', 1)
            path = PurePosixPath(name)
            if (not re.fullmatch(r'[0-9a-f]{64}', digest) or name in names
                    or path.is_absolute() or '..' in path.parts
                    or path.as_posix() != name or name == 'SOURCE-SHA256SUMS'):
                raise ValueError('Invalid source manifest entry.')
            p = root / name
            if (p.is_symlink() or not p.resolve().is_relative_to(root.resolve())
                    or not p.is_file()
                    or hashlib.sha256(p.read_bytes()).hexdigest() != digest):
                raise ValueError('Source manifest mismatch.')
            names.add(name)
        return sorted(names)
    raw = subprocess.check_output(['git', '-C', str(root), 'ls-files', '-z'])
    names = set(raw.decode().split('\0')) - {''}
    for pattern in policy.get('extra', []):
        names.update(p.relative_to(root).as_posix() for p in root.glob(pattern)
                     if p.is_file() or p.is_symlink())
    return sorted(names)


def exclusion(name, policy):
    p = PurePosixPath(name)
    if p.is_absolute() or '..' in p.parts or not p.parts:
        return 'unsafe-path'
    if p.parts[0] in PRIVATE_DIRS or p.parts[0].startswith('work'):
        return 'private-workspace'
    if p.suffix.lower() in BINARY_SUFFIXES or re.search(r'\.so(?:\.|$)', p.name):
        return 'generated-or-binary'
    if not matches(name, policy['include']):
        return 'outside-public-selection'
    if matches(name, policy.get('exclude', [])):
        return 'explicitly-excluded'
    if p.suffix == '.md' and not matches(name, policy['public_documents']):
        return 'internal-documentation'
    return None


def license_expressions(text, name):
    # A license copied inside a diff is not a license grant for the patch.
    if name.endswith(('.patch', '.diff')):
        text = re.split(r'(?m)^(?:diff --git |--- |@@)', text, maxsplit=1)[0]
    return [m.split('-->')[0].split('*/')[0].strip() for m in SPDX.findall(text)]


def reviewed_files(root, policy):
    """Apply only narrow, content-bound review decisions, never secret waivers."""
    name = policy.get('review_decisions')
    if not name:
        return {}, []
    issue = {'path': name, 'code': 'invalid-review-record',
             'detail': 'Review records must be valid, evidence-backed and match current inputs.'}
    try:
        path = root / name
        if exclusion(name, policy) or path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
            return {}, [issue]
        document = json.loads(path.read_text())
        if document.get('version') != 1:
            return {}, [issue]
        decisions = {}
        for entry in document['files']:
            target = entry['path']
            p = root / target
            codes = set(entry['resolved_codes'])
            if (not codes or not codes <= {'provenance-review', 'subscriber-identifier'}
                    or not isinstance(entry['evidence'], str) or len(entry['evidence']) < 30
                    or target in decisions or exclusion(target, policy)
                    or p.is_symlink() or not p.resolve().is_relative_to(root.resolve())
                    or hashlib.sha256(p.read_bytes()).hexdigest() != entry['sha256']):
                return {}, [issue]
            for dependency in entry.get('inputs', []):
                q = root / dependency['path']
                if (exclusion(dependency['path'], policy) or q.is_symlink()
                        or not q.resolve().is_relative_to(root.resolve())
                        or hashlib.sha256(q.read_bytes()).hexdigest() != dependency['sha256']):
                    return {}, [issue]
            decisions[target] = entry
        return decisions, []
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return {}, [issue]


def audit(root, policy):
    selected, excluded, issues = [], [], []
    try:
        names = inventory(root, policy)
    except (OSError, ValueError, subprocess.CalledProcessError):
        names = []
        issues.append({'path': 'SOURCE-SHA256SUMS', 'code': 'invalid-source-inventory',
                       'detail': 'A valid Git inventory or matching source archive manifest is required.'})
    source_manifest = policy.get('source_manifest')
    if source_manifest:
        valid = False
        try:
            path = root / source_manifest
            if (source_manifest in names and not exclusion(source_manifest, policy)
                    and not path.is_symlink()
                    and path.resolve().is_relative_to(root.resolve())):
                sources = tomllib.loads(path.read_text())
                entries = sources['component']
                ids = [entry['id'] for entry in entries]
                valid = (sources.get('version') == 1 and len(ids) == len(set(ids))
                         and set(policy.get('required_components', [])) <= set(ids))
                for entry in entries:
                    revision = entry.get('revision', '')
                    archive = entry.get('archive', '')
                    checksum = entry.get('archive_sha256', '')
                    commit_pin = (bool(re.fullmatch(r'[0-9a-f]{40}', revision))
                                  and entry.get('upstream', '').startswith('https://'))
                    archive_pin = (archive.startswith('https://')
                                   and bool(re.fullmatch(r'[0-9a-f]{64}', checksum)))
                    valid = valid and (commit_pin or archive_pin)
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            valid = False
        if not valid:
            issues.append({'path': source_manifest, 'code': 'component-source-pins',
                           'detail': 'Public component inputs require unique IDs and an immutable commit or checked archive hash.'})
    decisions, review_issues = reviewed_files(root, policy)
    issues.extend(review_issues)
    for name in names:
        reason = exclusion(name, policy)
        if reason:
            excluded.append({'path': name, 'reason': reason})
            continue
        p = root / name
        if p.is_symlink() or not p.resolve().is_relative_to(root.resolve()):
            issues.append({'path': name, 'code': 'symlink', 'detail': 'Review symlinks before distribution.'})
            continue
        if not p.is_file():
            issues.append({'path': name, 'code': 'missing-file', 'detail': 'Selected input does not exist.'})
            continue
        data = p.read_bytes()
        try:
            text = data.decode('utf-8')
            if '\0' in text:
                raise UnicodeError('NUL byte')
        except UnicodeError:
            issues.append({'path': name, 'code': 'binary-content', 'detail': 'Public selection is source text only.'})
            continue
        expressions = license_expressions(text, name)
        sidecar = name + '.license'
        if (not expressions and sidecar in names and not exclusion(sidecar, policy)):
            sidecar_path = root / sidecar
            if (not sidecar_path.is_symlink()
                    and sidecar_path.resolve().is_relative_to(root.resolve())):
                try:
                    expressions = license_expressions(sidecar_path.read_text(), sidecar)
                except (OSError, UnicodeError):
                    issues.append({'path': sidecar, 'code': 'invalid-license-sidecar',
                                   'detail': 'License sidecar is unreadable or not UTF-8 text.'})
        is_license = name == 'LICENSE' or name.startswith('LICENSES/') or name.endswith('/LICENSE')
        if not expressions and not is_license:
            issues.append({'path': name, 'code': 'license-review', 'detail': 'No explicit file-level SPDX grant; inspect existing notices and provenance, do not guess.'})
        for expression in expressions:
            tokens = re.findall(r'[A-Za-z0-9][A-Za-z0-9.+-]*', expression)
            for token in tokens:
                if token in {'AND', 'OR', 'WITH'}:
                    continue
                token = ALIASES.get(token, token)
                if token not in LICENSES:
                    issues.append({'path': name, 'code': 'license-expression-review', 'detail': 'Unrecognised license identifier: ' + token})
                elif not (root / 'LICENSES' / (token + '.txt')).is_file():
                    issues.append({'path': name, 'code': 'missing-license-text', 'detail': token})
        for requirement in policy.get('license_requirement', []):
            if matches(name, requirement['paths']):
                if not expressions or any(e not in requirement['expressions'] for e in expressions):
                    issues.append({'path': name, 'code': 'license-compatibility-review',
                                   'detail': requirement['reason']})
        for code, regex in CHECKS.items():
            if regex.search(text):
                issues.append({'path': name, 'code': code, 'detail': 'Review identified content; values intentionally omitted.'})
        payload_text = re.sub(r'(?m)^[+ -]', '', text) if name.endswith(('.patch', '.diff')) else text
        if name.endswith(('.c', '.h', '.cpp', '.patch', '.diff')) and PAYLOAD.search(payload_text):
            issues.append({'path': name, 'code': 'embedded-payload-review',
                           'detail': 'Review embedded byte payload source form; an outer license tag alone is insufficient.'})
        if name.endswith('.md'):
            if PROSE.search(text) or GERMAN.search(text):
                issues.append({'path': name, 'code': 'public-document-language', 'detail': 'Use neutral English public documentation.'})
            for target in re.findall(r'\]\(([^)]+)\)', text):
                if '://' not in target and not target.startswith('#'):
                    target = target.split('#')[0]
                    destination = (p.parent / target).resolve()
                    try:
                        relative = destination.relative_to(root.resolve()).as_posix()
                    except ValueError:
                        relative = ''
                    if (relative not in names or not destination.is_file()
                            or exclusion(relative, policy)):
                        issues.append({'path': name, 'code': 'public-document-link', 'detail': 'Link target is not part of the candidate: ' + target})
        for review in policy.get('review', []):
            if matches(name, review['paths']):
                issues.append({'path': name, 'code': review.get('code', 'provenance-review'), 'detail': review['reason']})
        selected.append({'path': name, 'sha256': hashlib.sha256(data).hexdigest(),
                         'bytes': len(data), 'license_expressions': expressions})
    selected_paths = {item['path'] for item in selected}
    for dependency in policy.get('source_dependency', []):
        if dependency['owner'] not in selected_paths:
            continue
        for required in dependency['requires']:
            if required not in selected_paths:
                issues.append({'path': dependency['owner'],
                               'code': 'missing-source-dependency',
                               'detail': 'Required source is absent from the candidate: ' + required})
    for blocker in policy.get('blocker', []):
        issues.append({'path': 'release/publication.toml', 'code': blocker['id'], 'detail': blocker['reason']})
    if not selected:
        issues.append({'path': 'release/publication.toml', 'code': 'empty-selection', 'detail': 'No source candidate selected.'})
    resolved = []
    remaining = []
    for item in issues:
        entry = decisions.get(item['path'])
        if entry and item['code'] in entry['resolved_codes']:
            resolved.append({**item, 'evidence': entry['evidence']})
        else:
            remaining.append(item)
    return {'format': 1, 'ready': not remaining, 'files': selected, 'excluded': excluded,
            'issues': remaining, 'resolved_findings': resolved}


def export(root, report, output):
    if not report['ready']:
        raise ValueError('Publication blocked; resolve the audit report before export.')
    # Recheck all inputs before creating the destination. Never overwrite a file.
    payloads = []
    for item in report['files']:
        p = root / item['path']
        if (p.is_symlink() or not p.resolve().is_relative_to(root.resolve())
                or not p.is_file()):
            raise ValueError('Source changed after audit: ' + item['path'])
        data = p.read_bytes()
        if hashlib.sha256(data).hexdigest() != item['sha256']:
            raise ValueError('Source changed after audit: ' + item['path'])
        payloads.append((item['path'], data, 0o755 if p.stat().st_mode & 0o111 else 0o644))
    manifest = ('# SPDX-License-Identifier: GPL-2.0-only\n' +
                ''.join(f"{x['sha256']}  {x['path']}\n" for x in report['files'])).encode()
    if any(name == 'SOURCE-SHA256SUMS' for name, _, _ in payloads):
        raise ValueError('Reserved archive name')
    payloads.append(('SOURCE-SHA256SUMS', manifest, 0o644))
    with output.open('xb') as raw:
        with gzip.GzipFile(fileobj=raw, mode='wb', filename='', mtime=0) as zipped:
            with tarfile.open(fileobj=zipped, mode='w', format=tarfile.PAX_FORMAT) as archive:
                for name, data, mode in payloads:
                    entry = tarfile.TarInfo(name)
                    entry.size, entry.mode = len(data), mode
                    entry.uid = entry.gid = entry.mtime = 0
                    archive.addfile(entry, io.BytesIO(data))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['audit', 'export'])
    parser.add_argument('--report', type=Path)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    policy = tomllib.loads((ROOT / 'release/publication.toml').read_text())
    report = audit(ROOT, policy)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2) + '\n')
    print(f"Selected {len(report['files'])} files; excluded {len(report['excluded'])}; blockers {len(report['issues'])}.")
    if args.command == 'export':
        if not args.output:
            parser.error('export requires --output')
        try:
            export(ROOT, report, args.output)
        except (ValueError, FileExistsError) as exc:
            parser.exit(1, str(exc) + '\n')
    return 0 if report['ready'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
