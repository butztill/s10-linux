<!-- SPDX-License-Identifier: GPL-2.0-only -->

# Shannon modem integration

This directory contains the S10-specific ModemManager plugin, raw-IP bearer
configuration, AT/IPC daemon extensions, SMS bridge and CLAT integration.
Recovered daemon dependencies and the reference CP boot/TTY sources are included.
No modem firmware, SIM state, EFS/NV data or vendor executables are included.

## Source boundary

The daemon's `rfsd.py`, `s10cp.py` and `ipc.py` dependencies have been recovered
from the development installation. Their original implementation and GPL-2.0-only
grant are confirmed. The boot/link startup scripts, radio-image copier, CP boot
module and TTY/network module are now included as source as well. Source hashes
and the scope of these grants are recorded in
[the recovery manifest](../../release/modem-source-recovery.json).
No firmware, NV/calibration files or subscriber data were copied.

`mm-plugin-s10.c` uses ModemManager's internal API. Its matching Alpine package
`1.25.95_git20261005` uses commit `7f0bf17ab518cd78925cf7e53cc0d5c42f88b41a`.
The public source archive matches the package recipe SHA-512. Native ModemManager
and plugin builds passed; the ARM64 CP boot and TTY/network modules also built.
The earlier development source snapshot differs in twelve files and is not the
release input. The matching source/configuration is required; a version string
alone does not establish target ABI compatibility.

`build-mm-plugin.sh` now requires explicit source, configured build and new output
paths. It prefers matching uninstalled pkg-config files, refuses an existing
output and installs nothing. See [building](../../docs/public/BUILDING.md).

The recovered startup helpers preserve the existing development behavior. They
are reference integration for an already configured SM-G973F, not a generic
installer: paths, module parameters and firmware sizes are installation-specific.
`load-radio.py` reads an externally supplied radio image and may load local NV
state into modem RAM. None of these helpers was executed during source recovery.
Do not interpret source availability as a new boot, modem or recovery qualification.

The two CLAT copies retain the complete upstream MIT notice. They derive from
clatd 2.1.0 with one local diagnostic sentence changed. TAYGA is a separate
external dependency and is not bundled here. Carrier APNs, NAT64 discovery,
route policy and NetworkManager profiles require target-specific configuration.

## Host checks

These checks use synthetic transports and do not contact a SIM or network:

```sh
python3 userspace/modem/test-daemon.py
python3 userspace/modem/test-sms.py
python3 userspace/modem/test-telephony-policy.py
```

The C IP-configuration parser can be checked with GLib development headers:

```sh
cc -std=gnu11 -Wall -Wextra -Werror \
  $(pkg-config --cflags glib-2.0) userspace/modem/test-ip-config.c \
  -o /tmp/test-s10-ip-config $(pkg-config --libs glib-2.0)
/tmp/test-s10-ip-config
```

## Operation and limitations

Only one process may own the CP receive path. Do not run the exclusive IPC
probes alongside the normal daemon. CP boot/authentication must occur once per
phone boot. Enter SIM PINs locally through the device interface.

If a userspace daemon restart is necessary on an existing development setup,
stop ModemManager first, restart the modem daemon, then start ModemManager.
Restarting the service that also boots the CP is not equivalent to restarting
only its AT daemon. Historical one-use deployment scripts are not general
recovery instructions. Identity, message content and call-number logging must
remain disabled or redacted.

See [hardware status](../../docs/public/STATUS.md),
[source provenance](../../docs/public/PROVENANCE.md) and
[build validation](../../docs/public/BUILD-VALIDATION.md) for current scope.
