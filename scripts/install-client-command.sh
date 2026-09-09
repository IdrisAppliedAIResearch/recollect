#!/usr/bin/env bash
# Install a private, reusable launcher without editing shell startup files.
set -euo pipefail

usage() {
    cat <<'USAGE'
Usage: bash scripts/install-client-command.sh --desktop-url https://DESKTOP_IP:8080 --token-file /path/to/token

Installs recollect-deploy in ~/.local/bin and saves the desktop address and
token-file path under ${XDG_CONFIG_HOME:-$HOME/.config}/recollect/client.args.
The token itself stays in its original file. No service is started.
Requires Ubuntu's Python 3 to secure the copied pairing file.

Optional: --bin-dir DIRECTORY, --config-dir DIRECTORY (for custom installations).
Run the installer again to update the saved connection or repository location.
USAGE
}

desktop_url=''
token_file=''
bin_dir="$HOME/.local/bin"
config_dir="${XDG_CONFIG_HOME:-$HOME/.config}/recollect"
while (( $# )); do
    case "$1" in
        --help|-h) usage; exit 0 ;;
        --desktop-url|--token-file|--bin-dir|--config-dir)
            if (( $# < 2 )) || [[ -z "$2" ]]; then
                printf 'A value is required for %s.\n' "$1" >&2
                exit 1
            fi
            case "$1" in
                --desktop-url) desktop_url="$2" ;;
                --token-file) token_file="$2" ;;
                --bin-dir) bin_dir="$2" ;;
                --config-dir) config_dir="$2" ;;
            esac
            shift 2
            ;;
        *) printf 'Unknown installer option: %s\n' "$1" >&2; exit 1 ;;
    esac
done

recollect_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
launcher="$recollect_root/scripts/launch-client.sh"
command_path="$bin_dir/recollect-deploy"
config_path="$config_dir/client.args"
marker='# Recollect managed Surface command v1'
if [[ ! -f "$launcher" ]]; then
    echo 'Run this installer from a Recollect checkout with scripts/launch-client.sh.' >&2
    exit 1
fi
if [[ -L "$command_path" || -e "$command_path" ]]; then
    if [[ -L "$command_path" || ! -f "$command_path" ]] ||
        [[ "$(sed -n '2p' -- "$command_path")" != "$marker" ]]; then
        printf 'Refusing to overwrite an unrelated command: %s\n' "$command_path" >&2
        exit 1
    fi
fi
if [[ -L "$config_path" || -e "$config_path" ]]; then
    if [[ -L "$config_path" || ! -f "$config_path" ]]; then
        printf 'Refusing to replace the existing configuration: %s\n' "$config_path" >&2
        exit 1
    fi
    mapfile -d '' -t saved < "$config_path"
    if (( ${#saved[@]} != 3 )) || [[ "${saved[0]}" != 'recollect-client-connection-v1' ]]; then
        printf 'Refusing to overwrite an unrelated configuration: %s\n' "$config_path" >&2
        exit 1
    fi
    desktop_url="${desktop_url:-${saved[1]}}"
    token_file="${token_file:-${saved[2]}}"
fi

# Accept an origin only; the Python deployment validator checks it again at launch.
origin_pattern='^https?://([[:alnum:]][[:alnum:]._-]*|\[[0-9a-fA-F:.]+\])(:([0-9]{1,5}))?/?$'
if [[ ! "$desktop_url" =~ $origin_pattern ]]; then
    echo 'Provide --desktop-url as an http:// or https:// host address, with an optional port and no path.' >&2
    exit 1
fi
port="${BASH_REMATCH[3]}"
if [[ -n "$port" ]] && (( 10#$port < 1 || 10#$port > 65535 )); then
    echo 'The desktop URL port must be between 1 and 65535.' >&2
    exit 1
fi
desktop_url="${desktop_url%/}"
if [[ ! -f "$token_file" || ! -r "$token_file" ]]; then
    echo 'Provide --token-file pointing to the readable token copied from the desktop.' >&2
    exit 1
fi
command -v python3 >/dev/null || { echo 'Python 3 is required to secure pairing credentials.' >&2; exit 1; }
python3 -I - "$recollect_root" "$token_file" <<'PYTHON'
import sys
from pathlib import Path

sys.path.insert(0, str(Path(sys.argv[1]) / "src"))
from recollect.private_files import read_private

read_private(Path(sys.argv[2]))
PYTHON
IFS= read -r -d '' token_file < <(realpath -z -- "$token_file")

umask 077
mkdir -p -- "$bin_dir" "$config_dir"
chmod 700 -- "$config_dir"
IFS= read -r -d '' bin_dir < <(realpath -z -- "$bin_dir")
IFS= read -r -d '' config_dir < <(realpath -z -- "$config_dir")
command_path="$bin_dir/recollect-deploy"
config_path="$config_dir/client.args"
config_tmp=''
command_tmp=''
cleanup() {
    if [[ -n "$config_tmp" ]]; then rm -f -- "$config_tmp"; fi
    if [[ -n "$command_tmp" ]]; then rm -f -- "$command_tmp"; fi
}
trap cleanup EXIT
config_tmp="$(mktemp "$config_dir/.client.args.XXXXXX")"
command_tmp="$(mktemp "$bin_dir/.recollect-deploy.XXXXXX")"
printf '%s\0' 'recollect-client-connection-v1' "$desktop_url" "$token_file" > "$config_tmp"
{
    printf '#!/usr/bin/env bash\n%s\nset -euo pipefail\n' "$marker"
    printf 'exec bash %q --connection-file %q "$@"\n' "$launcher" "$config_path"
} > "$command_tmp"
chmod 600 -- "$config_tmp"
chmod 755 -- "$command_tmp"
mv -f -- "$config_tmp" "$config_path"
config_tmp=''
mv -f -- "$command_tmp" "$command_path"
command_tmp=''
printf 'Installed %s\nSaved connection: %s\n' "$command_path" "$config_path"
case ":$PATH:" in
    *":$bin_dir:"*) printf 'Launch the Surface UI with: recollect-deploy\n' ;;
    *)
        printf 'For this terminal, run once:\n  export PATH=%q:"$PATH"\n' "$bin_dir"
        printf 'Then launch the Surface UI with: recollect-deploy\n'
        ;;
esac
