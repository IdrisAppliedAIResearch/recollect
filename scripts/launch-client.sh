#!/usr/bin/env bash
# Ubuntu Surface launcher. The desktop owns models and conversation storage.
set -euo pipefail

recollect_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd -- "$recollect_root"

for argument in "$@"; do
    if [[ "$argument" == "--help" || "$argument" == "-h" ]]; then
        cat <<'USAGE'
Usage: bash scripts/launch-client.sh --desktop-url https://DESKTOP_IP:8080 --token-file /path/to/token
       bash scripts/launch-client.sh --env-file /path/to/client.env
       recollect-deploy

Optional: --port 8080 (local Surface UI port; browser stays on localhost).
Requires uv. A missing or stale UI build also requires Node.js and npm.
The first launch installs Python 3.13 and client-only dependencies through uv.
Install the short command once with scripts/install-client-command.sh.
USAGE
        exit 0
    fi
done

saved_args=()
launch_args=()
while (( $# )); do
    if [[ "$1" == '--connection-file' ]]; then
        if (( $# < 2 )) || [[ ! -f "$2" || ! -r "$2" ]]; then
            echo 'Saved client connection is missing. Run install-client-command.sh again.' >&2
            exit 1
        fi
        # NUL-delimited data preserves paths literally. Never execute saved settings.
        mapfile -d '' -t saved < "$2"
        if (( ${#saved[@]} != 3 )) || [[ "${saved[0]}" != 'recollect-client-connection-v1' ]]; then
            echo 'Saved client connection is invalid. Run install-client-command.sh again.' >&2
            exit 1
        fi
        saved_args=(--desktop-url "${saved[1]}" --token-file "${saved[2]}")
        shift 2
    else
        launch_args+=("$1")
        shift
    fi
done

command -v uv >/dev/null || { echo 'Install uv before launching the client.' >&2; exit 1; }

ui_index="$recollect_root/ui/dist/index.html"
build_ui=false
if [[ ! -f "$ui_index" ]]; then
    build_ui=true
elif [[ -n "$(find ui/src ui/public ui/index.html ui/package.json ui/package-lock.json ui/vite.config.ts ui/tsconfig.json -type f -newer "$ui_index" -print -quit)" ]]; then
    build_ui=true
fi

if [[ "$build_ui" == true ]]; then
    command -v npm >/dev/null || { echo 'Install Node.js/npm to build the client UI.' >&2; exit 1; }
    (
        cd -- "$recollect_root/ui"
        if [[ ! -d node_modules || ! -f node_modules/.recollect-install || package-lock.json -nt node_modules/.recollect-install ]]; then
            npm ci
            touch node_modules/.recollect-install
        fi
        npm run build
    )
fi

# Script metadata gives the Surface an isolated environment: no episodic,
# embedding weights, CUDA packages, Docker, or sibling repository are required.
exec uv run --locked --script --python 3.13 "$recollect_root/deploy/client.py" "${saved_args[@]}" "${launch_args[@]}"
