const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const ts = require('typescript');
const { test } = require('node:test');

const compiled = ts.transpileModule(
  fs.readFileSync(path.join(__dirname, 'src/activityLog.ts'), 'utf8'),
  { compilerOptions: { module: ts.ModuleKind.CommonJS } },
).outputText;

function browser(failure) {
  const events = [], blobs = [], timers = [], anchors = [], connected = new Set(), revoked = [];
  const out = {};
  vm.runInNewContext(compiled, {
    exports: out,
    Blob,
    require(name) {
      assert.equal(name, './api');
      return { api() { throw new Error('Saving an already prepared log must not contact an API.'); } };
    },
    fetch() { throw new Error('Saving a log must not make a network request.'); },
    URL: {
      createObjectURL(blob) {
        assert.ok(blob instanceof Blob);
        blobs.push(blob);
        const url = 'blob:isolated-log-' + blobs.length;
        events.push(['create', url]);
        return url;
      },
      revokeObjectURL(url) { revoked.push(url); events.push(['revoke', url]); },
    },
    document: {
      createElement(tag) {
        assert.equal(tag, 'a', 'the browser receives a downloadable attachment link');
        const anchor = {
          style: {},
          click() {
            assert.ok(connected.has(anchor), 'the attachment is in the document when clicked');
            assert.equal(anchor.style.display, 'none', 'the temporary link adds no visible UI');
            assert.ok(!revoked.includes(anchor.href), 'the object URL stays valid during the download click');
            events.push(['click', anchor.href, anchor.download]);
            if (failure === 'click') throw new Error('Download click failed.');
          },
          remove() { connected.delete(anchor); events.push(['remove', anchor.href]); },
        };
        anchors.push(anchor);
        return anchor;
      },
      body: {
        appendChild(anchor) {
          events.push(['append', anchor.href]);
          if (failure === 'append') throw new Error('Document unavailable.');
          connected.add(anchor);
        },
      },
    },
    setTimeout(callback, delay) {
      timers.push({ callback, delay });
      events.push(['defer', delay]);
      return timers.length;
    },
  });
  return { save: out.saveActivityLog, events, blobs, timers, anchors, connected, revoked };
}

test('Activity saves the complete log as a UTF-8 text attachment without navigation or network', async () => {
  const ui = browser();
  const text = 'ReelDock Activity log\nEntries: 2\n1. Café ⭐\nDetails:\n  Saved 日本語\n2. Programme S01E02\n';
  const filename = 'ReelDock-Activity-20261024T231500Z.txt';
  assert.equal(ui.save({ text, filename, count: 2 }), undefined);
  assert.equal(ui.blobs.length, 1);
  assert.equal(ui.blobs[0].type, 'text/plain;charset=utf-8');
  const bytes = Buffer.from(await ui.blobs[0].arrayBuffer());
  assert.deepEqual([...bytes.subarray(0, 3)], [0xef, 0xbb, 0xbf], 'BOM makes UTF-8 clear to desktop text editors');
  assert.equal(bytes.subarray(3).toString('utf8'), text, 'all Unicode, notes and line breaks survive the saved file');
  assert.equal(ui.anchors[0].download, filename);
  assert.equal(ui.anchors[0].href, 'blob:isolated-log-1');
  assert.deepEqual(ui.events.map(event => event[0]), ['create', 'append', 'click', 'remove', 'defer']);
  assert.equal(ui.connected.size, 0, 'the temporary download link is removed immediately');
  assert.deepEqual(ui.revoked, [], 'the URL is not revoked before the browser can consume it');
  assert.equal(ui.timers.length, 1);
  assert.equal(ui.timers[0].delay, 1000);
  ui.timers[0].callback();
  assert.deepEqual(ui.revoked, ['blob:isolated-log-1'], 'the object URL is released after download initiation');
});

test('an empty filtered Activity log still downloads the prepared truthful text', async () => {
  const ui = browser();
  const text = 'ReelDock Activity log\nStatus filter: Failed\nEntries: 0\n\nNo matching activity is recorded.\n';
  ui.save({ text, filename: 'ReelDock-Activity-20261024T231501Z.txt', count: 0 });
  assert.equal((await ui.blobs[0].text()), text);
  assert.equal(ui.events.filter(event => event[0] === 'click').length, 1);
  assert.equal(ui.connected.size, 0);
  ui.timers[0].callback();
  assert.equal(ui.revoked.length, 1);
});

for (const failure of ['click', 'append']) {
  test(`download ${failure} failure propagates while removing the anchor and releasing the Blob URL`, () => {
    const ui = browser(failure);
    assert.throws(() => ui.save({ text: 'Prepared log', filename: 'ReelDock-Activity-fixture.txt', count: 1 }),
      failure === 'click' ? /Download click failed/ : /Document unavailable/);
    assert.equal(ui.connected.size, 0);
    assert.equal(ui.events.filter(event => event[0] === 'remove').length, 1);
    assert.equal(ui.events.filter(event => event[0] === 'click').length, failure === 'click' ? 1 : 0);
    assert.equal(ui.timers.length, 1, 'cleanup is scheduled even when the DOM action fails');
    assert.deepEqual(ui.revoked, []);
    ui.timers[0].callback();
    assert.deepEqual(ui.revoked, ['blob:isolated-log-1']);
    assert.equal(ui.blobs.length, 1, 'failure does not trigger another download attempt');
  });
}
