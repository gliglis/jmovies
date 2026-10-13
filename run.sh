#!/usr/bin/env bash
# run.sh — przygotowuje środowisko i uruchamia program LOKALNIE (bez SSH).
# Domyślnie PODGLĄD. Aby wykonać zmiany:   ./run.sh --dir /volume1/video/Filmy --apply
# Argumenty są przekazywane dalej do organize.py.

set -euo pipefail
cd "$(dirname "$0")"

if [[ ! -d .venv ]]; then
  echo "Tworzę lokalne środowisko Python (.venv)..."
  python3 -m venv .venv
fi

source .venv/bin/activate
pip install --quiet --upgrade pip
pip install --quiet requests

python organize.py "$@"