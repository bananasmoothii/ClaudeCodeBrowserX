#!/usr/bin/env python3
"""
Tests for the accessibility snapshot and ref tools (mcp-server/ax.py,
mcp-server/ax_page.js): argument handling, the script that is sent, how the
guard treats the six tools, and how the server dispatches them.

The page-side behaviour (the tree, refs, clicking, typing) is tested in
tests/ax_page.test.mjs, which runs ax_page.js against a DOM stub.

Run: python3 -m unittest tests.test_ax -v
"""

import json
import sys
import unittest
from pathlib import Path

from tests import TEST_HOME  # noqa: F401  (redirects HOME on import)

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'mcp-server'))

import ax  # noqa: E402
import safety  # noqa: E402
import server  # noqa: E402

OBSERVE = ('browser_snapshot', 'browser_find')
ACT = ('browser_click_ref', 'browser_type_ref', 'browser_select_ref',
       'browser_hover_ref')


def guard(**config):
    g = safety.SafetyGuard()
    g.config.update(config)
    g._compile_patterns()
    return g


class PrepareTests(unittest.TestCase):

    def test_snapshot_defaults_and_clamping(self):
        command, args = ax.prepare('browser_snapshot', {})
        self.assertEqual(command, 'snapshot')
        self.assertEqual(args, {'interactiveOnly': False,
                                'maxChars': ax.DEFAULT_MAX_CHARS})
        _, args = ax.prepare('browser_snapshot', {'max_chars': 1, 'interactive_only': 'true'})
        self.assertEqual(args['maxChars'], ax.MIN_MAX_CHARS)
        self.assertTrue(args['interactiveOnly'])
        _, args = ax.prepare('browser_snapshot', {'max_chars': 10 ** 9})
        self.assertEqual(args['maxChars'], ax.MAX_MAX_CHARS)

    def test_the_string_false_is_false(self):
        """bool("false") is True; a JSON client may well send the string."""
        _, args = ax.prepare('browser_snapshot', {'interactive_only': 'false'})
        self.assertFalse(args['interactiveOnly'])

    def test_a_selector_is_passed_and_bounded(self):
        _, args = ax.prepare('browser_snapshot', {'selector': 'main form'})
        self.assertEqual(args['selector'], 'main form')
        with self.assertRaises(ValueError):
            ax.prepare('browser_snapshot', {'selector': 'x' * 5000})
        with self.assertRaises(ValueError):
            ax.prepare('browser_snapshot', {'selector': 5})

    def test_find_needs_text_or_role(self):
        with self.assertRaises(ValueError):
            ax.prepare('browser_find', {})
        with self.assertRaises(ValueError):
            ax.prepare('browser_find', {'text': '', 'role': ''})
        _, args = ax.prepare('browser_find', {'text': 'Buy', 'limit': 9999})
        self.assertEqual(args['limit'], 100)
        self.assertEqual(args['text'], 'Buy')
        _, args = ax.prepare('browser_find', {'role': 'button', 'exact': True})
        self.assertTrue(args['exact'])

    def test_refs_are_normalised_and_checked(self):
        for given in ('ref_12', 'REF_12', ' ref_12 ', '12', 12):
            with self.subTest(given=given):
                self.assertEqual(ax.normalise_ref(given), 'ref_12')
        for bad in ('', 'ref_', 'ref_x', 'ref_1; alert(1)', '1' * 12, None, True,
                    [1], {'ref': 1}, -1, 1.5):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    ax.normalise_ref(bad)

    def test_every_action_requires_a_ref(self):
        for tool in ACT:
            with self.subTest(tool=tool):
                with self.assertRaises(ValueError) as caught:
                    ax.prepare(tool, {'text': 'x', 'value': 'x'})
                self.assertIn('ref', str(caught.exception))

    def test_click_arguments(self):
        _, args = ax.prepare('browser_click_ref', {'ref': 'ref_3'})
        self.assertEqual(args, {'ref': 'ref_3', 'button': 'left', 'doubleClick': False})
        _, args = ax.prepare('browser_click_ref',
                             {'ref': 3, 'button': 'RIGHT', 'double_click': True})
        self.assertEqual((args['button'], args['doubleClick']), ('right', True))
        with self.assertRaises(ValueError):
            ax.prepare('browser_click_ref', {'ref': 'ref_3', 'button': 'middle'})

    def test_type_needs_text_but_empty_text_is_allowed(self):
        with self.assertRaises(ValueError):
            ax.prepare('browser_type_ref', {'ref': 'ref_1'})
        _, args = ax.prepare('browser_type_ref', {'ref': 'ref_1', 'text': ''})
        self.assertEqual(args['text'], '')
        self.assertTrue(args['clear'])
        self.assertFalse(args['submit'])
        with self.assertRaises(ValueError):
            ax.prepare('browser_type_ref', {'ref': 'ref_1', 'text': 5})
        with self.assertRaises(ValueError):
            ax.prepare('browser_type_ref',
                       {'ref': 'ref_1', 'text': 'x' * (ax.MAX_TYPED_CHARS + 1)})

    def test_select_takes_exactly_one_choice(self):
        with self.assertRaises(ValueError):
            ax.prepare('browser_select_ref', {'ref': 'ref_1'})
        with self.assertRaises(ValueError) as caught:
            ax.prepare('browser_select_ref', {'ref': 'ref_1', 'value': 'a', 'text': 'A'})
        self.assertIn('only one', str(caught.exception))
        self.assertEqual(
            ax.prepare('browser_select_ref', {'ref': 'ref_1', 'index': '2'})[1]['index'], 2)
        self.assertEqual(
            ax.prepare('browser_select_ref', {'ref': 'ref_1', 'values': ['a', 2]})[1]['values'],
            ['a', '2'])
        for bad in ([], 'a', [{}], [True]):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    ax.prepare('browser_select_ref', {'ref': 'ref_1', 'values': bad})

    def test_tab_id_is_not_sent_to_the_page(self):
        _, args = ax.prepare('browser_hover_ref', {'ref': 'ref_1', 'tab_id': 4})
        self.assertNotIn('tab_id', args)
        self.assertNotIn('tabId', args)

    def test_an_unknown_tool_is_refused(self):
        with self.assertRaises(ValueError):
            ax.prepare('browser_get_text', {})


class BuildScriptTests(unittest.TestCase):

    def test_the_credential_logic_is_spliced_in_unchanged(self):
        script = ax.build_script('snapshot', {})
        credentials = (ROOT / 'extension' / 'credentials.js').read_text(encoding='utf-8')
        self.assertEqual(script.count(credentials), 1)
        self.assertIn('function isPasswordField', script)
        self.assertIn('function scrubNestedCredentials', script)

    def test_no_placeholder_survives(self):
        script = ax.build_script('click', {'ref': 'ref_1'})
        self.assertNotIn('__CREDENTIALS__', script)
        self.assertNotIn('__COMMAND__', script)

    def test_the_command_travels_as_one_json_object(self):
        script = ax.build_script('type', {'ref': 'ref_1', 'text': '"; alert(1); //'},
                                 allow_password=True)
        tail = script.rstrip().rsplit('})(', 1)[1].rstrip(';').rstrip(')')
        payload = json.loads(tail)
        self.assertEqual(payload['command'], 'type')
        self.assertEqual(payload['args']['text'], '"; alert(1); //')
        self.assertIs(payload['allowPassword'], True)

    def test_the_password_flag_defaults_to_off(self):
        script = ax.build_script('snapshot', {})
        self.assertIn('"allowPassword": false', script)

    def test_line_separators_in_text_cannot_end_the_literal(self):
        """JSON allows U+2028 in strings; older JavaScript does not."""
        script = ax.build_script('type', {'ref': 'ref_1', 'text': 'a b c'})
        self.assertNotIn(' ', script)
        self.assertNotIn(' ', script)

    def test_the_script_is_a_single_expression_with_no_network_or_eval(self):
        """No new network calls, no eval of remote content."""
        source = (ROOT / 'mcp-server' / 'ax_page.js').read_text(encoding='utf-8')
        for forbidden in ('fetch(', 'XMLHttpRequest', 'WebSocket', 'sendBeacon',
                          'importScripts', 'eval(', 'new Function', 'document.write',
                          'innerHTML', 'browser.', 'chrome.'):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, source)

    def test_the_two_placeholders_each_appear_exactly_once(self):
        source = (ROOT / 'mcp-server' / 'ax_page.js').read_text(encoding='utf-8')
        self.assertEqual(source.count('/*__CREDENTIALS__*/'), 1)
        self.assertEqual(source.count('/*__COMMAND__*/ null'), 1)


class InterpretTests(unittest.TestCase):

    def test_success_drops_the_ok_flag_and_keeps_the_rest(self):
        self.assertEqual(
            ax.interpret({'ok': True, 'snapshot': '- main', 'url': 'u'}),
            {'success': True, 'snapshot': '- main', 'url': 'u'})

    def test_failure_keeps_the_code_and_the_message(self):
        result = ax.interpret({'ok': False, 'code': 'stale_ref', 'error': 'ref_1 is stale'})
        self.assertEqual(result, {'success': False, 'error': 'ref_1 is stale',
                                  'code': 'stale_ref'})

    def test_no_result_says_what_that_usually_means(self):
        for raw in (None, 'text', 5):
            result = ax.interpret(raw)
            self.assertFalse(result['success'])
            self.assertIn('no result', result['error'])


class GuardTests(unittest.TestCase):

    def test_every_tool_is_covered_by_the_script_toggle(self):
        for tool in OBSERVE + ACT:
            with self.subTest(tool=tool):
                denial = guard(allow_script_execution=False).check(tool, {'ref': 'ref_1'})
                self.assertIsNotNone(denial)
                self.assertEqual(denial['safety_decision'], 'scripts_disabled')

    def test_every_tool_is_refused_on_a_protected_site(self):
        for tool in OBSERVE + ACT:
            with self.subTest(tool=tool):
                g = guard()
                g.note_url({'url': 'https://www.chase.com/transfer'})
                denial = g.check(tool, {'ref': 'ref_1'})
                self.assertIsNotNone(denial)
                self.assertEqual(denial['safety_decision'],
                                 'scripts_denied_on_protected_url')

    def test_reading_is_allowed_in_read_only_mode_and_acting_is_not(self):
        g = guard(read_only=True)
        for tool in OBSERVE:
            with self.subTest(tool=tool):
                self.assertIsNone(g.check(tool, {}))
        for tool in ACT:
            with self.subTest(tool=tool):
                denial = g.check(tool, {'ref': 'ref_1'})
                self.assertIsNotNone(denial)
                self.assertEqual(denial['safety_decision'], 'read_only')

    def test_the_actions_are_allowed_on_an_ordinary_page(self):
        g = guard()
        g.note_url({'url': 'https://example.com/'})
        for tool in OBSERVE + ACT:
            with self.subTest(tool=tool):
                self.assertIsNone(g.check(tool, {'ref': 'ref_1'}))

    def test_a_blocked_url_stops_every_tool(self):
        g = guard(blocked_url_patterns=[r'blocked\.example'])
        g.note_url({'url': 'https://blocked.example/x'})
        for tool in OBSERVE + ACT:
            with self.subTest(tool=tool):
                denial = g.check(tool, {'ref': 'ref_1'})
                self.assertEqual(denial['safety_decision'], 'blocked_url')

    def test_status_lists_all_six_among_the_script_tools(self):
        listed = guard().status()['script_tools']
        for tool in OBSERVE + ACT:
            self.assertIn(tool, listed)

    def test_a_rate_limit_applies_to_them_too(self):
        g = guard(max_actions_per_minute=2)
        g.note_url({'url': 'https://example.com/'})
        g.check('browser_snapshot', {})
        g.check('browser_snapshot', {})
        denial = g.check('browser_snapshot', {})
        self.assertEqual(denial['safety_decision'], 'rate_limited')

    def test_typed_text_does_not_reach_the_audit_log(self):
        redacted = safety.redact_arguments(
            {'ref': 'ref_4', 'text': 'correct horse battery staple', 'tab_id': 2})
        self.assertNotIn('correct horse', json.dumps(redacted))
        self.assertEqual(redacted['ref'], 'ref_4')

    def test_recheck_judges_the_page_the_script_ran_on(self):
        g = guard()
        self.assertIsNone(g.check('browser_snapshot', {}), 'no URL known yet')
        self.assertIsNone(g.recheck_script_after_url('browser_snapshot'))
        g.note_url({'url': 'https://www.chase.com/'})
        denial = g.recheck_script_after_url('browser_snapshot')
        self.assertEqual(denial['safety_decision'], 'scripts_denied_on_protected_url')

    def test_recheck_honours_the_off_switches(self):
        g = guard(deny_scripts_on_protected_urls=False)
        g.note_url({'url': 'https://www.chase.com/'})
        self.assertIsNone(g.recheck_script_after_url('browser_snapshot'))
        g = guard(enabled=False)
        g.note_url({'url': 'https://www.chase.com/'})
        self.assertIsNone(g.recheck_script_after_url('browser_snapshot'))


class RegistrationTests(unittest.TestCase):

    def test_the_six_tools_are_exposed_with_their_required_arguments(self):
        tools = {t.name: t for t in server.MCP_TOOLS}
        for name in OBSERVE + ACT:
            self.assertIn(name, tools)
            self.assertIn('tab_id', tools[name].input_schema['properties'])
        for name in ACT:
            self.assertIn('ref', tools[name].input_schema['required'])
        self.assertIn('text', tools['browser_type_ref'].input_schema['required'])

    def test_every_tool_is_classified_by_the_guard(self):
        """A tool the guard does not know defaults to a plain ACT tool, which
        for these would silently skip the script rules."""
        known = (safety.OBSERVE_SCRIPT_TOOLS | safety.FIXED_SCRIPT_ACT_TOOLS)
        self.assertEqual(known & set(ax.AX_TOOLS), set(ax.AX_TOOLS))
        self.assertEqual(ax.OBSERVE_AX_TOOLS, set(OBSERVE))
        self.assertEqual(ax.ACT_AX_TOOLS, set(ACT))
        self.assertLessEqual(ax.OBSERVE_AX_TOOLS, safety.OBSERVE_TOOLS)
        self.assertFalse(ax.ACT_AX_TOOLS & safety.OBSERVE_TOOLS)

    def test_results_are_labelled_as_page_content(self):
        import stdio_wrapper
        for name in OBSERVE + ACT:
            self.assertIn(name, stdio_wrapper.PAGE_CONTENT_TOOLS)

    def test_the_installers_copy_the_new_files(self):
        for script in ('scripts/install.ps1', 'scripts/install.sh'):
            text = (ROOT / script).read_text(encoding='utf-8')
            self.assertIn('ax.py', text, script)
            self.assertIn('ax_page.js', text, script)


class ServerDispatchTests(unittest.TestCase):

    def setUp(self):
        self.guard = safety.get_safety_guard()
        before = dict(self.guard.config)
        self.addCleanup(self.guard.config.update, before)
        self.guard.config.update({'allow_script_execution': True, 'read_only': False,
                                  'deny_scripts_on_protected_urls': True,
                                  'allow_password_typing': False,
                                  'max_actions_per_minute': 100000})
        self.guard.note_url({'url': 'https://example.com/'})
        self.addCleanup(self.guard.note_url, {'url': 'https://example.com/'})

    def _execute(self, tool, arguments, page_result=None):
        calls = []

        def fake_dispatch(action, tab_id, args):
            calls.append({'action': action, 'tab_id': tab_id, 'args': args})
            return {'success': True, 'result': page_result if page_result is not None
                    else {'ok': True, 'url': 'https://example.com/'}}

        handler = server.MCPHTTPHandler.__new__(server.MCPHTTPHandler)
        handler._dispatch_action = fake_dispatch
        return handler.execute_tool(tool, dict(arguments)), calls

    def _payload(self, call):
        script = call['args']['script']
        tail = script.rstrip().rsplit('})(', 1)[1].rstrip(';').rstrip(')')
        return json.loads(tail)

    def test_a_snapshot_goes_out_as_one_execute_script(self):
        result, calls = self._execute(
            'browser_snapshot', {'tab_id': '5', 'interactive_only': True},
            {'ok': True, 'snapshot': '- main [ref_1]', 'url': 'https://example.com/'})
        self.assertTrue(result['success'], result)
        self.assertEqual(result['snapshot'], '- main [ref_1]')
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]['action'], 'executeScript')
        self.assertEqual(calls[0]['tab_id'], 5)
        payload = self._payload(calls[0])
        self.assertEqual(payload['command'], 'snapshot')
        self.assertTrue(payload['args']['interactiveOnly'])

    def test_each_action_maps_to_its_command(self):
        for tool, command, args in (
                ('browser_click_ref', 'click', {'ref': 'ref_2'}),
                ('browser_hover_ref', 'hover', {'ref': 'ref_2'}),
                ('browser_type_ref', 'type', {'ref': 'ref_2', 'text': 'hi'}),
                ('browser_select_ref', 'select', {'ref': 'ref_2', 'value': 'a'}),
                ('browser_find', 'find', {'text': 'Buy'})):
            with self.subTest(tool=tool):
                result, calls = self._execute(tool, args)
                self.assertTrue(result['success'], result)
                self.assertEqual(self._payload(calls[0])['command'], command)

    def test_a_bad_argument_is_refused_before_anything_is_sent(self):
        result, calls = self._execute('browser_click_ref', {'ref': 'nope'})
        self.assertFalse(result['success'])
        self.assertIn('ref', result['error'])
        self.assertEqual(calls, [])

    def test_the_agent_cannot_turn_the_password_guard_off(self):
        for spelling in ('allow_password', 'allowPassword', 'ALLOW_PASSWORD'):
            with self.subTest(spelling=spelling):
                _, calls = self._execute(
                    'browser_snapshot', {spelling: True, 'allow_password_typing': True})
                self.assertIs(self._payload(calls[0])['allowPassword'], False)

    def test_only_the_safety_config_turns_it_on(self):
        self.guard.config['allow_password_typing'] = True
        _, calls = self._execute('browser_snapshot', {})
        self.assertIs(self._payload(calls[0])['allowPassword'], True)

    def test_a_stale_ref_error_reaches_the_agent_with_its_code(self):
        result, _ = self._execute('browser_click_ref', {'ref': 'ref_9'}, {
            'ok': False, 'code': 'stale_ref',
            'error': 'ref_9 is stale: take a new snapshot with browser_snapshot'})
        self.assertFalse(result['success'])
        self.assertEqual(result['code'], 'stale_ref')
        self.assertIn('browser_snapshot', result['error'])

    def test_a_transport_failure_is_reported_under_the_tool_name(self):
        handler = server.MCPHTTPHandler.__new__(server.MCPHTTPHandler)
        handler._dispatch_action = lambda *a: {'success': False, 'error': 'No browser connected'}
        result = handler.execute_tool('browser_snapshot', {})
        self.assertFalse(result['success'])
        self.assertIn('browser_snapshot', result['error'])
        self.assertIn('No browser connected', result['error'])

    def test_scripts_off_means_nothing_is_dispatched(self):
        self.guard.config['allow_script_execution'] = False
        for tool in OBSERVE + ACT:
            with self.subTest(tool=tool):
                result, calls = self._execute(tool, {'ref': 'ref_1', 'text': 'x'})
                self.assertEqual(result['safety_decision'], 'scripts_disabled')
                self.assertEqual(calls, [])

    def test_read_only_stops_the_actions_but_not_the_snapshot(self):
        self.guard.config['read_only'] = True
        result, calls = self._execute('browser_click_ref', {'ref': 'ref_1'})
        self.assertEqual(result['safety_decision'], 'read_only')
        self.assertEqual(calls, [])
        result, calls = self._execute('browser_snapshot', {})
        self.assertTrue(result['success'], result)
        self.assertEqual(len(calls), 1)

    def test_a_snapshot_of_a_protected_page_the_guard_did_not_know_of_is_withheld(self):
        """The first script tool of a session runs before the guard has seen a
        URL. The result names the page, and a protected one is not returned."""
        self.guard.note_url({'url': 'https://example.com/'})
        # The guard learns the URL from the result, so forget it first.
        with self.guard._lock:
            self.guard._current_url = None
        result, calls = self._execute(
            'browser_snapshot', {},
            {'ok': True, 'snapshot': '- textbox "Account number"',
             'url': 'https://www.chase.com/accounts'})
        self.assertEqual(len(calls), 1)
        self.assertFalse(result['success'])
        self.assertEqual(result['safety_decision'], 'scripts_denied_on_protected_url')
        self.assertNotIn('snapshot', result)

    def test_the_result_url_is_what_the_guard_judges_next(self):
        self._execute('browser_snapshot', {},
                      {'ok': True, 'snapshot': '', 'url': 'https://www.chase.com/x'})
        self.guard.config['deny_scripts_on_protected_urls'] = True
        result, calls = self._execute('browser_click_ref', {'ref': 'ref_1'})
        self.assertEqual(result['safety_decision'], 'scripts_denied_on_protected_url')
        self.assertEqual(calls, [])

    def test_a_missing_page_script_is_reported_not_raised(self):
        original = ax._template_cache
        self.addCleanup(setattr, ax, '_template_cache', original)
        ax._template_cache = None
        real = ax.HERE
        self.addCleanup(setattr, ax, 'HERE', real)
        ax.HERE = ROOT / 'tests' / 'no-such-directory'
        result, calls = self._execute('browser_snapshot', {})
        self.assertFalse(result['success'])
        self.assertIn('unavailable', result['error'])
        self.assertEqual(calls, [])


if __name__ == '__main__':
    unittest.main()
