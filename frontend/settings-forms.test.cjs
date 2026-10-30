const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const ts = require('typescript');
const React = require('react');
const { renderToStaticMarkup } = require('react-dom/server');
const { MantineProvider, Button: MantineButton, Stack } = require('@mantine/core');
const jsx = (type, props) => ({ type, props: props || {} });
const nodes = tree => !tree || typeof tree !== 'object' ? [] : Array.isArray(tree) ? tree.flatMap(nodes) : [tree, ...nodes(tree.props?.children)];
function load(filename, imports, globals = {}) {
  const output = {};
  vm.runInNewContext(ts.transpileModule(fs.readFileSync(path.join(__dirname, 'src', filename), 'utf8'), { compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX } }).outputText, {
    exports: output, Event: class {}, window: { dispatchEvent() {} },
    require: name => name === 'react/jsx-runtime' ? { jsx, jsxs: jsx, Fragment: 'Fragment' } : imports[name] || new Proxy({}, { get: (_, key) => key }),
    ...globals,
  });
  return output;
}
function hooks() {
  let index = 0, values = [];
  return {
    resetCursor() { index = 0; },
    react: {
      useState(initial) { const at = index++; if (!(at in values)) values[at] = initial; return [values[at], next => { values[at] = typeof next === 'function' ? next(values[at]) : next; }]; },
      useRef(initial) { const at = index++; return values[at] ??= { current: initial }; },
      useEffect() {},
    },
  };
}
const baseUI = { Group: 'Group', Row: 'Row', Toggle: 'Toggle', Field: 'Field', Notice: 'Notice', Loading: 'Loading' };
const find = (tree, type, text) => nodes(tree).find(node => node.type === type && (text === undefined || node.props.children === text));
function auditNativeForms() {
  const root=path.join(__dirname,'src');
  const walk=directory=>fs.readdirSync(directory,{withFileTypes:true}).flatMap(entry=>entry.isDirectory()?walk(path.join(directory,entry.name)):entry.name.endsWith('.tsx')?[path.join(directory,entry.name)]:[]);
  let submits=0,forms=0;
  for(const filename of [...walk(path.join(root,'settings')),path.join(root,'LiveSettings.tsx'),path.join(root,'YouTubeChannelImport.tsx'),path.join(root,'ResetReelDock.tsx')]) {
    const source=fs.readFileSync(filename,'utf8'),tree=ts.createSourceFile(filename,source,ts.ScriptTarget.Latest,true,ts.ScriptKind.TSX);
    const attribute=(opening,name)=>opening.attributes.properties.find(prop=>ts.isJsxAttribute(prop)&&prop.name.getText(tree)===name);
    function visit(node,inForm=false) {
      if(ts.isJsxElement(node)) {
        const opening=node.openingElement,tag=opening.tagName.getText(tree);
        const form=tag==='form'||attribute(opening,'component')?.initializer?.text==='form';
        if(form) {
          forms++;
          const child=node.children.find(entry=>!ts.isJsxText(entry)||entry.text.trim());
          const visible=node.children.filter(entry=>!(ts.isJsxText(entry)&&!entry.text.trim())).filter(entry=>!(ts.isJsxSelfClosingElement(entry)&&entry.tagName.getText(tree)==='input'&&attribute(entry,'type')?.initializer?.text==='hidden'));
          if(visible.length>1||!visible.some(entry=>ts.isJsxElement(entry)&&entry.openingElement.tagName.getText(tree)==='Button'))assert.ok(ts.isJsxElement(child)&&['SettingsStack','Stack'].includes(child.openingElement.tagName.getText(tree)),filename+' form spacing belongs to Mantine');
        }
        if(inForm&&tag==='Button'&&!attribute(opening,'onClick')) {
          submits++;
          assert.equal(attribute(opening,'type')?.initializer?.text,'submit',filename+' intended form action must explicitly submit');
        }
        for(const child of node.children)visit(child,inForm||form);
        return;
      }
      ts.forEachChild(node,child=>visit(child,inForm));
    }
    visit(tree);
  }
  assert.ok(forms>=12,'all settings forms are covered');
  assert.ok(submits>=12,'save/connect/reconnect/import and account actions are covered');
}
async function run() {
  auditNativeForms();
  // The styled dropdown is an HTMLInputElement. Its blur must not enter the
  // text-field autosave branch and duplicate its existing onChange write.
  const vodSource=fs.readFileSync(path.join(__dirname,'src/settings/m3u/VodSettings.tsx'),'utf8');
  const vodTree=ts.createSourceFile('VodSettings.tsx',vodSource,ts.ScriptTarget.Latest,true,ts.ScriptKind.TSX);
  let blurHandler;
  function findBlur(node) { if(ts.isJsxAttribute(node)&&node.name.getText(vodTree)==='onBlur')blurHandler=node.initializer.expression.getText(vodTree);ts.forEachChild(node,findBlur); }
  findBlur(vodTree); assert.ok(blurHandler);
  class InputFixture { constructor(role){this.role=role;this.type='text';} getAttribute(){return this.role;} checkValidity(){return true;} }
  class TextareaFixture extends InputFixture {}
  let autosaves=0;
  const blurTarget=target=>vm.runInNewContext('('+blurHandler+')(event)',{event:{target},source:{id:'fixture'},HTMLInputElement:InputFixture,HTMLTextAreaElement:TextareaFixture,persist(){autosaves++;}});
  blurTarget(new InputFixture('combobox')); assert.equal(autosaves,0,'a dropdown change has its own save and must not save twice on blur');
  blurTarget(new InputFixture(null)); blurTarget(new TextareaFixture(null)); assert.equal(autosaves,2,'ordinary text and multiline blur autosaves remain intact');
  // The reconnect action remains an actual native POST with its CSRF field.
  const youtube = load('settings/youtube/YouTubeSettings.tsx', {
    react: hooks().react,
    '../../SettingsUI': { ...baseUI, useSettings: url => ({ data: url.includes('cookies') ? { saved: false } : { use_google_api: true }, reload() {} }), useAction: () => ({ busy: false }) },
  }).default({ csrf: 'fixture-csrf' });
  const reconnect = nodes(youtube).find(node => node.type === 'form' && node.props.action === '/google/reconnect');
  assert.ok(reconnect); assert.equal(reconnect.props.method, 'post');
  const csrf = nodes(reconnect).find(node => node.type === 'input' && node.props.name === 'csrf');
  assert.equal(csrf.props.value, 'fixture-csrf');
  const reconnectButton = find(reconnect, 'Button', 'Change Google connection');
  assert.equal(reconnectButton.props.type, 'submit');
  const html = renderToStaticMarkup(React.createElement(MantineProvider, { forceColorScheme: 'dark' }, React.createElement('form', { method: reconnect.props.method, action: reconnect.props.action }, React.createElement(Stack, { gap: 'md' }, React.createElement('input', csrf.props), React.createElement(MantineButton, reconnectButton.props)))));
  assert.match(html, /<form[^>]*method="post"/); assert.match(html, /name="csrf" value="fixture-csrf"/); assert.match(html, /<button[^>]*type="submit"/);

  const vpnHooks = hooks(), vpnCalls = [], vpnPreferenceCalls = [];
  let vpnValues = { youtube: true, nzb: true, torrent: true, vod: true, metadata: false, downloads: true };
  const status = { available: true, configured: false, state: 'Disconnected', values: {}, external_ip: '', provider: '', endpoint: '', message: '' };
  const vpnModule = load('settings/VpnSettings.tsx', {
    react: vpnHooks.react,
    '../SettingsUI': { ...baseUI, useSettings: () => ({ data: status, reload() {}, setData() {} }), useAction: token => { assert.equal(token, 'fixture-csrf'); return { busy: false, run: async (url, body) => { vpnCalls.push({ url, body }); return null; } }; } },
    './useEngineSettings': { useEngineSettings: () => ({ values: vpnValues, busy: false, reload() {}, save: async values => { vpnPreferenceCalls.push(values); return true; } }) },
  });
  const vpn = vpnModule.default;
  const renderVpn = () => { vpnHooks.resetCursor(); return vpn({ csrf: 'fixture-csrf' }); };
  let tree = renderVpn(), picker = find(tree, 'FileInput');
  const countryRow = () => nodes(renderVpn()).find(node => node.type === 'Row' && node.props.title === 'Country');
  assert.equal(countryRow().props.detail, 'Detected from VPN exit IP');
  assert.equal(countryRow().props.children.props.children, 'Unavailable', 'the setup location must not stand in for an unknown exit country');
  status.country_code = 'GB';
  assert.equal(countryRow().props.children.props.children, 'United Kingdom (GB)');
  status.country_code = 'US';
  assert.equal(countryRow().props.children.props.children, 'United States (US)', 'a changed exit country updates the existing status row');
  const switches = () => nodes(renderVpn()).filter(node => node.type === 'Toggle');
  assert.deepEqual(switches().map(node => node.props.title), [
    'YouTube downloads through VPN', 'NZB downloads through VPN', 'Torrent downloads through VPN', 'VOD downloads through VPN', 'Metadata through VPN']);
  assert.deepEqual(switches().map(node => node.props.value), [true, true, true, true, false], 'legacy enabled downloads are represented by four enabled engine switches');
  for (const [index, key] of ['youtube', 'nzb', 'torrent', 'vod', 'metadata'].entries()) {
    if(key!=='metadata')assert.match(switches()[index].props.detail, /manual and automatic/, 'automatic downloads follow their engine switch');
    await switches()[index].props.onChange(false);
    assert.deepEqual(JSON.parse(JSON.stringify(vpnPreferenceCalls.at(-1))), { [key]: false }, 'a switch writes only its selected engine');
  }
  assert.deepEqual(switches().map(node => node.props.value), [true, true, true, true, false], 'controls retain server-confirmed values while a save is pending');
  vpnValues = { ...vpnValues, youtube: false };
  assert.deepEqual(switches().map(node => node.props.value), [false, true, true, true, false], 'one confirmed change retains the other engine and metadata switches');
  assert.equal(switches().length, 5, 'country display adds no extra routing control');
  assert.equal(vpnCalls.length, 0, 'rendering detected country performs no connection write');
  assert.equal(vpnModule.countryLabel('bad'), 'Unavailable');
  const noDisplayNames = load('settings/VpnSettings.tsx', {}, { Intl: {} });
  assert.equal(noDisplayNames.countryLabel('GB'), 'GB', 'older browsers retain a safe country code fallback');
  assert.equal(picker.props.name, 'wireguard_config'); assert.equal(picker.props.accept, '.conf');
  let resets = 0; picker.props.resetRef.current = () => resets++;
  await picker.props.onChange({ name: 'bad.conf', size: 16001, text: async () => 'not read' });
  tree = renderVpn(); assert.equal(find(tree, 'FileInput').props.value, null); assert.equal(find(tree, 'Button', 'Connect WireGuard').props.disabled, true); assert.ok(nodes(tree).some(node => node.type === 'Notice' && /16 KB/.test(node.props.text || '')));
  const config = '[Interface]\nPrivateKey=fixture\n[Peer]\nAllowedIPs=0.0.0.0/0';
  await find(tree, 'FileInput').props.onChange({ name: 'valid.conf', size: 100, text: async () => config });
  tree = renderVpn(); assert.equal(find(tree, 'Button', 'Connect WireGuard').props.disabled, false); assert.equal(find(tree, 'Button', 'Connect WireGuard').props.type, 'submit');
  await find(tree, 'form').props.onSubmit({ preventDefault() {} }); await new Promise(resolve => setImmediate(resolve));
  assert.equal(vpnCalls[0].url, '/api/vpn'); assert.deepEqual(JSON.parse(JSON.stringify(vpnCalls[0].body)), { action: 'wireguard', config });
  assert.equal(find(renderVpn(), 'FileInput').props.value.name, 'valid.conf', 'failed connections retain their selected config for retry');
  nodes(tree).find(node => node.props['aria-label'] === 'VPN connection method').props.onChange({ target: { value: 'privado' } });
  tree = renderVpn(); const privateInputs = nodes(tree).filter(node => node.type === 'TextInput');
  privateInputs.find(node => node.props.name === 'privado-username').props.onChange({ target: { value: 'fixture-user' } });
  privateInputs.find(node => node.props.name === 'privado-password').props.onChange({ target: { value: 'fixture-password' } });
  tree = renderVpn(); assert.equal(find(tree, 'Button', 'Connect PrivadoVPN').props.type, 'submit');
  await find(tree, 'form').props.onSubmit({ preventDefault() {} }); await new Promise(resolve => setImmediate(resolve));
  assert.deepEqual(JSON.parse(JSON.stringify(vpnCalls[1].body)), { action: 'privado', username: 'fixture-user', password: 'fixture-password', server: 'GB' });
  assert.ok(resets > 0);

  const importsHooks = hooks(), importCalls = []; let successful = false;
  const importer = load('YouTubeChannelImport.tsx', {
    react: importsHooks.react,
    './SettingsUI': { ...baseUI, useAction: token => { assert.equal(token, 'fixture-csrf'); return { busy: false, run: async (url, body) => { importCalls.push({ url, body }); return successful ? { message: 'Queued' } : null; } }; } },
  }).default;
  const renderImport = () => { importsHooks.resetCursor(); return importer({ csrf: 'fixture-csrf' }); };
  tree = renderImport(); picker = find(tree, 'FileInput'); assert.equal(picker.props.name, 'channels_csv');
  let csvResets = 0; picker.props.resetRef.current = () => csvResets++;
  await picker.props.onChange({ name: 'too-big.csv', size: 500001, text: async () => 'not read' });
  tree = renderImport(); assert.equal(find(tree, 'FileInput').props.value, null); assert.equal(find(tree, 'Button', 'Add channels').props.disabled, true);
  await find(tree, 'FileInput').props.onChange({ name: 'channels.csv', size: 500000, text: async () => '@fixture-channel' });
  tree = renderImport(); assert.equal(find(tree, 'TextArea').props.value, '@fixture-channel'); assert.equal(find(tree, 'Button', 'Add channels').props.type, 'submit');
  await find(tree, 'form').props.onSubmit({ preventDefault() {} });
  assert.equal(importCalls[0].url, '/api/youtube/channels'); assert.equal(importCalls[0].body.channels, '@fixture-channel');
  assert.equal(find(renderImport(), 'FileInput').props.value.name, 'channels.csv', 'failed imports retain their file and entered text');
  successful = true; await find(renderImport(), 'form').props.onSubmit({ preventDefault() {} });
  tree = renderImport(); assert.equal(find(tree, 'TextArea').props.value, ''); assert.equal(find(tree, 'FileInput').props.value, null); assert.ok(csvResets >= 2, 'confirmed imports reset the actual native file selection');
  console.log('Settings forms: native reconnect POST/CSRF, explicit submit actions, WireGuard/Privado payloads, upload caps and confirmed-only selection resets passed.');
}
run().catch(error => { console.error(error); process.exitCode = 1; });
