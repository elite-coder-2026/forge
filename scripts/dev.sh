#!/usr/bin/env bash
# Restart forge whenever a .py file under forge/ changes.
#
#   scripts/dev.sh              interactive REPL (-i)
#   scripts/dev.sh --plan -i    any forge arguments (keep them simple: no spaces inside one argument)
#
# Needs the dev extra:  pip install -e ".[dev]"
# A restart drops what you were typing; the conversation is resumed from the saved session.
set -euo pipefail
cd "$(dirname "$0")/.."
PY=".venv/bin/python"
exec "$PY" -m watchfiles --filter python "$PY -m forge.main ${*:--i}" forge
