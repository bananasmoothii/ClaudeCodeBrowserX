// Page-side half of the accessibility snapshot and ref tools.
//
// This file is a TEMPLATE, never loaded by a browser as it stands. ax.py
// replaces the two placeholders below and hands the result to the existing
// executeScript action, the way browser_audit_page runs its fixed script. That
// is the point: Firefox runs executeScript code in the extension's content
// script sandbox, so state kept on globalThis survives between calls on the
// same page (and the page itself cannot see it), and the signed extension does
// not have to change.
//
//   the credentials marker (a block comment, inside the function below):
//       replaced by extension/credentials.js, verbatim - the one definition
//       of "this field holds a credential".
//   the command marker (a block comment followed by null, on the last line):
//       replaced by one JSON object: {command, args, allowPassword}
//
// Commands: snapshot, find, click, hover, type, select. Every command returns
// a plain JSON object: {ok: true, ...} or {ok: false, code, error}.
//
// Refs. Every element the snapshot prints gets a ref_N. The number is issued
// once per element and never reused, so the same element keeps its ref across
// snapshots. The table lives in globalThis.__ccbAx, so a navigation (a new
// document, a new sandbox) empties it and an old ref reads as "unknown"; a ref
// whose element has left the page reads as "stale". Both tell the caller to
// take a new snapshot.
//
// Credentials. A field credentials.js calls a credential is printed without
// its value ("***" when it holds something), the text of one nested in the
// page is scrubbed out of the whole snapshot, and typing into one is refused,
// unless safety.json sets allow_password_typing (allowPassword here). Only
// the server sets that flag.
(function (CMD) {
  /*__CREDENTIALS__*/

  var G = globalThis;
  var ARGS = CMD.args || {};
  var ALLOW_PASSWORD = CMD.allowPassword === true;

  var STATE = G.__ccbAx;
  if (!STATE || STATE.version !== 1) {
    // Strong references on purpose. A WeakRef to an Xray wrapper can be
    // collected while the page node is alive, which would call a live element
    // stale. Entries for detached nodes are pruned at the next snapshot.
    STATE = G.__ccbAx = { version: 1, next: 1, ids: new WeakMap(), refs: new Map() };
  }

  var MAX_VISITED = 40000;     // elements looked at in one walk
  var MAX_REFS = 20000;        // refs kept; the oldest go first
  var MAX_LINE_TEXT = 400;     // one "text" line
  var MAX_NAME = 160;
  var FIND_CAP = 100;

  var SKIP_TAGS = { SCRIPT: 1, STYLE: 1, NOSCRIPT: 1, TEMPLATE: 1, HEAD: 1,
                    META: 1, LINK: 1, TITLE: 1, BASE: 1 };

  // Roles whose accessible name is their text.
  var NAME_FROM_CONTENT = { button: 1, link: 1, tab: 1, menuitem: 1,
    menuitemcheckbox: 1, menuitemradio: 1, option: 1, treeitem: 1, heading: 1,
    columnheader: 1, rowheader: 1, checkbox: 1, radio: 1, 'switch': 1,
    clickable: 1, tooltip: 1 };

  var INTERACTIVE_ROLES = { button: 1, link: 1, textbox: 1, searchbox: 1,
    checkbox: 1, radio: 1, 'switch': 1, combobox: 1, listbox: 1, option: 1,
    menuitem: 1, menuitemcheckbox: 1, menuitemradio: 1, tab: 1, slider: 1,
    spinbutton: 1, treeitem: 1, clickable: 1 };

  // Containers that are always printed, because they say where you are.
  var ALWAYS_NODE = { navigation: 1, main: 1, banner: 1, contentinfo: 1,
    complementary: 1, search: 1, dialog: 1, alertdialog: 1, table: 1, grid: 1,
    tablist: 1, tabpanel: 1, toolbar: 1, menu: 1, menubar: 1, listbox: 1,
    tree: 1, alert: 1 };
  // Printed only when the page named them; otherwise they are just wrappers.
  var NAMED_NODE = { form: 1, region: 1, group: 1, article: 1, list: 1 };

  // Roles whose children are not walked: the node already says everything.
  var LEAF_ROLES = { button: 1, link: 1, tab: 1, menuitem: 1, menuitemcheckbox: 1,
    menuitemradio: 1, option: 1, treeitem: 1, checkbox: 1, radio: 1, 'switch': 1,
    slider: 1, spinbutton: 1, textbox: 1, searchbox: 1, combobox: 1, img: 1,
    progressbar: 1, meter: 1, clickable: 1 };

  // ---------------------------------------------------------------- helpers

  function txt(s, n) {
    s = String(s == null ? '' : s).replace(/\s+/g, ' ').trim();
    return n && s.length > n ? s.slice(0, n - 3) + '...' : s;
  }

  function attr(el, name) {
    return el && el.getAttribute ? el.getAttribute(name) : null;
  }

  function styleOf(el) {
    try {
      var view = el.ownerDocument && el.ownerDocument.defaultView;
      return (view || window).getComputedStyle(el);
    } catch (e) { return null; }
  }

  function tagOf(el) { return String(el.tagName || '').toUpperCase(); }

  function skipForText(n) {
    if (n.nodeType !== 1) return false;
    if (SKIP_TAGS[tagOf(n)]) return true;
    if (attr(n, 'aria-hidden') === 'true') return true;
    var st = styleOf(n);
    return !!(st && (st.display === 'none' || st.visibility === 'hidden'));
  }

  function childNodesOf(el) {
    var sr = el.openOrClosedShadowRoot || el.shadowRoot;
    if (sr) return Array.prototype.slice.call(sr.childNodes);
    if (tagOf(el) === 'SLOT' && typeof el.assignedNodes === 'function') {
      var assigned = el.assignedNodes({ flatten: true });
      if (assigned.length) return Array.prototype.slice.call(assigned);
    }
    return Array.prototype.slice.call(el.childNodes);
  }

  // The visible text under el. Form controls contribute nothing (a value is
  // not text, and a credential must not leak through a label), images
  // contribute their alt text.
  function textOf(el, limit, includeHidden) {
    var out = [];
    var size = 0;
    function put(s) { out.push(s); size += s.length; }
    (function visit(n) {
      if (size > (limit || 300) * 3) return;
      if (n.nodeType === 3) { put(n.nodeValue || ''); return; }
      if (n.nodeType !== 1) return;
      if (!includeHidden && skipForText(n)) return;
      var tag = tagOf(n);
      if (SKIP_TAGS[tag]) return;
      if (tag === 'IMG') { put(attr(n, 'alt') || ''); return; }
      if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return;
      var st = includeHidden ? null : styleOf(n);
      var block = !!(st && !/^(inline|contents)/.test(st.display));
      if (block) put(' ');
      var kids = childNodesOf(n);
      for (var i = 0; i < kids.length; i++) visit(kids[i]);
      if (block) put(' ');
    })(el);
    return txt(out.join(''), limit || MAX_NAME);
  }

  function hasInteractiveDescendant(el) {
    try {
      return !!el.querySelector(
        'a[href],button,input:not([type=hidden]),select,textarea,' +
        '[role=button],[role=link],[tabindex]');
    } catch (e) { return false; }
  }

  // -------------------------------------------------------------- roles

  function implicitRole(el) {
    var tag = tagOf(el);
    switch (tag) {
      case 'A': case 'AREA': return el.hasAttribute('href') ? 'link' : null;
      case 'BUTTON': case 'SUMMARY': return 'button';
      case 'INPUT': {
        var ty = (attr(el, 'type') || 'text').toLowerCase();
        switch (ty) {
          case 'hidden': return null;
          case 'button': case 'submit': case 'reset': case 'image': case 'file':
            return 'button';
          case 'checkbox': return 'checkbox';
          case 'radio': return 'radio';
          case 'range': return 'slider';
          case 'number': return 'spinbutton';
          case 'search': return el.hasAttribute('list') ? 'combobox' : 'searchbox';
          default: return el.hasAttribute('list') ? 'combobox' : 'textbox';
        }
      }
      case 'SELECT': return (el.multiple || el.size > 1) ? 'listbox' : 'combobox';
      case 'TEXTAREA': return 'textbox';
      case 'OPTION': return 'option';
      case 'H1': case 'H2': case 'H3': case 'H4': case 'H5': case 'H6': return 'heading';
      case 'IMG': return attr(el, 'alt') === '' ? null : 'img';
      case 'SVG': return 'img';
      case 'NAV': return 'navigation';
      case 'MAIN': return 'main';
      case 'ASIDE': return 'complementary';
      case 'HEADER': return tagOf(el.parentNode || {}) === 'BODY' ? 'banner' : null;
      case 'FOOTER': return tagOf(el.parentNode || {}) === 'BODY' ? 'contentinfo' : null;
      case 'FORM': return 'form';
      case 'SECTION': return 'region';
      case 'ARTICLE': return 'article';
      case 'UL': case 'OL': return 'list';
      case 'LI': return 'listitem';
      case 'TABLE': return 'table';
      case 'TR': return 'row';
      case 'TH': return 'columnheader';
      case 'TD': return 'cell';
      case 'DIALOG': return 'dialog';
      case 'PROGRESS': return 'progressbar';
      case 'METER': return 'meter';
      case 'FIELDSET': case 'DETAILS': return 'group';
      case 'IFRAME': case 'FRAME': return 'iframe';
      case 'VIDEO': return 'video';
      case 'AUDIO': return 'audio';
    }
    if (el.hasAttribute && el.hasAttribute('contenteditable') &&
        (attr(el, 'contenteditable') || '').toLowerCase() !== 'false') {
      return 'textbox';
    }
    return null;
  }

  function roleOf(el) {
    var explicit = (attr(el, 'role') || '').trim().toLowerCase().split(/\s+/)[0];
    if (explicit === 'presentation' || explicit === 'none') return null;
    if (explicit) return explicit;
    return implicitRole(el);
  }

  function isInteractive(el, role) {
    if (role && INTERACTIVE_ROLES[role]) return true;
    var ti = attr(el, 'tabindex');
    if (ti !== null && ti !== '' && Number(ti) >= 0 && tagOf(el) !== 'BODY') return true;
    if (el.hasAttribute && el.hasAttribute('onclick')) return true;
    return false;
  }

  // ------------------------------------------------------------- names

  function ownNameOnly(el) {
    var by = attr(el, 'aria-labelledby');
    if (by) {
      var parts = [];
      by.split(/\s+/).forEach(function (id) {
        var n = el.ownerDocument.getElementById(id);
        if (n) parts.push(textOf(n, 200, true));
      });
      var joined = txt(parts.join(' '), MAX_NAME);
      if (joined) return joined;
    }
    return txt(attr(el, 'aria-label'), MAX_NAME);
  }

  function nameOf(el, role) {
    var s = ownNameOnly(el);
    if (s) return s;
    var tag = tagOf(el);
    if (tag === 'INPUT' || tag === 'SELECT' || tag === 'TEXTAREA' ||
        tag === 'BUTTON' || tag === 'METER' || tag === 'PROGRESS') {
      var ty = (attr(el, 'type') || '').toLowerCase();
      if (tag === 'INPUT' && ty === 'image') {
        s = txt(attr(el, 'alt'), MAX_NAME); if (s) return s;
      }
      var labelled = el.labels && el.labels.length
        ? Array.prototype.map.call(el.labels, function (l) { return textOf(l, 200); }).join(' ')
        : '';
      s = txt(labelled, MAX_NAME); if (s) return s;
      if (tag === 'INPUT' && (ty === 'button' || ty === 'submit' || ty === 'reset')) {
        // A button's value is its label, not user-entered text.
        s = txt(el.value || attr(el, 'value') || (ty === 'submit' ? 'Submit' : ty === 'reset' ? 'Reset' : ''), MAX_NAME);
        if (s) return s;
      }
      s = txt(attr(el, 'placeholder'), MAX_NAME); if (s) return s;
    }
    if (tag === 'IMG') { s = txt(attr(el, 'alt'), MAX_NAME); if (s) return s; }
    if (tag === 'SVG') {
      var t = el.querySelector && el.querySelector('title');
      s = t ? txt(t.textContent, MAX_NAME) : ''; if (s) return s;
    }
    if (tag === 'FIELDSET') {
      var lg = el.querySelector && el.querySelector('legend');
      s = lg ? textOf(lg, 200) : ''; if (s) return s;
    }
    if (tag === 'TABLE') {
      var cap = el.querySelector && el.querySelector('caption');
      s = cap ? textOf(cap, 200) : ''; if (s) return s;
    }
    if (tag === 'DETAILS') {
      var sm = el.querySelector && el.querySelector('summary');
      s = sm ? textOf(sm, 200) : ''; if (s) return s;
    }
    if (role && NAME_FROM_CONTENT[role]) {
      s = textOf(el, MAX_NAME); if (s) return s;
    }
    return txt(attr(el, 'title'), MAX_NAME);
  }

  function hasOwnName(el) {
    return !!(attr(el, 'aria-label') || attr(el, 'aria-labelledby') ||
              (tagOf(el) === 'FIELDSET' && el.querySelector('legend')));
  }

  // ------------------------------------------------------ values, states

  function isCredential(el) {
    if (ALLOW_PASSWORD) return false;
    try { return isPasswordField(el); } catch (e) { return true; }
  }

  function valueOf(el, role) {
    var tag = tagOf(el);
    var textual = role === 'textbox' || role === 'searchbox' || role === 'combobox' ||
                  role === 'spinbutton' || role === 'slider' || role === 'listbox';
    if (!textual) return null;
    var isField = tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT';
    if (!isField && !(el.isContentEditable === true)) {
      var now = attr(el, 'aria-valuenow') || attr(el, 'aria-valuetext');
      return now == null ? null : txt(now, 200);
    }
    if (isCredential(el)) return holdsEnteredText(el) ? '***' : '';
    if (tag === 'SELECT') {
      var picked = Array.prototype.filter.call(el.options || [], function (o) { return o.selected; })
        .map(function (o) { return txt(o.text, 80); });
      return picked.join(', ');
    }
    if (tag === 'INPUT' || tag === 'TEXTAREA') return txt(el.value, 200);
    return textOf(el, 200);
  }

  function flagsOf(el, role) {
    var out = [];
    var tag = tagOf(el);
    if (role === 'heading') {
      out.push('level=' + (/^H[1-6]$/.test(tag) ? tag.charAt(1) : (attr(el, 'aria-level') || '2')));
    }
    if (role === 'link') {
      var href = attr(el, 'href');
      if (href) out.push('href=' + JSON.stringify(txt(href, 120)));
    }
    if (role === 'checkbox' || role === 'radio' || role === 'switch' ||
        role === 'menuitemcheckbox' || role === 'menuitemradio') {
      var c = attr(el, 'aria-checked');
      if (tag === 'INPUT') c = el.indeterminate ? 'mixed' : String(!!el.checked);
      out.push(c === 'true' ? 'checked' : c === 'mixed' ? 'mixed' : 'unchecked');
    }
    var ex = attr(el, 'aria-expanded');
    if (tag === 'SUMMARY' && el.parentNode && tagOf(el.parentNode) === 'DETAILS') {
      ex = el.parentNode.open ? 'true' : 'false';
    }
    if (ex === 'true') out.push('expanded'); else if (ex === 'false') out.push('collapsed');
    if (attr(el, 'aria-selected') === 'true' || (tag === 'OPTION' && el.selected)) out.push('selected');
    if (attr(el, 'aria-pressed') === 'true') out.push('pressed');
    var cur = attr(el, 'aria-current');
    if (cur && cur !== 'false') out.push('current');
    var disabled = el.disabled === true || attr(el, 'aria-disabled') === 'true';
    if (!disabled) { try { disabled = !!(el.matches && el.matches(':disabled')); } catch (e) {} }
    if (disabled) out.push('disabled');
    if (el.required === true || attr(el, 'aria-required') === 'true') out.push('required');
    if (el.readOnly === true) out.push('readonly');
    if (attr(el, 'aria-invalid') === 'true') out.push('invalid');
    if (el.ownerDocument && el.ownerDocument.activeElement === el) out.push('focused');
    if (tag === 'INPUT' && (role === 'textbox' || role === 'searchbox')) {
      var ty = (attr(el, 'type') || 'text').toLowerCase();
      if (ty !== 'text' && ty !== 'search') out.push('type=' + ty);
    }
    if (tag === 'SELECT' && !isCredential(el)) {
      var opts = Array.prototype.slice.call(el.options || [], 0, 12)
        .map(function (o) { return txt(o.text, 40); });
      if (opts.length) {
        out.push('options=' + JSON.stringify(opts.join(' | ') +
          ((el.options || []).length > 12 ? ' | ...(' + el.options.length + ')' : '')));
      }
    }
    return out;
  }

  // -------------------------------------------------------------- refs

  function refFor(el) {
    var num = STATE.ids.get(el);
    if (!num) {
      num = STATE.next++;
      STATE.ids.set(el, num);
    }
    STATE.refs.set('ref_' + num, el);
    return num;
  }

  function pruneRefs() {
    STATE.refs.forEach(function (el, key) {
      var gone = false;
      try { gone = !el.isConnected; } catch (e) { gone = true; }
      if (gone) STATE.refs.delete(key);
    });
    if (STATE.refs.size > MAX_REFS) {
      var drop = STATE.refs.size - MAX_REFS;
      STATE.refs.forEach(function (el, key) {
        if (drop-- > 0) STATE.refs.delete(key);
      });
    }
  }

  function labelsAControl(label) {
    try { if (label.control) return true; } catch (e) {}
    return !!(label.hasAttribute && label.hasAttribute('for'));
  }

  // ------------------------------------------------------------- walk

  function runWalk(options) {
    var S = {
      lines: [], entries: [], used: 0, truncated: false, stop: false,
      visited: 0, pending: '', heading: null,
      interactiveOnly: options.interactiveOnly === true,
      maxChars: options.maxChars || 0,
      collect: options.collect === true
    };

    function addLine(depth, line) {
      if (S.collect) return;
      var full = new Array(depth + 1).join('  ') + line;
      if (S.maxChars && S.used + full.length + 1 > S.maxChars) {
        S.truncated = true; S.stop = true; return;
      }
      S.lines.push(full);
      S.used += full.length + 1;
    }

    function flush(depth) {
      if (S.interactiveOnly || S.collect) { S.pending = ''; return; }
      var t = txt(S.pending, MAX_LINE_TEXT);
      S.pending = '';
      if (t) addLine(depth, '- text ' + JSON.stringify(t));
    }

    function emit(el, role, depth, interactive) {
      var name = nameOf(el, role);
      var num = refFor(el);
      var flags = flagsOf(el, role);
      var value = valueOf(el, role);
      var line = '- ' + role + (name ? ' ' + JSON.stringify(name) : '') +
                 ' [ref_' + num + ']';
      var shown = flags.slice();
      if (value !== null && value !== undefined && (value !== '' || role !== 'textbox')) {
        shown.push('value=' + JSON.stringify(value));
      }
      shown.forEach(function (f) { line += ' [' + f + ']'; });
      S.entries.push({ ref: 'ref_' + num, role: role, name: name, flags: shown,
                       interactive: interactive, heading: S.heading, line: line });
      if (role === 'heading' && name) S.heading = name;
      if (!S.interactiveOnly || interactive) addLine(S.interactiveOnly ? 0 : depth, line);
    }

    function walk(node, depth, parentHidden, inLabel) {
      if (S.stop) return;
      if (++S.visited > MAX_VISITED) { S.stop = true; S.truncated = true; return; }
      if (node.nodeType === 3) {
        if (!S.interactiveOnly && !S.collect && !parentHidden && !inLabel) S.pending += node.nodeValue || '';
        return;
      }
      if (node.nodeType !== 1) return;
      var el = node;
      var tag = tagOf(el);
      if (SKIP_TAGS[tag]) return;
      if (attr(el, 'aria-hidden') === 'true' || el.inert === true) return;
      var st = styleOf(el);
      if (st && st.display === 'none') return;
      var hidden = st ? st.visibility !== 'visible' : false;
      var block = st ? !/^(inline|contents)/.test(st.display) : false;

      var role = roleOf(el);
      var interactive = isInteractive(el, role);
      if (interactive && !role) role = 'clickable';

      // <svg> is a leaf: an icon is one image or nothing.
      if (tag === 'SVG') {
        if (!hidden && (hasOwnName(el) || (el.querySelector && el.querySelector('title')))) {
          flush(depth); emit(el, 'img', depth, false);
        }
        return;
      }

      // A frame is printed (unless only interactive elements were asked for)
      // and entered when it is same-origin.
      if (role === 'iframe' && !hidden) {
        var inner = null;
        try { inner = el.contentDocument; } catch (e) { inner = null; }
        var readable = !!(inner && inner.body);
        if (!S.interactiveOnly) {
          flush(depth);
          emit(el, role, depth, false);
          if (!readable) addLine(depth + 1, '- (cross-origin frame, not readable)');
        }
        if (readable) {
          walk(inner.body, S.interactiveOnly ? depth : depth + 1, false, false);
          flush(depth + 1);
        }
        return;
      }

      var printable = !hidden && role && (
        interactive || ALWAYS_NODE[role] || role === 'heading' ||
        (role === 'img' && nameOf(el, role)) ||
        (NAMED_NODE[role] && hasOwnName(el)));
      // Interactive-only output is a flat list of things to act on.
      if (S.interactiveOnly && !interactive) printable = false;

      if (printable) {
        flush(depth);
        emit(el, role, depth, interactive);
        // A credential field is printed as a field and never descended into.
        var leaf = LEAF_ROLES[role] ||
          (role === 'heading' && !hasInteractiveDescendant(el)) ||
          isCredential(el);
        if (leaf) return;
        var kids = childNodesOf(el);
        for (var i = 0; i < kids.length; i++) walk(kids[i], depth + 1, hidden, inLabel);
        flush(depth + 1);
        return;
      }

      // Transparent wrapper: its children continue the current text run. The
      // text of a <label> that names a control is that control's name, so it
      // is not printed a second time (a control inside it still is).
      var labelled = tag === 'LABEL' && labelsAControl(el);
      if (block) flush(depth);
      var kids2 = childNodesOf(el);
      for (var j = 0; j < kids2.length; j++) walk(kids2[j], depth, hidden, inLabel || labelled);
      if (block) flush(depth);
    }

    var root = options.root;
    walk(root, 0, false, false);
    flush(0);
    S.rootNode = root;
    return S;
  }

  function scopeRoot() {
    if (ARGS.selector) {
      var found = null;
      try { found = document.querySelector(String(ARGS.selector)); }
      catch (e) { return { error: 'Invalid selector: ' + e.message }; }
      if (!found) return { error: 'No element matches selector ' + JSON.stringify(String(ARGS.selector)) + '.' };
      return { root: found };
    }
    return { root: document.body || document.documentElement };
  }

  // Credential text nested in the page (a contenteditable PIN, a textarea)
  // is scrubbed from everything handed back, with credentials.js's own scrub.
  function scrub(text, root) {
    if (ALLOW_PASSWORD || !text) return { text: text, masked: 0 };
    try {
      var r = scrubNestedCredentials(text, root);
      return { text: r.text, masked: r.masked };
    } catch (e) { return { text: text, masked: 0 }; }
  }

  function pageFacts() {
    return { url: location.href, title: document.title };
  }

  function fail(code, error, extra) {
    var out = { ok: false, code: code, error: error };
    if (extra) for (var k in extra) out[k] = extra[k];
    return out;
  }

  // --------------------------------------------------------- commands

  function cmdSnapshot() {
    var scope = scopeRoot();
    if (scope.error) return fail('bad_selector', scope.error);
    pruneRefs();
    var S = runWalk({ root: scope.root, interactiveOnly: ARGS.interactiveOnly === true,
                      maxChars: ARGS.maxChars });
    var body = scrub(S.lines.join('\n'), scope.root);
    var out = pageFacts();
    out.ok = true;
    out.snapshot = body.text;
    out.interactive_only = S.interactiveOnly;
    out.elements = S.entries.filter(function (e) { return !S.interactiveOnly || e.interactive; }).length;
    out.truncated = S.truncated;
    out.viewport = { width: window.innerWidth, height: window.innerHeight,
                     scrollY: Math.round(window.scrollY || 0) };
    if (body.masked) out.masked_secrets = body.masked;
    if (S.truncated) {
      out.note = 'Output was cut at max_chars. Raise max_chars, pass interactive_only, or ' +
                 'scope it with selector. Elements after the cut have no ref yet.';
    }
    return out;
  }

  function cmdFind() {
    var scope = scopeRoot();
    if (scope.error) return fail('bad_selector', scope.error);
    pruneRefs();
    var S = runWalk({ root: scope.root, collect: true });
    var text = ARGS.text == null ? '' : String(ARGS.text).toLowerCase().trim();
    var role = ARGS.role == null ? '' : String(ARGS.role).toLowerCase().trim();
    var exact = ARGS.exact === true;
    var limit = Math.max(1, Math.min(FIND_CAP, Math.floor(Number(ARGS.limit) || 20)));
    var interactiveOnly = ARGS.interactiveOnly === true;

    var hits = S.entries.filter(function (e) {
      if (interactiveOnly && !e.interactive) return false;
      if (role && e.role !== role) return false;
      if (text) {
        var hay = (e.name + ' ' + e.flags.join(' ')).toLowerCase();
        if (exact ? e.name.toLowerCase() !== text : hay.indexOf(text) < 0) return false;
      }
      return true;
    });
    function describe(e) {
      return { ref: e.ref, role: e.role, name: e.name, attributes: e.flags,
               after_heading: e.heading };
    }
    // One scrub over everything in a list, not one per match. Returns how many
    // secrets it masked.
    var glue = '\u0001';
    function scrubAll(list) {
      var joined = scrub(list.map(function (m) {
        return [m.name, m.attributes.join(glue), m.after_heading || ''].join(glue + glue);
      }).join(glue + glue + glue), scope.root);
      if (joined.masked) {
        var rows = joined.text.split(glue + glue + glue);
        list.forEach(function (m, i) {
          var bits = (rows[i] || '').split(glue + glue);
          m.name = bits[0] || '';
          m.attributes = (bits[1] || '').split(glue).filter(Boolean);
          m.after_heading = bits[2] || null;
        });
      }
      return joined.masked || 0;
    }

    var shown = hits.slice(0, limit).map(describe);
    var masked = scrubAll(shown);
    var out = pageFacts();
    out.ok = true;
    out.matches = shown;
    out.total_matches = hits.length;
    out.returned = shown.length;
    out.truncated = hits.length > shown.length || S.truncated;

    // exact compares the whole accessible name, and a name is often longer than
    // the label you can see (a Drive row reads "Ensimag Dossier de couleur ...
    // Plus d'infos"). Zero matches is then a miss, not an answer: offer what a
    // substring search finds. They stay out of "matches" so exact still means
    // exact.
    if (exact && text && hits.length === 0) {
      var near = S.entries.filter(function (e) {
        if (interactiveOnly && !e.interactive) return false;
        if (role && e.role !== role) return false;
        return (e.name + ' ' + e.flags.join(' ')).toLowerCase().indexOf(text) >= 0;
      });
      if (near.length) {
        var nearShown = near.slice(0, 5).map(describe);
        masked += scrubAll(nearShown);
        out.near_matches = nearShown;
        out.total_near_matches = near.length;
        out.note = 'No element has exactly that name, but ' + near.length +
          (near.length === 1 ? ' contains' : ' contain') + ' it (near_matches). A name is ' +
          'often longer than the visible label; retry without exact to match part of it.';
      }
    }
    if (masked) out.masked_secrets = masked;
    return out;
  }

  function resolveRef() {
    var raw = String(ARGS.ref == null ? '' : ARGS.ref).trim();
    var m = /^(?:ref_)?(\d+)$/.exec(raw);
    if (!m) {
      return { error: fail('bad_ref', 'Not a ref: ' + JSON.stringify(raw.slice(0, 40)) +
        '. Refs look like ref_12 and come from browser_snapshot or browser_find.') };
    }
    var key = 'ref_' + m[1];
    var el = STATE.refs.get(key);
    if (!el) {
      if (Number(m[1]) >= STATE.next) {
        return { error: fail('unknown_ref', key + ' was never issued for this page. The page has ' +
          'navigated or reloaded since that snapshot, or the ref came from another tab. ' +
          'Take a new snapshot with browser_snapshot, then use the refs it returns.') };
      }
      return { error: fail('stale_ref', key + ' is stale: its element has been removed from the ' +
        'page (it re-rendered or was replaced). Take a new snapshot with browser_snapshot ' +
        'and use the fresh ref.') };
    }
    var live = false;
    try { live = el.isConnected; } catch (e) { live = false; }
    if (!live) {
      STATE.refs.delete(key);
      return { error: fail('stale_ref', key + ' is stale: its element has been removed from the ' +
        'page (it re-rendered or was replaced). Take a new snapshot with browser_snapshot ' +
        'and use the fresh ref.') };
    }
    return { el: el, key: key };
  }

  function elementInfo(el, key) {
    var role = roleOf(el) || (isInteractive(el, null) ? 'clickable' : tagOf(el).toLowerCase());
    var name = isCredential(el) ? '' : nameOf(el, role);
    return { ref: key, role: role, name: name };
  }

  function centre(el) {
    var r = el.getBoundingClientRect();
    return { x: r.left + r.width / 2, y: r.top + r.height / 2, w: r.width, h: r.height };
  }

  function bring(el) {
    try { el.scrollIntoView({ block: 'center', inline: 'center' }); } catch (e) {}
  }

  function mouse(el, type, p, extra) {
    var view = el.ownerDocument.defaultView || window;
    var init = { bubbles: true, cancelable: true, view: view, clientX: p.x, clientY: p.y,
                 button: 0, buttons: 0, detail: 0 };
    for (var k in extra) init[k] = extra[k];
    var ev;
    if (/^pointer/.test(type)) {
      init.pointerId = 1; init.pointerType = 'mouse'; init.isPrimary = true;
      ev = new PointerEvent(type, init);
    } else {
      ev = new MouseEvent(type, init);
    }
    return el.dispatchEvent(ev);
  }

  function cmdClick(found) {
    var el = found.el;
    var tag = tagOf(el);
    if (el.disabled === true || attr(el, 'aria-disabled') === 'true') {
      return fail('disabled', found.key + ' is disabled; clicking it would do nothing.',
                  { element: elementInfo(el, found.key) });
    }
    if (tag === 'OPTION') {
      return fail('use_select', found.key + ' is an <option> of a native <select>. Use ' +
        'browser_select_ref on the select element instead.');
    }
    bring(el);
    var p = centre(el);
    var out = { ok: true, element: elementInfo(el, found.key) };
    var top = null;
    try { top = el.ownerDocument.elementFromPoint(p.x, p.y); } catch (e) {}
    if (top && top !== el && !el.contains(top) && !(top.contains && top.contains(el)) &&
        el.getRootNode() === el.ownerDocument) {
      var tn = txt(top.textContent, 60);
      out.covered_by = tagOf(top).toLowerCase() + (tn ? ' ' + JSON.stringify(tn) : '');
      out.warning = 'Another element sits on top of this one at its centre, so a real click ' +
        'would land there. The click was still delivered to the ref.';
    }
    try { if (typeof el.focus === 'function') el.focus({ preventScroll: true }); } catch (e) {}

    var right = String(ARGS.button || 'left').toLowerCase() === 'right';
    var dbl = ARGS.doubleClick === true;
    var held = right ? 2 : 1;
    function press(detail) {
      var b = right ? 2 : 0;
      mouse(el, 'pointerdown', p, { button: b, buttons: held });
      mouse(el, 'mousedown', p, { button: b, buttons: held, detail: detail });
      mouse(el, 'pointerup', p, { button: b });
      mouse(el, 'mouseup', p, { button: b, detail: detail });
    }
    if (right) {
      press(1);
      out.defaultPrevented = !mouse(el, 'contextmenu', p, { button: 2 });
    } else {
      press(1);
      out.defaultPrevented = !mouse(el, 'click', p, { detail: 1 });
      if (dbl) {
        press(2);
        mouse(el, 'click', p, { detail: 2 });
        mouse(el, 'dblclick', p, { detail: 2 });
      }
    }
    if (tag === 'SELECT') {
      out.note = 'A native <select> cannot be opened by script. Use browser_select_ref to choose an option.';
    }
    out.url = location.href;
    out.title = document.title;
    out.note = (out.note ? out.note + ' ' : '') +
      'url/title are from just after the click; if it navigated or re-rendered, call ' +
      'browser_snapshot again before using any ref.';
    return out;
  }

  function cmdHover(found) {
    var el = found.el;
    bring(el);
    var p = centre(el);
    var chain = [];
    for (var n = el; n && n.nodeType === 1; n = n.parentNode) chain.unshift(n);
    // A real pointer enters every ancestor on the way in; enter events do not
    // bubble, so each one is sent to its own element, outermost first.
    chain.forEach(function (a) {
      mouse(a, 'pointerenter', p, { bubbles: false, cancelable: false });
      mouse(a, 'mouseenter', p, { bubbles: false, cancelable: false });
    });
    mouse(el, 'pointerover', p, { cancelable: false });
    mouse(el, 'mouseover', p, { cancelable: false });
    mouse(el, 'pointermove', p, { cancelable: false });
    mouse(el, 'mousemove', p, { cancelable: false });
    return { ok: true, element: elementInfo(el, found.key) };
  }

  var NOT_TYPEABLE_INPUT = { checkbox: 1, radio: 1, button: 1, submit: 1, reset: 1,
                             image: 1, file: 1, hidden: 1 };

  function cmdType(found) {
    var el = found.el;
    var tag = tagOf(el);
    var ty = (attr(el, 'type') || 'text').toLowerCase();
    var editHost = el.isContentEditable === true;
    var typeable = ((tag === 'INPUT' && !NOT_TYPEABLE_INPUT[ty]) || tag === 'TEXTAREA' || editHost);
    if (!typeable) {
      return fail('not_typeable', found.key + ' is <' + tag.toLowerCase() + (tag === 'INPUT' ? ' type=' + ty : '') +
        '>, which cannot receive typed text. Nothing was typed.' +
        (tag === 'INPUT' && (ty === 'checkbox' || ty === 'radio')
          ? ' Use browser_click_ref to toggle it.' : '') +
        ' Use the ref of a textbox, searchbox or contenteditable from the snapshot.',
        { element: elementInfo(el, found.key) });
    }
    if (!ALLOW_PASSWORD && isCredential(el)) {
      return fail('password_field', "Refused: target is a password field. Use the browser's own " +
        'password manager (autofill) for credentials, or set "allow_password_typing": true in ' +
        '~/.claudecodebrowserx/safety.json if you really want automated password entry.');
    }
    if (el.disabled === true) return fail('disabled', found.key + ' is disabled.');
    if (el.readOnly === true) return fail('readonly', found.key + ' is read-only.');

    var text = String(ARGS.text == null ? '' : ARGS.text);
    var clear = ARGS.clear !== false;
    bring(el);
    try { el.focus({ preventScroll: true }); } catch (e) {}
    var d = el.ownerDocument;
    var view = d.defaultView || window;

    if (editHost && tag !== 'INPUT' && tag !== 'TEXTAREA') {
      var done = false;
      try {
        if (clear) d.execCommand('selectAll', false, null);
        done = d.execCommand('insertText', false, text);
      } catch (e) { done = false; }
      if (!done) {
        el.textContent = clear ? text : (el.textContent || '') + text;
        el.dispatchEvent(new Event('input', { bubbles: true }));
      }
    } else {
      var next = clear ? text : String(el.value || '') + text;
      // The prototype's own setter, not the instance property: a framework
      // (React) that wraps .value on the instance only notices a write that
      // goes round it.
      var proto = Object.getPrototypeOf(el);
      var desc = proto && Object.getOwnPropertyDescriptor(proto, 'value');
      if (desc && desc.set) desc.set.call(el, next); else el.value = next;
      el.dispatchEvent(new InputEvent('input', { bubbles: true, inputType: 'insertText', data: text }));
      el.dispatchEvent(new Event('change', { bubbles: true }));
    }

    var out = { ok: true, element: elementInfo(el, found.key), typed_chars: text.length };
    var now = editHost && tag !== 'INPUT' && tag !== 'TEXTAREA' ? (el.textContent || '') : String(el.value);
    // Never echo what a credential field holds, even when typing into one is
    // allowed.
    var credential = true;
    try { credential = isPasswordField(el); } catch (e) { credential = true; }
    if (!credential) out.value = txt(now, 200);
    if (clear && now !== text) {
      out.normalised = true;
      out.note = 'The field holds something other than what was typed (an input mask, or the ' +
                 'browser sanitising the value).';
    }
    if (ARGS.submit === true) {
      var init = { key: 'Enter', code: 'Enter', bubbles: true, cancelable: true, view: view };
      var go = el.dispatchEvent(new KeyboardEvent('keydown', init));
      el.dispatchEvent(new KeyboardEvent('keypress', init));
      el.dispatchEvent(new KeyboardEvent('keyup', init));
      // Synthetic key events have no default action, so Enter's is done here,
      // unless the page's own keydown handler took it.
      if (go && el.form) {
        if (typeof el.form.requestSubmit === 'function') el.form.requestSubmit();
        else el.form.submit();
        out.submitted = true;
      } else {
        out.submitted = false;
      }
    }
    return out;
  }

  function cmdSelect(found) {
    var el = found.el;
    var tag = tagOf(el);
    if (tag !== 'SELECT') {
      return fail('not_select', found.key + ' is <' + tag.toLowerCase() + '>, not a native <select>. ' +
        'For a custom dropdown (role combobox or listbox), open it with browser_click_ref, take a ' +
        'new snapshot, and click the option by ref.', { element: elementInfo(el, found.key) });
    }
    if (el.disabled === true) return fail('disabled', found.key + ' is disabled.');
    var list = Array.prototype.slice.call(el.options || []);
    function listing() {
      return list.slice(0, 30).map(function (o) { return JSON.stringify(o.value) + ' (' + txt(o.text, 40) + ')'; }).join(', ') +
        (list.length > 30 ? ', ...' : '');
    }
    function indexOfWanted(want, byText) {
      var s = String(want);
      var i = list.findIndex(function (o) { return byText ? o.text === s : o.value === s; });
      if (i < 0 && byText) {
        var low = txt(s).toLowerCase();
        i = list.findIndex(function (o) { return txt(o.text).toLowerCase() === low; });
      }
      return i;
    }
    var wants = [];
    if (Array.isArray(ARGS.values)) {
      if (!el.multiple) {
        return fail('not_multiple', found.key + ' is a single-choice <select>; pass value, text or index.');
      }
      ARGS.values.forEach(function (v) {
        var i = indexOfWanted(v, false);
        if (i < 0) i = indexOfWanted(v, true);
        wants.push({ asked: v, index: i });
      });
    } else if (ARGS.value !== undefined && ARGS.value !== null) {
      var iv = indexOfWanted(ARGS.value, false);
      if (iv < 0) iv = indexOfWanted(ARGS.value, true);
      wants.push({ asked: ARGS.value, index: iv });
    } else if (ARGS.text !== undefined && ARGS.text !== null) {
      wants.push({ asked: ARGS.text, index: indexOfWanted(ARGS.text, true) });
    } else if (ARGS.index !== undefined && ARGS.index !== null) {
      var ix = Number(ARGS.index);
      wants.push({ asked: ARGS.index, index: Number.isInteger(ix) && ix >= 0 && ix < list.length ? ix : -1 });
    } else {
      return fail('nothing_to_select', 'Pass one of value, text, index (or values for a multiple select).');
    }
    for (var w = 0; w < wants.length; w++) {
      if (wants[w].index < 0) {
        return fail('no_such_option', 'No option matching ' + JSON.stringify(wants[w].asked) +
          '. Available (value (text)): ' + listing(), { element: elementInfo(el, found.key) });
      }
    }
    bring(el);
    try { el.focus({ preventScroll: true }); } catch (e) {}
    if (el.multiple) {
      var chosen = {};
      wants.forEach(function (x) { chosen[x.index] = true; });
      list.forEach(function (o, i) { o.selected = !!chosen[i]; });
    } else {
      el.selectedIndex = wants[0].index;
    }
    el.dispatchEvent(new Event('input', { bubbles: true }));
    el.dispatchEvent(new Event('change', { bubbles: true }));
    var picked = list.filter(function (o) { return o.selected; });
    return { ok: true, element: elementInfo(el, found.key),
             selected: picked.map(function (o) { return { value: o.value, text: txt(o.text, 80), index: list.indexOf(o) }; }) };
  }

  // ------------------------------------------------------------ entry

  try {
    switch (CMD.command) {
      case 'snapshot': return cmdSnapshot();
      case 'find': return cmdFind();
      case 'click': case 'hover': case 'type': case 'select': {
        var found = resolveRef();
        if (found.error) return found.error;
        if (CMD.command === 'click') return cmdClick(found);
        if (CMD.command === 'hover') return cmdHover(found);
        if (CMD.command === 'type') return cmdType(found);
        return cmdSelect(found);
      }
      default:
        return fail('unknown_command', 'Unknown command ' + JSON.stringify(String(CMD.command)));
    }
  } catch (e) {
    return fail('script_error', 'Page script failed: ' + (e && e.message ? e.message : String(e)));
  }
})(/*__COMMAND__*/ null);
