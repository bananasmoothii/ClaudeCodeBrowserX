---
name: claudecodebrowserx
description: How to drive the user's real Firefox with the ClaudeCodeBrowserX MCP tools (mcp__claudecodebrowserx__browser_*), including getting real keystrokes into Google Docs and other canvas editors, starting Firefox with the BiDi port, restarting the server safely, and the traps found so far. Read before the first browser_* call, when typing "succeeds" but nothing appears, or before touching the server.
---

# ClaudeCodeBrowserX (Firefox)

Tools named `mcp__claudecodebrowserx__browser_*` act in the user's **own Firefox**
(their tabs and sign-ins), through a Firefox extension plus a local MCP server.
Safety rules (credential guard, protected URLs, audit log) are in
`~/.claudecodebrowserx/safety.json`; `browser_safety_status` shows them.
Load the tools you need with one ToolSearch (`select:` several names); they are
deferred.

## 0. Firefox has to be running (start it yourself)

These tools need a running Firefox. The MCP server is separate and usually up;
it cannot open a browser. If Firefox is closed, every call fails.

- `browser_safety_status` reports `extension.firefox_running` and
  `extension.connected`. A call that fails with "Firefox is not running" or (older
  servers) "Command … timed out waiting for browser response after 30s" means the
  same thing: check `Get-Process firefox` / `pgrep -x firefox`.
- **Firefox not running: start it yourself**, without asking, by running
  `scripts/start-firefox.ps1` (Windows) or `start-firefox.sh` from the repo. It
  uses the normal profile and opens the BiDi port, so real input works too. Wait
  a few seconds, then call `browser_safety_status` until `extension.connected` is
  `true` (the extension connects once Firefox has loaded). Tabs may come back
  with new ids.
- Firefox running but `extension.connected` stays `false`: the extension is
  disabled or not installed (`about:addons`). Tell the user; do not restart
  their Firefox.
- Do not keep retrying a tool that says Firefox is not running.

## 1. Then read `browser_safety_status`

Look at `bidi`:

| `bidi.connected` | Meaning | What to do |
|---|---|---|
| `true` | `browser_type`, `browser_press_key`, plain `browser_click`, `browser_screenshot` send **real, trusted** input (results say `"via": "webdriver-bidi"`). | Proceed. |
| `false` + hint "start Firefox with --remote-debugging-port" | Firefox was started without the flag. Input is **synthetic**; Google Docs, Drive menus and anything that checks `isTrusted` ignore it, and results say `"input": "synthetic"`. | Section 2. |
| `false` + hint "already has a BiDi session open" | Firefox allows **one** BiDi session and something holds it (a second server, or a server that was killed without ending it). | Stop the other holder; if it is gone, Firefox must be restarted (section 2). |

`browsers_connected: 0` is normal in attended mode; judge by whether tools answer.

## 2. Starting Firefox with the debug port

`scripts/start-firefox.ps1` (Windows) / `start-firefox.sh` in the repo
(`C:\Users\bezar\Programmation\Web\ClaudeCodeBrowserX`) starts Firefox with
`--remote-debugging-port=9222` on the normal profile. It **refuses if Firefox is
already running**, because the flag is only read at startup.

- No Firefox process running: run the script yourself.
- Firefox running without the flag: **ask the user** to quit it (File > Exit), or
  to start it themselves. Never kill their Firefox. Tabs come back, but tab ids
  change after a restart: call `browser_get_tabs` again.
- Suggest once that they add `--remote-debugging-port=9222` to their Firefox
  shortcut so the flag is always on.
- The port is localhost only but lets any local program drive the browser; it is
  opt-in for that reason. `CLAUDE_BROWSERX_BIDI=0` turns BiDi off,
  `CLAUDE_BROWSERX_BIDI_PORT` changes the port.

## 3. Never hard-kill the server

A server stopped with `Stop-Process -Force`, `taskkill /F` or SIGKILL cannot end
Firefox's BiDi session, and real input stays locked out until Firefox restarts.
Replace the server with:

```powershell
.\scripts\restart-server.ps1      # POST /admin/shutdown, wait, start the new one
```

After editing the repo, copy the changed files into the install dir first, then
restart (the server caches `ax_page.js` and imports the rest at start):

```bash
cd <repo>; for f in server.py safety.py headless_backend.py stdio_wrapper.py ax.py ax_page.js bidi.py; do
  cp -f mcp-server/$f ~/.claudecodebrowserx/mcp-server/; done
cp -f extension/credentials.js ~/.claudecodebrowserx/mcp-server/
```

The running server is `~/.claudecodebrowserx/mcp-server/server.py`, **not** the
repo. A repo fix does nothing until it is copied and the server restarted. The
extension itself (`extension/`) is separate: changes need a repackaged,
re-signed extension.

## 4. Choosing the right tool

- **Read a page**: `browser_get_text` (fast), `browser_snapshot` (roles, names,
  `ref_N`), `browser_find` (search the tree). Google Docs draws text on a canvas:
  `get_text` is empty; open `.../mobilebasic` to read the text, or use
  `browser_screenshot`.
- **Refs** (`click_ref`, `type_ref`, `hover_ref`, `select_ref`) are issued by a
  snapshot/find and are only valid on that page. A ref whose element was removed,
  or whose name changed (Drive recycles menu nodes), is refused as `stale_ref`:
  find again. **Ref actions dispatch synthetic events**, even when BiDi is
  connected; they work on ordinary buttons and inputs but not on things that need
  trusted input (hover menus, canvas, iframes).
- **Clicking anything that needs trusted input** (Docs/Drive menus, cross-origin
  iframes such as the Docs/Drive *Move* picker): `browser_screenshot`, then
  `browser_click` with x/y. Coordinates are **CSS pixels**; if the screenshot is
  wider than `window.innerWidth` (this machine: 2560 vs 2048, ratio 1.25),
  divide the image coordinates by the ratio. A dialog may sit over the target:
  re-screenshot after every step.
- **Keys**: `browser_press_key` (`{"key":"a","ctrl":true}`).
- `browser_execute_script` runs in the page and is fine for inspection
  (e.g. listing `[role=gridcell][data-id]`); it is blocked on protected sites.

## 5. Typing into Google Docs / canvas editors

Needs `bidi.connected: true`.

1. Close any open dialog (Find and replace floats over the page).
2. `browser_click` inside the page body (not on a dialog).
3. `browser_press_key` Ctrl+A to replace everything, or click at the caret.
4. `browser_type` with `text` and **no selector**. Newlines become Enter,
   Unicode (é, ß) arrives intact.
5. Verify with a screenshot.

Without BiDi, the working fallback is **Find and replace** (Ctrl+H): its fields
are ordinary inputs. `type_ref` into "Rechercher"/"Remplacer par", click "Tout
remplacer", one paragraph per replacement. Slower, but needs no setup.

## 6. Google Drive / Docs facts

- Open `https://docs.google.com/document/d/<id>/copy` and press the button to
  copy a doc; the copy lands in **My Drive root**, not the original's folder.
  Move it: Docs toolbar folder icon (next to the star) > pick the folder >
  *Déplacer* (BiDi clicks), then verify in the folder listing.
- Rename: the title input is a real input; `type_ref` then blur or Enter.
- Drive's file grid loads about 50 items at a time and uses a roving tabindex.
  `browser_find` sees every loaded cell (fixed in 433291c; an older server saw
  only the first). To open a folder, double-click it or read its `data-id` with
  `browser_execute_script` and navigate to
  `https://drive.google.com/drive/folders/<id>`.
- A file the user deleted shows "Le fichier se trouve dans la corbeille". Do not
  restore it; make a new one.

## 7. Traps

- Success does not prove the page took synthetic input: check `input` / `via` in
  the result and `input_note`.
- Nothing typed is a credential: password/OTP/card fields are refused; do not
  work around it.
- The first tool call after a long gap may find the server stopped (the stdio
  wrapper only starts it once). `restart-server.ps1` fixes that.
- Windows: Python tests that rely on POSIX permissions, `pwd` or cp1252 reads
  fail for reasons unrelated to the code; read `CHANGELOG.md` for what is known.
