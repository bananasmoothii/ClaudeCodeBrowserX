#!/usr/bin/env python3
"""
bidi.py tests that need no Firefox: key mapping, value decoding, the protocol
client against a scripted server, tab matching, and how the server decides
whether to use BiDi at all. tests/e2e/test_bidi_firefox.py runs the module
against a real browser.

Run: python3 -m unittest discover -s tests -t . -v
"""

import asyncio
import json
import sys
import threading
import unittest
import unittest.mock
from pathlib import Path

from tests import REPO_ROOT, TEST_HOME  # noqa: F401  (redirects HOME on import)

sys.path.insert(0, str(REPO_ROOT / 'mcp-server'))

import bidi  # noqa: E402
import server  # noqa: E402

try:
    import websockets
except ImportError:  # pragma: no cover
    websockets = None


class KeyMappingTests(unittest.TestCase):

    def test_named_keys_become_webdriver_code_points(self):
        self.assertEqual(bidi.key_value('Enter'), '\ue007')
        self.assertEqual(bidi.key_value('Tab'), '\ue004')
        self.assertEqual(bidi.key_value('Escape'), '\ue00c')
        self.assertEqual(bidi.key_value('ArrowDown'), '\ue015')
        self.assertEqual(bidi.key_value('Backspace'), '\ue003')
        self.assertEqual(bidi.key_value('F1'), '\ue031')
        self.assertEqual(bidi.key_value('F12'), '\ue03c')

    def test_playwright_code_names_become_the_character(self):
        self.assertEqual(bidi.key_value('KeyA'), 'a')
        self.assertEqual(bidi.key_value('Digit7'), '7')
        self.assertEqual(bidi.key_value('Space'), ' ')
        self.assertEqual(bidi.key_value('Minus'), '-')

    def test_a_character_is_itself(self):
        for char in ('a', 'é', '日', '😀', '+'):
            self.assertEqual(bidi.key_value(char), char)

    def test_an_unknown_name_is_refused_not_typed_as_letters(self):
        with self.assertRaises(ValueError):
            bidi.key_value('Frobnicate')
        with self.assertRaises(ValueError):
            bidi.key_value('')

    def test_control_or_meta_follows_the_platform(self):
        with unittest.mock.patch.object(bidi.sys, 'platform', 'darwin'):
            self.assertEqual(bidi.key_value('ControlOrMeta'), '\ue03d')
        with unittest.mock.patch.object(bidi.sys, 'platform', 'win32'):
            self.assertEqual(bidi.key_value('ControlOrMeta'), '\ue009')

    def test_combinations_split_into_modifiers_and_key(self):
        self.assertEqual(bidi.split_combination('Enter'), ([], 'Enter'))
        self.assertEqual(bidi.split_combination('Control+Shift+a'),
                         (['Control', 'Shift'], 'a'))
        self.assertEqual(bidi.split_combination('Control++'),
                         (['Control'], '+'))
        self.assertEqual(bidi.split_combination('+'), ([], '+'))

    def test_a_combination_holds_the_modifiers_around_the_key(self):
        actions = bidi.press_actions('Control+a')
        self.assertEqual([(a['type'], a['value']) for a in actions], [
            ('keyDown', '\ue009'), ('keyDown', 'a'),
            ('keyUp', 'a'), ('keyUp', '\ue009')])

    def test_shift_and_a_letter_is_the_capital(self):
        actions = bidi.press_actions('Shift+a')
        self.assertEqual(actions[1]['value'], 'A')

    def test_text_is_typed_one_code_point_at_a_time(self):
        actions = bidi.type_actions('é😀')
        self.assertEqual([a['value'] for a in actions], ['é', 'é', '😀', '😀'])
        self.assertEqual([a['type'] for a in actions],
                         ['keyDown', 'keyUp', 'keyDown', 'keyUp'])

    def test_a_newline_is_enter_and_a_tab_is_tab(self):
        values = [a['value'] for a in bidi.type_actions('a\n\tb')
                  if a['type'] == 'keyDown']
        self.assertEqual(values, ['a', '\ue007', '\ue004', 'b'])

    def test_a_carriage_return_is_dropped_so_crlf_is_one_enter(self):
        values = [a['value'] for a in bidi.type_actions('a\r\nb')
                  if a['type'] == 'keyDown']
        self.assertEqual(values, ['a', '\ue007', 'b'])

    def test_a_delay_pauses_between_characters(self):
        actions = bidi.type_actions('ab', 30)
        self.assertEqual([a['type'] for a in actions],
                         ['keyDown', 'keyUp', 'pause'] * 2)
        self.assertEqual(actions[2]['duration'], 30)


class DeserializeTests(unittest.TestCase):

    def test_primitives(self):
        self.assertEqual(bidi.deserialize({'type': 'string', 'value': 'x'}), 'x')
        self.assertEqual(bidi.deserialize({'type': 'number', 'value': 3}), 3)
        self.assertIs(bidi.deserialize({'type': 'boolean', 'value': True}), True)
        self.assertIsNone(bidi.deserialize({'type': 'undefined'}))
        self.assertIsNone(bidi.deserialize({'type': 'null'}))

    def test_special_numbers(self):
        self.assertEqual(bidi.deserialize({'type': 'number', 'value': 'Infinity'}),
                         float('inf'))
        nan = bidi.deserialize({'type': 'number', 'value': 'NaN'})
        self.assertNotEqual(nan, nan)

    def test_objects_and_arrays_nest(self):
        value = {'type': 'object', 'value': [
            ['editable', {'type': 'boolean', 'value': True}],
            ['tag', {'type': 'string', 'value': 'input'}],
            ['list', {'type': 'array', 'value': [
                {'type': 'number', 'value': 1}, {'type': 'null'}]}],
        ]}
        self.assertEqual(bidi.deserialize(value),
                         {'editable': True, 'tag': 'input', 'list': [1, None]})


class ScriptedServer:
    """A WebSocket server that answers BiDi commands from a table, in a
    thread of its own."""

    def __init__(self, handler):
        self.handler = handler
        self.received = []
        self.loop = asyncio.new_event_loop()
        self.ready = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        asyncio.set_event_loop(self.loop)

        async def serve():
            async def connection(ws):
                async for raw in ws:
                    message = json.loads(raw)
                    self.received.append(message)
                    reply = self.handler(message)
                    if reply is None:
                        await ws.close()
                        return
                    reply.setdefault('id', message['id'])
                    await ws.send(json.dumps(reply))
            self.stop = asyncio.Event()
            self.server = await websockets.serve(connection, '127.0.0.1', 0)
            self.port = self.server.sockets[0].getsockname()[1]
            self.ready.set()
            await self.stop.wait()
            self.server.close()
            await self.server.wait_closed()

        self.loop.run_until_complete(serve())
        self.loop.close()

    def __enter__(self):
        self.thread.start()
        self.ready.wait(10)
        return self

    def __exit__(self, *exc):
        self.loop.call_soon_threadsafe(self.stop.set)
        self.thread.join(10)


def firefox_session(message):
    return {'type': 'success', 'result': {'sessionId': 's', 'capabilities': {
        'browserName': 'firefox', 'browserVersion': '160.0'}}}


@unittest.skipIf(websockets is None, 'websockets is not installed')
class ClientTests(unittest.IsolatedAsyncioTestCase):

    async def test_it_opens_a_session_and_sends_commands(self):
        def handler(message):
            if message['method'] == 'session.new':
                return firefox_session(message)
            return {'type': 'success', 'result': {'echo': message['params']}}

        with ScriptedServer(handler) as srv:
            client = bidi.BiDiClient(f'ws://127.0.0.1:{srv.port}/session')
            reply = await client.call('browsingContext.getTree', {'a': 1})
            self.assertEqual(reply['result'], {'echo': {'a': 1}})
            self.assertEqual([m['method'] for m in srv.received],
                             ['session.new', 'browsingContext.getTree'])
            await client.close()

    async def test_closing_ends_the_session_so_firefox_frees_its_one_slot(self):
        # Firefox allows one BiDi session. Closing only the socket left it
        # counted as open after a server was stopped, and the next server was
        # refused until Firefox restarted.
        def handler(message):
            if message['method'] == 'session.new':
                return firefox_session(message)
            return {'type': 'success', 'result': {}}

        with ScriptedServer(handler) as srv:
            client = bidi.BiDiClient(f'ws://127.0.0.1:{srv.port}/session')
            await client.connect()
            await client.close()
            self.assertEqual([m['method'] for m in srv.received],
                             ['session.new', 'session.end'])
            self.assertFalse(client.connected)

    async def test_a_session_that_never_opened_is_not_ended(self):
        def handler(message):
            return {'type': 'error', 'error': 'session not created',
                    'message': 'Maximum number of active sessions'}

        with ScriptedServer(handler) as srv:
            client = bidi.BiDiClient(f'ws://127.0.0.1:{srv.port}/session')
            with self.assertRaises(bidi.BiDiUnavailable):
                await client.connect()
            await client.close()
            self.assertEqual([m['method'] for m in srv.received],
                             ['session.new'])

    async def test_a_command_error_is_raised_with_its_code(self):
        def handler(message):
            if message['method'] == 'session.new':
                return firefox_session(message)
            return {'type': 'error', 'error': 'no such frame',
                    'message': 'gone'}

        with ScriptedServer(handler) as srv:
            client = bidi.BiDiClient(f'ws://127.0.0.1:{srv.port}/session')
            with self.assertRaises(bidi.BiDiError) as caught:
                await client.call('input.performActions', {})
            self.assertEqual(caught.exception.code, 'no such frame')
            await client.close()

    async def test_a_session_held_by_someone_else_is_unavailable_with_backoff(self):
        def handler(message):
            return {'type': 'error', 'error': 'session not created',
                    'message': 'Maximum number of active sessions'}

        with ScriptedServer(handler) as srv:
            client = bidi.BiDiClient(f'ws://127.0.0.1:{srv.port}/session')
            with self.assertRaises(bidi.BiDiUnavailable) as caught:
                await client.connect()
            self.assertTrue(caught.exception.backoff)

    async def test_something_that_is_not_firefox_is_not_used(self):
        def handler(message):
            return {'type': 'success', 'result': {'capabilities': {
                'browserName': 'chrome'}}}

        with ScriptedServer(handler) as srv:
            client = bidi.BiDiClient(f'ws://127.0.0.1:{srv.port}/session')
            with self.assertRaises(bidi.BiDiUnavailable):
                await client.connect()

    async def test_nothing_listening_is_unavailable_with_backoff(self):
        client = bidi.BiDiClient('ws://127.0.0.1:1/session')
        with self.assertRaises(bidi.BiDiUnavailable) as caught:
            await client.connect()
        self.assertTrue(caught.exception.backoff)

    async def test_a_connection_that_dies_mid_call_reconnects_next_time(self):
        calls = {'n': 0}

        def handler(message):
            if message['method'] == 'session.new':
                return firefox_session(message)
            calls['n'] += 1
            if calls['n'] == 1:
                return None  # drop the connection
            return {'type': 'success', 'result': {'ok': True}}

        with ScriptedServer(handler) as srv:
            client = bidi.BiDiClient(f'ws://127.0.0.1:{srv.port}/session')
            with self.assertRaises(bidi.BiDiUnavailable):
                await client.call('x.y', {})
            reply = await client.call('x.y', {})
            self.assertEqual(reply['result'], {'ok': True})
            await client.close()


class FakeClient:
    """Answers getTree and the title and visibility probes for tab matching."""

    def __init__(self, contexts, titles=None, visible=None):
        self.contexts = contexts
        self.titles = titles or {}
        self.visible = visible or set()

    async def call(self, method, params=None, timeout=0):
        params = params or {}
        if method == 'browsingContext.getTree':
            return {'result': {'contexts': self.contexts}}
        if method == 'script.callFunction':
            ctx = params['target']['context']
            declaration = params['functionDeclaration']
            if 'document.title' in declaration:
                value = self.titles.get(ctx, '')
            else:
                value = 'visible' if ctx in self.visible else 'hidden'
            return {'result': {'type': 'success',
                               'result': {'type': 'string', 'value': value}}}
        raise AssertionError(method)


class TabMatchingTests(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        self.backend = bidi.BiDiBackend(port=1, enabled=True)

    async def resolve(self, client, tab):
        return await self.backend._context_for(client, tab)

    async def test_one_tab_with_the_url_is_the_one(self):
        client = FakeClient([{'context': 'a', 'url': 'https://x/1'},
                             {'context': 'b', 'url': 'https://x/2'}])
        got = await self.resolve(client, {'url': 'https://x/2'})
        self.assertEqual(got[0], 'b')

    async def test_a_fragment_does_not_matter(self):
        client = FakeClient([{'context': 'a', 'url': 'https://x/#top'}])
        got = await self.resolve(client, {'url': 'https://x/'})
        self.assertEqual(got[0], 'a')

    async def test_the_title_separates_tabs_with_the_same_url(self):
        client = FakeClient(
            [{'context': 'a', 'url': 'https://x/'},
             {'context': 'b', 'url': 'https://x/'}],
            titles={'a': 'Inbox', 'b': 'Drafts'})
        got = await self.resolve(client, {'url': 'https://x/', 'title': 'Drafts'})
        self.assertEqual(got[0], 'b')

    async def test_the_visible_one_wins_for_the_active_tab(self):
        client = FakeClient(
            [{'context': 'a', 'url': 'https://x/'},
             {'context': 'b', 'url': 'https://x/'}],
            titles={'a': 'Same', 'b': 'Same'}, visible={'b'})
        got = await self.resolve(client, {'url': 'https://x/', 'title': 'Same',
                                          'active': True})
        self.assertEqual(got[0], 'b')

    async def test_an_ambiguous_match_is_refused_rather_than_guessed(self):
        client = FakeClient(
            [{'context': 'a', 'url': 'https://x/'},
             {'context': 'b', 'url': 'https://x/'}],
            titles={'a': 'Same', 'b': 'Same'})
        with self.assertRaises(bidi.BiDiUnavailable) as caught:
            await self.resolve(client, {'url': 'https://x/', 'title': 'Same',
                                        'active': False})
        self.assertFalse(caught.exception.backoff)

    async def test_no_match_is_refused(self):
        client = FakeClient([{'context': 'a', 'url': 'https://x/'}])
        with self.assertRaises(bidi.BiDiUnavailable):
            await self.resolve(client, {'url': 'https://elsewhere/'})

    async def test_without_the_extension_a_lone_tab_is_used(self):
        client = FakeClient([{'context': 'a', 'url': 'https://x/'}])
        self.assertEqual((await self.resolve(client, None))[0], 'a')

    async def test_without_the_extension_several_tabs_are_not_guessed(self):
        client = FakeClient([{'context': 'a', 'url': 'https://x/'},
                             {'context': 'b', 'url': 'https://y/'}])
        with self.assertRaises(bidi.BiDiUnavailable):
            await self.resolve(client, None)


class WantsTests(unittest.TestCase):

    def setUp(self):
        self.backend = bidi.BiDiBackend(port=1, enabled=True)

    def test_the_four_actions_it_takes(self):
        self.assertTrue(self.backend.wants('type', {'text': 'x'}))
        self.assertTrue(self.backend.wants('pressKey', {'key': 'a'}))
        self.assertTrue(self.backend.wants('screenshot', {}))
        self.assertTrue(self.backend.wants('click', {'selector': '#a'}))
        self.assertTrue(self.backend.wants('click', {'x': 0, 'y': 0}))

    def test_other_actions_stay_with_the_extension(self):
        for action in ('navigate', 'getText', 'scroll', 'executeScript'):
            self.assertFalse(self.backend.wants(action, {}), action)

    def test_clicks_that_do_more_than_a_plain_click_stay_with_the_extension(self):
        for extra in ({'xpath': '//a'}, {'text': 'Go'},
                      {'double_click': True}, {'right_click': 'true'}):
            with self.subTest(extra=extra):
                self.assertFalse(self.backend.wants(
                    'click', {'selector': '#a', **extra}))

    def test_a_flag_sent_as_the_string_false_is_not_a_double_click(self):
        self.assertTrue(self.backend.wants(
            'click', {'selector': '#a', 'double_click': 'false'}))

    def test_a_click_with_no_target_is_left_to_the_extension_to_refuse(self):
        self.assertFalse(self.backend.wants('click', {}))

    def test_it_can_be_turned_off(self):
        off = bidi.BiDiBackend(port=1, enabled=False)
        self.assertFalse(off.should_try())

    def test_the_environment_turns_it_off(self):
        with unittest.mock.patch.dict('os.environ', {'CLAUDE_BROWSERX_BIDI': '0'}):
            self.assertFalse(bidi.BiDiBackend(port=1).enabled)
        with unittest.mock.patch.dict('os.environ', {'CLAUDE_BROWSERX_BIDI': '1'}):
            self.assertEqual(bidi.BiDiBackend(port=1).enabled,
                             bidi.websockets is not None)


class StatusTests(unittest.TestCase):

    def test_it_says_how_to_turn_it_on_when_it_is_not_connected(self):
        status = bidi.BiDiBackend(port=9333, enabled=True).status()
        self.assertTrue(status['enabled'])
        self.assertFalse(status['connected'])
        self.assertIn('--remote-debugging-port=9333', status['hint'])
        self.assertEqual(status['endpoint'], 'ws://127.0.0.1:9333/session')

    def test_a_session_held_elsewhere_is_not_blamed_on_a_missing_flag(self):
        # The port answers, so Firefox was started with the flag; telling the
        # agent to start it again sent it the wrong way.
        backend = bidi.BiDiBackend(port=9333, enabled=True)
        backend.last_reason = ('Firefox would not open a BiDi session (session '
                               'not created: Maximum number of active '
                               'sessions); another client may be holding it')
        status = backend.status()
        self.assertIn('already has a BiDi session', status['hint'])
        self.assertIn('restart Firefox', status['hint'])
        self.assertIn('Maximum number of active sessions', status['last_error'])

    def test_nothing_listening_still_says_to_start_firefox_with_the_flag(self):
        backend = bidi.BiDiBackend(port=9333, enabled=True)
        backend.last_reason = 'nothing is listening for WebDriver BiDi'
        status = backend.status()
        self.assertIn('--remote-debugging-port=9333', status['hint'])
        self.assertIn('nothing is listening', status['last_error'])

    def test_off_means_no_hint(self):
        status = bidi.BiDiBackend(port=9333, enabled=False).status()
        self.assertFalse(status['enabled'])
        self.assertNotIn('hint', status)

    def test_safety_status_reports_it(self):
        handler = server.MCPHTTPHandler.__new__(server.MCPHTTPHandler)
        out = handler.execute_tool('browser_safety_status', {})
        self.assertIn('bidi', out)
        self.assertIn('connected', out['bidi'])


class FallbackNoteTests(unittest.TestCase):
    """When typing goes out as synthetic events although real ones were on
    offer, the result says so: a page that ignores them (Google Docs) made the
    call report success with nothing typed, and nothing said why."""

    def setUp(self):
        self.handler = server.MCPHTTPHandler.__new__(server.MCPHTTPHandler)

    def flag(self, action, arguments, result, enabled=True, wants=True,
             headless=False):
        backend = unittest.mock.Mock()
        backend.enabled = enabled
        backend.wants.return_value = wants
        backend.fallback_note.return_value = 'NOTE'
        with unittest.mock.patch.object(bidi, 'get_bidi_backend',
                                        lambda: backend), \
                unittest.mock.patch.object(server, 'HEADLESS_MODE', headless):
            return self.handler._flag_synthetic_input(action, arguments, result)

    def test_an_extension_result_for_input_carries_the_note(self):
        out = self.flag('type', {'text': 'x'}, {'success': True})
        self.assertEqual(out, {'success': True, 'input': 'synthetic',
                               'input_note': 'NOTE'})

    def test_a_failure_is_annotated_too(self):
        out = self.flag('pressKey', {'key': 'a'},
                        {'success': False, 'error': 'nope'})
        self.assertEqual(out['input'], 'synthetic')

    def test_a_result_that_came_through_bidi_is_left_alone(self):
        result = {'success': True, 'via': 'webdriver-bidi', 'input': 'trusted'}
        self.assertEqual(self.flag('type', {}, dict(result)), result)

    def test_other_actions_are_left_alone(self):
        for action in ('navigate', 'screenshot', 'getText', 'scroll'):
            with self.subTest(action=action):
                self.assertEqual(self.flag(action, {}, {'success': True}),
                                 {'success': True})

    def test_a_click_bidi_was_never_going_to_take_is_left_alone(self):
        out = self.flag('click', {'xpath': '//a'}, {'success': True}, wants=False)
        self.assertEqual(out, {'success': True})

    def test_nothing_is_said_when_bidi_is_off_or_in_headless_mode(self):
        self.assertEqual(self.flag('type', {}, {'success': True}, enabled=False),
                         {'success': True})
        self.assertEqual(self.flag('type', {}, {'success': True}, headless=True),
                         {'success': True})

    def test_execute_tool_applies_it_to_the_extensions_answer(self):
        self.handler._dispatch_action = lambda action, tab_id, args: {
            'success': True}
        backend = unittest.mock.Mock(enabled=True)
        backend.wants.return_value = True
        backend.fallback_note.return_value = 'NOTE'
        with unittest.mock.patch.object(bidi, 'get_bidi_backend',
                                        lambda: backend):
            out = self.handler.execute_tool(
                'browser_type', {'text': 'x', 'selector': '#t'})
        self.assertEqual(out['input_note'], 'NOTE')

    def test_the_note_says_how_to_get_real_input_when_not_connected(self):
        backend = bidi.BiDiBackend(port=1, enabled=True)
        note = backend.fallback_note()
        self.assertIn('synthetic', note)
        self.assertIn('Google Docs', note)
        self.assertIn('scripts/start-firefox', note)

    def test_the_note_gives_the_reason_when_connected_but_not_used(self):
        backend = bidi.BiDiBackend(port=1, enabled=True)
        backend._client = unittest.mock.Mock(connected=True)
        backend.last_reason = '2 Firefox tabs match'
        note = backend.fallback_note()
        self.assertIn('2 Firefox tabs match', note)
        self.assertNotIn('scripts/start-firefox', note)

    def test_the_note_gives_the_reason_when_it_is_not_connected(self):
        backend = bidi.BiDiBackend(port=1, enabled=True)
        backend.last_reason = 'Maximum number of active sessions'
        note = backend.fallback_note()
        self.assertIn('Maximum number of active sessions', note)
        self.assertIn('bidi.hint', note)

    def test_an_unavailable_call_records_why(self):
        backend = bidi.BiDiBackend(port=free_port(), enabled=True)
        self.addCleanup(backend.close)
        self.assertFalse(backend.available())
        self.assertIn('nothing is listening', backend.last_reason)


def free_port():
    import socket
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


class FakeBackend:
    def __init__(self, wants=True, available=True, result=None, raises=None):
        self._wants, self._available = wants, available
        self.result, self.raises = result, raises
        self.executed = []

    def wants(self, action, arguments):
        return self._wants

    def available(self):
        return self._available

    def execute(self, action, tab, arguments):
        self.executed.append((action, tab, arguments))
        if self.raises:
            raise self.raises
        return self.result


class ServerRoutingTests(unittest.TestCase):
    """When the server uses BiDi, and when it leaves the call to the
    extension."""

    def setUp(self):
        self.handler = server.MCPHTTPHandler.__new__(server.MCPHTTPHandler)
        self.lookups = []

        def tab(tab_id):
            self.lookups.append(tab_id)
            return {'id': 5, 'url': 'https://x/', 'title': 'x',
                    'active': True, 'incognito': False}
        self.handler._bidi_tab = tab

    def route(self, backend, action='type', arguments=None):
        with unittest.mock.patch.object(bidi, 'get_bidi_backend',
                                        lambda: backend):
            return self.handler._try_bidi(action, None, arguments or {'text': 'x'})

    def test_with_no_bidi_port_the_extension_is_not_even_asked_about_the_tab(self):
        backend = FakeBackend(available=False)
        self.assertIsNone(self.route(backend))
        self.assertEqual(self.lookups, [])
        self.assertEqual(backend.executed, [])

    def test_a_call_bidi_does_not_do_is_left_alone(self):
        backend = FakeBackend(wants=False)
        self.assertIsNone(self.route(backend))
        self.assertEqual(self.lookups, [])

    def test_a_call_it_takes_goes_through_with_the_tab_description(self):
        backend = FakeBackend(result={'success': True, 'input': 'trusted'})
        self.assertEqual(self.route(backend)['input'], 'trusted')
        action, tab, arguments = backend.executed[0]
        self.assertEqual((action, tab['id']), ('type', 5))

    def test_unavailable_falls_back_to_the_extension(self):
        backend = FakeBackend(raises=bidi.BiDiUnavailable('no match'))
        self.assertIsNone(self.route(backend))

    def test_a_guard_refusal_is_returned_not_retried_in_the_extension(self):
        refusal = {'success': False, 'error': 'Refused: password'}
        self.assertEqual(self.route(FakeBackend(result=refusal)), refusal)

    def test_the_arguments_are_not_shared_with_the_backend(self):
        backend = FakeBackend(result={'success': True})
        arguments = {'text': 'x'}
        self.route(backend, arguments=arguments)
        self.assertIsNot(backend.executed[0][2], arguments)

    def test_a_screenshot_needs_the_extension_to_say_the_tab_is_not_private(self):
        for tab in ({'id': 1, 'url': 'u'},                       # not said
                    {'id': 1, 'url': 'u', 'incognito': True}):   # private
            with self.subTest(tab=tab):
                self.handler._bidi_tab = lambda tab_id, tab=tab: tab
                backend = FakeBackend(result={'success': True, 'data': 'x'})
                self.assertIsNone(self.route(backend, 'screenshot', {}))
                self.assertEqual(backend.executed, [])

    def test_a_screenshot_of_an_ordinary_tab_is_saved_like_the_extensions(self):
        saved = []
        self.handler._save_screenshot = lambda result, arguments: (
            saved.append(result) or {'success': True, 'filepath': 'p'})
        backend = FakeBackend(result={'success': True, 'data': 'data:image/png;base64,AA=='})
        out = self.route(backend, 'screenshot', {})
        self.assertEqual(out, {'success': True, 'filepath': 'p'})
        self.assertEqual(len(saved), 1)

    def test_dispatch_tries_bidi_first_but_not_in_headless_mode(self):
        calls = []
        self.handler._try_bidi = lambda action, tab_id, arguments: (
            calls.append(action) or {'success': True, 'via': 'bidi'})
        out = self.handler._dispatch_action('type', None, {'text': 'x'})
        self.assertEqual(out, {'success': True, 'via': 'bidi'})
        self.assertEqual(calls, ['type'])

        calls.clear()
        with unittest.mock.patch.object(server, 'HEADLESS_MODE', True), \
                unittest.mock.patch.object(
                    server, 'HEADLESS_STARTUP_TIMEOUT', 0.01), \
                unittest.mock.patch.object(
                    server.connection_manager, 'get_active_browser',
                    lambda: None), \
                unittest.mock.patch.object(server, 'MAIN_EVENT_LOOP', None):
            self.handler._dispatch_action('type', None, {'text': 'x'})
        self.assertEqual(calls, [], 'headless has no Firefox to attach to')

    def test_actions_bidi_does_not_take_never_reach_it(self):
        calls = []
        self.handler._try_bidi = lambda *a: calls.append(a) or None
        self.assertNotIn('getTabInfo', bidi.BiDiBackend.ACTIONS)
        self.assertNotIn('navigate', bidi.BiDiBackend.ACTIONS)
        self.assertEqual(calls, [])


if __name__ == '__main__':
    unittest.main()
