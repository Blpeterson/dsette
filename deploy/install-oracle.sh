#!/usr/bin/env bash
# Run as root after extracting this repository to /opt/dsette.
set -euo pipefail
cd /opt/dsette
if ! id dsette >/dev/null 2>&1; then
  useradd --system --user-group --home-dir /var/lib/dsette --shell /usr/sbin/nologin dsette
fi
install -d -o dsette -g dsette -m 0700 /var/lib/dsette /var/lib/dsette/tls /var/lib/dsette/placed
# Use an isolated Python runtime because the VM's system Python is 3.10.
if [ ! -x /opt/dsette/.bootstrap/bin/uv ]; then
  python3 -m pip install --disable-pip-version-check --target /opt/dsette/.bootstrap uv
fi
export UV_PYTHON_INSTALL_DIR=/opt/dsette/.python
UV=/opt/dsette/.bootstrap/bin/uv
"$UV" python install 3.12
if [ ! -x /opt/dsette/.venv/bin/python ]; then "$UV" venv --python 3.12 /opt/dsette/.venv; fi
if [ -f requirements.lock ]; then
  "$UV" pip install --python /opt/dsette/.venv/bin/python --require-hashes -r requirements.lock
  "$UV" pip install --python /opt/dsette/.venv/bin/python --no-deps .
else
  "$UV" pip install --python /opt/dsette/.venv/bin/python '.[dev]'
fi
/opt/dsette/.venv/bin/python -m pytest -q
if [ ! -s /var/lib/dsette/tls/server.key ]; then
  umask 077
  openssl req -x509 -newkey rsa:3072 -sha256 -nodes -days 365 \
    -keyout /var/lib/dsette/tls/server.key -out /var/lib/dsette/tls/server.crt \
    -subj '/CN=localhost' -addext 'subjectAltName=DNS:localhost,IP:127.0.0.1' \
    -addext 'basicConstraints=critical,CA:TRUE' >/dev/null 2>&1
  chown dsette:dsette /var/lib/dsette/tls/server.key /var/lib/dsette/tls/server.crt
  chmod 0600 /var/lib/dsette/tls/server.key
  chmod 0644 /var/lib/dsette/tls/server.crt
fi
install -o root -g root -m 0644 deploy/dsette.service /etc/systemd/system/dsette.service
systemctl daemon-reload
systemctl enable --now dsette.service
for attempt in $(seq 1 20); do
  if curl --silent --fail --cacert /var/lib/dsette/tls/server.crt https://localhost:8223/api/v1/health; then
    printf '\nDatasette TLS health check passed.\n'
    exit 0
  fi
  sleep 1
done
systemctl status dsette.service --no-pager
exit 1
