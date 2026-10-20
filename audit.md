# Linux Data Guardian — Audit (historical, superseded)

This file replaces an earlier 20 KB audit that described commit `7caf94d` as a
prototype with a broken privilege boundary. That snapshot is obsolete. The
issues it raised were carried into `ImprovePlan.md` as BUG IDs and most are now
fixed on `main`.

## Findings that were real

| Original concern | Current state |
| :--- | :--- |
| Unprivileged daemon could not apply `nft` rules; failures were silent. | Fixed: `NftablesManager` calls the root-owned helper and returns `(ok, message)`; failures are surfaced in the status snapshot and as a critical notification. |
| A root APT hook imported user-writable Python (LPE). | Removed. APT is blocked with `skuid 42` and allowed only through a timed helper-independent window. No root Python import of project code remains. |
| Strict mode dropped everything because the cgroup hierarchy did not exist. | Fixed: the helper creates `data_guardian/{foreground,whitelist}`, the daemon moves focused/whitelisted process trees into them, and a port-allowlist fallback is used when cgroup v2 is unavailable. |
| Quota arithmetic was duplicated between DAO and callers. | Fixed: `quota.evaluate()` is the only implementation; statistics and quota bookkeeping are separate tables (schema v3). |
| Notification reads consumed the queue. | Fixed: notifications live in a table and clients use a non-destructive cursor. |
| The uninstaller could not remove shims. | Fixed: it runs `remove-shims` with the correct user, home, and `PYTHONPATH`; `--purge` removes data. |
| Two daemons could fight over the socket/firewall. | Fixed: instance `flock` plus a stale-socket probe. A second live server refuses to start. |

## Findings that were wrong or stale

* "Firewall enforcement is zero" was true only for the audited commit; the
  helper path is installed and exercised now.
* Port allowlisting was described as the permanent strict implementation. It is
  the fallback; cgroups are the primary mechanism.
* Telemetry blocking was never implemented as a distinct feature. Balanced mode
  blocks APT (UID 42) and relies on strict mode for everything else.

The authoritative, evidence-based issue list and fixes are in
`ImprovePlan.md`. Current behaviour is documented in `README.md` and verified in
`report.md`.
