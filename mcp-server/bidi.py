#!/usr/bin/env python3
"""
Trusted input and screenshots for attended Firefox, over WebDriver BiDi.

MIT License
Copyright (c) 2025 Andre Watson (nanogenomic), Ligandal Inc.
Author: dre@ligandal.com

Why this exists. The extension can only dispatch synthetic DOM events, and a
page is free to ignore those: Google Docs, for one, draws on a canvas and reads
its text from a hidden iframe that only trusted keystrokes reach, so
browser_type "succeeded" without a letter appearing. Chrome extensions get
trusted input from chrome.debugger; Firefox extensions have nothing like it.
Firefox itself, though, speaks WebDriver BiDi when started with
--remote-debugging-port, and input.performActions delivers real key and
pointer events (isTrusted, any Unicode text, no focus fight with the user's
window). browsingContext.captureScreenshot also renders a tab that is in the
background or whose window is not in front, which captureVisibleTab cannot.

How it fits. The extension stays the source of truth for tabs, so its tab ids
and every guard in safety.py are unchanged. For type, pressKey, click and
screenshot the server first asks this module to do the job and falls back to
the extension when BiDi is not available (Firefox was not started with the
flag, another client holds the one BiDi session, the tab cannot be matched).
Typing, key presses and clicks run through HeadlessBrowser._run, so the
credential probes and the key allowlist are the same code, not a copy; this
module only supplies a page object with the Playwright methods those paths
call.

Setup: start Firefox with --remote-debugging-port=9222 (scripts/start-firefox
does it). CLAUDE_BROWSERX_BIDI=0 turns this off and
CLAUDE_BROWSERX_BIDI_PORT picks another port. The port is bound to localhost
by Firefox; anything on this machine that can reach it can drive the browser,
so it is opt-in by the flag and never opened by this project.
"""

import asyncio
import base64
import json
import logging
import sys
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

try:
    import websockets
except ImportError:  # server.py already warns about this
    websockets = None

from safety import env

logger = logging.getLogger('ClaudeCodeBrowserX.BiDi')

BIDI_HOST = '127.0.0.1'
DEFAULT_PORT = 9222

# After a failed connection, how long before trying again. Every call would
# otherwise pay the connect attempt, and a Firefox without the flag refuses
# at once but a wedged one does not.
RETRY_AFTER_SECONDS = 15.0
CONNECT_TIMEOUT = 5.0
CALL_TIMEOUT = 30.0

# Where probes run. A sandbox sees the page through Xray wrappers, so a page
# that overrides Element.prototype.getAttribute or defines its own
# `isPasswordField` cannot lie to the credential guard, and the sandbox is
# not subject to the page's Content-Security-Policy.
SANDBOX = 'ccbx'

# Typed in batches so one call stays a reasonable size; Firefox handles a
# batch as it arrives, so this is not a limit on the text.
KEYS_PER_BATCH = 400


class BiDiUnavailable(RuntimeError):
    """BiDi cannot be used for this call. The caller falls back to the
    extension; this is not an error the agent sees.

    backoff says whether BiDi as a whole looks down (no connection), so calls
    should not try it again for a while, or only this call missed (the tab
    could not be matched), which says nothing about the next one."""

    def __init__(self, message: str, backoff: bool = False):
        super().__init__(message)
        self.backoff = backoff


class BiDiError(RuntimeError):
    """The browser answered a command with an error."""

    def __init__(self, code: str, message: str):
        super().__init__(f'{code}: {message}' if message else code)
        self.code = code


# ---------------------------------------------------------------- keys

# WebDriver's private-use code points for keys that are not a character.
_SPECIAL_KEYS = {
    'backspace': '\ue003', 'tab': '\ue004', 'enter': '\ue007',
    'return': '\ue007', 'numpadenter': '\ue007',
    'shift': '\ue008', 'control': '\ue009', 'alt': '\ue00a',
    'pause': '\ue00b', 'escape': '\ue00c', 'esc': '\ue00c',
    'pageup': '\ue00e', 'pagedown': '\ue00f', 'end': '\ue010',
    'home': '\ue011', 'arrowleft': '\ue012', 'arrowup': '\ue013',
    'arrowright': '\ue014', 'arrowdown': '\ue015', 'insert': '\ue016',
    'delete': '\ue017', 'meta': '\ue03d', 'help': '\ue002', 'clear': '\ue005',
    'shiftleft': '\ue008', 'shiftright': '\ue050',
    'controlleft': '\ue009', 'controlright': '\ue051',
    'altleft': '\ue00a', 'altright': '\ue052',
    'metaleft': '\ue03d', 'metaright': '\ue053',
}
for _n in range(1, 13):
    _SPECIAL_KEYS[f'f{_n}'] = chr(0xE031 + _n - 1)

# Playwright's code names for printable keys, which is how pressKey callers
# name them ('KeyA', 'Digit1', 'Minus').
_CODE_NAMES = {
    'space': ' ', 'minus': '-', 'equal': '=', 'bracketleft': '[',
    'bracketright': ']', 'backslash': '\\', 'semicolon': ';', 'quote': "'",
    'backquote': '`', 'comma': ',', 'period': '.', 'slash': '/',
}

_MODIFIERS = ('control', 'shift', 'alt', 'meta')


def key_value(name: str) -> str:
    """The WebDriver key value for a Playwright-style key name."""
    if name == '':
        raise ValueError('empty key name')
    lowered = name.lower()
    if lowered == 'controlormeta':
        lowered = 'meta' if sys.platform == 'darwin' else 'control'
    if lowered in _SPECIAL_KEYS:
        return _SPECIAL_KEYS[lowered]
    if lowered in _CODE_NAMES:
        return _CODE_NAMES[lowered]
    if lowered.startswith('key') and len(name) == 4:
        return name[3].lower()
    if lowered.startswith('digit') and len(name) == 6:
        return name[5]
    if lowered.startswith('numpad') and len(name) == 7 and name[6].isdigit():
        return name[6]
    if len(name) == 1 or len(list(name)) == 1:
        return name
    raise ValueError(f'Unknown key "{name}"')


def split_combination(combination: str) -> Tuple[List[str], str]:
    """'Control+Shift+a' -> (['Control', 'Shift'], 'a'); 'Control++' ends in a
    literal plus."""
    if combination == '+':
        return [], '+'
    if combination.endswith('++'):
        head = combination[:-2]
        return [m for m in head.split('+') if m], '+'
    parts = combination.split('+')
    return [m for m in parts[:-1] if m], parts[-1]


def press_actions(combination: str) -> List[Dict[str, Any]]:
    """Key actions for one press of a key or a modifier combination."""
    modifiers, key = split_combination(combination)
    values = [key_value(m) for m in modifiers]
    target = key_value(key)
    # Shift+a is "A" to a page, and WebDriver sends the character it is given.
    if (any(m.lower() == 'shift' for m in modifiers)
            and len(target) == 1 and target.isalpha()):
        target = target.upper()
    actions: List[Dict[str, Any]] = []
    for value in values:
        actions.append({'type': 'keyDown', 'value': value})
    actions.append({'type': 'keyDown', 'value': target})
    actions.append({'type': 'keyUp', 'value': target})
    for value in reversed(values):
        actions.append({'type': 'keyUp', 'value': value})
    return actions


def type_actions(text: str, delay_ms: float = 0) -> List[Dict[str, Any]]:
    """Key actions that type text one code point at a time.

    A newline is the Enter key and a tab is Tab, as they are for a person
    typing. delay_ms pauses between characters.
    """
    actions: List[Dict[str, Any]] = []
    for char in text:
        if char == '\r':
            continue
        value = {'\n': '\ue007', '\t': '\ue004'}.get(char, char)
        actions.append({'type': 'keyDown', 'value': value})
        actions.append({'type': 'keyUp', 'value': value})
        if delay_ms and delay_ms > 0:
            actions.append({'type': 'pause', 'duration': int(delay_ms)})
    return actions


# ---------------------------------------------------------- values

def deserialize(value: Any) -> Any:
    """A BiDi RemoteValue as the Python value the page script returned."""
    if not isinstance(value, dict):
        return value
    kind = value.get('type')
    inner = value.get('value')
    if kind in ('undefined', 'null'):
        return None
    if kind in ('string', 'boolean'):
        return inner
    if kind == 'number':
        if isinstance(inner, str):
            return {'NaN': float('nan'), 'Infinity': float('inf'),
                    '-Infinity': float('-inf'), '-0': -0.0}.get(inner, inner)
        return inner
    if kind in ('array', 'set'):
        return [deserialize(v) for v in (inner or [])]
    if kind in ('object', 'map'):
        out = {}
        for pair in inner or []:
            key, item = pair
            out[key if isinstance(key, str) else deserialize(key)] = \
                deserialize(item)
        return out
    if kind == 'node':
        return {'node': value.get('sharedId')}
    return inner


# ---------------------------------------------------------- client

class BiDiClient:
    """One WebDriver BiDi session over a websocket.

    Commands are sent one at a time: the callers hold a lock anyway, and it
    keeps the reply matching trivial. Events are not subscribed to, but any
    that arrive are skipped.
    """

    def __init__(self, url: str):
        self.url = url
        self._ws = None
        self._next_id = 0
        self._lock = asyncio.Lock()
        self.capabilities: Dict[str, Any] = {}
        # True between a successful session.new and the matching session.end.
        self._session_open = False

    @property
    def connected(self) -> bool:
        ws = self._ws
        if ws is None:
            return False
        # websockets >= 14 has .state, older versions .open.
        state = getattr(ws, 'state', None)
        if state is not None:
            return getattr(state, 'name', str(state)) == 'OPEN'
        return bool(getattr(ws, 'open', False))

    async def connect(self) -> None:
        if websockets is None:
            raise BiDiUnavailable('the websockets package is not installed')
        await self.close()
        try:
            self._ws = await asyncio.wait_for(
                websockets.connect(self.url, max_size=None,
                                   ping_interval=None),
                CONNECT_TIMEOUT)
        except Exception as e:
            self._ws = None
            raise BiDiUnavailable(
                f'nothing is listening for WebDriver BiDi at {self.url} '
                f'({e}). Start Firefox with --remote-debugging-port.',
                backoff=True) from e
        try:
            reply = await self._send('session.new', {'capabilities': {}})
        except BiDiError as e:
            await self.close()
            # The one session is held by another client, usually a second
            # Claude session. Not an error for this call.
            raise BiDiUnavailable(
                f'Firefox would not open a BiDi session ({e}); another '
                'client may be holding it', backoff=True) from e
        except Exception:
            await self.close()
            raise
        self._session_open = True
        self.capabilities = (reply.get('result') or {}).get('capabilities', {})
        if self.capabilities.get('browserName') != 'firefox':
            await self.close()
            raise BiDiUnavailable(
                f'{self.url} is {self.capabilities.get("browserName")!r}, '
                'not Firefox', backoff=True)
        logger.info('WebDriver BiDi connected to Firefox %s',
                    self.capabilities.get('browserVersion'))

    async def close(self) -> None:
        """End the session, then the socket.

        Firefox allows one BiDi session. Closing only the socket left it
        counted as open after a server that was stopped abruptly, and every
        later session.new failed with "Maximum number of active sessions"
        until Firefox itself was restarted. session.end releases it at once;
        a process that is killed outright cannot call this, which is why
        server.py also ends the session from its exit and signal handlers.
        """
        ws, self._ws = self._ws, None
        was_open, self._session_open = self._session_open, False
        if ws is None:
            return
        if was_open:
            try:
                await asyncio.wait_for(ws.send(json.dumps(
                    {'id': self._next_id + 1, 'method': 'session.end',
                     'params': {}})), 2)
                await asyncio.wait_for(ws.recv(), 2)
            except Exception:
                pass
        try:
            await ws.close()
        except Exception:
            pass

    async def _send(self, method: str, params: Dict[str, Any],
                    timeout: float = CALL_TIMEOUT) -> Dict[str, Any]:
        async with self._lock:
            self._next_id += 1
            ident = self._next_id
            await self._ws.send(json.dumps(
                {'id': ident, 'method': method, 'params': params}))
            deadline = time.monotonic() + timeout
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise asyncio.TimeoutError(f'{method} timed out')
                raw = await asyncio.wait_for(self._ws.recv(), remaining)
                message = json.loads(raw)
                if message.get('id') != ident:
                    continue
                if message.get('type') == 'error':
                    raise BiDiError(message.get('error', 'error'),
                                    message.get('message', ''))
                return message

    async def call(self, method: str, params: Optional[Dict[str, Any]] = None,
                   timeout: float = CALL_TIMEOUT) -> Dict[str, Any]:
        if not self.connected:
            await self.connect()
        try:
            return await self._send(method, params or {}, timeout)
        except (BiDiError, asyncio.TimeoutError):
            raise
        except Exception as e:
            # The socket died mid-call; the next call reconnects.
            self._session_open = False
            await self.close()
            raise BiDiUnavailable(f'lost the BiDi connection: {e}',
                                  backoff=True) from e


# ------------------------------------------------------------ page

class _Keyboard:
    def __init__(self, page: 'BiDiPage'):
        self._page = page

    async def type(self, text: str, delay: float = 0) -> None:
        actions = type_actions(text, delay)
        # A pair of actions per character, plus a pause when delayed.
        step = KEYS_PER_BATCH * (3 if delay else 2)
        for start in range(0, len(actions), step):
            await self._page.perform('key', actions[start:start + step])

    async def press(self, combination: str) -> None:
        try:
            actions = press_actions(combination)
        except ValueError as e:
            raise RuntimeError(str(e)) from e
        await self._page.perform('key', actions)


class _Mouse:
    def __init__(self, page: 'BiDiPage'):
        self._page = page

    async def click(self, x: float, y: float) -> None:
        await self._page.perform('pointer', [
            {'type': 'pointerMove', 'x': int(round(x)), 'y': int(round(y)),
             'origin': 'viewport'},
            {'type': 'pointerDown', 'button': 0},
            {'type': 'pointerUp', 'button': 0},
        ], parameters={'pointerType': 'mouse'})


class BiDiPage:
    """The few Playwright Page methods HeadlessBrowser._run uses for type,
    pressKey and click, over one Firefox browsing context."""

    def __init__(self, client: BiDiClient, context: str, url: str = ''):
        self.client = client
        self.context = context
        self.url = url
        self.keyboard = _Keyboard(self)
        self.mouse = _Mouse(self)

    # Typing with no selector goes in at the caret, replacing a selection,
    # because the keys are real ones. See HeadlessBrowser._prepare_typing_js.
    keeps_selection = True

    async def perform(self, source: str, actions: List[Dict[str, Any]],
                      parameters: Optional[Dict[str, Any]] = None) -> None:
        entry: Dict[str, Any] = {'type': source, 'id': source, 'actions': actions}
        if parameters:
            entry['parameters'] = parameters
        try:
            await self.client.call('input.performActions', {
                'context': self.context, 'actions': [entry]})
        finally:
            # Leave no key or button held if an action failed half way.
            try:
                await self.client.call('input.releaseActions',
                                       {'context': self.context})
            except Exception:
                pass

    async def _call(self, declaration: str,
                    arguments: Optional[List[Dict[str, Any]]] = None) -> Any:
        reply = await self.client.call('script.callFunction', {
            'functionDeclaration': declaration,
            'awaitPromise': True,
            'target': {'context': self.context, 'sandbox': SANDBOX},
            'arguments': arguments or [],
            'resultOwnership': 'none',
            'serializationOptions': {'maxObjectDepth': 8, 'maxDomDepth': 0},
        })
        result = reply.get('result', {})
        if result.get('type') == 'exception':
            details = result.get('exceptionDetails', {})
            raise RuntimeError(details.get('text') or 'script exception')
        return deserialize(result.get('result'))

    async def evaluate(self, function_source: str) -> Any:
        return await self._call(function_source)

    async def eval_on_selector(self, selector: str, function_source: str) -> Any:
        declaration = (
            'function(selector) {\n'
            '  const el = document.querySelector(selector);\n'
            '  if (!el) throw new Error("failed to find element matching '
            'selector " + selector);\n'
            f'  return ({function_source})(el);\n'
            '}')
        return await self._call(declaration, [
            {'type': 'string', 'value': selector}])

    async def focus(self, selector: str) -> None:
        await self.eval_on_selector(selector, 'el => { el.focus(); }')

    async def click(self, selector: str, timeout: float = 10000) -> None:
        box = await self.eval_on_selector(selector, (
            'el => { el.scrollIntoView({block: "center", inline: "center"});'
            ' const r = el.getBoundingClientRect();'
            ' return {x: r.left + r.width / 2, y: r.top + r.height / 2,'
            ' w: r.width, h: r.height}; }'))
        if not box or not box.get('w') or not box.get('h'):
            raise RuntimeError(
                f'Element {selector} has no size, so there is nowhere to click')
        await self.mouse.click(box['x'], box['y'])

    async def title(self) -> str:
        return await self._call('() => document.title') or ''

    async def visible(self) -> bool:
        return await self._call(
            '() => document.visibilityState') == 'visible'

    async def screenshot(self, full_page: bool = False,
                         image_type: str = 'image/png',
                         quality: Optional[float] = None) -> bytes:
        fmt: Dict[str, Any] = {'type': image_type}
        if quality is not None and image_type == 'image/jpeg':
            fmt['quality'] = quality
        reply = await self.client.call('browsingContext.captureScreenshot', {
            'context': self.context,
            'origin': 'document' if full_page else 'viewport',
            'format': fmt,
        })
        return base64.b64decode(reply['result']['data'])


# --------------------------------------------------------- backend

def _same_url(a: str, b: str) -> bool:
    return (a or '').split('#')[0] == (b or '').split('#')[0]


class BiDiBackend:
    """Runs trusted actions against the Firefox the extension is in.

    execute() is called from the server's worker threads and blocks; the
    coroutines run on a private event loop in a daemon thread, so this works
    whether or not the server has a loop of its own.
    """

    # Actions this backend takes. Everything else stays with the extension.
    ACTIONS = ('type', 'pressKey', 'click', 'screenshot')

    def __init__(self, port: Optional[int] = None, enabled: Optional[bool] = None):
        if enabled is None:
            enabled = env('BIDI', '1').strip().lower() not in (
                '0', 'false', 'no', 'off') and websockets is not None
        if port is None:
            try:
                port = int(env('BIDI_PORT', str(DEFAULT_PORT)))
            except ValueError:
                port = DEFAULT_PORT
        self.enabled = enabled
        self.url = f'ws://{BIDI_HOST}:{port}/session'
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._start_lock = threading.Lock()
        self._client: Optional[BiDiClient] = None
        self._lock: Optional[asyncio.Lock] = None
        self._retry_at = 0.0
        self._headless = None
        # Why the last call was left to the extension, for fallback_note().
        self.last_reason: Optional[str] = None

    # -- threading

    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        with self._start_lock:
            if self._loop is None:
                loop = asyncio.new_event_loop()
                ready = threading.Event()

                def run():
                    asyncio.set_event_loop(loop)
                    ready.set()
                    loop.run_forever()

                self._thread = threading.Thread(
                    target=run, name='bidi-loop', daemon=True)
                self._thread.start()
                ready.wait()
                self._loop = loop

                async def init():
                    self._lock = asyncio.Lock()
                    self._client = BiDiClient(self.url)

                asyncio.run_coroutine_threadsafe(init(), loop).result()
            return self._loop

    def status(self) -> Dict[str, Any]:
        """What browser_safety_status tells the agent about this backend."""
        client = self._client
        connected = bool(client and client.connected)
        info: Dict[str, Any] = {
            'enabled': self.enabled,
            'connected': connected,
            'endpoint': self.url,
            'actions': list(self.ACTIONS),
        }
        if connected:
            info['firefox'] = client.capabilities.get('browserVersion')
        elif self.enabled:
            wait = self._retry_at - time.monotonic()
            port = self.url.rsplit(':', 1)[-1].split('/')[0]
            if self.last_reason:
                info['last_error'] = self.last_reason
            if self.last_reason and 'Maximum number of active sessions' in self.last_reason:
                # The port answers, so Firefox is already started with the
                # flag: telling the agent to start it again was wrong.
                info['hint'] = (
                    'Firefox is listening on port %s but already has a BiDi '
                    'session open: another client holds it, or a previous '
                    'server was killed without ending it. Stop the other '
                    'client, or restart Firefox with scripts/start-firefox '
                    '(Firefox frees the session on restart). Until then these '
                    'actions use the extension.' % port)
            else:
                info['hint'] = (
                    'Start Firefox with --remote-debugging-port=%s (see '
                    'scripts/start-firefox) for real key and mouse events and '
                    'screenshots of background tabs; until then these actions '
                    'use the extension.' % port)
            if wait > 0:
                info['retry_in_seconds'] = round(wait)
        return info

    def should_try(self) -> bool:
        return self.enabled and time.monotonic() >= self._retry_at

    def wants(self, action: str, arguments: Dict[str, Any]) -> bool:
        """Whether BiDi can do this call as well as the extension can.

        A click is taken only in its plain forms, a selector or a point: the
        extension also finds elements by XPath or text and can double-click
        or right-click, and sending those here would quietly do less.
        """
        if action in ('type', 'pressKey', 'screenshot'):
            return True
        if action != 'click':
            return False
        from headless_backend import parse_flag
        if arguments.get('xpath') or arguments.get('text'):
            return False
        if parse_flag(arguments.get('double_click'), False) or \
                parse_flag(arguments.get('right_click'), False):
            return False
        return bool(arguments.get('selector')) or (
            arguments.get('x') is not None and arguments.get('y') is not None)

    def available(self) -> bool:
        """Whether a BiDi session is open (opening one if need be).

        Cheap when BiDi is down: a failed attempt is remembered for
        RETRY_AFTER_SECONDS, so a Firefox started without the flag costs one
        refused connection every fifteen seconds, not one per call. Asked
        before anything is looked up in the extension, for the same reason.
        """
        if not self.should_try():
            return False
        loop = self._ensure_loop()

        async def connect():
            async with self._lock:
                if not self._client.connected:
                    await self._client.connect()

        try:
            asyncio.run_coroutine_threadsafe(connect(), loop).result(
                timeout=CONNECT_TIMEOUT + 5)
            return True
        except BiDiUnavailable as e:
            logger.info('WebDriver BiDi not available: %s', e)
            self.last_reason = str(e)
        except Exception as e:
            logger.info('WebDriver BiDi connection failed: %s', e)
            self.last_reason = str(e)
        self._retry_at = time.monotonic() + RETRY_AFTER_SECONDS
        return False

    def fallback_note(self) -> str:
        """What the agent is told when typing, a key press or a click went to
        the extension instead.

        Without this, a page that ignores synthetic events (Google Docs) made
        the call report success while nothing happened, and nothing said why
        or what to do about it.
        """
        note = ('Sent as synthetic DOM events, which some pages ignore '
                '(Google Docs and other canvas editors), so success here does '
                'not prove the page received it. ')
        client = self._client
        if client is not None and client.connected:
            return note + ('WebDriver BiDi is connected but was not used for '
                           'this call (%s).' % (self.last_reason or 'unknown reason'))
        if self.last_reason:
            return note + ('WebDriver BiDi is not connected (%s). Check '
                           'browser_safety_status (bidi.hint).'
                           % self.last_reason)
        return note + ('For real keystrokes, start Firefox with '
                       'scripts/start-firefox and check browser_safety_status '
                       '(bidi.connected).')

    def execute(self, action: str, tab: Optional[Dict[str, Any]],
                arguments: Dict[str, Any], timeout: float = CALL_TIMEOUT
                ) -> Dict[str, Any]:
        """Do one action. Raises BiDiUnavailable when the caller should use
        the extension instead."""
        if not self.should_try():
            raise BiDiUnavailable('WebDriver BiDi is off or was unavailable '
                                  'a moment ago')
        loop = self._ensure_loop()
        future = asyncio.run_coroutine_threadsafe(
            self._execute(action, tab, arguments), loop)
        try:
            return future.result(timeout=timeout)
        except BiDiUnavailable as e:
            self.last_reason = str(e)
            if e.backoff:
                self._retry_at = time.monotonic() + RETRY_AFTER_SECONDS
            raise
        except TimeoutError:
            future.cancel()
            raise BiDiUnavailable(f'{action} over BiDi timed out')
        except Exception as e:
            if type(e).__name__ == 'TimeoutError':
                future.cancel()
                raise BiDiUnavailable(f'{action} over BiDi timed out')
            raise

    # -- the work

    async def _execute(self, action: str, tab: Optional[Dict[str, Any]],
                       arguments: Dict[str, Any]) -> Dict[str, Any]:
        async with self._lock:
            client = self._client
            if not client.connected:
                await client.connect()
            context, url = await self._context_for(client, tab)
            page = BiDiPage(client, context, url)
            try:
                if action == 'screenshot':
                    return await self._screenshot(page, tab, arguments)
                # Keys and clicks reach only the visible tab of a window.
                if not await page.visible():
                    await client.call('browsingContext.activate',
                                      {'context': context})
                if self._headless is None:
                    from headless_backend import HeadlessBrowser
                    self._headless = HeadlessBrowser()
                try:
                    result = await self._headless._run(page, action, arguments)
                except (BiDiUnavailable, asyncio.CancelledError):
                    raise
                except Exception as e:
                    # A refusal from a guard, or the page not taking the
                    # input. The agent should see it; the extension would
                    # not do better.
                    return {'success': False, 'error': str(e)}
                if isinstance(result, dict):
                    result.setdefault('input', 'trusted')
                    result.setdefault('via', 'webdriver-bidi')
                return result
            except BiDiError as e:
                if e.code in ('no such frame', 'no such node'):
                    raise BiDiUnavailable(str(e)) from e
                return {'success': False, 'error': f'Firefox refused: {e}'}

    async def _screenshot(self, page: BiDiPage, tab: Optional[Dict[str, Any]],
                          arguments: Dict[str, Any]) -> Dict[str, Any]:
        from headless_backend import parse_flag
        full_page = parse_flag(arguments.get('full_page'), False)
        fmt = str(arguments.get('format') or 'png').lower()
        jpeg = fmt in ('jpeg', 'jpg')
        quality = None
        if jpeg:
            try:
                quality = min(max(float(arguments.get('quality', 90)), 1), 100) / 100
            except (TypeError, ValueError):
                quality = 0.9
        image = await page.screenshot(
            full_page, 'image/jpeg' if jpeg else 'image/png', quality)
        mime = 'image/jpeg' if jpeg else 'image/png'
        result = {
            'success': True,
            'data': f'data:{mime};base64,' + base64.b64encode(image).decode('ascii'),
            'type': 'full_page' if full_page else 'visible',
            'tab': {'id': (tab or {}).get('id'), 'url': page.url,
                    'title': (tab or {}).get('title') or await page.title()},
            'privateWindow': False,
            'wasFocused': False,
            'via': 'webdriver-bidi',
        }
        if full_page:
            result['fullPageCaptured'] = True
        return result

    async def _context_for(self, client: BiDiClient,
                           tab: Optional[Dict[str, Any]]) -> Tuple[str, str]:
        """The browsing context for the extension's tab.

        The extension and BiDi number tabs differently, so the tab is found by
        what both can see: its URL, then its title, then which one is in
        front. When that does not pick exactly one, this refuses rather than
        type into the wrong tab.
        """
        reply = await client.call('browsingContext.getTree', {})
        tops = reply['result']['contexts']
        if not tab:
            if len(tops) == 1:
                return tops[0]['context'], tops[0].get('url', '')
            raise BiDiUnavailable(
                'cannot tell which of the open tabs is meant without the '
                'extension')
        candidates = [c for c in tops if _same_url(c.get('url', ''), tab.get('url', ''))]
        if len(candidates) > 1 and tab.get('title'):
            titled = []
            for c in candidates:
                try:
                    probe = BiDiPage(client, c['context'])
                    if await probe.title() == tab['title']:
                        titled.append(c)
                except Exception:
                    continue
            if titled:
                candidates = titled
        if len(candidates) > 1 and tab.get('active'):
            visible = []
            for c in candidates:
                try:
                    if await BiDiPage(client, c['context']).visible():
                        visible.append(c)
                except Exception:
                    continue
            if visible:
                candidates = visible
        if len(candidates) != 1:
            raise BiDiUnavailable(
                f'{len(candidates)} Firefox tabs match {tab.get("url")!r}')
        return candidates[0]['context'], candidates[0].get('url', '')

    def close(self) -> None:
        loop = self._loop
        if loop is not None and self._client is not None:
            try:
                asyncio.run_coroutine_threadsafe(
                    self._client.close(), loop).result(timeout=3)
            except Exception:
                pass


_backend: Optional[BiDiBackend] = None
_backend_lock = threading.Lock()


def get_bidi_backend() -> BiDiBackend:
    global _backend
    with _backend_lock:
        if _backend is None:
            _backend = BiDiBackend()
        return _backend
