<!-- SPDX-License-Identifier: GPL-2.0-only -->

# Preparing a public source release

Publication is being prepared; no release has been approved or published.
The development workspace and its Git history are not the public source tree.

## Candidate boundary

`release/publication.toml` defines the intended source selection and exclusions.
The audit reads the current index file list plus explicitly declared release
preparation files. Ignored local content is not selected. Historical notes,
raw logs, firmware trees, keys, compiled device trees and unresolved vendor
extractions are excluded from the candidate. Exclusion preserves local research;
it does not remove those files from earlier Git commits.

```sh
python3 scripts/publication.py audit --report artifacts/publication-audit.json
python3 -m unittest discover -s tests -p 'test_publication.py'
python3 scripts/publication.py export --output artifacts/s10-public-source.tar.gz
```

The exporter refuses unresolved blockers, preserves source paths/notices and
writes an SHA-256 manifest. An extracted source archive can be audited without
Git; the manifest then supplies the inventory and every listed source checksum
must match. A working Git checkout continues to use its index plus declared
preparation inputs. It never uploads, pushes, changes repository
visibility or copies `.git`. Its report identifies excluded inputs separately
from selected files that still require review. Do not bypass an audit failure
by declaring unknown third-party material to be project-owned.

## Source-preview review

- Preserve reviewed notices and source provenance; re-audit new or changed files.
- Keep external sensor runtime/tuning data excluded until redistribution and source-form questions are resolved; describe the dependency accurately.
- Include applicable license texts and exact upstream revisions.
- Review device identifiers, credentials and personal data, including old history.
- Make all selected documentation English and remove conversational attribution.
- Satisfy every required-source entry; pin/build every advertised component and provide corresponding source for released binaries.
- Describe historical hardware results and unqualified changes separately; state that no supported installation image is provided.
- Document proprietary runtime dependencies separately from the open source.

## Additional image or deployment review

An image/installer release additionally requires matching kernel/module builds,
a tested installation and recovery procedure, rights for every bundled runtime
component and qualification of advertised device behavior. Source-preview compile
checks do not establish these properties. The new camera loader still needs a
controlled target comparison. Do not add firmware files or deploy a test build
merely because the source audit passes.

The audit cannot prove absence of every secret, copyright issue or untranslated
sentence. Review the actual candidate and its complete build inputs. The current
blockers are recorded in `RELEASE-READINESS.md` after running the audit.

Publishing a fresh reviewed source snapshot is the intended migration path.
Publishing or rewriting existing Git history requires a separate explicit action.
Do not change device boot configuration as part of repository publication work.
