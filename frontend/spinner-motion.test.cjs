const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const path = require('node:path');
const { createRequire } = require('node:module');
const { test } = require('node:test');
const React = require('react');
const { renderToStaticMarkup } = require('react-dom/server');
const { MantineProvider, Loader, Button, ActionIcon } = require('@mantine/core');
const postcss = createRequire(require.resolve('vite'))('postcss');
const root = path.join(__dirname, 'src');
const base = postcss.parse(readFileSync(path.join(root, 'app/base.css'), 'utf8'));
const app = postcss.parse(readFileSync(path.join(root, 'app/App.module.css'), 'utf8'));
const render = element => renderToStaticMarkup(React.createElement(MantineProvider, null, element));
const declaration = (sheet, selector, prop, reduced = false) => {
  let value;
  sheet.walkRules(rule => {
    const motion = rule.parent.type === 'atrule' && /prefers-reduced-motion:\s*reduce/.test(rule.parent.params);
    if (motion !== reduced || !rule.selectors.includes(selector)) return;
    rule.walkDecls(prop, entry => { value = { value: entry.value, important: entry.important }; });
  });
  return value;
};

test('all actual Mantine loader shapes and loading controls use the shared slower cycle', () => {
  assert.equal(declaration(base, ':root', '--reeldock-spinner-duration').value, '1.8s');
  assert.equal(declaration(base, '.mantine-Loader-root::after', 'animation-duration').value, 'var(--reeldock-spinner-duration)');
  assert.equal(declaration(base, '.mantine-Loader-root>span', 'animation-duration').value, 'var(--reeldock-spinner-duration)');
  const installed = readFileSync(require.resolve('@mantine/core/styles.css'), 'utf8');
  assert.match(installed, /\.m_b34414df::after\s*\{[^}]*animation:/, 'Oval animation belongs to its pseudo-element');
  for (const type of ['oval', 'bars', 'dots']) {
    const html = render(React.createElement(Loader, { type, 'aria-label': 'Loading ' + type }));
    assert.match(html, /<span class="[^"]*mantine-Loader-root[^"]*"/, type + ' retains its stock loader root');
    if (type !== 'oval') assert.equal((html.match(/<span class="m_[^"]+"><\/span>/g) || []).length, 3, type + ' animates the three direct child spans');
  }
  for (const Component of [Button, ActionIcon]) {
    const html = render(React.createElement(Component, { loading: true, 'aria-label': 'Save' }, 'Save'));
    assert.match(html, /data-loading="true"/);
    assert.match(html, /mantine-Loader-root/, 'Framework loading controls receive the same cycle');
  }
  const main = readFileSync(path.join(root, 'main.tsx'), 'utf8');
  assert.ok(main.indexOf("'@mantine/core/styles.css'") < main.indexOf("'./app/base.css'"), 'The shared duration follows framework defaults');
});

test('Sync uses the same cycle and reduced motion stops every spinner', () => {
  assert.equal(declaration(app, '.spin', 'animation').value, 'turn var(--reeldock-spinner-duration) linear infinite');
  for (const selector of ['.mantine-Loader-root::after', '.mantine-Loader-root>span']) {
    assert.deepEqual(declaration(base, selector, 'animation', true), { value: 'none', important: true });
  }
  assert.equal(declaration(app, '.spin', 'animation', true).value, 'none');
  assert.match(readFileSync(path.join(root, 'LiveDashboard.tsx'), 'utf8'), /syncBusy\?styles\.spin:undefined/, 'Sync keeps its existing busy-only spinner');
});
