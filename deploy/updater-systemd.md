# Signed update supervisor setup

Updates are disabled by default. They fail closed until a Git remote exists,
commits are signed, and the signer allow-list is configured. The current
`/opt/dsette` deployment has no Git remote, so do not enable this yet.

Required environment (store in root-owned `/etc/dsette/update.env`, mode 0640):

```ini
DSETTE_UPDATE_REPO=/opt/dsette-source
DSETTE_UPDATE_REMOTE=origin
DSETTE_UPDATE_REMOTE_URL=https://example.com/OWNER/REPOSITORY.git
DSETTE_UPDATE_BRANCH=main
DSETTE_TRUSTED_SIGNERS=FULL_GPG_OR_SSH_FINGERPRINT
DSETTE_SIGNATURE_FORMAT=gpg
DSETTE_UPDATE_CA_FILE=/var/lib/dsette/tls/server.crt
```

For SSH-signed commits also set `DSETTE_SIGNATURE_FORMAT=ssh` and
`DSETTE_SSH_ALLOWED_SIGNERS=/etc/dsette/allowed_signers`. Pin principals and
public keys in that root-owned file. Never put repository credentials in a Git
remote URL; use a read-only deploy key in a 0600 file outside the repository.

The service must run from `/var/lib/dsette/current/.venv/bin/dsetd`. Install
`updater-trigger.path` as `dsette-updater.path` and
`updater-supervisor.service` as `dsette-updater.service`; install a stable copy
of the updater package in `/opt/dsette-updater/.venv`. The API only creates a
0600 job file and `/var/lib/dsette/update-trigger`. The separate unprivileged
supervisor unit processes queued jobs. It is not a child of `dsette`, so a
`KillMode=control-group` restart cannot kill it. The
supervisor stages and tests a signed release, takes an online SQLite backup,
atomically changes `current`, restarts only `dsette`, checks the new commit for
30 seconds, and rolls back both code and DB automatically on failure.

Before enabling updates, initialize `current` to the presently tested Git
worktree and change `dsette.service` to execute through that symlink. The
updater refuses to switch anything if `current` is absent or is not a symlink;
this prevents an untracked first deployment from losing its rollback target.
Create `/var/lib/dsette/releases` as `dsette:dsette` mode 0750. The source
checkout must be writable only by `dsette` and administrators. Login-time
checks use credential-free `DSETTE_UPDATE_REMOTE_URL` with `git ls-remote` and
do not fetch into the source checkout.

The supervisor runs as `dsette`, not root. Install only these exact rules using
`visudo`:

```sudoers
dsette ALL=(root) NOPASSWD: /bin/systemctl stop dsette
dsette ALL=(root) NOPASSWD: /bin/systemctl start dsette
dsette ALL=(root) NOPASSWD: /bin/systemctl restart dsette
```

Never grant wildcard systemctl or shell access. Add `from
dsette_server.updater_routes import router as updater_router`
and `app.include_router(updater_router)` to the application, and reject new
announcements/chunks with HTTP 503 whenever `maintenance_active()` is true
(the shared marker is `/var/lib/dsette/maintenance`).
Live relays must create `/var/lib/dsette/active-files-PID-TRANSFER_ID` at
announce and unlink it on every completion/abort cleanup path. The updater
checks these cross-process markers as well as stored-transfer rows, and removes
markers whose Linux PID no longer exists.
Expose the deployed Git commit from `/api/v1/health` as `git_commit`; health
verification deliberately fails until this is wired.

Enable the supervisor after configuration:

```bash
sudo install -m 0644 deploy/updater-trigger.path /etc/systemd/system/dsette-updater.path
sudo install -m 0644 deploy/updater-supervisor.service /etc/systemd/system/dsette-updater.service
sudo systemctl daemon-reload
sudo systemctl enable --now dsette-updater.path
# Queue a manual job through dsetd or the authenticated admin API.
```

Do not accept an arbitrary branch/ref from an API request. The configured
branch is the sole update source. Keep signature verification enabled.

