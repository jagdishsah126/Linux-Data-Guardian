# Contributing to Linux Data Guardian

Thanks for taking the time to improve the project. This is a small, security-
sensitive Linux application; clarity and safety matter more than clever code.

## Development setup

```bash
git clone https://github.com/jagdishsah126/Linux-Data-Guardian.git
cd Linux-Data-Guardian

python3 -m pytest -q
ruff check src tests
```

The test suite is hermetic: it uses temporary XDG directories and does not touch
your real config, database, firewall, or systemd units. Tests that need root or
kernel features skip themselves when those features are unavailable.

Runtime dependencies are system packages, not Python packages installed with
pip:

```bash
sudo apt install python3-gi python3-yaml nftables network-manager
```

See the README for the full list and optional packages.

## Before changing code

Read [File.md](File.md). It maps every file and explains the design invariants.

The most important ones:

1. **One daemon writer.** Only the daemon applies firewall rules and mutates
   quota state.
2. **One quota engine.** `src/data_guardian/quota.py` is the only place that
   compares usage with limits.
3. **One privileged surface.** `scripts/data-guardian-helper` accepts fixed
   actions and validates rulesets. Do not add a second sudoers entry.
4. **No root Python.** Never import project code as root. If a script needs to
   run project Python as the desktop user, use `sudo -u "$TARGET_USER"`.
5. **Statistics are not quota.** `daily_usage` and `network_daily_usage` are for
   display; `quota_state` is authoritative.
6. **Metered is frozen at write time.** Changing the designated network must not
   rewrite historical rows.
7. **Fail visibly.** Firewall failures must reach `status`, notifications, and
   `doctor`.

## Changing behaviour

When you change behaviour, update both:

- `README.md` for users;
- `File.md` for people reading the implementation.

If you add a new file, add it to the file guide. If you add a new runtime
directory, add it to `scripts/deploy.sh`.

## Tests

Add a regression test for every bug fix. Prefer a test that fails before the fix
and passes after it.

Useful commands:

```bash
python3 -m pytest -q
python3 -m pytest -q tests/test_quota.py
python3 -m pytest -q -k firewall
```

The firewall tests parse real rules with `nft -c` when `nft` is available. They
should not require root.

## Style

- Python 3.10+ syntax, type hints where they improve clarity.
- Keep line length at or below 100 characters.
- Prefer standard library over new dependencies.
- Do not add a dependency without a clear reason.
- Comments should explain *why*, not restate the code.

## Privacy in tests and docs

Use example values only:

```text
SSID:          Example Wi-Fi
Interface:     wlan0
UUID:          uuid-example
Username:      user
```

Never commit real SSIDs, connection UUIDs, interface names, hostnames, usernames,
home-directory paths, logs, database files, or screenshots that identify a
device or network.

## Security-sensitive changes

Changes to any of these need extra care and a test:

- `scripts/data-guardian-helper`
- `scripts/install.sh`
- `scripts/uninstall.sh`
- `config/sudoers-data-guardian`
- `systemd/data-guardian.service`
- `src/data_guardian/firewall/`
- `src/data_guardian/ipc.py`
- permission handling in `src/data_guardian/database.py`

Do not widen the sudoers rule. Do not add `include`, `flush ruleset`, or foreign
table support to the helper. Do not let the helper interpret caller-supplied
code.

## Submitting changes

1. Create a focused branch.
2. Run the test suite and lint.
3. Keep commits small and descriptive.
4. Explain what changed and why in the pull request.
5. Do not include personal data or machine-specific paths.

For security reports, follow [SECURITY.md](SECURITY.md) instead of opening a
public issue with sensitive logs.
