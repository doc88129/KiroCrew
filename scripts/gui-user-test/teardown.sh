#!/usr/bin/env bash
# Stop everything boot.sh started (browser, gateway, Xvfb) and drop the scratch
# home + browser profile. Safe to run twice; never touches anything outside the
# paths boot.sh recorded in $GUI_OUT.
set -uo pipefail
: "${GUI_OUT:?GUI_OUT must be set}"

if [ -f "$GUI_OUT/pids" ]; then
  # Reverse order: browser, gateway, Xvfb. Each was started with setsid or as
  # its own background job, so signal the whole group, then the pid itself.
  tac "$GUI_OUT/pids" | while read -r pid; do
    [ -n "$pid" ] || continue
    kill -TERM -- "-$pid" 2> /dev/null || kill -TERM "$pid" 2> /dev/null || true
  done
  sleep 2
  tac "$GUI_OUT/pids" | while read -r pid; do
    [ -n "$pid" ] || continue
    kill -KILL -- "-$pid" 2> /dev/null || kill -KILL "$pid" 2> /dev/null || true
  done
fi

if [ -f "$GUI_OUT/target.paths" ]; then
  while IFS='=' read -r key path; do
    case "$key" in
      home|profile)
        # Only the two mktemp directories boot.sh created; refuse anything else.
        case "$path" in
          */gui-home.??????|*/gui-chrome.??????) rm -rf -- "$path" ;;
          *) echo "refusing to remove unexpected path for $key: $path" >&2 ;;
        esac ;;
    esac
  done < "$GUI_OUT/target.paths"
fi
rm -f -- "$GUI_OUT/target.env"
echo "gui-user-test target torn down"
