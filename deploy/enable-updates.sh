#!/usr/bin/env bash
# Install the signed updater after the public repository and signing policy exist.
# Usage: sudo ./deploy/enable-updates.sh REPO_URL ALLOWED_SIGNERS TRUSTED_SHA256 [HEALTH_HOST]
set -Eeuo pipefail
umask 027

die() { printf 'enable-updates: %s\n' "$*" >&2; exit 1; }
[[ $EUID -eq 0 ]] || die 'run as root'
[[ $# -ge 3 && $# -le 4 ]] || die 'usage: REPO_URL ALLOWED_SIGNERS_FILE TRUSTED_SSH_FINGERPRINT [HEALTH_HOST]'
REPO_URL=$1
SIGNERS_SOURCE=$2
TRUSTED_FINGERPRINT=$3
HEALTH_HOST=${4:-localhost}

# Public HTTPS URL only: no embedded username/token and no local/file transport.
[[ $REPO_URL =~ ^https://[A-Za-z0-9.-]+/[A-Za-z0-9._/-]+\.git$ ]] || die 'repository must be a credential-free public HTTPS .git URL'
[[ $TRUSTED_FINGERPRINT =~ ^SHA256:[A-Za-z0-9+/=]+$ ]] || die 'trusted fingerprint must be a full SHA256 SSH fingerprint'
[[ $HEALTH_HOST =~ ^[A-Za-z0-9.-]+$ ]] || die 'invalid health host'
[[ -f $SIGNERS_SOURCE && ! -L $SIGNERS_SOURCE ]] || die 'allowed-signers source must be a regular file'
command -v git >/dev/null || die 'git is required'
command -v curl >/dev/null || die 'curl is required'
command -v visudo >/dev/null || die 'visudo is required'
[[ -x /opt/dsette/.venv/bin/python ]] || die 'existing isolated Python runtime not found'
[[ -x /opt/dsette/.bootstrap/bin/uv ]] || die 'existing uv runtime not found'
[[ -f /var/lib/dsette/tls/server.crt ]] || die 'server CA certificate not found'
[[ ! -e /opt/dsette-source ]] || die '/opt/dsette-source already exists; refusing to overwrite'
[[ ! -e /var/lib/dsette/current ]] || die 'current release link already exists; updater appears initialized'

install -d -o root -g dsette -m 0750 /etc/dsette
install -o root -g dsette -m 0640 "$SIGNERS_SOURCE" /etc/dsette/allowed_signers
install -d -o dsette -g dsette -m 0750 /opt/dsette-source /var/lib/dsette/releases /var/lib/dsette/backups /var/lib/dsette/update-jobs
install -d -o dsette -g dsette -m 0700 /var/lib/dsette/.update-cache

cleanup_failed_clone() {
  if [[ ! -e /var/lib/dsette/current ]]; then rm -rf --one-file-system /opt/dsette-source; fi
}
trap cleanup_failed_clone ERR

sudo -u dsette git clone --branch main --single-branch --no-tags "$REPO_URL" /opt/dsette-source
COMMIT=$(sudo -u dsette git -C /opt/dsette-source rev-parse --verify 'HEAD^{commit}')
[[ $COMMIT =~ ^[0-9a-f]{40,64}$ ]] || die 'clone returned an invalid commit id'
sudo -u dsette git -C /opt/dsette-source \
  -c gpg.format=ssh -c gpg.ssh.allowedSignersFile=/etc/dsette/allowed_signers \
  verify-commit --raw "$COMMIT"
ACTUAL_FINGERPRINT=$(sudo -u dsette git -C /opt/dsette-source \
  -c gpg.format=ssh -c gpg.ssh.allowedSignersFile=/etc/dsette/allowed_signers \
  show -s --format=%GF "$COMMIT")
[[ $ACTUAL_FINGERPRINT == "$TRUSTED_FINGERPRINT" ]] || die 'HEAD signer fingerprint is not the configured trusted fingerprint'

RELEASE=/var/lib/dsette/releases/$COMMIT
[[ ! -e $RELEASE ]] || die 'initial release directory already exists'
sudo -u dsette git -C /opt/dsette-source worktree add --detach "$RELEASE" "$COMMIT"
printf '%s\n' "$COMMIT" | install -o dsette -g dsette -m 0644 /dev/stdin "$RELEASE/.dsette-commit"
[[ -f $RELEASE/requirements.lock ]] || die 'signed release has no requirements.lock'
grep -q -- '--hash=' "$RELEASE/requirements.lock" || die 'requirements.lock is not hash-locked'

UV=/opt/dsette/.bootstrap/bin/uv
PY=/opt/dsette/.venv/bin/python
sudo -u dsette env UV_CACHE_DIR=/var/lib/dsette/.update-cache "$UV" venv --python "$PY" "$RELEASE/.venv"
sudo -u dsette env UV_CACHE_DIR=/var/lib/dsette/.update-cache "$UV" pip install --python "$RELEASE/.venv/bin/python" --require-hashes -r "$RELEASE/requirements.lock"
sudo -u dsette env UV_CACHE_DIR=/var/lib/dsette/.update-cache "$UV" pip install --python "$RELEASE/.venv/bin/python" --no-deps "$RELEASE"
sudo -u dsette env DSETTE_DATA_DIR="$RELEASE/.test-data" "$RELEASE/.venv/bin/python" -m pytest -q "$RELEASE/server/tests" "$RELEASE/client/tests"
rm -rf --one-file-system "$RELEASE/.test-data"

# Stable supervisor environment is outside the release symlink it changes.
install -d -o root -g root -m 0755 /opt/dsette-updater
"$UV" venv --python "$PY" /opt/dsette-updater/.venv
UV_CACHE_DIR=/var/lib/dsette/.update-cache "$UV" pip install --python /opt/dsette-updater/.venv/bin/python --require-hashes -r "$RELEASE/requirements.lock"
UV_CACHE_DIR=/var/lib/dsette/.update-cache "$UV" pip install --python /opt/dsette-updater/.venv/bin/python --no-deps "$RELEASE"
chmod -R a+rX /opt/dsette-updater

cat >/etc/dsette/update.env <<EOF
DSETTE_DATA_DIR=/var/lib/dsette
DSETTE_UPDATE_REPO=/opt/dsette-source
DSETTE_UPDATE_REMOTE=origin
DSETTE_UPDATE_REMOTE_URL=$REPO_URL
DSETTE_UPDATE_BRANCH=main
DSETTE_TRUSTED_SIGNERS=$TRUSTED_FINGERPRINT
DSETTE_SIGNATURE_FORMAT=ssh
DSETTE_SSH_ALLOWED_SIGNERS=/etc/dsette/allowed_signers
DSETTE_UPDATE_CA_FILE=/var/lib/dsette/tls/server.crt
DSETTE_UPDATE_HEALTH_URL=https://$HEALTH_HOST:8223/api/v1/health
EOF
chown root:dsette /etc/dsette/update.env
chmod 0640 /etc/dsette/update.env

cat >/etc/sudoers.d/dsette-update <<'EOF'
dsette ALL=(root) NOPASSWD: /bin/systemctl stop dsette
dsette ALL=(root) NOPASSWD: /bin/systemctl start dsette
dsette ALL=(root) NOPASSWD: /bin/systemctl restart dsette
EOF
chmod 0440 /etc/sudoers.d/dsette-update
visudo -cf /etc/sudoers.d/dsette-update >/dev/null || die 'sudoers validation failed'

install -o root -g root -m 0644 "$RELEASE/deploy/updater-trigger.path" /etc/systemd/system/dsette-updater.path
install -o root -g root -m 0644 "$RELEASE/deploy/updater-supervisor.service" /etc/systemd/system/dsette-updater.service
install -d -o root -g root -m 0755 /etc/systemd/system/dsette.service.d
cat >/etc/systemd/system/dsette.service.d/20-signed-release.conf <<'EOF'
[Service]
EnvironmentFile=/etc/dsette/update.env
WorkingDirectory=/var/lib/dsette/current
ExecStart=
ExecStart=/var/lib/dsette/current/.venv/bin/dsetd serve
EOF

ln -s "$RELEASE" /var/lib/dsette/current.new
mv -T /var/lib/dsette/current.new /var/lib/dsette/current
trap - ERR
systemctl daemon-reload
systemctl enable --now dsette-updater.path
healthy=0
systemctl restart dsette
for attempt in $(seq 1 30); do
  if curl --silent --fail --cacert /var/lib/dsette/tls/server.crt "https://$HEALTH_HOST:8223/api/v1/health" | grep -q "$COMMIT"; then healthy=1; break; fi
  sleep 1
done
if [[ $healthy -ne 1 ]]; then
  printf 'Initial signed release failed health; restoring original service command.\n' >&2
  rm -f /etc/systemd/system/dsette.service.d/20-signed-release.conf /var/lib/dsette/current
  systemctl disable --now dsette-updater.path >/dev/null 2>&1 || true
  systemctl daemon-reload
  systemctl restart dsette || true
  exit 1
fi
printf 'Signed updater enabled at commit %s.\n' "$COMMIT"
