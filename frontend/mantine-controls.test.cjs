const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const ts = require('typescript');
const React = require('react');
const { renderToStaticMarkup } = require('react-dom/server');
const { MantineProvider } = require('@mantine/core');
const { SelectField: NativeSelect, normalizeSelectData } = require('./select-field-test-helper.cjs')();
function load(file, imports = {}) {
  const exports = {};
  const code = ts.transpileModule(fs.readFileSync(__dirname + '/src/' + file, 'utf8'), { compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX } }).outputText;
  vm.runInNewContext(code, { exports, require: name => imports[name] || require(name) });
  return exports;
}
const { mantineTheme } = load('mantineTheme.ts');
const { Button, Switch } = load('components/ui.tsx', { '../ScrollToTop': { default: () => null }, '../notifications-state': { registerNotificationHost: () => () => {} } });
const { TextInput } = load('TextField.tsx');
const render = children => renderToStaticMarkup(React.createElement(MantineProvider, { forceColorScheme: 'dark', theme: mantineTheme }, children));
const buttons = render(React.createElement('form', null,
  React.createElement(Button, { name: 'safe' }, 'Secondary action'),
  React.createElement(Button, { type: 'submit', name: 'save' }, 'Save'),
  React.createElement(Button, { className: 'icon-button', 'aria-label': 'Refresh' }, '+')));
assert.match(buttons, /<button[^>]*type="button"[^>]*name="safe"/);
assert.match(buttons, /<button[^>]*type="submit"[^>]*name="save"/);
assert.match(buttons, /<button[^>]*aria-label="Refresh"/);
const field = render(React.createElement(TextInput, { name: 'channel', 'aria-label': 'Channel', defaultValue: '@fixture' }));
assert.match(field, /<input[^>]*name="channel"[^>]*value="@fixture"/);
const clear = field.match(/<button[^>]*aria-label="Clear Channel"[^>]*>/)?.[0];
assert.ok(clear, 'each clear action has a specific accessible name');
assert.match(clear, /type="button"/, 'clear never submits the enclosing form');
const toggle = render(React.createElement(Switch, { label: 'Pause downloads', name: 'paused', checked: true, onCheckedChange() {} }));
const toggleInput = toggle.match(/<input[^>]*type="checkbox"[^>]*>/)?.[0];
assert.ok(toggleInput); assert.match(toggleInput, /role="switch"/); assert.match(toggleInput, /aria-label="Pause downloads"/); assert.match(toggleInput, /checked=""/);
const select = render(React.createElement(NativeSelect, { name: 'source', 'aria-label': 'Connection type', value: 'xtream', onChange() {} },
  React.createElement('option', { value: 'm3u' }, 'M3U playlist'), React.createElement('option', { value: 'xtream' }, 'Xtream login')));
const combobox = select.match(/<input[^>]*role="combobox"[^>]*>/)?.[0];
assert.ok(combobox); assert.match(combobox, /aria-label="Connection type"/); assert.match(combobox, /value="Xtream login"/);
const namedValue=(select.match(/<input[^>]*>/g)||[]).find(tag=>tag.includes('name="source"'));
assert.match(namedValue, /type="hidden"/); assert.match(namedValue, /value="xtream"/, 'named form value preserves the option ID');
assert.doesNotMatch(select, /<select\b|<option\b/, 'dropdown menus render within Mantine rather than an OS popup');
assert.match(select, /min-height:44px/, 'styled dropdown shares the 44px control height');
assert.deepEqual(JSON.parse(JSON.stringify(normalizeSelectData({children:[React.createElement('option',{value:'m3u'},'M3U playlist'),React.createElement('option',{value:'xtream'},'Xtream login')]}))).map(option=>[option.value,option.label]),[['m3u','M3U playlist'],['xtream','Xtream login']]);
console.log('Real Mantine controls: form submission, clear actions, switch names/state and styled dropdown values and form selection passed.');

const { Toggle, Row } = load('SettingsUI.tsx', { './api': {}, './components/ui': { Button, Switch }, './components/layout': load('components/layout.tsx'), './notifications': {} });
const changeQuality = () => {};
const qualityProps={value:'1080',data:[{value:'1080',label:'1080p'}],onChange:changeQuality};
const qualityRow = Row({title:'Maximum quality',children:React.createElement(NativeSelect,qualityProps)});
const qualityControl=qualityRow.props.children;
assert.equal(qualityControl.props.onChange,changeQuality); assert.equal(qualityControl.props.value,'1080','adding an accessible name preserves the controlled value');
const qualityHtml=render(qualityRow);
assert.match(qualityHtml.match(/<input[^>]*role="combobox"[^>]*>/)?.[0],/aria-label="Maximum quality"/,'a row title names its styled dropdown');
const explicitHtml=render(React.createElement(Row,{title:'Maximum quality'},React.createElement(NativeSelect,{...qualityProps,'aria-label':'Preferred video quality'})));
assert.match(explicitHtml.match(/<input[^>]*role="combobox"[^>]*>/)?.[0],/aria-label="Preferred video quality"/,'an explicit caller name remains authoritative');
const describedRow=Row({title:'Maximum quality',children:React.createElement(NativeSelect,{...qualityProps,'aria-labelledby':'external-quality-label'})});
assert.equal(describedRow.props.children.props['aria-label'],undefined,'external label references are preserved without adding a competing name');
const statusRow=Row({title:'Status',children:React.createElement('span',null,'Connected')});
assert.equal(statusRow.props.children[1].props.children.props['aria-label'],undefined,'status text is not mislabelled as a form control');
const actionRow=Row({title:'Connection',children:React.createElement(Button,{onClick(){}},'Connect')});
assert.equal(actionRow.props.children[1].props.children.props['aria-label'],undefined,'action buttons keep their own accessible text');
const longToggle = render(React.createElement(Toggle, { title: 'Automatically download future uploads from this channel', detail: 'Help remains available.', value: true, onChange() {} }));
const toggleRows = longToggle.match(/<div[^>]*class="[^"]*mantine-Group-root[^"]*"[^>]*>/g) || [];
assert.match(toggleRows[0], /--group-wrap:nowrap/, 'the switch stays beside its label on narrow phones');
assert.match(longToggle, /flex:1;min-width:0/, 'long toggle titles can wrap into the remaining space');
assert.ok((longToggle.match(/flex-shrink:0/g) || []).length >= 1, 'the switch retains its own space');
assert.doesNotMatch(longToggle,/aria-label="More information"/,'explanations stay under the title rather than changing label-row height');
assert.match(longToggle, /role="switch"/); assert.match(longToggle, /aria-label="Automatically download future uploads from this channel"/);

// Form labels and descriptions share one explicit order in the rebuilt UI.
const { Field } = load('SettingsUI.tsx', { './api': {}, './components/ui': { Button, Switch }, './components/layout': load('components/layout.tsx'), './notifications': {} });
const grammar=render(React.createElement(Field,{label:'Server address',help:'Use a URL reachable from ReelDock.'},React.createElement(TextInput,{name:'server',defaultValue:'https://fixture.test'})));
assert.ok(grammar.indexOf('Server address')<grammar.indexOf('name="server"'),'labels precede their fields');
assert.ok(grammar.indexOf('Use a URL reachable from ReelDock.')>grammar.indexOf('name="server"'),'descriptions follow their fields');
assert.doesNotMatch(grammar,/aria-label="More information"/,'help text never adds a taller label control');
console.log('Rebuilt shared controls: field naming, label/input/description order and aligned toggles passed.');
