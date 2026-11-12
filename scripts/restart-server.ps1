# Replace the running ClaudeCodeBrowserX MCP server with the installed code.
#
#   .\scripts\restart-server.ps1 [-Python C:\path\to\python.exe] [-Port 8765]
#
# Why not Stop-Process? Firefox allows one WebDriver BiDi session, and a server
# killed outright cannot end it: real keystrokes then stay locked out until
# Firefox itself is restarted. This asks the server to stop through
# POST /admin/shutdown (API key required), which ends the session first, waits
# for it to go, and starts the installed copy again.
#
# The stdio wrapper only starts the server when an MCP client connects, so a
# stopped server stays stopped; this script starts the new one itself.
#
# Copy changed files into ~/.claudecodebrowserx first (scripts\install.ps1).

param(
    [string]$Python = "",
    [int]$Port = $(if ($env:CLAUDE_BROWSERX_HTTP_PORT) { [int]$env:CLAUDE_BROWSERX_HTTP_PORT } else { 8765 })
)

$ErrorActionPreference = "Stop"
$InstallDir = Join-Path $env:USERPROFILE ".claudecodebrowserx"
$Server = Join-Path $InstallDir "mcp-server\server.py"
$TokenFile = Join-Path $InstallDir "api_token"
$Base = "http://127.0.0.1:$Port"

if (-not (Test-Path $Server)) { throw "Not installed: $Server is missing. Run scripts\install.ps1 first." }
if (-not $Python) {
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if (-not $cmd) { throw "python not found on PATH. Pass -Python C:\path\to\python.exe." }
    $Python = $cmd.Source
}

function Test-Health {
    try { Invoke-RestMethod -Uri "$Base/health" -TimeoutSec 2 | Out-Null; return $true }
    catch { return $false }
}

if (Test-Health) {
    if (-not (Test-Path $TokenFile)) { throw "No API token at $TokenFile." }
    $token = (Get-Content $TokenFile -Raw).Trim()
    Invoke-RestMethod -Method Post -Uri "$Base/admin/shutdown" `
        -Headers @{ "X-API-Key" = $token } -ContentType "application/json" -Body "{}" | Out-Null
    $deadline = (Get-Date).AddSeconds(10)
    while ((Test-Health) -and (Get-Date) -lt $deadline) { Start-Sleep -Milliseconds 250 }
    if (Test-Health) { throw "The server did not stop within 10 seconds. It may be an older version without /admin/shutdown; see CHANGELOG." }
    Write-Host "Stopped the old server (BiDi session ended)."
} else {
    Write-Host "No server was running."
}

Start-Process -FilePath $Python -ArgumentList "`"$Server`"" -WindowStyle Hidden | Out-Null
$deadline = (Get-Date).AddSeconds(15)
while (-not (Test-Health) -and (Get-Date) -lt $deadline) { Start-Sleep -Milliseconds 250 }
if (-not (Test-Health)) { throw "The new server did not come up on $Base. Check ~\.claudecodebrowserx\logs\mcp_server.log." }
Write-Host "Server is up on $Base. Check browser_safety_status (bidi.connected)."
