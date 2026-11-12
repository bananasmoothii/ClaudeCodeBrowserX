#!/usr/bin/env python3
"""
Accessibility-tree snapshot and ref-based actions for ClaudeCodeBrowserX.

MIT License
Copyright (c) 2025 Andre Watson (nanogenomic), Ligandal Inc.
Author: dre@ligandal.com

One browser_snapshot call returns a compact tree of the page - roles,
accessible names, states, values - with a stable ref (ref_12) on each element.
browser_find searches that tree by text or role. browser_click_ref,
browser_hover_ref, browser_type_ref and browser_select_ref act on a ref.

Nothing here touches the extension. The page-side code is mcp-server/ax_page.js,
a fixed script this module fills in and sends through the existing executeScript
action, exactly the way browser_audit_page sends its audit script. Firefox runs
that code in the extension's content-script sandbox, so the ref table kept on
globalThis survives from one call to the next and the page cannot see it.

Because these tools run a script in the page they are guarded as script tools
(safety.py: OBSERVE_SCRIPT_TOOLS and FIXED_SCRIPT_ACT_TOOLS): refused when
allow_script_execution is false and on a protected URL, and the four ref
actions are also refused in read-only mode. Credential handling is
extension/credentials.js, spliced into the script unchanged.
"""

import json
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

HERE = Path(__file__).resolve().parent

# tool name -> the command ax_page.js runs
COMMANDS = {
    'browser_snapshot': 'snapshot',
    'browser_find': 'find',
    'browser_click_ref': 'click',
    'browser_hover_ref': 'hover',
    'browser_type_ref': 'type',
    'browser_select_ref': 'select',
}

AX_TOOLS = frozenset(COMMANDS)
# Tools that only read the page.
OBSERVE_AX_TOOLS = frozenset({'browser_snapshot', 'browser_find'})
# Tools that change the page by acting on a ref.
ACT_AX_TOOLS = AX_TOOLS - OBSERVE_AX_TOOLS

DEFAULT_MAX_CHARS = 20000
MIN_MAX_CHARS = 500
MAX_MAX_CHARS = 200000
MAX_TYPED_CHARS = 20000
MAX_SELECTOR_CHARS = 1000

_TRUE = {'true', '1', 'yes', 'on'}
_FALSE = {'false', '0', 'no', 'off', ''}


def _flag(value: Any, fallback: bool) -> bool:
    """bool("false") is True, so a JSON client that sends the string would
    otherwise get the opposite of what it asked for."""
    if value is None:
        return fallback
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        text = value.strip().lower()
        if text in _TRUE:
            return True
        if text in _FALSE:
            return False
    return fallback


def _int(value: Any, fallback: int, low: int, high: int) -> int:
    try:
        number = int(float(value))
    except (TypeError, ValueError):
        return fallback
    return max(low, min(high, number))


def _text(name: str, value: Any, limit: int) -> str:
    if not isinstance(value, str):
        raise ValueError(f'{name} must be a string.')
    if len(value) > limit:
        raise ValueError(f'{name} is too long ({len(value)} characters; '
                         f'the limit is {limit}).')
    return value


def normalise_ref(value: Any) -> str:
    """Accept ref_12, "12" or 12 and return "ref_12"."""
    if isinstance(value, bool):
        value = None
    if isinstance(value, int):
        text = str(value)
    elif isinstance(value, str):
        text = value.strip()
    else:
        text = ''
    if text.lower().startswith('ref_'):
        text = text[4:]
    if not text.isdigit() or len(text) > 9:
        raise ValueError(
            'ref must look like ref_12 and come from browser_snapshot or '
            'browser_find.')
    return f'ref_{int(text)}'


def prepare(tool_name: str, arguments: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
    """Validate a tool call and return (command, args) for ax_page.js.

    Raises ValueError with a message meant for the agent. Argument names go to
    the page in camelCase.
    """
    if tool_name not in COMMANDS:
        raise ValueError(f'Unknown tool: {tool_name}')
    command = COMMANDS[tool_name]
    out: Dict[str, Any] = {}

    selector = arguments.get('selector')
    if selector not in (None, ''):
        out['selector'] = _text('selector', selector, MAX_SELECTOR_CHARS)

    if command == 'snapshot':
        out['interactiveOnly'] = _flag(arguments.get('interactive_only'), False)
        out['maxChars'] = _int(arguments.get('max_chars'), DEFAULT_MAX_CHARS,
                               MIN_MAX_CHARS, MAX_MAX_CHARS)
    elif command == 'find':
        text = arguments.get('text')
        role = arguments.get('role')
        if text in (None, '') and role in (None, ''):
            raise ValueError('browser_find needs text, role, or both.')
        if text not in (None, ''):
            out['text'] = _text('text', text, 500)
        if role not in (None, ''):
            out['role'] = _text('role', role, 60)
        out['exact'] = _flag(arguments.get('exact'), False)
        out['interactiveOnly'] = _flag(arguments.get('interactive_only'), False)
        out['limit'] = _int(arguments.get('limit'), 20, 1, 100)
    else:
        out['ref'] = normalise_ref(arguments.get('ref'))
        if command == 'click':
            button = str(arguments.get('button', 'left')).strip().lower() or 'left'
            if button not in ('left', 'right'):
                raise ValueError('button must be "left" or "right".')
            out['button'] = button
            out['doubleClick'] = _flag(arguments.get('double_click'), False)
        elif command == 'type':
            if 'text' not in arguments:
                raise ValueError('browser_type_ref needs text (use "" to clear the field).')
            out['text'] = _text('text', arguments.get('text'), MAX_TYPED_CHARS)
            out['clear'] = _flag(arguments.get('clear'), True)
            out['submit'] = _flag(arguments.get('submit'), False)
        elif command == 'select':
            given = [k for k in ('value', 'text', 'index', 'values')
                     if arguments.get(k) is not None]
            if not given:
                raise ValueError('browser_select_ref needs one of value, text, '
                                 'index or values.')
            if len(given) > 1:
                raise ValueError('Give only one of value, text, index or values '
                                 f'(got {", ".join(given)}).')
            key = given[0]
            if key == 'values':
                values = arguments['values']
                if (not isinstance(values, list) or not values or len(values) > 100
                        or not all(isinstance(v, (str, int)) and not isinstance(v, bool)
                                   for v in values)):
                    raise ValueError('values must be a non-empty list of option '
                                     'values or texts.')
                out['values'] = [str(v) for v in values]
            elif key == 'index':
                try:
                    out['index'] = int(arguments['index'])
                except (TypeError, ValueError):
                    raise ValueError('index must be an integer.')
            else:
                out[key] = _text(key, arguments[key], 2000) \
                    if isinstance(arguments[key], str) else str(arguments[key])
    return command, out


_template_cache: Optional[str] = None


def _load_template() -> str:
    """ax_page.js with credentials.js spliced in, placeholders left for the
    command. credentials.js is looked for next to this file (the installed
    layout) and in the checkout's extension/ directory."""
    global _template_cache
    if _template_cache is not None:
        return _template_cache
    page = HERE / 'ax_page.js'
    credentials = None
    for candidate in (HERE / 'credentials.js',
                      HERE.parent / 'extension' / 'credentials.js'):
        if candidate.is_file():
            credentials = candidate
            break
    if not page.is_file():
        raise RuntimeError(f'ax_page.js not found next to {Path(__file__).name}; '
                           'reinstall the server files.')
    if credentials is None:
        raise RuntimeError('credentials.js not found next to ax.py or in '
                           'extension/: the snapshot cannot mask credential '
                           'fields without it, so it will not run.')
    template = page.read_text(encoding='utf-8')
    # Exactly once each: str.replace would fill a mention in a comment too.
    if (template.count('/*__CREDENTIALS__*/') != 1
            or template.count('/*__COMMAND__*/ null') != 1):
        raise RuntimeError('ax_page.js must have each placeholder ax.py fills in '
                           'exactly once.')
    _template_cache = template.replace(
        '/*__CREDENTIALS__*/', credentials.read_text(encoding='utf-8'))
    return _template_cache


def build_script(command: str, args: Dict[str, Any], allow_password: bool = False) -> str:
    """The complete script to hand to executeScript."""
    payload = json.dumps(
        {'command': command, 'args': args, 'allowPassword': bool(allow_password)},
        ensure_ascii=True)
    return _load_template().replace('/*__COMMAND__*/ null', payload)


def interpret(raw: Any) -> Dict[str, Any]:
    """Turn what ax_page.js returned into a tool result."""
    if not isinstance(raw, dict):
        return {'success': False,
                'error': 'The page returned no result. It may still be loading, or it '
                         'is a page extensions cannot script (about:, the add-on store).'}
    body = {k: v for k, v in raw.items() if k != 'ok'}
    if raw.get('ok') is True:
        return {'success': True, **body}
    return {'success': False,
            'error': str(body.pop('error', 'The page script failed.')),
            **body}
