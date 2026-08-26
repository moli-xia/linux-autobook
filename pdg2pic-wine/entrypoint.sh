#!/bin/sh
set -eu

source_dir="${1:-}"
result_path="${2:-}"
timeout_seconds="${PDG2PIC_TIMEOUT_SECONDS:-7200}"

if [ -z "$source_dir" ] || [ -z "$result_path" ]; then
    echo "usage: pdg2pic-convert SOURCE_DIRECTORY RESULT_PDF" >&2
    exit 2
fi
if [ ! -d "$source_dir" ]; then
    echo "Pdg2Pic source directory does not exist: $source_dir" >&2
    exit 2
fi

source_dir="$(readlink -f "$source_dir")"
if [ "$source_dir" = "/work/input" ]; then
    echo "Pdg2Pic source must be outside the container-private /work/input path" >&2
    exit 2
fi
result_dir="$(dirname "$result_path")"
mkdir -p "$result_dir"
result_dir="$(readlink -f "$result_dir")"
result_path="$result_dir/$(basename "$result_path")"

# The GUI folder chooser is deliberately driven to this private, fixed path.
# The gateway passes a shared-volume source and the ephemeral container copies
# it here, so no host path or book title ever needs to be typed into the GUI.
rm -rf /work/input
mkdir -p /work/input
cp -a "$source_dir"/. /work/input/
rm -f /work/input.pdf "$result_path"

log_path="$result_dir/pdg2pic-wine.log"
: >"$log_path"
Xvfb "$DISPLAY" -screen 0 1280x800x24 -nolisten tcp >>"$log_path" 2>&1 &
xvfb_pid=$!
wine_pid=""

cleanup() {
    if [ -n "$wine_pid" ]; then
        kill "$wine_pid" 2>/dev/null || true
    fi
    wineserver -k 2>/dev/null || true
    kill "$xvfb_pid" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

wait_for_window() {
    pattern="$1"
    attempts="$2"
    count=0
    while [ "$count" -lt "$attempts" ]; do
        window="$(xdotool search --onlyvisible --name "$pattern" 2>/dev/null | tail -1 || true)"
        if [ -n "$window" ]; then
            printf '%s\n' "$window"
            return 0
        fi
        count=$((count + 1))
        sleep 0.25
    done
    return 1
}

sleep 1
(
    cd /opt/pdg2pic
    wine ./Pdg2Pic.exe
) >>"$log_path" 2>&1 &
wine_pid=$!

main_window="$(wait_for_window '^Pdg2Pic$' 120)" || {
    echo "Pdg2Pic main window did not appear" >&2
    tail -n 80 "$log_path" >&2 || true
    exit 1
}
xdotool windowmove "$main_window" 268 83
xdotool windowfocus --sync "$main_window"

# Open the source-folder chooser (button at 590,37 relative to the fixed main
# window), then traverse My Computer -> Z: -> work -> input.
xdotool mousemove --window "$main_window" 590 37 click 1
folder_window="$(wait_for_window '^选择存放PDG文件的文件夹$' 80)" || {
    echo "Pdg2Pic source-folder chooser did not appear" >&2
    exit 1
}
xdotool windowmove "$folder_window" 451 251
xdotool mousemove --window "$folder_window" 40 119 click 1
sleep 0.25
xdotool mousemove --window "$folder_window" 59 151 click 1
sleep 0.25
xdotool mousemove --window "$folder_window" 199 199 click --repeat 12 --delay 25 5
sleep 0.25
xdotool mousemove --window "$folder_window" 77 183 click 1
sleep 0.25
xdotool mousemove --window "$folder_window" 109 199 click 1
sleep 0.25
xdotool mousemove --window "$folder_window" 227 297 click 1

statistics_window="$(wait_for_window '^格式统计$' 240)" || {
    echo "Pdg2Pic did not finish scanning the source directory" >&2
    exit 1
}
xdotool windowfocus --sync "$statistics_window"
xdotool key --window "$statistics_window" Return

# The enabled top-right button is "4、开始转换". On a cold Wine start the
# statistics dialog can disappear slightly before the main form accepts this
# click, so retry only until the output file is first created.
sleep 3
xdotool windowfocus --sync "$main_window"
start_attempt=0
while [ "$start_attempt" -lt 10 ] && [ ! -f /work/input.pdf ]; do
    xdotool mousemove --window "$main_window" 676 62 click 1
    start_attempt=$((start_attempt + 1))
    sleep 5
done
if [ ! -f /work/input.pdf ]; then
    echo "Pdg2Pic did not start converting after the source scan" >&2
    exit 1
fi

deadline=$(( $(date +%s) + timeout_seconds ))
previous_size=-1
stable_count=0
while [ "$(date +%s)" -lt "$deadline" ]; do
    if [ -f /work/input.pdf ]; then
        current_size="$(stat -c %s /work/input.pdf)"
        if [ "$current_size" = "$previous_size" ] && [ "$current_size" -gt 0 ]; then
            stable_count=$((stable_count + 1))
        else
            previous_size="$current_size"
            stable_count=0
        fi
        if [ "$stable_count" -ge 3 ]; then
            break
        fi
    fi
    if ! kill -0 "$wine_pid" 2>/dev/null; then
        echo "Pdg2Pic exited before producing a stable PDF" >&2
        exit 1
    fi
    sleep 2
done

if [ ! -s /work/input.pdf ] || [ "$stable_count" -lt 3 ]; then
    echo "Pdg2Pic conversion timed out or produced no PDF" >&2
    exit 1
fi

cp -f /work/input.pdf "$result_path"
printf 'Pdg2Pic conversion complete: %s bytes\n' "$(stat -c %s "$result_path")"
