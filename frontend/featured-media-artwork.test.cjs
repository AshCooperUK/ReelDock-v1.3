const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const ts = require('typescript');
const React = require('react');
const Mantine = require('@mantine/core');
const { renderToStaticMarkup } = require('react-dom/server');
const h = React.createElement;
const { MediaArtwork } = require('./media-browse-test-helper.cjs')();
const nodes = value => Array.isArray(value) ? value.flatMap(nodes) : value && typeof value === 'object' ? [value, ...nodes(value.props?.children)] : [];
const text = value => typeof value === 'string' ? value : Array.isArray(value) ? value.map(text).join('') : value && typeof value === 'object' ? text(value.props?.children) : '';

function compile(file, imports = {}, globals = {}) {
  const out = {};
  vm.runInNewContext(ts.transpileModule(fs.readFileSync(path.join(__dirname, 'src', file), 'utf8'), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, esModuleInterop: true },
  }).outputText, { exports: out, ...globals, require: name => imports[name] || require(name) });
  return out;
}

function workspace(items) {
  const state = [], opened = [], saved = new Map();
  let index = 0;
  const hooks = { ...React, useState(initial) {
    const slot = index++;
    if (!(slot in state)) state[slot] = slot === 0 ? [{ id: 'local', label: 'Local Library', media: ['youtube'], sorts: ['new'] }] : typeof initial === 'function' ? initial() : initial;
    return [state[slot], next => { state[slot] = typeof next === 'function' ? next(state[slot]) : next; }];
  }, useRef: current => ({ current }), useEffect() {}, useMemo: callback => callback(), useCallback: callback => callback };
  const generic = new Proxy({ __esModule: true, default: 'Stub' }, { get: (value, key) => key in value ? value[key] : 'Stub' });
  const imports = {
    react: hooks, '@mantine/core': Mantine,
    '../components/browse': { MediaArtwork, MediaCard: 'BrowseCard' },
    '../components/ui': { Button: 'button', Dialog: 'Dialog', DialogContent: 'DialogContent', PopupShell: 'PopupShell' },
    '../appNavigation': { mediaSearchQuery: () => '' },
    './ordering': { sortGroups: [] },
    './pagePreloader': { createPagePreloader: () => ({ clear() {}, load() { throw new Error('No read expected.'); } }) },
    './localParent': { createParentOpener: () => ({ open: item => opened.push(item), cancel() {} }) },
    '../collections/TileActions': { __esModule: true, default: 'TileActions', mediaTileTarget: () => ({}) },
  };
  const module = compile('viewMedia/ViewMedia.tsx', new Proxy(imports, { get: (value, key) => key in value ? value[key] : key.startsWith('.') ? generic : require(key) }), {
    location: { search: '' }, localStorage: { getItem: key => saved.get(key) || null, setItem: (key, value) => saved.set(key, value), removeItem: key => saved.delete(key) },
    URLSearchParams, AbortController, Map, Date,
  });
  return { opened, saved, draw() {
    index = 0;
    return module.default({ csrf: 'fixture', initialSource: 'local', initialMedia: 'youtube', sourceLocked: true,
      initial: { source: 'local', media: 'youtube', label: 'YouTube', nextOffset: null, items } });
  } };
}

for (const image of ['/artwork/failed-channel-image', '']) {
  const card = { id: 'group-' + 'a'.repeat(24), title: 'Autoalex Cars', kind: 'youtube', image, count: 2,
    action: { type: 'local-group', id: 'group-' + 'a'.repeat(24) } };
  const second = { ...card, id: 'group-' + 'b'.repeat(24), title: 'Second saved channel', image: '', action: { type: 'local-group', id: 'group-' + 'b'.repeat(24) } };
  const ui = workspace([card, second]);
  let tree = ui.draw();
  const panel = nodes(tree).find(node => node.props?.['aria-label'] === 'Featured media');
  assert.ok(panel);
  const slot = nodes(panel).find(node => node.props?.['data-featured-artwork'] !== undefined);
  assert.ok(slot, 'featured media always has the shared fallback slot, including empty artwork');
  assert.equal(slot.props.visibleFrom, 'sm');
  assert.equal(slot.props.w, 72);
  assert.equal(slot.props.style.flexShrink, 0);
  assert.equal(React.Children.count(slot.props.children), 1);
  assert.equal(slot.props.children.type, MediaArtwork, 'the selected panel uses the same artwork component as media tiles');
  assert.equal(slot.props.children.props.src, image || undefined);
  assert.equal(slot.props.children.props.kind, 'youtube');
  const details = nodes(panel).filter(node => node.type === 'button' && text(node) === 'View details');
  assert.equal(details.length, 1, 'one detail action remains in the selected panel');
  assert.ok(!nodes(panel).some(node => node.type === 'TileActions'), 'local groups retain no duplicate collection/info actions');
  details[0].props.onClick();
  assert.equal(ui.opened[0], card, 'the original folder/parent identity is passed unchanged');
  nodes(panel).find(node => node.props?.['aria-label'] === 'Next featured title').props.onClick();
  tree = ui.draw();
  assert.ok(text(nodes(tree).find(node => node.props?.['aria-label'] === 'Featured media')).includes(second.title));
  nodes(tree).find(node => node.props?.['aria-label'] === 'Previous featured title').props.onClick();
  tree = ui.draw();
  assert.ok(text(nodes(tree).find(node => node.props?.['aria-label'] === 'Featured media')).includes(card.title));
  nodes(tree).find(node => node.props?.['aria-label'] === 'Hide featured media').props.onClick();
  assert.equal(ui.saved.get('reeldock-hero-hidden'), '1');
  tree = ui.draw();
  assert.ok(!nodes(tree).some(node => node.props?.['aria-label'] === 'Featured media'));
  nodes(tree).find(node => node.type === 'button' && text(node) === 'Show featured').props.onClick();
  assert.ok(nodes(ui.draw()).some(node => node.props?.['aria-label'] === 'Featured media'));
  const html = renderToStaticMarkup(h(Mantine.MantineProvider, { forceColorScheme: 'dark' }, slot));
  assert.equal((html.match(/mantine-AspectRatio-root/g) || []).length, 1);
  assert.equal((html.match(/<img\b/g) || []).length, image ? 1 : 0);
}

// Drive the actual shared image handlers: the failed native image is hidden,
// and the placeholder remains in the same slot until a successful retry loads.
const state = [];
let index = 0;
const hooks = { ...React, useState(initial) { const slot = index++; if (!(slot in state)) state[slot] = initial; return [state[slot], next => { state[slot] = typeof next === 'function' ? next(state[slot]) : next; }]; }, useRef: current => ({ current }), useEffect() {} };
const artwork = compile('LiveArtwork.tsx', { react: hooks });
const src = '/artwork/failed-channel-image';
const keyed = artwork.default({ src, placeholder: h('span', { 'data-fixture-placeholder': true }, 'Artwork unavailable') });
assert.equal(keyed.key, src, 'a new URL starts its own image loading/failure state');
const draw = () => { index = 0; return keyed.type(keyed.props); };
let imageTree = draw();
let image = nodes(imageTree).find(node => node.type === Mantine.Image);
assert.equal(image.props.style.visibility, 'hidden');
image.props.onError();
imageTree = draw();
image = nodes(imageTree).find(node => node.type === Mantine.Image);
assert.equal(image.props.style.visibility, 'hidden', 'failed images never expose the browser broken-image glyph');
assert.equal(nodes(imageTree).filter(node => node.props?.['data-fixture-placeholder']).length, 1);
let html = renderToStaticMarkup(h(Mantine.MantineProvider, { forceColorScheme: 'dark' }, imageTree));
assert.match(html, /visibility:hidden/);
assert.match(html, /Artwork unavailable/);
assert.equal((html.match(/<img\b/g) || []).length, 1, 'error feedback does not create a second image element');
image.props.onLoad();
imageTree = draw();
image = nodes(imageTree).find(node => node.type === Mantine.Image);
assert.equal(image.props.style.visibility, undefined);
assert.ok(!nodes(imageTree).some(node => node.props?.['data-fixture-placeholder']), 'successful retries replace the placeholder inside the existing slot');
console.log('Featured Library artwork: shared bounded empty/error fallback, one image slot, exact parent identity, one details action and unchanged hide/cycle controls passed.');
