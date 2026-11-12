/**
 * Tests for mcp-server/ax_page.js, the page-side script behind browser_snapshot,
 * browser_find and the ref actions. No dependencies: a small DOM stub is built
 * here, the script is filled in the way ax.py fills it (credentials.js spliced
 * in, one JSON command), and it is run in a vm context. Calling it again in the
 * same context is "the same page, a later call"; a new context is a navigation.
 *
 * Run: node tests/ax_page.test.mjs
 */

import { readFileSync } from 'node:fs';
import { createContext, runInContext } from 'node:vm';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import assert from 'node:assert/strict';

const here = dirname(fileURLToPath(import.meta.url));
const TEMPLATE = readFileSync(join(here, '..', 'mcp-server', 'ax_page.js'), 'utf8');
const CREDENTIALS = readFileSync(join(here, '..', 'extension', 'credentials.js'), 'utf8');

// ----------------------------------------------------------------- DOM stub

const INLINE = new Set(['A', 'SPAN', 'B', 'I', 'EM', 'STRONG', 'CODE', 'SMALL', 'LABEL',
  'BUTTON', 'INPUT', 'SELECT', 'TEXTAREA', 'IMG', 'SVG']);

class Ev {
  constructor(type, init = {}) {
    this.type = type;
    Object.assign(this, init);
    this.defaultPrevented = false;
  }
  preventDefault() { this.defaultPrevented = true; }
}

function matchesSimple(el, sel) {
  sel = sel.trim();
  let m = /^([a-z0-9]*)((?:\[[^\]]+\])*)((?::not\(\[[^\]]+\]\))?)$/i.exec(sel);
  if (!m) return false;
  const [, tag, attrs, not] = m;
  if (tag && el.tagName !== tag.toUpperCase()) return false;
  for (const a of attrs.match(/\[[^\]]+\]/g) || []) {
    const [k, v] = a.slice(1, -1).split('=');
    if (v === undefined ? !el.hasAttribute(k) : el.getAttribute(k) !== v) return false;
  }
  if (not) {
    const [k, v] = not.slice(6, -2).split('=');
    if (el.getAttribute(k) === v) return false;
  }
  return true;
}

function build(doc) {
  class El {
    constructor(tag, attrs, kids) {
      this.nodeType = 1;
      this.tagName = tag.toUpperCase();
      this._attrs = { ...attrs };
      this.childNodes = [];
      this.parentNode = null;
      this.ownerDocument = doc;
      this._style = {};
      this._listeners = {};
      this.disabled = 'disabled' in attrs;
      this.required = 'required' in attrs;
      this.readOnly = 'readonly' in attrs;
      this.inert = false;
      this.log = [];
      for (const k of kids) this.add(k);
    }
    add(kid) {
      const node = typeof kid === 'string'
        ? { nodeType: 3, nodeValue: kid, parentNode: this } : kid;
      node.parentNode = this;
      this.childNodes.push(node);
      return node;
    }
    get id() { return this._attrs.id || ''; }
    get name() { return this._attrs.name || ''; }
    get type() { return this._attrs.type || (this.tagName === 'INPUT' ? 'text' : undefined); }
    get href() { return this._attrs.href; }
    get isContentEditable() {
      const v = this._attrs.contenteditable;
      return v !== undefined && v !== 'false';
    }
    get textContent() {
      return this.childNodes.map(c => c.nodeType === 3 ? c.nodeValue : c.textContent).join('');
    }
    set textContent(v) {
      this.childNodes = [];
      this.add(String(v));
    }
    querySelectorAll(sel) {
      const parts = sel.split(',');
      return doc.all(this).filter(e => parts.some(p => matchesSimple(e, p)));
    }
    get isConnected() {
      let n = this;
      while (n.parentNode) n = n.parentNode;
      return n === doc.documentElement;
    }
    get form() {
      for (let n = this.parentNode; n; n = n.parentNode) if (n.tagName === 'FORM') return n;
      return null;
    }
    get labels() {
      if (!['INPUT', 'SELECT', 'TEXTAREA', 'BUTTON'].includes(this.tagName)) return undefined;
      return doc.all().filter(l => l.tagName === 'LABEL' &&
        ((l._attrs.for && l._attrs.for === this.id) || l.contains(this)));
    }
    get control() {
      if (this.tagName !== 'LABEL') return undefined;
      if (this._attrs.for) return doc.byId(this._attrs.for);
      return doc.all(this).find(e => ['INPUT', 'SELECT', 'TEXTAREA'].includes(e.tagName)) || null;
    }
    getAttribute(n) { return n in this._attrs ? this._attrs[n] : null; }
    hasAttribute(n) { return n in this._attrs; }
    contains(o) { for (let n = o; n; n = n.parentNode) if (n === this) return true; return false; }
    getRootNode() { return doc; }
    matches(sel) { return sel === ':disabled' ? this.disabled : false; }
    querySelector(sel) {
      const parts = sel.split(',');
      return doc.all(this).find(e => parts.some(p => matchesSimple(e, p))) || null;
    }
    getBoundingClientRect() { return { left: 10, top: 20, width: 100, height: 30 }; }
    scrollIntoView() { this.log.push('scroll'); }
    focus() { doc.activeElement = this; this.log.push('focus'); }
    addEventListener(type, fn) { (this._listeners[type] = this._listeners[type] || []).push(fn); }
    dispatchEvent(ev) {
      ev.target = this;
      doc.events.push({ type: ev.type, el: this, ev });
      (this._listeners[ev.type] || []).forEach(fn => fn(ev));
      return !ev.defaultPrevented;
    }
  }

  // A text control with its value on the prototype, as in a browser: the page
  // script has to go through the setter for a framework to notice the write.
  class TextControl extends El {
    constructor(...a) { super(...a); this._v = this._attrs.value || ''; }
    get value() { return this._v; }
    set value(v) { this._v = String(v); doc.setterCalls++; }
  }
  class Select extends El {
    get options() { return this.childNodes.filter(c => c.tagName === 'OPTION'); }
    get multiple() { return 'multiple' in this._attrs; }
    get size() { return Number(this._attrs.size || 0); }
    get selectedIndex() { return this.options.findIndex(o => o.selected); }
    set selectedIndex(i) { this.options.forEach((o, j) => { o.selected = j === i; }); }
    get value() { const o = this.options[this.selectedIndex]; return o ? o.value : ''; }
  }
  class Option extends El {
    constructor(...a) { super(...a); this.selected = 'selected' in this._attrs; }
    get value() { return 'value' in this._attrs ? this._attrs.value : this.textContent; }
    get text() { return this.textContent; }
  }
  class Check extends El {
    constructor(...a) { super(...a); this.checked = 'checked' in this._attrs; this.indeterminate = false; }
  }
  class Form extends El {
    requestSubmit() { doc.submitted += 1; }
  }

  doc.El = El;
  doc.make = (tag, attrs = {}, ...kids) => {
    const T = tag.toLowerCase();
    const type = (attrs.type || 'text').toLowerCase();
    let Cls = El;
    if (T === 'input') Cls = (type === 'checkbox' || type === 'radio') ? Check : TextControl;
    else if (T === 'textarea') Cls = TextControl;
    else if (T === 'select') Cls = Select;
    else if (T === 'option') Cls = Option;
    else if (T === 'form') Cls = Form;
    const e = new Cls(tag, attrs, kids);
    if (attrs.style) e._style = attrs.style;
    return e;
  };
  return doc;
}

function makePage(buildBody) {
  const doc = { events: [], setterCalls: 0, submitted: 0, activeElement: null, _hit: null };
  build(doc);
  doc.documentElement = doc.make('html');
  doc.body = doc.make('body');
  doc.documentElement.add(doc.body);
  doc.all = (root = doc.documentElement) => {
    const out = [];
    (function visit(n) { for (const c of n.childNodes) if (c.nodeType === 1) { out.push(c); visit(c); } })(root);
    return out;
  };
  doc.byId = id => doc.all().find(e => e.id === id) || null;
  doc.getElementById = doc.byId;
  doc.querySelector = sel => {
    const parts = sel.split(',');
    return doc.all().find(e => parts.some(p => matchesSimple(e, p))) || null;
  };
  doc.elementFromPoint = () => doc._hit;
  doc.execCommand = () => false;
  doc.title = 'Test page';

  const win = {
    innerWidth: 1000, innerHeight: 800, scrollY: 0,
    getComputedStyle(el) {
      const own = el._style || {};
      return {
        display: own.display || (INLINE.has(el.tagName) ? 'inline' : 'block'),
        visibility: own.visibility || 'visible',
      };
    },
  };
  doc.defaultView = win;
  buildBody(doc, doc.body);
  return doc;
}

const el = (doc, tag, attrs, ...kids) => doc.make(tag, attrs, ...kids);

/** One "page": a vm context that stays alive across calls, as the extension's
 *  content-script sandbox does. */
function session(buildBody, href = 'https://example.test/page') {
  const doc = makePage(buildBody);
  const ctx = createContext({
    document: doc, window: doc.defaultView, location: { href },
    Event: Ev, MouseEvent: Ev, PointerEvent: Ev, InputEvent: Ev, KeyboardEvent: Ev,
  });
  ctx.globalThis = ctx;
  const call = (command, args = {}, allowPassword = false) => {
    const payload = JSON.stringify({ command, args, allowPassword });
    const script = TEMPLATE
      .replace('/*__CREDENTIALS__*/', CREDENTIALS)
      .replace('/*__COMMAND__*/ null', payload);
    // Through JSON, as executeScript's result is: arrays made inside the vm
    // context are not instances of this realm's Array.
    return JSON.parse(JSON.stringify(runInContext(script, ctx)));
  };
  return { doc, call };
}

// -------------------------------------------------------------------- tests

let passed = 0;
let failed = 0;
function test(name, fn) {
  try { fn(); passed++; console.log(`ok   ${name}`); }
  catch (e) { failed++; console.log(`FAIL ${name}\n     ${e && e.stack ? e.stack.split('\n').slice(0, 4).join('\n     ') : e}`); }
}

const shop = (d, body) => {
  body.add(el(d, 'main', {},
    el(d, 'h1', {}, 'Shop'),
    el(d, 'p', {}, 'Welcome ', el(d, 'b', {}, 'back')),
    el(d, 'a', { href: '/cart' }, 'Cart (2)'),
    el(d, 'form', { 'aria-label': 'Login' },
      el(d, 'label', { for: 'email' }, 'Email'),
      el(d, 'input', { id: 'email', type: 'email', placeholder: 'you@example.com' }),
      el(d, 'label', {}, el(d, 'input', { type: 'checkbox', checked: '' }), 'Remember me'),
      el(d, 'button', {}, 'Sign in'))));
};

test('snapshot prints roles, names, states and refs as a tree', () => {
  const { call } = session(shop);
  const r = call('snapshot', { maxChars: 20000 });
  assert.equal(r.ok, true);
  assert.equal(r.url, 'https://example.test/page');
  assert.equal(r.snapshot, [
    '- main [ref_1]',
    '  - heading "Shop" [ref_2] [level=1]',
    '  - text "Welcome back"',
    '  - link "Cart (2)" [ref_3] [href="/cart"]',
    '  - form "Login" [ref_4]',
    '    - textbox "Email" [ref_5] [type=email]',
    '    - checkbox "Remember me" [ref_6] [checked]',
    '    - button "Sign in" [ref_7]',
  ].join('\n'));
  assert.equal(r.truncated, false);
});

test('a label that names a control is not printed a second time', () => {
  const { call } = session(shop);
  assert.ok(!/text "Email"/.test(call('snapshot', {}).snapshot));
  assert.ok(!/text "Remember me"/.test(call('snapshot', {}).snapshot));
});

test('hidden elements are left out', () => {
  const { call } = session((d, b) => {
    b.add(el(d, 'button', { style: { display: 'none' } }, 'Gone'));
    b.add(el(d, 'button', { 'aria-hidden': 'true' }, 'Also gone'));
    b.add(el(d, 'button', { style: { visibility: 'hidden' } }, 'Invisible'));
    b.add(el(d, 'button', {}, 'Here'));
  });
  const s = call('snapshot', {}).snapshot;
  assert.ok(s.includes('"Here"'));
  assert.ok(!/Gone|Invisible/.test(s));
});

test('interactive_only is a flat list of things to act on', () => {
  const { call } = session(shop);
  const r = call('snapshot', { interactiveOnly: true });
  assert.deepEqual(r.snapshot.split('\n').map(l => l.split(' [')[0]), [
    '- link "Cart (2)"', '- textbox "Email"', '- checkbox "Remember me"', '- button "Sign in"']);
  assert.equal(r.elements, 4);
});

test('max_chars cuts the snapshot and says so', () => {
  const { call } = session((d, b) => {
    for (let i = 0; i < 50; i++) b.add(el(d, 'button', {}, `Button number ${i}`));
  });
  const r = call('snapshot', { maxChars: 500 });
  assert.equal(r.truncated, true);
  assert.ok(r.snapshot.length <= 500);
  assert.match(r.note, /max_chars/);
});

test('selector scopes the snapshot, and a bad one is an error', () => {
  const { call } = session(shop);
  assert.ok(!/Shop/.test(call('snapshot', { selector: 'form' }).snapshot));
  assert.ok(/Sign in/.test(call('snapshot', { selector: 'form' }).snapshot));
  const r = call('snapshot', { selector: 'nothing' });
  assert.equal(r.ok, false);
  assert.equal(r.code, 'bad_selector');
});

test('select prints its options and the chosen text', () => {
  const { call } = session((d, b) => {
    b.add(el(d, 'select', { 'aria-label': 'Size' },
      el(d, 'option', { value: 's' }, 'Small'),
      el(d, 'option', { value: 'm', selected: '' }, 'Medium')));
  });
  const s = call('snapshot', {}).snapshot;
  assert.match(s, /combobox "Size" \[ref_1\] \[options="Small \| Medium"\] \[value="Medium"\]/);
});

test('same-origin iframes are entered, cross-origin ones are reported', () => {
  const { call } = session((d, b) => {
    const inner = d.make('body');
    inner.add(el(d, 'button', {}, 'Inside'));
    const frame = el(d, 'iframe', { title: 'Widget' });
    frame.contentDocument = { body: inner };
    b.add(frame);
    const other = el(d, 'iframe', { title: 'Ad' });
    other.contentDocument = null;
    b.add(other);
  });
  const s = call('snapshot', {}).snapshot;
  assert.match(s, /- iframe "Widget"/);
  assert.match(s, / {2}- button "Inside"/);
  assert.match(s, /cross-origin frame/);
});

// ---- credentials

const login = (d, b) => {
  b.add(el(d, 'input', { type: 'text', 'aria-label': 'User', value: 'alice' }));
  b.add(el(d, 'input', { type: 'password', 'aria-label': 'Password', value: 'hunter2' }));
  b.add(el(d, 'input', { type: 'text', name: 'otp_code', 'aria-label': 'Code', value: '' }));
  b.add(el(d, 'div', { contenteditable: 'true', id: 'pin-field', role: 'textbox' }, 'secret-pin-91'));
  b.add(el(d, 'p', {}, 'Your pin is secret-pin-91 so keep it safe'));
};

test('credential fields are printed without their value', () => {
  const { call } = session(login);
  const s = call('snapshot', {}).snapshot;
  assert.match(s, /textbox "User" \[ref_1\] \[value="alice"\]/);
  assert.match(s, /"Password" \[ref_2\] \[type=password\] \[value="\*\*\*"\]/);
  assert.ok(!s.includes('hunter2'));
  // An empty credential field is still not read.
  assert.ok(!/"Code" \[ref_3\].*value=/.test(s));
});

test('credential text nested in the page is scrubbed from the whole snapshot', () => {
  const { call } = session(login);
  const r = call('snapshot', {});
  assert.ok(!r.snapshot.includes('secret-pin-91'));
  assert.ok(r.masked_secrets >= 1);
});

test('allow_password_typing lifts the masking, and only the server sets it', () => {
  const { call } = session(login);
  const s = call('snapshot', {}, true).snapshot;
  assert.ok(s.includes('hunter2'));
  // An agent-supplied flag inside the arguments does nothing.
  const { call: c2 } = session(login);
  assert.ok(!c2('snapshot', { allowPassword: true, allow_password: true }).snapshot.includes('hunter2'));
});

test('typing into a credential field is refused and never echoed', () => {
  const { call, doc } = session(login);
  call('snapshot', {});
  const r = call('type', { ref: 'ref_2', text: 'x' });
  assert.equal(r.ok, false);
  assert.equal(r.code, 'password_field');
  assert.ok(!JSON.stringify(r).includes('hunter2'));
  const field = doc.all().find(e => e.type === 'password');
  assert.equal(field.value, 'hunter2');
});

test('with allow_password_typing the write happens but the value is not echoed', () => {
  const { call, doc } = session(login);
  call('snapshot', {}, true);
  const r = call('type', { ref: 'ref_2', text: 'newsecret' }, true);
  assert.equal(r.ok, true);
  assert.ok(!('value' in r));
  assert.equal(doc.all().find(e => e.type === 'password').value, 'newsecret');
});

// ---- refs

test('an element keeps its ref across snapshots', () => {
  const { call } = session(shop);
  const a = call('snapshot', {}).snapshot;
  const b = call('snapshot', {}).snapshot;
  assert.equal(a, b);
});

test('a ref whose element left the page is stale and says to re-snapshot', () => {
  const { call, doc } = session(shop);
  call('snapshot', {});
  const btn = doc.all().find(e => e.tagName === 'BUTTON');
  btn.parentNode.childNodes.splice(btn.parentNode.childNodes.indexOf(btn), 1);
  btn.parentNode = null;
  const r = call('click', { ref: 'ref_7' });
  assert.equal(r.ok, false);
  assert.equal(r.code, 'stale_ref');
  assert.match(r.error, /browser_snapshot/);
});

test('a ref from before a navigation is unknown and says to re-snapshot', () => {
  const first = session(shop);
  first.call('snapshot', {});
  const next = session(shop);            // a new document: a new sandbox
  const r = next.call('click', { ref: 'ref_7' });
  assert.equal(r.ok, false);
  assert.equal(r.code, 'unknown_ref');
  assert.match(r.error, /browser_snapshot/);
});

test('a malformed ref is rejected', () => {
  const { call } = session(shop);
  for (const bad of ['', 'abc', 'ref_', 'ref_1x', '12; drop']) {
    assert.equal(call('click', { ref: bad }).code, 'bad_ref', bad);
  }
});

test('a bare number is accepted as a ref', () => {
  const { call } = session(shop);
  call('snapshot', {});
  assert.equal(call('click', { ref: '7' }).ok, true);
});

// ---- click / hover

test('click sends one click, after press events, to the ref', () => {
  const { call, doc } = session(shop);
  call('snapshot', {});
  doc.events.length = 0;
  const r = call('click', { ref: 'ref_7' });
  assert.equal(r.ok, true);
  assert.deepEqual(doc.events.map(e => e.type),
    ['pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click']);
  assert.equal(doc.events.filter(e => e.type === 'click').length, 1);
  assert.equal(r.element.role, 'button');
  assert.equal(r.element.name, 'Sign in');
  assert.equal(r.defaultPrevented, false);
});

test('click reports a page that cancelled it, and a covering element', () => {
  const { call, doc } = session(shop);
  call('snapshot', {});
  const btn = doc.all().find(e => e.tagName === 'BUTTON');
  btn.addEventListener('click', ev => ev.preventDefault());
  doc._hit = el(doc, 'div', {}, 'Cookie banner');
  const r = call('click', { ref: 'ref_7' });
  assert.equal(r.defaultPrevented, true);
  assert.match(r.covered_by, /Cookie banner/);
});

test('click refuses a disabled element', () => {
  const { call } = session((d, b) => b.add(el(d, 'button', { disabled: '' }, 'Off')));
  call('snapshot', {});
  const r = call('click', { ref: 'ref_1' });
  assert.equal(r.code, 'disabled');
});

test('right click sends contextmenu and double click sends two clicks', () => {
  const { call, doc } = session(shop);
  call('snapshot', {});
  doc.events.length = 0;
  call('click', { ref: 'ref_7', button: 'right' });
  assert.ok(doc.events.some(e => e.type === 'contextmenu'));
  assert.ok(!doc.events.some(e => e.type === 'click'));
  doc.events.length = 0;
  call('click', { ref: 'ref_7', doubleClick: true });
  assert.equal(doc.events.filter(e => e.type === 'click').length, 2);
  assert.equal(doc.events.filter(e => e.type === 'dblclick').length, 1);
});

test('hover enters the ancestors, outermost first, then the element', () => {
  const { call, doc } = session(shop);
  call('snapshot', {});
  doc.events.length = 0;
  const r = call('hover', { ref: 'ref_3' });
  assert.equal(r.ok, true);
  const enters = doc.events.filter(e => e.type === 'mouseenter').map(e => e.el.tagName);
  assert.deepEqual(enters.slice(0, 2), ['HTML', 'BODY']);
  assert.equal(enters[enters.length - 1], 'A');
  assert.ok(doc.events.some(e => e.type === 'mouseover' && e.el.tagName === 'A'));
  assert.ok(!doc.events.some(e => e.type === 'click'));
});

// ---- type

test('type replaces the value through the prototype setter and fires input and change', () => {
  const { call, doc } = session(shop);
  call('snapshot', {});
  const field = doc.byId('email');
  field._v = 'old';
  doc.events.length = 0;
  doc.setterCalls = 0;
  const r = call('type', { ref: 'ref_5', text: 'me@example.com' });
  assert.equal(r.ok, true);
  assert.equal(field.value, 'me@example.com');
  assert.equal(r.value, 'me@example.com');
  assert.equal(doc.setterCalls, 1);
  assert.deepEqual(doc.events.map(e => e.type), ['input', 'change']);
});

test('type with clear false appends', () => {
  const { call, doc } = session(shop);
  call('snapshot', {});
  doc.byId('email')._v = 'ab';
  call('type', { ref: 'ref_5', text: 'cd', clear: false });
  assert.equal(doc.byId('email').value, 'abcd');
});

test('type with submit presses Enter and submits the form unless the page took it', () => {
  const { call, doc } = session(shop);
  call('snapshot', {});
  const r = call('type', { ref: 'ref_5', text: 'x', submit: true });
  assert.equal(r.submitted, true);
  assert.equal(doc.submitted, 1);
  doc.byId('email').addEventListener('keydown', ev => ev.preventDefault());
  assert.equal(call('type', { ref: 'ref_5', text: 'y', submit: true }).submitted, false);
  assert.equal(doc.submitted, 1);
});

test('type refuses what cannot take text, and says what to do', () => {
  const { call } = session(shop);
  call('snapshot', {});
  const box = call('type', { ref: 'ref_6', text: 'x' });
  assert.equal(box.code, 'not_typeable');
  assert.match(box.error, /browser_click_ref/);
  assert.equal(call('type', { ref: 'ref_7', text: 'x' }).code, 'not_typeable');
});

test('type into a contenteditable falls back to textContent when execCommand does not take', () => {
  const { call, doc } = session((d, b) =>
    b.add(el(d, 'div', { contenteditable: 'true', 'aria-label': 'Notes' }, 'old')));
  call('snapshot', {});
  const r = call('type', { ref: 'ref_1', text: 'new notes' });
  assert.equal(r.ok, true);
  assert.equal(doc.all().find(e => e.tagName === 'DIV').textContent, 'new notes');
});

// ---- select

const sizes = (d, b) => b.add(el(d, 'select', { 'aria-label': 'Size' },
  el(d, 'option', { value: 's' }, 'Small'),
  el(d, 'option', { value: 'm' }, 'Medium'),
  el(d, 'option', { value: 'l' }, 'Large')));

test('select by value, by text (case-insensitively) and by index', () => {
  const { call, doc } = session(sizes);
  call('snapshot', {});
  assert.equal(call('select', { ref: 'ref_1', value: 'm' }).selected[0].text, 'Medium');
  assert.equal(call('select', { ref: 'ref_1', text: 'large' }).selected[0].value, 'l');
  assert.equal(call('select', { ref: 'ref_1', index: 0 }).selected[0].value, 's');
  assert.deepEqual(doc.events.slice(-2).map(e => e.type), ['input', 'change']);
});

test('select with no such option lists the options', () => {
  const { call } = session(sizes);
  call('snapshot', {});
  const r = call('select', { ref: 'ref_1', value: 'xl' });
  assert.equal(r.code, 'no_such_option');
  assert.match(r.error, /"s" \(Small\)/);
});

test('select on something that is not a native select points at the click flow', () => {
  const { call } = session(shop);
  call('snapshot', {});
  const r = call('select', { ref: 'ref_7', value: 'x' });
  assert.equal(r.code, 'not_select');
  assert.match(r.error, /browser_click_ref/);
});

test('select with values sets a multiple select', () => {
  const { call } = session((d, b) => b.add(el(d, 'select', { 'aria-label': 'Toppings', multiple: '' },
    el(d, 'option', { value: 'a' }, 'Ham'), el(d, 'option', { value: 'b' }, 'Olives'),
    el(d, 'option', { value: 'c' }, 'Basil'))));
  call('snapshot', {});
  const r = call('select', { ref: 'ref_1', values: ['a', 'Basil'] });
  assert.deepEqual(r.selected.map(s => s.value), ['a', 'c']);
});

// ---- find

test('find by text returns refs with their heading, by role filters, limit caps', () => {
  const { call } = session((d, b) => {
    b.add(el(d, 'h2', {}, 'Phones'));
    b.add(el(d, 'button', {}, 'Buy now'));
    b.add(el(d, 'h2', {}, 'Laptops'));
    b.add(el(d, 'button', {}, 'Buy now'));
    b.add(el(d, 'a', { href: '/buy' }, 'Buy guide'));
  });
  const r = call('find', { text: 'buy' });
  assert.equal(r.total_matches, 3);
  assert.deepEqual(r.matches.map(m => m.after_heading), ['Phones', 'Laptops', 'Laptops']);
  const buttons = call('find', { text: 'buy', role: 'button' });
  assert.deepEqual(buttons.matches.map(m => m.ref), ['ref_2', 'ref_4']);
  assert.equal(call('find', { role: 'heading' }).total_matches, 2);
  const capped = call('find', { text: 'buy', limit: 1 });
  assert.equal(capped.returned, 1);
  assert.equal(capped.truncated, true);
  assert.equal(call('find', { text: 'buy NOW', exact: true }).total_matches, 2);
  assert.equal(call('find', { text: 'nope' }).total_matches, 0);
});

test('find refs are the ones snapshot prints, and they work', () => {
  const { call } = session(shop);
  const ref = call('find', { text: 'sign in' }).matches[0].ref;
  assert.match(call('snapshot', {}).snapshot, new RegExp(`button "Sign in" \\[${ref}\\]`));
  assert.equal(call('click', { ref }).ok, true);
});

test('find does not return a credential field value', () => {
  const { call } = session(login);
  const r = call('find', { role: 'textbox' });
  assert.ok(!JSON.stringify(r).includes('hunter2'));
  assert.ok(!JSON.stringify(r).includes('secret-pin-91'));
});

test('an unknown command is an error, not a crash', () => {
  const { call } = session(shop);
  assert.equal(call('frobnicate', {}).code, 'unknown_command');
});

console.log(`\n${passed}/${passed + failed} passed`);
if (failed) process.exit(1);
