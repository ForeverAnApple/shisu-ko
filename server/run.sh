#!/usr/bin/env bash
# Starts the Shisu-ko transcription server (Linux/macOS) and restarts it if it stops
# unexpectedly. Extra arguments are passed through, e.g. ./run.sh --cookies-from-browser firefox
set -uo pipefail
VENV="${HOME}/.shisu-ko/venv"
HERE="$(cd "$(dirname "$0")" && pwd)"
[ -x "${VENV}/bin/python" ] || { echo "Run ./setup.sh first"; exit 1; }

while true; do
  "${VENV}/bin/python" "${HERE}/server.py" "$@"
  code=$?
  [ "$code" -eq 0 ] && exit 0
  [ "$code" -eq 2 ] && exit 2
  echo "The server stopped unexpectedly (exit code $code). Restarting in 5 seconds... press Ctrl+C to quit."
  sleep 5
done
