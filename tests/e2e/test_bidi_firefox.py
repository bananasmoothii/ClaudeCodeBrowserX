#!/usr/bin/env python3
"""
End-to-end tests: bidi.py driving a real Firefox over WebDriver BiDi.

The unit tests in tests/test_bidi.py check what the module sends. These check
what Firefox then does with it, which is the whole point of the module: a page
that ignores untrusted events (Google Docs is the real one) must receive the
text, and accents, CJK and emoji must arrive as typed.

Firefox is started headless with a throwaway profile and its own debugging
port, so nothing here touches the person's browser. Skipped, with the reason,
when Firefox is not installed; CCB_E2E_REQUIRE=1 turns the skip into a
failure. CCB_FIREFOX names the binary.

Run: python3 -m unittest tests.e2e.test_bidi_firefox -v
"""

import asyncio
import base64
import http.server
import os
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import unittest.mock
from pathlib import Path

from tests import REPO_ROOT, TEST_HOME  # noqa: F401  (redirects HOME on import)

sys.path.insert(0, str(REPO_ROOT / 'mcp-server'))

import bidi  # noqa: E402


def find_firefox():
    candidates = [os.environ.get('CCB_FIREFOX'), shutil.which('firefox'),
                  r'C:\Program Files\Mozilla Firefox\firefox.exe',
                  r'C:\Program Files (x86)\Mozilla Firefox\firefox.exe',
                  '/Applications/Firefox.app/Contents/MacOS/firefox',
                  '/usr/bin/firefox', '/snap/bin/firefox']
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return candidate
    return None


FIREFOX = find_firefox()
REQUIRE = os.environ.get('CCB_E2E_REQUIRE') == '1'

PAGES = {
    # Every observation is written into the DOM: the probes run in a sandbox
    # that sees the page through Xray wrappers, so a variable the page's own
    # script sets is not visible to them, and nor should it be.
    '/form': """<!doctype html><title>form page</title>
<textarea id="t"></textarea>
<input id="pw" type="password">
<button id="go">go</button>
<pre id="log"></pre>
<script>
  const log = document.getElementById('log');
  for (const ev of ['keydown', 'beforeinput']) {
    document.addEventListener(ev, e => {
      if (!e.isTrusted) log.dataset.untrusted = '1';
      else log.dataset.trusted = (log.dataset.trusted || '') + ev[0];
    }, true);
  }
  document.getElementById('go').addEventListener('click', e => {
    log.dataset.click = e.isTrusted ? 'trusted' : 'untrusted';
  });
</script>""",
    # A page that behaves like Google Docs: it paints on a canvas, takes
    # focus on a click, and reads typed text from a hidden editable frame,
    # acting only on trusted events.
    '/docs': """<!doctype html><title>docs-like</title>
<div id="canvas" style="width:300px;height:120px;background:#ddd">canvas</div>
<iframe id="sink" style="position:absolute;left:-2000px"
        srcdoc="<body contenteditable></body>"></iframe>
<pre id="doc"></pre>
<script>
  const sink = document.getElementById('sink');
  const doc = document.getElementById('doc');
  document.getElementById('canvas').addEventListener('click', e => {
    if (e.isTrusted) sink.contentDocument.body.focus();
  });
  function attach() {
    sink.contentDocument.addEventListener('beforeinput', e => {
      if (e.isTrusted && e.data) { doc.textContent += e.data; }
      e.preventDefault();
    }, true);
  }
  if (sink.contentDocument.readyState === 'complete') attach();
  sink.addEventListener('load', attach);
</script>""",
    '/tall': """<!doctype html><title>tall page</title>
<div style="height:3000px;background:linear-gradient(red,blue)">tall</div>""",
    '/other': """<!doctype html><title>other page</title><textarea id="t"></textarea>""",
}


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = PAGES.get(self.path.split('?')[0])
        if body is None:
            self.send_response(404)
            self.end_headers()
            return
        data = body.encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


def free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


def png_size(data_url):
    raw = base64.b64decode(data_url.split(',', 1)[1])
    assert raw[:8] == b'\x89PNG\r\n\x1a\n', 'not a PNG'
    return struct.unpack('>II', raw[16:24])


@unittest.skipUnless(FIREFOX or REQUIRE,
                     'Firefox not found (set CCB_FIREFOX to its binary)')
class BiDiFirefoxTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        if not FIREFOX:
            raise AssertionError('Firefox not found and CCB_E2E_REQUIRE=1')
        cls.httpd = http.server.ThreadingHTTPServer(('127.0.0.1', 0), _Handler)
        cls.base = f'http://127.0.0.1:{cls.httpd.server_address[1]}'
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

        cls.profile = tempfile.mkdtemp(prefix='ccbx-ff-')
        cls.port = free_port()
        cls.firefox = subprocess.Popen(
            [FIREFOX, '--headless', '--no-remote', '--profile', cls.profile,
             f'--remote-debugging-port={cls.port}', 'about:blank'],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            try:
                socket.create_connection(('127.0.0.1', cls.port), 1).close()
                break
            except OSError:
                time.sleep(0.5)
        else:
            raise AssertionError('Firefox never opened its debugging port')
        cls.backend = bidi.BiDiBackend(port=cls.port, enabled=True)

        # Open one tab per page, through the backend's own session: Firefox
        # allows a single BiDi session, so the test cannot hold a second one.
        cls.tabs = {}
        for path in ('/form', '/docs', '/tall', '/other'):
            cls.tabs[path] = cls.open_tab(path)
        # The last tab opened is in front; the tests run against the first.

    @classmethod
    def tearDownClass(cls):
        try:
            cls.backend.close()
        finally:
            cls.firefox.terminate()
            try:
                cls.firefox.wait(timeout=15)
            except subprocess.TimeoutExpired:
                cls.firefox.kill()
            cls.httpd.shutdown()
            shutil.rmtree(cls.profile, ignore_errors=True)

    # -- helpers

    @classmethod
    def run_async(cls, coro_fn):
        cls.backend._ensure_loop()
        return asyncio.run_coroutine_threadsafe(
            coro_fn(), cls.backend._loop).result(60)

    @classmethod
    def open_tab(cls, path):
        url = cls.base + path

        async def go():
            async with cls.backend._lock:
                client = cls.backend._client
                if not client.connected:
                    await client.connect()
                made = await client.call('browsingContext.create',
                                         {'type': 'tab'})
                ctx = made['result']['context']
                await client.call('browsingContext.navigate',
                                  {'context': ctx, 'url': url,
                                   'wait': 'complete'})
                return ctx
        cls.run_async(go)
        title = {'/form': 'form page', '/docs': 'docs-like',
                 '/tall': 'tall page', '/other': 'other page'}[path]
        return {'id': len(cls.tabs) + 1, 'url': url, 'title': title,
                'active': False, 'incognito': False}

    def act(self, action, path, **arguments):
        return self.backend.execute(action, self.tabs[path], arguments)

    def read(self, path, expression):
        """A DOM value from the page, as the sandbox sees it."""
        async def go():
            async with self.backend._lock:
                client = self.backend._client
                ctx, url = await self.backend._context_for(client, self.tabs[path])
                return await bidi.BiDiPage(client, ctx, url)._call(
                    f'() => {expression}')
        return self.run_async(go)

    # -- typing

    def test_text_with_accents_cjk_and_emoji_arrives_as_typed(self):
        text = 'Bézard Grüßen 日本語 😀'
        result = self.act('type', '/form', selector='#t', text=text,
                          clear=True, allow_password=False)
        self.assertTrue(result.get('success'), result)
        self.assertEqual(self.read('/form', "document.getElementById('t').value"),
                         text)
        self.assertEqual(result.get('input'), 'trusted')

    def test_the_events_the_page_sees_are_trusted(self):
        self.act('type', '/form', selector='#t', text='ab', clear=True,
                 allow_password=False)
        self.assertIsNone(
            self.read('/form', "document.getElementById('log').dataset.untrusted")
            or None)
        self.assertTrue(
            self.read('/form', "document.getElementById('log').dataset.trusted"))

    def test_a_newline_is_the_enter_key(self):
        self.act('type', '/form', selector='#t', text='one\ntwo', clear=True,
                 allow_password=False)
        self.assertEqual(self.read('/form', "document.getElementById('t').value"),
                         'one\ntwo')

    def test_a_password_field_is_refused(self):
        result = self.act('type', '/form', selector='#pw', text='hunter2',
                          allow_password=False)
        self.assertFalse(result.get('success'), result)
        self.assertIn('password', result.get('error', '').lower())
        self.assertEqual(self.read('/form', "document.getElementById('pw').value"),
                         '')

    def test_a_page_that_ignores_untrusted_events_gets_the_text(self):
        """The Google Docs case: a trusted click focuses a hidden frame, and
        only trusted keystrokes are read from it."""
        clicked = self.act('click', '/docs', selector='#canvas')
        self.assertTrue(clicked.get('success'), clicked)
        typed = self.act('type', '/docs', text='Grüße aus Wien',
                         allow_password=False)
        self.assertTrue(typed.get('success'), typed)
        self.assertEqual(self.read('/docs', "document.getElementById('doc').textContent"),
                         'Grüße aus Wien')

    # -- keys and clicks

    def test_a_key_combination_acts_on_the_page(self):
        self.act('type', '/form', selector='#t', text='abc', clear=True,
                 allow_password=False)
        pressed = self.act('pressKey', '/form', key='a', ctrl=True,
                           allow_password=False)
        self.assertTrue(pressed.get('success'), pressed)
        self.act('type', '/form', text='X', allow_password=False)
        self.assertEqual(self.read('/form', "document.getElementById('t').value"),
                         'X')

    def test_a_click_reaches_the_page_as_trusted(self):
        result = self.act('click', '/form', selector='#go')
        self.assertTrue(result.get('success'), result)
        self.assertEqual(
            self.read('/form', "document.getElementById('log').dataset.click"),
            'trusted')

    def test_typing_reaches_a_tab_that_is_not_in_front(self):
        # /other was opened last, so /form is in the background.
        result = self.act('type', '/form', selector='#t', text='behind',
                          clear=True, allow_password=False)
        self.assertTrue(result.get('success'), result)
        self.assertEqual(self.read('/form', "document.getElementById('t').value"),
                         'behind')

    # -- screenshots

    def test_a_screenshot_is_a_png_of_the_tab(self):
        result = self.act('screenshot', '/form')
        self.assertTrue(result.get('success'), result)
        width, height = png_size(result['data'])
        self.assertGreater(width, 100)
        self.assertGreater(height, 100)

    def test_a_full_page_screenshot_is_taller_than_the_window(self):
        visible = png_size(self.act('screenshot', '/tall')['data'])
        full = self.act('screenshot', '/tall', full_page=True)
        self.assertTrue(full.get('fullPageCaptured'))
        self.assertGreater(png_size(full['data'])[1], visible[1])

    # -- the server's side of it

    def server_handler(self, path):
        import server
        handler = server.MCPHTTPHandler.__new__(server.MCPHTTPHandler)
        handler._bidi_tab = lambda tab_id: self.tabs[path]
        return server, handler

    def test_the_server_types_through_bidi_and_saves_screenshots(self):
        server, handler = self.server_handler('/form')
        with unittest.mock.patch.object(bidi, 'get_bidi_backend',
                                        lambda: self.backend):
            typed = handler._try_bidi(
                'type', None, {'selector': '#t', 'text': 'Grüße', 'clear': True,
                               'allow_password': False})
            self.assertTrue(typed.get('success'), typed)
            self.assertEqual(typed.get('via'), 'webdriver-bidi')
            self.assertEqual(
                self.read('/form', "document.getElementById('t').value"), 'Grüße')

            shot = handler._try_bidi('screenshot', None, {})
        self.assertTrue(shot.get('success'), shot)
        written = Path(shot['filepath'])
        self.assertTrue(written.is_file())
        self.assertEqual(written.read_bytes()[:8],
                         b'\x89PNG\r\n\x1a\n')

    def test_a_private_tab_is_not_photographed_by_the_server(self):
        server, handler = self.server_handler('/form')
        handler._bidi_tab = lambda tab_id: dict(self.tabs['/form'],
                                                incognito=True)
        with unittest.mock.patch.object(bidi, 'get_bidi_backend',
                                        lambda: self.backend):
            self.assertIsNone(handler._try_bidi('screenshot', None, {}))

    # -- falling back

    def test_a_tab_that_cannot_be_found_is_left_to_the_extension(self):
        ghost = {'id': 99, 'url': self.base + '/nowhere', 'title': 'x',
                 'active': False, 'incognito': False}
        with self.assertRaises(bidi.BiDiUnavailable):
            self.backend.execute('type', ghost, {'text': 'x'})


class UnavailableTests(unittest.TestCase):
    """No Firefox needed: nothing is listening."""

    def test_a_closed_port_is_unavailable_and_is_not_retried_at_once(self):
        backend = bidi.BiDiBackend(port=free_port(), enabled=True)
        self.addCleanup(backend.close)
        with self.assertRaises(bidi.BiDiUnavailable):
            backend.execute('type', {'url': 'http://x/'}, {'text': 'x'})
        self.assertFalse(backend.should_try())


if __name__ == '__main__':
    unittest.main()
