#!/usr/bin/env bash
# Deploy firmware/ to the board over ONE mpremote connection.
#
#   ./tools/deploy.sh                 # sync everything
#   ./tools/deploy.sh --port /dev/ttyUSB1
#   ./tools/deploy.sh --no-main       # skip main.py, leaving the board at the REPL
#
# Each mpremote connection toggles DTR/RTS and resets the ESP32, so everything is
# chained into a single invocation with "+" rather than one call per file.
#
# Recovery: hold BOOT while resetting to skip the application (see firmware/main.py).
set -euo pipefail

PORT="${HELIOSTAT_PORT:-/dev/ttyUSB0}"
SKIP_MAIN=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --port) PORT="$2"; shift 2 ;;
        --no-main) SKIP_MAIN=1; shift ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT/firmware"
find . -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null || true

root_files=()
for f in *.py; do
    if [[ "$f" == "main.py" && $SKIP_MAIN -eq 1 ]]; then
        continue
    fi
    root_files+=("$f")
done

args=(connect "$PORT" fs cp -r hal solar control net ble util :)
args+=(+ fs cp "${root_files[@]}" :)
if [[ $SKIP_MAIN -eq 1 ]]; then
    # Remove a stale main.py so the board really does stop at the REPL.
    args+=(+ exec "import os; [os.remove(f) for f in os.listdir() if f == 'main.py']")
fi

echo "deploying to $PORT$([[ $SKIP_MAIN -eq 1 ]] && echo ' (main.py skipped)')"
python3 -m mpremote "${args[@]}"
echo "done"
