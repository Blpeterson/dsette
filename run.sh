#!/usr/bin/env bash
set -euo pipefail
ROOT=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
LAUNCHER="$ROOT/scripts/dsette_launcher.py"
[[ -f "$LAUNCHER" ]] || { printf 'Datasette launcher is missing: %s\n' "$LAUNCHER" >&2; exit 1; }

valid_python() { "$@" -c 'import sys; raise SystemExit(sys.version_info < (3, 9))' >/dev/null 2>&1; }
for candidate in "$ROOT/.venv312/bin/python" "$ROOT/.venv/bin/python"; do
  if [[ -x "$candidate" ]] && valid_python "$candidate"; then exec "$candidate" "$LAUNCHER" run "$@"; fi
done
if command -v python3 >/dev/null 2>&1 && valid_python python3; then exec python3 "$LAUNCHER" run "$@"; fi
if command -v python >/dev/null 2>&1 && valid_python python; then exec python "$LAUNCHER" run "$@"; fi
if command -v py >/dev/null 2>&1 && valid_python py -3; then exec py -3 "$LAUNCHER" run "$@"; fi
printf 'Datasette requires Python 3.9+ to start its isolated launcher.\n' >&2
exit 1

