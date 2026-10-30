const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const ts = require('typescript');
const React = require('react');
const { renderToStaticMarkup } = require('react-dom/server');
const { MantineProvider } = require('@mantine/core');
let viewport = 1440, requests = 0;
function load(filename, imports = {}) {
  const output = {};
  const source = fs.readFileSync(path.join(__dirname, 'src', filename), 'utf8');
  vm.runInNewContext(ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX } }).outputText, {
    exports: output,
    require: name => imports[name] || (name.endsWith('/components/SelectField') ? require('./select-field-test-helper.cjs')() : require(name)),
    fetch() { requests++; throw new Error('Rendering a wizard fixture must never contact or change application state.'); },
  });
  return output;
}
const { mantineTheme } = load('mantineTheme.ts');
const AuthApp = load('AuthApp.tsx', {
  './notifications': { notify() {} },
  '@mantine/hooks': { useMediaQuery: query => viewport <= Number(/max-width:\s*([\d.]+)em/.exec(query)?.[1] || 0) * 16 },
}).default;
const ready = {
  admin: true, origin: 'https://fixture.example.test', csrf: 'fixture-csrf',
  storage: { ready: true, base: '/fixture/media', roots: ['/fixture/media'], writable: true, free_gb: 10, names: {} },
  use_google_api: false, import_method: 'manual', source_ready: true, youtube_connected: false,
  client_id_present: false, client_secret_present: false, id_from_yaml: false, secret_from_yaml: false,
  checks: [{ name: 'Storage', ok: true }, { name: 'Account', ok: true }],
};
const setup = patch => ({ page: 'setup', version: 'V1.3 fixture', initial: { ...ready, ...patch } });
const scenarios = [
  { id: 'account', step: 1, bootstrap: setup({ admin: false }), text: 'Create account' },
  { id: 'address', step: 2, bootstrap: setup({ origin: '' }), text: 'Verify address' },
  { id: 'folders', step: 3, bootstrap: setup({ storage: { ...ready.storage, ready: false } }), text: 'Check folders again' },
  { id: 'youtube-google', step: 4, bootstrap: setup({ use_google_api: true, import_method: 'google', source_ready: false }), text: 'Save connection details' },
  { id: 'youtube-cookies', step: 4, bootstrap: setup({ import_method: 'cookies', source_ready: false, cookies_saved: true }), text: 'Save cookies and continue' },
  { id: 'youtube-manual', step: 4, bootstrap: setup({ source_ready: false }), text: 'Import and continue' },
  { id: 'ready', step: 5, bootstrap: setup({}), text: 'Open ReelDock' },
  { id: 'login', bootstrap: { page: 'login', version: 'V1.3 fixture', csrf_token: 'fixture-login-csrf' }, text: 'Sign in' },
];
function renderScenario(scenario, width) {
  viewport = width;
  return renderToStaticMarkup(React.createElement(MantineProvider, { theme: mantineTheme, forceColorScheme: 'dark' }, React.createElement(AuthApp, { bootstrap: scenario.bootstrap })));
}
const input = (html, name) => (html.match(/<input\b[^>]*>/g) || []).find(tag => tag.includes(`name="${name}"`));
function run() {
  const directory = process.env.REELDOCK_AUTH_FIXTURE_DIR;
  const css = directory ? fs.readFileSync(require.resolve('@mantine/core/styles.css'), 'utf8') : '';
  if (directory) fs.mkdirSync(directory, { recursive: true });
  for (const width of [320, 768, 1440]) for (const scenario of scenarios) {
    const html = renderScenario(scenario, width);
    assert.ok(html.includes(scenario.text), `${scenario.id} action remains available at ${width}px`);
    assert.ok(html.includes('mantine-Stack-root'), 'flow spacing comes from Mantine Stack');
    assert.match(html, /<main[^>]*mantine-Paper-root/, 'one main form surface replaces the old split-card composition');
    assert.equal((html.match(/mantine-Paper-root/g)||[]).length,1,'auth and setup have a single primary paper');
    assert.doesNotMatch(html, /class="[^"]*\bauth-/, 'wizard has no legacy auth layout classes');
    if (scenario.step) assert.ok(html.includes(`STEP ${scenario.step} OF 5`), `${scenario.id} retains its correct setup stage`);
    if (width < 992 && scenario.step) assert.ok(html.includes(`aria-label="Setup step ${scenario.step} of 5"`), 'phone and tablet use compact progress');
    if (scenario.id === 'account') {
      for (const name of ['username', 'password', 'confirm']) assert.ok(input(html, name), `${name} remains a named native form field`);
      assert.match(input(html, 'password'), /type="password"/); assert.match(input(html, 'password'), /minLength="12"/i); assert.match(input(html, 'confirm'), /maxLength="256"/i);
      assert.equal((html.match(/<form\b/g) || []).length, 1);
      assert.ok(html.indexOf('3–64 letters')>html.indexOf(input(html,'username')),'field descriptions follow their input');
    }
    if (scenario.id === 'address') { assert.match(input(html, 'url'), /type="url"/); assert.ok(html.includes('Skip — use locally')); }
    if (scenario.id === 'folders') assert.ok(html.includes('/fixture/media'));
    if (scenario.step === 4) { assert.match(html, /role="combobox"/, 'wizard methods use the styled Mantine list, not an OS select'); assert.doesNotMatch(html, /<select\b/); }
    if (scenario.id === 'youtube-google') { assert.ok(input(html, 'client_id')); assert.match(input(html, 'client_secret'), /type="password"/); assert.ok(html.includes('/oauth/google/callback')); }
    if (scenario.id === 'youtube-cookies' || scenario.id === 'youtube-manual') {
      const name = scenario.id === 'youtube-cookies' ? 'cookies' : 'channels_csv';
      assert.match(input(html, name), /type="file"/); assert.match(input(html, name), /display:none/, 'native picker is hidden behind Mantine FileInput');
      assert.ok(html.includes('mantine-FileInput-input'), 'visible file picker uses the Mantine input');
      assert.ok(html.includes('min-height:44px'), 'file control keeps the shared control size');
      if (name === 'channels_csv') { assert.ok(html.includes('mantine-Textarea-input')); assert.ok(html.includes('Add channels later')); }
    }
    if (scenario.id === 'login') {
      const loginForm = html.match(/<form\b[^>]*>/)?.[0]; assert.match(loginForm, /method="post"/); assert.match(loginForm, /action="\/login"/); assert.match(input(html, 'csrf'), /value="fixture-login-csrf"/);
      assert.match(input(html, 'username'), /autoComplete="username"|autocomplete="username"/); assert.match(input(html, 'password'), /type="password"/);
      assert.doesNotMatch(input(html, 'username'), /disabled/); assert.doesNotMatch(input(html, 'password'), /disabled/, 'native login fields remain successful form controls');
    }
    if (directory) fs.writeFileSync(path.join(directory, `${scenario.id}-${width}.html`), `<!doctype html><html lang="en" data-mantine-color-scheme="dark"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Wizard layout fixture · ${scenario.id}</title><style>${css}</style></head><body>${html}</body></html>`);
  }
  assert.equal(requests, 0, 'SSR verification neither reads nor writes the configured application');
  const source = fs.readFileSync(__dirname + '/src/AuthApp.tsx', 'utf8');
  assert.doesNotMatch(source, /import ['"]\.\/auth\.css/, 'legacy wizard stylesheet is not loaded');
  assert.match(source, /data\.append\('cookies', cookieFile\)/, 'cookie uploads retain the exact multipart field');
  assert.match(source, /new FormData\(event\.currentTarget\)/, 'account and Google setup retain native named field collection');
  console.log('Wizard: all five stages, three import modes and single-paper login retain the shared Mantine form grammar, native fields, file controls and validation at 320/768/1440; no backend requests.');
}
if (require.main === module) run();
module.exports = { scenarios, renderScenario, run };
