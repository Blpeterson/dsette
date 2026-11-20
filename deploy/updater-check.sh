#!/usr/bin/env bash
set -euo pipefail
test -f /etc/dsette/update.env
set -a
. /etc/dsette/update.env
set +a
test -n "${DSETTE_TRUSTED_SIGNERS:-}"
git -C "${DSETTE_UPDATE_REPO:?}" remote get-url "${DSETTE_UPDATE_REMOTE:-origin}" >/dev/null
test -f "${DSETTE_UPDATE_REPO}/requirements.lock"
grep -q -- '--hash=' "${DSETTE_UPDATE_REPO}/requirements.lock"
echo 'Updater prerequisites are present; no update was applied.'
