const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const ts = require('typescript');
const out = {};
vm.runInNewContext(ts.transpileModule(fs.readFileSync(__dirname + '/src/searchShortcut.ts', 'utf8'), {
  compilerOptions: { module: ts.ModuleKind.CommonJS }
}).outputText, { exports: out });

function input(visible) { return { visible, focused: false, selected: false, focus() { this.focused = true; }, select() { this.selected = true; }, getClientRects() { return { length: visible ? 1 : 0 }; } }; }
function page({ inputs = [], global = [], dialog = false } = {}) { return { querySelector: s => (s === '[role="dialog"]' && dialog ? {} : null), querySelectorAll: s => (s === '[data-page-search] input' ? inputs : s === '[data-global-search] input' ? global : []) }; }
function key(extra = {}) { return { key: '/', ctrlKey: false, metaKey: false, altKey: false, defaultPrevented: false, target: { tagName: 'BODY' }, prevented: false, preventDefault() { this.prevented = true; }, ...extra }; }

let hidden = input(false), shown = input(true), event = key();
assert.equal(out.focusSearch(event, page({ inputs: [hidden, shown] })), true);
assert.ok(shown.focused && shown.selected && event.prevented); assert.ok(!hidden.focused); // The visible box, e.g. desktop vs mobile layout.
for (const extra of [{ key: 'a' }, { ctrlKey: true }, { metaKey: true }, { altKey: true }, { defaultPrevented: true }, { target: { tagName: 'INPUT' } }, { target: { tagName: 'TEXTAREA' } }, { target: { tagName: 'SELECT' } }, { target: { tagName: 'DIV', isContentEditable: true } }]) {
  const box = input(true), e = key(extra);
  assert.equal(out.focusSearch(e, page({ inputs: [box] })), false, JSON.stringify(extra));
  assert.ok(!box.focused && !e.prevented); // Typing '/' in a field still types '/'.
}
const behind = input(true); event = key();
assert.equal(out.focusSearch(event, page({ inputs: [behind], dialog: true })), false); assert.ok(!behind.focused && !event.prevented);
// The global topbar search wins; a hidden one falls back to the page's own box.
{ const top = input(true), local = input(true); event = key();
  assert.equal(out.focusSearch(event, page({ global: [top], inputs: [local] })), true);
  assert.ok(top.focused && top.selected && event.prevented && !local.focused); }
{ const top = input(false), local = input(true); event = key();
  assert.equal(out.focusSearch(event, page({ global: [top], inputs: [local] })), true);
  assert.ok(local.focused && !top.focused); }
{ const top = input(true), e = key({ target: { tagName: 'INPUT' } });
  assert.equal(out.focusSearch(e, page({ global: [top] })), false); assert.ok(!top.focused); }
{ const top = input(true); event = key();
  assert.equal(out.focusSearch(event, page({ global: [top], dialog: true })), false); assert.ok(!top.focused && !event.prevented); }
event = key(); assert.equal(out.focusSearch(event, page()), false); assert.ok(!event.prevented); // Home has no search box: '/' does nothing.
console.log("'/' shortcut: focuses the visible page search, ignores fields, modifiers, open windows and pages without search.");

{const fallback=input(true),e=key();const doc={querySelector:()=>null,querySelectorAll:selector=>selector==='main input[type="search"]'?[fallback]:[]};assert.equal(out.focusSearch(e,doc),true);assert.ok(fallback.focused&&fallback.selected&&e.prevented,'standard framework page inputs remain a fallback without legacy CSS selectors');}
