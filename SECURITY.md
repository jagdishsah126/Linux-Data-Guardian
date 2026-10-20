# Security Policy

## Supported version

The `main` branch and the current 2.x code are supported.

## Reporting a vulnerability

Please use GitHub's **private security advisory** feature for this repository.
Do not open a public issue containing:

- firewall bypass details for an unpatched version;
- sudoers or helper exploitation steps;
- your `config.yaml` or `guardian.db`;
- logs containing SSIDs, connection UUIDs, interface names, usernames, or paths;
- screenshots that identify your device or network.

If you are unsure whether something is sensitive, redact it first. A minimal,
sanitized reproduction is much more useful than a full log dump.

## Security model

Linux Data Guardian separates ordinary application code from the privileged
surface:

- The daemon and UI run as the desktop user.
- The only root process is `/usr/local/libexec/data-guardian-helper`.
- Sudoers grants exactly that one command, with no wildcard.
- The helper accepts a fixed action list and validates every ruleset with
  `nft -c` before applying it.
- The helper rejects `flush ruleset`, `include`, `exec`, `define`, and any table
  other than `table inet data_guardian`.
- Project Python is never imported by a root-owned interpreter.
- The IPC socket is owner-only and lives in a private runtime directory.
- The config and database are owner-only; the database uses WAL mode.

These properties are covered by tests, but the tests are not a substitute for
reviewing changes to the helper, installer, sudoers template, systemd unit, or
firewall renderer.

## Not in scope

The project does not defend against:

- an attacker who already has root;
- a compromised kernel, firmware, or NetworkManager;
- a malicious package installed by the user with their own privileges;
- physical access to an unlocked machine;
- traffic that does not pass through the monitored interface;
- VPN or proxy software that intentionally bypasses the interface rules;
- Wayland focus tracking (strict-mode foreground tracking is X11-only).

## Safe defaults

- The default profile is `balanced`.
- A quota limit of `0` means unlimited.
- `off` removes the Data Guardian firewall table entirely.
- `data-guardian bypass` and `scripts/emergency-disable.sh` are intentional
  recovery paths, not vulnerabilities.

## Privacy

There is no telemetry, account, cloud service, or remote listener. All state is
local. When sharing diagnostics, sanitize network names, UUIDs, interface names,
usernames, and file paths.
