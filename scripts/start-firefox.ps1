# Start Firefox with WebDriver BiDi on, so ClaudeCodeBrowserX can send real key
# and mouse events and photograph background tabs (mcp-server/bidi.py).
#
#   .\scripts\start-firefox.ps1 [-Port 9222] [-Firefox C:\path\to\firefox.exe]
#
# Uses your normal profile: your tabs, logins and the extension are all there.
# Firefox only reads the flag at startup, so it has to be started by this
# script, not already running.
#
# The port is Firefox's remote-control port. It listens on localhost only, but
# any program on this machine that can reach it can drive the browser, so leave
# it off (start Firefox the usual way) when you do not want that.

param(
    [int]$Port = 9222,
    [string]$Firefox = ""
)

$ErrorActionPreference = "Stop"

if (-not $Firefox) {
    $candidates = @(
        "$env:ProgramFiles\Mozilla Firefox\firefox.exe",
        "${env:ProgramFiles(x86)}\Mozilla Firefox\firefox.exe",
        "$env:LOCALAPPDATA\Mozilla Firefox\firefox.exe"
    )
    $Firefox = $candidates | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
    if (-not $Firefox) {
        throw "Could not find firefox.exe. Pass it with -Firefox."
    }
}

if (Get-Process firefox -ErrorAction SilentlyContinue) {
    throw ("Firefox is already running, and the debugging port can only be turned on " +
           "when it starts. Quit Firefox completely (File > Exit), then run this again.")
}

Start-Process -FilePath $Firefox -ArgumentList "--remote-debugging-port=$Port"
Write-Host "Started Firefox with WebDriver BiDi on ws://127.0.0.1:$Port"
Write-Host "Set CLAUDE_BROWSERX_BIDI_PORT=$Port for the MCP server if you chose another port."
