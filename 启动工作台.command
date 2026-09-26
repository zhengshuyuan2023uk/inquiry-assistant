#!/bin/zsh
set -e
cd -- "$(dirname -- "$0")"
exec python3 -B start_workbench.py --background --open "$@"
