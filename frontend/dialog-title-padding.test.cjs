const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const ts = require('typescript');
const React = require('react');
const Mantine = require('@mantine/core');
const { renderToStaticMarkup } = require('react-dom/server');

function load(file, dependencies = {}, expose = '') {
  const exports = {};
  vm.runInNewContext(ts.transpileModule(fs.readFileSync(__dirname + '/src/' + file, 'utf8'), {
    compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, esModuleInterop: true },
  }).outputText + expose, {
    exports,
    require: name => name.endsWith('.module.css')?{__esModule:true,default:{surface:'surface',tile:'tile',preview:'preview',bannerInformation:'bannerInformation'}}:Object.hasOwn(dependencies, name) ? dependencies[name] : require(name),
  });
  return exports;
}

// Exercise the real shared wrapper inside its real content context and real
// Mantine Modal.Title. Replacing DialogTitle with Mantine.Title hid this bug.
const ui = load('components/ui.tsx', {
  '../notifications-state': { registerNotificationHost: () => () => {} },
  '../ScrollToTop': { __esModule: true, default: () => null },
}, '\nexports.TestContentContext = ContentContext;');
const { Header } = load('components/DetailSheet.tsx', {
  './ui': ui,
  './SelectField': { SelectField: () => null },
  '../channelNavigation': { channelEvent: 'unused-test-channel-event' },
}, '\nexports.Header = DetailHeader;');

function render(child, scheme = 'dark', width = 320) {
  return renderToStaticMarkup(React.createElement(Mantine.MantineProvider, { forceColorScheme: scheme },
    React.createElement(Mantine.Modal.Root, { opened: true, onClose() {}, withinPortal: false },
      React.createElement(ui.TestContentContext.Provider, { value: true },
        React.createElement(Mantine.Box, { w: width }, child)))));
}

function headingStyle(html, marker) {
  const tag = html.match(new RegExp('<h2\\b[^>]*' + marker + '[^>]*>'));
  assert.ok(tag, 'The shared title must render Mantine Modal.Title as an h2');
  return tag[0].match(/style="([^"]*)"/)?.[1] || '';
}

function padding(style) {
  return style.match(/(?:^|;)padding-right:([^;]+)/)?.[1];
}

for (const scheme of ['dark', 'light']) {
  const zero = render(React.createElement(ui.DialogTitle, { 'data-title-zero': '', style: { paddingRight: 0, overflowWrap: 'anywhere' } }, 'A full-width title'), scheme);
  assert.match(padding(headingStyle(zero, 'data-title-zero')), /^0(?:px|rem)?$/, 'An explicit zero must survive Mantine spacing props');
  assert.match(headingStyle(zero, 'data-title-zero'), /overflow-wrap:anywhere/, 'Other caller styles remain intact');

  const standard = render(React.createElement(ui.DialogTitle, { 'data-title-default': '' }, 'A regular dialog'), scheme);
  assert.match(padding(headingStyle(standard, 'data-title-default')), /4rem|64px/, 'Regular dialog titles reserve 64px for the close button');

  for (const width of [320, 768, 1440]) {
    for (const banner of ['/fixture-banner.jpg', undefined]) {
      const html = render(React.createElement(Header, {
        title: 'How a long video title wraps across the available information width',
        subtitle: 'YouTube', banner,
        media: React.createElement('img', { src: '/fixture-poster.jpg', alt: '' }),
        channel: React.createElement(Mantine.Button, { mih: 44 }, 'Source channel'),
      }), scheme, width);
      const actual = padding(headingStyle(html, 'data-detail-title'));
      if (banner) assert.match(actual, /^0(?:px|rem)?$/, 'The banner moves title content below the close button, so no duplicate reserve is applied');
      else assert.match(actual, /4rem|64px/, 'A title without a banner retains the close-button reserve');
      assert.equal((html.match(/data-detail-title=/g) || []).length, 1);
      assert.equal((html.match(/data-detail-channel=/g) || []).length, 1);
    }
  }
}
console.log('Actual DialogTitle + Mantine Modal.Title preserve explicit zero padding and the default close-button reserve, including shared detail headers.');
