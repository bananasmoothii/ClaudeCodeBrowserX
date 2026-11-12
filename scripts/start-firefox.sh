#!/usr/bin/env bash
# Start Firefox with WebDriver BiDi on, so ClaudeCodeBrowserX can send real key
# and mouse events and photograph background tabs (mcp-server/bidi.py).
#
#   scripts/start-firefox.sh [port]        (default 9222)
#   FIREFOX=/path/to/firefox scripts/start-firefox.sh
#
# Uses your normal profile: your tabs, logins and the extension are all there.
# Firefox only reads the flag at startup, so it has to be started by this
# script, not already running.
#
# The port is Firefox's remote-control port. It listens on localhost only, but
# any program on this machine that can reach it can drive the browser, so leave
# it off (start Firefox the usual way) when you do not want that.

set -euo pipefail

PORT="${1:-9222}"

find_firefox() {
    if [ -n "${FIREFOX:-}" ]; then echo "$FIREFOX"; return; fi
    if command -v firefox >/dev/null 2>&1; then command -v firefox; return; fi
    local mac="/Applications/Firefox.app/Contents/MacOS/firefox"
    if [ -x "$mac" ]; then echo "$mac"; return; fi
    return 1
}

BIN="$(find_firefox)" || { echo "Could not find Firefox. Set FIREFOX=/path/to/firefox." >&2; exit 1; }

if pgrep -x firefox >/dev/null 2>&1 || pgrep -x firefox-bin >/dev/null 2>&1; then
    echo "Firefox is already running, and the debugging port can only be turned on" >&2
    echo "when it starts. Quit Firefox completely, then run this again." >&2
    exit 1
fi

nohup "$BIN" --remote-debugging-port="$PORT" >/dev/null 2>&1 &
echo "Started Firefox with WebDriver BiDi on ws://127.0.0.1:$PORT"
echo "Set CLAUDE_BROWSERX_BIDI_PORT=$PORT for the MCP server if you chose another port."
