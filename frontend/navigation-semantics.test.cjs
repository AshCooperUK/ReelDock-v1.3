const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const ts = require('typescript');
const React = require('react');
const { renderToStaticMarkup } = require('react-dom/server');
const { MantineProvider } = require('@mantine/core');

// Render each actual navigation declaration with Mantine. An <a> without href
// has no native keyboard activation; every controlled SPA action must remain a
// button, so Enter/Space, focus and form semantics come from the browser.
const navigation = [];
for (const filename of ['LiveDashboard.tsx', 'LiveSettings.tsx']) {
  const source = fs.readFileSync(__dirname + '/src/' + filename, 'utf8');
  const tree = ts.createSourceFile(filename, source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  function visit(node) {
    if ((ts.isJsxSelfClosingElement(node) || ts.isJsxOpeningElement(node)) && node.tagName.getText(tree) === 'NavLink') {
      assert.ok(ts.isJsxSelfClosingElement(node), 'audit a new navigation composition explicitly');
      navigation.push(node.getText(tree));
    }
    ts.forEachChild(node, visit);
  }
  visit(tree);
}
assert.equal(navigation.length,3,'primary navigation, System status and Settings category declarations are all covered');
const componentSource = `
import { NavLink } from '@mantine/core';
const compact=false,compactNav = false, label = 'Discover', item = 'Discover', view = 'Discover', name = 'General', section = 'General';
const Icon=()=>null,HardDrive=Icon,Settings2=Icon,p={view:'Discover',label:'Discover',Icon};const navigate=()=>{},onSelect=()=>{},setNavOpen=()=>{},setStatusOpen=()=>{},labelFor=value=>value,setDrawerPanel=()=>{};
export function Navigation() { return <>${navigation.join('')}</>; }
`;
const output = {};
vm.runInNewContext(ts.transpileModule(componentSource, { compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX } }).outputText, { exports: output, require });
const html = renderToStaticMarkup(React.createElement(MantineProvider, { forceColorScheme: 'dark' }, React.createElement(output.Navigation)));
const buttons = html.match(/<button\b[^>]*>/g) || [];
assert.equal(buttons.length, navigation.length, 'every actual navigation control renders a native keyboard button');
for (const button of buttons) assert.match(button, /type="button"/, 'navigation never submits an enclosing form');
assert.doesNotMatch(html, /<a\b/, 'controlled navigation must not render anchors without destinations');
assert.match(html, /aria-label="Discover"/, 'compact primary navigation retains its accessible name');
assert.match(html, />General</, 'settings categories retain visible accessible labels');
console.log('Mantine navigation: all primary, overview, settings and category controls retain native button/keyboard semantics.');

// Structural shell guarantees are independent of visual class names.
const shellSource=fs.readFileSync(__dirname+'/src/LiveDashboard.tsx','utf8');
const shellTree=ts.createSourceFile('LiveDashboard.tsx',shellSource,ts.ScriptTarget.Latest,true,ts.ScriptKind.TSX);
let header,settingsNode,searchInputs=0;const routeViews=[];
function shellVisit(node){
 if(ts.isJsxOpeningElement(node)&&node.tagName.getText(shellTree)==='AppShell')header=node.attributes.properties.find(attribute=>attribute.name?.text==='header')?.initializer?.expression;
 if(ts.isJsxSelfClosingElement(node)&&node.tagName.getText(shellTree)==='LiveSettings')settingsNode=node;
 if((ts.isJsxOpeningElement(node)||ts.isJsxSelfClosingElement(node))&&node.tagName.getText(shellTree)==='TextInput'&&node.attributes.properties.some(attribute=>attribute.name?.text==='type'&&attribute.initializer?.text==='search'))searchInputs++;
 if(ts.isVariableDeclaration(node)&&node.name.getText(shellTree)==='pages'&&ts.isArrayLiteralExpression(node.initializer))for(const item of node.initializer.elements){const view=item.properties.find(property=>property.name?.getText(shellTree)==='view');routeViews.push(view.initializer.text);}
 ts.forEachChild(node,shellVisit);
}
shellVisit(shellTree);assert.equal(header.properties.find(property=>property.name.getText(shellTree)==='height').initializer.text,'64','the phone header uses the shared compact height');
assert.deepEqual(routeViews,['Home','Discover','Subscriptions','TV Guide','Favourites','Watchlist','Local Library','Activity','Settings'],'all main functions remain in navigation');
assert.equal(searchInputs,1,'the shell declares one shared search field');assert.ok(settingsNode);
let ancestor=settingsNode.parent,insideMain=false;while(ancestor){if(ts.isJsxElement(ancestor)){const tag=ancestor.openingElement.tagName.getText(shellTree);assert.ok(!['Drawer','Dialog','DialogContent'].includes(tag),'Settings is a full page rather than a nested window');if(tag==='AppShell.Main')insideMain=true;}ancestor=ancestor.parent;}assert.ok(insideMain);
const shellCss=fs.readFileSync(__dirname+'/src/app/App.module.css','utf8');assert.match(shellCss,/grid-template-columns:repeat\(4,minmax\(0,1fr\)\)/,'phone navigation has four equal reachable destinations');assert.match(shellCss,/min-height:54px;min-width:44px/,'bottom navigation targets stay touch sized');
const mainSource=fs.readFileSync(__dirname+'/src/main.tsx','utf8'),mainTree=ts.createSourceFile('main.tsx',mainSource,ts.ScriptTarget.Latest,true,ts.ScriptKind.TSX);
const appearanceRoots=[];function appearanceVisit(node){if(ts.isJsxElement(node)&&node.openingElement.tagName.getText(mainTree)==='AppearanceProvider')appearanceRoots.push(node);ts.forEachChild(node,appearanceVisit);}appearanceVisit(mainTree);
assert.equal(appearanceRoots.length,1,'one global appearance provider controls every application surface');
const appearanceContents=appearanceRoots[0].getText(mainTree);for(const surface of ['AuthApp','LiveDashboard','ChannelDialog','NotificationCenter'])assert.match(appearanceContents,new RegExp('<'+surface+'\\b'),surface+' inherits the selected light/dark appearance');
assert.doesNotMatch(mainSource,/forceColorScheme\s*=\s*["']dark["']/,'the application entry cannot bypass the selected appearance with forced dark mode');
const appearanceProvider=fs.readFileSync(__dirname+'/src/app/AppearanceProvider.tsx','utf8');assert.match(appearanceProvider,/forceColorScheme=\{appearance\.scheme\}/,'Mantine receives the shared scheme state');assert.match(appearanceProvider,/primaryColor:appearance\.accent/,'the same provider applies the shared accent');
console.log('Fresh shell: all main routes, one search declaration, 64px header, four touch-sized bottom slots, full-page Settings and global appearance passed.');

assert.doesNotMatch(shellSource,/aria-label="Search scope"|\bsearchScope\b|\bsetSearchScope\b/,'YouTube and every other route share the same global search without options');
const searchCss=/\.search\s*\{([^}]*)\}/.exec(shellCss)?.[1];assert.ok(searchCss);assert.match(searchCss,/flex\s*:\s*1/,'the global field fills the available header width');assert.doesNotMatch(searchCss,/max-width/,'the search bar has no640px cap');
