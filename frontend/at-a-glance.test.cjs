const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const ts = require('typescript');
const React = require('react');
const { renderToStaticMarkup } = require('react-dom/server');
const Mantine=require('@mantine/core');
const layout={};vm.runInNewContext(ts.transpileModule(fs.readFileSync(__dirname+'/src/components/layout.tsx','utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX}}).outputText,{exports:layout,require});
const exportsObject = {};
vm.runInNewContext(ts.transpileModule(fs.readFileSync(__dirname + '/src/dashboard/AtAGlance.tsx', 'utf8'), {
  compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX }
}).outputText, { exports: exportsObject, require: name => name==='../components/layout'?layout:name.endsWith('.css')?{}:require(name), Date });
const Component = exportsObject.default;
const targets = ['YouTube', 'M3U Subscriptions', 'nzb', 'torrent', 'settings:Auto-Download', 'settings:Metadata', 'settings:Media Players', 'Discover'];
const rows = targets.map((target, index) => ({ id: String(index), title: target, target, status: index === 2 ? 'Unavailable' : 'Ready', detail: '<private> & text', attention: 0 }));
let buttons = 0;
const summary = { rows, updated: 1791621000, paused: true, worker_online: false, attention: 0, completed_today: 2, indexed_youtube: 1, next: null };
const tree = Component({ summary });
function visit(node) {
  if (!node || typeof node !== 'object') return;
  if (node.type === 'button' || node.type === 'a' || node.props?.onClick) buttons++;
  React.Children.forEach(node.props?.children, visit);
}
visit(tree);
assert.equal(buttons, 0);
const render=node=>renderToStaticMarkup(React.createElement(Mantine.MantineProvider,{forceColorScheme:'dark'},node));
const html=render(tree);
assert.match(html, /Downloads paused/);
assert.match(html, /Worker offline/);
assert.match(html, /Unavailable/);
assert.match(html, /&lt;private&gt; &amp; text/);
assert.match(html, /YouTube \/ VOD downloads/);
assert.doesNotMatch(html, /completion dates are not recorded|Other engines are not|Refreshes every/);
assert.match(render(React.createElement(Component, { onOpen() {} })), /summaries unavailable/);
const vpnTree=React.createElement(Component,{summary:{...summary,rows:[...rows,{id:'vpn',title:'VPN',target:'settings:VPN',status:'Blocked',attention:1,detail:'',brief:''}]}});
const vpnHtml=render(vpnTree);
assert.match(vpnHtml, /VPN/);
assert.match(vpnHtml,/Blocked/);
assert.match(vpnHtml,/--badge-bg:var\(--mantine-color-yellow-light\)/,'attention remains a visible framework warning badge');
assert.doesNotMatch(html, />VPN</);
console.log('At a glance rendering, scope labels, escaping and read-only rows passed.');

const aligned=Component({summary:{...summary,rows:[{id:'youtube',title:'YouTube',target:'Subscriptions',status:'Checks paused',brief:'4 channels',detail:'',attention:0}]}});
let statusBand;function findBand(n){if(!n||typeof n!=='object')return;if(n.props?.['aria-label']==='Installation status')statusBand=n;React.Children.forEach(n.props?.children,findBand);}findBand(aligned);assert.ok(statusBand);const statusHtml=render(statusBand);assert.match(statusHtml,/Downloads paused/);assert.match(statusHtml,/Worker offline/);assert.match(statusHtml,/YouTube checks paused/);assert.equal((render(aligned).match(/YouTube checks paused/g)||[]).length,1,'one clearly scoped check badge, sharing the installation status row');
